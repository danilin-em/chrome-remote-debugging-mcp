"""Drive a real Chrome end to end: snapshot, act by ref, snapshot again.

Not a pytest test — it needs a live browser and the network. Run it by hand
after changing the rendering or action code.
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from chrome_remote_debugging_mcp import actions, browse, cdp  # noqa: E402

# Matches the start of any interactive line ax.py can render — "role#ref ...",
# e.g. "link#3 ..." or "button#5 ...". Keyed off the shape (a role name, "#",
# digits) rather than a hardcoded role list, so it tracks ax.INTERACTIVE_ROLES
# without duplicating it.
_REF_LINE = re.compile(r"^\S+#\d+\s")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cdp-url", default="http://127.0.0.1:9223")
    parser.add_argument("--url", default="https://news.ycombinator.com/")
    parser.add_argument("--click-text", default="More")
    args = parser.parse_args()

    targets = await cdp.list_targets(args.cdp_url)
    page = next((t for t in targets if t.get("type") == "page"), None)
    if page is None:
        sys.exit(f"no page target found at {args.cdp_url}")
    ws_url = page["webSocketDebuggerUrl"]
    tab_id = page["id"]

    await cdp.send(ws_url, "Page.navigate", {"url": args.url})
    await actions.settle(ws_url, timeout=10.0)
    await asyncio.sleep(2)

    view, refs, _ = await browse.snapshot(ws_url, tab_id)
    print(f"view: {len(view.splitlines())} lines, ~{len(view) // 4} tokens, {refs} refs")
    print("\n".join(view.splitlines()[:12]))

    wanted = next(
        (line for line in view.splitlines()
         if _REF_LINE.match(line.strip()) and f'"{args.click_text}"' in line),
        None,
    )
    if wanted is None:
        sys.exit(f"no interactive element matching {args.click_text!r} in the view")
    ref = int(wanted.strip().split("#")[1].split()[0])
    print(f"\nclicking {wanted.strip()}")

    steps, error = await browse.run_actions(ws_url, tab_id, [{"do": "click", "ref": ref}])
    print("steps:", steps, "error:", error)
    await asyncio.sleep(2)

    view2, refs2, _ = await browse.snapshot(ws_url, tab_id)
    print(f"after: {len(view2.splitlines())} lines, {refs2} refs")
    print(view2.splitlines()[0])


if __name__ == "__main__":
    asyncio.run(main())
