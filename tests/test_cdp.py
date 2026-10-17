"""Unit tests for the thin CDP client — no real Chrome, no network."""

import asyncio
import gc
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


# --- Sessions: one flattened session per tab, and send(page, ...) ----------

def _tabs(frame):
    """Answer an attach with ``S-<targetId>`` and anything else with an echo."""
    if frame["method"] == "Target.attachToTarget":
        session_id = f"S-{frame['params']['targetId']}"
        return [{"id": frame["id"], "result": {"sessionId": session_id}}]
    return [{"id": frame["id"], "result": {"echo": frame["method"]}}]


def _attaches(fake):
    return [f for f in fake.sent if f["method"] == "Target.attachToTarget"]


def _holding(method):
    """Like :func:`_tabs`, but never answer ``method``."""
    return lambda frame: [] if frame["method"] == method else _tabs(frame)


def test_send_attaches_once_and_reuses_session(monkeypatch):
    fake = FakeBrowserWS(_tabs)
    _patch_connect(monkeypatch, fake)
    page = cdp.Page(cdp.Browser(_provider("ws://x")), "T1")

    async def scenario():
        return [await cdp.send(page, "Runtime.evaluate", {"expression": "1"}),
                await cdp.send(page, "Runtime.evaluate")]

    assert asyncio.run(scenario()) == [{"echo": "Runtime.evaluate"}] * 2
    attaches = _attaches(fake)
    assert attaches == [{"id": 1, "method": "Target.attachToTarget",
                         "params": {"targetId": "T1", "flatten": True}}]
    evaluates = [f for f in fake.sent if f["method"] == "Runtime.evaluate"]
    assert [f["sessionId"] for f in evaluates] == ["S-T1", "S-T1"]
    assert evaluates[0]["params"] == {"expression": "1"}


def test_concurrent_sends_share_one_attach(monkeypatch):
    fake = FakeBrowserWS(_tabs)
    _patch_connect(monkeypatch, fake)
    page = cdp.Page(cdp.Browser(_provider("ws://x")), "T1")

    async def scenario():
        return await asyncio.gather(*(cdp.send(page, "Runtime.evaluate")
                                      for _ in range(3)))

    assert asyncio.run(scenario()) == [{"echo": "Runtime.evaluate"}] * 3
    assert len(_attaches(fake)) == 1


def test_cancelled_sender_does_not_cancel_shared_attach(monkeypatch):
    fake = FakeBrowserWS(_holding("Target.attachToTarget"))
    _patch_connect(monkeypatch, fake)
    page = cdp.Page(cdp.Browser(_provider("ws://x")), "T1")

    async def scenario():
        first = asyncio.ensure_future(cdp.send(page, "A.a"))
        second = asyncio.ensure_future(cdp.send(page, "B.b"))
        await _settle()
        first.cancel()
        await _settle()
        attach = _attaches(fake)[0]
        fake.push({"id": attach["id"], "result": {"sessionId": "S-T1"}})
        result = await second
        assert first.cancelled()
        return result

    assert asyncio.run(scenario()) == {"echo": "B.b"}
    assert len(_attaches(fake)) == 1


def test_attach_error_raises_target_detached(monkeypatch):
    failed = []

    def handler(frame):
        if frame["method"] == "Target.attachToTarget" and not failed:
            failed.append(frame)
            return [{"id": frame["id"], "error": {
                "code": -32602, "message": "No target with given id found"}}]
        return _tabs(frame)

    fake = FakeBrowserWS(handler)
    _patch_connect(monkeypatch, fake)
    page = cdp.Page(cdp.Browser(_provider("ws://x")), "T1")

    async def scenario():
        with pytest.raises(cdp.TargetDetached,
                           match="tab T1 is gone: No target with given id found") as info:
            await cdp.send(page, "Runtime.evaluate")
        assert not isinstance(info.value, cdp.CDPError)
        return await cdp.send(page, "Runtime.evaluate")

    assert asyncio.run(scenario()) == {"echo": "Runtime.evaluate"}
    assert len(_attaches(fake)) == 2


