"""Rendering rules R1-R12, tested against captured accessibility trees."""

import re

import chrome_remote_debugging_mcp.ax as ax

# Matches only the "role#ref " token `format_interactive` puts at the start of
# a rendered line (after any block indentation) — not an incidental "#123 "
# that might appear inside a node's own label text.
_REF_LINE_RE = re.compile(r"^\s*[A-Za-z]+#(\d+) ")


def _named(nodes, name):
    return next(n for n in nodes if ax.node_name(n) == name)


def test_accessors_read_the_nested_blobs(probe_nodes):
    button = _named(probe_nodes, "Обычная кнопка")
    assert ax.node_role(button) == "button"
    assert ax.node_name(button) == "Обычная кнопка"


def test_accessors_tolerate_missing_and_null_blobs():
    assert ax.node_role({}) == ""
    assert ax.node_role({"role": None}) == ""
    assert ax.node_role({"role": {"value": None}}) == ""
    assert ax.node_name({}) == ""
    assert ax.node_name({"name": None}) == ""
    assert ax.node_name({"name": {"value": None}}) == ""
    assert ax.node_value({}) == ""
    assert ax.node_value({"value": None}) == ""
    assert ax.node_value({"value": {"value": None}}) == ""
    assert ax.node_properties({}) == {}
    assert ax.node_properties({"properties": None}) == {}


def test_node_properties_flattens_the_property_list():
    node = {"properties": [
        {"name": "checked", "value": {"type": "tristate", "value": "true"}},
        {"name": "focusable", "value": {"type": "booleanOrUndefined", "value": True}},
    ]}
    assert ax.node_properties(node) == {"checked": "true", "focusable": True}


def test_is_punctuation_matches_separators_only():
    assert ax.is_punctuation("|")
    assert ax.is_punctuation(" · ")
    assert ax.is_punctuation("()")
    assert not ax.is_punctuation("by")
    assert not ax.is_punctuation("")


def test_strip_sources_removes_name_metadata(ax_fixture):
    nodes = [n for frame in ax_fixture("probe_raw") for n in frame["nodes"]]
    assert any("sources" in (n.get("name") or {}) for n in nodes)
    ax.strip_sources(nodes)
    assert not any("sources" in (n.get("name") or {}) for n in nodes)


def test_strip_sources_tolerates_missing_names():
    nodes = [{"nodeId": "1"}, {"nodeId": "2", "name": None}]
    ax.strip_sources(nodes)
    assert nodes == [{"nodeId": "1"}, {"nodeId": "2", "name": None}]


def test_strip_sources_leaves_other_sources_keys_alone():
    """R11 near-miss: only ``name.sources`` is dropped. A ``value`` blob that
    happens to carry its own ``sources`` key must survive untouched — the rule
    targets ``name`` specifically, not every ``sources`` key anywhere in the
    tree."""
    nodes = [{"nodeId": "1",
              "name": {"value": "x", "sources": ["a"]},
              "value": {"value": "y", "sources": ["b"]}}]
    ax.strip_sources(nodes)
    assert "sources" not in nodes[0]["name"]
    assert nodes[0]["value"]["sources"] == ["b"]


def test_unnamed_backend_ids_finds_only_unlabelled_interactives(probe_nodes):
    ids = ax.unnamed_backend_ids(probe_nodes)
    assert isinstance(ids, list)
    # the probe page's <input name="q2"> has no accessible name and no url
    assert any(n.get("backendDOMNodeId") in ids
               for n in probe_nodes
               if ax.node_role(n) == "textbox" and not ax.node_name(n))
    # a named control is never included
    named = _named(probe_nodes, "Обычная кнопка")
    assert named.get("backendDOMNodeId") not in ids


def test_unnamed_backend_ids_skips_ignored_named_and_url_bearing():
    nodes = [
        {"nodeId": "1", "role": {"value": "link"}, "name": {"value": ""},
         "ignored": True, "backendDOMNodeId": 11},
        {"nodeId": "2", "role": {"value": "link"}, "name": {"value": "Home"},
         "backendDOMNodeId": 12},
        {"nodeId": "3", "role": {"value": "link"}, "name": {"value": ""},
         "properties": [{"name": "url", "value": {"value": "https://x/"}}],
         "backendDOMNodeId": 13},
        {"nodeId": "4", "role": {"value": "generic"}, "name": {"value": ""},
         "backendDOMNodeId": 14},
        {"nodeId": "5", "role": {"value": "button"}, "name": {"value": ""}},
        {"nodeId": "6", "role": {"value": "button"}, "name": {"value": ""},
         "backendDOMNodeId": 16},
    ]
    assert ax.unnamed_backend_ids(nodes) == [16]


def test_format_interactive_uses_the_accessible_name():
    node = {"role": {"value": "button"}, "name": {"value": "Отправить"}}
    assert ax.format_interactive(node, 12, {}) == 'button#12 "Отправить"'


def test_format_interactive_falls_back_to_url_then_attributes_then_marker():
    with_url = {"role": {"value": "link"}, "name": {"value": ""},
                "properties": [{"name": "url", "value": {"value": "https://x/v?id=1"}}]}
    assert ax.format_interactive(with_url, 3, {}) == "link#3 -> https://x/v?id=1"

    with_attr = {"role": {"value": "textbox"}, "name": {"value": ""},
                 "backendDOMNodeId": 40}
    assert ax.format_interactive(with_attr, 4, {40: {"name": "q"}}) == 'textbox#4 "q"'

    nothing = {"role": {"value": "button"}, "name": {"value": ""},
               "backendDOMNodeId": 41}
    assert ax.format_interactive(nothing, 5, {41: {}}) == "button#5 [unnamed]"
    assert ax.format_interactive(nothing, 5, {}) == "button#5 [unnamed]"


