"""FastMCP server exposing Chrome control over CDP as MCP tools.

Run over stdio (the default transport) as a console script or via
``python -m chrome_remote_debugging_mcp``. The Chrome endpoint is read from the
``CDP_URL`` environment variable (default ``http://localhost:9222``).
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

from mcp.server.fastmcp import FastMCP

from . import actions, cdp
from . import browse as browse_engine
from .tunnel import SshLocalForwardTunnel


def _env_int(name: str) -> int | None:
    """Read an int env var; ``None`` when unset or blank."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    return int(raw)


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

mcp = FastMCP("chrome-remote-debugging-mcp")


class _TargetError(Exception):
    """Raised when a usable page target can't be resolved (reported as error)."""


async def _page_targets() -> list[dict]:
    """Return only the ``page`` targets from Chrome (i.e. real tabs)."""
    targets = await cdp.list_targets(CDP_URL)
    return [t for t in targets if t.get("type") == "page"]


async def _resolve_target(tab_id: str | None) -> tuple[dict, str]:
    """Resolve ``tab_id`` (or the first page tab) to ``(target, ws_url)``.

    Raises :class:`_TargetError` with a human-readable message when Chrome is
    unreachable, no tabs are open, the id is unknown, or the target is not
    attachable.
    """
    try:
        pages = await _page_targets()
    except Exception as exc:  # network / HTTP failure
        raise _TargetError(f"cannot reach Chrome at {CDP_URL}: {exc}")
    if not pages:
        raise _TargetError("no page targets open")

    if tab_id is None:
        target = pages[0]
    else:
        target = next((t for t in pages if t.get("id") == tab_id), None)
        if target is None:
            raise _TargetError(f"tab {tab_id} not found")

    ws_url = target.get("webSocketDebuggerUrl")
    if not ws_url:
        raise _TargetError("target has no webSocketDebuggerUrl (already attached?)")
    return target, ws_url


@mcp.tool()
async def ping() -> dict:
    """Check connectivity to Chrome's DevTools endpoint (``CDP_URL``).

    Returns ``{"connected": True, "cdp_url", "browser", "protocol"}`` when
    Chrome answers, or ``{"connected": False, "cdp_url", "error": ...}`` when it
    is unreachable.
    """
    try:
        info = await cdp.get_version(CDP_URL)
    except Exception as exc:
        return {"connected": False, "cdp_url": CDP_URL, "error": str(exc)}
    return {
        "connected": True,
        "cdp_url": CDP_URL,
        "browser": info.get("Browser"),
        "protocol": info.get("Protocol-Version"),
    }


@mcp.tool()
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
async def navigate(url: str, tab_id: str | None = None) -> dict:
    """Navigate a Chrome tab to ``url``.

    Uses the first page tab when ``tab_id`` is omitted. Returns
    ``{"tab_id", "url", "frameId"}`` on success, ``{"error": ...}`` otherwise.
    """
    try:
        target, ws_url = await _resolve_target(tab_id)
    except _TargetError as exc:
        return {"error": str(exc)}

    try:
        result = await cdp.send(ws_url, "Page.navigate", {"url": url})
    except cdp.CDPError as exc:
        return {"error": f"CDP error: {exc}"}
    except Exception as exc:
        return {"error": f"navigation failed: {exc}"}

    return {"tab_id": target.get("id"), "url": url, "frameId": result.get("frameId")}


