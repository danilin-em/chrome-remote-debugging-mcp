"""Unit tests for the thin CDP client — no real Chrome, no network."""

import asyncio
import itertools
import json

import httpx
import pytest
import websockets

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


class HangingWS(FakeWS):
    """A socket that accepts the command but never answers — a stalled tunnel."""

    async def recv(self):
        await asyncio.Event().wait()


def test_send_times_out_when_no_response_arrives(monkeypatch):
    _patch(monkeypatch, HangingWS([]))
    with pytest.raises(cdp.CDPTimeout, match=r"no response to DOM\.getDocument within 0\.01s"):
        asyncio.run(cdp.send("ws://x", "DOM.getDocument", timeout=0.01))


def test_send_uses_module_default_timeout(monkeypatch):
    _patch(monkeypatch, HangingWS([]))
    monkeypatch.setattr(cdp, "SEND_TIMEOUT", 0.01)
    with pytest.raises(cdp.CDPTimeout):
        asyncio.run(cdp.send("ws://x", "Page.navigate"))


def test_send_times_out_despite_a_stream_of_unrelated_events(monkeypatch):
    # The deadline covers the whole exchange, not each recv(): events that
    # keep arriving must not keep the call alive forever.
    class ChattyWS(FakeWS):
        async def recv(self):
            await asyncio.sleep(0.001)
            return json.dumps({"method": "Network.dataReceived", "params": {}})

    _patch(monkeypatch, ChattyWS([]))
    with pytest.raises(cdp.CDPTimeout):
        asyncio.run(cdp.send("ws://x", "Page.navigate", timeout=0.05))


def test_timeout_is_not_a_cdp_error():
    # actions.py turns CDPError into "stale ref"; a stalled transport must not
    # be misdiagnosed that way.
    assert not issubclass(cdp.CDPTimeout, cdp.CDPError)


# --- Browser: one persistent browser-socket connection ---------------------

CLOSE = object()  # inbox sentinel: recv() raises as if the peer closed


class FakeBrowserWS:
    """In-memory stand-in for the persistent browser websocket.

    ``send`` records the decoded frame and enqueues whatever ``handler(frame)``
    returns; tests can also ``push`` messages (out-of-order replies, events)
    or ``drop`` the connection.
    """

    def __init__(self, handler=None):
        self.sent = []
        self.inbox = asyncio.Queue()
        self.handler = handler
        self.closes = 0

    async def send(self, data):
        frame = json.loads(data)
        self.sent.append(frame)
        for reply in self.handler(frame) if self.handler else []:
            self.inbox.put_nowait(reply)

    async def recv(self):
        message = await self.inbox.get()
        if message is CLOSE:
            raise websockets.ConnectionClosedError(None, None)
        return json.dumps(message)

    def push(self, message):
        self.inbox.put_nowait(message)

    def drop(self):
        self.inbox.put_nowait(CLOSE)

    async def close(self):
        self.closes += 1


def _echo(frame):
    return [{"id": frame["id"], "result": {"ok": frame["method"]}}]


def _patch_connect(monkeypatch, *outcomes):
    """Make ``websockets.connect`` hand out ``outcomes`` in order (an
    exception instance is raised instead). Returns the ``(url, kw)`` log."""
    monkeypatch.setattr(cdp, "_ids", itertools.count(1))
    queue = list(outcomes)
    connects = []

    async def fake_connect(url, **kw):
        connects.append((url, kw))
        outcome = queue.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(cdp.websockets, "connect", fake_connect)
    return connects


def _provider(*urls):
    """A ws-url provider returning ``urls`` in turn (the last one repeats)."""
    calls = []

    async def provider():
        calls.append(None)
        return urls[min(len(calls), len(urls)) - 1]

    provider.calls = calls
    return provider


async def _settle():
    # Let the reader and any started calls run until they block again.
    for _ in range(10):
        await asyncio.sleep(0)


def test_call_returns_result_and_frames_have_ids(monkeypatch):
    fake = FakeBrowserWS(_echo)
    _patch_connect(monkeypatch, fake)
    b = cdp.Browser(_provider("ws://x"))

    assert asyncio.run(b.call("Browser.getVersion")) == {"ok": "Browser.getVersion"}
    assert fake.sent[0] == {"id": 1, "method": "Browser.getVersion", "params": {}}


def test_call_adds_session_id_when_given(monkeypatch):
    fake = FakeBrowserWS(_echo)
    _patch_connect(monkeypatch, fake)
    b = cdp.Browser(_provider("ws://x"))

    asyncio.run(b.call("Runtime.evaluate", {"expression": "1"}, session_id="S1"))

    assert fake.sent[0] == {"id": 1, "method": "Runtime.evaluate",
                            "params": {"expression": "1"}, "sessionId": "S1"}


def test_call_defaults_result_to_empty_dict(monkeypatch):
    _patch_connect(monkeypatch, FakeBrowserWS(lambda f: [{"id": f["id"]}]))
    b = cdp.Browser(_provider("ws://x"))

    assert asyncio.run(b.call("Some.method")) == {}