def test_detach_event_fails_only_that_sessions_pending(monkeypatch):
    fake = FakeBrowserWS(_holding("Runtime.evaluate"))
    _patch_connect(monkeypatch, fake)
    b = cdp.Browser(_provider("ws://x"))

    async def scenario():
        t1 = asyncio.ensure_future(cdp.send(cdp.Page(b, "T1"), "Runtime.evaluate"))
        t2 = asyncio.ensure_future(cdp.send(cdp.Page(b, "T2"), "Runtime.evaluate"))
        await _settle()
        fake.push({"method": "Target.detachedFromTarget",
                   "params": {"sessionId": "S-T1", "targetId": "T1"}})
        with pytest.raises(cdp.TargetDetached) as info:
            await t1
        assert not isinstance(info.value, cdp.CDPError)
        assert not t2.done()
        [held] = [f for f in fake.sent if f.get("sessionId") == "S-T2"]
        fake.push({"id": held["id"], "result": {"value": 2}})
        assert await t2 == {"value": 2}
        return await cdp.send(cdp.Page(b, "T1"), "Page.reload")

    assert asyncio.run(scenario()) == {"echo": "Page.reload"}
    assert [f["params"]["targetId"] for f in _attaches(fake)] == ["T1", "T2", "T1"]


def test_detach_event_for_unknown_session_is_ignored(monkeypatch):
    fake = FakeBrowserWS(_tabs)
    _patch_connect(monkeypatch, fake)
    page = cdp.Page(cdp.Browser(_provider("ws://x")), "T1")

    async def scenario():
        await cdp.send(page, "A.a")
        fake.push({"method": "Target.detachedFromTarget",
                   "params": {"sessionId": "S-ghost"}})
        await _settle()
        return await cdp.send(page, "B.b")

    assert asyncio.run(scenario()) == {"echo": "B.b"}
    assert len(_attaches(fake)) == 1
    assert fake.sent[-1]["sessionId"] == "S-T1"


def test_detach_event_without_a_session_id_is_ignored(monkeypatch):
    fake = FakeBrowserWS(_tabs)
    _patch_connect(monkeypatch, fake)
    page = cdp.Page(cdp.Browser(_provider("ws://x")), "T1")

    async def scenario():
        await cdp.send(page, "A.a")
        fake.push({"method": "Target.detachedFromTarget"})
        await _settle()
        return await cdp.send(page, "B.b")

    assert asyncio.run(scenario()) == {"echo": "B.b"}
    assert len(_attaches(fake)) == 1


def test_session_not_found_error_becomes_target_detached(monkeypatch):
    def handler(frame):
        if frame["method"] == "Gone.method":
            return [{"id": frame["id"], "error": {
                "code": -32001, "message": "Session with given id not found."}}]
        return _tabs(frame)

    fake = FakeBrowserWS(handler)
    _patch_connect(monkeypatch, fake)
    b = cdp.Browser(_provider("ws://x"))
    page = cdp.Page(b, "T1")

    async def scenario():
        with pytest.raises(cdp.TargetDetached,
                           match="Session with given id not found") as info:
            await cdp.send(page, "Gone.method")
        assert not isinstance(info.value, cdp.CDPError)
        assert await cdp.send(page, "Page.reload") == {"echo": "Page.reload"}
        with pytest.raises(cdp.CDPError, match="Session with given id not found"):
            await b.call("Gone.method")

    asyncio.run(scenario())
    assert len(_attaches(fake)) == 2


