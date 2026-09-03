"""Snapshot assembly and batch execution, with the CDP layer stubbed."""

import asyncio
import re

import pytest

import chrome_remote_debugging_mcp.browse as browse

WS = "ws://fake"

FRAME_TREE_ONE = {"frameTree": {"frame": {"id": "F1", "url": "http://a/"}}}
FRAME_TREE_TWO = {
    "frameTree": {
        "frame": {"id": "F1", "url": "http://a/"},
        "childFrames": [{"frame": {"id": "F2", "url": "http://b/"}}],
    }
}

MAIN_NODES = [
    {"nodeId": "1", "role": {"value": "heading"}, "name": {"value": "Заголовок"}},
    {"nodeId": "2", "role": {"value": "button"}, "name": {"value": "Жми"},
     "backendDOMNodeId": 201},
]
CHILD_NODES = [
    {"nodeId": "1", "role": {"value": "button"}, "name": {"value": "Внутри"},
     "backendDOMNodeId": 301},
]
# An interactive node CDP gave no backendDOMNodeId for — cannot be resolved to
# a DOM node, so it must never consume a ref number (fix round 1, Finding 1).
UNRESOLVABLE_NODES = [
    {"nodeId": "1", "role": {"value": "button"}, "name": {"value": "Мёртвая кнопка"}},
]

FRAME_TREE_LONG_ID = {
    "frameTree": {
        "frame": {"id": "F1", "url": "http://a/"},
        "childFrames": [{"frame": {"id": "201F341A9E7B4C2D", "url": "http://b/"}}],
    }
}


class Fake:
    """Scripted CDP responses, keyed by method and — for AX — by frame id."""

    def __init__(self, frame_tree=FRAME_TREE_ONE, trees=None,
                 meta=("http://a/", "Заголовок A"), describe=None, errors=None):
        self.calls = []
        self._frame_tree = frame_tree
        self._trees = trees or {"F1": MAIN_NODES}
        self._meta = meta
        self._describe = describe or {}
        self._errors = errors or {}

    async def send(self, ws_url, method, params=None):
        params = params or {}
        self.calls.append((method, params))
        if method in self._errors:
            raise self._errors[method]
        if method == "Page.getFrameTree":
            return self._frame_tree
        if method == "Runtime.evaluate":
            return {"result": {"value": list(self._meta)}}
        if method == "Accessibility.getFullAXTree":
            return {"nodes": self._trees.get(params.get("frameId"), [])}
        if method == "DOM.describeNode":
            return {"node": {"attributes":
                             self._describe.get(params["backendNodeId"], [])}}
        return {}

    def methods(self):
        return [m for m, _ in self.calls]


@pytest.fixture(autouse=True)
def _clear_registry():
    browse.REGISTRY.clear()
    yield
    browse.REGISTRY.clear()


def _install(monkeypatch, fake):
    monkeypatch.setattr(browse.cdp, "send", fake.send)


def test_snapshot_renders_a_header_and_the_view(monkeypatch):
    fake = Fake()
    _install(monkeypatch, fake)
    view, count = asyncio.run(browse.snapshot(WS, "T1"))
    assert view.splitlines()[0] == "url    http://a/"
    assert view.splitlines()[1] == "title  Заголовок A"
    assert 'heading "Заголовок"' in view
    assert 'button#1 "Жми"' in view
    assert count == 1


def test_snapshot_stores_frame_and_backend_ids_in_the_registry(monkeypatch):
    fake = Fake()
    _install(monkeypatch, fake)
    asyncio.run(browse.snapshot(WS, "T1"))
    assert browse.REGISTRY["T1"] == {1: ("F1", 201)}


def test_snapshot_replaces_the_previous_registry(monkeypatch):
    fake = Fake()
    _install(monkeypatch, fake)
    browse.REGISTRY["T1"] = {99: ("OLD", 1)}
    asyncio.run(browse.snapshot(WS, "T1"))
    assert 99 not in browse.REGISTRY["T1"]


def test_snapshot_walks_child_frames_and_continues_ref_numbering(monkeypatch):
    fake = Fake(frame_tree=FRAME_TREE_TWO,
                trees={"F1": MAIN_NODES, "F2": CHILD_NODES})
    _install(monkeypatch, fake)
    view, count = asyncio.run(browse.snapshot(WS, "T1"))
    assert "frame F2" in view
    assert 'button#2 "Внутри"' in view
    assert count == 2
    assert browse.REGISTRY["T1"] == {1: ("F1", 201), 2: ("F2", 301)}


