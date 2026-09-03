"""Drive a real Chrome end to end: snapshot, act by ref, snapshot again.

Not a pytest test — it needs a live browser and the network. Run it by hand
after changing the rendering or action code.
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from chrome_remote_debugging_mcp import actions, browse, cdp  # noqa: E402


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cdp-url", default="http://127.0.0.1:9223")
    parser.add_argument("--url", default="https://news.ycombinator.com/")
    parser.add_argument("--click-text", default="More")
    args = parser.parse_args()

    targets = await cdp.list_targets(args.cdp_url)
    page = next(t for t in targets if t.get("type") == "page")
    ws_url = page["webSocketDebuggerUrl"]
    tab_id = page["id"]

    await cdp.send(ws_url, "Page.navigate", {"url": args.url})
    await actions.settle(ws_url, timeout=10.0)
    await asyncio.sleep(2)

    view, refs = await browse.snapshot(ws_url, tab_id)
    print(f"view: {len(view.splitlines())} lines, ~{len(view) // 4} tokens, {refs} refs")
    print("\n".join(view.splitlines()[:12]))

    wanted = next(
        (line for line in view.splitlines()
         if line.strip().startswith("link#") and f'"{args.click_text}"' in line),
        None,
    )
    if wanted is None:
        print(f"\nno link named {args.click_text!r} in the view")
        return
    ref = int(wanted.strip().split("#")[1].split()[0])
    print(f"\nclicking {wanted.strip()}")

    steps, error = await browse.run_actions(ws_url, tab_id, [{"do": "click", "ref": ref}])
    print("steps:", steps, "error:", error)
    await asyncio.sleep(2)

    view2, refs2 = await browse.snapshot(ws_url, tab_id)
    print(f"after: {len(view2.splitlines())} lines, {refs2} refs")
    print(view2.splitlines()[0])


if __name__ == "__main__":
    asyncio.run(main())
