"""Action dispatch, with the CDP layer stubbed — no real Chrome."""

import asyncio

import pytest

import chrome_remote_debugging_mcp.actions as actions
from chrome_remote_debugging_mcp import cdp

WS = "ws://fake"
BOX = {"model": {"content": [10, 20, 30, 20, 30, 40, 10, 40]}}


class Recorder:
    """Collects the CDP frames an action produces, replying from a script."""

    def __init__(self, replies=None, errors=None):
        self.calls = []
        self._replies = replies or {}
        self._errors = errors or {}

    async def send(self, ws_url, method, params=None):
        self.calls.append((method, params or {}))
        if method in self._errors:
            raise self._errors[method]
        return self._replies.get(method, {})

    def methods(self):
        return [m for m, _ in self.calls]


def _install(monkeypatch, recorder):
    monkeypatch.setattr(actions.cdp, "send", recorder.send)


def test_resolve_box_returns_the_content_box_centre(monkeypatch):
    rec = Recorder(replies={"DOM.getBoxModel": BOX})
    _install(monkeypatch, rec)
    x, y = asyncio.run(actions.resolve_box(WS, 3, 77))
    assert (x, y) == (20.0, 30.0)
    assert rec.methods() == ["DOM.scrollIntoViewIfNeeded", "DOM.getBoxModel"]
    assert rec.calls[0][1] == {"backendNodeId": 77}
    assert rec.calls[1][1] == {"backendNodeId": 77}


def test_resolve_box_reports_a_stale_ref(monkeypatch):
    rec = Recorder(errors={"DOM.getBoxModel": cdp.CDPError("Could not compute box model.")})
    _install(monkeypatch, rec)
    with pytest.raises(actions.ActionError) as excinfo:
        asyncio.run(actions.resolve_box(WS, 5, 77))
    assert "ref 5 is stale" in str(excinfo.value)


def test_resolve_box_reports_a_failed_scroll(monkeypatch):
    rec = Recorder(errors={"DOM.scrollIntoViewIfNeeded": cdp.CDPError("Node is detached")})
    _install(monkeypatch, rec)
    with pytest.raises(actions.ActionError) as excinfo:
        asyncio.run(actions.resolve_box(WS, 5, 77))
    assert "ref 5 is stale" in str(excinfo.value)


def test_click_dispatches_a_press_and_a_release(monkeypatch):
    rec = Recorder(replies={"DOM.getBoxModel": BOX})
    _install(monkeypatch, rec)
    asyncio.run(actions.click(WS, 1, 77))
    assert rec.methods() == [
        "DOM.scrollIntoViewIfNeeded", "DOM.getBoxModel",
        "Input.dispatchMouseEvent", "Input.dispatchMouseEvent",
    ]
    press, release = rec.calls[2][1], rec.calls[3][1]
    assert press["type"] == "mousePressed"
    assert release["type"] == "mouseReleased"
    assert press["x"] == 20.0 and press["y"] == 30.0
    assert press["button"] == "left" and press["clickCount"] == 1
    assert release["x"] == 20.0 and release["y"] == 30.0
    assert release["button"] == "left" and release["clickCount"] == 1


def test_resolve_box_propagates_transport_errors(monkeypatch):
    """Transport errors (not CDP errors) must propagate, not convert to stale-ref."""
    rec = Recorder(errors={"DOM.getBoxModel": OSError("Connection refused")})
    _install(monkeypatch, rec)
    with pytest.raises(OSError, match="Connection refused"):
        asyncio.run(actions.resolve_box(WS, 5, 77))


def test_type_text_focuses_then_inserts(monkeypatch):
    rec = Recorder(replies={"DOM.getBoxModel": BOX})
    _install(monkeypatch, rec)
    asyncio.run(actions.type_text(WS, 2, 77, "коты"))
    assert rec.methods() == [
        "DOM.scrollIntoViewIfNeeded", "DOM.getBoxModel",
        "Input.dispatchMouseEvent", "Input.dispatchMouseEvent",
        "Input.insertText",
    ]
    assert rec.calls[-1][1] == {"text": "коты"}


