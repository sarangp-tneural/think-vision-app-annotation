"""Pure merge decision logic: split buckets, class maps, label comparison, data.yaml edits (no db, no SSH)."""
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
import yaml  # noqa: E402

import yolo_codec  # noqa: E402
from dataset_import import merge_plan as mp  # noqa: E402


# ---------- split assignment ----------

def test_bucket_is_stable_and_spread_roughly_evenly():
    assert mp.bucket("abc") == mp.bucket("abc") and 0 <= mp.bucket("abc") < 1
    shares = mp.split_shares(0.7, 0.2, 0.1, ["train", "valid", "test"])
    got = Counter(mp.assign_split(f"hash-{i}", shares) for i in range(5000))
    assert abs(got["train"] / 5000 - 0.7) < 0.03 and abs(got["valid"] / 5000 - 0.2) < 0.03 and abs(got["test"] / 5000 - 0.1) < 0.03


def test_same_group_key_always_lands_in_the_same_split():
    shares = mp.split_shares(0.5, 0.3, 0.2, ["train", "valid", "test"])
    assert len({mp.assign_split("video-42", shares) for _ in range(20)}) == 1  # all frames of a video stay together


def test_shares_only_use_the_splits_the_server_has_and_renormalise():
    assert mp.split_shares(0.7, 0.2, 0.1, ["train", "valid"]) == [("train", pytest.approx(0.7 / 0.9)), ("valid", pytest.approx(0.2 / 0.9))]
    only_test = mp.split_shares(0.7, 0.2, 0.0, ["test"])
    assert only_test == [("test", 1.0)]
    assert all(mp.assign_split(f"k{i}", mp.split_shares(0.7, 0.2, 0.1, ["train", "valid"])) in ("train", "valid") for i in range(200))
    with pytest.raises(ValueError):
        mp.split_shares(0.7, 0.2, 0.1, [])


# ---------- classes ----------

NAMES = ["person", "cash", "id_visible"]


def test_class_maps_by_name_without_a_link():
    m = mp.class_maps(NAMES, ["id_visible", "person", "extra"], None)
    assert m["label_to_idx"] == {"person": 0, "cash": 1, "id_visible": 2} and m["error"] is None and m["name_drift"] == []


def test_class_maps_follow_the_import_mapping_and_report_drift_and_skips():
    link = {"server_names": NAMES, "class_map": {"0": "human", "1": None, "2": "id_visible"}}
    m = mp.class_maps(NAMES, ["human", "id_visible"], link)
    assert m["label_to_idx"] == {"human": 0, "id_visible": 2}
    assert m["idx_to_label"][1] is None  # skipped server class: its lines are never rewritten
    assert m["name_drift"] == [{"idx": 0, "server_name": "person", "project_label": "human"}]


def test_class_maps_reject_a_server_whose_class_list_changed_under_us():
    link = {"server_names": NAMES, "class_map": {"0": "person", "1": "cash", "2": "id_visible"}}
    assert mp.class_maps(["cash", "person", "id_visible"], [], link)["error"]
    assert mp.class_maps(NAMES + ["extra"], [], link)["error"] is None  # appended classes are fine


def test_new_class_slots_continue_after_the_last_server_index_in_project_order():
    slots = mp.new_class_slots(["zeta", "alpha", "person"], ["person", "zeta", "alpha"], {"person": 0}, 3)
    assert slots == [{"idx": 3, "name": "zeta"}, {"idx": 4, "name": "alpha"}]  # project order, not alphabetical
    assert mp.new_class_slots(["person"], ["person"], {"person": 0}, 3) == []


# ---------- label comparison ----------

IDX = {0: "person", 1: None, 2: "id_visible"}


def test_remote_signature_separates_owned_boxes_from_lines_we_must_leave_alone():
    text = "0 0.5 0.5 0.2 0.4\n1 0.1 0.1 0.1 0.1\n2 0.3 0.3 0.1 0.1\n9 0.2 0.2 0.1 0.1\n5 0.1 0.1 0.2 0.1 0.2 0.2 0.1 0.2\njunk\n"
    sig = mp.remote_label_signature(text, IDX)
    mine = yolo_codec.canonical_hash([(0, 0.4, 0.3, 0.2, 0.4), (2, 0.25, 0.25, 0.1, 0.1)])
    assert sig["hash"] == mine
    assert sig["others"] == sorted(["1 0.1 0.1 0.1 0.1", "9 0.2 0.2 0.1 0.1", "5 0.1 0.1 0.2 0.1 0.2 0.2 0.1 0.2", "junk"])


def test_only_changed_labels_need_the_server_copy_to_be_read():
    assert mp.needs_remote_label("h1", "h1") is False
    assert mp.needs_remote_label("h1", "h2") is True
    assert mp.needs_remote_label(None, "h1") is True