def test_format_interactive_prefers_attributes_in_declared_order():
    node = {"role": {"value": "textbox"}, "name": {"value": ""},
            "backendDOMNodeId": 7}
    attrs = {7: {"id": "search-box", "placeholder": "Найти", "name": "q"}}
    assert ax.format_interactive(node, 1, attrs) == 'textbox#1 "Найти"'


def test_format_interactive_appends_value_and_flags():
    node = {
        "role": {"value": "textbox"}, "name": {"value": "Поиск"},
        "value": {"value": "котики"},
        "properties": [
            {"name": "required", "value": {"value": True}},
            {"name": "readonly", "value": {"value": "false"}},
            {"name": "focusable", "value": {"value": True}},
        ],
    }
    assert ax.format_interactive(node, 7, {}) == 'textbox#7 "Поиск" = "котики" [required]'


def test_format_interactive_omits_empty_value_and_absent_flags():
    node = {"role": {"value": "textbox"}, "name": {"value": "Поиск"},
            "value": {"value": ""}}
    assert ax.format_interactive(node, 2, {}) == 'textbox#2 "Поиск"'


def _link(name, url):
    return {"role": {"value": "link"}, "name": {"value": name},
            "properties": [{"name": "url", "value": {"value": url}}]}


def test_format_interactive_appends_a_named_links_url():
    node = _link("GitHub", "https://github.com/foo")
    assert ax.format_interactive(node, 1, {}) == 'link#1 "GitHub" -> https://github.com/foo'


def test_format_interactive_shortens_a_same_origin_url_to_its_path():
    base = "https://admin.example.com/admin/player/view/1"
    same = _link("Игроки", "https://admin.example.com/admin/player/?q=1#top")
    assert (ax.format_interactive(same, 1, {}, base_url=base)
            == 'link#1 "Игроки" -> /admin/player/?q=1#top')
    other = _link("GitHub", "https://github.com/foo")
    assert (ax.format_interactive(other, 2, {}, base_url=base)
            == 'link#2 "GitHub" -> https://github.com/foo')
    other_port = _link("Dev", "https://admin.example.com:8443/x")
    assert (ax.format_interactive(other_port, 3, {}, base_url=base)
            == 'link#3 "Dev" -> https://admin.example.com:8443/x')
    mail = _link("mail", "mailto:a@b.c")
    assert ax.format_interactive(mail, 4, {}, base_url=base) == 'link#4 "mail" -> mailto:a@b.c'


def test_format_interactive_shows_a_link_to_the_current_page_as_its_fragment():
    base = "https://h.test/player/view/1?tab=a"
    for url, shown in [("https://h.test/player/view/1?tab=a#", "#"),
                       ("https://h.test/player/view/1?tab=a", "#"),
                       ("https://h.test/player/view/1?tab=a#notes", "#notes"),
                       ("https://h.test/player/view/1?tab=b", "/player/view/1?tab=b"),
                       ("https://h.test/player/view/2?tab=a", "/player/view/2?tab=a")]:
        assert (ax.format_interactive(_link("x", url), 1, {}, base_url=base)
                == f'link#1 "x" -> {shown}')


def test_format_interactive_truncates_a_long_url():
    long = "https://h.test/sessions?filter=" + "a%3A1" * 60
    line = ax.format_interactive(_link("История", long), 1, {}, base_url="https://h.test/")
    shown = line.split(" -> ")[1]
    assert len(shown) == ax.MAX_URL
    assert shown == ("/sessions?filter=" + "a%3A1" * 60)[:ax.MAX_URL - 1] + "…"
    unnamed = ax.format_interactive(_link("", long), 2, {}, base_url="https://h.test/")
    assert unnamed == f"link#2 -> {shown}"
    exact = "https://x.test/" + "b" * (ax.MAX_URL - len("https://x.test/"))
    assert ax.format_interactive(_link("y", exact), 3, {}) == f'link#3 "y" -> {exact}'


def test_format_interactive_keeps_a_root_slash_for_a_bare_origin_url():
    node = _link("Home", "https://admin.example.com")
    assert (ax.format_interactive(node, 1, {}, base_url="https://admin.example.com/a")
            == 'link#1 "Home" -> /')


def test_format_interactive_does_not_repeat_a_url_used_as_the_label():
    node = _link("", "https://admin.example.com/v?id=1")
    assert (ax.format_interactive(node, 3, {}, base_url="https://admin.example.com/")
            == "link#3 -> /v?id=1")


def test_format_interactive_prints_no_arrow_for_a_link_without_url():
    node = {"role": {"value": "link"}, "name": {"value": "#"}}
    assert ax.format_interactive(node, 1, {}) == 'link#1 "#"'