def test_connect_kwargs(monkeypatch):
    connects = _patch_connect(monkeypatch, FakeBrowserWS(_echo))
    b = cdp.Browser(_provider("ws://x"))

    asyncio.run(b.call("Browser.getVersion"))

    assert connects == [("ws://x", {"max_size": None, "open_timeout": 10.0})]


def test_concurrent_calls_are_multiplexed_out_of_order(monkeypatch):
    fake = FakeBrowserWS()  # holds every reply; the test answers by hand
    _patch_connect(monkeypatch, fake)
    b = cdp.Browser(_provider("ws://x"))

    async def scenario():
        calls = asyncio.gather(b.call("A.first"), b.call("B.second"))
        await _settle()
        assert [f["id"] for f in fake.sent] == [1, 2]
        fake.push({"id": 2, "result": {"who": "second"}})
        fake.push({"id": 1, "result": {"who": "first"}})
        return await calls

    assert asyncio.run(scenario()) == [{"who": "first"}, {"who": "second"}]


def test_concurrent_first_calls_share_one_connect(monkeypatch):
    connects = _patch_connect(monkeypatch, FakeBrowserWS(_echo))
    provider = _provider("ws://x")
    b = cdp.Browser(provider)

    async def scenario():
        return await asyncio.gather(b.call("A.a"), b.call("B.b"))

    assert asyncio.run(scenario()) == [{"ok": "A.a"}, {"ok": "B.b"}]
    assert len(connects) == 1
    assert len(provider.calls) == 1


def test_failed_connect_raises_to_all_waiters_then_retries(monkeypatch):
    connects = _patch_connect(monkeypatch, OSError("refused"), FakeBrowserWS(_echo))
    b = cdp.Browser(_provider("ws://x"))

    async def scenario():
        first = await asyncio.gather(b.call("A.a"), b.call("B.b"),
                                     return_exceptions=True)
        third = await b.call("C.c")
        return first, third

    first, third = asyncio.run(scenario())

    assert all(isinstance(exc, OSError) for exc in first)
    assert third == {"ok": "C.c"}
    assert len(connects) == 2


def test_cancelled_waiter_does_not_cancel_shared_connect(monkeypatch):
    monkeypatch.setattr(cdp, "_ids", itertools.count(1))
    fake = FakeBrowserWS(_echo)

    async def scenario():
        opened = asyncio.Event()

        async def slow_connect(url, **kw):
            await opened.wait()
            return fake

        monkeypatch.setattr(cdp.websockets, "connect", slow_connect)
        b = cdp.Browser(_provider("ws://x"))
        a = asyncio.ensure_future(b.call("A.a"))
        bb = asyncio.ensure_future(b.call("B.b"))
        await _settle()
        a.cancel()
        await _settle()
        opened.set()
        result = await bb
        assert a.cancelled()
        return result

    assert asyncio.run(scenario()) == {"ok": "B.b"}


def test_cdp_error_raises_cdperror(monkeypatch):
    _patch_connect(monkeypatch, FakeBrowserWS(
        lambda f: [{"id": f["id"], "error": {"code": -32000, "message": "boom"}}]))
    b = cdp.Browser(_provider("ws://x"))

    with pytest.raises(cdp.CDPError, match="boom"):
        asyncio.run(b.call("Bad.method"))


def test_cdp_error_without_message_stringifies_error(monkeypatch):
    _patch_connect(monkeypatch, FakeBrowserWS(
        lambda f: [{"id": f["id"], "error": {"code": -32000}}]))
    b = cdp.Browser(_provider("ws://x"))

    with pytest.raises(cdp.CDPError, match="-32000"):
        asyncio.run(b.call("Bad.method"))


def test_events_are_dropped(monkeypatch):
    def handler(frame):
        return [{"method": "Page.loadEventFired", "params": {}},
                {"id": frame["id"], "result": {"frameId": "F1"}}]

    _patch_connect(monkeypatch, FakeBrowserWS(handler))
    b = cdp.Browser(_provider("ws://x"))

    assert asyncio.run(b.call("Page.navigate")) == {"frameId": "F1"}


def _echo_unless_slow(frame):
    return [] if frame["method"] == "Slow.method" else _echo(frame)


def test_timeout_raises_cdptimeout_and_keeps_connection(monkeypatch):
    connects = _patch_connect(monkeypatch, FakeBrowserWS(_echo_unless_slow))
    b = cdp.Browser(_provider("ws://x"))

    async def scenario():
        with pytest.raises(cdp.CDPTimeout,
                           match=r"no response to Slow\.method within 0\.01s") as info:
            await b.call("Slow.method", timeout=0.01)
        assert not isinstance(info.value, cdp.CDPError)
        return await b.call("Fast.method")

    assert asyncio.run(scenario()) == {"ok": "Fast.method"}
    assert len(connects) == 1


