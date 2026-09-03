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
    return {p["name"]: p["value"].get("value") for p in (node.get("properties") or [])}


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


def _label(node: dict, attrs: dict[int, dict]) -> str:
    """Best available label for an interactive node (R7)."""
    name = node_name(node)
    if name:
        return f'"{name}"'
    url = node_properties(node).get("url")
    if url:
        return f"-> {url}"
    node_attrs = attrs.get(node.get("backendDOMNodeId")) or {}
    for key in ATTR_FALLBACKS:
        value = (node_attrs.get(key) or "").strip()
        if value:
            return f'"{value}"'
    return "[unnamed]"


def format_interactive(node: dict, ref: int, attrs: dict[int, dict]) -> str:
    """Render one actionable node as ``role#ref "label" = "value" [flags]``."""
    parts = [f"{node_role(node)}#{ref} {_label(node, attrs)}"]
    value = node_value(node)
    if value:
        parts.append(f'= "{value}"')
    properties = node_properties(node)
    flags = [name for name in FLAG_PROPERTIES
             if properties.get(name) not in (None, False, "false")]
    if flags:
        parts.append("[" + " ".join(flags) + "]")
    return " ".join(parts)


def format_structure(node: dict) -> str | None:
    """Render a named landmark as ``role "name"``; ``None`` when not one (R10)."""
    role = node_role(node)
    name = node_name(node)
    if role in STRUCTURE_ROLES and name:
        return f'{role} "{name}"'
    return None


def document_order(nodes: list[dict], by_id: dict[str, dict]) -> list[dict]:
    """Reorder a level-ordered AX array into document (pre-order) order.

    ``Accessibility.getFullAXTree`` returns its nodes breadth-first: every node
    at depth *n* precedes every node at depth *n+1*. Rendering that array
    directly orders the view by tree depth rather than by the page's layout, so
    a Hacker News story's title and its own score land hundreds of lines apart
    and the footer prints before the header. Walking ``childIds`` from the roots
    restores the order a reader sees (D6).

    Purely in-memory: no I/O, like everything else here. Nodes that no root can
    reach — orphans, or a ``parentId`` pointing at a node the capture dropped —
    are appended in their original order rather than silently lost, and a
    ``visited`` set makes a cycle in ``childIds`` terminate instead of hang.
    """
    visited: set[str] = set()
    ordered: list[dict] = []

    def walk(node: dict) -> None:
        # Iterative, so a deeply nested page cannot exhaust the interpreter's
        # stack the way recursion would.
        stack = [node]
        while stack:
            current = stack.pop()
            node_id = current["nodeId"]
            if node_id in visited:
                continue
            visited.add(node_id)
            ordered.append(current)
            children = [by_id[child_id]
                        for child_id in reversed(current.get("childIds") or [])
                        if child_id in by_id]
            stack.extend(children)

    for node in nodes:
        parent_id = node.get("parentId")
        if parent_id is None or parent_id not in by_id:
            walk(node)
    for node in nodes:
        if node["nodeId"] not in visited:
            walk(node)
    return ordered


def block_path(node: dict, by_id: dict[str, dict]) -> tuple[str, ...]:
    """Ids of the node's block ancestors, outermost first (R12).

    Wrapper roles (R2) are skipped, so a layout table nesting a semantic table
    contributes only one level of depth rather than two.
    """
    path = []
    current = by_id.get(node.get("parentId"))
    while current is not None:
        if node_role(current) in BLOCK_ROLES and not current.get("ignored"):
            path.append(current["nodeId"])
        current = by_id.get(current.get("parentId"))
    return tuple(reversed(path))


def _content_line(node: dict, by_id: dict[str, dict], attrs: dict[int, dict],
                  ref: int) -> tuple[str | None, int, int | None]:
    """Render one node. Returns ``(line, next_ref, backend_id_for_ref)``.

    ``line`` is ``None`` when the node produces nothing (R1-R5).
    """
    if node.get("ignored"):
        return None, ref, None
    role = node_role(node)
    if role in WRAPPER_ROLES or role == "RootWebArea":
        return None, ref, None

    if role == "StaticText":
        name = node_name(node)
        if not name or is_punctuation(name):
            return None, ref, None
        parent = by_id.get(node.get("parentId"))
        if parent is not None and node_name(parent) == name:
            return None, ref, None
        return f'text "{name}"', ref, None

    if role in INTERACTIVE_ROLES:
        backend_id = node.get("backendDOMNodeId")
        if backend_id is None:
            # Nothing to resolve a ref back to (CDP documents this field as
            # optional). Must never consume a ref number — a caller stitching
            # several frames together sizes its next ref_start from how many
            # refs actually landed in the map, so a ref handed out here with
            # no map entry desyncs that count and a later frame reissues it.
            # Fall back to a plain named line, like a landmark; drop entirely
            # when there is no name either.
            name = node_name(node)
            return (f'{role} "{name}"' if name else None), ref, None
        ref += 1
        return format_interactive(node, ref, attrs), ref, backend_id

    structure = format_structure(node)
    if structure is not None:
        return structure, ref, None
    return None, ref, None


def render_nodes(
    nodes: list[dict],
    ref_start: int = 0,
    refs: dict[int, int] | None = None,
    attrs: dict[int, dict] | None = None,
) -> tuple[list[str], dict[int, int]]:
    """Render one frame's accessibility nodes as view lines.

    Nodes are emitted in document order (see :func:`document_order`), not in
    the depth-first-by-level order CDP hands back, so the view reproduces the
    page's own layout.

    Returns the lines and ``{ref: backend_node_id}``. Refs are numbered in the
    order they appear in the emitted view and continue from ``ref_start``; when
    ``refs`` is given it is updated in place and returned, so a caller can
    number a whole page across several frames.
    """
    refs = refs if refs is not None else {}
    attrs = attrs or {}
    by_id = {n["nodeId"]: n for n in nodes}

    items: list[tuple[tuple[str, ...], str]] = []
    ref = ref_start
    for node in document_order(nodes, by_id):
        line, ref, backend_id = _content_line(node, by_id, attrs, ref)
        if line is None:
            continue
        if backend_id is not None:
            refs[ref] = backend_id
        items.append((block_path(node, by_id), line))

    if not items:
        return [], refs

    # Normalise indentation so the shallowest content sits at column 0, then
    # separate sibling blocks with a blank line (D7).
    base = min(len(path) for path, _ in items)
    lines: list[str] = []
    previous: tuple[str, ...] | None = None
    for path, line in items:
        if previous is not None and path != previous:
            lines.append("")
        previous = path
        lines.append("  " * (len(path) - base) + line)
    return lines, refs