def test_type_text_clears_the_field_first_when_asked(monkeypatch):
    rec = Recorder(replies={"DOM.getBoxModel": BOX})
    _install(monkeypatch, rec)
    asyncio.run(actions.type_text(WS, 2, 77, "новое", clear=True))
    methods = rec.methods()
    assert "Input.dispatchKeyEvent" in methods
    select_all = [p for m, p in rec.calls
                  if m == "Input.dispatchKeyEvent" and p.get("commands")]
    assert select_all and select_all[0]["commands"] == ["selectAll"]
    assert methods[-1] == "Input.insertText"


def test_press_sends_a_key_down_and_key_up(monkeypatch):
    rec = Recorder()
    _install(monkeypatch, rec)
    asyncio.run(actions.press(WS, "Enter"))
    assert rec.methods() == ["Input.dispatchKeyEvent", "Input.dispatchKeyEvent"]
    down, up = rec.calls[0][1], rec.calls[1][1]
    assert down["type"] == "keyDown" and up["type"] == "keyUp"
    assert down["key"] == "Enter" and down["windowsVirtualKeyCode"] == 13


def test_press_rejects_an_unknown_key(monkeypatch):
    rec = Recorder()
    _install(monkeypatch, rec)
    with pytest.raises(actions.ActionError) as excinfo:
        asyncio.run(actions.press(WS, "Meta+Shift+Wat"))
    assert "unsupported key" in str(excinfo.value)


def test_press_space_sends_its_single_char_text_on_key_down_only(monkeypatch):
    """Enter's mapped text is multi-character, so it never exercises the
    ``len(text) == 1`` arm of the text-building conditional; Space's mapped
    text is a single space, so it takes that arm on keyDown and still sends
    an empty string on keyUp."""
    rec = Recorder()
    _install(monkeypatch, rec)
    asyncio.run(actions.press(WS, "Space"))
    down, up = rec.calls[0][1], rec.calls[1][1]
    assert down["type"] == "keyDown" and down["text"] == " "
    assert up["type"] == "keyUp" and up["text"] == ""


def test_press_space_sends_a_single_space_as_the_dom_key_value(monkeypatch):
    """The DOM ``KeyboardEvent.key`` for the space bar is a single space
    character, not the literal string "Space" — page code testing
    ``event.key === ' '`` must see the space. ``code`` stays "Space"."""
    rec = Recorder()
    _install(monkeypatch, rec)
    asyncio.run(actions.press(WS, "Space"))
    down, up = rec.calls[0][1], rec.calls[1][1]
    assert down["key"] == " " and up["key"] == " "
    assert down["code"] == "Space" and up["code"] == "Space"


def test_select_sets_the_value_and_fires_events(monkeypatch):
    rec = Recorder(replies={
        "DOM.resolveNode": {"object": {"objectId": "OBJ1"}},
        "Runtime.callFunctionOn": {"result": {"value": True}},
    })
    _install(monkeypatch, rec)
    asyncio.run(actions.select(WS, 4, 77, "два"))
    assert rec.methods() == ["DOM.resolveNode", "Runtime.callFunctionOn"]
    params = rec.calls[1][1]
    assert params["objectId"] == "OBJ1"
    assert params["arguments"] == [{"value": "два"}]
    assert "change" in params["functionDeclaration"]
    assert "input" in params["functionDeclaration"]


def test_select_reports_a_missing_option(monkeypatch):
    rec = Recorder(replies={
        "DOM.resolveNode": {"object": {"objectId": "OBJ1"}},
        "Runtime.callFunctionOn": {"result": {"value": False}},
    })
    _install(monkeypatch, rec)
    with pytest.raises(actions.ActionError) as excinfo:
        asyncio.run(actions.select(WS, 4, 77, "нет такого"))
    assert "no option" in str(excinfo.value)


def test_select_reports_a_stale_ref(monkeypatch):
    rec = Recorder(errors={"DOM.resolveNode": cdp.CDPError("Could not resolve node")})
    _install(monkeypatch, rec)
    with pytest.raises(actions.ActionError) as excinfo:
        asyncio.run(actions.select(WS, 4, 77, "два"))
    assert "ref 4 is stale" in str(excinfo.value)