def _select(options, role="combobox", value=""):
    """A select-like node with its options under a MenuListPopup, as Chrome builds it."""
    nodes = [
        {"nodeId": "s", "role": {"value": role}, "name": {"value": "Страна"},
         "value": {"value": value}, "childIds": ["p"]},
        {"nodeId": "p", "role": {"value": "MenuListPopup"}, "parentId": "s",
         "childIds": [f"o{i}" for i in range(len(options))]},
    ]
    nodes += [{"nodeId": f"o{i}", "role": {"value": "option"}, "name": {"value": name},
               "parentId": "p"} for i, name in enumerate(options)]
    return nodes, {n["nodeId"]: n for n in nodes}


def test_format_interactive_lists_the_options_of_a_select(probe_nodes):
    by_id = {n["nodeId"]: n for n in probe_nodes}
    select = next(n for n in probe_nodes if ax.node_role(n) == "combobox")
    assert (ax.format_interactive(select, 9, {}, by_id=by_id)
            == 'combobox#9 [unnamed] = "один" {один | два}')


def test_format_interactive_lists_every_option_without_collapsing():
    names = [f"o{i}" for i in range(13)]
    nodes, by_id = _select(names, role="listbox")
    assert (ax.format_interactive(nodes[0], 1, {}, by_id=by_id)
            == 'listbox#1 "Страна" {' + " | ".join(names) + "}")


def test_format_interactive_skips_ignored_and_unnamed_options():
    nodes, by_id = _select(["a", "", "b"])
    by_id["o2"]["ignored"] = True
    assert ax.format_interactive(nodes[0], 1, {}, by_id=by_id) == 'combobox#1 "Страна" {a}'


def test_format_interactive_prints_no_braces_without_options():
    nodes, by_id = _select([])
    assert ax.format_interactive(nodes[0], 1, {}, by_id=by_id) == 'combobox#1 "Страна"'
    # Without the tree there is nothing to walk.
    nodes, _ = _select(["a"])
    assert ax.format_interactive(nodes[0], 1, {}) == 'combobox#1 "Страна"'


def test_format_interactive_survives_a_dangling_child_id():
    nodes, by_id = _select(["a"])
    by_id["p"]["childIds"].append("gone")
    assert ax.format_interactive(nodes[0], 1, {}, by_id=by_id) == 'combobox#1 "Страна" {a}'


def _ranged(role, **bounds):
    return {"role": {"value": role}, "name": {"value": "Громкость"},
            "value": {"value": "30"},
            "properties": [{"name": k, "value": {"value": v}} for k, v in bounds.items()]}


def test_format_interactive_shows_range_bounds():
    assert (ax.format_interactive(_ranged("slider", valuemin=0, valuemax=100), 5, {})
            == 'slider#5 "Громкость" = "30" [0..100]')
    assert (ax.format_interactive(_ranged("spinbutton", valuemin=1), 6, {})
            == 'spinbutton#6 "Громкость" = "30" [1..]')
    assert (ax.format_interactive(_ranged("spinbutton", valuemax=9.5), 7, {})
            == 'spinbutton#7 "Громкость" = "30" [..9.5]')
    assert (ax.format_interactive(_ranged("slider"), 8, {})
            == 'slider#8 "Громкость" = "30"')


def test_format_interactive_shows_bounds_before_flags_and_only_for_range_roles():
    node = _ranged("slider", valuemin=0, valuemax=10)
    node["properties"].append({"name": "disabled", "value": {"value": True}})
    assert (ax.format_interactive(node, 1, {})
            == 'slider#1 "Громкость" = "30" [0..10] [disabled]')
    textbox = _ranged("textbox", valuemin=0, valuemax=10)
    assert ax.format_interactive(textbox, 2, {}) == 'textbox#2 "Громкость" = "30"'


def test_render_passes_base_url_and_tree_to_interactive_lines(probe_nodes):
    lines, _ = ax.render_nodes(probe_nodes)
    assert any(line.strip().endswith("{один | два}") for line in lines)
    link = dict(_link("Игроки", "https://h.test/players"), nodeId="1",
                backendDOMNodeId=5)
    lines, _ = ax.render_nodes([link], base_url="https://h.test/x")
    assert lines == ['link#1 "Игроки" -> /players']


def test_format_structure_prints_named_landmarks_only():
    assert ax.format_structure(
        {"role": {"value": "heading"}, "name": {"value": "Web accessibility"}}
    ) == 'heading "Web accessibility"'
    assert ax.format_structure(
        {"role": {"value": "navigation"}, "name": {"value": ""}}
    ) is None
    assert ax.format_structure(
        {"role": {"value": "paragraph"}, "name": {"value": "x"}}
    ) is None


def _render(nodes, **kw):
    lines, refs = ax.render_nodes(nodes, **kw)
    return lines, refs


def test_render_drops_ignored_wrappers_and_root(probe_nodes):
    lines, _ = _render(probe_nodes)
    joined = "\n".join(lines)
    assert "СКРЫТО" not in joined
    assert "RootWebArea" not in joined
    assert "generic" not in joined
    assert "InlineTextBox" not in joined


def test_render_only_drops_the_literal_root_web_area_role():
    """R5 near-miss: text whose *name* happens to be "RootWebArea" must still
    render — the rule drops a node whose *role* is exactly "RootWebArea", not
    anything that merely mentions it."""
    nodes = [{"nodeId": "1", "role": {"value": "StaticText"},
              "name": {"value": "RootWebArea"}}]
    lines, _ = ax.render_nodes(nodes)
    assert lines == ['text "RootWebArea"']


