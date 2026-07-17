# chrome-remote-debugging-mcp

MCP server that controls a running Chrome over the **Chrome DevTools Protocol (CDP)**.
It connects to Chrome's remote-debugging endpoint and exposes browser control as MCP tools.

## Requirements

- Python ≥ 3.10
- A Chrome/Chromium started with remote debugging:

  ```bash
  google-chrome --remote-debugging-port=9222
  ```

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
| `evaluate`  | `expression`, `tab_id?`     | `{tab_id, value, type}`                   |
| `cdp_command` | `method`, `params?`, `tab_id?` | `{tab_id, method, result}`           |

`cdp_command` is a raw CDP escape hatch: pass any CDP `method` + `params` and get
the raw response, for cases the typed tools above don't cover. It targets a *page*
socket, so page-domain methods work (`Page.*`, `DOM.*`, `Runtime.*`,
`Network.*`, …); browser-level domains (`Browser.*`, `Target.*`) do not.

On any Chrome connection problem a tool returns `{"error": "..."}` instead of
crashing.

### Configuration

| Env var   | Default                   | Meaning                        |
|-----------|---------------------------|--------------------------------|
| `CDP_URL` | `http://localhost:9222`   | Chrome remote-debugging origin |

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