def test_snapshot_never_reissues_a_ref_across_frames(monkeypatch):
    """An interactive node with no backendDOMNodeId must not consume a ref.

    Regression for fix round 1, Finding 1: if F1 contributes an unresolvable
    interactive node, its ref number must not be handed out at all — not
    printed without a registry entry, and not silently reused by the next
    frame. Every printed ``#N`` must resolve in the registry, and no number
    may appear twice in the assembled view.
    """
    fake = Fake(frame_tree=FRAME_TREE_TWO,
                trees={"F1": UNRESOLVABLE_NODES, "F2": CHILD_NODES})
    _install(monkeypatch, fake)
    view, count = asyncio.run(browse.snapshot(WS, "T1"))
    printed_refs = [int(n) for n in re.findall(r"#(\d+)", view)]
    assert printed_refs, "expected at least one ref to be printed"
    assert len(printed_refs) == len(set(printed_refs)), (
        f"a ref number was reissued in the assembled view:\n{view}"
    )
    for ref in printed_refs:
        assert ref in browse.REGISTRY["T1"], (
            f"ref #{ref} was printed but has no registry entry:\n{view}"
        )
    assert count == len(browse.REGISTRY["T1"])


def test_snapshot_truncates_the_frame_marker_to_eight_characters(monkeypatch):
    fake = Fake(frame_tree=FRAME_TREE_LONG_ID,
                trees={"F1": MAIN_NODES, "201F341A9E7B4C2D": CHILD_NODES})
    _install(monkeypatch, fake)
    view, _ = asyncio.run(browse.snapshot(WS, "T1"))
    assert "frame 201F341A" in view
    assert "201F341A9E7B4C2D" not in view
    # The registry itself must still key on the full frame id (D4/D-spec: the
    # marker is a display truncation only, never used to address a frame).
    assert browse.REGISTRY["T1"][2][0] == "201F341A9E7B4C2D"


def test_snapshot_omits_a_frame_marker_for_an_empty_child_frame(monkeypatch):
    fake = Fake(frame_tree=FRAME_TREE_TWO, trees={"F1": MAIN_NODES, "F2": []})
    _install(monkeypatch, fake)
    view, _ = asyncio.run(browse.snapshot(WS, "T1"))
    assert "frame F2" not in view


def test_snapshot_fetches_attributes_for_unnamed_controls(monkeypatch):
    nodes = [{"nodeId": "1", "role": {"value": "textbox"}, "name": {"value": ""},
              "backendDOMNodeId": 401}]
    fake = Fake(trees={"F1": nodes}, describe={401: ["name", "q", "type", "text"]})
    _install(monkeypatch, fake)
    view, _ = asyncio.run(browse.snapshot(WS, "T1"))
    assert 'textbox#1 "q"' in view
    assert "DOM.describeNode" in fake.methods()


def test_snapshot_survives_a_failing_describe_node(monkeypatch):
    nodes = [{"nodeId": "1", "role": {"value": "textbox"}, "name": {"value": ""},
              "backendDOMNodeId": 401}]
    fake = Fake(trees={"F1": nodes},
                errors={"DOM.describeNode": RuntimeError("gone")})
    _install(monkeypatch, fake)
    view, _ = asyncio.run(browse.snapshot(WS, "T1"))
    assert "textbox#1 [unnamed]" in view


def test_snapshot_falls_back_when_the_metadata_probe_fails(monkeypatch):
    fake = Fake(errors={"Runtime.evaluate": RuntimeError("no context")})
    _install(monkeypatch, fake)
    view, _ = asyncio.run(browse.snapshot(WS, "T1"))
    assert view.splitlines()[0] == "url    (unknown)"


def test_registry_for_returns_an_empty_map_for_an_unknown_tab():
    assert browse.registry_for("nope") == {}


def _prime(tab_id="T1", refs=None):
    browse.REGISTRY[tab_id] = refs or {1: ("F1", 201), 2: ("F1", 202)}