def test_disconnect_clears_sessions(monkeypatch):
    first, second = FakeBrowserWS(_tabs), FakeBrowserWS(_tabs)
    _patch_connect(monkeypatch, first, second)
    page = cdp.Page(cdp.Browser(_provider("ws://x")), "T1")

    async def scenario():
        await cdp.send(page, "A.a")
        first.drop()
        await _settle()
        return await cdp.send(page, "B.b")

    assert asyncio.run(scenario()) == {"echo": "B.b"}
    assert len(_attaches(first)) == 1
    assert [f["method"] for f in second.sent] == ["Target.attachToTarget", "B.b"]
    assert second.sent[1]["sessionId"] == "S-T1"


def test_attach_answered_as_connection_drops_is_not_cached(monkeypatch):
    # The attach reply arrives, then the socket dies before the attach
    # finishes: the session belongs to the dead socket, so it must not land in
    # the cache the new socket uses.
    first = FakeBrowserWS(_holding("Target.attachToTarget"))
    second = FakeBrowserWS(_tabs)
    _patch_connect(monkeypatch, first, second)
    page = cdp.Page(cdp.Browser(_provider("ws://x")), "T1")

    async def scenario():
        pending = asyncio.ensure_future(cdp.send(page, "A.a"))
        await _settle()
        first.push({"id": _attaches(first)[0]["id"], "result": {"sessionId": "S-old"}})
        first.drop()
        await pending
        return await cdp.send(page, "B.b")

    assert asyncio.run(scenario()) == {"echo": "B.b"}
    assert second.sent[-1]["sessionId"] == "S-T1"


def test_disconnect_during_attach_lets_next_send_reattach(monkeypatch):
    first = FakeBrowserWS(_holding("Target.attachToTarget"))
    second = FakeBrowserWS(_tabs)
    _patch_connect(monkeypatch, first, second)
    page = cdp.Page(cdp.Browser(_provider("ws://x")), "T1")

    async def scenario():
        pending = asyncio.ensure_future(cdp.send(page, "A.a"))
        await _settle()
        first.drop()
        with pytest.raises(cdp.CDPDisconnected):
            await pending
        return await cdp.send(page, "B.b")

    assert asyncio.run(scenario()) == {"echo": "B.b"}
    assert [f["method"] for f in second.sent] == ["Target.attachToTarget", "B.b"]


def test_slow_tab_does_not_block_other_tab(monkeypatch):
    def handler(frame):
        if frame.get("sessionId") == "S-T1":
            return []  # T1's page is busy and never answers
        return _tabs(frame)

    _patch_connect(monkeypatch, FakeBrowserWS(handler))
    b = cdp.Browser(_provider("ws://x"))
    finished = []

    async def tracked(name, coro):
        try:
            return await coro
        finally:
            finished.append(name)

    async def scenario():
        return await asyncio.gather(
            tracked("T1", cdp.send(cdp.Page(b, "T1"), "Runtime.evaluate", timeout=0.2)),
            tracked("T2", cdp.send(cdp.Page(b, "T2"), "Runtime.evaluate")),
            return_exceptions=True)

    slow, fast = asyncio.run(scenario())
    assert isinstance(slow, cdp.CDPTimeout)
    assert fast == {"echo": "Runtime.evaluate"}
    assert finished == ["T2", "T1"]


def test_send_passes_timeout(monkeypatch):
    _patch_connect(monkeypatch, FakeBrowserWS(_holding("X")))
    page = cdp.Page(cdp.Browser(_provider("ws://x")), "T1")

    with pytest.raises(cdp.CDPTimeout, match=r"no response to X within 0\.01s"):
        asyncio.run(cdp.send(page, "X", timeout=0.01))


