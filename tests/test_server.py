"""Tool-level tests for the MCP server tools — CDP layer stubbed, no real Chrome."""

import asyncio

import pytest

import chrome_remote_debugging_mcp.server as server

PAGE = {
    "id": "T1",
    "type": "page",
    "title": "x",
    "url": "about:blank",
    "webSocketDebuggerUrl": "ws://fake",
}


def _fn(tool):
    # Works whether @mcp.tool() returns the raw function or a Tool wrapper.
    return getattr(tool, "fn", tool)


def _stub(monkeypatch, targets=None, send_result=None, send_exc=None):
    async def fake_list_targets(url):
        if targets is None:
            raise RuntimeError("connection refused")
        return targets

    async def fake_send(ws_url, method, params=None):
        if send_exc is not None:
            raise send_exc
        return send_result

    monkeypatch.setattr(server.cdp, "list_targets", fake_list_targets)
    monkeypatch.setattr(server.cdp, "send", fake_send)


def test_ping_connected(monkeypatch):
    async def fake_version(url):
        return {"Browser": "Chrome/120.0", "Protocol-Version": "1.3"}

    monkeypatch.setattr(server.cdp, "get_version", fake_version)
    out = asyncio.run(_fn(server.ping)())
    assert out["connected"] is True
    assert out["browser"] == "Chrome/120.0"
    assert out["protocol"] == "1.3"


def test_ping_disconnected(monkeypatch):
    async def fake_version(url):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(server.cdp, "get_version", fake_version)
    out = asyncio.run(_fn(server.ping)())
    assert out["connected"] is False
    assert "refused" in out["error"]


def test_cdp_command_raw_passthrough(monkeypatch):
    _stub(monkeypatch, targets=[PAGE], send_result={"root": {"nodeId": 1}})
    out = asyncio.run(_fn(server.cdp_command)("DOM.getDocument", {"depth": 1}))
    assert out == {
        "tab_id": "T1",
        "method": "DOM.getDocument",
        "result": {"root": {"nodeId": 1}},
    }


def test_cdp_command_reports_cdp_error(monkeypatch):
    async def fake_list_targets(url):
        return [PAGE]

    async def fake_send(ws_url, method, params=None):
        raise server.cdp.CDPError("'Nope.method' wasn't found")

    monkeypatch.setattr(server.cdp, "list_targets", fake_list_targets)
    monkeypatch.setattr(server.cdp, "send", fake_send)
    out = asyncio.run(_fn(server.cdp_command)("Nope.method"))
    assert "CDP error" in out["error"]


def test_evaluate_returns_value(monkeypatch):
    _stub(monkeypatch, targets=[PAGE], send_result={"result": {"type": "number", "value": 42}})
    out = asyncio.run(_fn(server.evaluate)("1 + 41"))
    assert out == {"tab_id": "T1", "value": 42, "type": "number"}


def test_evaluate_reports_js_exception(monkeypatch):
    _stub(
        monkeypatch,
        targets=[PAGE],
        send_result={"exceptionDetails": {"exception": {"description": "ReferenceError: x"}}},
    )
    out = asyncio.run(_fn(server.evaluate)("x"))
    assert "JS exception" in out["error"]
    assert "ReferenceError" in out["error"]


def test_evaluate_reports_unreachable_chrome(monkeypatch):
    _stub(monkeypatch, targets=None)
    out = asyncio.run(_fn(server.evaluate)("1"))
    assert "cannot reach Chrome" in out["error"]


def test_evaluate_reports_cdp_error(monkeypatch):
    _stub(monkeypatch, targets=[PAGE], send_exc=server.cdp.CDPError("bad domain"))
    out = asyncio.run(_fn(server.evaluate)("1"))
    assert out["error"] == "CDP error: bad domain"


def test_evaluate_reports_generic_failure(monkeypatch):
    _stub(monkeypatch, targets=[PAGE], send_exc=RuntimeError("socket died"))
    out = asyncio.run(_fn(server.evaluate)("1"))
    assert "evaluate failed" in out["error"]
    assert "socket died" in out["error"]