def test_run_actions_executes_every_step_in_order(monkeypatch):
    performed = []

    async def fake_click(ws_url, ref, backend_id):
        performed.append(("click", ref, backend_id))

    async def fake_type(ws_url, ref, backend_id, text, clear=False):
        performed.append(("type", ref, text, clear))

    async def fake_settle(ws_url, timeout=2.0):
        performed.append(("settle",))

    monkeypatch.setattr(browse.actions, "click", fake_click)
    monkeypatch.setattr(browse.actions, "type_text", fake_type)
    monkeypatch.setattr(browse.actions, "settle", fake_settle)
    _prime()

    steps, error = asyncio.run(browse.run_actions(WS, "T1", [
        {"do": "type", "ref": 2, "text": "коты"},
        {"do": "click", "ref": 1},
    ]))
    assert error is None
    assert [s["ok"] for s in steps] == [True, True]
    assert ("type", 2, "коты", False) in performed
    assert ("click", 1, 201) in performed
    assert performed.count(("settle",)) == 2


def test_run_actions_stops_at_the_first_failure(monkeypatch):
    calls = []

    async def fake_click(ws_url, ref, backend_id):
        calls.append(ref)
        raise browse.actions.ActionError("ref 1 is stale; take a new snapshot")

    monkeypatch.setattr(browse.actions, "click", fake_click)
    monkeypatch.setattr(browse.actions, "settle",
                        lambda *a, **k: asyncio.sleep(0))
    _prime()

    steps, error = asyncio.run(browse.run_actions(WS, "T1", [
        {"do": "click", "ref": 1},
        {"do": "click", "ref": 2},
    ]))
    assert "stale" in error
    assert len(steps) == 1 and steps[0]["ok"] is False
    assert calls == [1]


def test_run_actions_rejects_an_unknown_ref(monkeypatch):
    _prime()
    steps, error = asyncio.run(browse.run_actions(WS, "T1", [{"do": "click", "ref": 99}]))
    assert "ref 99 is not in the current view" in error
    assert steps[0]["ok"] is False


def test_run_actions_rejects_an_unknown_action(monkeypatch):
    _prime()
    steps, error = asyncio.run(browse.run_actions(WS, "T1", [{"do": "teleport"}]))
    assert 'unknown action "teleport"' in error


def test_run_actions_requires_a_ref_where_one_is_needed(monkeypatch):
    _prime()
    steps, error = asyncio.run(browse.run_actions(WS, "T1", [{"do": "click"}]))
    assert "needs a ref" in error


def test_run_actions_reports_a_missing_registry():
    steps, error = asyncio.run(browse.run_actions(WS, "T9", [{"do": "click", "ref": 1}]))
    assert "no view for this tab" in error
    assert steps == []


def test_run_actions_dispatches_every_supported_verb(monkeypatch):
    seen = []

    async def record(name, *args, **kwargs):
        seen.append(name)

    monkeypatch.setattr(browse.actions, "click",
                        lambda *a, **k: record("click"))
    monkeypatch.setattr(browse.actions, "type_text",
                        lambda *a, **k: record("type"))
    monkeypatch.setattr(browse.actions, "press",
                        lambda *a, **k: record("press"))
    monkeypatch.setattr(browse.actions, "select",
                        lambda *a, **k: record("select"))
    monkeypatch.setattr(browse.actions, "set_checked",
                        lambda *a, **k: record("checked"))
    monkeypatch.setattr(browse.actions, "scroll",
                        lambda *a, **k: record("scroll"))
    monkeypatch.setattr(browse.actions, "hover",
                        lambda *a, **k: record("hover"))
    monkeypatch.setattr(browse.actions, "wait_for",
                        lambda *a, **k: record("wait_for"))
    monkeypatch.setattr(browse.actions, "settle",
                        lambda *a, **k: asyncio.sleep(0))
    _prime()

    steps, error = asyncio.run(browse.run_actions(WS, "T1", [
        {"do": "click", "ref": 1},
        {"do": "type", "ref": 1, "text": "x"},
        {"do": "press", "key": "Enter"},
        {"do": "select", "ref": 1, "value": "два"},
        {"do": "check", "ref": 1},
        {"do": "uncheck", "ref": 1},
        {"do": "scroll", "to": "bottom"},
        {"do": "hover", "ref": 1},
        {"do": "wait_for", "text": "готово"},
    ]))
    assert error is None
    assert seen == ["click", "type", "press", "select", "checked", "checked",
                    "scroll", "hover", "wait_for"]
    assert len(steps) == 9


