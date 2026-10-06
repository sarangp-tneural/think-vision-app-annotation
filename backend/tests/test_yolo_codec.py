"""YOLO detect parsing / serialising and the canonical label hash (pure, no database)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import yolo_codec as yc  # noqa: E402


def test_detect_line_converts_centre_to_top_left():
    b = yc.parse_detect_line("3 0.5 0.5 0.2 0.4")
    assert b["cls"] == 3
    assert (round(b["x"], 6), round(b["y"], 6), round(b["w"], 6), round(b["h"], 6)) == (0.4, 0.3, 0.2, 0.4)


def test_round_trip_through_format_detect_line():
    line = "2 0.300000 0.700000 0.100000 0.200000"
    b = yc.parse_detect_line(line)
    assert yc.format_detect_line(b["cls"], b["x"], b["y"], b["w"], b["h"]) == line


def test_box_overshooting_the_edge_is_clamped_not_dropped():
    b = yc.parse_detect_line("0 0.99 0.5 0.1 0.2")  # right edge at 1.04
    assert b["x"] + b["w"] == pytest.approx(1.0)


@pytest.mark.parametrize("line", [
    "0 0.5 0.5 0 0.2",      # zero width
    "-1 0.5 0.5 0.2 0.2",   # negative class
    "0 1.5 0.5 0.2 0.2",    # not normalized (pixel coordinates)
    "0 nan 0.5 0.2 0.2",
    "x 0.5 0.5 0.2 0.2",
    "0.5 0.5 0.5 0.2 0.2",  # fractional class id
])
def test_invalid_detect_lines_are_malformed(line):
    parsed = yc.parse_label_text(line)
    assert parsed.boxes == [] and parsed.malformed == [line]


def test_label_text_separates_detect_unsupported_and_garbage():
    text = "\n".join([
        "# comment", "",
        "0 0.5 0.5 0.2 0.2",
        "1 0.1 0.1 0.5 0.1 0.5 0.5 0.1 0.5",   # 9 tokens: segmentation / OBB
        "hello world",
        "2 0.25 0.25 0.1 0.1",
    ])
    p = yc.parse_label_text(text)
    assert [b["cls"] for b in p.boxes] == [0, 2]
    assert p.boxes[0]["raw"] == "0 0.5 0.5 0.2 0.2"
    assert p.unsupported == ["1 0.1 0.1 0.5 0.1 0.5 0.5 0.1 0.5"]
    assert p.malformed == ["hello world"]


def test_empty_label_file_is_valid_and_empty():
    assert yc.parse_label_text("") == yc.ParsedLabels([], [], [])
    assert yc.parse_label_text("\n\n") == yc.ParsedLabels([], [], [])


def test_canonical_hash_ignores_order_and_float_formatting():
    a = yc.parse_label_text("0 0.5 0.5 0.2 0.4\n1 0.25 0.25 0.1 0.1").boxes
    b = yc.parse_label_text("1 0.250000 0.250000 0.100000 0.100000\n0 0.50 0.50 0.20 0.40").boxes
    items = lambda boxes: [(x["cls"], x["x"], x["y"], x["w"], x["h"]) for x in boxes]  # noqa: E731
    assert yc.canonical_hash(items(a)) == yc.canonical_hash(items(b))


def test_canonical_hash_detects_a_real_change():
    base = [(0, 0.4, 0.3, 0.2, 0.4)]
    assert yc.canonical_hash(base) != yc.canonical_hash([(0, 0.4, 0.3, 0.2, 0.41)])
    assert yc.canonical_hash(base) != yc.canonical_hash([(1, 0.4, 0.3, 0.2, 0.4)])
    assert yc.canonical_hash(base) != yc.canonical_hash([])


def test_app_boxes_translate_back_to_the_same_hash_through_the_class_map():
    parsed = yc.parse_label_text("0 0.5 0.5 0.2 0.4\n2 0.25 0.25 0.1 0.1")
    imported = yc.canonical_hash([(b["cls"], b["x"], b["y"], b["w"], b["h"]) for b in parsed.boxes])
    app_boxes = [{"type": "bbox", "label": {0: "person", 2: "cash"}[b["cls"]], "x": b["x"], "y": b["y"], "w": b["w"], "h": b["h"]}
                 for b in parsed.boxes]
    now = yc.canonical_hash(yc.boxes_to_canonical_items(app_boxes, {"person": 0, "cash": 2}))
    assert now == imported
    # a label with no server index, or a non-box shape, is ignored rather than guessed
    assert yc.boxes_to_canonical_items(app_boxes + [{"type": "polygon", "label": "person"}, {"type": "bbox", "label": "new"}],
                                       {"person": 0, "cash": 2}) == yc.boxes_to_canonical_items(app_boxes, {"person": 0, "cash": 2})


# ---------- segmentation / oriented boxes ----------

import math  # noqa: E402

SEG = "3 0.1 0.1 0.5 0.1 0.3 0.6"                       # triangle, 7 tokens
SQ = "1 0.1 0.1 0.5 0.1 0.5 0.5 0.1 0.5"                 # 9 tokens: also a valid OBB


def test_seg_lines_become_polygons_and_boxes_are_still_read():
    p = yc.parse_label_text(SEG + "\n0 0.5 0.5 0.2 0.2", "seg")
    assert [b["kind"] for b in p.boxes] == ["polygon", "bbox"] and p.unsupported == [] and p.malformed == []
    poly = p.boxes[0]
    assert poly["cls"] == 3 and poly["points"] == [[0.1, 0.1], [0.5, 0.1], [0.3, 0.6]]
    assert (poly["x"], poly["y"], poly["w"], poly["h"]) == (0.1, 0.1, pytest.approx(0.4), pytest.approx(0.5))


def test_the_same_nine_token_line_is_a_polygon_or_an_oriented_box_depending_on_the_format():
    assert yc.parse_label_text(SQ, "seg").boxes[0]["kind"] == "polygon"
    obb = yc.parse_label_text(SQ, "obb", size=(200, 100)).boxes[0]
    assert obb["kind"] == "obb" and obb["rotation"] == pytest.approx(0.0)
    assert (obb["x"], obb["y"], obb["w"], obb["h"]) == (pytest.approx(0.1), pytest.approx(0.1), pytest.approx(0.4), pytest.approx(0.4))
    assert yc.parse_label_text(SQ, "detect").unsupported == [SQ]  # read as plain boxes it is not representable


def test_other_lengths_are_unsupported_for_obb_and_degenerate_shapes_are_malformed():
    assert yc.parse_label_text(SEG, "obb", size=(10, 10)).unsupported == [SEG]  # an oriented box needs exactly 4 corners
    for bad in ["1 0.1 0.1 0.2 0.2 0.3 0.3",            # collinear: zero area
                "1 0.1 0.1 0.2 0.2 0.3 0.3 0.4",        # odd coordinate count
                "1 0.1 0.1 1.5 0.1 0.3 0.6",            # not normalized
                "1 0.1 0.1 nan 0.1 0.3 0.6"]:
        assert yc.parse_label_text(bad, "seg").boxes == [], bad
    assert yc.parse_label_text("1 0.1 0.1 0.9 0.2 0.8 0.9 0.2 0.8", "obb", size=(100, 100)).malformed  # not a rectangle
    assert yc.parse_label_text(SQ, "obb").malformed  # no image size: cannot be converted, kept as-is


def test_oriented_box_conversion_matches_the_annotators_pixel_space_convention_on_a_wide_image():
    W, H = 400, 200
    box = {"x": 0.3, "y": 0.4, "w": 0.2, "h": 0.1, "rotation": math.radians(30)}  # 80 x 20 px, tilted 30 deg clockwise
    corners = yc.obb_to_corners(box["x"], box["y"], box["w"], box["h"], box["rotation"], W, H)
    # the first edge runs along (cos, sin) in PIXELS
    dx, dy = (corners[1][0] - corners[0][0]) * W, (corners[1][1] - corners[0][1]) * H
    assert math.degrees(math.atan2(dy, dx)) == pytest.approx(30.0, abs=1e-6) and math.hypot(dx, dy) == pytest.approx(80.0)
    back = yc.obb_from_corners(corners, W, H)
    assert all(back[k] == pytest.approx(box[k], abs=1e-9) for k in box)


@pytest.mark.parametrize("order", [[0, 1, 2, 3], [1, 2, 3, 0], [2, 3, 0, 1], [3, 2, 1, 0], [0, 3, 2, 1]])
def test_any_corner_order_describes_the_same_rectangle(order):
    W, H = 320, 240
    base = yc.obb_to_corners(0.2, 0.3, 0.3, 0.2, math.radians(-20), W, H)
    got = yc.obb_from_corners([base[i] for i in order], W, H)
    ref = yc.obb_from_corners(base, W, H)
    h1 = yc.canonical_hash([{"kind": "obb", "cls": 0, **got}], (W, H))
    h2 = yc.canonical_hash([{"kind": "obb", "cls": 0, **ref}], (W, H))
    assert h1 == h2  # the hash is what decides "edited or not", so it must not depend on where the file started


def test_canonical_hash_treats_swapped_axes_and_the_pi_boundary_as_the_same_box():
    s = (100, 100)
    a = yc.canonical_hash([{"kind": "obb", "cls": 0, "x": .1, "y": .1, "w": .4, "h": .2, "rotation": 0.0}], s)
    b = yc.canonical_hash([{"kind": "obb", "cls": 0, "x": .2, "y": 0.0, "w": .2, "h": .4, "rotation": math.pi / 2}], s)  # same rectangle, axes swapped
    c = yc.canonical_hash([{"kind": "obb", "cls": 0, "x": .1, "y": .1, "w": .4, "h": .2, "rotation": math.pi - 1e-12}], s)
    assert a == b == c
    assert a != yc.canonical_hash([{"kind": "obb", "cls": 0, "x": .1, "y": .1, "w": .4, "h": .2, "rotation": 0.3}], s)
    # a square has four equivalent orientations
    sq = lambda r: yc.canonical_hash([{"kind": "obb", "cls": 0, "x": .2, "y": .2, "w": .3, "h": .3, "rotation": r}], s)  # noqa: E731
    assert sq(0.0) == sq(math.pi / 2) == sq(math.pi)


def test_polygon_and_box_hashes_are_distinct_and_box_hashes_are_unchanged_from_before():
    poly = yc.canonical_hash([{"kind": "polygon", "cls": 0, "points": [[.1, .1], [.5, .1], [.3, .6]]}])
    assert poly != yc.canonical_hash([{"kind": "polygon", "cls": 0, "points": [[.1, .1], [.5, .1], [.3, .61]]}])
    # hashes written by earlier imports must still match: tuples and kind=bbox dicts use the original encoding
    old = yc.canonical_hash([(0, 0.4, 0.3, 0.2, 0.4)])
    assert old == yc.canonical_hash([{"kind": "bbox", "cls": 0, "x": 0.4, "y": 0.3, "w": 0.2, "h": 0.4}])
    import hashlib
    assert old == hashlib.sha256(b"0 0.500000 0.500000 0.200000 0.400000").hexdigest()  # the exact encoding earlier imports stored


@pytest.mark.parametrize("fmt,line", [("detect", "2 0.300000 0.700000 0.100000 0.200000"), ("seg", "3 0.100000 0.100000 0.500000 0.100000 0.300000 0.600000"),
                                      ("obb", "1 0.100000 0.100000 0.500000 0.100000 0.500000 0.500000 0.100000 0.500000")])
def test_every_format_round_trips_file_to_annotation_to_file(fmt, line):
    size = (200, 100)
    parsed = yc.parse_label_text(line, fmt, size).boxes[0]
    ann = yc.box_to_annotation(parsed, "thing")
    items, unknown, lossy = yc.annotations_to_items([ann], {"thing": parsed["cls"]}, fmt, size)
    assert unknown == [] and lossy == 0
    assert yc.item_to_line(items[0], size) == line
    assert yc.canonical_hash(items, size) == yc.canonical_hash(yc.annotations_to_items(
        [yc.box_to_annotation(yc.parse_label_text(line, fmt, size).boxes[0], "thing")], {"thing": parsed["cls"]}, fmt, size)[0], size)


def test_boxes_polygons_and_rotated_boxes_are_converted_for_each_target_format():
    size = (200, 100)
    anns = [{"type": "bbox", "label": "a", "x": .1, "y": .1, "w": .2, "h": .2},
            {"type": "bbox", "label": "a", "x": .5, "y": .5, "w": .2, "h": .2, "rotation": 0.4},
            {"type": "polygon", "label": "a", "points": [[.1, .1], [.5, .1], [.3, .6]]},
            {"type": "ellipse", "label": "a", "x": .5, "y": .5, "rx": .1, "ry": .1}]
    detect, _u, lossy_d = yc.annotations_to_items(anns, {"a": 0}, "detect", size)
    seg, _u, lossy_s = yc.annotations_to_items(anns, {"a": 0}, "seg", size)
    obb, _u, lossy_o = yc.annotations_to_items(anns, {"a": 0}, "obb", size)
    assert len(detect) == len(seg) == len(obb) == 4 and all(isinstance(i, tuple) for i in detect)
    assert [i["kind"] for i in seg] == ["polygon"] * 4 and len(seg[0]["points"]) == 4 and len(seg[3]["points"]) == 24
    assert [i["kind"] for i in obb] == ["obb"] * 4 and obb[1]["rotation"] == pytest.approx(0.4) and obb[2]["rotation"] == 0.0
    assert (lossy_d, lossy_s, lossy_o) == (3, 1, 2)  # what each format cannot keep exactly: rotation+polygon+ellipse / ellipse / polygon+ellipse
    # a rotated box written as a polygon keeps its rotation (it needs the image size to do so)
    assert seg[1]["points"] != [[.5, .5], [.7, .5], [.7, .7], [.5, .7]]
    with pytest.raises(ValueError):
        yc.item_to_line(obb[0])  # an oriented box cannot be written without the image size


def test_resolve_format_only_guesses_when_the_files_make_it_unambiguous():
    assert yc.resolve_format([5, 5, 5]) == ("detect", None) and yc.resolve_format([]) == ("detect", None)
    assert yc.resolve_format([5, 7, 9, 13]) == ("seg", None)       # a 3-point or 6-point polygon cannot be an OBB
    fmt, problem = yc.resolve_format([9, 9, 5])
    assert fmt is None and "segmentation" in problem and "OBB" in problem
    assert yc.resolve_format([9, 9], "obb") == ("obb", None) and yc.resolve_format([5], "seg") == ("seg", None)