def test_evaluate_falls_back_to_text_for_exception(monkeypatch):
    # exceptionDetails without exception.description → uses "text".
    _stub(
        monkeypatch,
        targets=[PAGE],
        send_result={"exceptionDetails": {"text": "Uncaught"}},
    )
    out = asyncio.run(_fn(server.evaluate)("throw 1"))
    assert out["error"] == "JS exception: Uncaught"


# --- list_tabs ------------------------------------------------------------

def test_list_tabs_filters_to_pages(monkeypatch):
    worker = {"id": "BG", "type": "service_worker", "title": "sw", "url": "chrome://sw"}
    _stub(monkeypatch, targets=[PAGE, worker])
    out = asyncio.run(_fn(server.list_tabs)())
    assert out == {
        "tabs": [{"id": "T1", "title": "x", "url": "about:blank", "type": "page"}]
    }


def test_list_tabs_reports_unreachable_chrome(monkeypatch):
    _stub(monkeypatch, targets=None)
    out = asyncio.run(_fn(server.list_tabs)())
    assert "cannot reach Chrome" in out["error"]


# --- navigate -------------------------------------------------------------

def test_navigate_returns_frame_id(monkeypatch):
    _stub(monkeypatch, targets=[PAGE], send_result={"frameId": "F9"})
    out = asyncio.run(_fn(server.navigate)("https://example.com"))
    assert out == {"tab_id": "T1", "url": "https://example.com", "frameId": "F9"}


def test_navigate_reports_cdp_error(monkeypatch):
    _stub(monkeypatch, targets=[PAGE], send_exc=server.cdp.CDPError("Cannot navigate"))
    out = asyncio.run(_fn(server.navigate)("https://example.com"))
    assert out["error"] == "CDP error: Cannot navigate"


def test_navigate_reports_generic_failure(monkeypatch):
    _stub(monkeypatch, targets=[PAGE], send_exc=RuntimeError("ws closed"))
    out = asyncio.run(_fn(server.navigate)("https://example.com"))
    assert "navigation failed" in out["error"]
    assert "ws closed" in out["error"]


# --- _resolve_target error branches (surfaced through navigate) -----------

def test_navigate_no_page_targets(monkeypatch):
    _stub(monkeypatch, targets=[])
    out = asyncio.run(_fn(server.navigate)("https://example.com"))
    assert out["error"] == "no page targets open"


def test_navigate_unknown_tab_id(monkeypatch):
    _stub(monkeypatch, targets=[PAGE])
    out = asyncio.run(_fn(server.navigate)("https://example.com", tab_id="ZZ"))
    assert out["error"] == "tab ZZ not found"


def test_navigate_target_without_ws_url(monkeypatch):
    detached = {"id": "T2", "type": "page", "title": "y", "url": "about:blank"}
    _stub(monkeypatch, targets=[detached])
    out = asyncio.run(_fn(server.navigate)("https://example.com"))
    assert "webSocketDebuggerUrl" in out["error"]


def test_navigate_picks_requested_tab(monkeypatch):
    second = {**PAGE, "id": "T2", "webSocketDebuggerUrl": "ws://two"}
    _stub(monkeypatch, targets=[PAGE, second], send_result={"frameId": "F2"})
    out = asyncio.run(_fn(server.navigate)("https://e", tab_id="T2"))
    assert out["tab_id"] == "T2"


# --- cdp_command error branches ------------------------------------------

def test_cdp_command_reports_unreachable_chrome(monkeypatch):
    _stub(monkeypatch, targets=None)
    out = asyncio.run(_fn(server.cdp_command)("DOM.getDocument"))
    assert "cannot reach Chrome" in out["error"]


def test_cdp_command_reports_generic_failure(monkeypatch):
    _stub(monkeypatch, targets=[PAGE], send_exc=RuntimeError("boom"))
    out = asyncio.run(_fn(server.cdp_command)("DOM.getDocument"))
    assert "CDP command failed" in out["error"]
    assert "boom" in out["error"]


# --- main() lifecycle -----------------------------------------------------

def test_main_without_tunnel_just_runs(monkeypatch):
    events = []
    monkeypatch.setattr(server, "SSH_PROXY_TO", None)
    monkeypatch.setattr(server, "CDP_URL", "http://localhost:9222")
    monkeypatch.setattr(server.mcp, "run", lambda: events.append("run"))

    def boom(*a, **k):
        raise AssertionError("tunnel must not be built when SSH_PROXY_TO unset")

    monkeypatch.setattr(server, "SshLocalForwardTunnel", boom)

    server.main()
    assert events == ["run"]
    assert server.CDP_URL == "http://localhost:9222"


