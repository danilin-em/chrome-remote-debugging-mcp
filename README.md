# chrome-remote-debugging-mcp

MCP server that controls a running Chrome over the **Chrome DevTools Protocol (CDP)**.
It connects to Chrome's remote-debugging endpoint and exposes browser control as MCP tools.

## Requirements

- Python ≥ 3.10
- A Chrome/Chromium started with remote debugging:

  ```bash
  google-chrome --remote-debugging-port=9222 --user-data-dir="$HOME/.config/chrome-remote-debugging-mcp"
  ```

  `--user-data-dir` is required: since Chrome 136 the debugging port is not
  opened for the default profile. The separate profile starts logged out of
  every site; keep it at a persistent path so logins survive restarts.

  Alternatively, set `CHROME_LAUNCH=1` and let the server start Chrome itself —
  see [Auto-launch Chrome](#auto-launch-chrome-optional).

## Register with an MCP client

Example `mcpServers` entry:

```json
{
  "mcpServers": {
    "chrome-remote-debugging": {
      "command": "uvx",
      "args": ["chrome-remote-debugging-mcp"],
      "env": { "CDP_URL": "http://localhost:9222" }
    }
  }
}
```

## Tools

| Tool        | Args                        | Returns                                   |
|-------------|-----------------------------|-------------------------------------------|
| `ping`      | —                           | `{connected, cdp_url, browser, protocol}` |
| `list_tabs` | —                           | `{"tabs": [{id, title, url, type}, ...]}` |
| `navigate`  | `url`, `tab_id?`            | `{tab_id, url, frameId}`                  |
| `evaluate`  | `expression`, `tab_id`      | `{tab_id, value, type}`                   |
| `cdp_command` | `method`, `tab_id`, `params?` | `{tab_id, method, result}`            |
| `browse`      | `url`, `tab_id?`                | `{tab_id, url, view, refs}`               |
| `browse_view` | `tab_id`                        | `{tab_id, view, refs}`                    |
| `browse_act`  | `actions_list`, `tab_id`        | `{tab_id, steps, view, refs}`             |
| `close_tab`   | `tab_id`                        | `{tab_id, closed}`                        |

`navigate` and `browse` open a **new tab** when `tab_id` is omitted and return its
id; every other tab-scoped tool requires an explicit `tab_id`. Close tabs you are
done with via `close_tab`.

`cdp_command` is a raw CDP escape hatch: pass any CDP `method` + `params` and get
the raw response, for cases the typed tools above don't cover. It runs in the
tab's persistent session, so `objectId`/`nodeId` values and domain state (e.g.
after `Network.enable`) carry over between calls while the tab is open and the
server's connection to Chrome stays up. Events
are not delivered, so a command that waits for the client to answer one hangs
the tab until it is undone or the tab is closed: `Fetch.enable` (undo with
`Fetch.disable`), `Debugger.pause` (`Debugger.resume`), `Target.setAutoAttach`
with `waitForDebuggerOnStart: true` (set it back to `false`) and
`Page.setInterceptFileChooserDialog` with `enabled: true` (set it to `false`).
`Browser.*` and `Target.*` are not supported; their results are undefined.

On any Chrome connection problem a tool returns `{"error": "..."}` instead of
crashing.

### Browsing a page

`browse` returns what a person would see, not HTML. The view is built from
Chrome's accessibility tree, so it covers closed shadow DOM and cross-origin
iframes — content a JavaScript DOM walk cannot reach — while hidden elements are
filtered out by Chrome itself.

```
url    https://news.ycombinator.com/
title  Hacker News

link#1 -> #
link#2 "Hacker News" -> /news
link#3 "new" -> /newest
link#4 "past" -> /front
link#5 "comments" -> /newcomments
link#6 "ask" -> /ask
link#7 "show" -> /show
link#8 "jobs" -> /jobs
link#9 "submit" -> /submit
link#10 "login" -> /login?goto=news

  |  |  |  |
  |---|---|---|
  | 1. | link#11 -> /vote?id=49546753&how=up&goto=news | link#12 "Pre-Release of Polars 2.0" -> https://pola.rs/posts/announcing-polars-2/ link#13 "pola.rs" -> /from?site=pola.rs |
  |  | 107 points by link#14 "komape" -> /user?id=komape link#15 "2 hours ago" -> /item?id=49546753 link#16 "hide" -> /hide?id=49546753&goto=news link#17 "18 comments" -> /item?id=49546753 |  |
  …
```

