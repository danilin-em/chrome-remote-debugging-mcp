"""The captured fixtures must contain what the renderer's tests rely on."""

import pytest

NAMES = ["probe", "hn", "youtube", "wikipedia"]

# Node-count floors, well below the counts a real capture produced (probe 51,
# hn 1597, youtube 3431, wikipedia 8037) but far above a trivial "did it
# parse" check. A capture script has no readiness check beyond a fixed
# sleep(), so a cookie-consent wall, region block, A/B variant, or slow/
# partial load can still write a small-but-nonempty tree that a `> 5` floor
# would pass trivially. These floors are sized to survive ordinary page
# drift (comment counts, ads, layout experiments) while still failing loudly
# on a capture that's a small fraction of the real page.
NODE_FLOORS = {"probe": 20, "hn": 500, "youtube": 1500, "wikipedia": 3000}

# A string each page's own chrome (site name, nav, skip-link) reliably
# carries regardless of today's content. Deliberately NOT a headline, video
# title, or search result -- those rotate hourly and would make this test
# flaky for the wrong reason. Presence of this string is a proxy for "this
# is really the intended page, fully rendered" -- see the fix report for why
# each string was chosen.
IDENTIFYING_CONTENT = {
    "hn": "Hacker News",
    "youtube": "YouTube Home",
    "wikipedia": "Jump to content",
}


@pytest.mark.parametrize("name", NAMES)
def test_fixture_loads_and_has_nodes(ax_fixture, name):
    frames = ax_fixture(name)
    assert frames, f"{name} has no frames"
    assert all("frameId" in f and "nodes" in f for f in frames)
    assert sum(len(f["nodes"]) for f in frames) > NODE_FLOORS[name]


@pytest.mark.parametrize("name", sorted(IDENTIFYING_CONTENT))
def test_fixture_contains_durable_identifying_content(ax_fixture, name):
    frames = ax_fixture(name)
    names = {
        (n.get("name") or {}).get("value", "")
        for frame in frames
        for n in frame["nodes"]
    }
    landmark = IDENTIFYING_CONTENT[name]
    assert any(landmark in n for n in names), (
        f"{name} fixture is missing its durable landmark {landmark!r} -- "
        "capture may be degraded (cookie wall / region block / A-B variant "
        "/ partial load) even though it has enough nodes to pass the floor "
        "check"
    )


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
    # Chrome drops display:none / visibility:hidden content from the
    # accessibility tree entirely -- it does not surface as a node with
    # ignored=True -- so the hidden strings must be absent from every node,
    # not just the ones an `ignored` filter would already exclude.
    all_names = {(n.get("name") or {}).get("value", "") for n in probe_nodes}
    assert not any("СКРЫТО" in name for name in all_names)

    # The fixture must still carry at least one ignored=True node somewhere,
    # so it can exercise the renderer's ignored-filter rule (a later task's
    # R1 depends on this). These come from structural/presentational nodes
    # (e.g. the checkbox <label> wrapper), not from the hidden paragraphs.
    assert any(n.get("ignored") for n in probe_nodes)


def test_raw_probe_still_carries_name_sources(ax_fixture):
    frames = ax_fixture("probe_raw")
    assert any(
        "sources" in (n.get("name") or {})
        for frame in frames
        for n in frame["nodes"]
    )
