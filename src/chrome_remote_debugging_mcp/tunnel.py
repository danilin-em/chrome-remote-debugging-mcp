"""Optional embedded SSH local-port-forward tunnel.

When ``SSH_PROXY_TO`` is set, the server spawns
``ssh -N -L 127.0.0.1:<local_port>:<remote_host>:<remote_port> <target>`` and
points the CDP layer at the forwarded local port. This lets the MCP server run
on the host while reaching a Chrome remote-debugging endpoint that only listens
on a *remote* host's loopback (e.g. Chrome started with
``--remote-debugging-port=9222`` on a box reachable only over SSH).

A local forward (``-L``) is used rather than a SOCKS5 dynamic forward (``-D``)
because Chrome's debugging port has DNS-rebinding protection: it refuses any
request whose ``Host`` header is not ``localhost``/``127.0.0.1``. Forwarding a
local port keeps the ``Host`` header at ``127.0.0.1`` and makes Chrome emit
``webSocketDebuggerUrl`` values that already point back through the local port.

Everything here logs to **stderr**: stdout is the MCP stdio (JSON-RPC) channel
and must never be polluted.
"""

from __future__ import annotations

import atexit
import contextlib
import socket
import subprocess
import sys
import threading
import time


def _pick_free_port() -> int:
    """Ask the OS for an unused loopback TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _port_open(port: int) -> bool:
    """True once something accepts connections on 127.0.0.1:port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _log(msg: str) -> None:
    print(f"[ssh-tunnel] {msg}", file=sys.stderr, flush=True)


class SshLocalForwardTunnel:
    """A managed ``ssh -N -L`` local port forward.

    Lifecycle mirrors the server: ``start()`` before serving, ``stop()`` after.
    Forwards a local loopback port to ``remote_host:remote_port`` as resolved on
    the *remote* side of the ssh connection.
    """

    def __init__(
        self,
        target: str,
        remote_host: str,
        remote_port: int,
        local_port: int | None = None,
        *,
        connect_timeout: float = 15.0,
    ) -> None:
        self.target = target
        self.remote_host = remote_host
        self.remote_port = remote_port
        self.local_port = local_port if (local_port is not None and local_port > 0) else _pick_free_port()
        self._connect_timeout = connect_timeout
        self._proc: subprocess.Popen[str] | None = None

    @property
    def local_url(self) -> str:
        return f"http://127.0.0.1:{self.local_port}"

    def _build_cmd(self) -> list[str]:
        # BatchMode=yes: fail fast instead of hanging on a password prompt
        #   (key-based auth is required for an unattended tunnel).
        # ExitOnForwardFailure=yes: if the local -L bind fails, ssh exits so we
        #   surface the error rather than a silently-dead forward.
        # ServerAlive*: detect a dropped connection and let ssh tear down.
        return [
            "ssh",
            "-N",
            "-T",
            "-o", "BatchMode=yes",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3",
            "-o", "StrictHostKeyChecking=accept-new",
            "-L", f"127.0.0.1:{self.local_port}:{self.remote_host}:{self.remote_port}",
            self.target,
        ]

    def _drain_stderr(self) -> None:
        """Forward the ssh child's stderr to our log so a full pipe never
        blocks it and steady-state ssh diagnostics stay visible."""
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            for line in proc.stderr:
                _log(line.rstrip())
        except (ValueError, OSError):
            pass  # pipe closed during shutdown

    def start(self) -> None:
        """Spawn ssh and block until the forwarded port accepts connections."""
        cmd = self._build_cmd()
        _log(f"starting: {' '.join(cmd)}")
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        # Ensure the child never outlives us even on an unclean exit.
        atexit.register(self.stop)

        deadline = time.monotonic() + self._connect_timeout
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                err = self._proc.stderr.read() if self._proc.stderr else ""
                raise RuntimeError(
                    f"ssh tunnel to {self.target} exited early "
                    f"(code {self._proc.returncode}): {err.strip() or '<no stderr>'}"
                )
            if _port_open(self.local_port):
                threading.Thread(target=self._drain_stderr, daemon=True).start()
                _log(f"local forward ready on {self.local_url}")
                return
            time.sleep(0.2)

        self.stop()
        raise RuntimeError(
            f"ssh tunnel to {self.target} not ready within {self._connect_timeout:.0f}s"
        )

    def stop(self) -> None:
        """Terminate the ssh child (idempotent)."""
        if self._proc is None:
            return
        proc, self._proc = self._proc, None
        if proc.poll() is None:
            proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=5)
            if proc.poll() is None:
                proc.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=5)
        _log("stopped")

    def __enter__(self) -> "SshLocalForwardTunnel":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
