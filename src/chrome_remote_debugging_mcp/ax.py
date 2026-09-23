"""Render Chrome's accessibility tree as a compact, agent-readable page view.

Pure by design: every function here is a function of its arguments. Nothing in
this module performs I/O or knows that MCP or CDP exist, so the rendering rules
can be tested against captured trees with no browser and no mocks.

Rule numbers (R1-R12) refer to the design document
``.superpowers/docs/specs/2026-09-03-browse-tools-design.md``.
"""

from __future__ import annotations

import string
from urllib.parse import urlsplit, urlunsplit

# R2 — layout and grouping nodes that carry no information of their own.
WRAPPER_ROLES = frozenset({
    "InlineTextBox", "LineBreak", "generic", "none", "presentation",
    "GenericContainer", "LayoutTable", "LayoutTableRow", "LayoutTableCell",
})

# R6 — roles an agent can act on; these receive a ref. "option" is
# deliberately excluded (M8): a native <option> cannot be clicked — that is
# why `select` exists as D5's documented exception — so a ref on one is a ref
# no action could ever resolve, the same defect class as a node with no
# backendDOMNodeId (see the comment in _content_line below).
INTERACTIVE_ROLES = frozenset({
    "button", "link", "textbox", "checkbox", "radio", "combobox", "searchbox",
    "menuitem", "menuitemcheckbox", "menuitemradio", "tab", "switch", "slider",
    "spinbutton", "listbox",
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

# Roles whose options are listed inline — all of them, never collapsed.
OPTION_ROLES = frozenset({"combobox", "listbox"})

# Longest url printed after "->"; anything longer ends in "…".
MAX_URL = 120

# Roles whose valuemin/valuemax are shown as "[min..max]".
RANGE_ROLES = frozenset({"slider", "spinbutton"})

# Tables rendered as Markdown grids, and the roles that make up their cells.
TABLE_ROLES = frozenset({"table", "grid", "treegrid"})
CELL_ROLES = frozenset({"cell", "gridcell", "columnheader", "rowheader"})

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


def short_url(url: str, base_url: str | None) -> str:
    """``url`` as printed after ``->``, relative to the page at ``base_url``.

    The page's own url heads every view, so a same-origin link prints as its
    path, and a link to the page itself — typically a ``href="#"`` tab or
    accordion toggle — as just its ``#fragment``. A foreign origin stays
    whole. Anything longer than :data:`MAX_URL` is cut and ends in ``…``.
    """
    parts = urlsplit(url)
    base = urlsplit(base_url or "")
    if not parts.netloc or (parts.scheme, parts.netloc) != (base.scheme, base.netloc):
        shown = url
    elif (parts.path or "/", parts.query) == (base.path or "/", base.query):
        shown = "#" + parts.fragment
    else:
        shown = urlunsplit(("", "", parts.path or "/", parts.query, parts.fragment))
    return shown if len(shown) <= MAX_URL else shown[:MAX_URL - 1] + "…"


def _options(node: dict, by_id: dict[str, dict]) -> str | None:
    """``{a | b | c}`` of every option descendant of a select-like node."""
    names: list[str] = []
    seen: set[str] = set()
    stack = list(reversed(node.get("childIds") or []))
    while stack:
        child = by_id.get(stack.pop())
        if child is None or child["nodeId"] in seen:
            continue
        seen.add(child["nodeId"])
        name = node_name(child)
        if node_role(child) == "option" and name and not child.get("ignored"):
            names.append(name)
        stack.extend(reversed(child.get("childIds") or []))
    if not names:
        return None
    return "{" + " | ".join(names) + "}"


def _bound(value) -> str:
    """A range bound without a trailing ``.0``; ``""`` when absent."""
    if value is None:
        return ""
    return str(int(value)) if float(value).is_integer() else str(value)


def _label(node: dict, attrs: dict[int, dict], base_url: str | None = None) -> str:
    """Best available label for an interactive node (R7)."""
    name = node_name(node)
    if name:
        return f'"{name}"'
    url = node_properties(node).get("url")
    if url:
        return f"-> {short_url(url, base_url)}"
    node_attrs = attrs.get(node.get("backendDOMNodeId")) or {}
    for key in ATTR_FALLBACKS:
        value = (node_attrs.get(key) or "").strip()
        if value:
            return f'"{value}"'
    return "[unnamed]"


def format_interactive(node: dict, ref: int, attrs: dict[int, dict],
                       by_id: dict[str, dict] | None = None,
                       base_url: str | None = None) -> str:
    """Render one actionable node as a single view line.

    ``role#ref "label" [-> url] [= "value"] [{options}] [[min..max]] [flags]``.
    ``by_id`` is the frame's node map, needed to list a select's options;
    ``base_url`` is the page url, against which same-origin links shorten.
    """
    role = node_role(node)
    properties = node_properties(node)
    parts = [f"{role}#{ref} {_label(node, attrs, base_url)}"]
    url = properties.get("url")
    if url and node_name(node):
        parts.append(f"-> {short_url(url, base_url)}")
    value = node_value(node)
    if value:
        parts.append(f'= "{value}"')
    if role in OPTION_ROLES and by_id:
        options = _options(node, by_id)
        if options:
            parts.append(options)
    if role in RANGE_ROLES:
        low, high = _bound(properties.get("valuemin")), _bound(properties.get("valuemax"))
        if low or high:
            parts.append(f"[{low}..{high}]")
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


def _prints_its_name(node: dict) -> bool:
    """True when ``node`` renders a line that already shows its own name (R3).

    Chrome computes a name from content for roles that print nothing here —
    ``cell``, ``columnheader``, ``row`` — so a text child equal to *their*
    name is the only place that text appears and must be kept. Callers pass
    only non-ignored nodes (see :func:`_owner`).
    """
    return node_role(node) in INTERACTIVE_ROLES or format_structure(node) is not None


def _owner(node: dict, by_id: dict[str, dict]) -> dict | None:
    """Nearest ancestor that means something: wrappers and ignored nodes are
    transparent, since they print nothing of their own."""
    current = by_id.get(node.get("parentId"))
    while current is not None and (current.get("ignored")
                                   or node_role(current) in WRAPPER_ROLES):
        current = by_id.get(current.get("parentId"))
    return current


def _already_shown(text_node: dict, name: str, by_id: dict[str, dict]) -> bool:
    """True when some other line already carries this text (R3).

    That is either its owner printing the same name — ``link "Home"`` over
    ``text "Home"``, even with wrappers between them — or an ``option``
    whose select lists it in braces.
    """
    owner = _owner(text_node, by_id)
    if owner is None:
        return False
    if node_name(owner) == name and _prints_its_name(owner):
        return True
    if node_role(owner) != "option":
        return False
    select = _owner(owner, by_id)
    while select is not None and node_role(select) not in OPTION_ROLES:
        select = _owner(select, by_id)
    return select is not None


def _content_line(node: dict, by_id: dict[str, dict], attrs: dict[int, dict],
                  ref: int, base_url: str | None = None
                  ) -> tuple[str | None, int, int | None]:
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
        if _already_shown(node, name, by_id):
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
        return format_interactive(node, ref, attrs, by_id, base_url), ref, backend_id

    structure = format_structure(node)
    if structure is not None:
        return structure, ref, None
    return None, ref, None


def _descendants(node: dict, by_id: dict[str, dict], stop=lambda n: False):
    """``node``'s descendants in document order, each at most once.

    A descendant for which ``stop`` is true is yielded but not entered — how a
    table hands a whole row, and a row a whole cell, to its own renderer.
    """
    seen: set[str] = set()
    stack = [by_id[c] for c in reversed(node.get("childIds") or []) if c in by_id]
    while stack:
        current = stack.pop()
        if current["nodeId"] in seen:
            continue
        seen.add(current["nodeId"])
        yield current
        if not stop(current):
            stack.extend(by_id[c] for c in reversed(current.get("childIds") or [])
                         if c in by_id)


class _Refs:
    """Ref numbering a table tries out, committed only if it renders as one."""

    def __init__(self, ref: int) -> None:
        self.ref = ref
        self.assigned: dict[int, int] = {}

    def line(self, node, by_id, attrs, base_url) -> str | None:
        line, self.ref, backend_id = _content_line(node, by_id, attrs, self.ref, base_url)
        if backend_id is not None:
            self.assigned[self.ref] = backend_id
        return line

    def cell_part(self, node, by_id, attrs, base_url) -> str | None:
        """Like :meth:`line`, but plain text comes bare — a cell is text already."""
        line = self.line(node, by_id, attrs, base_url)
        if line is not None and node_role(node) == "StaticText":
            return node_name(node)
        return line


def _cell_text(cell: dict, by_id, attrs, base_url, refs: _Refs) -> str:
    """Everything a cell would render, joined into one Markdown-safe string.

    A table nested inside the cell is flattened into the same string.
    """
    parts = [refs.cell_part(n, by_id, attrs, base_url)
             for n in [cell, *_descendants(cell, by_id)]]
    return " ".join(p for p in parts if p).replace("|", "\\|")


def _table_items(table: dict, by_id, attrs, base_url, refs: _Refs):
    """Lines of ``table`` as ``[(block_path, line)]``, or ``None`` if it has no grid.

    Rows become Markdown rows. A first row holding a ``columnheader`` is the
    header; otherwise the header is blank, since Markdown requires one. The
    tree carries no colspan, so short rows are padded, and rows with no
    content are dropped. Content outside any row — a caption, a footer note —
    prints as ordinary lines before or after the grid, in document order.
    """
    before: list[tuple[tuple[str, ...], str]] = []
    after: list[tuple[tuple[str, ...], str]] = []
    rows: list[list[str]] = []
    header = False

    head = refs.line(table, by_id, attrs, base_url)
    if head is not None:
        before.append((block_path(table, by_id), head))
    for node in _descendants(table, by_id, stop=lambda n: node_role(n) == "row"):
        if node_role(node) != "row":
            line = refs.line(node, by_id, attrs, base_url)
            if line is not None:
                (after if rows else before).append((block_path(node, by_id), line))
            continue
        cells: list[str] = []
        is_header = False
        for child in _descendants(node, by_id, stop=lambda n: node_role(n) in CELL_ROLES):
            if node_role(child) in CELL_ROLES:
                is_header = is_header or node_role(child) == "columnheader"
                cells.append(_cell_text(child, by_id, attrs, base_url, refs))
                continue
            loose = refs.cell_part(child, by_id, attrs, base_url)
            if loose is not None:
                cells.append(loose.replace("|", "\\|"))
        if any(cells):
            header = header or (is_header and not rows)
            rows.append(cells)

    if not rows:
        return None
    width = max(len(r) for r in rows)
    padded = [r + [""] * (width - len(r)) for r in rows]
    head_row = padded.pop(0) if header else [""] * width
    grid = ["| " + " | ".join(head_row) + " |", "|" + "---|" * width]
    grid += ["| " + " | ".join(r) + " |" for r in padded]
    grid_path = block_path(table, by_id) + (table["nodeId"],)
    return before + [(grid_path, line) for line in grid] + after


def _is_table(node: dict) -> bool:
    return node_role(node) in TABLE_ROLES and not node.get("ignored")


def render_nodes(
    nodes: list[dict],
    ref_start: int = 0,
    refs: dict[int, int] | None = None,
    attrs: dict[int, dict] | None = None,
    base_url: str | None = None,
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
    claimed: set[str] = set()
    for node in document_order(nodes, by_id):
        if node["nodeId"] in claimed:
            continue
        if _is_table(node):
            trial = _Refs(ref)
            table_items = _table_items(node, by_id, attrs, base_url, trial)
            if table_items is not None:
                claimed.update(n["nodeId"] for n in _descendants(node, by_id))
                refs.update(trial.assigned)
                ref = trial.ref
                items.extend(table_items)
                continue
        line, ref, backend_id = _content_line(node, by_id, attrs, ref, base_url)
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
