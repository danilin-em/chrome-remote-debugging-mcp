"""Tool-level tests for the MCP server tools — CDP layer stubbed, no real Chrome."""

import asyncio

import pytest

import chrome_remote_debugging_mcp.server as server

PAGE = {"targetId": "T1", "type": "page", "title": "x", "url": "about:blank"}


def _fn(tool):
    # Works whether @mcp.tool() returns the raw function or a Tool wrapper.
    return getattr(tool, "fn", tool)


@pytest.fixture(autouse=True)
def _no_browser(monkeypatch):
    # The real connection is bound to the event loop that opened it, and
    # every test runs its own asyncio.run — never carry one across tests.
    monkeypatch.setattr(server, "_BROWSER", None)


class FakeBrowser:
    """Stands in for ``cdp.Browser``: answers browser-level commands from
    canned data and records every call as ``(method, params)``. With
    ``targets=None`` every call fails as if Chrome were unreachable;
    ``errors`` maps a method to the exception it raises."""

    def __init__(self, targets, new="T1", version=None, errors=None):
        self.targets, self.new, self.version = targets, new, version or {}
        self.errors = errors or {}
        self.calls = []

    async def call(self, method, params=None, **kw):
        self.calls.append((method, params))
        if self.targets is None:
            raise RuntimeError("connection refused")
        if method in self.errors:
            raise self.errors[method]
        return {
            "Target.getTargets": {"targetInfos": self.targets},
            "Target.createTarget": {"targetId": self.new},
            "Target.closeTarget": {"success": True},
            "Browser.getVersion": self.version,
        }[method]


def _stub(monkeypatch, targets=None, send_result=None, send_exc=None, new="T1",
          version=None, errors=None):
    fake = FakeBrowser(targets, new, version, errors)

    async def fake_send(page, method, params=None):
        if send_exc is not None:
            raise send_exc
        return send_result

    monkeypatch.setattr(server, "_browser", lambda: fake)
    monkeypatch.setattr(server.cdp, "send", fake_send)
    return fake


def test_ping_connected(monkeypatch):
    _stub(monkeypatch, targets=[], version={"product": "Chrome/120.0", "protocolVersion": "1.3"})
    out = asyncio.run(_fn(server.ping)())
    assert out["connected"] is True
    assert out["browser"] == "Chrome/120.0"
    assert out["protocol"] == "1.3"


def test_ping_disconnected(monkeypatch):
    _stub(monkeypatch, targets=None)
    out = asyncio.run(_fn(server.ping)())
    assert out["connected"] is False
    assert "refused" in out["error"]


def test_cdp_command_raw_passthrough(monkeypatch):
    _stub(monkeypatch, targets=[PAGE], send_result={"root": {"nodeId": 1}})
    out = asyncio.run(_fn(server.cdp_command)("DOM.getDocument", "T1", {"depth": 1}))
    assert out == {
        "tab_id": "T1",
        "method": "DOM.getDocument",
        "result": {"root": {"nodeId": 1}},
    }


def test_cdp_command_reports_cdp_error(monkeypatch):
    _stub(monkeypatch, targets=[PAGE], send_exc=server.cdp.CDPError("'Nope.method' wasn't found"))
    out = asyncio.run(_fn(server.cdp_command)("Nope.method", "T1"))
    assert "CDP error" in out["error"]


def test_evaluate_returns_value(monkeypatch):
    _stub(monkeypatch, targets=[PAGE], send_result={"result": {"type": "number", "value": 42}})
    out = asyncio.run(_fn(server.evaluate)("1 + 41", "T1"))
    assert out == {"tab_id": "T1", "value": 42, "type": "number"}


def test_evaluate_reports_js_exception(monkeypatch):
    _stub(
        monkeypatch,
        targets=[PAGE],
        send_result={"exceptionDetails": {"exception": {"description": "ReferenceError: x"}}},
    )
    out = asyncio.run(_fn(server.evaluate)("x", "T1"))
    assert "JS exception" in out["error"]
    assert "ReferenceError" in out["error"]


