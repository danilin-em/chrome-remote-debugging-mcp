"""Perform one page action through real CDP input events.

Actions address a node by its ``backendNodeId``, resolved to page coordinates
via ``DOM.getBoxModel``, and are dispatched with ``Input.*`` so the events carry
``isTrusted: true``. Synthetic JavaScript events are deliberately not used: a
site that checks ``isTrusted`` would ignore them.

Failures raise :class:`ActionError` with a message meant for the agent; the
tool layer converts it to ``{"error": ...}``.
"""

from __future__ import annotations

from . import cdp


class ActionError(Exception):
    """Raised when an action cannot be performed, with an agent-readable cause."""


async def resolve_box(ws_url: str, ref: int, backend_id: int) -> tuple[float, float]:
    """Scroll the node into view and return its content-box centre.

    A node that no longer has a box model is gone from the page, which is how a
    stale ref surfaces (see D4 in the design document).
    """
    try:
        await cdp.send(ws_url, "DOM.scrollIntoViewIfNeeded",
                       {"backendNodeId": backend_id})
        box = await cdp.send(ws_url, "DOM.getBoxModel", {"backendNodeId": backend_id})
    except Exception as exc:
        raise ActionError(f"ref {ref} is stale; take a new snapshot ({exc})")
    quad = box["model"]["content"]
    return (quad[0] + quad[4]) / 2, (quad[1] + quad[5]) / 2


async def click(ws_url: str, ref: int, backend_id: int) -> None:
    """Click the node at its centre with a real press/release pair."""
    x, y = await resolve_box(ws_url, ref, backend_id)
    for event_type in ("mousePressed", "mouseReleased"):
        await cdp.send(ws_url, "Input.dispatchMouseEvent", {
            "type": event_type, "x": x, "y": y,
            "button": "left", "clickCount": 1,
        })
