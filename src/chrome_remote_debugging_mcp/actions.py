"""Perform one page action through real CDP input events.

Actions address a node by its ``backendNodeId``, resolved to page coordinates
via ``DOM.getBoxModel``, and are dispatched with ``Input.*`` so the events carry
``isTrusted: true``. Synthetic JavaScript events are deliberately not used: a
site that checks ``isTrusted`` would ignore them.

Failures raise :class:`ActionError` with a message meant for the agent; the
tool layer converts it to ``{"error": ...}``.
"""

from __future__ import annotations

import asyncio
import json
import time

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
    except cdp.CDPError as exc:
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


# Virtual key codes for the keys an agent realistically presses. Chrome needs
# the code as well as the name for the key to reach the page.
_KEYS = {
    "Enter": (13, "Enter"), "Tab": (9, "Tab"), "Escape": (27, "Escape"),
    "Backspace": (8, "Backspace"), "Delete": (46, "Delete"),
    "ArrowUp": (38, "ArrowUp"), "ArrowDown": (40, "ArrowDown"),
    "ArrowLeft": (37, "ArrowLeft"), "ArrowRight": (39, "ArrowRight"),
    "Home": (36, "Home"), "End": (35, "End"),
    "PageUp": (33, "PageUp"), "PageDown": (34, "PageDown"),
    "Space": (32, " "),
}

_SET_SELECT_VALUE = """
function (wanted) {
  const option = Array.from(this.options)
    .find(o => o.value === wanted || o.text.trim() === wanted);
  if (!option) return false;
  this.value = option.value;
  this.dispatchEvent(new Event('input', {bubbles: true}));
  this.dispatchEvent(new Event('change', {bubbles: true}));
  return true;
}
"""

_READ_CHECKED = "function () { return Boolean(this.checked); }"


async def _call_on_node(ws_url: str, ref: int, backend_id: int,
                        declaration: str, arguments: list | None = None):
    """Run a function with the node as ``this`` and return its value."""
    try:
        resolved = await cdp.send(ws_url, "DOM.resolveNode",
                                  {"backendNodeId": backend_id})
        result = await cdp.send(ws_url, "Runtime.callFunctionOn", {
            "objectId": resolved["object"]["objectId"],
            "functionDeclaration": declaration,
            "arguments": arguments or [],
            "returnByValue": True,
        })
    except cdp.CDPError as exc:
        raise ActionError(f"ref {ref} is stale; take a new snapshot ({exc})")
    return result.get("result", {}).get("value")


async def type_text(ws_url: str, ref: int, backend_id: int, text: str,
                    clear: bool = False) -> None:
    """Focus the field by clicking it, optionally clear it, then insert ``text``."""
    await click(ws_url, ref, backend_id)
    if clear:
        for event_type in ("keyDown", "keyUp"):
            await cdp.send(ws_url, "Input.dispatchKeyEvent", {
                "type": event_type, "key": "a", "code": "KeyA",
                "windowsVirtualKeyCode": 65, "modifiers": 2,
                "commands": ["selectAll"],
            })
    await cdp.send(ws_url, "Input.insertText", {"text": text})


async def press(ws_url: str, key: str) -> None:
    """Send one key press to whatever currently has focus."""
    if key not in _KEYS:
        raise ActionError(
            f"unsupported key {key!r}; supported: {', '.join(sorted(_KEYS))}"
        )
    code, text = _KEYS[key]
    key_value = text if len(text) == 1 else key
    for event_type in ("keyDown", "keyUp"):
        await cdp.send(ws_url, "Input.dispatchKeyEvent", {
            "type": event_type, "key": key_value, "code": key,
            "windowsVirtualKeyCode": code, "nativeVirtualKeyCode": code,
            "text": text if event_type == "keyDown" and len(text) == 1 else "",
        })