def test_render_keeps_visible_content_of_the_probe_page(probe_nodes):
    lines, refs = _render(probe_nodes)
    joined = "\n".join(lines)
    assert 'heading "Заголовок A"' in joined
    assert 'text "Видимый абзац."' in joined
    assert any(l.startswith("button#") and "Обычная кнопка" in l for l in lines)
    assert any("CLOSED SHADOW BUTTON" in l for l in lines)
    assert refs, "interactive nodes must produce refs"


def test_render_numbers_refs_from_ref_start_and_maps_backend_ids():
    nodes = [
        {"nodeId": "1", "role": {"value": "button"}, "name": {"value": "A"},
         "backendDOMNodeId": 101},
        {"nodeId": "2", "role": {"value": "button"}, "name": {"value": "B"},
         "backendDOMNodeId": 102},
    ]
    lines, refs = ax.render_nodes(nodes, ref_start=5)
    assert lines == ['button#6 "A"', 'button#7 "B"']
    assert refs == {6: 101, 7: 102}


def test_render_skips_a_ref_for_an_interactive_node_with_no_backend_id():
    """A node CDP gave no backendDOMNodeId for must never consume a ref number
    (fix round 1, Finding 1): printing ``#N`` with no matching entry in the
    refs map desyncs any caller that sizes its next ``ref_start`` from
    ``len(refs)`` — as ``browse.snapshot`` does across frames — letting a
    later frame reissue that same number for an unrelated element.
    """
    nodes = [
        {"nodeId": "1", "role": {"value": "button"}, "name": {"value": "Мёртвая"}},
        {"nodeId": "2", "role": {"value": "button"}, "name": {"value": "B"},
         "backendDOMNodeId": 102},
    ]
    lines, refs = ax.render_nodes(nodes)
    assert lines == ['button "Мёртвая"', 'button#1 "B"']
    assert refs == {1: 102}


def test_render_drops_an_unnamed_interactive_node_with_no_backend_id():
    nodes = [{"nodeId": "1", "role": {"value": "button"}, "name": {"value": ""}}]
    lines, refs = ax.render_nodes(nodes)
    assert lines == []
    assert refs == {}


def test_render_updates_a_supplied_ref_map_in_place():
    nodes = [{"nodeId": "1", "role": {"value": "button"}, "name": {"value": "A"},
              "backendDOMNodeId": 101}]
    existing = {1: 99}
    lines, refs = ax.render_nodes(nodes, ref_start=1, refs=existing)
    assert refs is existing
    assert existing == {1: 99, 2: 101}


def test_render_drops_punctuation_text_and_parent_duplicates():
    nodes = [
        {"nodeId": "1", "role": {"value": "link"}, "name": {"value": "Home"},
         "backendDOMNodeId": 1, "childIds": ["2"]},
        {"nodeId": "2", "role": {"value": "StaticText"}, "name": {"value": "Home"},
         "parentId": "1"},
        {"nodeId": "3", "role": {"value": "StaticText"}, "name": {"value": "|"}},
        {"nodeId": "4", "role": {"value": "StaticText"}, "name": {"value": ""}},
        {"nodeId": "5", "role": {"value": "StaticText"}, "name": {"value": "by"}},
    ]
    lines, _ = ax.render_nodes(nodes)
    assert lines == ['link#1 "Home"', 'text "by"']


def test_render_keeps_text_whose_parent_name_is_never_printed():
    """A table cell's accessible name is computed from its own text, but a
    cell prints no line of its own — dropping the text as a "duplicate" of
    it erased every plain-text cell from the view (live admin page, sport
    bets table: only the id links survived)."""
    nodes = [
        {"nodeId": "r", "role": {"value": "row"}, "childIds": ["c1", "c2"]},
        {"nodeId": "c1", "role": {"value": "cell"}, "name": {"value": "100.00 RU"},
         "parentId": "r", "childIds": ["t1"]},
        {"nodeId": "t1", "role": {"value": "StaticText"}, "name": {"value": "100.00 RU"},
         "parentId": "c1"},
        {"nodeId": "c2", "role": {"value": "columnheader"}, "name": {"value": "Сумма"},
         "parentId": "r", "childIds": ["t2"]},
        {"nodeId": "t2", "role": {"value": "StaticText"}, "name": {"value": "Сумма"},
         "parentId": "c2"},
    ]
    lines, _ = ax.render_nodes(nodes)
    assert [line.strip() for line in lines if line] == ['text "100.00 RU"', 'text "Сумма"']


def test_render_drops_text_only_when_its_parent_prints_that_name():
    heading = [
        {"nodeId": "1", "role": {"value": "heading"}, "name": {"value": "Итоги"},
         "childIds": ["2"]},
        {"nodeId": "2", "role": {"value": "StaticText"}, "name": {"value": "Итоги"},
         "parentId": "1"},
    ]
    assert ax.render_nodes(heading)[0] == ['heading "Итоги"']
    # An ignored link prints nothing, so its text must survive.
    ignored_link = [
        {"nodeId": "1", "role": {"value": "link"}, "name": {"value": "Home"},
         "ignored": True, "backendDOMNodeId": 1, "childIds": ["2"]},
        {"nodeId": "2", "role": {"value": "StaticText"}, "name": {"value": "Home"},
         "parentId": "1"},
    ]
    assert ax.render_nodes(ignored_link)[0] == ['text "Home"']
    # A link with no backend id still prints `link "Home"` — text is a duplicate.
    unresolvable_link = [
        {"nodeId": "1", "role": {"value": "link"}, "name": {"value": "Home"},
         "childIds": ["2"]},
        {"nodeId": "2", "role": {"value": "StaticText"}, "name": {"value": "Home"},
         "parentId": "1"},
    ]
    assert ax.render_nodes(unresolvable_link)[0] == ['link "Home"']


