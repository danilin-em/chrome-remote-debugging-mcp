"""Address provider for Chrome's ``autoConnect`` mode.

With remote debugging switched on in ``chrome://inspect`` (personal profile),
Chrome serves no HTTP (``/json/*`` answers 404). The only way to find its
browser websocket is ``<user-data-dir>/DevToolsActivePort`` -- line 1 is the
port, line 2 is ``/devtools/browser/<id>`` -- and that file changes whenever
debugging is toggled. ``AutoConnect.ws_url`` is the provider ``cdp.Browser``
calls on every (re)connect, so it re-reads the file each time.

Over ssh the file is read with ``ssh cat`` and a local-forward tunnel is
(re)pointed lazily at whatever port the file currently names. No HTTP here.
"""

from __future__ import annotations

import asyncio
import os
import posixpath
import shlex
import subprocess

import websockets

from .tunnel import SSH_OPTIONS, SshLocalForwardTunnel

DEFAULT_USER_DATA_DIR = "~/.config/google-chrome"
ALLOW_WAIT = 45.0
SSH_READ_TIMEOUT = 15.0

_FILE = "DevToolsActivePort"


class AutoConnectError(ConnectionError):
    """Chrome's address could not be found. Deliberately not a ``cdp.CDPError``."""


def parse_active_port(text: str) -> tuple[int, str] | None:
    """``(port, ws_path)`` from a DevToolsActivePort file, or None if malformed."""
    lines = text.splitlines()
    if len(lines) < 2 or not lines[0].strip().isdigit():
        return None
    port = int(lines[0])
    path = lines[1].strip()
    if not 1 <= port <= 65535 or not path.startswith("/devtools/browser/"):
        return None
    return port, path


class AutoConnect:
    def __init__(self, ssh_target: str | None, user_data_dir: str, local_port: int | None) -> None:
        self.ssh_target = ssh_target
        self.user_data_dir = user_data_dir
        self._local_port = local_port
        self._tunnel: SshLocalForwardTunnel | None = None

    def describe(self) -> str:
        if self.ssh_target:
            return f"autoconnect:{self.ssh_target}:{self.user_data_dir}"
        return f"autoconnect:{self.user_data_dir}"

    @property
    def _where(self) -> str:
        return f" on {self.ssh_target}" if self.ssh_target else ""

    def not_debuggable(self, detail: str) -> str:
        return (
            f"Chrome{self._where} is not accepting debugging connections: ask the user to "
            f"enable remote debugging in chrome://inspect/#remote-debugging ({detail})"
        )

    @property
    def wait_message(self) -> str:
        return (
            f"Chrome{self._where} has not accepted the debugging connection within "
            f"{ALLOW_WAIT:.0f}s: it is probably showing an \"Allow\" dialog. Ask the user "
            "to click Allow in Chrome, then call the tool again; the pending connection "
            "is kept, so no second dialog appears"
        )

    async def connect(self, url: str):
        """Open the browser websocket with no handshake timeout.

        While Chrome shows its "Allow" dialog the handshake hangs; a timeout would
        abandon the attempt and the next call would open a second dialog. The
        attempt ends on its own: Allow -> socket, Deny / debugging off -> HTTP 403,
        connection closed -> error.
        """
        try:
            return await websockets.connect(url, max_size=None, open_timeout=None)
        except websockets.InvalidStatus as exc:
            if exc.response.status_code == 403:
                raise AutoConnectError(
                    f"Chrome{self._where} refused the debugging connection (HTTP 403): "
                    "the Allow dialog was declined or remote debugging is off. Ask the "
                    "user to enable remote debugging in chrome://inspect/#remote-debugging "
                    "and click Allow, then call the tool again"
                ) from exc
            raise AutoConnectError(self.not_debuggable(f"{type(exc).__name__}: {exc}")) from exc
        except (websockets.InvalidHandshake, OSError, EOFError) as exc:
            # A stale DevToolsActivePort: refused locally, accepted-then-closed by an ssh tunnel.
            raise AutoConnectError(self.not_debuggable(f"{type(exc).__name__}: {exc}")) from exc

    async def ws_url(self) -> str:
        if self.ssh_target:
            text = await asyncio.to_thread(self._read_remote)
        else:
            text = await asyncio.to_thread(self._read_local)
        parsed = parse_active_port(text)
        if parsed is None:
            raise AutoConnectError(self.not_debuggable(f"malformed {_FILE}"))
        port, path = parsed
        if self.ssh_target:
            port = await asyncio.to_thread(self._tunnel_to, port)
        return f"ws://127.0.0.1:{port}{path}"

    def _read_local(self) -> str:
        file = os.path.join(os.path.expanduser(self.user_data_dir), _FILE)
        try:
            with open(file, encoding="utf-8") as f:
                return f.read()
        except OSError as exc:
            raise AutoConnectError(self.not_debuggable(f"cannot read {file}: {exc.strerror}")) from exc

    def _read_remote(self) -> str:
        d = self.user_data_dir
        # `~` must be expanded by the remote shell, so it stays outside the quotes.
        if d == "~" or d.startswith("~/"):
            remote_path = "~/" + shlex.quote(posixpath.join(d[2:], _FILE))
        else:
            remote_path = shlex.quote(d + "/" + _FILE)
        try:
            proc = subprocess.run(
                ["ssh", *SSH_OPTIONS, self.ssh_target, f"cat -- {remote_path}"],
                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                timeout=SSH_READ_TIMEOUT,
            )
        except subprocess.TimeoutExpired as exc:
            raise AutoConnectError(
                f"ssh to {self.ssh_target} did not answer within {SSH_READ_TIMEOUT:.0f}s"
            ) from exc
        if proc.returncode == 255:
            raise AutoConnectError(
                f"cannot reach {self.ssh_target} over ssh: {proc.stderr.strip()}"
            )
        if proc.returncode != 0:
            raise AutoConnectError(self.not_debuggable(f"{_FILE} not found in {d}"))
        return proc.stdout

    def _tunnel_to(self, remote_port: int) -> int:
        """Point the local forward at ``remote_port``; return its local port.

        Single-flight (``Browser`` serialises provider calls), so no lock. The
        local port is kept across re-pointing so a caller's address stays valid.
        """
        tunnel = self._tunnel
        if tunnel is not None and tunnel.remote_port == remote_port:
            tunnel.ensure_alive()
            return tunnel.local_port
        if tunnel is not None:
            self._local_port = tunnel.local_port
            tunnel.stop()
            self._tunnel = None  # a failed start below must not leave the stopped one
        tunnel = SshLocalForwardTunnel(self.ssh_target, "127.0.0.1", remote_port, self._local_port)
        tunnel.start()
        self._tunnel = tunnel
        self._local_port = tunnel.local_port
        return tunnel.local_port

    def stop(self) -> None:
        tunnel, self._tunnel = self._tunnel, None
        if tunnel is not None:
            tunnel.stop()
