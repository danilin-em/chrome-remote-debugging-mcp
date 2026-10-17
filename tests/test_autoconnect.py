import asyncio
import subprocess

import pytest
import websockets
import websockets.datastructures
import websockets.http11

from chrome_remote_debugging_mcp import autoconnect, cdp
from chrome_remote_debugging_mcp.autoconnect import (
    AutoConnect,
    AutoConnectError,
    parse_active_port,
)


def test_parse_active_port_valid():
    assert parse_active_port("9222\n/devtools/browser/abc\n") == (9222, "/devtools/browser/abc")


@pytest.mark.parametrize(
    "text",
    ["", "x\n/devtools/browser/a", "0\n/devtools/browser/a", "70000\n/devtools/browser/a",
     "9222\n", "9222\n/other"],
)
def test_parse_active_port_invalid(text):
    assert parse_active_port(text) is None


def test_autoconnect_error_is_not_a_cdp_error():
    assert issubclass(AutoConnectError, ConnectionError)
    assert not issubclass(AutoConnectError, cdp.CDPError)


def test_local_ws_url(tmp_path):
    (tmp_path / "DevToolsActivePort").write_text("9222\n/devtools/browser/abc\n")
    ac = AutoConnect(None, str(tmp_path), None)
    assert asyncio.run(ac.ws_url()) == "ws://127.0.0.1:9222/devtools/browser/abc"


def test_local_missing_file(tmp_path):
    ac = AutoConnect(None, str(tmp_path), None)
    with pytest.raises(AutoConnectError, match="chrome://inspect/#remote-debugging") as exc:
        asyncio.run(ac.ws_url())
    assert " on " not in str(exc.value)


def test_local_malformed_file(tmp_path):
    (tmp_path / "DevToolsActivePort").write_text("garbage")
    ac = AutoConnect(None, str(tmp_path), None)
    with pytest.raises(AutoConnectError, match="chrome://inspect/#remote-debugging"):
        asyncio.run(ac.ws_url())


# --- ssh mode ------------------------------------------------------------------

class FakeTunnel:
    instances = []

    def __init__(self, target, host, port, local_port):
        self.args = (target, host, port, local_port)
        self.remote_port = port
        self.local_port = local_port or 5555
        self.calls = []
        FakeTunnel.instances.append(self)

    def start(self):
        self.calls.append("start")

    def ensure_alive(self):
        self.calls.append("ensure_alive")

    def stop(self):
        self.calls.append("stop")


@pytest.fixture
def ssh(monkeypatch):
    """Patch ssh + tunnel; `state['out']` is what the remote cat returns."""
    FakeTunnel.instances = []
    state = {"out": "9222\n/devtools/browser/abc\n", "rc": 0, "stderr": "", "argv": [], "kw": {}}

    def run(argv, **kw):
        state["argv"], state["kw"] = argv, kw
        if state.get("raise"):
            raise state["raise"]
        return subprocess.CompletedProcess(argv, state["rc"], state["out"], state["stderr"])

    monkeypatch.setattr(autoconnect.subprocess, "run", run)
    monkeypatch.setattr(autoconnect, "SshLocalForwardTunnel", FakeTunnel)
    return state


def test_remote_tunnel_lifecycle(ssh):
    ac = AutoConnect("u@h", "~/.config/google-chrome", None)
    assert asyncio.run(ac.ws_url()) == "ws://127.0.0.1:5555/devtools/browser/abc"
    assert len(FakeTunnel.instances) == 1
    first = FakeTunnel.instances[0]
    assert first.args == ("u@h", "127.0.0.1", 9222, None)
    assert first.calls == ["start"]

    asyncio.run(ac.ws_url())  # same port: reuse
    assert len(FakeTunnel.instances) == 1
    assert first.calls == ["start", "ensure_alive"]

    ssh["out"] = "9333\n/devtools/browser/xyz\n"
    assert asyncio.run(ac.ws_url()) == "ws://127.0.0.1:5555/devtools/browser/xyz"
    second = FakeTunnel.instances[1]
    assert first.calls[-1] == "stop"
    assert second.args == ("u@h", "127.0.0.1", 9333, 5555)  # old local port kept
    assert second.calls == ["start"]


def test_failed_repoint_is_retried_not_silently_reused(ssh, monkeypatch):
    ac = AutoConnect("u@h", "~/d", None)
    asyncio.run(ac.ws_url())
    old = FakeTunnel.instances[0]
    ssh["out"] = "9333\n/devtools/browser/xyz\n"
    monkeypatch.setattr(FakeTunnel, "start", lambda self: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(ac.ws_url())
    monkeypatch.undo()
    monkeypatch.setattr(autoconnect.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "9222\n/devtools/browser/abc\n", ""))
    monkeypatch.setattr(autoconnect, "SshLocalForwardTunnel", FakeTunnel)
    asyncio.run(ac.ws_url())  # old port again: must start a fresh tunnel
    fresh = FakeTunnel.instances[-1]
    assert fresh is not old and fresh.calls == ["start"]
    assert fresh.args[3] == 5555