(the unedited opening of a real view, rendered from the front-page tree captured in
`tests/fixtures/hn.json.gz`.
Numbering and printing order follow document order, not Chrome's
breadth-first `Accessibility.getFullAXTree` array, so the site header's nine
navigation links print first — refs `#1`-`#10` — and only then the front
page's stories. Those sit in a semantic `<table>`, so they print as a Markdown
table: each cell's content joined on one line, links keeping their refs. The
stories, ref numbers and comment counts will differ on any given run; the
front page changes constantly.)

A blank line separates blocks and indentation shows nesting, one level per
block of ancestry — see `block_path` in `ax.py`. A table (`table`, `grid`,
`treegrid`) prints as a Markdown grid: its first row is the header when it
holds a `columnheader`, otherwise the header is left blank; short rows are
padded (the tree carries no colspan), rows with no content are dropped, and a
`|` inside a cell is escaped as `\|`.

A link shows where it goes after `->`: just the path when it stays on the
page's own origin, just `#fragment` when it points at the page itself (a
`href="#"` tab or toggle prints as `-> #`), the full url otherwise; a url
longer than 120 characters is cut with `…`. A `combobox`/`listbox` lists its
options in braces — all of them, e.g. `{один | два}` — and a
`slider`/`spinbutton` shows its bounds as `[min..max]`.

