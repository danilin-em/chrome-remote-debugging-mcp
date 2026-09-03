"""Snapshot assembly and batch execution, with the CDP layer stubbed."""

import asyncio

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