@mcp.tool()
async def evaluate(expression: str, tab_id: str | None = None) -> dict:
    """Evaluate a JavaScript ``expression`` in a Chrome tab.

    Uses the first page tab when ``tab_id`` is omitted. Awaits promises and
    returns the value by value. Returns ``{"tab_id", "value", "type"}`` on
    success, or ``{"error": ...}`` on failure or an uncaught JS exception.
    """
    try:
        target, ws_url = await _resolve_target(tab_id)
    except _TargetError as exc:
        return {"error": str(exc)}

    try:
        result = await cdp.send(
            ws_url,
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
async def cdp_command(
    method: str, params: dict | None = None, tab_id: str | None = None
) -> dict:
    """Send a raw CDP command and return its raw result — escape hatch.

    Use when the higher-level tools don't cover what you need. ``method`` is any
    CDP method (e.g. ``"Page.captureScreenshot"``, ``"DOM.getDocument"``,
    ``"Network.enable"``); ``params`` are its parameters. Runs against the given
    tab (or the first page tab). Note: this targets a *page* websocket, so
    page-domain methods work; browser-level domains (``Browser.*``, ``Target.*``)
    do not. Returns ``{"tab_id", "method", "result"}`` or ``{"error": ...}``.
    """
    try:
        target, ws_url = await _resolve_target(tab_id)
    except _TargetError as exc:
        return {"error": str(exc)}

    try:
        result = await cdp.send(ws_url, method, params or {})
    except cdp.CDPError as exc:
        return {"error": f"CDP error: {exc}"}
    except Exception as exc:
        return {"error": f"CDP command failed: {exc}"}

    return {"tab_id": target.get("id"), "method": method, "result": result}


@mcp.tool()
async def browse(url: str, tab_id: str | None = None) -> dict:
    """Open ``url`` and return a readable view of the page, with numbered refs.

    The view is built from Chrome's accessibility tree — what a screen reader
    would announce — not from HTML, so it is compact enough to act on. Every
    interactive element gets a ``#N`` ref usable with ``browse_act``.

    Uses the first page tab when ``tab_id`` is omitted. Returns
    ``{"tab_id", "url", "view", "refs"}`` or ``{"error": ...}``.
    """
    try:
        target, ws_url = await _resolve_target(tab_id)
    except _TargetError as exc:
        return {"error": str(exc)}

    try:
        await cdp.send(ws_url, "Page.navigate", {"url": url})
    except Exception as exc:
        return {"error": f"navigation failed: {exc}"}

    await actions.settle(ws_url, timeout=10.0)
    try:
        view, refs, observed_url = await browse_engine.snapshot(ws_url, target.get("id"))
    except Exception as exc:
        return {"error": f"snapshot failed: {exc}"}
    # The post-navigation url the snapshot itself observed (M10), which can
    # differ from the requested url after a redirect.
    return {"tab_id": target.get("id"), "url": observed_url, "view": view, "refs": refs}


@mcp.tool()
async def browse_view(tab_id: str | None = None) -> dict:
    """Re-read the current page and return a fresh view with fresh refs.

    Refs from an earlier view stop being valid once this returns. Returns
    ``{"tab_id", "view", "refs"}`` or ``{"error": ...}``.
    """
    try:
        target, ws_url = await _resolve_target(tab_id)
    except _TargetError as exc:
        return {"error": str(exc)}
    try:
        view, refs, _ = await browse_engine.snapshot(ws_url, target.get("id"))
    except Exception as exc:
        return {"error": f"snapshot failed: {exc}"}
    return {"tab_id": target.get("id"), "view": view, "refs": refs}


@mcp.tool()
async def browse_act(actions_list: list[dict], tab_id: str | None = None) -> dict:
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
        target, ws_url = await _resolve_target(tab_id)
    except _TargetError as exc:
        return {"error": str(exc)}

    tab = target.get("id")
    try:
        steps, error = await browse_engine.run_actions(ws_url, tab, actions_list)
    except Exception as exc:
        # I5: run_actions was previously called unguarded — a latent breach of
        # the never-raise contract (unreachable today only because FastMCP's
        # own validation happens to keep it that way). Wrap it the same way
        # snapshot already is below.
        return {"tab_id": tab, "error": f"action batch failed: {exc}"}
    try:
        view, refs, _ = await browse_engine.snapshot(ws_url, tab)
    except Exception as exc:
        return {"tab_id": tab, "steps": steps, "error": f"snapshot failed: {exc}"}

    out = {"tab_id": tab, "steps": steps, "view": view, "refs": refs}
    if error:
        out["error"] = error
    return out


from . import debuglog  # noqa: E402  DEBUG ONLY — do not commit

debuglog.install(mcp)


def main() -> None:
    """Console-script entry point: run the MCP server over stdio.

    When ``SSH_PROXY_TO`` is set, open an ``ssh -L`` tunnel to the CDP endpoint
    first and repoint ``CDP_URL`` at the forwarded local port, so every tool
    (which reads ``CDP_URL`` at call time) transparently egresses through the
    remote host. The tunnel lives exactly as long as the server.
    """
    global CDP_URL
    tunnel: SshLocalForwardTunnel | None = None
    if SSH_PROXY_TO:
        host, port = _split_endpoint(CDP_URL)
        tunnel = SshLocalForwardTunnel(SSH_PROXY_TO, host, port, SSH_PROXY_PORT)
        tunnel.start()
        CDP_URL = tunnel.local_url
    try:
        mcp.run()
    finally:
        if tunnel is not None:
            tunnel.stop()


if __name__ == "__main__":
    main()