def test_evaluate_reports_unreachable_chrome(monkeypatch):
    _stub(monkeypatch, targets=None)
    out = asyncio.run(_fn(server.evaluate)("1", "T1"))
    assert "cannot reach Chrome" in out["error"]


def test_evaluate_reports_cdp_error(monkeypatch):
    _stub(monkeypatch, targets=[PAGE], send_exc=server.cdp.CDPError("bad domain"))
    out = asyncio.run(_fn(server.evaluate)("1", "T1"))
    assert out["error"] == "CDP error: bad domain"


def test_evaluate_reports_generic_failure(monkeypatch):
    _stub(monkeypatch, targets=[PAGE], send_exc=RuntimeError("socket died"))
    out = asyncio.run(_fn(server.evaluate)("1", "T1"))
    assert "evaluate failed" in out["error"]
    assert "socket died" in out["error"]


def test_evaluate_falls_back_to_text_for_exception(monkeypatch):
    # exceptionDetails without exception.description → uses "text".
    _stub(
        monkeypatch,
        targets=[PAGE],
        send_result={"exceptionDetails": {"text": "Uncaught"}},
    )
    out = asyncio.run(_fn(server.evaluate)("throw 1", "T1"))
    assert out["error"] == "JS exception: Uncaught"


# --- list_tabs ------------------------------------------------------------

def test_list_tabs_filters_to_pages(monkeypatch):
    worker = {"targetId": "BG", "type": "service_worker", "title": "sw", "url": "chrome://sw"}
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

def test_navigate_opens_a_new_tab_when_tab_id_omitted(monkeypatch):
    seen = []

    async def fake_send(page, method, params=None):
        seen.append(page)
        return {"frameId": "F1"}

    fake = _stub(monkeypatch, targets=[PAGE], new="NEW")
    monkeypatch.setattr(server.cdp, "send", fake_send)
    out = asyncio.run(_fn(server.navigate)("https://example.com"))
    assert out["tab_id"] == "NEW"
    assert seen == [server.cdp.Page(fake, "NEW")]


def test_navigate_reports_a_new_tab_failure(monkeypatch):
    _stub(monkeypatch, targets=None)
    out = asyncio.run(_fn(server.navigate)("https://example.com"))
    assert "cannot open a new tab" in out["error"]
    assert "connection refused" in out["error"]


def test_navigate_unknown_tab_id(monkeypatch):
    _stub(monkeypatch, targets=[PAGE])
    out = asyncio.run(_fn(server.navigate)("https://example.com", tab_id="ZZ"))
    assert out["error"] == "tab ZZ not found"


def test_navigate_picks_requested_tab(monkeypatch):
    second = {**PAGE, "targetId": "T2"}
    _stub(monkeypatch, targets=[PAGE, second], send_result={"frameId": "F2"})
    out = asyncio.run(_fn(server.navigate)("https://e", tab_id="T2"))
    assert out["tab_id"] == "T2"


# --- cdp_command error branches ------------------------------------------

def test_cdp_command_reports_unreachable_chrome(monkeypatch):
    _stub(monkeypatch, targets=None)
    out = asyncio.run(_fn(server.cdp_command)("DOM.getDocument", "T1"))
    assert "cannot reach Chrome" in out["error"]


def test_cdp_command_reports_generic_failure(monkeypatch):
    _stub(monkeypatch, targets=[PAGE], send_exc=RuntimeError("boom"))
    out = asyncio.run(_fn(server.cdp_command)("DOM.getDocument", "T1"))
    assert "CDP command failed" in out["error"]
    assert "boom" in out["error"]


# --- the persistent browser connection --------------------------------------

