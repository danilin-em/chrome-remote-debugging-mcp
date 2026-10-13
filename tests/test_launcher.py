"""Chrome launcher tests — no real Chrome: PATH lookup, Popen, the port probe
and the clock are faked; only _port_alive is exercised against a real socket."""

import http.server
import socket
import threading

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