def test_main_with_tunnel_forwards_and_repoints(monkeypatch):
    events = []

    class FakeTunnel:
        local_url = "http://127.0.0.1:5999"

        def __init__(self, target, remote_host, remote_port, local_port):
            events.append(("init", target, remote_host, remote_port, local_port))

        def start(self):
            events.append("start")

        def stop(self):
            events.append("stop")

    monkeypatch.setattr(server, "SSH_PROXY_TO", "user@remote")
    monkeypatch.setattr(server, "SSH_PROXY_PORT", None)
    monkeypatch.setattr(server, "CDP_URL", "http://remotehost:9222")
    monkeypatch.setattr(server, "SshLocalForwardTunnel", FakeTunnel)
    monkeypatch.setattr(server.mcp, "run", lambda: events.append("run"))

    server.main()

    assert events == [
        ("init", "user@remote", "remotehost", 9222, None),
        "start",
        "run",
        "stop",
    ]
    assert server.CDP_URL == "http://127.0.0.1:5999"


def test_main_stops_tunnel_even_when_run_raises(monkeypatch):
    events = []

    class FakeTunnel:
        local_url = "http://127.0.0.1:5999"

        def __init__(self, *a):
            pass

        def start(self):
            events.append("start")

        def stop(self):
            events.append("stop")

    def exploding_run():
        events.append("run")
        raise RuntimeError("serve crashed")

    monkeypatch.setattr(server, "SSH_PROXY_TO", "user@remote")
    monkeypatch.setattr(server, "SSH_PROXY_PORT", None)
    monkeypatch.setattr(server, "CDP_URL", "http://remotehost:9222")
    monkeypatch.setattr(server, "SshLocalForwardTunnel", FakeTunnel)
    monkeypatch.setattr(server.mcp, "run", exploding_run)

    with pytest.raises(RuntimeError, match="serve crashed"):
        server.main()
    assert events == ["start", "run", "stop"]  # stop ran in finally


# --- browse / browse_view / browse_act -------------------------------------

def _stub_browse(monkeypatch, view="url    http://a/\ntitle  T\n\nbutton#1 \"Жми\"",
                 refs=1, url="http://a/", steps=None, error=None,
                 snapshot_exc=None, run_actions_exc=None):
    async def fake_snapshot(ws_url, tab_id):
        if snapshot_exc is not None:
            raise snapshot_exc
        return view, refs, url

    async def fake_run_actions(ws_url, tab_id, action_list):
        if run_actions_exc is not None:
            raise run_actions_exc
        return steps if steps is not None else [], error

    monkeypatch.setattr(server.browse_engine, "snapshot", fake_snapshot)
    monkeypatch.setattr(server.browse_engine, "run_actions", fake_run_actions)


def test_browse_navigates_then_returns_the_view(monkeypatch):
    _stub(monkeypatch, targets=[PAGE], send_result={})
    _stub_browse(monkeypatch)

    async def fake_settle(ws_url, timeout=2.0):
        return None

    monkeypatch.setattr(server.actions, "settle", fake_settle)
    out = asyncio.run(_fn(server.browse)("http://a/"))
    assert out["tab_id"] == "T1"
    assert out["url"] == "http://a/"
    assert out["refs"] == 1
    assert 'button#1 "Жми"' in out["view"]


def test_browse_returns_the_post_navigation_url_not_the_requested_one(monkeypatch):
    """M10: the url comes from what the snapshot itself observed
    (location.href after any redirect), not merely the requested url echoed
    back."""
    _stub(monkeypatch, targets=[PAGE], send_result={})
    _stub_browse(monkeypatch, url="http://a/redirected")

    async def fake_settle(ws_url, timeout=2.0):
        return None

    monkeypatch.setattr(server.actions, "settle", fake_settle)
    out = asyncio.run(_fn(server.browse)("http://a/"))
    assert out["url"] == "http://a/redirected"


def test_browse_reports_an_unreachable_chrome(monkeypatch):
    _stub(monkeypatch, targets=None)
    out = asyncio.run(_fn(server.browse)("http://a/"))
    assert "cannot reach Chrome" in out["error"]


