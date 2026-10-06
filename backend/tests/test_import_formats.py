"""Importing YOLO-seg, YOLO-OBB, COCO and Pascal VOC zips through the real import API."""
import io
import json
import math
import os
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402
import _inprocess  # noqa: E402
from _inprocess import fresh_db, get_client  # noqa: E402

import yolo_codec as yc  # noqa: E402

NAMES_YAML = "train: train/images\nval: valid/images\nnames:\n  0: person\n  1: cash\n"


def img(color, size=(100, 50)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def zipped(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


@pytest.fixture(scope="module")
def db():
    yield fresh_db()


@pytest.fixture(scope="module")
def client(db):
    return get_client()


def _register(client, name):
    r = client.post("/api/auth/register", json={"email": f"{name}@example.com", "password": "pw123456", "name": name})
    assert r.status_code == 200, r.text
    return r.json()["user"]["id"], {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture(scope="module")
def world(client):
    w = {}
    w["owner_id"], w["owner"] = _register(client, "fowner")
    w["team"] = next(t for t in client.get("/api/teams", headers=w["owner"]).json() if t["is_personal"])["id"]
    return w


@pytest.fixture(autouse=True)
def storage(monkeypatch):
    monkeypatch.setattr(_inprocess.server, "put_object", lambda path, data, ct: {"path": path, "size": len(data)})


def new_project(client, world):
    return client.post("/api/projects", json={"name": "Fmt", "team_id": world["team"]}, headers=world["owner"]).json()["id"]


def start(client, world, pid, data, **form):
    r = client.post(f"/api/projects/{pid}/imports", files={"file": ("ds.zip", data, "application/zip")},
                    data={"review_mode": "approved", **form}, headers=world["owner"])
    assert r.status_code == 202, r.text
    return client.get(f"/api/imports/{r.json()['batch_id']}", headers=world["owner"]).json()


def commit(client, world, bid, **body):
    assert client.post(f"/api/imports/{bid}/commit", json=body, headers=world["owner"]).status_code == 202
    return client.get(f"/api/imports/{bid}", headers=world["owner"]).json()


def by_name(db, pid):
    return {i["filename"]: i for i in db.images.find({"project_id": pid})}


# ---------------------------------------------------------------- YOLO segmentation

SEG_ZIP = {
    "data.yaml": NAMES_YAML,
    "train/images/a.png": img((255, 0, 0)), "train/labels/a.txt": "0 0.1 0.1 0.5 0.1 0.3 0.6\n1 0.5 0.5 0.2 0.2\n",  # polygon + a plain box
    "valid/images/b.png": img((0, 255, 0)), "valid/labels/b.txt": "1 0.2 0.2 0.8 0.2 0.8 0.7 0.5 0.9 0.2 0.7\n",   # 5-point polygon
}


def test_segmentation_is_detected_from_the_polygons_and_imported_as_polygons(client, db, world):
    pid = new_project(client, world)
    b = start(client, world, pid, zipped(SEG_ZIP))
    assert b["status"] == "ready" and b["report"]["label_format"] == "seg" and b["report"]["source_format"] == "yolo"
    assert b["report"]["unsupported_format"] == 0 and b["report"]["importable"] == 2
    commit(client, world, b["id"])
    imgs = by_name(db, pid)
    a = imgs["a.png"]
    assert [x["type"] for x in a["annotations"]] == ["polygon", "bbox"] and a["annotations"][0]["label"] == "person"
    assert a["annotations"][0]["points"] == [[0.1, 0.1], [0.5, 0.1], [0.3, 0.6]]
    assert a["review_status"] == "approved" and a["provenance"]["label_format"] == "seg"
    assert len(imgs["b.png"]["annotations"][0]["points"]) == 5
    link = db.projects.find_one({"id": pid})["remote_dataset"]
    assert link["label_format"] == "seg" and db.projects.find_one({"id": pid})["classes_locked"] is True
    # the baseline is what the merge recomputes from the annotations, so an untouched image never looks edited
    ltoi = {"person": 0, "cash": 1}
    items, _u, lossy = yc.annotations_to_items(a["annotations"], ltoi, "seg", (a["width"], a["height"]))
    assert lossy == 0 and yc.canonical_hash(items, (a["width"], a["height"])) == a["provenance"]["label_hash_at_import"]


# ---------------------------------------------------------------- YOLO oriented boxes

def obb_line(cls, x, y, w, h, rot, size, order=(0, 1, 2, 3)):
    corners = yc.obb_to_corners(x, y, w, h, rot, size[0], size[1])
    return f"{cls} " + " ".join(f"{corners[i][0]:.6f} {corners[i][1]:.6f}" for i in order)


def test_nine_value_labels_need_a_declared_format_and_obb_is_read_in_pixel_space(client, db, world):
    size = (100, 50)  # NOT square: this is where normalized-space rotation goes wrong
    box = (0.2, 0.3, 0.4, 0.2, 0.5)  # x, y, w, h, rotation (rad)
    zip_bytes = zipped({"data.yaml": NAMES_YAML,
                        "train/images/a.png": img((255, 0, 0), size), "train/labels/a.txt": obb_line(0, *box, size) + "\n",
                        "valid/images/b.png": img((0, 255, 0), size), "valid/labels/b.txt": obb_line(1, *box, size, order=(2, 3, 0, 1)) + "\n"})
    pid = new_project(client, world)
    ambiguous = start(client, world, pid, zip_bytes)
    assert ambiguous["status"] == "failed" and "segmentation" in ambiguous["error"] and "OBB" in ambiguous["error"]

    b = start(client, world, pid, zip_bytes, label_format="obb")
    assert b["status"] == "ready" and b["report"]["label_format"] == "obb" and b["report"]["unsupported_format"] == 0
    commit(client, world, b["id"])
    imgs = by_name(db, pid)
    for name in ("a.png", "b.png"):
        ann = imgs[name]["annotations"][0]
        # starting from another corner may give the same rectangle rotated by an extra pi: equal modulo pi
        assert ann["type"] == "bbox" and abs(((ann["rotation"] - 0.5 + math.pi / 2) % math.pi) - math.pi / 2) < 1e-3
        assert (ann["x"], ann["y"], ann["w"], ann["h"]) == pytest.approx(box[:4], abs=1e-4)
    # the same rectangle listed from a different starting corner is the same label
    assert imgs["a.png"]["provenance"]["label_hash_at_import"] != imgs["b.png"]["provenance"]["label_hash_at_import"]  # (different class)
    one = yc.canonical_hash(yc.annotations_to_items([imgs["a.png"]["annotations"][0]], {"person": 7}, "obb", (100, 50))[0], (100, 50))
    two = yc.canonical_hash(yc.annotations_to_items([imgs["b.png"]["annotations"][0]], {"cash": 7}, "obb", (100, 50))[0], (100, 50))
    assert one == two


def test_obb_lines_that_are_not_rectangles_or_have_other_lengths_are_not_trusted(client, db, world):
    size = (100, 50)
    skew = "0 0.1 0.1 0.9 0.2 0.8 0.9 0.2 0.8"  # four corners, but not a rectangle
    six = "0 0.1 0.1 0.5 0.1 0.3 0.6"
    zip_bytes = zipped({"data.yaml": NAMES_YAML, "train/images/a.png": img((1, 1, 1), size), "train/labels/a.txt": skew + "\n",
                        "train/images/b.png": img((2, 2, 2), size), "train/labels/b.txt": six + "\n"})
    pid = new_project(client, world)
    b = start(client, world, pid, zip_bytes, label_format="obb")
    assert b["report"]["malformed_lines"] == 1 and b["report"]["unsupported_format"] == 1
    commit(client, world, b["id"], include_unsupported=True)
    imgs = by_name(db, pid)
    assert imgs["a.png"]["provenance"]["skipped_label_lines"] == [skew] and imgs["a.png"]["review_status"] == "unassigned"
    assert imgs["b.png"]["provenance"]["skipped_label_lines"] == [six] and imgs["b.png"]["review_status"] == "unassigned"


# ---------------------------------------------------------------- COCO

def coco_json(images, annotations, categories):
    return json.dumps({"images": images, "annotations": annotations, "categories": categories})


ROBOFLOW_CATS = [{"id": 0, "name": "myG-project", "supercategory": "none"}, {"id": 1, "name": "person", "supercategory": "myG-project"},
                 {"id": 2, "name": "cash", "supercategory": "myG-project"}]


def roboflow_coco():
    train = coco_json(
        [{"id": 0, "file_name": "a.png", "width": 100, "height": 50}, {"id": 1, "file_name": "b.png", "width": 100, "height": 50},
         {"id": 2, "file_name": "gone.png", "width": 100, "height": 50}],
        [{"id": 1, "image_id": 0, "category_id": 1, "bbox": [10, 5, 20, 10], "segmentation": [], "iscrowd": 0},
         {"id": 2, "image_id": 0, "category_id": 2, "bbox": [0, 0, 100, 25], "segmentation": [[10, 5, 50, 5, 30, 40]], "iscrowd": 0},
         {"id": 3, "image_id": 1, "category_id": 1, "bbox": [1, 1, 5, 5], "segmentation": {"counts": "abc", "size": [50, 100]}, "iscrowd": 0}],
        ROBOFLOW_CATS)
    valid = coco_json([{"id": 0, "file_name": "c.png", "width": 100, "height": 50}],
                      [{"id": 9, "image_id": 0, "category_id": 2, "bbox": [50, 25, 50, 25], "iscrowd": 0}], ROBOFLOW_CATS)
    return {"train/_annotations.coco.json": train, "train/a.png": img((9, 0, 0)), "train/b.png": img((0, 9, 0)),
            "valid/_annotations.coco.json": valid, "valid/c.png": img((0, 0, 9))}


def test_roboflow_coco_classes_splits_boxes_polygons_and_unsupported_annotations(client, db, world):
    pid = new_project(client, world)
    b = start(client, world, pid, zipped(roboflow_coco()))
    rep = b["report"]
    assert b["status"] == "ready" and rep["source_format"] == "coco" and rep["label_format"] == "coco"
    assert rep["server_names"] == ["person", "cash"]  # the unused catch-all category (supercategory "none") is not a class
    assert rep["counts_by_split"] == {"train": 2, "valid": 1} and rep["missing_files"] == 1  # gone.png is listed but not in the zip
    assert rep["unsupported_format"] == 1 and "alphabetical" not in (rep["names_order"] or "")
    commit(client, world, b["id"])
    assert "b.png" not in by_name(db, pid)  # an image with only an unsupported annotation is skipped unless asked for
    pid = new_project(client, world)
    b = start(client, world, pid, zipped(roboflow_coco()))
    commit(client, world, b["id"], include_unsupported=True)
    imgs = by_name(db, pid)
    a = imgs["a.png"]
    assert a["split"] == "train" and a["review_status"] == "approved"
    box, poly = a["annotations"]
    assert box["type"] == "bbox" and (box["x"], box["y"], box["w"], box["h"]) == pytest.approx((0.1, 0.1, 0.2, 0.2))  # pixels -> normalized
    assert poly["type"] == "polygon" and poly["label"] == "cash" and poly["points"][1] == pytest.approx([0.5, 0.1])
    assert imgs["b.png"]["review_status"] == "unassigned" and imgs["b.png"]["provenance"]["has_unsupported_lines"]  # RLE mask: never trusted
    assert imgs["c.png"]["annotations"][0]["label"] == "cash" and imgs["c.png"]["split"] == "valid"
    proj = db.projects.find_one({"id": pid})
    assert proj["classes"] == ["person", "cash"]
    assert not proj.get("classes_locked") and not proj.get("remote_dataset")  # a COCO file has no server class order to follow


def test_generic_coco_layout_images_folder_and_split_from_the_json_name(client, db, world):
    cats = [{"id": 5, "name": "dog"}, {"id": 3, "name": "cat"}]
    entries = {
        "annotations/instances_train.json": coco_json([{"id": 1, "file_name": "x.png", "width": 100, "height": 50}],
                                                      [{"image_id": 1, "category_id": 5, "bbox": [0, 0, 10, 10]}], cats),
        "annotations/instances_val.json": coco_json([{"id": 1, "file_name": "y.png", "width": 100, "height": 50}],
                                                    [{"image_id": 1, "category_id": 3, "bbox": [0, 0, 10, 10], "iscrowd": 1}], cats),
        "images/x.png": img((1, 2, 3)), "images/y.png": img((3, 2, 1)),
    }
    pid = new_project(client, world)
    b = start(client, world, pid, zipped(entries), source_format="coco")
    assert b["report"]["server_names"] == ["cat", "dog"]  # sorted by category id (3, 5)
    assert b["report"]["counts_by_split"] == {"train": 1, "valid": 1}
    commit(client, world, b["id"], include_unsupported=True)
    imgs = by_name(db, pid)
    assert imgs["x.png"]["annotations"][0]["label"] == "dog" and imgs["y.png"]["review_status"] == "unassigned"  # crowd region


def test_coco_files_that_disagree_about_categories_are_refused(client, db, world):
    entries = {"a/_annotations.coco.json": coco_json([], [], [{"id": 1, "name": "x"}]), "b/_annotations.coco.json": coco_json([], [], [{"id": 1, "name": "y"}])}
    b = start(client, world, new_project(client, world), zipped(entries))
    assert b["status"] == "failed" and "disagree" in b["error"]


# ---------------------------------------------------------------- Pascal VOC

def voc_xml(filename, objects, size=(100, 50)):
    objs = "".join(f"<object><name>{n}</name><bndbox><xmin>{x0}</xmin><ymin>{y0}</ymin><xmax>{x1}</xmax><ymax>{y1}</ymax></bndbox>{extra}</object>"
                   for n, x0, y0, x1, y1, extra in objects)
    return f"<annotation><filename>{filename}</filename><size><width>{size[0]}</width><height>{size[1]}</height></size>{objs}</annotation>"


def test_roboflow_style_voc_images_and_xml_side_by_side(client, db, world):
    entries = {"train/a.png": img((1, 0, 0)), "train/a.xml": voc_xml("a.png", [("person", 10, 5, 30, 25, "")]),
               "valid/b.png": img((0, 1, 0)), "valid/b.xml": voc_xml("b.png", [("cash", 0, 0, 50, 25, "<polygon><x1>10</x1><y1>5</y1><x2>50</x2><y2>5</y2><x3>30</x3><y3>40</y3></polygon>")])}
    pid = new_project(client, world)
    b = start(client, world, pid, zipped(entries))
    assert b["report"]["source_format"] == "voc" and b["report"]["server_names"] == ["cash", "person"]  # alphabetical: VOC has no class list
    assert "alphabetical" in b["report"]["names_order"] and b["report"]["counts_by_split"] == {"train": 1, "valid": 1}
    commit(client, world, b["id"])
    imgs = by_name(db, pid)
    assert imgs["a.png"]["annotations"][0]["type"] == "bbox" and imgs["a.png"]["annotations"][0]["x"] == pytest.approx(0.1)
    assert imgs["b.png"]["annotations"][0]["type"] == "polygon" and imgs["b.png"]["annotations"][0]["label"] == "cash"
    assert not db.projects.find_one({"id": pid}).get("classes_locked")


def test_classic_voc_layout_with_imagesets_split_lists(client, db, world):
    entries = {"VOC/JPEGImages/001.png": img((1, 1, 0)), "VOC/JPEGImages/002.png": img((0, 1, 1)),
               "VOC/Annotations/001.xml": voc_xml("001.png", [("dog", 1, 1, 20, 20, "")]),
               "VOC/Annotations/002.xml": voc_xml("002.png", [("dog", 5, 5, 40, 30, "")]),
               "VOC/ImageSets/Main/train.txt": "001\n", "VOC/ImageSets/Main/val.txt": "002\n", "VOC/ImageSets/Main/trainval.txt": "001\n002\n"}
    pid = new_project(client, world)
    b = start(client, world, pid, zipped(entries), source_format="voc")
    assert b["report"]["counts_by_split"] == {"train": 1, "valid": 1}
    commit(client, world, b["id"])
    imgs = by_name(db, pid)
    assert imgs["001.png"]["split"] == "train" and imgs["002.png"]["split"] == "valid"


def test_xml_with_entity_declarations_is_refused_not_expanded(client, db, world):
    bomb = ('<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">]>'
            "<annotation><filename>a.png</filename><object><name>&b;</name></object></annotation>")
    b = start(client, world, new_project(client, world), zipped({"a.png": img((1, 2, 3)), "a.xml": bomb}))
    assert b["status"] == "failed" and "Pascal VOC" in b["error"]  # nothing usable: the document was never parsed


# ---------------------------------------------------------------- choosing a format

def test_format_options_are_validated_and_mismatches_are_explained(client, db, world):
    pid = new_project(client, world)
    bad = client.post(f"/api/projects/{pid}/imports", files={"file": ("d.zip", zipped(SEG_ZIP), "application/zip")},
                      data={"source_format": "tiff"}, headers=world["owner"])
    assert bad.status_code == 400
    assert client.post(f"/api/projects/{pid}/imports", files={"file": ("d.zip", zipped(SEG_ZIP), "application/zip")},
                       data={"label_format": "polygons"}, headers=world["owner"]).status_code == 400
    wrong = start(client, world, pid, zipped(SEG_ZIP), source_format="coco")
    assert wrong["status"] == "failed" and "COCO" in wrong["error"]
    unknown = start(client, world, pid, zipped({"readme.txt": "hi", "a.png": img((1, 1, 1))}))
    assert unknown["status"] == "failed" and "Could not recognise" in unknown["error"]
