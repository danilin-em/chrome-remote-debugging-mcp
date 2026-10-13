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

import pathlib
import shutil
import sys
import urllib.request

CHROME_CANDIDATES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")
DEFAULT_PROFILE_DIR = pathlib.Path.home() / ".cache" / "chrome-remote-debugging-mcp" / "chrome-profile"
STARTUP_TIMEOUT = 15.0
_ACTIVE_PORT_FILE = "DevToolsActivePort"


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
