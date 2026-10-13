"""Chrome launcher tests — no real Chrome: PATH lookup, Popen, the port probe
and the clock are faked; only _port_alive is exercised against a real socket."""

import http.server
import itertools
import pathlib
import socket
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

from chrome_remote_debugging_mcp import launcher


def _write_port(profile, text):
    (profile / "DevToolsActivePort").write_text(text, encoding="utf-8")


# --- find_chrome / build_command ---------------------------------------------

def test_find_chrome_returns_first_binary_on_path(monkeypatch):
    found = {
        "google-chrome-stable": "/usr/bin/google-chrome-stable",
        "chromium": "/usr/bin/chromium",
    }
    monkeypatch.setattr(launcher.shutil, "which", found.get)
    assert launcher.find_chrome() == "/usr/bin/google-chrome-stable"


def test_find_chrome_raises_when_nothing_is_on_path(monkeypatch):
    monkeypatch.setattr(launcher.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match=r"Chrome not found in PATH \(tried: google-chrome,"):
        launcher.find_chrome()


def test_build_command(tmp_path):
    assert launcher.build_command("/usr/bin/google-chrome", tmp_path) == [
        "/usr/bin/google-chrome",
        "--remote-debugging-port=0",
        f"--user-data-dir={tmp_path}",
        "--no-first-run",
        "--no-default-browser-check",
    ]


# --- read_active_port ---------------------------------------------------------

def test_read_active_port_parses_the_first_line(tmp_path):
    _write_port(tmp_path, "41234\n/devtools/browser/abc\n")
    assert launcher.read_active_port(tmp_path) == 41234


def test_read_active_port_is_none_without_the_file(tmp_path):
    assert launcher.read_active_port(tmp_path) is None


@pytest.mark.parametrize("text", ["", "\n", "abc\n/devtools/browser/x", "0\n", "70000\n"])
def test_read_active_port_rejects_malformed_content(tmp_path, text):
    _write_port(tmp_path, text)
    assert launcher.read_active_port(tmp_path) is None


# --- _port_alive --------------------------------------------------------------

class _VersionHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass  # keep pytest output clean


def test_port_alive_true_when_an_http_endpoint_answers():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _VersionHandler)
    threading.Thread(target=srv.handle_request, daemon=True).start()
    try:
        assert launcher._port_alive(srv.server_address[1]) is True
    finally:
        srv.server_close()


class _SlowVersionHandler(_VersionHandler):
    def do_GET(self):
        time.sleep(1.0)  # a busy browser UI thread (session restore, swap)
        super().do_GET()


def test_port_alive_waits_for_a_slow_but_live_endpoint():
    # A Chrome that answers late is alive: misreading it as dead makes
    # ensure_alive kill it (when owned) or unlink the shared port file.
    srv = http.server.HTTPServer(("127.0.0.1", 0), _SlowVersionHandler)
    threading.Thread(target=srv.handle_request, daemon=True).start()
    try:
        assert launcher._port_alive(srv.server_address[1]) is True
    finally:
        srv.server_close()


def test_port_alive_false_when_nothing_listens():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert launcher._port_alive(port) is False


# --- _log ---------------------------------------------------------------------

def test_log_goes_to_stderr_never_stdout(capsys):
    # stdout is the MCP JSON-RPC channel; a stray line there corrupts it.
    launcher._log("hello")
    out, err = capsys.readouterr()
    assert out == ""
    assert err == "[chrome-launcher] hello\n"


# --- ChromeLauncher -----------------------------------------------------------

class FakeProc:
    """Popen stand-in: alive until terminated; ``stubborn`` ignores SIGTERM."""

    def __init__(self, alive=True, stubborn=False):
        self.alive, self.stubborn, self.calls = alive, stubborn, []

    def poll(self):
        return None if self.alive else 0

    def terminate(self):
        self.calls.append("terminate")
        if not self.stubborn:
            self.alive = False

    def kill(self):
        self.calls.append("kill")
        self.alive = False

    def wait(self, timeout=None):
        if self.alive:
            raise subprocess.TimeoutExpired("chrome", timeout)
        return 0


def _live_ports(monkeypatch):
    """Fake the port probe: a port is alive iff it is in the returned set."""
    live = set()
    monkeypatch.setattr(launcher, "_port_alive", lambda port: port in live)
    return live