def test_call_uses_module_default_timeout(monkeypatch):
    _patch_connect(monkeypatch, FakeBrowserWS())
    monkeypatch.setattr(cdp, "SEND_TIMEOUT", 0.01)
    b = cdp.Browser(_provider("ws://x"))

    with pytest.raises(cdp.CDPTimeout):
        asyncio.run(b.call("Page.navigate"))


def test_late_reply_after_timeout_is_ignored(monkeypatch):
    fake = FakeBrowserWS(_echo_unless_slow)
    _patch_connect(monkeypatch, fake)
    b = cdp.Browser(_provider("ws://x"))

    async def scenario():
        with pytest.raises(cdp.CDPTimeout):
            await b.call("Slow.method", timeout=0.01)
        fake.push({"id": 1, "result": {}})  # nobody waits for id 1 any more
        await _settle()
        return await b.call("Fast.method")

    assert asyncio.run(scenario()) == {"ok": "Fast.method"}


def test_reply_racing_a_cancelled_caller_keeps_reader_alive(monkeypatch):
    # The reply lands in the same loop step as the caller's cancellation: the
    # reader must not trip over the abandoned call.
    fake = FakeBrowserWS(_echo_unless_slow)
    _patch_connect(monkeypatch, fake)
    b = cdp.Browser(_provider("ws://x"))

    async def scenario():
        slow = asyncio.ensure_future(b.call("Slow.method"))
        await _settle()
        fake.push({"id": 1, "result": {}})
        slow.cancel()
        await _settle()
        assert slow.cancelled()
        return await b.call("Fast.method")

    assert asyncio.run(scenario()) == {"ok": "Fast.method"}


def test_disconnect_fails_pending_with_cdpdisconnected(monkeypatch):
    fake = FakeBrowserWS()
    _patch_connect(monkeypatch, fake)
    b = cdp.Browser(_provider("ws://x"))

    async def scenario():
        pending = asyncio.ensure_future(b.call("Page.navigate"))
        await _settle()
        fake.drop()
        with pytest.raises(cdp.CDPDisconnected) as info:
            await pending
        return info.value

    exc = asyncio.run(scenario())
    assert not isinstance(exc, cdp.CDPError)


def test_reconnect_asks_provider_again(monkeypatch):
    first, second = FakeBrowserWS(_echo), FakeBrowserWS(_echo)
    connects = _patch_connect(monkeypatch, first, second)
    b = cdp.Browser(_provider("ws://a", "ws://b"))

    async def scenario():
        await b.call("A.a")
        first.drop()
        await _settle()
        return await b.call("B.b")

    assert asyncio.run(scenario()) == {"ok": "B.b"}
    assert [url for url, _ in connects] == ["ws://a", "ws://b"]
    assert second.sent[0]["method"] == "B.b"


def test_send_failure_raises_cdpdisconnected(monkeypatch):
    class BrokenWS(FakeBrowserWS):
        async def send(self, data):
            raise websockets.ConnectionClosedError(None, None)

    _patch_connect(monkeypatch, BrokenWS())
    b = cdp.Browser(_provider("ws://x"))

    with pytest.raises(cdp.CDPDisconnected):
        asyncio.run(b.call("Page.navigate"))


def test_close_is_idempotent(monkeypatch):
    fake = FakeBrowserWS(_echo)
    _patch_connect(monkeypatch, fake)
    b = cdp.Browser(_provider("ws://x"))

    async def scenario():
        await b.call("A.a")
        await b.close()
        await b.close()

    asyncio.run(scenario())
    assert fake.closes == 1


def test_close_fails_pending_with_cdpdisconnected(monkeypatch):
    fake = FakeBrowserWS()
    _patch_connect(monkeypatch, fake)
    b = cdp.Browser(_provider("ws://x"))

    async def scenario():
        pending = asyncio.ensure_future(b.call("Page.navigate"))
        await _settle()
        await b.close()
        with pytest.raises(cdp.CDPDisconnected):
            await pending

    asyncio.run(scenario())


def test_close_before_connect_is_a_noop():
    asyncio.run(cdp.Browser(_provider("ws://x")).close())


def test_browser_ws_url_rewrites_host_and_port(monkeypatch):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={
            "webSocketDebuggerUrl": "ws://localhost:9222/devtools/browser/X"})

    _mock_http(monkeypatch, handler)
    out = asyncio.run(cdp.browser_ws_url("http://127.0.0.1:41234"))

    assert out == "ws://127.0.0.1:41234/devtools/browser/X"
    assert seen["url"] == "http://127.0.0.1:41234/json/version"


def test_browser_ws_url_raises_on_http_error(monkeypatch):
    _mock_http(monkeypatch, lambda request: httpx.Response(404, text="nope"))

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(cdp.browser_ws_url("http://localhost:9222"))


def test_exception_hierarchy():
    assert not issubclass(cdp.CDPDisconnected, cdp.CDPError)
    assert not issubclass(cdp.TargetDetached, cdp.CDPError)
    assert issubclass(cdp.CDPDisconnected, ConnectionError)