def test_run_actions_reports_whether_a_checkbox_moved(monkeypatch):
    async def fake_set_checked(ws_url, ref, backend_id, target):
        return False

    monkeypatch.setattr(browse.actions, "set_checked", fake_set_checked)
    monkeypatch.setattr(browse.actions, "settle", lambda *a, **k: asyncio.sleep(0))
    _prime()
    steps, error = asyncio.run(browse.run_actions(WS, "T1", [{"do": "check", "ref": 1}]))
    assert error is None
    assert steps[0]["detail"] == "already checked"


def test_run_actions_reports_a_successful_checkbox_click(monkeypatch):
    """Complements the already-in-target-state test above: when the checkbox
    actually moved, ``set_checked`` returns True and no detail is reported."""
    async def fake_set_checked(ws_url, ref, backend_id, target):
        return True

    monkeypatch.setattr(browse.actions, "set_checked", fake_set_checked)
    monkeypatch.setattr(browse.actions, "settle", lambda *a, **k: asyncio.sleep(0))
    _prime()
    steps, error = asyncio.run(browse.run_actions(WS, "T1", [{"do": "check", "ref": 1}]))
    assert error is None
    assert "detail" not in steps[0]


def test_run_actions_resolves_a_scroll_targeted_by_ref(monkeypatch):
    seen = {}

    async def fake_scroll(ws_url, ref, backend_id, to):
        seen.update(ref=ref, backend_id=backend_id, to=to)

    monkeypatch.setattr(browse.actions, "scroll", fake_scroll)
    monkeypatch.setattr(browse.actions, "settle", lambda *a, **k: asyncio.sleep(0))
    _prime()
    steps, error = asyncio.run(browse.run_actions(WS, "T1", [
        {"do": "scroll", "ref": 1},
    ]))
    assert error is None
    assert seen == {"ref": 1, "backend_id": 201, "to": None}