async def select(ws_url: str, ref: int, backend_id: int, value: str) -> None:
    """Pick an option by value or visible text, firing ``input`` and ``change``.

    A ``<select>`` cannot be driven by clicking: the popup is browser chrome, not
    page content, so the value is set programmatically and the events a page
    listens for are dispatched explicitly.
    """
    ok = await _call_on_node(ws_url, ref, backend_id, _SET_SELECT_VALUE,
                             [{"value": value}])
    if not ok:
        raise ActionError(f"ref {ref} has no option matching {value!r}")


async def set_checked(ws_url: str, ref: int, backend_id: int,
                      target: bool) -> bool:
    """Bring a checkbox or radio to ``target``. Returns whether it clicked.

    The current state is read first because a blind click toggles, which would
    make the same batch behave differently depending on the page's state.
    """
    current = bool(await _call_on_node(ws_url, ref, backend_id, _READ_CHECKED))
    if current == target:
        return False
    await click(ws_url, ref, backend_id)
    return True


async def scroll(ws_url: str, ref: int | None, backend_id: int | None,
                 to: str | None) -> None:
    """Scroll a ref into view, or the page to its top or bottom."""
    if backend_id is not None:
        try:
            await cdp.send(ws_url, "DOM.scrollIntoViewIfNeeded",
                           {"backendNodeId": backend_id})
        except cdp.CDPError as exc:
            raise ActionError(f"ref {ref} is stale; take a new snapshot ({exc})")
        return
    if to not in ("top", "bottom"):
        raise ActionError('scroll target must be a ref, or to "top" or "bottom"')
    delta = 20000 if to == "bottom" else -20000
    await cdp.send(ws_url, "Input.dispatchMouseEvent", {
        "type": "mouseWheel", "x": 10, "y": 10, "deltaX": 0, "deltaY": delta,
    })


async def hover(ws_url: str, ref: int, backend_id: int) -> None:
    """Move the pointer over the node, for menus that open on hover."""
    x, y = await resolve_box(ws_url, ref, backend_id)
    await cdp.send(ws_url, "Input.dispatchMouseEvent",
                   {"type": "mouseMoved", "x": x, "y": y})


_POLL_INTERVAL = 0.15


async def settle(ws_url: str, timeout: float = 2.0) -> None:
    """Wait until the document reports ``complete``, or give up quietly.

    Deliberately never raises: a page that keeps a request open forever should
    not fail an otherwise valid batch. Waiting for *content* is the agent's job,
    through :func:`wait_for`.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            result = await cdp.send(ws_url, "Runtime.evaluate", {
                "expression": "document.readyState", "returnByValue": True,
            })
            if result.get("result", {}).get("value") == "complete":
                return
        except Exception:
            return
        if time.monotonic() >= deadline:
            return
        await asyncio.sleep(_POLL_INTERVAL)


async def wait_for(ws_url: str, text: str | None, ref_gone: int | None,
                   backend_id: int | None, timeout: float = 5.0) -> None:
    """Poll until ``text`` appears on the page, or the node behind a ref is gone."""
    if text is None and ref_gone is None:
        raise ActionError("wait_for needs text or ref_gone")

    deadline = time.monotonic() + timeout
    expression = None
    if text is not None:
        expression = f"document.body.innerText.includes({json.dumps(text, ensure_ascii=False)})"

    while True:
        if expression is not None:
            try:
                result = await cdp.send(ws_url, "Runtime.evaluate", {
                    "expression": expression, "returnByValue": True,
                })
                if result.get("result", {}).get("value"):
                    return
            except Exception:
                pass
        else:
            try:
                await cdp.send(ws_url, "DOM.getBoxModel",
                               {"backendNodeId": backend_id})
            except cdp.CDPError:
                return
        if time.monotonic() >= deadline:
            what = f"text {text!r}" if text is not None else f"ref {ref_gone} to disappear"
            raise ActionError(f"timed out after {timeout}s waiting for {what}")
        await asyncio.sleep(_POLL_INTERVAL)
