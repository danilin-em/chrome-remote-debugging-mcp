"""Capture accessibility-tree fixtures from a live Chrome.

Developer tool, not part of the shipped package. Run it when fixtures need
refreshing; see the docstring of ``main`` for the required harness.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from chrome_remote_debugging_mcp import cdp  # noqa: E402

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "tests" / "fixtures"

# Fields the renderer reads. Everything else is dropped so fixtures stay small.
KEEP = ("nodeId", "parentId", "childIds", "role", "name", "value",
        "properties", "ignored", "backendDOMNodeId")

PAGES = [
    ("probe", "http://127.0.0.1:9443/probe.html", 2.0, False),
    ("hn", "https://news.ycombinator.com/", 3.5, True),
    ("youtube", "https://www.youtube.com/results?search_query=4k+video", 7.0, True),
    ("wikipedia", "https://en.wikipedia.org/wiki/Web_accessibility", 4.0, True),
]


def trim(nodes: list[dict]) -> list[dict]:
    """Keep only the fields the renderer reads, collapsing name/value/role blobs."""
    out = []
    for node in nodes:
        kept = {k: node[k] for k in KEEP if k in node}
        if "name" in kept:
            kept["name"] = {"value": (node["name"] or {}).get("value", "")}
        if "value" in kept:
            kept["value"] = {"value": (node["value"] or {}).get("value")}
        if "role" in kept:
            kept["role"] = {"value": node["role"].get("value")}
        out.append(kept)
    return out


async def capture(cdp_url: str, ws_url: str, url: str, wait: float) -> list[dict]:
    await cdp.send(ws_url, "Page.navigate", {"url": url})
    await asyncio.sleep(wait)
    tree = await cdp.send(ws_url, "Page.getFrameTree")
    frames: list[str] = []

    def walk(frame: dict) -> None:
        frames.append(frame["frame"]["id"])
        for child in frame.get("childFrames", []):
            walk(child)

    walk(tree["frameTree"])
    out = []
    for frame_id in frames:
        ax = await cdp.send(ws_url, "Accessibility.getFullAXTree", {"frameId": frame_id})
        out.append({"frameId": frame_id, "nodes": ax["nodes"]})
    return out


async def main() -> None:
    """Capture every fixture.

    Requires a harness the script does not manage:

        google-chrome --headless=new --remote-debugging-port=9223 \\
            --user-data-dir=/tmp/crdm-fixtures about:blank &
        python3 -m http.server 9443 --bind 127.0.0.1 -d tests/fixtures &
        python3 -m http.server 9444 --bind 127.0.0.1 -d tests/fixtures &
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--cdp-url", default="http://127.0.0.1:9223")
    args = parser.parse_args()

    targets = await cdp.list_targets(args.cdp_url)
    page = next(t for t in targets if t.get("type") == "page")
    ws_url = page["webSocketDebuggerUrl"]

    FIXTURES.mkdir(parents=True, exist_ok=True)
    for name, url, wait, compress in PAGES:
        frames = await capture(args.cdp_url, ws_url, url, wait)
        if name == "probe":
            # Kept raw and uncompressed: small, and the only fixture that still
            # carries name.sources, which test_strip_sources needs.
            (FIXTURES / "probe_raw.json").write_text(
                json.dumps(frames, ensure_ascii=False), encoding="utf-8"
            )
        for frame in frames:
            frame["nodes"] = trim(frame["nodes"])
        blob = json.dumps(frames, ensure_ascii=False)
        if compress:
            with gzip.open(FIXTURES / f"{name}.json.gz", "wt", encoding="utf-8") as fh:
                fh.write(blob)
        else:
            (FIXTURES / f"{name}.json").write_text(blob, encoding="utf-8")
        print(f"{name}: {len(frames)} frame(s), "
              f"{sum(len(f['nodes']) for f in frames)} nodes")


if __name__ == "__main__":
    asyncio.run(main())
