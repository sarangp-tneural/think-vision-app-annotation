"""Server class -> project class mapping rules (pure, no database)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from dataset_import import class_map as cm  # noqa: E402

SERVER = ["person", "cash", "id_visible"]


def test_names_from_list_and_index_keyed_dict():
    assert cm.server_names_from_yaml(["a", "b"]) == ["a", "b"]
    assert cm.server_names_from_yaml({1: "b", 0: "a", 10: "k", 2: "c", 3: "d", 4: "e", 5: "f", 6: "g", 7: "h", 8: "i", 9: "j"})[10] == "k"
    assert cm.server_names_from_yaml({"0": "a", "1": " b "}) == ["a", "b"]


@pytest.mark.parametrize("bad", [{0: "a", 2: "c"}, {1: "a"}, {"x": "a"}, [], None, ["a", " "], 5])
def test_bad_names_are_rejected(bad):
    with pytest.raises(ValueError):
        cm.server_names_from_yaml(bad)


def test_default_mapping_matches_exact_names_and_marks_the_rest_new():
    m = cm.default_mapping(SERVER, ["cash", "Person"])
    assert [(x["action"], x["target"]) for x in m] == [("new", "person"), ("match", "cash"), ("new", "id_visible")]  # exact: "Person" != "person"


def test_resolve_appends_new_classes_in_server_order_and_never_sorts():
    r = cm.resolve_mapping(SERVER, ["zebra"], None)
    assert r["project_classes_after"] == ["zebra", "person", "cash", "id_visible"]
    assert r["class_map"] == {"0": "person", "1": "cash", "2": "id_visible"}
    assert r["new_classes"] == ["person", "cash", "id_visible"]


def test_resolve_with_user_choices_match_skip_and_rename():
    r = cm.resolve_mapping(SERVER, ["cash", "dude"], [
        {"server_idx": 0, "action": "match", "target": "dude"},
        {"server_idx": 2, "action": "skip"},
    ])
    assert r["project_classes_after"] == ["cash", "dude"]
    assert r["class_map"] == {"0": "dude", "1": "cash", "2": None}
    r = cm.resolve_mapping(SERVER, [], [{"server_idx": 0, "action": "new", "target": "human"}])
    assert r["project_classes_after"] == ["human", "cash", "id_visible"]


@pytest.mark.parametrize("project,mapping,fragment", [
    (["a"], [{"server_idx": 0, "action": "match", "target": "ghost"}], "not a project class"),
    (["person"], [{"server_idx": 0, "action": "new", "target": "person"}], "already exists"),
    (["a"], [{"server_idx": 0, "action": "new", "target": "x"}, {"server_idx": 1, "action": "new", "target": "x"}], "Two server classes"),
    (["a"], [{"server_idx": 0, "action": "match", "target": "a"}, {"server_idx": 1, "action": "match", "target": "a"}], "both map to"),
    (["a"], [{"server_idx": 9, "action": "skip"}], "unknown server class"),
    (["a"], [{"server_idx": 0, "action": "wat"}], "Unknown mapping action"),
    (["a"], [{"server_idx": 0, "action": "new", "target": "  "}], "Invalid new class name"),
])
def test_invalid_mappings_are_rejected(project, mapping, fragment):
    with pytest.raises(ValueError, match=fragment):
        cm.resolve_mapping(SERVER, project, mapping)


def test_replace_uses_the_server_order_only_when_nothing_is_annotated():
    r = cm.resolve_mapping(SERVER, ["zebra", "cash"], [{"server_idx": 1, "action": "skip"}],
                           replace_project_classes=True, can_replace=True)
    assert r["project_classes_after"] == SERVER  # server order wins; skip is ignored so indices line up
    assert r["class_map"] == {"0": "person", "1": "cash", "2": "id_visible"}
    with pytest.raises(ValueError, match="no image has annotations"):
        cm.resolve_mapping(SERVER, ["zebra"], None, replace_project_classes=True, can_replace=False)