def test_list_tabs_maps_target_infos(monkeypatch):
    worker = {"targetId": "BG", "type": "service_worker", "title": "sw", "url": "chrome://sw"}
    page = {"targetId": "T1", "type": "page", "title": "Home", "url": "http://a/",
            "attached": False, "browserContextId": "C1"}
    _stub(monkeypatch, targets=[page, worker])
    out = asyncio.run(_fn(server.list_tabs)())
    assert out == {"tabs": [{"id": "T1", "title": "Home", "url": "http://a/", "type": "page"}]}


def test_open_target_uses_create_target(monkeypatch):
    fake = _stub(monkeypatch, targets=[PAGE], send_result={"frameId": "F1"}, new="NEW")
    out = asyncio.run(_fn(server.navigate)("https://example.com"))
    assert ("Target.createTarget", {"url": "about:blank"}) in fake.calls
    assert out["tab_id"] == "NEW"


def test_open_target_malformed_reply_becomes_an_error(monkeypatch):
    class Bare(FakeBrowser):
        async def call(self, method, params=None, **kw):
            return {}

    monkeypatch.setattr(server, "_browser", lambda: Bare([]))
    out = asyncio.run(_fn(server.navigate)("https://example.com"))
    assert out["error"].startswith("cannot open a new tab at")


def test_close_tab_uses_close_target(monkeypatch):
    fake = _stub(monkeypatch, targets=[PAGE])
    out = asyncio.run(_fn(server.close_tab)("T1"))
    assert fake.calls[-1] == ("Target.closeTarget", {"targetId": "T1"})
    assert out == {"tab_id": "T1", "closed": True}


def test_ping_uses_browser_get_version(monkeypatch):
    fake = _stub(monkeypatch, targets=[], version={"product": "Chrome/155", "protocolVersion": "1.3"})
    monkeypatch.setattr(server, "CDP_URL", "http://localhost:9222")
    out = asyncio.run(_fn(server.ping)())
    assert fake.calls == [("Browser.getVersion", None)]
    assert out == {"connected": True, "cdp_url": "http://localhost:9222",
                   "browser": "Chrome/155", "protocol": "1.3"}


@pytest.mark.parametrize("exc", [server.cdp.CDPDisconnected("closed"),
                                 server.cdp.TargetDetached("closed")])
def test_tool_reports_disconnect_as_error(monkeypatch, exc):
    _stub(monkeypatch, targets=[PAGE], send_exc=exc)
    out = asyncio.run(_fn(server.evaluate)("1", "T1"))
    assert "closed" in out["error"]


def test_browser_provider_reads_cdp_url_at_connect_time(monkeypatch):
    seen = []

    async def fake_browser_ws_url(cdp_url):
        seen.append(cdp_url)
        return "ws://127.0.0.1:5555/devtools/browser/X"

    browser = server._browser()
    assert server._browser() is browser
    monkeypatch.setattr(server, "CDP_URL", "http://127.0.0.1:5555")
    monkeypatch.setattr(server.cdp, "browser_ws_url", fake_browser_ws_url)
    assert asyncio.run(browser._ws_url()) == "ws://127.0.0.1:5555/devtools/browser/X"
    assert seen == ["http://127.0.0.1:5555"]


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
    async def fake_snapshot(page, tab_id):
        if snapshot_exc is not None:
            raise snapshot_exc
        return view, refs, url

    async def fake_run_actions(page, tab_id, action_list):
        if run_actions_exc is not None:
            raise run_actions_exc
        return steps if steps is not None else [], error

    monkeypatch.setattr(server.browse_engine, "snapshot", fake_snapshot)
    monkeypatch.setattr(server.browse_engine, "run_actions", fake_run_actions)


def test_browse_navigates_then_returns_the_view(monkeypatch):
    _stub(monkeypatch, targets=[PAGE], send_result={})
    _stub_browse(monkeypatch)

    async def fake_settle(page, timeout=2.0):
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

    async def fake_settle(page, timeout=2.0):
        return None

    monkeypatch.setattr(server.actions, "settle", fake_settle)
    out = asyncio.run(_fn(server.browse)("http://a/"))
    assert out["url"] == "http://a/redirected"


