import io
import socket
import subprocess

import pytest

from chrome_remote_debugging_mcp.server import _split_endpoint, _env_int
from chrome_remote_debugging_mcp.tunnel import (
    SshLocalForwardTunnel,
    _pick_free_port,
    _port_open,
)


def test_build_cmd_local_forward_spec():
    t = SshLocalForwardTunnel("user@remote", "localhost", 9222, local_port=5555)
    cmd = t._build_cmd()
    assert cmd[0] == "ssh"
    assert "-N" in cmd
    i = cmd.index("-L")
    assert cmd[i + 1] == "127.0.0.1:5555:localhost:9222"
    assert cmd[-1] == "user@remote"
    for flag in ("BatchMode=yes", "ExitOnForwardFailure=yes",
                 "StrictHostKeyChecking=accept-new"):
        assert flag in cmd


def test_local_url_uses_local_port():
    t = SshLocalForwardTunnel("user@remote", "localhost", 9222, local_port=6001)
    assert t.local_url == "http://127.0.0.1:6001"


def test_default_local_port_is_auto_picked():
    t = SshLocalForwardTunnel("user@remote", "localhost", 9222)
    assert 1024 < t.local_port < 65536


def test_local_port_zero_is_auto_picked():
    t = SshLocalForwardTunnel("user@remote", "localhost", 9222, local_port=0)
    assert t.local_port > 0


def test_pick_free_port_is_bindable():
    port = _pick_free_port()
    assert 1024 < port < 65536
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", port))  # nothing else holds it


def test_drain_stderr_forwards_lines(capsys):
    from types import SimpleNamespace
    t = SshLocalForwardTunnel("user@remote", "localhost", 9222, local_port=5000)
    t._proc = SimpleNamespace(stderr=iter(["boom\n", "warn: dropped\n"]))
    t._drain_stderr()
    err = capsys.readouterr().err
    assert "boom" in err
    assert "warn: dropped" in err


def test_drain_stderr_no_proc_is_noop(capsys):
    t = SshLocalForwardTunnel("user@remote", "localhost", 9222, local_port=5000)
    t._proc = None
    t._drain_stderr()  # must not raise
    assert capsys.readouterr().err == ""


def test_drain_stderr_suppresses_pipe_error(capsys):
    from types import SimpleNamespace

    def exploding_lines():
        raise ValueError("I/O operation on closed file")
        yield  # pragma: no cover — makes this a generator

    t = SshLocalForwardTunnel("user@remote", "localhost", 9222, local_port=5000)
    t._proc = SimpleNamespace(stderr=exploding_lines())
    t._drain_stderr()  # ValueError/OSError swallowed — must not raise


def test_port_open_true_then_false():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    assert _port_open(port) is True
    srv.close()
    assert _port_open(port) is False


def test_stop_without_start_is_noop():
    t = SshLocalForwardTunnel("user@remote", "localhost", 9222, local_port=5000)
    t.stop()  # _proc is None → early return, no raise
    assert t._proc is None


def test_stop_kills_when_terminate_times_out():
    class StubbornProc:
        def __init__(self):
            self.terminated = False
            self.killed = False
            self._wait_calls = 0

        def poll(self):
            return None if not self.killed else 0

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True

        def wait(self, timeout=None):
            self._wait_calls += 1
            if self._wait_calls == 1:
                raise subprocess.TimeoutExpired(cmd="ssh", timeout=timeout)
            return 0

    proc = StubbornProc()
    t = SshLocalForwardTunnel("user@remote", "localhost", 9222, local_port=5000)
    t._proc = proc
    t.stop()
    assert proc.terminated is True
    assert proc.killed is True  # terminate timed out → escalated to kill
    assert t._proc is None


def test_stop_when_process_already_dead_skips_signals():
    class DeadProc:
        def __init__(self):
            self.terminated = False

        def poll(self):
            return 0  # already exited

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    proc = DeadProc()
    t = SshLocalForwardTunnel("user@remote", "localhost", 9222, local_port=5000)
    t._proc = proc
    t.stop()
    assert proc.terminated is False  # poll() != None → no terminate/kill
    assert t._proc is None


