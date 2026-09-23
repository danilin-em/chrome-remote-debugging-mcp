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

uv run python scripts/capture_ax_fixtures.py   # refresh tests/fixtures (needs live Chrome; see its docstring)
uv run python scripts/smoke_browse.py          # end-to-end check against a live Chrome
```

Tests need **no real Chrome and no network** — the CDP layer / websockets are monkeypatched (see `_stub` in `tests/test_server.py`, `FakeWS` in `tests/test_cdp.py`).

`pyproject.toml` bakes `--cov-branch --cov-fail-under=100` into pytest `addopts`, so **any partial run (one test or one file) reports a coverage failure and exits non-zero even when the tests themselves pass** — append `--no-cov` for those. The full `pytest` run must stay at 100%, so a new tool needs tests covering every branch (including its `{"error": ...}` paths). `__main__.py` is coverage-omitted (entry shim). CI (`.github/workflows/ci.yml`) reruns the suite on Python 3.10–3.13.

## Architecture

Five modules, layered so the I/O-free pieces (`cdp.py`, `ax.py`) stay testable
without a browser and the MCP-aware layer (`server.py`) stays thin:

- **`cdp.py`** — thin CDP client, zero MCP awareness. HTTP endpoints (`list_targets` → `GET /json/list`, `get_version` → `GET /json/version`, `new_target` → `PUT /json/new?about:blank`, `close_target` → `GET /json/close/{id}`) and one `send(ws_url, method, params)` that opens a **fresh websocket per command**, sends one frame, and reads until a message whose `id` matches (unrelated CDP *events* have no matching id and are skipped). Raises `CDPError` on a CDP-level error.
- **`ax.py`** — accessibility tree → view lines. **Pure: no I/O, no `cdp` import.** Holds every rendering rule (role tables, filtering, block grouping, indentation). Because it is pure it is tested directly against captured trees in `tests/fixtures/`, with no Chrome and no mocks. When it needs data the tree lacks — DOM attributes for elements Chrome could not name — it does not fetch: `unnamed_backend_ids(nodes)` reports what is needed and the caller passes it back via `render_nodes(..., attrs=...)`.
- **`actions.py`** — one page action → CDP frames. Uses real `Input.*` events resolved through `DOM.getBoxModel`, never synthetic JS events, so `isTrusted` is true — with one documented exception: `select` sets a `<select>`'s value with `Runtime.callFunctionOn` and dispatches `input`/`change` itself, because a native option popup is browser chrome, not page content, and cannot be driven by a click. Handlers that convert a CDP failure into a stale-ref message catch `cdp.CDPError` only, never bare `Exception`, so a transport failure (e.g. the websocket itself dying) is reported honestly instead of being misdiagnosed as a stale ref. Raises `ActionError` with agent-readable text.
- **`browse.py`** — per-tab ref registry (`REGISTRY[tab_id] = _Snapshot(refs, main_frame_id, loader_id)`, where `refs` is `{ref: (frame_id, backend_node_id)}`), snapshot assembly across frames guarded by a per-tab `asyncio.Lock`, and batch execution that stops at the first failure. Registry entries (and their locks, in `_LOCKS`) are pruned only by `browse.forget`, which the `close_tab` tool calls; a tab closed any other way (by the user, by the page) leaves its entry behind.
- **`server.py`** — `FastMCP` instance + all `@mcp.tool()` functions + `main()`. Imports the browse module as `from . import browse as browse_engine`, because the tool function `browse` would otherwise shadow the module name — never write `import browse` when describing this layout.

Three invariants to preserve when adding tools:

1. **Tools never raise — they return dicts.** On any failure return `{"error": "..."}`. Every existing tool wraps its CDP calls in try/except and converts exceptions (including `cdp.CDPError`) to an `{"error": ...}` payload. This is the contract MCP clients rely on.
2. **Target resolution goes through `_resolve_target(tab_id)`.** It filters to `type == "page"` targets and raises `_TargetError` (a human-readable message) when Chrome is unreachable / unknown id / not attachable. There is no "default tab": only `navigate` and `browse` accept `tab_id=None`, and they route it through `_target_or_new`, which opens a fresh tab via `cdp.new_target` (`PUT /json/new?about:blank`, then `Page.navigate`). Every other tab-scoped tool takes a required `tab_id`. Callers catch `_TargetError` and return it as `{"error": ...}`.
3. **`ax.py` stays pure.** No imports from `cdp`, no `async`. This is what makes the rendering rules testable against fixtures; adding I/O there would force mocks into every rule test.

`cdp_command` is the raw escape hatch: it forwards any `method` + `params` to `cdp.send`. Because it targets a **page** websocket, only page-domain methods work (`Page.*`, `DOM.*`, `Runtime.*`, `Network.*`, …); browser-level domains (`Browser.*`, `Target.*`) do not — they'd need the browser-level socket from `get_version`, which is not wired up.

**Refs are per-snapshot, by design.** Numbering follows tree order, so a DOM change earlier in the document shifts every later ref. `browse.snapshot` replaces the tab's registry wholesale, and staleness is detected per verb by whichever CDP getter that verb calls against the (possibly dead) backend id: `DOM.getBoxModel` via `resolve_box` for `click`, `type` (which clicks first) and `hover`; `DOM.scrollIntoViewIfNeeded` called directly for a bare scroll-by-ref; and `DOM.resolveNode` via `_call_on_node` for `select`, `check` and `uncheck` — whichever one runs, a dead node makes it fail with `cdp.CDPError`, caught and re-raised as an agent-readable stale-ref message. An interactive node with no `backendDOMNodeId` renders without a `#N` rather than consuming a ref number it could never resolve back through (see `_content_line` in `ax.py`) — this is what keeps the printed refs and the ref map in lockstep by construction; do not "fix" it into always numbering. Do not add ref stability without re-reading D4 in `.superpowers/docs/specs/2026-09-03-browse-tools-design.md`.