def test_select_propagates_transport_errors(monkeypatch):
    """A dropped websocket must surface honestly, not as a stale-ref report."""
    rec = Recorder(errors={"DOM.resolveNode": OSError("Connection refused")})
    _install(monkeypatch, rec)
    with pytest.raises(OSError, match="Connection refused"):
        asyncio.run(actions.select(WS, 4, 77, "два"))


def test_set_checked_clicks_only_when_the_state_differs(monkeypatch):
    rec = Recorder(replies={
        "DOM.resolveNode": {"object": {"objectId": "OBJ1"}},
        "Runtime.callFunctionOn": {"result": {"value": False}},
        "DOM.getBoxModel": BOX,
    })
    _install(monkeypatch, rec)
    assert asyncio.run(actions.set_checked(WS, 9, 77, True)) is True
    assert "Input.dispatchMouseEvent" in rec.methods()


def test_set_checked_is_a_no_op_when_already_in_the_target_state(monkeypatch):
    rec = Recorder(replies={
        "DOM.resolveNode": {"object": {"objectId": "OBJ1"}},
        "Runtime.callFunctionOn": {"result": {"value": True}},
    })
    _install(monkeypatch, rec)
    assert asyncio.run(actions.set_checked(WS, 9, 77, True)) is False
    assert "Input.dispatchMouseEvent" not in rec.methods()


def test_scroll_to_a_ref_uses_scroll_into_view(monkeypatch):
    rec = Recorder()
    _install(monkeypatch, rec)
    asyncio.run(actions.scroll(WS, 3, 77, None))
    assert rec.methods() == ["DOM.scrollIntoViewIfNeeded"]


def test_scroll_to_bottom_and_top_use_the_wheel(monkeypatch):
    rec = Recorder()
    _install(monkeypatch, rec)
    asyncio.run(actions.scroll(WS, None, None, "bottom"))
    asyncio.run(actions.scroll(WS, None, None, "top"))
    assert rec.methods() == ["Input.dispatchMouseEvent", "Input.dispatchMouseEvent"]
    assert rec.calls[0][1]["deltaY"] > 0
    assert rec.calls[1][1]["deltaY"] < 0


def test_scroll_rejects_an_unknown_direction(monkeypatch):
    rec = Recorder()
    _install(monkeypatch, rec)
    with pytest.raises(actions.ActionError) as excinfo:
        asyncio.run(actions.scroll(WS, None, None, "sideways"))
    assert "scroll target" in str(excinfo.value)


def test_scroll_reports_a_stale_ref(monkeypatch):
    rec = Recorder(errors={"DOM.scrollIntoViewIfNeeded": cdp.CDPError("Node is detached")})
    _install(monkeypatch, rec)
    with pytest.raises(actions.ActionError) as excinfo:
        asyncio.run(actions.scroll(WS, 3, 77, None))
    assert "ref 3 is stale" in str(excinfo.value)


def test_scroll_propagates_transport_errors(monkeypatch):
    """A dropped websocket must surface honestly, not as a stale-ref report."""
    rec = Recorder(errors={"DOM.scrollIntoViewIfNeeded": OSError("Connection refused")})
    _install(monkeypatch, rec)
    with pytest.raises(OSError, match="Connection refused"):
        asyncio.run(actions.scroll(WS, 3, 77, None))


def test_hover_moves_the_mouse_without_clicking(monkeypatch):
    rec = Recorder(replies={"DOM.getBoxModel": BOX})
    _install(monkeypatch, rec)
    asyncio.run(actions.hover(WS, 3, 77))
    assert rec.methods()[-1] == "Input.dispatchMouseEvent"
    assert rec.calls[-1][1]["type"] == "mouseMoved"


def test_settle_returns_as_soon_as_the_document_is_complete(monkeypatch):
    rec = Recorder(replies={"Runtime.evaluate": {"result": {"value": "complete"}}})
    _install(monkeypatch, rec)
    asyncio.run(actions.settle(WS))
    assert rec.methods() == ["Runtime.evaluate"]