def test_start_timeout_raises_when_port_never_opens(monkeypatch):
    from chrome_remote_debugging_mcp import tunnel as tunnel_mod

    class NeverReadyProc:
        def __init__(self):
            self.stderr = io.StringIO("")

        def poll(self):
            return None  # stays alive, but the port never opens

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr(tunnel_mod.subprocess, "Popen", lambda *a, **k: NeverReadyProc())
    monkeypatch.setattr(tunnel_mod, "_port_open", lambda port: False)
    monkeypatch.setattr(tunnel_mod.time, "sleep", lambda s: None)  # no real delay
    t = tunnel_mod.SshLocalForwardTunnel(
        "user@remote", "localhost", 9222, local_port=6444, connect_timeout=0.05
    )
    with pytest.raises(RuntimeError, match="not ready within"):
        t.start()


def test_context_manager_starts_and_stops(monkeypatch):
    from chrome_remote_debugging_mcp import tunnel as tunnel_mod

    class FakeProc:
        def __init__(self):
            self.stderr = iter([])
            self._alive = True
            self.terminated = False

        def poll(self):
            return None if self._alive else 0

        def terminate(self):
            self.terminated = True
            self._alive = False

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    fake = FakeProc()
    monkeypatch.setattr(tunnel_mod.subprocess, "Popen", lambda *a, **k: fake)
    monkeypatch.setattr(tunnel_mod, "_port_open", lambda port: True)

    with tunnel_mod.SshLocalForwardTunnel(
        "user@remote", "localhost", 9222, local_port=6333
    ) as t:
        assert t._proc is fake  # __enter__ ran start()
    assert fake.terminated is True  # __exit__ ran stop()
    assert t._proc is None


def test_split_endpoint_parses_host_and_port():
    assert _split_endpoint("http://localhost:9222") == ("localhost", 9222)


def test_split_endpoint_defaults_port_to_9222():
    assert _split_endpoint("http://127.0.0.1") == ("127.0.0.1", 9222)


def test_env_int_blank_is_none(monkeypatch):
    monkeypatch.delenv("SSH_PROXY_PORT", raising=False)
    assert _env_int("SSH_PROXY_PORT") is None
    monkeypatch.setenv("SSH_PROXY_PORT", "  ")
    assert _env_int("SSH_PROXY_PORT") is None
    monkeypatch.setenv("SSH_PROXY_PORT", "7000")
    assert _env_int("SSH_PROXY_PORT") == 7000


def test_start_early_exit_raises_with_ssh_stderr(monkeypatch):
    from chrome_remote_debugging_mcp import tunnel as tunnel_mod

    class FakeProc:
        returncode = 255
        def __init__(self):
            self.stderr = io.StringIO("Permission denied (publickey).\n")
        def poll(self):
            return 255  # already exited
        def terminate(self): pass
        def wait(self, timeout=None): return 255
        def kill(self): pass

    monkeypatch.setattr(tunnel_mod.subprocess, "Popen", lambda *a, **k: FakeProc())
    t = tunnel_mod.SshLocalForwardTunnel("user@remote", "localhost", 9222, local_port=6111)
    with pytest.raises(RuntimeError) as exc:
        t.start()
    assert "Permission denied" in str(exc.value)


def test_start_success_then_stop_terminates(monkeypatch):
    from chrome_remote_debugging_mcp import tunnel as tunnel_mod

    class FakeProc:
        def __init__(self):
            self.stderr = iter([])   # drain thread ends immediately
            self._alive = True
            self.terminated = False
        def poll(self):
            return None if self._alive else 0
        def terminate(self):
            self.terminated = True
            self._alive = False
        def wait(self, timeout=None): return 0
        def kill(self): pass

    fake = FakeProc()
    monkeypatch.setattr(tunnel_mod.subprocess, "Popen", lambda *a, **k: fake)
    monkeypatch.setattr(tunnel_mod, "_port_open", lambda port: True)
    t = tunnel_mod.SshLocalForwardTunnel("user@remote", "localhost", 9222, local_port=6222)
    t.start()
    assert t._proc is fake
    assert t.local_url == "http://127.0.0.1:6222"
    t.stop()
    assert fake.terminated is True
    assert t._proc is None