def test_abandoned_connect_failure_is_retrieved(monkeypatch):
    # A caller that gives up on a connect which later fails must not leave
    # "Task exception was never retrieved" behind.
    release = {}
    contexts = []
    err = OSError("refused")

    async def fake_connect(url, **kw):
        await release["event"].wait()
        raise err

    monkeypatch.setattr(cdp.websockets, "connect", fake_connect)
    b = cdp.Browser(_provider("ws://x"))

    async def scenario():
        asyncio.get_running_loop().set_exception_handler(
            lambda loop, context: contexts.append(context))
        release["event"] = asyncio.Event()
        try:
            await asyncio.wait_for(b.call("Some.method"), 0.01)
        except asyncio.TimeoutError:
            pass
        release["event"].set()
        await _settle()
        gc.collect()

    asyncio.run(scenario())
    assert not [c for c in contexts
                if c.get("exception") is err
                or "never retrieved" in c.get("message", "")
                or "shielded future" in c.get("message", "")]


def test_send_reattaches_when_session_died_before_the_command(monkeypatch):
    # The attach is answered and the socket dies at once: the session id the
    # sender gets belongs to the dead connection, so send must attach again.
    def dying(frame):
        return [{"id": frame["id"], "result": {"sessionId": "S1"}}, CLOSE]

    def second_handler(frame):
        if frame["method"] == "Target.attachToTarget":
            return [{"id": frame["id"], "result": {"sessionId": "S2"}}]
        return _echo(frame)

    first, second = FakeBrowserWS(dying), FakeBrowserWS(second_handler)
    connects = _patch_connect(monkeypatch, first, second)
    page = cdp.Page(cdp.Browser(_provider("ws://x")), "T1")

    assert asyncio.run(cdp.send(page, "Runtime.evaluate")) == {"ok": "Runtime.evaluate"}
    assert second.sent[-1]["sessionId"] == "S2"
    assert len(connects) == 2


# --- call_scope: at most one connection per tool call -----------------------

def test_scope_does_not_reconnect_after_a_failed_connect(monkeypatch):
    refused = OSError("refused")
    connects = _patch_connect(monkeypatch, refused, FakeBrowserWS(_echo))
    b = cdp.Browser(_provider("ws://x"))

    async def failing_call():
        with pytest.raises(OSError) as info:
            await b.call("A.a")
        return info.value

    async def scenario():
        with cdp.call_scope():
            first = await failing_call()
            second = await failing_call()
        with cdp.call_scope():
            result = await b.call("A.a")
        return first, second, result

    first, second, result = asyncio.run(scenario())
    assert first is refused and second is refused
    assert result == {"ok": "A.a"}
    assert len(connects) == 2


def test_scope_does_not_reconnect_after_the_connection_drops(monkeypatch):
    fake = FakeBrowserWS(_echo)
    connects = _patch_connect(monkeypatch, fake, FakeBrowserWS(_echo))
    b = cdp.Browser(_provider("ws://x"))

    async def scenario():
        with cdp.call_scope():
            await b.call("A.a")
            await b.call("A.again")  # a live bound socket is reused
            fake.drop()
            await _settle()
            with pytest.raises(cdp.CDPDisconnected, match="during this tool call"):
                await b.call("A.b")

    asyncio.run(scenario())
    assert len(connects) == 1


def test_scope_counts_a_joined_attempt_that_failed(monkeypatch):
    refused = OSError("refused")
    connects = []

    async def fake_connect(url, **kw):
        connects.append(url)
        await asyncio.sleep(0)
        raise refused

    monkeypatch.setattr(cdp.websockets, "connect", fake_connect)
    b = cdp.Browser(_provider("ws://x"))

    async def scenario():
        with cdp.call_scope():
            results = await asyncio.gather(b.call("A.a"), b.call("B.b"),
                                           return_exceptions=True)
            with pytest.raises(OSError) as info:
                await b.call("C.c")
        return results, info.value

    results, third = asyncio.run(scenario())
    assert results == [refused, refused]
    assert third is refused
    assert len(connects) == 1


