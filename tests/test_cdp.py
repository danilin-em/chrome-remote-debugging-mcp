"""Unit tests for the thin CDP client — no real Chrome, no network."""

import asyncio
import itertools
import json

import httpx
import pytest

import chrome_remote_debugging_mcp.cdp as cdp


def _mock_http(monkeypatch, handler):
    """Repoint ``cdp.httpx.AsyncClient`` at a real client backed by a
    ``MockTransport``, so the HTTP probes exercise genuine httpx code
    (``raise_for_status``, ``.json()``) against an in-memory handler."""
    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient  # capture before patching (cdp.httpx is global httpx)
    monkeypatch.setattr(
        cdp.httpx,
        "AsyncClient",
        lambda **kw: real_client(transport=transport, **kw),
    )


class FakeWS:
    """In-memory stand-in for a websockets connection.

    Records sent frames and replays a queued list of server messages, so tests
    can exercise :func:`cdp.send` without a real WebSocket.
    """

    def __init__(self, messages):
        self._messages = list(messages)
        self.sent = []

    async def send(self, data):
        self.sent.append(data)

    async def recv(self):
        return json.dumps(self._messages.pop(0))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _patch(monkeypatch, fake):
    # Deterministic ids so queued responses can target a known command id.
    monkeypatch.setattr(cdp, "_ids", itertools.count(1))
    monkeypatch.setattr(cdp.websockets, "connect", lambda *a, **k: fake)


def test_send_returns_result_and_skips_events(monkeypatch):
    fake = FakeWS(
        [
            {"method": "Page.loadEventFired", "params": {}},  # event, no id → skipped
            {"id": 1, "result": {"frameId": "F1"}},
        ]
    )
    _patch(monkeypatch, fake)

    result = asyncio.run(cdp.send("ws://x", "Page.navigate", {"url": "https://e"}))

    assert result == {"frameId": "F1"}
    sent = json.loads(fake.sent[0])
    assert sent["method"] == "Page.navigate"
    assert sent["params"] == {"url": "https://e"}
    assert sent["id"] == 1


def test_send_raises_cdperror(monkeypatch):
    fake = FakeWS([{"id": 1, "error": {"message": "boom"}}])
    _patch(monkeypatch, fake)

    with pytest.raises(cdp.CDPError, match="boom"):
        asyncio.run(cdp.send("ws://x", "Bad.method"))


def test_send_error_without_message_stringifies_error(monkeypatch):
    # error dict lacks "message" → falls back to str(error).
    fake = FakeWS([{"id": 1, "error": {"code": -32000}}])
    _patch(monkeypatch, fake)

    with pytest.raises(cdp.CDPError, match="-32000"):
        asyncio.run(cdp.send("ws://x", "Bad.method"))


def test_send_defaults_result_to_empty_dict(monkeypatch):
    # A response with no "result" key returns {} rather than None.
    fake = FakeWS([{"id": 1}])
    _patch(monkeypatch, fake)

    assert asyncio.run(cdp.send("ws://x", "Some.method")) == {}


def test_list_targets_hits_json_list_and_parses(monkeypatch):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["method"] = request.method
        return httpx.Response(200, json=[{"id": "A", "type": "page"}])

    _mock_http(monkeypatch, handler)
    out = asyncio.run(cdp.list_targets("http://localhost:9222"))

    assert out == [{"id": "A", "type": "page"}]
    assert seen["url"] == "http://localhost:9222/json/list"
    assert seen["method"] == "GET"


def test_get_version_hits_json_version_and_parses(monkeypatch):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"Browser": "Chrome/120", "Protocol-Version": "1.3"})

    _mock_http(monkeypatch, handler)
    out = asyncio.run(cdp.get_version("http://localhost:9222"))

    assert out["Browser"] == "Chrome/120"
    assert seen["url"] == "http://localhost:9222/json/version"


def test_list_targets_raises_on_http_error(monkeypatch):
    _mock_http(monkeypatch, lambda request: httpx.Response(500, text="boom"))

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(cdp.list_targets("http://localhost:9222"))


def test_get_version_raises_on_http_error(monkeypatch):
    _mock_http(monkeypatch, lambda request: httpx.Response(404, text="nope"))

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(cdp.get_version("http://localhost:9222"))


def test_new_target_puts_json_new_with_about_blank(monkeypatch):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["method"] = request.method
        return httpx.Response(200, json={"id": "N", "type": "page"})

    _mock_http(monkeypatch, handler)
    out = asyncio.run(cdp.new_target("http://localhost:9222"))

    assert out == {"id": "N", "type": "page"}
    assert seen["url"] == "http://localhost:9222/json/new?about:blank"
    assert seen["method"] == "PUT"


def test_close_target_hits_json_close(monkeypatch):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, text="Target is closing")

    _mock_http(monkeypatch, handler)
    asyncio.run(cdp.close_target("http://localhost:9222", "T1"))

    assert seen["url"] == "http://localhost:9222/json/close/T1"


def test_close_target_raises_on_http_error(monkeypatch):
    _mock_http(monkeypatch, lambda request: httpx.Response(404, text="No such target id"))

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(cdp.close_target("http://localhost:9222", "ZZ"))
