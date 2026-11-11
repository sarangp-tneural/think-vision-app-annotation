"""Null images: marked empty, submitted, reviewed, and exported/trained as background images."""
import io
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


def _register(client, name):
    r = client.post("/api/auth/register", json={"email": f"{name}@example.com", "password": "pw123456", "name": name})
    assert r.status_code == 200, r.text
    return r.json()["user"]["id"], {"Authorization": f"Bearer {r.json()['token']}"}


def test_null_flow(client, db):
    owner_id, owner = _register(client, "nl_owner")
    team = next(t for t in client.get("/api/teams", headers=owner).json() if t["is_personal"])["id"]
    pid = client.post("/api/projects", json={"name": "NL", "team_id": team}, headers=owner).json()["id"]
    db.projects.update_one({"id": pid}, {"$set": {"classes": ["a"]}})
    box = {"type": "bbox", "label": "a", "x": .1, "y": .1, "w": .2, "h": .2}
    db.images.insert_many([{"id": i, "project_id": pid, "filename": f"{i}.jpg", "annotations": [], "annotated": False,
                            "review_status": "assigned", "assigned_to": owner_id} for i in ("n1", "n2", "n3", "n4")])
    # a null image can't carry boxes
    assert client.put("/api/images/n1/annotations", json={"boxes": [box], "is_null": True}, headers=owner).status_code == 400
    # submitting an empty image without null is still refused
    assert client.put("/api/images/n1/annotations", json={"boxes": [], "submit": True}, headers=owner).status_code == 400
    assert client.post("/api/images/n1/submit", headers=owner).status_code == 400
    # null + submit works
    r = client.put("/api/images/n1/annotations", json={"boxes": [], "is_null": True, "submit": True}, headers=owner)
    assert r.status_code == 200, r.text
    d = db.images.find_one({"id": "n1"})
    assert d["is_null"] and d["annotated"] and d["review_status"] == "submitted"
    # saving boxes clears null
    client.put("/api/images/n2/annotations", json={"boxes": [], "is_null": True}, headers=owner)
    assert db.images.find_one({"id": "n2"})["annotated"] is True
    client.put("/api/images/n2/annotations", json={"boxes": [box]}, headers=owner)
    d = db.images.find_one({"id": "n2"})
    assert d["is_null"] is False and d["annotated"] is True
    client.put("/api/images/n2/annotations", json={"boxes": []}, headers=owner)
    assert db.images.find_one({"id": "n2"})["annotated"] is False
    # bulk: n2 (empty, open) + n3 (open) marked; n4 has boxes -> skipped; n1 already submitted -> locked
    db.images.update_one({"id": "n4"}, {"$set": {"annotations": [box], "annotated": True}})
    r = client.post(f"/api/projects/{pid}/images/bulk-null", json={"image_ids": ["n1", "n2", "n3", "n4", "zz"]}, headers=owner)
    assert r.json() == {"marked": 2, "skipped_has_annotations": 1, "skipped_locked": 2}
    assert {d["id"] for d in db.images.find({"is_null": True, "review_status": "submitted"})} == {"n1", "n2", "n3"}
    # approve one null image: it counts for training and exports with an empty label file
    db.images.update_one({"id": "n1"}, {"$set": {"review_status": "approved"}})
    assert db.images.count_documents({"project_id": pid, "review_status": "approved", **__import__("server").HAS_LABELS}) == 1
    z = zipfile.ZipFile(io.BytesIO(client.get(f"/api/projects/{pid}/export", params={"format": "yolo"}, headers=owner).content))
    assert "dataset/labels/n1.txt" in z.namelist() and z.read("dataset/labels/n1.txt") == b""