def test_scope_stops_a_swallowing_retry_loop_from_reconnecting(monkeypatch):
    from chrome_remote_debugging_mcp import actions

    def handler(frame):
        if frame["method"] == "Target.attachToTarget":
            return [{"id": frame["id"], "result": {"sessionId": "S1"}}]
        return _echo(frame)

    fake = FakeBrowserWS(handler)
    connects = _patch_connect(monkeypatch, fake, FakeBrowserWS(handler))
    b = cdp.Browser(_provider("ws://x"))

    async def scenario():
        with cdp.call_scope():
            await b.call("A.a")
            fake.drop()
            await _settle()
            with pytest.raises(actions.ActionError, match="timed out"):
                await actions.wait_for(cdp.Page(b, "T1"), text="x",
                                       ref_gone=None, backend_id=None,
                                       timeout=0.2)

    asyncio.run(scenario())
    assert len(connects) == 1


# --- pluggable connect and the caller-side connect wait ---------------------


def _gated_hook():
    """A connect hook that blocks on ``hook.gate`` and then hands out an echo
    socket. ``hook.calls`` logs the urls it was called with."""
    gate = asyncio.Event()
    calls = []

    async def hook(url):
        calls.append(url)
        await gate.wait()
        return FakeBrowserWS(_echo)

    hook.gate, hook.calls = gate, calls
    return hook


def _record_loop_errors(loop):
    contexts = []
    loop.set_exception_handler(lambda _loop, context: contexts.append(context))
    return contexts


def test_connect_wait_times_out_but_keeps_the_attempt(monkeypatch):
    monkeypatch.setattr(cdp, "_ids", itertools.count(1))
    hook = _gated_hook()
    b = cdp.Browser(_provider("ws://x"), connect=hook, connect_wait=0.05,
                    wait_message="click Allow")

    async def scenario():
        contexts = _record_loop_errors(asyncio.get_running_loop())
        with cdp.call_scope():
            with pytest.raises(cdp.CDPConnectTimeout, match="click Allow"):
                await b.call("A.a")
        hook.gate.set()
        await _settle()
        with cdp.call_scope():
            result = await b.call("A.b")
        return contexts, result

    contexts, result = asyncio.run(scenario())
    assert result == {"ok": "A.b"}
    assert hook.calls == ["ws://x"]
    assert contexts == []


def test_connect_wait_default_message(monkeypatch):
    hook = _gated_hook()
    b = cdp.Browser(_provider("ws://x"), connect=hook, connect_wait=0.05)

    with pytest.raises(cdp.CDPConnectTimeout,
                       match="no connection to Chrome within 0.05s"):
        asyncio.run(b.call("A.a"))


def test_connect_wait_does_not_mistake_the_attempts_own_timeout(monkeypatch):
    async def hook(url):
        raise TimeoutError("handshake")

    b = cdp.Browser(_provider("ws://x"), connect=hook, connect_wait=5)

    with pytest.raises(TimeoutError, match="handshake") as info:
        asyncio.run(b.call("A.a"))
    assert not isinstance(info.value, cdp.CDPConnectTimeout)


def test_cancelled_waiter_keeps_the_attempt(monkeypatch):
    monkeypatch.setattr(cdp, "_ids", itertools.count(1))
    hook = _gated_hook()
    b = cdp.Browser(_provider("ws://x"), connect=hook)

    async def scenario():
        contexts = _record_loop_errors(asyncio.get_running_loop())
        first = asyncio.ensure_future(b.call("A.a"))
        await _settle()
        first.cancel()
        await asyncio.wait({first})
        hook.gate.set()
        await _settle()
        return contexts, await b.call("A.b")

    contexts, result = asyncio.run(scenario())
    assert result == {"ok": "A.b"}
    assert hook.calls == ["ws://x"]
    assert contexts == []


def _failing_then_echo_hook(error):
    """A connect hook whose first attempt blocks on ``hook.gate`` and then
    raises ``error``; later attempts hand out an echo socket."""
    gate = asyncio.Event()
    calls = []

    async def hook(url):
        calls.append(url)
        if len(calls) == 1:
            await gate.wait()
            raise error
        return FakeBrowserWS(_echo)

    hook.gate, hook.calls = gate, calls
    return hook


