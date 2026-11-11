"""Batched export: parts extract into one dataset folder with unique ids."""
import io
import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402
from _inprocess import fresh_db, get_client  # noqa: E402


@pytest.fixture(scope="module")
def db():
    yield fresh_db()


@pytest.fixture(scope="module")
def client(db):
    return get_client()


@pytest.mark.parametrize("fmt", ["yolo", "coco"])
def test_parts_merge(client, db, fmt):
    r = client.post("/api/auth/register", json={"email": f"eb_{fmt}@example.com", "password": "pw123456", "name": "eb"})
    h = {"Authorization": f"Bearer {r.json()['token']}"}
    team = next(t for t in client.get("/api/teams", headers=h).json() if t["is_personal"])["id"]
    pid = client.post("/api/projects", json={"name": "EB", "team_id": team}, headers=h).json()["id"]
    db.projects.update_one({"id": pid}, {"$set": {"classes": ["a"]}})
    ids = [f"{fmt}{i}" for i in range(5)]
    db.images.insert_many([{"id": i, "project_id": pid, "filename": f"{i}.jpg", "review_status": "approved", "annotated": True,
                            "annotations": [{"type": "bbox", "label": "a", "x": .1, "y": .1, "w": .2, "h": .2}]} for i in ids])
    names, coco_imgs, coco_anns, id_off, ann_off = set(), [], [], 0, 0
    for p, chunk in ((1, ids[:3]), (2, ids[3:])):
        res = client.post(f"/api/projects/{pid}/export", json={"format": fmt, "image_ids": chunk, "part": p, "total_parts": 2,
                                                              "id_offset": id_off, "ann_offset": ann_off}, headers=h)
        assert res.status_code == 200, res.text
        assert f"part{p}of2" in res.headers["content-disposition"]
        id_off += int(res.headers["x-exported-count"]); ann_off += int(res.headers["x-annotation-count"])
        z = zipfile.ZipFile(io.BytesIO(res.content))
        names |= set(z.namelist())
        if fmt == "coco":
            c = json.loads(z.read(f"dataset/annotations_part{p}.json"))
            coco_imgs += c["images"]; coco_anns += c["annotations"]
    assert "dataset/classes.txt" in names
    if fmt == "yolo":
        assert {f"dataset/labels/{i}.txt" for i in ids} <= names
    else:
        assert "dataset/annotations.json" not in names
        assert sorted(i["id"] for i in coco_imgs) == list(range(5))
        assert sorted(a["id"] for a in coco_anns) == list(range(1, 6))
        assert {a["image_id"] for a in coco_anns} == set(range(5))
