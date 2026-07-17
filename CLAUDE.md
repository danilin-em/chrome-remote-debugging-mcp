# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An MCP server (stdio transport, built on `FastMCP`) that controls an *already-running* Chrome over the Chrome DevTools Protocol (CDP). It does **not** launch Chrome — a browser must already be listening on the remote-debugging endpoint (`google-chrome --remote-debugging-port=9222`). Currently a skeleton meant to be extended with more tools.

## Commands

```bash
uv sync                         # venv + runtime + dev group (pytest, pytest-cov)

pytest -q                       # full suite; addopts enforce 100% branch coverage
pytest tests/test_server.py::test_ping_connected --no-cov   # single test — see gate note

python -m chrome_remote_debugging_mcp   # run server over stdio
uvx --from . chrome-remote-debugging-mcp
```

Tests need **no real Chrome and no network** — the CDP layer / websockets are monkeypatched (see `_stub` in `tests/test_server.py`, `FakeWS` in `tests/test_cdp.py`).

`pyproject.toml` bakes `--cov-branch --cov-fail-under=100` into pytest `addopts`, so **any partial run (one test or one file) reports a coverage failure and exits non-zero even when the tests themselves pass** — append `--no-cov` for those. The full `pytest` run must stay at 100%, so a new tool needs tests covering every branch (including its `{"error": ...}` paths). `__main__.py` is coverage-omitted (entry shim). CI (`.github/workflows/ci.yml`) reruns the suite on Python 3.10–3.13.

## Architecture

Two layers, deliberately separated so `cdp.py` stays testable without MCP:

- **`cdp.py`** — thin CDP client, zero MCP awareness. Two HTTP probes (`list_targets` → `GET /json/list`, `get_version` → `GET /json/version`) and one `send(ws_url, method, params)` that opens a **fresh websocket per command**, sends one frame, and reads until a message whose `id` matches (unrelated CDP *events* have no matching id and are skipped). Raises `CDPError` on a CDP-level error.
- **`server.py`** — `FastMCP` instance + all `@mcp.tool()` functions + `main()`. Each tool resolves a target, calls `cdp.send`, and shapes the result.

Two invariants to preserve when adding tools:

1. **Tools never raise — they return dicts.** On any failure return `{"error": "..."}`. Every existing tool wraps its CDP calls in try/except and converts exceptions (including `cdp.CDPError`) to an `{"error": ...}` payload. This is the contract MCP clients rely on.
2. **Target resolution goes through `_resolve_target(tab_id)`.** It filters to `type == "page"` targets, defaults to the first page tab when `tab_id` is `None`, and raises `_TargetError` (a human-readable message) when Chrome is unreachable / no tabs / unknown id / not attachable. Callers catch `_TargetError` and return it as `{"error": ...}`.

`cdp_command` is the raw escape hatch: it forwards any `method` + `params` to `cdp.send`. Because it targets a **page** websocket, only page-domain methods work (`Page.*`, `DOM.*`, `Runtime.*`, `Network.*`, …); browser-level domains (`Browser.*`, `Target.*`) do not — they'd need the browser-level socket from `get_version`, which is not wired up.

## Conventions

- Config comes from env vars read once at import in `server.py`: `CDP_URL` (default `http://localhost:9222`), and the optional SSH tunnel pair `SSH_PROXY_TO` / `SSH_PROXY_PORT`.
- **Optional SSH tunnel** (`tunnel.py`): when `SSH_PROXY_TO` (e.g. `user@remote`) is set, `main()` opens `ssh -N -L 127.0.0.1:<local>:<host>:<port> <target>` to the `CDP_URL` endpoint (now interpreted as the *remote-side* address), then rewrites the module-global `CDP_URL` to the forwarded `http://127.0.0.1:<local>`. `SSH_PROXY_PORT` fixes the local port (else auto). A local forward — not SOCKS5 — is required: Chrome's debugging port refuses any `Host` header that is not `localhost`/`127.0.0.1`. Tools are unchanged because they read `CDP_URL` at call time.
- `cdp._ids` is a module-level `itertools.count` for command/response correlation; tests monkeypatch it to reset for deterministic frame-id assertions — keep it module-level.
- All I/O is `async`; tools are `async def` and tests drive them with `asyncio.run(_fn(tool)())`. `_fn` unwraps the tool whether `@mcp.tool()` returns the raw function or a wrapper.
