"""Render Chrome's accessibility tree as a compact, agent-readable page view.

Pure by design: every function here is a function of its arguments. Nothing in
this module performs I/O or knows that MCP or CDP exist, so the rendering rules
can be tested against captured trees with no browser and no mocks.

Rule numbers (R1-R12) refer to the design document
``.superpowers/docs/specs/2026-09-03-browse-tools-design.md``.
"""

from __future__ import annotations

import string

# R2 — layout and grouping nodes that carry no information of their own.
WRAPPER_ROLES = frozenset({
    "InlineTextBox", "LineBreak", "generic", "none", "presentation",
    "GenericContainer", "LayoutTable", "LayoutTableRow", "LayoutTableCell",
})

# R6 — roles an agent can act on; these receive a ref.
INTERACTIVE_ROLES = frozenset({
    "button", "link", "textbox", "checkbox", "radio", "combobox", "searchbox",
    "menuitem", "menuitemcheckbox", "menuitemradio", "tab", "switch", "slider",
    "spinbutton", "listbox", "option",
})

# R10 — landmarks worth printing for orientation; no ref.
STRUCTURE_ROLES = frozenset({
    "heading", "region", "navigation", "main", "article", "list", "table",
    "form", "dialog", "alert", "status", "banner", "contentinfo",
    "complementary", "search",
})

# R12 — roles that mark a visible block. They group lines; a role may also be
# interactive or structural, in which case it both prints a line and opens a
# block.
BLOCK_ROLES = frozenset({
    "paragraph", "listitem", "row", "cell", "gridcell", "rowheader",
    "columnheader", "article", "region", "section", "blockquote", "figure",
    "list", "table", "grid", "form", "dialog", "note", "banner", "navigation",
    "main", "contentinfo", "complementary", "search", "group", "heading",
    "tabpanel", "menu", "listbox", "alert", "status",
})

# R9 — state worth showing, in this order.
FLAG_PROPERTIES = ("checked", "disabled", "required", "readonly", "expanded",
                   "selected")

# R7 — DOM attributes to fall back on when Chrome computed no name.
ATTR_FALLBACKS = ("placeholder", "aria-label", "title", "name", "id", "alt",
                  "type")

_PUNCTUATION = set(string.punctuation) | {"|", "·", "•", "—", "–", "»", "«"}


def node_role(node: dict) -> str:
    """Role string, or ``""`` when the node carries none."""
    return (node.get("role") or {}).get("value", "") or ""


def node_name(node: dict) -> str:
    """Accessible name, stripped. ``""`` when absent."""
    return ((node.get("name") or {}).get("value") or "").strip()


def node_value(node: dict) -> str:
    """Current value of a form control, stripped. ``""`` when absent."""
    return str((node.get("value") or {}).get("value") or "").strip()


def node_properties(node: dict) -> dict:
    """Flatten the ``properties`` list into ``{name: value}``."""
    return {p["name"]: p["value"].get("value") for p in node.get("properties", [])}


def is_punctuation(text: str) -> bool:
    """True when ``text`` is non-empty and made only of separators (R3)."""
    return bool(text) and all(ch in _PUNCTUATION or ch.isspace() for ch in text)


def strip_sources(nodes: list[dict]) -> None:
    """Drop ``name.sources`` from every node, in place (R11).

    The renderer never reads it and it is roughly 29% of a raw tree's bytes;
    dropping it early keeps large pages cheap to hold in memory.
    """
    for node in nodes:
        name = node.get("name")
        if isinstance(name, dict):
            name.pop("sources", None)


def unnamed_backend_ids(nodes: list[dict]) -> list[int]:
    """Backend ids of interactive nodes Chrome named neither directly nor by url.

    ``ax`` cannot fetch DOM attributes itself — that is I/O. It reports which
    nodes need them so the caller can fetch and pass them back to
    :func:`render_nodes` (R7).
    """
    out = []
    for node in nodes:
        if node.get("ignored"):
            continue
        if node_role(node) not in INTERACTIVE_ROLES:
            continue
        if node_name(node) or node_properties(node).get("url"):
            continue
        backend_id = node.get("backendDOMNodeId")
        if backend_id:
            out.append(backend_id)
    return out
