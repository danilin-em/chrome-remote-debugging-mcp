"""Thin Chrome DevTools Protocol (CDP) client.

One job: talk to a running Chrome's remote-debugging endpoint. It can
enumerate targets over HTTP and issue a single CDP command over a target's
WebSocket. No MCP awareness lives here, which keeps it independently testable.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

import httpx
import websockets

# Monotonic ids for CDP command/response correlation. Module-level so a test
# can reset it via monkeypatch for deterministic frame assertions.
_ids = itertools.count(1)

# Deadline for one whole send() exchange (connect, command, matching reply).
# Without it a stalled transport — e.g. an ssh tunnel whose peer vanished but
# whose process has not yet noticed — would hang the tool call indefinitely.
# Generous because Runtime.evaluate with awaitPromise legitimately waits.
SEND_TIMEOUT = 30.0

# Deadline for the websocket handshake of the persistent browser connection.
# Separate from SEND_TIMEOUT: a slow command must not eat the connect budget,
# and vice versa.
CONNECT_TIMEOUT = 10.0


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


async def list_targets(cdp_url: str) -> list[dict]:
    """Return raw Chrome target objects via ``GET {cdp_url}/json/list``.

    Each target dict carries ``id``, ``type``, ``title``, ``url`` and, for
    attachable targets, ``webSocketDebuggerUrl``.
    """
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{cdp_url}/json/list", timeout=10.0)
        resp.raise_for_status()
        return resp.json()


async def get_version(cdp_url: str) -> dict:
    """Return Chrome/Browser info via ``GET {cdp_url}/json/version``.

    Cheapest CDP endpoint — used as a connectivity probe. Carries ``Browser``,
    ``Protocol-Version``, ``User-Agent``, and the browser-level websocket url.
    """
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{cdp_url}/json/version", timeout=10.0)
        resp.raise_for_status()
        return resp.json()


async def new_target(cdp_url: str) -> dict:
    """Open a new blank tab via ``PUT {cdp_url}/json/new?about:blank``.

    Returns the new target dict (same shape as a ``/json/list`` entry). Always
    opens ``about:blank`` — callers navigate with ``Page.navigate`` afterwards,
    which sidesteps escaping an arbitrary url into the query string. Chrome
    111+ refuses ``GET`` here, hence ``PUT``.
    """
    async with httpx.AsyncClient() as client:
        resp = await client.put(f"{cdp_url}/json/new?about:blank", timeout=10.0)
        resp.raise_for_status()
        return resp.json()


async def close_target(cdp_url: str, target_id: str) -> None:
    """Close a target via ``GET {cdp_url}/json/close/{target_id}``."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{cdp_url}/json/close/{target_id}", timeout=10.0)
        resp.raise_for_status()


async def send(ws_url: str, method: str, params: dict | None = None,
               *, timeout: float | None = None) -> dict:
    """Open ``ws_url``, run one CDP command, and return its ``result``.

    Unrelated CDP events (which have no matching ``id``) are skipped until the
    response for this command arrives. Raises :class:`CDPError` on a CDP error
    and :class:`CDPTimeout` if the whole exchange exceeds ``timeout`` seconds
    (default :data:`SEND_TIMEOUT`).
    """
    command = {"id": next(_ids), "method": method, "params": params or {}}
    limit = SEND_TIMEOUT if timeout is None else timeout
    try:
        return await asyncio.wait_for(_exchange(ws_url, command), limit)
    except asyncio.TimeoutError:
        raise CDPTimeout(f"no response to {method} within {limit}s") from None


async def _exchange(ws_url: str, command: dict) -> dict:
    async with websockets.connect(ws_url, max_size=None) as ws:
        await ws.send(json.dumps(command))
        while True:
            message = json.loads(await ws.recv())
            if message.get("id") != command["id"]:
                continue  # unrelated CDP event; keep waiting
            if "error" in message:
                error = message["error"]
                raise CDPError(error.get("message", str(error)))
            return message.get("result", {})


async def browser_ws_url(cdp_url: str) -> str:
    """Return the browser-level websocket url for the Chrome at ``cdp_url``.

    Taken from ``GET {cdp_url}/json/version``, with its host and port replaced
    by ``cdp_url``'s: behind an ssh forward Chrome reports its own (remote)
    port, but the socket must be opened on the local forwarded one.
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
    events are dropped. The connection is opened lazily by the first
    :meth:`call` and, once it dies, reopened by the next one — never retried
    on its own. ``ws_url`` is asked for the address on every (re)connect, so
    a respawned tunnel or a relaunched Chrome on a new port is picked up.
    """

    def __init__(self, ws_url: Callable[[], Awaitable[str]]) -> None:
        self._ws_url = ws_url
        self._ws = None  # live socket, or None when not connected
        self._reader: asyncio.Task | None = None
        self._connecting: asyncio.Task | None = None
        # Futures awaiting a reply, by command id. Callers await them through
        # asyncio.shield, so an entry is never cancelled under the reader; it
        # leaves the dict when the reader resolves it or its caller gives up.
        self._pending: dict[int, asyncio.Future] = {}

    async def call(self, method: str, params: dict | None = None, *,
                   session_id: str | None = None,
                   timeout: float | None = None) -> dict:
        """Run one CDP command and return its ``result`` (``{}`` if absent).

        ``session_id`` routes the command to an attached target. Raises
        :class:`CDPError` on a CDP error, :class:`CDPTimeout` if no reply
        arrives within ``timeout`` seconds (default :data:`SEND_TIMEOUT`;
        the connection stays open), and :class:`CDPDisconnected` if the
        connection closes first. Connect errors propagate unchanged.
        """
        ws = await self._connection()
        command = {"id": next(_ids), "method": method, "params": params or {}}
        if session_id is not None:
            command["sessionId"] = session_id
        limit = SEND_TIMEOUT if timeout is None else timeout
        reply = asyncio.get_running_loop().create_future()
        self._pending[command["id"]] = reply
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
            raise CDPError(error.get("message", str(error)))
        return message.get("result", {})

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

        Concurrent callers share one connect attempt. They wait on it through
        ``asyncio.shield``, so a cancelled caller does not cancel the attempt
        the others are waiting on.
        """
        if self._ws is not None:
            return self._ws
        if self._connecting is None:
            self._connecting = asyncio.ensure_future(self._connect())
        return await asyncio.shield(self._connecting)

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
                # Events, and late replies whose caller gave up, match nothing.
                reply = self._pending.pop(message.get("id"), None)
                if reply is not None:
                    reply.set_result(message)
        except Exception:
            pass  # the socket is gone; whatever the cause, handled below
        finally:
            # Mark the connection dead so the next call reconnects, and fail
            # every call still waiting on it.
            self._ws = self._reader = None
            pending, self._pending = self._pending, {}
            for reply in pending.values():
                reply.set_exception(CDPDisconnected(
                    "connection to Chrome closed before the reply arrived"))