def test_remote_command_quotes_dir_but_expands_tilde(ssh):
    ac = AutoConnect("u@h", "~/my dir", None)
    asyncio.run(ac.ws_url())
    assert ssh["argv"][0] == "ssh"
    assert ssh["argv"][-2] == "u@h"
    assert ssh["argv"][-1] == "cat -- ~/'my dir/DevToolsActivePort'"
    assert "ExitOnForwardFailure=yes" not in ssh["argv"]
    assert "BatchMode=yes" in ssh["argv"]
    assert ssh["kw"]["timeout"] == autoconnect.SSH_READ_TIMEOUT
    assert ssh["kw"]["stdin"] == subprocess.DEVNULL  # never the MCP stdio channel


def test_remote_command_expands_a_bare_tilde(ssh):
    ac = AutoConnect("u@h", "~", None)
    asyncio.run(ac.ws_url())
    assert ssh["argv"][-1] == "cat -- ~/DevToolsActivePort"


def test_remote_command_absolute_dir_is_quoted(ssh):
    ac = AutoConnect("u@h", "/opt/my chrome", None)
    asyncio.run(ac.ws_url())
    assert ssh["argv"][-1] == "cat -- '/opt/my chrome/DevToolsActivePort'"


def test_remote_unreachable(ssh):
    ssh.update(rc=255, stderr="Connection refused\n")
    with pytest.raises(AutoConnectError, match="cannot reach u@h over ssh: Connection refused"):
        asyncio.run(AutoConnect("u@h", "~/d", None).ws_url())


def test_remote_missing_file(ssh):
    ssh.update(rc=1)
    with pytest.raises(AutoConnectError, match=r"Chrome on u@h is not accepting"):
        asyncio.run(AutoConnect("u@h", "~/d", None).ws_url())


def test_remote_malformed_file(ssh):
    ssh["out"] = "nope"
    with pytest.raises(AutoConnectError, match="chrome://inspect/#remote-debugging"):
        asyncio.run(AutoConnect("u@h", "~/d", None).ws_url())


def test_remote_timeout(ssh):
    ssh["raise"] = subprocess.TimeoutExpired("ssh", 15)
    with pytest.raises(AutoConnectError, match="ssh to u@h did not answer within 15s"):
        asyncio.run(AutoConnect("u@h", "~/d", None).ws_url())


def test_describe():
    assert AutoConnect(None, "/d", None).describe() == "autoconnect:/d"
    assert AutoConnect("u@h", "~/d", None).describe() == "autoconnect:u@h:~/d"


def test_stop_is_idempotent(ssh):
    ac = AutoConnect("u@h", "~/d", None)
    ac.stop()  # no tunnel: no-op
    asyncio.run(ac.ws_url())
    ac.stop()
    ac.stop()
    assert FakeTunnel.instances[0].calls.count("stop") == 1


def _status(code):
    return websockets.InvalidStatus(
        websockets.http11.Response(code, "x", websockets.datastructures.Headers())
    )


def _patch_connect(monkeypatch, result=None, exc=None):
    seen = {}

    async def fake(url, **kwargs):
        seen["url"], seen["kwargs"] = url, kwargs
        if exc is not None:
            raise exc
        return result

    monkeypatch.setattr(autoconnect.websockets, "connect", fake)
    return seen


def test_connect_returns_socket_without_handshake_timeout(monkeypatch):
    sentinel = object()
    seen = _patch_connect(monkeypatch, result=sentinel)
    ac = AutoConnect(None, "/d", None)
    assert asyncio.run(ac.connect("ws://x")) is sentinel
    assert seen["url"] == "ws://x"
    assert seen["kwargs"] == {"max_size": None, "open_timeout": None}


@pytest.mark.parametrize("target,where", [(None, ""), ("u@h", " on u@h")])
def test_connect_403_explains_allow(monkeypatch, target, where):
    _patch_connect(monkeypatch, exc=_status(403))
    with pytest.raises(AutoConnectError) as e:
        asyncio.run(AutoConnect(target, "/d", None).connect("ws://x"))
    assert f"Chrome{where} refused the debugging connection (HTTP 403)" in str(e.value)
    assert "Allow" in str(e.value)
    assert isinstance(e.value.__cause__, websockets.InvalidStatus)


@pytest.mark.parametrize(
    "exc",
    [_status(500), websockets.InvalidMessage("did not receive a valid HTTP response"),
     ConnectionRefusedError(), EOFError()],
)
def test_connect_dead_port_is_not_debuggable(monkeypatch, exc):
    _patch_connect(monkeypatch, exc=exc)
    with pytest.raises(AutoConnectError) as e:
        asyncio.run(AutoConnect(None, "/d", None).connect("ws://x"))
    assert "is not accepting debugging connections" in str(e.value)
    assert type(exc).__name__ in str(e.value)
    assert e.value.__cause__ is exc


def test_wait_message():
    assert "45s" in AutoConnect(None, "/d", None).wait_message
    assert "Allow" in AutoConnect(None, "/d", None).wait_message
    assert "Chrome has not accepted" in AutoConnect(None, "/d", None).wait_message
    assert "Chrome on u@h has not accepted" in AutoConnect("u@h", "/d", None).wait_message