def test_browse_reports_an_unreachable_chrome(monkeypatch):
    _stub(monkeypatch, targets=None)
    out = asyncio.run(_fn(server.browse)("http://a/"))
    assert "cannot open a new tab" in out["error"]


def test_browse_reports_a_navigation_failure(monkeypatch):
    _stub(monkeypatch, targets=[PAGE], send_exc=RuntimeError("boom"))
    out = asyncio.run(_fn(server.browse)("http://a/"))
    assert "navigation failed" in out["error"]


def test_browse_reports_a_snapshot_failure(monkeypatch):
    _stub(monkeypatch, targets=[PAGE], send_result={})
    _stub_browse(monkeypatch, snapshot_exc=RuntimeError("tree gone"))

    async def fake_settle(page, timeout=2.0):
        return None

    monkeypatch.setattr(server.actions, "settle", fake_settle)
    out = asyncio.run(_fn(server.browse)("http://a/"))
    assert "snapshot failed" in out["error"]


def test_browse_view_snapshots_without_navigating(monkeypatch):
    calls = []

    async def fake_send(page, method, params=None):
        calls.append(method)
        return {}

    _stub(monkeypatch, targets=[PAGE])
    monkeypatch.setattr(server.cdp, "send", fake_send)
    _stub_browse(monkeypatch)
    out = asyncio.run(_fn(server.browse_view)("T1"))
    assert out["tab_id"] == "T1"
    assert out["refs"] == 1
    assert "Page.navigate" not in calls


def test_browse_view_reports_an_unknown_tab(monkeypatch):
    _stub(monkeypatch, targets=[])
    out = asyncio.run(_fn(server.browse_view)("T1"))
    assert out["error"] == "tab T1 not found"


def test_browse_view_reports_a_snapshot_failure(monkeypatch):
    _stub(monkeypatch, targets=[PAGE], send_result={})
    _stub_browse(monkeypatch, snapshot_exc=RuntimeError("tree gone"))
    out = asyncio.run(_fn(server.browse_view)("T1"))
    assert "snapshot failed" in out["error"]


def test_browse_act_returns_steps_and_a_fresh_view(monkeypatch):
    _stub(monkeypatch, targets=[PAGE], send_result={})
    _stub_browse(monkeypatch, steps=[{"do": "click", "ok": True}])
    out = asyncio.run(_fn(server.browse_act)([{"do": "click", "ref": 1}], "T1"))
    assert out["steps"] == [{"do": "click", "ok": True}]
    assert "error" not in out
    assert out["refs"] == 1


def test_browse_act_surfaces_the_error_and_still_returns_a_view(monkeypatch):
    _stub(monkeypatch, targets=[PAGE], send_result={})
    _stub_browse(monkeypatch,
                 steps=[{"do": "click", "ok": False, "error": "ref 1 is stale"}],
                 error="ref 1 is stale")
    out = asyncio.run(_fn(server.browse_act)([{"do": "click", "ref": 1}], "T1"))
    assert out["error"] == "ref 1 is stale"
    assert out["view"]


def test_browse_act_reports_an_unreachable_chrome(monkeypatch):
    _stub(monkeypatch, targets=None)
    out = asyncio.run(_fn(server.browse_act)([{"do": "click", "ref": 1}], "T1"))
    assert "cannot reach Chrome" in out["error"]


def test_browse_act_reports_a_snapshot_failure_after_acting(monkeypatch):
    _stub(monkeypatch, targets=[PAGE], send_result={})
    _stub_browse(monkeypatch, steps=[{"do": "click", "ok": True}],
                 snapshot_exc=RuntimeError("tree gone"))
    out = asyncio.run(_fn(server.browse_act)([{"do": "click", "ref": 1}], "T1"))
    assert "snapshot failed" in out["error"]
    assert out["steps"] == [{"do": "click", "ok": True}]


