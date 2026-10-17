"""FastMCP server exposing Chrome control over CDP as MCP tools.

Run over stdio (the default transport) as a console script or via
``python -m chrome_remote_debugging_mcp``. The Chrome endpoint is read from the
``CDP_URL`` environment variable (default ``http://localhost:9222``); with
``CHROME_LAUNCH`` set, the server starts a local Chrome itself instead (see
``launcher.py``).
"""

from __future__ import annotations

import asyncio
import functools
import os
from urllib.parse import urlparse

from mcp.server.fastmcp import FastMCP

from . import actions, cdp
from . import browse as browse_engine
from .tunnel import SshLocalForwardTunnel
from .launcher import ChromeLauncher


def _env_int(name: str) -> int | None:
    """Read an int env var; ``None`` when unset or blank."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    return int(raw)


def _env_flag(name: str) -> bool:
    """True when the env var is ``1``/``true``/``yes``/``on`` (any case)."""
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


def _split_endpoint(url: str) -> tuple[str, int]:
    """Parse a CDP URL into the ``(host, port)`` an ``ssh -L`` forward targets.

    Defaults the port to 9222 when the URL omits it.
    """
    parsed = urlparse(url)
    return parsed.hostname or "localhost", parsed.port or 9222


# When SSH_PROXY_TO is set, CDP_URL is the *remote-side* endpoint that gets
# forwarded; main() rewrites CDP_URL to the local forwarded URL at startup.
CDP_URL = os.environ.get("CDP_URL", "http://localhost:9222")
SSH_PROXY_TO = os.environ.get("SSH_PROXY_TO") or None
SSH_PROXY_PORT = _env_int("SSH_PROXY_PORT")
# The live tunnel while main() is serving, else None.
_TUNNEL: SshLocalForwardTunnel | None = None
# CHROME_LAUNCH: start (or attach to) a local Chrome lazily instead of
# connecting to CDP_URL. Mutually exclusive with CDP_URL and SSH_PROXY_TO —
# "set" means present in the environment, not "differs from the default".
CHROME_LAUNCH = _env_flag("CHROME_LAUNCH")
_CDP_URL_SET = "CDP_URL" in os.environ
# The launcher while main() is serving with CHROME_LAUNCH, else None.
_LAUNCHER: ChromeLauncher | None = None
# The one persistent browser connection, created by the first _browser().
_BROWSER: cdp.Browser | None = None

mcp = FastMCP("chrome-remote-debugging-mcp")


class _TargetError(Exception):
    """Raised when a usable page target can't be resolved (reported as error)."""


async def _ensure_tunnel() -> None:
    """Respawn the ssh tunnel if it died; no-op when there is no tunnel.

    Runs in a worker thread because a respawn blocks for up to the tunnel's
    connect timeout. Raises ``RuntimeError`` if the respawn fails; callers
    already turn any exception here into an ``{"error": ...}`` payload.
    """
    if _TUNNEL is not None:
        await asyncio.to_thread(_TUNNEL.ensure_alive)


async def _ensure_chrome() -> None:
    """Make sure the auto-launched Chrome is up; no-op without CHROME_LAUNCH.

    Rewrites ``CDP_URL`` on every call: a relaunch (the user closed the
    window) lands on a new port, and the persistent connection reads ``CDP_URL`` when it (re)connects.
    Runs in a worker thread because a launch blocks for up to the startup
    timeout. Raises ``RuntimeError`` on failure; callers already turn any
    exception here into an ``{"error": ...}`` payload.
    """
    global CDP_URL
    if _LAUNCHER is not None:
        CDP_URL = await asyncio.to_thread(_LAUNCHER.ensure_alive)


def _browser() -> cdp.Browser:
    """The process-wide browser connection, created on first use.

    Its address provider reads ``CDP_URL`` at connect time, not now: a
    respawned tunnel or a relaunched Chrome rewrites ``CDP_URL``, and the
    next reconnect must follow it.
    """
    global _BROWSER
    if _BROWSER is None:
        _BROWSER = cdp.Browser(lambda: cdp.browser_ws_url(CDP_URL))
    return _BROWSER


async def _page_targets() -> list[dict]:
    """Return only the ``page`` targets from Chrome (i.e. real tabs).

    Each is reduced to ``{"id", "type", "title", "url"}``.
    """
    await _ensure_tunnel()
    await _ensure_chrome()
    result = await _browser().call("Target.getTargets")
    return [
        {"id": t.get("targetId"), "type": t.get("type"),
         "title": t.get("title"), "url": t.get("url")}
        for t in result["targetInfos"]
        if t.get("type") == "page"
    ]