def _fake_chrome(monkeypatch, live, *, ports=(41234,), write=True, alive=True, stubborn=False):
    """Fake PATH lookup + Popen. Each spawn takes the next port from ``ports``,
    writes it to DevToolsActivePort (when ``write``) and marks it live.
    Returns the spawn log: a list of ``(argv, popen_kwargs, FakeProc)``."""
    spawns = []
    next_port = iter(ports)
    monkeypatch.setattr(launcher, "find_chrome", lambda: "/usr/bin/google-chrome")

    def fake_popen(cmd, **kwargs):
        proc = FakeProc(alive=alive, stubborn=stubborn)
        spawns.append((cmd, kwargs, proc))
        if write:
            port = next(next_port)
            profile = pathlib.Path(cmd[2].split("=", 1)[1])
            _write_port(profile, f"{port}\n/devtools/browser/x\n")
            live.add(port)
        return proc

    monkeypatch.setattr(launcher.subprocess, "Popen", fake_popen)
    return spawns


def test_ensure_alive_launches_chrome_and_owns_it(monkeypatch, tmp_path):
    live = _live_ports(monkeypatch)
    spawns = _fake_chrome(monkeypatch, live)
    profile = tmp_path / "nested" / "profile"  # does not exist yet
    chrome = launcher.ChromeLauncher(profile)

    assert chrome.ensure_alive() == "http://127.0.0.1:41234"
    assert chrome.owned is True
    assert profile.is_dir()
    cmd, kwargs, _ = spawns[0]
    assert cmd == launcher.build_command("/usr/bin/google-chrome", profile)
    # Chrome's output must never reach the MCP stdio channel.
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stdin"] is subprocess.DEVNULL


def test_ensure_alive_reuses_a_live_chrome_without_respawning(monkeypatch, tmp_path):
    live = _live_ports(monkeypatch)
    spawns = _fake_chrome(monkeypatch, live)
    chrome = launcher.ChromeLauncher(tmp_path)
    chrome.ensure_alive()
    assert chrome.ensure_alive() == "http://127.0.0.1:41234"
    assert len(spawns) == 1


def test_ensure_alive_attaches_to_a_chrome_already_serving_the_profile(monkeypatch, tmp_path):
    live = _live_ports(monkeypatch)
    live.add(50000)
    _write_port(tmp_path, "50000\n/devtools/browser/y\n")
    spawns = _fake_chrome(monkeypatch, live)
    chrome = launcher.ChromeLauncher(tmp_path)

    assert chrome.ensure_alive() == "http://127.0.0.1:50000"
    assert chrome.owned is False
    assert spawns == []


def test_ensure_alive_is_not_owner_when_chrome_hands_off_to_a_running_one(monkeypatch, tmp_path):
    # Our process found the profile locked by another Chrome, handed off and
    # exited; the other Chrome wrote DevToolsActivePort.
    live = _live_ports(monkeypatch)
    _fake_chrome(monkeypatch, live, alive=False)
    chrome = launcher.ChromeLauncher(tmp_path)

    assert chrome.ensure_alive() == "http://127.0.0.1:41234"
    assert chrome.owned is False


def test_ensure_alive_relaunches_after_chrome_was_closed(monkeypatch, tmp_path):
    live = _live_ports(monkeypatch)
    spawns = _fake_chrome(monkeypatch, live, ports=(41234, 41999))
    chrome = launcher.ChromeLauncher(tmp_path)
    chrome.ensure_alive()

    spawns[0][2].alive = False  # the user closed the window
    live.clear()
    assert chrome.ensure_alive() == "http://127.0.0.1:41999"
    assert len(spawns) == 2
    assert chrome.owned is True


def test_ensure_alive_stops_its_own_hung_chrome_before_relaunching(monkeypatch, tmp_path):
    live = _live_ports(monkeypatch)
    spawns = _fake_chrome(monkeypatch, live, ports=(41234, 41999))
    chrome = launcher.ChromeLauncher(tmp_path)
    chrome.ensure_alive()

    live.discard(41234)  # port stopped answering, process still alive
    assert chrome.ensure_alive() == "http://127.0.0.1:41999"
    assert spawns[0][2].calls == ["terminate"]


