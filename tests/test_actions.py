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