async def _resolve_target(tab_id: str) -> tuple[dict, cdp.Page]:
    """Resolve ``tab_id`` to ``(target, page)``.

    Raises :class:`_TargetError` with a human-readable message when Chrome is
    unreachable or the id is unknown.
    """
    try:
        pages = await _page_targets()
    except Exception as exc:  # network / HTTP failure
        raise _TargetError(f"cannot reach Chrome at {CDP_URL}: {exc}")

    target = next((t for t in pages if t.get("id") == tab_id), None)
    if target is None:
        raise _TargetError(f"tab {tab_id} not found")
    return target, cdp.Page(_browser(), tab_id)


async def _open_target() -> tuple[dict, cdp.Page]:
    """Open a new blank tab and return ``(target, page)``."""
    try:
        await _ensure_tunnel()
        await _ensure_chrome()
        result = await _browser().call("Target.createTarget", {"url": "about:blank"})
        tab_id = result["targetId"]
    except Exception as exc:
        raise _TargetError(f"cannot open a new tab at {CDP_URL}: {exc}")
    target = {"id": tab_id, "type": "page", "title": "", "url": "about:blank"}
    return target, cdp.Page(_browser(), tab_id)


async def _target_or_new(tab_id: str | None) -> tuple[dict, cdp.Page]:
    """``tab_id``'s target, or a freshly opened tab when ``tab_id`` is None."""
    if tab_id is None:
        return await _open_target()
    return await _resolve_target(tab_id)


