"""Optional local Chrome auto-launch (``CHROME_LAUNCH``).

When ``CHROME_LAUNCH`` is set, the server starts its own Chrome on first use
instead of expecting one at ``CDP_URL``. Chrome is started with
``--remote-debugging-port=0`` (Chrome picks a free port) in a persistent,
dedicated profile, and writes the port it chose to
``<profile>/DevToolsActivePort`` — that file is how we learn it.

The profile is shared by every server instance on the machine. If a Chrome is
already serving it (another MCP session launched it), we attach to that one
instead of launching, and only the instance that launched Chrome stops it.

Everything here logs to **stderr**: stdout is the MCP stdio (JSON-RPC) channel
and must never be polluted — which is also why Chrome's own output goes to
``DEVNULL``.
"""

from __future__ import annotations

import contextlib
import pathlib
import shutil
import subprocess
import sys
import threading
import time
import urllib.request

CHROME_CANDIDATES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")
DEFAULT_PROFILE_DIR = pathlib.Path.home() / ".cache" / "chrome-remote-debugging-mcp" / "chrome-profile"
STARTUP_TIMEOUT = 15.0
_ACTIVE_PORT_FILE = "DevToolsActivePort"
_POLL_INTERVAL = 0.1


def _log(msg: str) -> None:
    print(f"[chrome-launcher] {msg}", file=sys.stderr, flush=True)


def _url(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def find_chrome() -> str:
    """Absolute path of the first Chrome binary found on ``PATH``."""
    for name in CHROME_CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    raise RuntimeError(f"Chrome not found in PATH (tried: {', '.join(CHROME_CANDIDATES)})")


def build_command(binary: str, profile_dir: pathlib.Path) -> list[str]:
    """argv that starts Chrome on a debugging port of Chrome's own choosing."""
    return [
        binary,
        "--remote-debugging-port=0",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
    ]


def read_active_port(profile_dir: pathlib.Path) -> int | None:
    """Port from ``<profile>/DevToolsActivePort``; ``None`` if absent or malformed."""
    try:
        text = (profile_dir / _ACTIVE_PORT_FILE).read_text(encoding="utf-8")
        port = int(text.split("\n", 1)[0].strip())
    except (OSError, ValueError):
        return None
    return port if 0 < port < 65536 else None


def _port_alive(port: int) -> bool:
    """True when a DevTools HTTP endpoint answers on ``127.0.0.1:port``."""
    try:
        with urllib.request.urlopen(f"{_url(port)}/json/version", timeout=0.5):
            return True
    except Exception:  # refused, reset, timeout, HTTP error: not a live endpoint
        return False


class ChromeLauncher:
    """Starts — or attaches to — the Chrome serving ``profile_dir``, on demand.

    ``ensure_alive()`` is the single entry point; it costs one HTTP probe when
    Chrome is already up. ``stop()`` closes Chrome only if this instance
    launched it (``owned``); an attached Chrome belongs to someone else.
    """

    def __init__(
        self,
        profile_dir: pathlib.Path = DEFAULT_PROFILE_DIR,
        *,
        startup_timeout: float = STARTUP_TIMEOUT,
    ) -> None:
        self.profile_dir = profile_dir
        self._startup_timeout = startup_timeout
        # Our Chrome child — set only while we own the Chrome serving the
        # profile. None when attached to another instance's Chrome or idle.
        self._proc: subprocess.Popen[bytes] | None = None
        self._port: int | None = None
        # Serialises ensure_alive(): concurrent tool calls run it in worker
        # threads, and two of them must not both launch Chrome.
        self._lock = threading.Lock()

    @property
    def owned(self) -> bool:
        return self._proc is not None

    def ensure_alive(self) -> str:
        """Return a live ``http://127.0.0.1:<port>``, attaching or launching as needed.

        Raises ``RuntimeError`` when no Chrome binary is found or Chrome does
        not open its debugging port within ``startup_timeout``.
        """
        with self._lock:
            if self._port is not None and _port_alive(self._port):
                return _url(self._port)
            self.stop()  # release whatever we held: a closed or hung Chrome
            port = read_active_port(self.profile_dir)
            if port is not None and _port_alive(port):
                _log(f"attaching to the Chrome already serving {self.profile_dir} on port {port}")
                self._port = port
                return _url(port)
            return self._launch()

    def _launch(self) -> str:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        # A file left by a crashed Chrome would point us at a dead port.
        (self.profile_dir / _ACTIVE_PORT_FILE).unlink(missing_ok=True)
        cmd = build_command(find_chrome(), self.profile_dir)
        _log(f"starting: {' '.join(cmd)}")
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._proc = proc
        deadline = time.monotonic() + self._startup_timeout
        while time.monotonic() < deadline:
            port = read_active_port(self.profile_dir)
            if port is not None and _port_alive(port):
                self._port = port
                if proc.poll() is not None:
                    # Our process found the profile held by a running Chrome,
                    # handed off to it and exited: we are attached, not owner.
                    self._proc = None
                _log(f"Chrome ready on {_url(port)} ({'launched' if self.owned else 'attached'})")
                return _url(port)
            time.sleep(_POLL_INTERVAL)
        self.stop()
        raise RuntimeError(
            f"Chrome did not open its debugging port within {self._startup_timeout:.0f}s "
            f"(profile {self.profile_dir}); the profile may be in use by a Chrome "
            "started without --remote-debugging-port"
        )

    def stop(self) -> None:
        """Close Chrome if we launched it; forget the port either way (idempotent)."""
        proc, self._proc, self._port = self._proc, None, None
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)
        if proc.poll() is None:
            proc.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=5)
        _log("stopped")
