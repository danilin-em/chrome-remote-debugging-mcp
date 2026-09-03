"""Assemble page views and run batches of actions against them.

Holds the per-tab ref registry. A ref is only meaningful for the snapshot that
produced it: numbering follows tree order, so any change earlier in the document
shifts every later number (D4 in the design document). Each snapshot therefore
replaces its tab's registry wholesale.
"""

from __future__ import annotations

from . import actions, ax, cdp

# {tab_id: {ref: (frame_id, backend_node_id)}}
REGISTRY: dict[str, dict[int, tuple[str, int]]] = {}


def registry_for(tab_id: str) -> dict[int, tuple[str, int]]:
    """Refs from this tab's most recent snapshot; empty when there is none."""
    return REGISTRY.get(tab_id, {})


def _frame_ids(frame_tree: dict) -> list[str]:
    """Flatten ``Page.getFrameTree`` into main-frame-first order."""
    out: list[str] = []

    def walk(node: dict) -> None:
        out.append(node["frame"]["id"])
        for child in node.get("childFrames", []):
            walk(child)

    walk(frame_tree["frameTree"])
    return out


async def _page_header(ws_url: str) -> tuple[str, str]:
    """Current url and title, or placeholders when the page has no JS context."""
    try:
        result = await cdp.send(ws_url, "Runtime.evaluate", {
            "expression": "[location.href, document.title]",
            "returnByValue": True,
        })
        url, title = result["result"]["value"]
        return url, title
    except Exception:
        return "(unknown)", "(unknown)"


async def _attributes_for(ws_url: str, backend_ids: list[int]) -> dict[int, dict]:
    """DOM attributes of the nodes Chrome could not name (R7)."""
    attrs: dict[int, dict] = {}
    for backend_id in backend_ids:
        try:
            node = await cdp.send(ws_url, "DOM.describeNode",
                                  {"backendNodeId": backend_id})
        except Exception:
            continue
        flat = node.get("node", {}).get("attributes", [])
        attrs[backend_id] = dict(zip(flat[::2], flat[1::2]))
    return attrs


async def snapshot(ws_url: str, tab_id: str) -> tuple[str, int]:
    """Render the whole page — every frame — and refresh the tab's registry."""
    frame_tree = await cdp.send(ws_url, "Page.getFrameTree")
    frame_ids = _frame_ids(frame_tree)
    url, title = await _page_header(ws_url)

    lines = [f"url    {url}", f"title  {title}", ""]
    registry: dict[int, tuple[str, int]] = {}
    ref_count = 0

    for index, frame_id in enumerate(frame_ids):
        tree = await cdp.send(ws_url, "Accessibility.getFullAXTree",
                              {"frameId": frame_id})
        nodes = tree.get("nodes", [])
        ax.strip_sources(nodes)
        attrs = await _attributes_for(ws_url, ax.unnamed_backend_ids(nodes))
        frame_refs: dict[int, int] = {}
        frame_lines, frame_refs = ax.render_nodes(nodes, ref_count, frame_refs, attrs)
        if not frame_lines:
            continue
        if index > 0:
            lines.append(f"frame {frame_id}")
        lines.extend(frame_lines)
        for ref, backend_id in frame_refs.items():
            registry[ref] = (frame_id, backend_id)
        ref_count += len(frame_refs)

    REGISTRY[tab_id] = registry
    return "\n".join(lines), len(registry)
