"""Thin Chrome DevTools Protocol (CDP) client.

One job: talk to a running Chrome's remote-debugging endpoint. Every CDP
command — browser-level ones and those run in a tab's session — goes over one
persistent browser websocket; the only HTTP request is ``/json/version``, to
learn that socket's address. No MCP awareness lives here, which keeps it
independently testable.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import itertools
import json
from collections.abc import Awaitable, Callable
from typing import NamedTuple
from urllib.parse import urlsplit

import httpx
import websockets

# Monotonic ids for CDP command/response correlation. Module-level so a test
# can reset it via monkeypatch for deterministic frame assertions.
_ids = itertools.count(1)

# Deadline for a command's reply (not the connect). Without it a stalled
# transport — e.g. an ssh tunnel whose peer vanished but whose process has not
# yet noticed — would hang the tool call indefinitely. Generous because
# Runtime.evaluate with awaitPromise legitimately waits.
SEND_TIMEOUT = 30.0

# Deadline for the websocket handshake of the persistent browser connection.
# Separate from SEND_TIMEOUT: a slow command must not eat the connect budget,
# and vice versa.
CONNECT_TIMEOUT = 10.0

# CDP error code for a command sent to a session Chrome no longer has.
SESSION_NOT_FOUND = -32001


class CDPError(Exception):
    """Raised when Chrome returns a CDP-level ``error`` for a command."""


class CDPTimeout(TimeoutError):
    """Raised when a command gets no reply in time.

    Deliberately *not* a :class:`CDPError`: callers treat CDPError as "Chrome
    rejected this", and a silent transport is a different failure.
    """


class CDPDisconnected(ConnectionError):
    """Raised when the browser connection closed before a command's reply.

    Not a :class:`CDPError`, for the same reason as :class:`CDPTimeout`.
    """


class TargetDetached(Exception):
    """Raised when a tab's session went away before a command's reply.

    Not a :class:`CDPError`, for the same reason as :class:`CDPTimeout`.
    """


class _Scope:
    """What one call scope has learned about the connection (see call_scope)."""

    def __init__(self) -> None:
        self.ws = None  # socket the scope is bound to
        self.error: BaseException | None = None  # first connect failure


# The active call scope, or None. A mutable object shared by every task the
# scope's context is copied into (asyncio.gather and friends).
_scope: contextvars.ContextVar[_Scope | None] = contextvars.ContextVar(
    "cdp_scope", default=None)


@contextlib.contextmanager
def call_scope():
    """Limit the code inside to one connection to Chrome.

    Opening a connection can cost the user a permission prompt, and retry
    loops that swallow errors would open one per attempt. Inside a scope the
    first connect failure is re-raised for every later call, and a call after
    the scope's socket died raises :class:`CDPDisconnected` instead of
    reconnecting; only the next scope connects again.
    """
    token = _scope.set(_Scope())
    try:
        yield
    finally:
        _scope.reset(token)


async def browser_ws_url(cdp_url: str) -> str:
    """Return the browser-level websocket url for the Chrome at ``cdp_url``.

    Taken from ``GET {cdp_url}/json/version``, with its host and port replaced
    by ``cdp_url``'s. Defensive: whatever address Chrome reports, the socket
    always goes through ``cdp_url``'s host:port (Chrome only accepts a
    ``localhost``/``127.0.0.1`` Host header).
    """
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{cdp_url}/json/version", timeout=10.0)
        resp.raise_for_status()
        ws_url = urlsplit(resp.json()["webSocketDebuggerUrl"])
    return ws_url._replace(netloc=urlsplit(cdp_url).netloc).geturl()


class Browser:
    """One persistent connection to Chrome's browser-level websocket.

    Commands are multiplexed over it: each gets a fresh ``id`` and a future
    that a single reader task resolves when the matching reply arrives;
    events are dropped, except ``Target.detachedFromTarget``. Each tab gets
    one flattened session (:meth:`session`), reused by every command sent
    to it until the tab detaches or the connection dies. The connection is
    opened lazily by the first :meth:`call` and, once it dies, reopened by
    the next one — never retried on its own. ``ws_url`` is asked for the
    address on every (re)connect, so a respawned tunnel or a relaunched
    Chrome on a new port is picked up.
    """

    def __init__(self, ws_url: Callable[[], Awaitable[str]]) -> None:
        self._ws_url = ws_url
        self._ws = None  # live socket, or None when not connected
        self._reader: asyncio.Task | None = None
        self._connecting: asyncio.Task | None = None
        # (session id, future) of each call awaiting a reply, by command id.
        # Callers await the future through asyncio.shield, so an entry is
        # never cancelled under the reader; it leaves the dict when it is
        # resolved or its caller gives up.
        self._pending: dict[int, tuple[str | None, asyncio.Future]] = {}
        # Session id by target id, and in-flight attaches by target id. Both
        # belong to the current connection: its death swaps in fresh dicts,
        # so an attach still finishing on the dead socket cannot write into
        # the next connection's cache.
        self._sessions: dict[str, str] = {}
        self._attaching: dict[str, asyncio.Task] = {}

    async def call(self, method: str, params: dict | None = None, *,
                   session_id: str | None = None,
                   timeout: float | None = None) -> dict:
        """Run one CDP command and return its ``result`` (``{}`` if absent).

        ``session_id`` routes the command to an attached target. Raises
        :class:`CDPError` on a CDP error, :class:`CDPTimeout` if no reply
        arrives within ``timeout`` seconds (default :data:`SEND_TIMEOUT`;
        the connection stays open), :class:`CDPDisconnected` if the
        connection closes first, and :class:`TargetDetached` if
        ``session_id``'s tab detaches first. Connect errors propagate
        unchanged.
        """
        ws = await self._connection()
        command = {"id": next(_ids), "method": method, "params": params or {}}
        if session_id is not None:
            command["sessionId"] = session_id
        limit = SEND_TIMEOUT if timeout is None else timeout
        reply = asyncio.get_running_loop().create_future()
        self._pending[command["id"]] = (session_id, reply)
        try:
            try:
                await ws.send(json.dumps(command))
            except websockets.ConnectionClosed as exc:
                raise CDPDisconnected(
                    f"connection to Chrome closed before {method} was sent") from exc
            try:
                message = await asyncio.wait_for(asyncio.shield(reply), limit)
            except asyncio.TimeoutError:
                raise CDPTimeout(f"no response to {method} within {limit}s") from None
        finally:
            self._pending.pop(command["id"], None)
        if "error" in message:
            error = message["error"]
            text = error.get("message", str(error))
            if session_id is not None and error.get("code") == SESSION_NOT_FOUND:
                # The tab closed between the attach and this command.
                self._detach(session_id)
                raise TargetDetached(f"tab closed before {method} ran: {text}")
            raise CDPError(text)
        return message.get("result", {})

    async def session(self, target_id: str) -> str:
        """Return the session id for ``target_id``, attaching if needed.

        Concurrent callers share one attach. They wait on it through
        ``asyncio.shield``, so a cancelled caller does not cancel the attach
        the others are waiting on. Raises :class:`TargetDetached` if Chrome
        refuses the attach (the tab is gone); nothing is cached then.
        """
        session_id = self._sessions.get(target_id)
        if session_id is not None:
            return session_id
        if target_id not in self._attaching:
            self._attaching[target_id] = asyncio.ensure_future(
                self._attach(target_id, self._sessions, self._attaching))
        return await asyncio.shield(self._attaching[target_id])

    async def _attach(self, target_id: str, sessions: dict[str, str],
                      attaching: dict[str, asyncio.Task]) -> str:
        # ``sessions``/``attaching`` are the dicts of the connection this
        # attach started on; see __init__.
        try:
            result = await self.call("Target.attachToTarget",
                                     {"targetId": target_id, "flatten": True})
        except CDPError as exc:
            raise TargetDetached(f"tab {target_id} is gone: {exc}") from exc
        finally:
            del attaching[target_id]  # a failed attach is not reused
        sessions[target_id] = result["sessionId"]
        return result["sessionId"]

    def _detach(self, session_id: str) -> None:
        """Forget ``session_id`` and fail the calls still waiting in it."""
        for target_id in [t for t, s in self._sessions.items() if s == session_id]:
            del self._sessions[target_id]
        for command_id in [i for i, (s, _) in self._pending.items() if s == session_id]:
            _, reply = self._pending.pop(command_id)
            reply.set_exception(TargetDetached(
                "the tab detached before the reply arrived"))

    async def close(self) -> None:
        """Close the connection and stop its reader. Safe to call repeatedly."""
        ws, reader = self._ws, self._reader
        if ws is None:
            return
        reader.cancel()
        await asyncio.wait({reader})  # its cleanup fails pending calls
        await ws.close()

    async def _connection(self):
        """Return the live socket, connecting first if there is none.

        Concurrent callers share one connect attempt. They wait on it with
        ``asyncio.wait``, which a cancelled caller abandons without cancelling
        the attempt the others are waiting on. Inside a :func:`call_scope`
        it connects at most once (see there).
        """
        scope = _scope.get()
        if scope is None:
            return await self._open()
        if scope.error is not None:
            raise scope.error
        if scope.ws is not None:
            if self._ws is not scope.ws:
                raise CDPDisconnected("the connection to Chrome was lost during "
                                      "this tool call; call the tool again")
            return scope.ws
        try:
            scope.ws = await self._open()
        except Exception as exc:
            scope.error = exc
            raise
        return scope.ws

    async def _open(self):
        if self._ws is not None:
            return self._ws
        if self._connecting is None:
            self._connecting = asyncio.ensure_future(self._connect())
            # If every waiter gave up, nobody reads a failure: retrieve it so
            # asyncio does not log "Task exception was never retrieved".
            self._connecting.add_done_callback(
                lambda t: t.cancelled() or t.exception())
        connecting = self._connecting
        await asyncio.wait({connecting})
        return connecting.result()

    async def _connect(self):
        try:
            ws = await websockets.connect(
                await self._ws_url(), max_size=None, open_timeout=CONNECT_TIMEOUT)
        finally:
            self._connecting = None  # a failed attempt is not reused
        self._ws = ws
        self._reader = asyncio.ensure_future(self._read(ws))
        return ws

    async def _read(self, ws) -> None:
        try:
            while True:
                message = json.loads(await ws.recv())
                if message.get("method") == "Target.detachedFromTarget":
                    self._detach(message.get("params", {}).get("sessionId"))
                    continue
                # Other events, and late replies whose caller gave up, match
                # nothing.
                entry = self._pending.pop(message.get("id"), None)
                if entry is not None:
                    entry[1].set_result(message)
        except Exception:
            pass  # the socket is gone; whatever the cause, handled below
        finally:
            # Mark the connection dead so the next call reconnects (and
            # re-attaches), and fail every call still waiting on it.
            self._ws = self._reader = None
            self._sessions, self._attaching = {}, {}
            pending, self._pending = self._pending, {}
            for _, reply in pending.values():
                reply.set_exception(CDPDisconnected(
                    "connection to Chrome closed before the reply arrived"))


class Page(NamedTuple):
    """A tab, addressed through a :class:`Browser` connection."""

    browser: Browser
    target_id: str


async def send(page: Page, method: str, params: dict | None = None,
               *, timeout: float | None = None) -> dict:
    """Run one CDP command in ``page``'s session and return its ``result``.

    Attaches to the tab first if it has no session yet. Raises what
    :meth:`Browser.call` raises; a tab that is gone (refused attach, detach
    while waiting) raises :class:`TargetDetached`. ``timeout`` bounds only
    the wait for this command's reply.
    """
    session_id = await page.browser.session(page.target_id)
    if session_id not in page.browser._sessions.values():
        # Attached on a connection that died before this command went out:
        # attach again, on the new one.
        session_id = await page.browser.session(page.target_id)
    return await page.browser.call(method, params, session_id=session_id,
                                   timeout=timeout)