def test_render_keeps_text_that_differs_from_its_parent_name():
    nodes = [
        {"nodeId": "1", "role": {"value": "paragraph"}, "name": {"value": "Intro"},
         "childIds": ["2"]},
        {"nodeId": "2", "role": {"value": "StaticText"}, "name": {"value": "Body"},
         "parentId": "1"},
    ]
    lines, _ = ax.render_nodes(nodes)
    assert lines == ['text "Body"']


def test_render_separates_blocks_with_a_blank_line_and_indents_nesting():
    nodes = [
        {"nodeId": "r1", "role": {"value": "row"}, "name": {"value": ""}},
        {"nodeId": "t1", "role": {"value": "StaticText"}, "name": {"value": "one"},
         "parentId": "r1"},
        {"nodeId": "r2", "role": {"value": "row"}, "name": {"value": ""}},
        {"nodeId": "t2", "role": {"value": "StaticText"}, "name": {"value": "two"},
         "parentId": "r2"},
        {"nodeId": "c1", "role": {"value": "cell"}, "name": {"value": ""},
         "parentId": "r2"},
        {"nodeId": "t3", "role": {"value": "StaticText"}, "name": {"value": "deep"},
         "parentId": "c1"},
    ]
    lines, _ = ax.render_nodes(nodes)
    assert lines == ['text "one"', "", 'text "two"', "", '  text "deep"']


def test_render_drops_blocks_with_no_renderable_content():
    nodes = [
        {"nodeId": "r1", "role": {"value": "row"}, "name": {"value": ""}},
        {"nodeId": "r2", "role": {"value": "row"}, "name": {"value": ""}},
        {"nodeId": "t1", "role": {"value": "StaticText"}, "name": {"value": "only"},
         "parentId": "r2"},
    ]
    lines, _ = ax.render_nodes(nodes)
    assert lines == ['text "only"']


def test_render_prints_a_heading_line_and_opens_a_block():
    nodes = [
        {"nodeId": "h", "role": {"value": "heading"}, "name": {"value": "Title"}},
        {"nodeId": "t", "role": {"value": "StaticText"}, "name": {"value": "Body"},
         "parentId": "h"},
    ]
    lines, _ = ax.render_nodes(nodes)
    assert lines == ['heading "Title"', "", '  text "Body"']


def test_block_path_skips_wrapper_ancestors_and_keeps_block_ones():
    nodes = [
        {"nodeId": "sec", "role": {"value": "region"}, "name": {"value": ""}},
        {"nodeId": "lt", "role": {"value": "LayoutTable"}, "name": {"value": ""},
         "parentId": "sec"},
        {"nodeId": "ltr", "role": {"value": "LayoutTableRow"}, "name": {"value": ""},
         "parentId": "lt"},
        {"nodeId": "cell", "role": {"value": "cell"}, "name": {"value": ""},
         "parentId": "ltr"},
        {"nodeId": "t", "role": {"value": "StaticText"}, "name": {"value": "x"},
         "parentId": "cell"},
    ]
    by_id = {n["nodeId"]: n for n in nodes}
    # "lt" and "ltr" are wrapper roles and must not appear in the path; "sec"
    # (region) and "cell" are block roles and must, outermost first.
    assert ax.block_path(by_id["t"], by_id) == ("sec", "cell")


def test_render_ignores_wrapper_ancestors_when_computing_indentation():
    # Two items at genuinely different block depths: "flat" reaches its root
    # through wrapper roles only (LayoutTable/LayoutTableRow), so its block
    # path is empty and it must render at column 0; "nested" sits one level
    # inside a real block role ("row"), so it must be indented by one level.
    # A `block_path` that (wrongly) counted wrapper ancestors would instead
    # put "flat" one level deeper than "nested" (path lengths 2 vs 1), which
    # flips both the indentation and which side of the blank-line split each
    # line lands on — so this fails under that bug.
    nodes = [
        {"nodeId": "lt", "role": {"value": "LayoutTable"}, "name": {"value": ""}},
        {"nodeId": "ltr", "role": {"value": "LayoutTableRow"}, "name": {"value": ""},
         "parentId": "lt"},
        {"nodeId": "t1", "role": {"value": "StaticText"}, "name": {"value": "flat"},
         "parentId": "ltr"},
        {"nodeId": "r1", "role": {"value": "row"}, "name": {"value": ""}},
        {"nodeId": "t2", "role": {"value": "StaticText"}, "name": {"value": "nested"},
         "parentId": "r1"},
    ]
    lines, _ = ax.render_nodes(nodes)
    assert lines == ['text "flat"', "", '  text "nested"']


def test_render_normalises_indentation_to_the_shallowest_content():
    nodes = [
        {"nodeId": "m", "role": {"value": "main"}, "name": {"value": ""}},
        {"nodeId": "p", "role": {"value": "paragraph"}, "name": {"value": ""},
         "parentId": "m"},
        {"nodeId": "t", "role": {"value": "StaticText"}, "name": {"value": "only"},
         "parentId": "p"},
    ]
    lines, _ = ax.render_nodes(nodes)
    assert lines == ['text "only"']