def test_run_actions_rejects_an_unknown_scroll_ref(monkeypatch):
    """CONTROLLER RULING (fix round 1, Finding 3): a supplied-but-unknown
    scroll ref must raise the same "not in the current view" error every
    other verb gives, not fall through to actions.scroll's generic "must be
    a ref, or to top/bottom" message as though no ref had been given at all.

    Proven deterministically: actions.scroll is spied on and must never be
    called.
    """
    calls = []

    async def spy_scroll(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(browse.actions, "scroll", spy_scroll)
    _prime()
    steps, error = asyncio.run(browse.run_actions(WS, "T1", [
        {"do": "scroll", "ref": 99},
    ]))
    assert "ref 99 is not in the current view" in error
    assert steps[0]["ok"] is False
    assert calls == []


def test_run_actions_passes_press_its_key(monkeypatch):
    seen = {}

    async def fake_press(ws_url, key):
        seen.update(ws_url=ws_url, key=key)

    monkeypatch.setattr(browse.actions, "press", fake_press)
    monkeypatch.setattr(browse.actions, "settle", lambda *a, **k: asyncio.sleep(0))
    _prime()
    steps, error = asyncio.run(browse.run_actions(WS, "T1", [
        {"do": "press", "key": "Enter"},
    ]))
    assert error is None
    assert seen == {"ws_url": WS, "key": "Enter"}


def test_run_actions_passes_select_its_ref_backend_id_and_value(monkeypatch):
    seen = {}

    async def fake_select(ws_url, ref, backend_id, value):
        seen.update(ws_url=ws_url, ref=ref, backend_id=backend_id, value=value)

    monkeypatch.setattr(browse.actions, "select", fake_select)
    monkeypatch.setattr(browse.actions, "settle", lambda *a, **k: asyncio.sleep(0))
    _prime()
    steps, error = asyncio.run(browse.run_actions(WS, "T1", [
        {"do": "select", "ref": 2, "value": "два"},
    ]))
    assert error is None
    assert seen == {"ws_url": WS, "ref": 2, "backend_id": 202, "value": "два"}


def test_run_actions_passes_hover_its_ref_and_backend_id(monkeypatch):
    seen = {}

    async def fake_hover(ws_url, ref, backend_id):
        seen.update(ws_url=ws_url, ref=ref, backend_id=backend_id)

    monkeypatch.setattr(browse.actions, "hover", fake_hover)
    monkeypatch.setattr(browse.actions, "settle", lambda *a, **k: asyncio.sleep(0))
    _prime()
    steps, error = asyncio.run(browse.run_actions(WS, "T1", [
        {"do": "hover", "ref": 2},
    ]))
    assert error is None
    assert seen == {"ws_url": WS, "ref": 2, "backend_id": 202}


def test_run_actions_passes_check_and_uncheck_distinct_targets(monkeypatch):
    """Fix round 1, Finding 2: the `target` boolean is the piece most likely
    to be inverted by a careless edit — assert it differs between "check"
    and "uncheck", along with the ref/backend_id actually forwarded."""
    seen = []

    async def fake_set_checked(ws_url, ref, backend_id, target):
        seen.append((ws_url, ref, backend_id, target))
        return False

    monkeypatch.setattr(browse.actions, "set_checked", fake_set_checked)
    monkeypatch.setattr(browse.actions, "settle", lambda *a, **k: asyncio.sleep(0))
    _prime()
    steps, error = asyncio.run(browse.run_actions(WS, "T1", [
        {"do": "check", "ref": 1},
        {"do": "uncheck", "ref": 2},
    ]))
    assert error is None
    assert seen == [(WS, 1, 201, True), (WS, 2, 202, False)]


def test_run_actions_passes_wait_for_a_resolved_ref(monkeypatch):
    seen = {}

    async def fake_wait_for(ws_url, text, ref_gone, backend_id, timeout=5.0):
        seen.update(text=text, ref_gone=ref_gone, backend_id=backend_id,
                    timeout=timeout)

    monkeypatch.setattr(browse.actions, "wait_for", fake_wait_for)
    monkeypatch.setattr(browse.actions, "settle", lambda *a, **k: asyncio.sleep(0))
    _prime()
    asyncio.run(browse.run_actions(WS, "T1", [
        {"do": "wait_for", "ref_gone": 2, "timeout": 3},
    ]))
    assert seen == {"text": None, "ref_gone": 2, "backend_id": 202, "timeout": 3}


def test_run_actions_rejects_wait_for_with_an_unknown_ref_gone(monkeypatch):
    """CONTROLLER RULING 1: an unknown ref_gone must raise through _resolve,
    not silently resolve to backend_id=None (which DOM.getBoxModel would then
    read as "the node is gone", reporting success for a ref that never was).

    Fix round 1, Finding 1: proven deterministically, not by an incidental
    sandbox network failure. actions.wait_for is spied on and must never be
    called at all — the only way to know _resolve intercepted first, rather
    than some downstream call happening to fail for an unrelated reason.
    """
    calls = []

    async def spy_wait_for(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(browse.actions, "wait_for", spy_wait_for)
    _prime()
    steps, error = asyncio.run(browse.run_actions(WS, "T1", [
        {"do": "wait_for", "ref_gone": 99},
    ]))
    assert "ref 99 is not in the current view" in error
    assert steps[0]["ok"] is False
    assert calls == []


def test_run_actions_rejects_wait_for_given_both_text_and_ref_gone(monkeypatch):
    """actions.wait_for silently prefers text when given both; _perform must
    not let that ambiguity through unannounced.

    Fix round 1, Finding 1: same deterministic proof as the test above —
    spy on actions.wait_for and assert it is never called.
    """
    calls = []

    async def spy_wait_for(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(browse.actions, "wait_for", spy_wait_for)
    _prime()
    steps, error = asyncio.run(browse.run_actions(WS, "T1", [
        {"do": "wait_for", "text": "готово", "ref_gone": 2},
    ]))
    assert "not both" in error
    assert steps[0]["ok"] is False
    assert calls == []


def test_run_actions_reports_an_unexpected_failure(monkeypatch):
    """CONTROLLER RULING 2: a non-ActionError exception (unexpected CDP or
    transport failure) must still be recorded as a failed step, not propagate
    and crash the batch."""
    async def fake_click(ws_url, ref, backend_id):
        raise RuntimeError("socket closed")

    monkeypatch.setattr(browse.actions, "click", fake_click)
    _prime()
    steps, error = asyncio.run(browse.run_actions(WS, "T1", [
        {"do": "click", "ref": 1},
    ]))
    assert error == "click failed: socket closed"
    assert steps == [{"do": "click", "ok": False, "error": "click failed: socket closed"}]
