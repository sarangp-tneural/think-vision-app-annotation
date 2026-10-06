"""Class indices stay fixed: append-only class list, and a lock that rejects anything that would shift them."""
import asyncio
import io
import json
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402
import _inprocess  # noqa: E402
from _inprocess import fresh_db, get_client  # noqa: E402

NOW = datetime.now(timezone.utc).isoformat()


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
def world(client, db):
    w = {}
    w["owner_id"], w["owner"] = _register(client, "lowner")
    team = next(t for t in client.get("/api/teams", headers=w["owner"]).json() if t["is_personal"])
    w["pid"] = client.post("/api/projects", json={"name": "Lock", "team_id": team["id"]}, headers=w["owner"]).json()["id"]
    _, w["ann"] = _register(client, "lann")
    assert client.post(f"/api/teams/{team['id']}/invite", json={"email": "lann@example.com", "role": "annotator"},
                       headers=w["owner"]).status_code == 200
    return w


def _box(label):
    return {"type": "bbox", "label": label, "x": .1, "y": .1, "w": .2, "h": .2}


def _setup(db, world, classes, locked=False):
    db.projects.update_one({"id": world["pid"]}, {"$set": {"classes": list(classes), "classes_locked": locked},
                                                  "$unset": {"class_name_drift": "", "class_origin": ""}})
    db.images.delete_many({"project_id": world["pid"]})
    db.images.insert_one({"id": "l-img", "project_id": world["pid"], "review_status": "assigned",
                          "assigned_to": world["owner_id"], "annotated": False, "annotations": [], "created_at": NOW})


def _save(client, world, *labels):
    return client.put("/api/images/l-img/annotations", json={"boxes": [_box(label) for label in labels]},
                      headers=world["owner"])


def _classes(db, world):
    return db.projects.find_one({"id": world["pid"]})["classes"]


def test_save_appends_new_labels_in_first_seen_order_without_sorting(client, db, world):
    _setup(db, world, ["zebra", "ant"])
    r = _save(client, world, "mango", "bee", "mango", "zebra")
    assert r.status_code == 200, r.text
    assert r.json()["classes"] == ["zebra", "ant", "mango", "bee"]  # was: sorted(...) shifting every index
    assert _classes(db, world) == ["zebra", "ant", "mango", "bee"]


def test_locked_project_rejects_unknown_label_and_writes_nothing(client, db, world):
    _setup(db, world, ["zebra", "ant"], locked=True)
    r = _save(client, world, "zebra", "ghost", "phantom")
    assert r.status_code == 422
    assert r.json()["detail"]["unknown_labels"] == ["ghost", "phantom"]
    assert _classes(db, world) == ["zebra", "ant"]
    assert db.images.find_one({"id": "l-img"})["annotations"] == []  # the rejected save changed nothing
    ok = _save(client, world, "ant", "zebra")
    assert ok.status_code == 200 and ok.json()["classes"] == ["zebra", "ant"]


def test_lock_endpoint_permissions_idempotence_and_project_fields(client, db, world):
    _setup(db, world, ["a", "b"])
    pid = world["pid"]
    assert client.post(f"/api/projects/{pid}/classes/lock", json={}, headers=world["ann"]).status_code == 403
    r = client.post(f"/api/projects/{pid}/classes/lock",
                    json={"source": "import", "data_yaml_path": "/srv/ds/data.yaml"}, headers=world["owner"])
    assert r.status_code == 200 and r.json() == {"classes": ["a", "b"], "classes_locked": True}
    first = db.projects.find_one({"id": pid})["class_origin"]
    assert first["source"] == "import" and first["data_yaml_path"] == "/srv/ds/data.yaml"
    again = client.post(f"/api/projects/{pid}/classes/lock", json={"source": "manual"}, headers=world["owner"])
    assert again.status_code == 200
    assert db.projects.find_one({"id": pid})["class_origin"] == first  # first lock's origin is kept
    proj = client.get(f"/api/projects/{pid}", headers=world["owner"]).json()
    assert proj["classes_locked"] is True and proj["class_origin"]["source"] == "import"


def test_locked_project_blocks_delete_and_merge_but_allows_rename_and_append(client, db, world):
    _setup(db, world, ["a", "b", "c"], locked=True)
    pid = world["pid"]
    assert client.delete(f"/api/projects/{pid}/classes/b", headers=world["owner"]).status_code == 409
    assert client.patch(f"/api/projects/{pid}/classes/a", json={"new_name": "c"}, headers=world["owner"]).status_code == 409
    assert _classes(db, world) == ["a", "b", "c"]

    r = client.patch(f"/api/projects/{pid}/classes/b", json={"new_name": "beta"}, headers=world["owner"])
    assert r.status_code == 200 and r.json()["classes"] == ["a", "beta", "c"]  # index preserved
    assert db.projects.find_one({"id": pid})["class_name_drift"] is True

    r = client.post(f"/api/projects/{pid}/classes/bulk", json={"labels": ["d", "e"]}, headers=world["owner"])
    assert r.status_code == 200 and r.json()["classes"] == ["a", "beta", "c", "d", "e"]