def test_render_handles_an_empty_tree():
    assert ax.render_nodes([]) == ([], {})


def test_render_drops_option_nodes_since_they_cannot_be_clicked():
    """M8: "option" was removed from INTERACTIVE_ROLES. A native <option>
    cannot be clicked — that is why `select` exists as D5's documented
    exception — so giving it a ref would be a ref no action could ever
    resolve, the same defect class already avoided for a node with no
    backendDOMNodeId."""
    nodes = [{"nodeId": "1", "role": {"value": "option"}, "name": {"value": "один"}}]
    lines, refs = ax.render_nodes(nodes)
    assert lines == []
    assert refs == {}


def test_render_survives_a_broken_parent_link():
    nodes = [{"nodeId": "1", "role": {"value": "StaticText"},
              "name": {"value": "orphan"}, "parentId": "missing"}]
    lines, _ = ax.render_nodes(nodes)
    assert lines == ['text "orphan"']


def test_render_finds_resolvable_refs_on_real_pages(ax_fixture):
    """Renamed from ...and_no_unresolved_labels (M11): that name promised a
    guarantee the test never checked, and it would in fact be false — Hacker
    News's bottom search box has no name, no url and no matching DOM attribute
    in the captured tree, so it renders exactly one `[unnamed]` line. What is
    actually true, and what this test checks, is that every ref printed in the
    text has a matching entry in the refs map and vice versa."""
    for name in ("hn", "youtube", "wikipedia"):
        frames = ax_fixture(name)
        lines, refs = ax.render_nodes(frames[0]["nodes"])
        assert refs, f"{name} produced no refs"
        for ref in refs:
            assert any(f"#{ref} " in line for line in lines), (
                f"{name} ref #{ref} is not reachable in the rendered text"
            )
        text_refs = set()
        for line in lines:
            match = _REF_LINE_RE.match(line)
            if match:
                text_refs.add(int(match.group(1)))
        orphans = text_refs - set(refs)
        assert not orphans, (
            f"{name} rendered ref number(s) {orphans} with no entry in the refs map"
        )
        assert len(refs) >= 50, f"{name} produced only {len(refs)} refs, expected >= 50"
        assert lines, f"{name} produced no lines"


def test_render_labels_everything_it_can_and_names_what_it_cannot(ax_fixture):
    """The true claim the old test's name only implied: R7's attribute fallback
    resolves every interactive node's label on YouTube and Wikipedia, and all
    but one on Hacker News (the bottom search box), which still renders with a
    resolvable ref rather than being silently dropped."""
    hn_lines, _ = ax.render_nodes(ax_fixture("hn")[0]["nodes"])
    unnamed = [l for l in hn_lines if "[unnamed]" in l]
    assert len(unnamed) == 1, f"expected exactly one [unnamed] line on hn, got {unnamed}"

    for name in ("youtube", "wikipedia"):
        lines, _ = ax.render_nodes(ax_fixture(name)[0]["nodes"])
        assert not any("[unnamed]" in l for l in lines), f"{name} has an unresolved label"


# --- C1: document order, not tree-level order (see document_order docstring) -

def test_document_order_walks_depth_first_not_level_by_level():
    """``Accessibility.getFullAXTree`` returns nodes breadth-first: every node
    at depth *n* precedes every node at depth *n+1*. A level-ordered walk of
    this tree would yield root, a, b, a1 (b before a's own child); document
    order must instead descend into ``a`` before moving on to its sibling
    ``b``."""
    nodes = [
        {"nodeId": "root", "childIds": ["a", "b"]},
        {"nodeId": "a", "parentId": "root", "childIds": ["a1"]},
        {"nodeId": "b", "parentId": "root"},
        {"nodeId": "a1", "parentId": "a"},
    ]
    by_id = {n["nodeId"]: n for n in nodes}
    ordered = [n["nodeId"] for n in ax.document_order(nodes, by_id)]
    assert ordered == ["root", "a", "a1", "b"]


def test_document_order_terminates_on_a_child_id_cycle():
    """A cycle in ``childIds`` must terminate the walk, not hang the renderer."""
    nodes = [
        {"nodeId": "a", "childIds": ["b"]},
        {"nodeId": "b", "parentId": "a", "childIds": ["a"]},
    ]
    by_id = {n["nodeId"]: n for n in nodes}
    ordered = [n["nodeId"] for n in ax.document_order(nodes, by_id)]
    assert ordered == ["a", "b"]


def test_document_order_keeps_a_cycle_unreachable_from_any_root():
    """Every node here points to another node present in ``by_id``, so neither
    qualifies as a root under the missing/dangling-``parentId`` rule — they
    must still be emitted, not silently dropped, and the cycle guard must
    still stop the walk that finds them."""
    nodes = [
        {"nodeId": "x", "parentId": "y", "childIds": ["y"]},
        {"nodeId": "y", "parentId": "x", "childIds": ["x"]},
    ]
    by_id = {n["nodeId"]: n for n in nodes}
    ordered = {n["nodeId"] for n in ax.document_order(nodes, by_id)}
    assert ordered == {"x", "y"}


