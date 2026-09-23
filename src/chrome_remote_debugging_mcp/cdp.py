"""Thin Chrome DevTools Protocol (CDP) client.

One job: talk to a running Chrome's remote-debugging endpoint. It can
enumerate targets over HTTP and issue a single CDP command over a target's
WebSocket. No MCP awareness lives here, which keeps it independently testable.
"""

from __future__ import annotations

import itertools
import json

import httpx
import websockets

# Monotonic ids for CDP command/response correlation. Module-level so a test
# can reset it via monkeypatch for deterministic frame assertions.
_ids = itertools.count(1)


class CDPError(Exception):
    """Raised when Chrome returns a CDP-level ``error`` for a command."""


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


async def send(ws_url: str, method: str, params: dict | None = None) -> dict:
    """Open ``ws_url``, run one CDP command, and return its ``result``.

    Unrelated CDP events (which have no matching ``id``) are skipped until the
    response for this command arrives. Raises :class:`CDPError` on a CDP error.
    """
    command = {"id": next(_ids), "method": method, "params": params or {}}
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