def test_ensure_alive_times_out_kills_chrome_and_explains(monkeypatch, tmp_path):
    live = _live_ports(monkeypatch)
    _write_port(tmp_path, "50000\n")  # stale: nothing answers on 50000
    spawns = _fake_chrome(monkeypatch, live, write=False)
    ticks = itertools.count(0, 0.1)
    monkeypatch.setattr(
        launcher, "time", SimpleNamespace(monotonic=lambda: next(ticks), sleep=lambda s: None)
    )
    chrome = launcher.ChromeLauncher(tmp_path, startup_timeout=0.25)

    with pytest.raises(RuntimeError) as exc:
        chrome.ensure_alive()
    msg = str(exc.value)
    assert str(tmp_path) in msg
    assert "--remote-debugging-port" in msg
    assert not (tmp_path / "DevToolsActivePort").exists()  # stale file removed
    assert spawns[0][2].calls == ["terminate"]
    assert chrome.owned is False


def test_ensure_alive_fails_fast_when_chrome_exits_without_a_port(monkeypatch, tmp_path):
    # e.g. no display, or a sandboxed (snap) Chromium that cannot start:
    # our process is gone and no port will ever appear.
    live = _live_ports(monkeypatch)
    spawns = _fake_chrome(monkeypatch, live, write=False, alive=False)
    ticks = itertools.count(0, 0.1)
    monkeypatch.setattr(
        launcher, "time", SimpleNamespace(monotonic=lambda: next(ticks), sleep=lambda s: None)
    )
    chrome = launcher.ChromeLauncher(tmp_path, startup_timeout=15)

    with pytest.raises(RuntimeError) as exc:
        chrome.ensure_alive()
    msg = str(exc.value)
    assert "exited with code 0" in msg
    assert str(tmp_path) in msg
    assert next(ticks) < 5  # gave up after a short grace, not the 15 s timeout
    assert spawns[0][2].calls == []
    assert chrome.owned is False


def test_ensure_alive_reports_a_missing_binary(monkeypatch, tmp_path):
    _live_ports(monkeypatch)
    monkeypatch.setattr(launcher.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="Chrome not found in PATH"):
        launcher.ChromeLauncher(tmp_path).ensure_alive()


def test_concurrent_ensure_alive_launches_chrome_once(monkeypatch, tmp_path):
    live = _live_ports(monkeypatch)
    spawns = _fake_chrome(monkeypatch, live)
    fake_popen = launcher.subprocess.Popen

    def slow_popen(cmd, **kwargs):
        time.sleep(0.05)  # widen the race window
        return fake_popen(cmd, **kwargs)

    monkeypatch.setattr(launcher.subprocess, "Popen", slow_popen)
    chrome = launcher.ChromeLauncher(tmp_path)
    urls = []
    threads = [threading.Thread(target=lambda: urls.append(chrome.ensure_alive())) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert urls == ["http://127.0.0.1:41234"] * 4
    assert len(spawns) == 1


def test_stop_terminates_an_owned_chrome(monkeypatch, tmp_path):
    live = _live_ports(monkeypatch)
    spawns = _fake_chrome(monkeypatch, live)
    chrome = launcher.ChromeLauncher(tmp_path)
    chrome.ensure_alive()

    chrome.stop()
    assert spawns[0][2].calls == ["terminate"]
    assert chrome.owned is False
    chrome.stop()  # idempotent
    assert spawns[0][2].calls == ["terminate"]


def test_stop_kills_a_chrome_that_ignores_terminate(monkeypatch, tmp_path):
    live = _live_ports(monkeypatch)
    spawns = _fake_chrome(monkeypatch, live, stubborn=True)
    chrome = launcher.ChromeLauncher(tmp_path)
    chrome.ensure_alive()

    chrome.stop()
    assert spawns[0][2].calls == ["terminate", "kill"]


def test_stop_leaves_a_chrome_that_already_exited_alone(monkeypatch, tmp_path):
    live = _live_ports(monkeypatch)
    spawns = _fake_chrome(monkeypatch, live)
    chrome = launcher.ChromeLauncher(tmp_path)
    chrome.ensure_alive()
    spawns[0][2].alive = False

    chrome.stop()
    assert spawns[0][2].calls == []