def test_browse_act_reports_an_action_batch_failure(monkeypatch):
    """I5: run_actions was previously called unguarded inside browse_act, a
    latent breach of the never-raise contract — wrap it the way snapshot
    already is."""
    _stub(monkeypatch, targets=[PAGE], send_result={})
    _stub_browse(monkeypatch, run_actions_exc=RuntimeError("registry corrupted"))
    out = asyncio.run(_fn(server.browse_act)([{"do": "click", "ref": 1}], "T1"))
    assert out["tab_id"] == "T1"
    assert out["error"] == "action batch failed: registry corrupted"


def test_browse_in_an_existing_tab_does_not_open_a_new_one(monkeypatch):
    fake = _stub(monkeypatch, targets=[PAGE], send_result={}, new="NEW")
    _stub_browse(monkeypatch)

    async def fake_settle(page, timeout=2.0):
        return None

    monkeypatch.setattr(server.actions, "settle", fake_settle)
    out = asyncio.run(_fn(server.browse)("http://a/", tab_id="T1"))
    assert out["tab_id"] == "T1"
    assert ("Target.createTarget", {"url": "about:blank"}) not in fake.calls


# --- close_tab --------------------------------------------------------------

def test_close_tab_closes_and_forgets_the_refs(monkeypatch):
    fake = _stub(monkeypatch, targets=[PAGE])
    server.browse_engine.REGISTRY["T1"] = object()
    server.browse_engine._lock_for("T1")
    out = asyncio.run(_fn(server.close_tab)("T1"))
    assert out == {"tab_id": "T1", "closed": True}
    assert ("Target.closeTarget", {"targetId": "T1"}) in fake.calls
    assert "T1" not in server.browse_engine.REGISTRY
    assert "T1" not in server.browse_engine._LOCKS


def test_close_tab_reports_an_unknown_tab(monkeypatch):
    _stub(monkeypatch, targets=[PAGE])
    out = asyncio.run(_fn(server.close_tab)("ZZ"))
    assert out["error"] == "tab ZZ not found"


def test_close_tab_reports_a_close_failure(monkeypatch):
    _stub(monkeypatch, targets=[PAGE], errors={"Target.closeTarget": RuntimeError("boom")})
    out = asyncio.run(_fn(server.close_tab)("T1"))
    assert out["error"] == "cannot close tab T1: boom"


# --- lazy tunnel restart before any CDP traffic -----------------------------

class _TunnelSpy:
    def __init__(self, events, exc=None):
        self.events = events
        self.exc = exc

    def ensure_alive(self):
        self.events.append("ensure_alive")
        if self.exc is not None:
            raise self.exc


def test_list_tabs_revives_tunnel_before_talking_to_chrome(monkeypatch):
    fake = _stub(monkeypatch, targets=[PAGE])
    monkeypatch.setattr(server, "_TUNNEL", _TunnelSpy(fake.calls))
    out = asyncio.run(_fn(server.list_tabs)())
    assert fake.calls == ["ensure_alive", ("Target.getTargets", None)]
    assert out["tabs"][0]["id"] == "T1"


def test_list_tabs_reports_a_tunnel_that_cannot_be_revived(monkeypatch):
    _stub(monkeypatch, targets=[PAGE])
    monkeypatch.setattr(server, "_TUNNEL", _TunnelSpy([], RuntimeError("ssh tunnel to u@h exited early")))
    out = asyncio.run(_fn(server.list_tabs)())
    assert "ssh tunnel to u@h exited early" in out["error"]


def test_new_tab_revives_tunnel_first(monkeypatch):
    events = []
    _stub(monkeypatch, targets=[PAGE], send_result={"frameId": "F"})
    monkeypatch.setattr(server, "_TUNNEL", _TunnelSpy(events, RuntimeError("tunnel down")))
    out = asyncio.run(_fn(server.navigate)("http://a/"))
    assert events == ["ensure_alive"]
    assert "tunnel down" in out["error"]


