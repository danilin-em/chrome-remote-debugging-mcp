"""Assemble page views and run batches of actions against them.

Holds the per-tab ref registry. A ref is only meaningful for the snapshot that
produced it: numbering follows tree order, so any change earlier in the document
shifts every later number (D4 in the design document). Each snapshot therefore
replaces its tab's registry wholesale.
"""

from __future__ import annotations

import asyncio
from typing import NamedTuple

from . import actions, ax, cdp


class _Snapshot(NamedTuple):
    """Everything a tab's most recent snapshot needs to validate a later batch.

    ``refs`` maps a ref to the ``(frame_id, backend_node_id)`` it resolved to.
    ``main_frame_id`` lets an action honestly refuse a ref that lives in a
    child frame (I2): every action goes to the page-target websocket, which
    only ever addresses the main frame's renderer, so a node in another frame
    cannot actually be clicked from here. ``loader_id`` is the main frame's
    ``Page.Frame.loaderId`` at snapshot time; a cross-process navigation
    restarts Blink's backend-id numbering from 1, so an old ref can otherwise
    resolve to a live, unrelated node in the new document (I3) — a batch is
    refused when the tab's current loaderId no longer matches this stamp.
    """

    refs: dict[int, tuple[str, int]]
    main_frame_id: str
    loader_id: str | None


# {tab_id: _Snapshot}
REGISTRY: dict[str, _Snapshot] = {}

# Per-tab locks serialising snapshot assembly (I4): two concurrent
# browse/browse_view calls on one tab must not race to replace REGISTRY[tab_id]
# — whichever registry-write lands last would silently invalidate the refs just
# handed to the caller whose write didn't. Keyed per tab, never global, so
# unrelated tabs never wait on each other. Entries are pruned only by
# ``forget`` (called by the ``close_tab`` tool), like REGISTRY itself.
_LOCKS: dict[str, asyncio.Lock] = {}


def forget(tab_id: str) -> None:
    """Drop a closed tab's registry entry and lock."""
    REGISTRY.pop(tab_id, None)
    _LOCKS.pop(tab_id, None)


def _lock_for(tab_id: str) -> asyncio.Lock:
    """This tab's snapshot lock, created on first use."""
    lock = _LOCKS.get(tab_id)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[tab_id] = lock
    return lock


def registry_for(tab_id: str) -> dict[int, tuple[str, int]]:
    """Refs from this tab's most recent snapshot; empty when there is none."""
    state = REGISTRY.get(tab_id)
    return state.refs if state is not None else {}


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


async def snapshot(ws_url: str, tab_id: str) -> tuple[str, int, str]:
    """Render the whole page — every frame — and refresh the tab's registry.

    Returns ``(view, ref_count, url)``, where ``url`` is the location the
    snapshot actually observed (``location.href`` at capture time), which can
    differ from a URL a caller navigated to when the page redirected (M10).

    Runs under this tab's lock (I4) so two concurrent snapshots of the same
    tab cannot interleave and leave the registry holding a mix of both.
    """
    async with _lock_for(tab_id):
        frame_tree = await cdp.send(ws_url, "Page.getFrameTree")
        frame_ids = _frame_ids(frame_tree)
        main_frame_id = frame_ids[0]
        loader_id = frame_tree["frameTree"]["frame"].get("loaderId")
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
            frame_lines, frame_refs = ax.render_nodes(nodes, ref_count, attrs=attrs,
                                                     base_url=url)
            if not frame_lines:
                continue
            if index > 0:
                # A blank line before the marker (M6): without it, a frame
                # marker collides with the previous frame's last content line
                # instead of reading as its own boundary, like every other
                # block transition in the view (D7).
                if lines and lines[-1] != "":
                    lines.append("")
                lines.append(f"frame {frame_id[:8]}")
            lines.extend(frame_lines)
            for ref, backend_id in frame_refs.items():
                registry[ref] = (frame_id, backend_id)
            ref_count += len(frame_refs)

        REGISTRY[tab_id] = _Snapshot(refs=registry, main_frame_id=main_frame_id,
                                     loader_id=loader_id)
        return "\n".join(lines), len(registry), url


# Verbs that address an element and therefore require a resolvable ref.
_NEEDS_REF = frozenset({"click", "type", "select", "check", "uncheck", "hover"})


def _resolve(state: _Snapshot, ref) -> int:
    """Backend id behind a ref, or raise with an agent-readable reason.

    A ref that resolved in a frame other than the main frame is refused
    outright (I2): every action here is dispatched against the page target's
    own websocket, which can only ever reach the main frame's renderer — a
    node from a same- or cross-origin child frame lives in a different
    coordinate space (and, for a cross-origin frame, a different renderer
    process) that this tool has no way to address. Refusing honestly, rather
    than trying and reporting a misleading "stale ref", is the fix — not
    coordinate offsetting or a per-frame socket (out of scope; see the design
    document).
    """
    if ref is None:
        raise actions.ActionError("this action needs a ref")
    if ref not in state.refs:
        raise actions.ActionError(
            f"ref {ref} is not in the current view; take a new snapshot"
        )
    frame_id, backend_id = state.refs[ref]
    if frame_id != state.main_frame_id:
        raise actions.ActionError(
            f"ref {ref} is in frame {frame_id[:8]}, not the main frame; "
            "actions on nodes in other frames are not supported"
        )
    return backend_id


async def _perform(ws_url: str, state: _Snapshot, action: dict) -> str | None:
    """Run one action. Returns an optional detail string for the step result."""
    verb = action.get("do")
    ref = action.get("ref")
    backend_id = _resolve(state, ref) if verb in _NEEDS_REF else None

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
        scroll_backend = _resolve(state, scroll_ref) if scroll_ref is not None else None
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
        gone_backend = _resolve(state, gone) if gone is not None else None
        await actions.wait_for(ws_url, text, gone, gone_backend,
                               float(action.get("timeout", 5.0)))
        return None
    raise actions.ActionError(f'unknown action "{verb}"')


async def _current_loader_id(ws_url: str) -> str | None:
    """The main frame's ``loaderId`` right now, straight from Chrome."""
    frame_tree = await cdp.send(ws_url, "Page.getFrameTree")
    return frame_tree["frameTree"]["frame"].get("loaderId")


async def run_actions(ws_url: str, tab_id: str,
                      action_list: list[dict]) -> tuple[list[dict], str | None]:
    """Run a batch against the tab's current view.

    The first failure stops the batch: continuing would mean acting on a page
    whose state is no longer understood. Returns the per-step results and the
    error that stopped it, or ``None``.
    """
    state = REGISTRY.get(tab_id)
    if state is None:
        return [], "no view for this tab; call browse or browse_view first"

    # I3: a cross-process navigation restarts Blink's backendNodeId numbering
    # from 1, so an old ref can resolve to a live, unrelated node instead of
    # failing as stale. Reachable in ordinary use — a browse whose snapshot
    # failed leaves the prior registry in place, navigate can run between
    # browse_view and browse_act, or a person switches the tab by hand — so
    # every batch re-checks the stamp taken at snapshot time before acting.
    try:
        current_loader_id = await _current_loader_id(ws_url)
    except Exception as exc:
        return [], f"could not verify the page is unchanged: {exc}"
    if current_loader_id != state.loader_id:
        return [], "the page has navigated since this view; take a new snapshot"

    steps: list[dict] = []
    for action in action_list:
        verb = action.get("do")
        try:
            detail = await _perform(ws_url, state, action)
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