@pytest.mark.parametrize("baseline,current,remote_hash,remote_others,expected", [
    ("b", "b", "b", [], "unchanged"),                    # same everywhere
    ("b", "c", "c", [], "unchanged"),                    # server already has our edit
    ("b", "c", "b", [], "update_label"),                 # we edited, server untouched since
    ("b", "c", "x", [], "conflict"),                     # server changed independently
    (None, "c", "c", [], "unchanged"),                   # no history, but they agree
    (None, "c", "b", [], "conflict"),                    # no history, they differ: do not guess
    ("b", "c", "b", ["9 0.1 0.1 0.1 0.1"], "conflict"),  # lines we don't own changed on the server
])
def test_decide_matched_matrix(baseline, current, remote_hash, remote_others, expected):
    assert mp.decide_matched(baseline, current, {"hash": remote_hash, "others": remote_others}, [])[0] == expected


def test_preserved_lines_are_part_of_the_comparison_and_rewritten_verbatim():
    keep = ["9 0.2 0.2 0.1 0.1"]
    assert mp.decide_matched("b", "c", {"hash": "b", "others": keep}, keep)[0] == "update_label"
    text = mp.label_file_text([(0, 0.4, 0.3, 0.2, 0.4)], keep)
    assert text == "0 0.500000 0.500000 0.200000 0.400000\n9 0.2 0.2 0.1 0.1\n"
    assert mp.label_file_text([], []) == ""


def test_annotations_to_items_reduces_shapes_to_boxes_and_reports_unknown_and_lossy():
    anns = [{"type": "bbox", "label": "person", "x": .1, "y": .1, "w": .2, "h": .2},
            {"type": "polygon", "label": "person", "points": [[.1, .1], [.5, .1], [.3, .6]]},
            {"type": "bbox", "label": "person", "x": .1, "y": .1, "w": .2, "h": .2, "rotation": .3},
            {"type": "bbox", "label": "ghost", "x": 0, "y": 0, "w": .1, "h": .1},
            {"type": "bbox", "label": "person", "x": 0, "y": 0, "w": 0, "h": .1}]
    items, unknown, lossy = yolo_codec.annotations_to_items(anns, {"person": 0})
    assert len(items) == 3 and unknown == ["ghost"] and lossy == 2
    assert items[1][1:] == pytest.approx((.1, .1, .4, .5))


# ---------- data.yaml append ----------

BLOCK_DICT = "# dataset\npath: /root/x\n\ntrain: train/images\nval: valid/images\n\nnc: 3\n\nnames:\n  0: person\n  1: cash\n  2: id_visible\n"


def _names(text):
    n = yaml.safe_load(text)["names"]
    return [n[k] for k in sorted(n)] if isinstance(n, dict) else n


def test_block_dict_names_are_edited_in_place_keeping_comments_and_nc():
    new, reformatted = mp.append_class_names(BLOCK_DICT, [{"idx": 3, "name": "mop"}, {"idx": 4, "name": "odd: name"}])
    assert reformatted is False and new.startswith("# dataset\npath: /root/x\n")
    assert "  3: mop\n  4: \"odd: name\"\n" in new and "nc: 5" in new
    assert _names(new) == NAMES + ["mop", "odd: name"]
    old, now = yaml.safe_load(BLOCK_DICT), yaml.safe_load(new)
    assert {k: v for k, v in old.items() if k not in ("names", "nc")} == {k: v for k, v in now.items() if k not in ("names", "nc")}


def test_block_list_names_are_edited_in_place():
    text = "train: t\nval: v\nnc: 2\nnames:\n  - a\n  - b\n"
    new, reformatted = mp.append_class_names(text, [{"idx": 2, "name": "c"}])
    assert reformatted is False and new.endswith("  - b\n  - c\n") and "nc: 3" in new and _names(new) == ["a", "b", "c"]


def test_flow_style_names_fall_back_to_reserialising_and_say_so():
    new, reformatted = mp.append_class_names("# keep me\ntrain: t\nnames: [a, b]\nnc: 2\n", [{"idx": 2, "name": "c"}])
    assert reformatted is True and _names(new) == ["a", "b", "c"] and yaml.safe_load(new)["nc"] == 3


def test_append_requires_consecutive_indices_and_is_a_noop_without_additions():
    with pytest.raises(ValueError):
        mp.append_class_names(BLOCK_DICT, [{"idx": 5, "name": "x"}])
    assert mp.append_class_names(BLOCK_DICT, []) == (BLOCK_DICT, False)


def test_names_followed_by_other_keys_are_not_swallowed():
    text = "names:\n  0: a\n  1: b\nextra: 1\n"
    new, _ = mp.append_class_names(text, [{"idx": 2, "name": "c"}])
    assert _names(new) == ["a", "b", "c"] and yaml.safe_load(new)["extra"] == 1