def test_ping_reports_a_tunnel_that_cannot_be_revived(monkeypatch):
    fake = _stub(monkeypatch, targets=[])
    monkeypatch.setattr(server, "_TUNNEL", _TunnelSpy([], RuntimeError("tunnel down")))
    out = asyncio.run(_fn(server.ping)())
    assert out["connected"] is False
    assert "tunnel down" in out["error"]
    assert fake.calls == []  # never reached Chrome through a dead tunnel


def test_main_exposes_tunnel_while_serving(monkeypatch):
    seen = []

    class FakeTunnel:
        local_url = "http://127.0.0.1:5999"
        def __init__(self, *a): pass
        def start(self): pass
        def stop(self): pass

    monkeypatch.setattr(server, "SSH_PROXY_TO", "user@remote")
    monkeypatch.setattr(server, "SSH_PROXY_PORT", None)
    monkeypatch.setattr(server, "CDP_URL", "http://remotehost:9222")
    monkeypatch.setattr(server, "_TUNNEL", None)
    monkeypatch.setattr(server, "SshLocalForwardTunnel", FakeTunnel)
    monkeypatch.setattr(server.mcp, "run", lambda: seen.append(server._TUNNEL))
    server.main()
    assert isinstance(seen[0], FakeTunnel)
    assert server._TUNNEL is None  # cleared once the tunnel is stopped


