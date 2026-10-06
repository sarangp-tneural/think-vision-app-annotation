"""approved -> rejected 'reopen' transition, and the fingerprint fields set on normal uploads."""
import hashlib
import io
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402
import _inprocess  # noqa: E402
from _inprocess import fresh_db, get_client  # noqa: E402

NOW = datetime.now(timezone.utc).isoformat()
BOX = {"type": "bbox", "label": "a", "x": .1, "y": .1, "w": .2, "h": .2}


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
    w["owner_id"], w["owner"] = _register(client, "ropen_owner")
    team = next(t for t in client.get("/api/teams", headers=w["owner"]).json() if t["is_personal"])["id"]
    for name, role in (("ropen_ann", "annotator"), ("ropen_rev", "reviewer")):
        w[name + "_id"], w[name] = _register(client, name)
        assert client.post(f"/api/teams/{team}/invite", json={"email": f"{name}@example.com", "role": role}, headers=w["owner"]).status_code == 200
    w["pid"] = client.post("/api/projects", json={"name": "Reopen", "team_id": team}, headers=w["owner"]).json()["id"]
    db.projects.update_one({"id": w["pid"]}, {"$set": {"classes": ["a"]}})
    return w


def _approved(db, world, iid="ro-1"):
    db.images.delete_many({"id": iid})
    db.images.insert_one({"id": iid, "project_id": world["pid"], "review_status": "approved", "assigned_to": None, "annotated": True,
                          "annotations": [BOX], "reviewed_by": world["owner_id"], "reviewed_at": NOW, "created_at": NOW})
    return iid


def test_reopen_needs_notes_and_a_reviewer_role_and_an_approved_image(client, db, world):
    iid = _approved(db, world)
    url = f"/api/images/{iid}/reopen"
    assert client.post(url, json={"notes": "wrong box"}, headers=world["ropen_ann"]).status_code == 403
    assert client.post(url, json={"notes": "  "}, headers=world["ropen_rev"]).status_code == 400
    assert db.images.find_one({"id": iid})["review_status"] == "approved"
    assert client.post(url, json={"notes": "wrong box"}, headers=world["ropen_rev"]).status_code == 200
    img = db.images.find_one({"id": iid})
    assert img["review_status"] == "rejected" and img["review_notes"] == "wrong box" and img["reviewed_by"] == world["ropen_rev_id"]
    assert client.post(url, json={"notes": "again"}, headers=world["owner"]).status_code == 409  # no longer approved
    assert db.activity_log.find_one({"project_id": world["pid"], "action": "image_reopened"})


def test_reopened_image_goes_through_the_normal_edit_submit_review_loop(client, db, world):
    iid = _approved(db, world, "ro-2")
    r = client.post(f"/api/images/{iid}/reopen", json={"notes": "fix", "assigned_to": world["ropen_ann_id"]}, headers=world["ropen_rev"])
    assert r.status_code == 200
    # the assignee can edit and submit, exactly like a rejected image; nobody else can
    assert client.put(f"/api/images/{iid}/annotations", json={"boxes": [BOX]}, headers=world["ropen_rev"]).status_code == 403
    assert client.put(f"/api/images/{iid}/annotations", json={"boxes": [{**BOX, "w": .3}]}, headers=world["ropen_ann"]).status_code == 200
    assert client.post(f"/api/images/{iid}/submit", headers=world["ropen_ann"]).status_code == 200
    assert client.post(f"/api/images/{iid}/review", json={"decision": "approve"}, headers=world["ropen_rev"]).status_code == 200
    assert db.images.find_one({"id": iid})["review_status"] == "approved"


def test_reopen_rejects_assigning_to_a_non_member(client, db, world):
    iid = _approved(db, world, "ro-3")
    r = client.post(f"/api/images/{iid}/reopen", json={"notes": "x", "assigned_to": "nobody"}, headers=world["ropen_rev"])
    assert r.status_code == 400 and db.images.find_one({"id": iid})["review_status"] == "approved"


def test_normal_upload_records_hash_dimensions_and_source(client, db, world, monkeypatch):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (12, 5), (1, 2, 3)).save(buf, "PNG")
    data = buf.getvalue()
    monkeypatch.setattr(_inprocess.server, "put_object", lambda path, d, ct: {"path": path, "size": len(d)})
    r = client.post(f"/api/projects/{world['pid']}/images", files={"file": ("x.png", data, "image/png")}, headers=world["owner"])
    assert r.status_code == 200, r.text
    img = db.images.find_one({"id": r.json()["id"]})
    assert img["content_hash"] == hashlib.sha256(data).hexdigest()
    assert (img["width"], img["height"]) == (12, 5) and img["source"] == "upload"
    r = client.post(f"/api/projects/{world['pid']}/images", files={"file": ("y.png", b"not an image", "image/png")}, headers=world["owner"])
    assert r.status_code == 200 and r.json()["width"] is None  # undecodable files are still stored, as before