def test_unlocked_project_can_still_delete_and_merge(client, db, world):
    _setup(db, world, ["a", "b", "c"])
    pid = world["pid"]
    assert client.patch(f"/api/projects/{pid}/classes/a", json={"new_name": "c"}, headers=world["owner"]).status_code == 200
    assert client.delete(f"/api/projects/{pid}/classes/b", headers=world["owner"]).status_code == 200
    assert _classes(db, world) == ["c"]


def test_export_skips_unknown_labels_instead_of_mapping_them_to_class_0(client, db, world, monkeypatch):
    _setup(db, world, ["a", "b"])
    pid = world["pid"]
    db.images.insert_one({"id": "l-app", "project_id": pid, "filename": "x.jpg", "storage_path": "p/x.jpg",
                          "review_status": "approved", "annotated": True, "created_at": NOW,
                          "annotations": [_box("b"), _box("ghost")]})
    monkeypatch.setattr(_inprocess.server, "get_object", lambda path: (b"img", "image/jpeg"))

    r = client.get(f"/api/projects/{pid}/export?format=yolo", headers=world["owner"])
    assert r.status_code == 200 and r.headers["X-Skipped-Unknown-Labels"] == "1"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    lines = z.read("dataset/labels/l-app.txt").decode().splitlines()
    assert len(lines) == 1 and lines[0].startswith("1 ")  # "b" is index 1; "ghost" is gone, not class 0

    r = client.get(f"/api/projects/{pid}/export?format=coco", headers=world["owner"])
    assert r.status_code == 200 and r.headers["X-Skipped-Unknown-Labels"] == "1"
    coco = json.loads(zipfile.ZipFile(io.BytesIO(r.content)).read("dataset/annotations.json"))
    assert [a["category_id"] for a in coco["annotations"]] == [1]


def test_auto_label_in_locked_project_never_introduces_classes(monkeypatch):
    srv = _inprocess.server

    async def fake(project_id, image_bytes, project=None):
        return [{"label": "Dog", "x": 0, "y": 0, "w": .1, "h": .1}, {"label": "unicorn", "x": 0, "y": 0, "w": .1, "h": .1}], "gemini"

    monkeypatch.setattr(srv, "_auto_label_router_unfiltered", fake)
    locked = {"id": "p", "classes": ["dog", "cat"], "classes_locked": True}
    boxes, source = asyncio.run(srv._auto_label_router("p", b"", project=locked))
    assert [b["label"] for b in boxes] == ["dog"] and source == "gemini"  # remapped to the exact spelling, unknown dropped

    unlocked = {"id": "p", "classes": ["dog", "cat"]}
    boxes, _ = asyncio.run(srv._auto_label_router("p", b"", project=unlocked))
    assert [b["label"] for b in boxes] == ["Dog", "unicorn"]  # unchanged behaviour when not locked


def test_obb_export_rotates_in_pixel_space_so_it_matches_what_import_reads(client, db, world, monkeypatch):
    """A rotated box on a 100x50 image: the corners must be rotated on the pixel grid (this is what the annotator draws
    and what the OBB import converts back), not in normalized units."""
    import yolo_codec as yc
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (100, 50), (5, 5, 5)).save(buf, "PNG")
    _setup(db, world, ["a"])
    pid = world["pid"]
    box = {"type": "bbox", "label": "a", "x": .3, "y": .3, "w": .3, "h": .15, "rotation": 1.1}
    db.images.insert_one({"id": "obb-wide", "project_id": pid, "filename": "w.png", "storage_path": "p/w", "review_status": "approved",
                          "annotated": True, "created_at": NOW, "annotations": [box]})  # no stored width/height: measured from the bytes
    monkeypatch.setattr(_inprocess.server, "get_object", lambda path: (buf.getvalue(), "image/png"))
    r = client.get(f"/api/projects/{pid}/export?format=yolo_obb", headers=world["owner"])
    assert r.status_code == 200
    line = zipfile.ZipFile(io.BytesIO(r.content)).read("dataset/labels/obb-wide.txt").decode().strip()
    expect = yc.obb_to_corners(.3, .3, .3, .15, 1.1, 100, 50)
    assert line == "0 " + " ".join(f"{x:.6f} {y:.6f}" for x, y in expect)
    back = yc.parse_label_text(line, "obb", (100, 50)).boxes[0]
    assert back["rotation"] == pytest.approx(1.1, abs=1e-4)  # import reads back what export wrote
    db.images.update_one({"id": "obb-wide"}, {"$set": {"width": 100, "height": 50}})  # stored dimensions give the same result
    again = zipfile.ZipFile(io.BytesIO(client.get(f"/api/projects/{pid}/export?format=yolo_obb", headers=world["owner"]).content))
    assert again.read("dataset/labels/obb-wide.txt").decode().strip() == line