def test_document_order_appends_unreachable_orphans_in_original_order():
    nodes = [
        {"nodeId": "root", "childIds": ["c1"]},
        {"nodeId": "c1", "parentId": "root"},
        {"nodeId": "orphan-a", "parentId": "missing"},
        {"nodeId": "orphan-b", "parentId": "missing"},
    ]
    by_id = {n["nodeId"]: n for n in nodes}
    ordered = [n["nodeId"] for n in ax.document_order(nodes, by_id)]
    assert ordered == ["root", "c1", "orphan-a", "orphan-b"]


def test_render_orders_hn_by_document_not_by_tree_level(ax_fixture):
    """Regression for C1. Confirmed on the real capture before this fix: the
    footer link "Guidelines" sat at array index 19, the first story title at
    355, and the first "points" text at 161 — the view printed the footer
    before the header, and a story's own score hundreds of lines from its
    title. Pinned against the real fixture, not synthetic nodes: that gap is
    exactly what let the bug survive eleven task reviews and a 100%-covered
    suite.
    """
    lines, _ = ax.render_nodes(ax_fixture("hn")[0]["nodes"])

    header_idx = next(i for i, l in enumerate(lines) if '"Hacker News"' in l)
    footer_idx = next(i for i, l in enumerate(lines) if '"Guidelines"' in l)
    assert header_idx < 10, "the site header must be near the top of the view"
    assert header_idx < footer_idx, "the header must render before the footer"

    title_idx = next(i for i, l in enumerate(lines)
                     if '"Pre-Release of Polars 2.0"' in l)
    points_idx = next(i for i, l in enumerate(lines) if "points" in l)
    age_idx = next(i for i, l in enumerate(lines) if ' ago"' in l)
    assert title_idx < points_idx < title_idx + 10, (
        "a story's own score must render in the same block region as its title"
    )
    assert title_idx < age_idx < title_idx + 10, (
        "a story's own timestamp must render in the same block region as its title"
    )


def test_render_ref_counts_survive_the_ordering_fix(ax_fixture):
    """Reordering must not create or drop any interactive node — same content,
    different order. Pinned to the counts measured before this fix (226, 713)."""
    _, hn_refs = ax.render_nodes(ax_fixture("hn")[0]["nodes"])
    _, wiki_refs = ax.render_nodes(ax_fixture("wikipedia")[0]["nodes"])
    assert len(hn_refs) == 226
    assert len(wiki_refs) == 713


# --- Markdown tables -------------------------------------------------------

def _tree(spec, parent=None, out=None):
    """Build AX nodes from ``(id, role, name, children, extra)`` tuples."""
    out = [] if out is None else out
    node_id, role, name, children, extra = (list(spec) + [None, None])[:5]
    node = {"nodeId": node_id, "role": {"value": role}, "name": {"value": name or ""},
            "childIds": [c[0] for c in children or []]}
    if parent is not None:
        node["parentId"] = parent
    node.update(extra or {})
    out.append(node)
    for child in children or []:
        _tree(child, node_id, out)
    return out


def _text(node_id, text):
    return (node_id, "StaticText", text)


def _cell(node_id, *children, role="cell"):
    return (node_id, role, "", list(children))


def test_render_prints_a_table_as_markdown_with_its_header_row():
    nodes = _tree(("t", "table", "", [
        ("r0", "row", "", [
            _cell("h1", _text("h1t", "Id"), role="columnheader"),
            _cell("h2", _text("h2t", "Сумма"), role="columnheader"),
        ]),
        ("r1", "row", "", [
            _cell("c1", ("l1", "link", "6382470", [_text("l1t", "6382470")],
                         {"backendDOMNodeId": 70,
                          "properties": [{"name": "url",
                                          "value": {"value": "https://h.test/bet/1"}}]})),
            _cell("c2", _text("c2t", "100.00 RU")),
        ]),
    ]))
    lines, refs = ax.render_nodes(nodes, base_url="https://h.test/")
    assert lines == [
        "| Id | Сумма |",
        "|---|---|",
        '| link#1 "6382470" -> /bet/1 | 100.00 RU |',
    ]
    assert refs == {1: 70}


def test_render_gives_a_headerless_table_an_empty_header_and_pads_short_rows():
    nodes = _tree(("t", "table", "", [
        ("r1", "row", "", [_cell("a", _text("at", "a")), _cell("b", _text("bt", "b"))]),
        ("r2", "row", "", [_cell("c", _text("ct", "c"))]),
    ]))
    assert ax.render_nodes(nodes)[0] == [
        "|  |  |",
        "|---|---|",
        "| a | b |",
        "| c |  |",
    ]


def test_render_table_joins_cell_content_escapes_pipes_and_drops_empty_rows():
    nodes = _tree(("t", "grid", "", [
        ("rg", "rowgroup", "", [
            ("r1", "row", "", [
                _cell("a", _text("a1", "12.08.26"), _text("a2", "14:33"), role="gridcell"),
                _cell("b", _text("b1", "P2P|debug"), role="gridcell"),
            ]),
            ("r2", "row", "", [_cell("e1"), _cell("e2")]),
        ]),
    ]))
    assert ax.render_nodes(nodes)[0] == [
        "|  |  |",
        "|---|---|",
        "| 12.08.26 14:33 | P2P\\|debug |",
    ]