Every interactive
element Chrome can address carries a `#N` ref (the rare element with no backend
DOM id — see the invariants in `CLAUDE.md` — renders without one rather than
taking a number it can't be resolved back through). Pass those refs to
`browse_act`, which runs a batch of actions and returns one new view:

```json
[{"do": "type", "ref": 9, "text": "polars"},
 {"do": "press", "key": "Enter"},
 {"do": "wait_for", "text": "results", "timeout": 5},
 {"do": "click", "ref": 20}]
```

Supported actions: `click`, `type`, `press`, `select`, `check`, `uncheck`,
`scroll`, `hover`, `wait_for`. Actions are dispatched as real CDP input events,
so sites that check `isTrusted` behave normally — except `select`: a native
`<option>` list is browser chrome, not page content, so its value is set
programmatically and `input`/`change` events are dispatched for the page to see.

**Refs live for one view.** Numbering follows document order, so any change
earlier in the page shifts every later number. Refs from an older view are
rejected with a `stale` or `not in the current view` error rather than clicking
the wrong thing. Take a new view and use its numbers.

**Refs in a child frame can be seen but not acted on.** The view is built from
every frame — including cross-origin iframes, which a JavaScript DOM walk
cannot reach at all — so an element inside one still gets a `#N` ref and
prints in the view. But every action is dispatched in the page target's own
session, which only ever reaches the main frame's renderer; a node in a
same- or cross-origin child frame lives in a different coordinate space (and,
cross-origin, a different renderer process) this tool has no way to address.
Acting on such a ref is refused with an explicit "not the main frame" error
rather than being sent anyway to fail later as a misleading stale ref.

For a page whose content arrives after load — most single-page applications —
`browse` waits for the document, not for the content. Use an explicit
`wait_for` step.

### Configuration

| Env var   | Default                   | Meaning                        |
|-----------|---------------------------|--------------------------------|
| `CDP_URL` | `http://localhost:9222`   | Chrome remote-debugging origin |
| `CHROME_LAUNCH` | unset (off)       | `1`/`true`/`yes`/`on` → start a local Chrome on first use instead of connecting to `CDP_URL`. Cannot be combined with `CDP_URL` or `SSH_PROXY_TO`. |
| `CHROME_AUTOCONNECT` | unset (off) | `1`/`true`/`yes`/`on` → attach to your own running Chrome (see [Attach to your own Chrome](#attach-to-your-own-chrome-autoconnect)). Cannot be combined with `CDP_URL` or `CHROME_LAUNCH`; works with `SSH_PROXY_TO`. |
| `CHROME_USER_DATA_DIR` | `~/.config/google-chrome` | Chrome profile directory holding `DevToolsActivePort`. Only used with `CHROME_AUTOCONNECT` (on the remote host when `SSH_PROXY_TO` is set). |
| `MCP_DEBUG_LOG` | unset (off)         | Log every tool call — arguments, result, duration — to a file. `1`/`true`/`yes`/`on` → `/tmp/chrome-remote-debugging-mcp.log`; any other value is the log file path; `0`/`false`/`no`/`off` or empty → off. Read once at startup. |

## Auto-launch Chrome (optional)

```json
{
  "mcpServers": {
    "chrome-remote-debugging": {
      "command": "uvx",
      "args": ["chrome-remote-debugging-mcp"],
      "env": { "CHROME_LAUNCH": "1" }
    }
  }
}
```

The first tool call starts Chrome (`google-chrome`, `google-chrome-stable`,
`chromium` or `chromium-browser`, whichever is first on `PATH`) with its own
persistent profile at `~/.cache/chrome-remote-debugging-mcp/chrome-profile`
and a debugging port Chrome picks itself. Log in to sites once in that window;
the logins survive restarts. If Chrome is closed, the next tool call starts it
again. Several MCP sessions share the same Chrome; it is closed when the
session that started it exits.

Do not set `CDP_URL` or `SSH_PROXY_TO` together with `CHROME_LAUNCH` — the
server refuses to start.

## Attach to your own Chrome (autoConnect)

Use your everyday Chrome, with its logins, instead of a separate debugging profile:

```json
{
  "mcpServers": {
    "chrome-remote-debugging": {
      "command": "uvx",
      "args": ["chrome-remote-debugging-mcp"],
      "env": { "CHROME_AUTOCONNECT": "1" }
    }
  }
}
```

Open `chrome://inspect/#remote-debugging` in that Chrome and switch remote
debugging on. The server finds Chrome through `<profile>/DevToolsActivePort`
(`CHROME_USER_DATA_DIR`, default `~/.config/google-chrome`); Chrome serves no
HTTP in this mode. The first tool call makes Chrome show an **Allow** dialog;
click it. That is once per server process: the connection then stays open.

- **Debugging is off.** The tool returns "Chrome is not accepting debugging
  connections: ask the user to enable remote debugging in
  chrome://inspect/#remote-debugging, then call the tool again; that call makes
  Chrome show an "Allow" dialog for the user to click". Switch it on, call
  again and click **Allow** in the dialog that call opens.
- **"has not accepted the debugging connection within 45s".** Chrome is
  waiting for you to click **Allow**. Click it and call the tool again; the
  pending connection is kept, so no second dialog appears.
- **"refused the debugging connection (HTTP 403)".** You clicked Deny, or
  debugging is off. Enable it in `chrome://inspect/#remote-debugging` and call
  the tool again; that call opens a new Allow dialog — click it.
- **The connection dropped mid-work** (Chrome restarted, debugging toggled).
  The next tool call reconnects and Chrome shows one new Allow dialog.

For a Chrome on another machine add `SSH_PROXY_TO` (e.g. `user@remote`) and,
if needed, `CHROME_USER_DATA_DIR` for the remote profile. The server reads
`DevToolsActivePort` over `ssh` and opens the `ssh -L` forward itself, on
demand; the Allow dialog appears on the remote Chrome.

Do not set `CDP_URL` or `CHROME_LAUNCH` together with `CHROME_AUTOCONNECT` —
the server refuses to start.

## SSH tunnel (optional)

To control a Chrome that only listens on a remote host's loopback, set:

| Env var | Default | Meaning |
|---------|---------|---------|
| `CDP_URL` | `http://localhost:9222` | Chrome endpoint. With a tunnel active, the **remote-side** address to forward to. |
| `SSH_PROXY_TO` | unset | SSH target, e.g. `user@remote`. Presence enables the tunnel. |
| `SSH_PROXY_PORT` | unset | Fixed local port. Auto-assigned when unset. |

The server runs `ssh -N -L 127.0.0.1:<local>:<host>:<port> <target>` and points
CDP at the forwarded port. Requires key-based SSH auth (`BatchMode=yes`) and the
`ssh` client on `PATH`. The tunnel uses `StrictHostKeyChecking=accept-new`
(trust-on-first-use: unknown host keys are accepted automatically on first
connect).

## Develop

```bash
uv sync        # venv + runtime + dev group
pytest -q
```

Locally, from a checkout (no publish needed):

```bash
uvx --from . chrome-remote-debugging-mcp
# or
python -m chrome_remote_debugging_mcp
```
