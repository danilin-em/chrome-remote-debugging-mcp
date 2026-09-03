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
        frame_lines, frame_refs = ax.render_nodes(nodes, ref_count, attrs=attrs)
        if not frame_lines:
            continue
        if index > 0:
            lines.append(f"frame {frame_id[:8]}")
        lines.extend(frame_lines)
        for ref, backend_id in frame_refs.items():
            registry[ref] = (frame_id, backend_id)
        ref_count += len(frame_refs)

    REGISTRY[tab_id] = registry
    return "\n".join(lines), len(registry)


# Verbs that address an element and therefore require a resolvable ref.
_NEEDS_REF = frozenset({"click", "type", "select", "check", "uncheck", "hover"})


def _resolve(registry: dict[int, tuple[str, int]], ref) -> int:
    """Backend id behind a ref, or raise with an agent-readable reason."""
    if ref is None:
        raise actions.ActionError("this action needs a ref")
    if ref not in registry:
        raise actions.ActionError(
            f"ref {ref} is not in the current view; take a new snapshot"
        )
    return registry[ref][1]


async def _perform(ws_url: str, registry: dict[int, tuple[str, int]],
                   action: dict) -> str | None:
    """Run one action. Returns an optional detail string for the step result."""
    verb = action.get("do")
    ref = action.get("ref")
    backend_id = _resolve(registry, ref) if verb in _NEEDS_REF else None

    if verb == "click":
        await actions.click(ws_url, ref, backend_id)
        return None
    if verb == "type":
        await actions.type_text(ws_url, ref, backend_id,
                                action.get("text", ""),
                                bool(action.get("clear", False)))
        return None
    if verb == "press":
        await actions.press(ws_url, action.get("key", ""))
        return None
    if verb == "select":
        await actions.select(ws_url, ref, backend_id, action.get("value", ""))
        return None
    if verb in ("check", "uncheck"):
        target = verb == "check"
        moved = await actions.set_checked(ws_url, ref, backend_id, target)
        return None if moved else f"already {'checked' if target else 'unchecked'}"
    if verb == "scroll":
        scroll_ref = action.get("ref")
        # Like wait_for's ref_gone: a ref is optional here (the alternative is
        # "to"), but a *supplied* ref must resolve through the same guard
        # every other verb uses — an unknown ref must raise, not be treated
        # as if no ref were given at all.
        scroll_backend = _resolve(registry, scroll_ref) if scroll_ref is not None else None
        await actions.scroll(ws_url, scroll_ref, scroll_backend, action.get("to"))
        return None
    if verb == "hover":
        await actions.hover(ws_url, ref, backend_id)
        return None
    if verb == "wait_for":
        text = action.get("text")
        gone = action.get("ref_gone")
        # actions.wait_for silently prefers text when given both; refuse the
        # ambiguity here instead of letting one condition win unannounced.
        if text is not None and gone is not None:
            raise actions.ActionError(
                "wait_for takes either text or ref_gone, not both"
            )
        gone_backend = _resolve(registry, gone) if gone is not None else None
        await actions.wait_for(ws_url, text, gone, gone_backend,
                               float(action.get("timeout", 5.0)))
        return None
    raise actions.ActionError(f'unknown action "{verb}"')


async def run_actions(ws_url: str, tab_id: str,
                      action_list: list[dict]) -> tuple[list[dict], str | None]:
    """Run a batch against the tab's current view.

    The first failure stops the batch: continuing would mean acting on a page
    whose state is no longer understood. Returns the per-step results and the
    error that stopped it, or ``None``.
    """
    registry = REGISTRY.get(tab_id)
    if registry is None:
        return [], "no view for this tab; call browse or browse_view first"

    steps: list[dict] = []
    for action in action_list:
        verb = action.get("do")
        try:
            detail = await _perform(ws_url, registry, action)
        except actions.ActionError as exc:
            steps.append({"do": verb, "ok": False, "error": str(exc)})
            return steps, str(exc)
        except Exception as exc:  # unexpected CDP or transport failure
            message = f"{verb} failed: {exc}"
            steps.append({"do": verb, "ok": False, "error": message})
            return steps, message
        step: dict = {"do": verb, "ok": True}
        if detail:
            step["detail"] = detail
        steps.append(step)
        await actions.settle(ws_url)
    return steps, None