def test_browse_reports_a_navigation_failure(monkeypatch):
    _stub(monkeypatch, targets=[PAGE], send_exc=RuntimeError("boom"))
    out = asyncio.run(_fn(server.browse)("http://a/"))
    assert "navigation failed" in out["error"]


def test_browse_reports_a_snapshot_failure(monkeypatch):
    _stub(monkeypatch, targets=[PAGE], send_result={})
    _stub_browse(monkeypatch, snapshot_exc=RuntimeError("tree gone"))

    async def fake_settle(ws_url, timeout=2.0):
        return None

    monkeypatch.setattr(server.actions, "settle", fake_settle)
    out = asyncio.run(_fn(server.browse)("http://a/"))
    assert "snapshot failed" in out["error"]


def test_browse_view_snapshots_without_navigating(monkeypatch):
    calls = []

    async def fake_list_targets(url):
        return [PAGE]

    async def fake_send(ws_url, method, params=None):
        calls.append(method)
        return {}

    monkeypatch.setattr(server.cdp, "list_targets", fake_list_targets)
    monkeypatch.setattr(server.cdp, "send", fake_send)
    _stub_browse(monkeypatch)
    out = asyncio.run(_fn(server.browse_view)())
    assert out["tab_id"] == "T1"
    assert out["refs"] == 1
    assert "Page.navigate" not in calls


def test_browse_view_reports_no_tabs(monkeypatch):
    _stub(monkeypatch, targets=[])
    out = asyncio.run(_fn(server.browse_view)())
    assert out["error"] == "no page targets open"


def test_browse_view_reports_a_snapshot_failure(monkeypatch):
    _stub(monkeypatch, targets=[PAGE], send_result={})
    _stub_browse(monkeypatch, snapshot_exc=RuntimeError("tree gone"))
    out = asyncio.run(_fn(server.browse_view)())
    assert "snapshot failed" in out["error"]


def test_browse_act_returns_steps_and_a_fresh_view(monkeypatch):
    _stub(monkeypatch, targets=[PAGE], send_result={})
    _stub_browse(monkeypatch, steps=[{"do": "click", "ok": True}])
    out = asyncio.run(_fn(server.browse_act)([{"do": "click", "ref": 1}]))
    assert out["steps"] == [{"do": "click", "ok": True}]
    assert "error" not in out
    assert out["refs"] == 1


def test_browse_act_surfaces_the_error_and_still_returns_a_view(monkeypatch):
    _stub(monkeypatch, targets=[PAGE], send_result={})
    _stub_browse(monkeypatch,
                 steps=[{"do": "click", "ok": False, "error": "ref 1 is stale"}],
                 error="ref 1 is stale")
    out = asyncio.run(_fn(server.browse_act)([{"do": "click", "ref": 1}]))
    assert out["error"] == "ref 1 is stale"
    assert out["view"]


def test_browse_act_reports_an_unreachable_chrome(monkeypatch):
    _stub(monkeypatch, targets=None)
    out = asyncio.run(_fn(server.browse_act)([{"do": "click", "ref": 1}]))
    assert "cannot reach Chrome" in out["error"]


def test_browse_act_reports_a_snapshot_failure_after_acting(monkeypatch):
    _stub(monkeypatch, targets=[PAGE], send_result={})
    _stub_browse(monkeypatch, steps=[{"do": "click", "ok": True}],
                 snapshot_exc=RuntimeError("tree gone"))
    out = asyncio.run(_fn(server.browse_act)([{"do": "click", "ref": 1}]))
    assert "snapshot failed" in out["error"]
    assert out["steps"] == [{"do": "click", "ok": True}]


def test_browse_act_reports_an_action_batch_failure(monkeypatch):
    """I5: run_actions was previously called unguarded inside browse_act, a
    latent breach of the never-raise contract — wrap it the way snapshot
    already is."""
    _stub(monkeypatch, targets=[PAGE], send_result={})
    _stub_browse(monkeypatch, run_actions_exc=RuntimeError("registry corrupted"))
    out = asyncio.run(_fn(server.browse_act)([{"do": "click", "ref": 1}]))
    assert out["tab_id"] == "T1"
    assert out["error"] == "action batch failed: registry corrupted"