def test_a_failure_no_waiter_received_is_raised_by_the_next_call(monkeypatch):
    # The wait expires, then the user clicks Deny: the next call must report
    # that 403 instead of dialling again (a new dialog) and blaming the wait.
    monkeypatch.setattr(cdp, "_ids", itertools.count(1))
    denied = OSError("HTTP 403")
    hook = _failing_then_echo_hook(denied)
    b = cdp.Browser(_provider("ws://x"), connect=hook, connect_wait=0.05)

    async def scenario():
        contexts = _record_loop_errors(asyncio.get_running_loop())
        with cdp.call_scope():
            with pytest.raises(cdp.CDPConnectTimeout):
                await b.call("A.a")
        hook.gate.set()
        await _settle()
        with cdp.call_scope():
            with pytest.raises(OSError) as info:
                await b.call("A.b")
            assert len(hook.calls) == 1  # raised without a second connect
        with cdp.call_scope():
            result = await b.call("A.c")  # raised once: this one dials
        return contexts, info.value, result

    contexts, kept, result = asyncio.run(scenario())
    assert kept is denied
    assert result == {"ok": "A.c"}
    assert len(hook.calls) == 2
    assert contexts == []


def test_a_failure_a_waiter_received_is_not_kept(monkeypatch):
    monkeypatch.setattr(cdp, "_ids", itertools.count(1))
    denied = OSError("HTTP 403")
    hook = _failing_then_echo_hook(denied)
    b = cdp.Browser(_provider("ws://x"), connect=hook)

    async def scenario():
        gave_up = asyncio.ensure_future(b.call("A.a"))
        waiting = asyncio.ensure_future(b.call("B.b"))
        await _settle()
        gave_up.cancel()
        hook.gate.set()
        with pytest.raises(OSError) as info:
            await waiting
        await asyncio.wait({gave_up})
        return info.value, await b.call("C.c")

    received, result = asyncio.run(scenario())
    assert received is denied
    assert result == {"ok": "C.c"}
    assert len(hook.calls) == 2


def test_joining_an_attach_does_not_inherit_a_stale_scope(monkeypatch):
    # Call A's scope saw its socket die; call B is fresh. A asks for the
    # session first, so B must not join an attach that fails on A's scope.
    first, second = FakeBrowserWS(_tabs), FakeBrowserWS(_tabs)
    connects = _patch_connect(monkeypatch, first, second)
    b = cdp.Browser(_provider("ws://x"))
    page = cdp.Page(b, "T1")

    async def fresh():
        with cdp.call_scope():
            return await cdp.send(page, "B.b")

    async def scenario():
        with cdp.call_scope():
            await b.call("A.a")
            first.drop()
            await _settle()
            return await asyncio.gather(cdp.send(page, "A.b"), fresh(),
                                        return_exceptions=True)

    stale, result = asyncio.run(scenario())
    assert isinstance(stale, cdp.CDPDisconnected)
    assert result == {"echo": "B.b"}
    assert len(connects) == 2


def test_concurrent_scoped_calls_share_one_attempt(monkeypatch):
    monkeypatch.setattr(cdp, "_ids", itertools.count(1))
    hook = _gated_hook()
    b = cdp.Browser(_provider("ws://x"), connect=hook)

    async def scoped(method):
        with cdp.call_scope():
            return await b.call(method)

    async def scenario():
        both = asyncio.gather(scoped("A.a"), scoped("B.b"))
        await _settle()
        hook.gate.set()
        return await both

    assert asyncio.run(scenario()) == [{"ok": "A.a"}, {"ok": "B.b"}]
    assert hook.calls == ["ws://x"]


def test_connect_timeout_is_a_timeout_not_a_cdp_error():
    assert issubclass(cdp.CDPConnectTimeout, cdp.CDPTimeout)
    assert not issubclass(cdp.CDPConnectTimeout, cdp.CDPError)