def test_render_table_keeps_document_order_refs_before_inside_and_after():
    def button(node_id, name, backend):
        return (node_id, "button", name, [], {"backendDOMNodeId": backend})
    nodes = _tree(("root", "RootWebArea", "", [
        button("before", "До", 1),
        ("t", "table", "", [
            ("r1", "row", "", [_cell("c1", button("in1", "A", 2)),
                               _cell("c2", button("in2", "B", 3))]),
        ]),
        button("after", "После", 4),
    ]))
    lines, refs = ax.render_nodes(nodes)
    assert [line.strip() for line in lines if line] == [
        'button#1 "До"',
        "|  |  |",
        "|---|---|",
        '| button#2 "A" | button#3 "B" |',
        'button#4 "После"',
    ]
    assert refs == {1: 1, 2: 2, 3: 3, 4: 4}


def test_render_table_prints_its_name_and_caption_around_the_grid():
    nodes = _tree(("t", "table", "Ставки", [
        ("cap", "caption", "", [_text("capt", "Последние 8")]),
        ("r1", "row", "", [_cell("c", _text("ct", "x"))]),
        _text("tail", "итого 1"),
    ]))
    assert ax.render_nodes(nodes)[0] == [
        'table "Ставки"',
        "",
        '  text "Последние 8"',
        "  |  |",
        "  |---|",
        "  | x |",
        '  text "итого 1"',
    ]


def test_render_table_flattens_a_nested_table_and_text_outside_cells():
    nodes = _tree(("t", "table", "", [
        ("r1", "row", "", [
            _text("loose", "вне ячейки"),
            _cell("c", ("inner", "table", "", [
                ("ir", "row", "", [_cell("ic1", _text("i1", "x")),
                                   _cell("ic2", _text("i2", "y"))]),
            ])),
        ]),
    ]))
    assert ax.render_nodes(nodes)[0] == [
        "|  |  |",
        "|---|---|",
        "| вне ячейки | x y |",
    ]


def test_render_falls_back_to_plain_lines_for_a_table_without_content_rows():
    nodes = _tree(("t", "table", "Пусто", [
        ("r1", "row", "", [_cell("c")]),
        _text("note", "No data"),
    ]))
    assert ax.render_nodes(nodes)[0] == ['table "Пусто"', "", '  text "No data"']


def test_render_skips_an_ignored_table_wrapper_but_keeps_its_content():
    nodes = _tree(("t", "table", "", [_text("x", "plain")], {"ignored": True}))
    assert ax.render_nodes(nodes)[0] == ['text "plain"']


def test_render_table_sees_through_wrappers_and_survives_a_repeated_child_id():
    nodes = _tree(("t", "table", "", [
        ("r1", "row", "", [
            ("w", "generic", "", [_cell("c1", _text("t1", "a"))]),
            _cell("c2", _text("t2", "b")),
        ]),
    ]))
    by_id = {n["nodeId"]: n for n in nodes}
    by_id["r1"]["childIds"].append("c2")  # the same cell listed twice
    assert ax.render_nodes(nodes)[0] == ["|  |  |", "|---|---|", "| a | b |"]


# --- R3 duplicate text: nearest meaningful ancestor --------------------------

def test_render_drops_text_duplicating_an_ancestor_behind_wrappers():
    """Hole 1: `link → generic → StaticText` printed the link's name twice
    (Wikipedia "Search", YouTube tabs "All") because only the direct parent
    was compared."""
    nodes = _tree(("root", "RootWebArea", "", [
        ("l", "link", "Search", [("w", "generic", "", [_text("t1", "Search")])],
         {"backendDOMNodeId": 1}),
        ("tab", "tab", "All", [("w2", "none", "", [
            ("w3", "generic", "", [_text("t2", "All")], {"ignored": True})])],
         {"backendDOMNodeId": 2}),
    ]))
    assert ax.render_nodes(nodes)[0] == ['link#1 "Search"', 'tab#2 "All"']


def test_render_keeps_cell_text_behind_a_wrapper():
    nodes = _tree(("root", "RootWebArea", "", [
        ("c", "cell", "100.00 RU", [("w", "generic", "", [_text("t", "100.00 RU")])]),
    ]))
    assert ax.render_nodes(nodes)[0] == ['text "100.00 RU"']


def test_render_drops_option_text_already_listed_by_its_select():
    """Hole 4: an ARIA listbox's options carry StaticText; the listbox prints
    them in braces, so they must not also print as text lines."""
    nodes = _tree(("root", "RootWebArea", "", [
        ("lb", "listbox", "Страна", [
            ("o1", "option", "Россия", [_text("t1", "Россия")]),
            ("o2", "option", "Сербия", [("w", "generic", "", [_text("t2", "Сербия")])]),
        ], {"backendDOMNodeId": 1}),
    ]))
    lines, _ = ax.render_nodes(nodes)
    assert [line.strip() for line in lines if line] == ['listbox#1 "Страна" {Россия | Сербия}']


def test_render_keeps_option_text_with_no_select_to_list_it():
    nodes = _tree(("root", "RootWebArea", "", [
        ("o", "option", "Сирота", [_text("t", "Сирота")]),
        ("lb", "listbox", "", [("o2", "option", "Скрыт", [_text("t2", "Скрыт")])],
         {"ignored": True}),
    ]))
    lines, _ = ax.render_nodes(nodes)
    assert [line.strip() for line in lines if line] == ['text "Сирота"', 'text "Скрыт"']