def test_settle_gives_up_quietly_when_the_document_never_completes(monkeypatch):
    """Strengthened per the deferred minor in the final review: asserting only
    ``rec.methods()`` is non-empty would pass for a poll-once-and-quit
    ``settle``. Require at least two polls, matching the same proof used for
    ``wait_for`` below (``test_wait_for_text_ignores_transient_errors_and_
    still_times_out``), so the loop's retry behaviour is actually pinned."""
    rec = Recorder(replies={"Runtime.evaluate": {"result": {"value": "loading"}}})
    _install(monkeypatch, rec)
    asyncio.run(actions.settle(WS, timeout=0.05))
    assert len(rec.calls) >= 2                 # it kept polling, not just once
    # and it did not raise


def test_settle_ignores_a_transport_error(monkeypatch):
    rec = Recorder(errors={"Runtime.evaluate": RuntimeError("socket closed")})
    _install(monkeypatch, rec)
    asyncio.run(actions.settle(WS, timeout=0.05))


def test_wait_for_text_succeeds_once_the_text_appears(monkeypatch):
    rec = Recorder(replies={"Runtime.evaluate": {"result": {"value": True}}})
    _install(monkeypatch, rec)
    asyncio.run(actions.wait_for(WS, "Результаты", None, None, timeout=1))
    assert "Runtime.evaluate" in rec.methods()
    assert "Результаты" in rec.calls[0][1]["expression"]


def test_wait_for_text_times_out(monkeypatch):
    rec = Recorder(replies={"Runtime.evaluate": {"result": {"value": False}}})
    _install(monkeypatch, rec)
    with pytest.raises(actions.ActionError) as excinfo:
        asyncio.run(actions.wait_for(WS, "Нет", None, None, timeout=0.05))
    assert "timed out" in str(excinfo.value)


def test_wait_for_text_ignores_transient_errors_and_still_times_out(monkeypatch):
    """The text-polling branch must swallow a failed Runtime.evaluate and keep
    polling rather than abort — only the deadline should end the wait."""
    rec = Recorder(errors={"Runtime.evaluate": RuntimeError("socket closed")})
    _install(monkeypatch, rec)
    with pytest.raises(actions.ActionError) as excinfo:
        asyncio.run(actions.wait_for(WS, "Нет", None, None, timeout=0.05))
    assert "timed out" in str(excinfo.value)
    assert len(rec.calls) >= 2                # it kept retrying, not just once


def test_wait_for_ref_gone_succeeds_when_the_box_disappears(monkeypatch):
    rec = Recorder(errors={"DOM.getBoxModel": cdp.CDPError("Could not compute box model.")})
    _install(monkeypatch, rec)
    asyncio.run(actions.wait_for(WS, None, 7, 77, timeout=1))
    assert "DOM.getBoxModel" in rec.methods()


def test_wait_for_ref_gone_propagates_transport_errors(monkeypatch):
    """A dropped websocket must surface honestly, not be reported as the ref
    having disappeared — that would manufacture success out of a transport
    failure."""
    rec = Recorder(errors={"DOM.getBoxModel": OSError("Connection refused")})
    _install(monkeypatch, rec)
    with pytest.raises(OSError, match="Connection refused"):
        asyncio.run(actions.wait_for(WS, None, 7, 77, timeout=1))


def test_wait_for_ref_gone_times_out_while_the_node_lives(monkeypatch):
    rec = Recorder(replies={"DOM.getBoxModel": BOX})
    _install(monkeypatch, rec)
    with pytest.raises(actions.ActionError) as excinfo:
        asyncio.run(actions.wait_for(WS, None, 7, 77, timeout=0.05))
    assert "ref 7" in str(excinfo.value)


def test_wait_for_requires_a_condition(monkeypatch):
    rec = Recorder()
    _install(monkeypatch, rec)
    with pytest.raises(actions.ActionError) as excinfo:
        asyncio.run(actions.wait_for(WS, None, None, None, timeout=1))
    assert "needs text or ref_gone" in str(excinfo.value)
