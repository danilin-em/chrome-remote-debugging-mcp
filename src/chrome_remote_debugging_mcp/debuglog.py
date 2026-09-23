"""DEBUG ONLY: log every tool call and its result to a file as plain text.

Off unless ``MCP_DEBUG_LOG`` is set. ``MCP_DEBUG_LOG=1`` (or ``true``/``yes``/
``on``) logs to ``/tmp/chrome-remote-debugging-mcp.log``; any other value is
taken as the log file path. Unset, empty or ``0``/``false``/``no``/``off``
leaves the tools unwrapped.
String values (e.g. ``view``) are written verbatim, with real newlines.
"""

from __future__ import annotations

import functools
import os
import time
from pprint import pformat

DEFAULT_PATH = "/tmp/chrome-remote-debugging-mcp.log"


def _log_path(raw: str | None) -> str | None:
    """Log file path for an ``MCP_DEBUG_LOG`` value, or ``None`` when off."""
    value = (raw or "").strip()
    if value.lower() in ("", "0", "false", "no", "off"):
        return None
    if value.lower() in ("1", "true", "yes", "on"):
        return DEFAULT_PATH
    return value


LOG_PATH = _log_path(os.environ.get("MCP_DEBUG_LOG"))


def _fmt(value) -> str:  # pragma: no cover
    if isinstance(value, dict):
        parts = []
        for k, v in value.items():
            if isinstance(v, str) and "\n" in v:
                parts.append(f"{k}:\n{v}")
            elif isinstance(v, str):
                parts.append(f"{k}: {v}")
            else:
                parts.append(f"{k}:\n{pformat(v, width=100, sort_dicts=False)}")
        return "\n".join(parts)
    return value if isinstance(value, str) else pformat(value, width=100)


def _write(text: str) -> None:  # pragma: no cover
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(text)


def _wrap(name, fn):  # pragma: no cover
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        stamp = time.strftime("%H:%M:%S")
        _write(f"\n===== {stamp} >>> {name}\n{_fmt(kwargs)}\n")
        t0 = time.monotonic()
        try:
            result = await fn(*args, **kwargs)
        except Exception as exc:
            _write(f"----- <<< {name} RAISED ({time.monotonic() - t0:.2f}s)\n{exc!r}\n")
            raise
        _write(f"----- <<< {name} ({time.monotonic() - t0:.2f}s)\n{_fmt(result)}\n")
        return result

    return wrapper


def install(mcp) -> None:
    """Wrap every registered tool's function with the file logger, if enabled."""
    if LOG_PATH is None:
        return
    for tool in mcp._tool_manager._tools.values():  # pragma: no cover
        tool.fn = _wrap(tool.name, tool.fn)