**Actions can only reach the main frame.** Every action is dispatched against the page target's own websocket, which addresses only the main frame's renderer — a node from a same- or cross-origin child frame lives in a different coordinate space (and, cross-origin, a different renderer process) this tool cannot reach. `browse._resolve` therefore refuses a ref whose `frame_id` differs from the snapshot's `main_frame_id` with an honest "not the main frame" error instead of forwarding it to fail later as a misleading stale ref. Do not attempt coordinate offsetting or a per-frame socket to "fix" this — it is a documented limitation (see the design document's Out of scope section), not a bug.

**A snapshot's refs are only valid for that document, not just that array shape.** `browse.snapshot` stamps each `_Snapshot` with the main frame's `Page.Frame.loaderId`. `backendNodeId` numbering is monotonic and unique only within one renderer process; a cross-process navigation restarts it from 1, so without this stamp an old ref could resolve to a live, unrelated node in the new document instead of failing as stale. `run_actions` re-fetches the current loaderId before running any action in a batch and refuses the whole batch if it no longer matches.

**Snapshot assembly is serialised per tab.** `browse.snapshot` runs its whole body under `_lock_for(tab_id)`, an `asyncio.Lock` created lazily per tab id. Without it, two concurrent `browse`/`browse_view` calls on the same tab could finish in either order and the one that lost the race would return a view whose refs no longer match what `REGISTRY[tab_id]` ends up holding. The lock is per-tab, never global — do not collapse `_LOCKS` into a single lock; that would serialise unrelated tabs against each other for no reason.

**`wait_for` refuses `text` and `ref_gone` together.** `actions.wait_for` would silently prefer `text` if both were passed through; `browse._perform` rejects the combination up front with an `ActionError` instead of letting one condition win unannounced. Do not remove this guard to simplify the call site — it is the only thing standing between "ambiguous input" and "silently wrong behavior".

**The accessibility tree does not include child frames.** The main frame's `Iframe` node has empty `childIds`. `browse.snapshot` therefore walks `Page.getFrameTree` and calls `Accessibility.getFullAXTree({frameId})` once per frame. In the assembled view, every frame after the first is marked with a `frame <id>` line showing only the **first 8 characters** of the frame id (a display truncation only — the registry keys on the full id, never the truncated one).

**`Accessibility.*` and `DOM.*` getters need no `enable` call** and work on the stateless fresh-socket-per-command transport; `enable` gates a domain's event stream, not its commands. That is why `cdp.py` needs no session support.

## Conventions

- Config comes from env vars read once at import in `server.py`: `CDP_URL` (default `http://localhost:9222`), and the optional SSH tunnel pair `SSH_PROXY_TO` / `SSH_PROXY_PORT`.
- **Optional SSH tunnel** (`tunnel.py`): when `SSH_PROXY_TO` (e.g. `user@remote`) is set, `main()` opens `ssh -N -L 127.0.0.1:<local>:<host>:<port> <target>` to the `CDP_URL` endpoint (now interpreted as the *remote-side* address), then rewrites the module-global `CDP_URL` to the forwarded `http://127.0.0.1:<local>`. `SSH_PROXY_PORT` fixes the local port (else auto). A local forward — not SOCKS5 — is required: Chrome's debugging port refuses any `Host` header that is not `localhost`/`127.0.0.1`. Tools are unchanged because they read `CDP_URL` at call time.
- `cdp._ids` is a module-level `itertools.count` for command/response correlation; tests monkeypatch it to reset for deterministic frame-id assertions — keep it module-level.
- All I/O is `async`; tools are `async def` and tests drive them with `asyncio.run(_fn(tool)())`. `_fn` unwraps the tool whether `@mcp.tool()` returns the raw function or a wrapper.