def _scoped(fn):
    """Run a tool inside :func:`cdp.call_scope`: one connection per call."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        with cdp.call_scope():
            return await fn(*args, **kwargs)
    return wrapper


@mcp.tool()
@_scoped
async def ping() -> dict:
    """Check connectivity to Chrome's DevTools endpoint (``CDP_URL``).

    Returns ``{"connected": True, "cdp_url", "browser", "protocol"}`` when
    Chrome answers, or ``{"connected": False, "cdp_url", "error": ...}`` when it
    is unreachable.
    """
    try:
        await _ensure_tunnel()
        await _ensure_chrome()
        info = await _browser().call("Browser.getVersion")
    except Exception as exc:
        return {"connected": False, "cdp_url": CDP_URL, "error": str(exc)}
    return {
        "connected": True,
        "cdp_url": CDP_URL,
        "browser": info.get("product"),
        "protocol": info.get("protocolVersion"),
    }


@mcp.tool()
@_scoped
async def list_tabs() -> dict:
    """List open Chrome page tabs.

    Returns ``{"tabs": [{id, title, url, type}, ...]}`` or ``{"error": ...}``
    if Chrome is unreachable.
    """
    try:
        pages = await _page_targets()
    except Exception as exc:
        return {"error": f"cannot reach Chrome at {CDP_URL}: {exc}"}
    return {
        "tabs": [
            {
                "id": t.get("id"),
                "title": t.get("title"),
                "url": t.get("url"),
                "type": t.get("type"),
            }
            for t in pages
        ]
    }


@mcp.tool()
@_scoped
async def navigate(url: str, tab_id: str | None = None) -> dict:
    """Navigate a Chrome tab to ``url``.

    Opens a new tab when ``tab_id`` is omitted; pass the returned ``tab_id`` to
    later calls to keep working in it. Returns ``{"tab_id", "url", "frameId"}``
    on success, ``{"error": ...}`` otherwise.
    """
    try:
        target, page = await _target_or_new(tab_id)
    except _TargetError as exc:
        return {"error": str(exc)}

    try:
        result = await cdp.send(page, "Page.navigate", {"url": url})
    except cdp.CDPError as exc:
        return {"error": f"CDP error: {exc}"}
    except Exception as exc:
        return {"error": f"navigation failed: {exc}"}

    return {"tab_id": target.get("id"), "url": url, "frameId": result.get("frameId")}


@mcp.tool()
@_scoped
async def evaluate(expression: str, tab_id: str) -> dict:
    """Evaluate a JavaScript ``expression`` in a Chrome tab.

    Awaits promises and
    returns the value by value. Returns ``{"tab_id", "value", "type"}`` on
    success, or ``{"error": ...}`` on failure or an uncaught JS exception.
    A promise that does not settle within 30 s is reported as an error.
    """
    try:
        target, page = await _resolve_target(tab_id)
    except _TargetError as exc:
        return {"error": str(exc)}

    try:
        result = await cdp.send(
            page,
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
    except cdp.CDPError as exc:
        return {"error": f"CDP error: {exc}"}
    except Exception as exc:
        return {"error": f"evaluate failed: {exc}"}

    if "exceptionDetails" in result:
        details = result["exceptionDetails"]
        text = (
            details.get("exception", {}).get("description")
            or details.get("text")
            or "JS exception"
        )
        return {"error": f"JS exception: {text}"}

    value = result.get("result", {})
    return {"tab_id": target.get("id"), "value": value.get("value"), "type": value.get("type")}


@mcp.tool()
@_scoped
async def cdp_command(method: str, tab_id: str, params: dict | None = None) -> dict:
    """Send a raw CDP command and return its raw result — escape hatch.

    Use when the higher-level tools don't cover what you need. ``method`` is any
    CDP method (e.g. ``"Page.captureScreenshot"``, ``"DOM.getDocument"``,
    ``"Network.enable"``); ``params`` are its parameters. Runs in the given
    tab's persistent session, so ``objectId``/``nodeId`` values and domain
    state (e.g. ``Network.enable``) carry over between calls while the tab is
    open and the connection lives.

    Events are not delivered. So ``Fetch.enable``, ``Debugger.pause``,
    ``Target.setAutoAttach {waitForDebuggerOnStart: true}`` and
    ``Page.setInterceptFileChooserDialog {enabled: true}`` hang the tab until
    undone (``Fetch.disable``, ``Debugger.resume``,
    ``…{waitForDebuggerOnStart: false}``, ``…{enabled: false}``) or the tab is
    closed. ``Browser.*`` and ``Target.*`` are not supported; their results
    are undefined. Returns ``{"tab_id", "method", "result"}`` or
    ``{"error": ...}``.
    """
    try:
        target, page = await _resolve_target(tab_id)
    except _TargetError as exc:
        return {"error": str(exc)}

    try:
        result = await cdp.send(page, method, params or {})
    except cdp.CDPError as exc:
        return {"error": f"CDP error: {exc}"}
    except Exception as exc:
        return {"error": f"CDP command failed: {exc}"}

    return {"tab_id": target.get("id"), "method": method, "result": result}


@mcp.tool()
@_scoped
async def browse(url: str, tab_id: str | None = None) -> dict:
    """Open ``url`` and return a readable view of the page, with numbered refs.

    The view is built from Chrome's accessibility tree — what a screen reader
    would announce — not from HTML, so it is compact enough to act on. Every
    interactive element gets a ``#N`` ref usable with ``browse_act``.

    Opens a new tab when ``tab_id`` is omitted; pass the returned ``tab_id`` to
    ``browse_act`` / ``browse_view``. Returns
    ``{"tab_id", "url", "view", "refs"}`` or ``{"error": ...}``.
    """
    try:
        target, page = await _target_or_new(tab_id)
    except _TargetError as exc:
        return {"error": str(exc)}

    try:
        await cdp.send(page, "Page.navigate", {"url": url})
    except Exception as exc:
        return {"error": f"navigation failed: {exc}"}

    await actions.settle(page, timeout=10.0)
    try:
        view, refs, observed_url = await browse_engine.snapshot(page, target.get("id"))
    except Exception as exc:
        return {"error": f"snapshot failed: {exc}"}
    # The post-navigation url the snapshot itself observed (M10), which can
    # differ from the requested url after a redirect.
    return {"tab_id": target.get("id"), "url": observed_url, "view": view, "refs": refs}


@mcp.tool()
@_scoped
async def browse_view(tab_id: str) -> dict:
    """Re-read the current page and return a fresh view with fresh refs.

    Refs from an earlier view stop being valid once this returns. Returns
    ``{"tab_id", "view", "refs"}`` or ``{"error": ...}``.
    """
    try:
        target, page = await _resolve_target(tab_id)
    except _TargetError as exc:
        return {"error": str(exc)}
    try:
        view, refs, _ = await browse_engine.snapshot(page, target.get("id"))
    except Exception as exc:
        return {"error": f"snapshot failed: {exc}"}
    return {"tab_id": target.get("id"), "view": view, "refs": refs}


@mcp.tool()
@_scoped
async def browse_act(actions_list: list[dict], tab_id: str) -> dict:
    """Run several actions against the current view, then return one new view.

    Each action is a dict with a ``do`` key:

    - ``{"do": "click", "ref": 3}``
    - ``{"do": "type", "ref": 2, "text": "коты", "clear": false}``
    - ``{"do": "press", "key": "Enter"}``
    - ``{"do": "select", "ref": 5, "value": "два"}``
    - ``{"do": "check", "ref": 9}`` / ``{"do": "uncheck", "ref": 9}``
    - ``{"do": "scroll", "ref": 7}`` or ``{"do": "scroll", "to": "bottom"}``
    - ``{"do": "hover", "ref": 4}``
    - ``{"do": "wait_for", "text": "Результаты", "timeout": 5}`` or
      ``{"do": "wait_for", "ref_gone": 3}``

    Refs come from the most recent ``browse`` or ``browse_view`` on this tab and
    are invalid afterwards. The batch stops at the first failure; a view is
    returned either way so you can see where the page ended up.

    Returns ``{"tab_id", "steps", "view", "refs"}``, plus ``"error"`` when a
    step failed, or ``{"error": ...}`` when the tab could not be resolved.
    """
    try:
        target, page = await _resolve_target(tab_id)
    except _TargetError as exc:
        return {"error": str(exc)}

    tab = target.get("id")
    try:
        steps, error = await browse_engine.run_actions(page, tab, actions_list)
    except Exception as exc:
        # I5: run_actions was previously called unguarded — a latent breach of
        # the never-raise contract (unreachable today only because FastMCP's
        # own validation happens to keep it that way). Wrap it the same way
        # snapshot already is below.
        return {"tab_id": tab, "error": f"action batch failed: {exc}"}
    try:
        view, refs, _ = await browse_engine.snapshot(page, tab)
    except Exception as exc:
        return {"tab_id": tab, "steps": steps, "error": f"snapshot failed: {exc}"}

    out = {"tab_id": tab, "steps": steps, "view": view, "refs": refs}
    if error:
        out["error"] = error
    return out


@mcp.tool()
@_scoped
async def close_tab(tab_id: str) -> dict:
    """Close a Chrome tab opened earlier (e.g. by ``browse`` / ``navigate``).

    Its refs are dropped along with it. Returns ``{"tab_id", "closed": True}``
    or ``{"error": ...}``.
    """
    try:
        await _resolve_target(tab_id)
    except _TargetError as exc:
        return {"error": str(exc)}
    try:
        await _browser().call("Target.closeTarget", {"targetId": tab_id})
    except Exception as exc:
        return {"error": f"cannot close tab {tab_id}: {exc}"}
    browse_engine.forget(tab_id)
    return {"tab_id": tab_id, "closed": True}


from . import debuglog  # noqa: E402

debuglog.install(mcp)


def main() -> None:
    """Console-script entry point: run the MCP server over stdio.

    When ``SSH_PROXY_TO`` is set, open an ``ssh -L`` tunnel to the CDP endpoint
    first and repoint ``CDP_URL`` at the forwarded local port, so every tool
    (the persistent browser connection reads ``CDP_URL`` whenever it
    (re)connects) transparently egresses through the remote host. The tunnel lives exactly as long as the server; if ssh dies
    in between, the next tool call respawns it on the same port.

    When ``CHROME_LAUNCH`` is set, create a :class:`ChromeLauncher` but do not
    launch yet: the first tool call that needs Chrome starts it (or attaches
    to the one already serving the profile). Chrome is closed on exit only if
    this server launched it.
    """
    global CDP_URL, _TUNNEL, _LAUNCHER
    if CHROME_LAUNCH and (_CDP_URL_SET or SSH_PROXY_TO):
        raise SystemExit(
            "CHROME_LAUNCH cannot be combined with CDP_URL or SSH_PROXY_TO: "
            "either launch a local Chrome or connect to an existing one"
        )
    tunnel: SshLocalForwardTunnel | None = None
    if SSH_PROXY_TO:
        host, port = _split_endpoint(CDP_URL)
        tunnel = SshLocalForwardTunnel(SSH_PROXY_TO, host, port, SSH_PROXY_PORT)
        tunnel.start()
        CDP_URL = tunnel.local_url
        _TUNNEL = tunnel
    launcher: ChromeLauncher | None = None
    if CHROME_LAUNCH:
        launcher = ChromeLauncher()
        _LAUNCHER = launcher
    try:
        mcp.run()
    finally:
        if tunnel is not None:
            _TUNNEL = None
            tunnel.stop()
        if launcher is not None:
            _LAUNCHER = None
            launcher.stop()


if __name__ == "__main__":
    main()
