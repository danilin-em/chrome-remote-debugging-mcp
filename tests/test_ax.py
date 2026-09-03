"""Rendering rules R1-R12, tested against captured accessibility trees."""

import chrome_remote_debugging_mcp.ax as ax


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
