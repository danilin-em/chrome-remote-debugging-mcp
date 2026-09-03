"""The captured fixtures must contain what the renderer's tests rely on."""

import pytest

NAMES = ["probe", "hn", "youtube", "wikipedia"]


@pytest.mark.parametrize("name", NAMES)
def test_fixture_loads_and_has_nodes(ax_fixture, name):
    frames = ax_fixture(name)
    assert frames, f"{name} has no frames"
    assert all("frameId" in f and "nodes" in f for f in frames)
    assert sum(len(f["nodes"]) for f in frames) > 5


def test_probe_covers_shadow_and_both_iframe_origins(ax_fixture):
    frames = ax_fixture("probe")
    names = {
        (n.get("name") or {}).get("value", "")
        for frame in frames
        for n in frame["nodes"]
    }
    assert "CLOSED SHADOW BUTTON" in names
    assert "OPEN SHADOW BUTTON" in names
    assert "SAME ORIGIN IFRAME BUTTON" in names
    assert "CROSS ORIGIN IFRAME BUTTON" in names
    assert len(frames) == 3


def test_probe_hides_what_the_page_hides(probe_nodes):
    visible = {
        (n.get("name") or {}).get("value", "")
        for n in probe_nodes
        if not n.get("ignored")
    }
    assert not any("СКРЫТО" in name for name in visible)


def test_raw_probe_still_carries_name_sources(ax_fixture):
    frames = ax_fixture("probe_raw")
    assert any(
        "sources" in (n.get("name") or {})
        for frame in frames
        for n in frame["nodes"]
    )