# --- CHROME_LAUNCH: lazy local Chrome ----------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("1", True), ("true", True), ("YES", True), (" on ", True),
    ("0", False), ("false", False), ("", False), (None, False),
])
def test_env_flag(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("CHROME_LAUNCH", raising=False)
    else:
        monkeypatch.setenv("CHROME_LAUNCH", raw)
    assert server._env_flag("CHROME_LAUNCH") is expected


class _LauncherSpy:
    def __init__(self, url="http://127.0.0.1:41234", exc=None):
        self.url, self.exc, self.calls = url, exc, 0

    def ensure_alive(self):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return self.url


def test_ping_launches_chrome_and_reports_its_url(monkeypatch):
    fake = _stub(monkeypatch, targets=[], version={"product": "Chrome/141.0", "protocolVersion": "1.3"})
    monkeypatch.setattr(server, "CDP_URL", "http://localhost:9222")
    monkeypatch.setattr(server, "_LAUNCHER", _LauncherSpy())
    out = asyncio.run(_fn(server.ping)())
    assert fake.calls == [("Browser.getVersion", None)]
    assert out["connected"] is True
    assert out["cdp_url"] == "http://127.0.0.1:41234"


def test_ensure_chrome_follows_a_relaunch_to_a_new_port(monkeypatch):
    spy = _LauncherSpy()
    monkeypatch.setattr(server, "CDP_URL", "http://localhost:9222")
    monkeypatch.setattr(server, "_LAUNCHER", spy)
    asyncio.run(server._ensure_chrome())
    spy.url = "http://127.0.0.1:41999"  # Chrome was closed and relaunched
    asyncio.run(server._ensure_chrome())
    assert server.CDP_URL == "http://127.0.0.1:41999"
    assert spy.calls == 2


def test_ensure_chrome_without_a_launcher_is_a_noop(monkeypatch):
    monkeypatch.setattr(server, "CDP_URL", "http://localhost:9222")
    monkeypatch.setattr(server, "_LAUNCHER", None)
    asyncio.run(server._ensure_chrome())
    assert server.CDP_URL == "http://localhost:9222"


def test_list_tabs_reports_a_chrome_that_cannot_be_launched(monkeypatch):
    _stub(monkeypatch, targets=[PAGE])
    monkeypatch.setattr(server, "_LAUNCHER", _LauncherSpy(exc=RuntimeError("Chrome not found in PATH (tried: google-chrome)")))
    out = asyncio.run(_fn(server.list_tabs)())
    assert "Chrome not found in PATH" in out["error"]


def test_new_tab_reports_a_chrome_that_cannot_be_launched(monkeypatch):
    _stub(monkeypatch, targets=[PAGE], send_result={"frameId": "F"})
    monkeypatch.setattr(server, "_LAUNCHER", _LauncherSpy(exc=RuntimeError("Chrome did not open its debugging port within 15s")))
    out = asyncio.run(_fn(server.navigate)("http://a/"))
    assert "did not open its debugging port" in out["error"]


def test_ping_reports_a_chrome_that_cannot_be_launched(monkeypatch):
    fake = _stub(monkeypatch, targets=[])
    monkeypatch.setattr(server, "_LAUNCHER", _LauncherSpy(exc=RuntimeError("Chrome not found in PATH")))
    out = asyncio.run(_fn(server.ping)())
    assert out["connected"] is False
    assert "Chrome not found in PATH" in out["error"]
    assert fake.calls == []  # never talked to Chrome when the launch failed


@pytest.mark.parametrize("cdp_url_set, ssh", [(True, None), (False, "user@remote")])
def test_main_refuses_chrome_launch_with_cdp_url_or_ssh(monkeypatch, cdp_url_set, ssh):
    monkeypatch.setattr(server, "CHROME_LAUNCH", True)
    monkeypatch.setattr(server, "_CDP_URL_SET", cdp_url_set)
    monkeypatch.setattr(server, "SSH_PROXY_TO", ssh)
    monkeypatch.setattr(server.mcp, "run", lambda: pytest.fail("must not serve a conflicting config"))
    with pytest.raises(SystemExit, match="CHROME_LAUNCH cannot be combined with CDP_URL or SSH_PROXY_TO"):
        server.main()


def test_main_with_chrome_launch_creates_the_launcher_lazily_and_stops_it(monkeypatch):
    events = []

    class FakeLauncher:
        def __init__(self):
            events.append("init")

        def ensure_alive(self):
            raise AssertionError("main() must not launch Chrome — the launch is lazy")

        def stop(self):
            events.append("stop")

    monkeypatch.setattr(server, "CHROME_LAUNCH", True)
    monkeypatch.setattr(server, "_CDP_URL_SET", False)
    monkeypatch.setattr(server, "SSH_PROXY_TO", None)
    monkeypatch.setattr(server, "_LAUNCHER", None)
    monkeypatch.setattr(server, "ChromeLauncher", FakeLauncher)
    monkeypatch.setattr(server.mcp, "run", lambda: events.append(("run", type(server._LAUNCHER).__name__)))
    server.main()
    assert events == ["init", ("run", "FakeLauncher"), "stop"]
    assert server._LAUNCHER is None  # cleared once Chrome is stopped


def test_main_without_chrome_launch_has_no_launcher(monkeypatch):
    seen = []
    monkeypatch.setattr(server, "CHROME_LAUNCH", False)
    monkeypatch.setattr(server, "SSH_PROXY_TO", None)
    monkeypatch.setattr(server, "_LAUNCHER", None)
    monkeypatch.setattr(server.mcp, "run", lambda: seen.append(server._LAUNCHER))
    server.main()
    assert seen == [None]


def test_tools_run_inside_a_call_scope(monkeypatch):
    fake = _stub(monkeypatch, targets=[], version={})
    seen = []
    real_call = fake.call

    async def call(method, params=None, **kw):
        seen.append(server.cdp._scope.get() is not None)
        return await real_call(method, params, **kw)

    fake.call = call
    asyncio.run(_fn(server.ping)())
    assert seen and all(seen)


def test_scoped_tools_keep_their_schema():
    tools = asyncio.run(server.mcp.list_tools())
    schema = next(t for t in tools if t.name == "navigate").inputSchema
    assert set(schema["properties"]) == {"url", "tab_id"}
    assert schema["required"] == ["url"]
