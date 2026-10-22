"""approved/rejected -> submitted 'undo review': a reviewer takes back a mis-click and decides again."""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402
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
    w["owner_id"], w["owner"] = _register(client, "ur_owner")
    team = next(t for t in client.get("/api/teams", headers=w["owner"]).json() if t["is_personal"])["id"]
    for name, role in (("ur_ann", "annotator"), ("ur_rev", "reviewer"), ("ur_rev2", "reviewer")):
        w[name + "_id"], w[name] = _register(client, name)
        assert client.post(f"/api/teams/{team}/invite", json={"email": f"{name}@example.com", "role": role}, headers=w["owner"]).status_code == 200
    w["pid"] = client.post("/api/projects", json={"name": "Undo", "team_id": team}, headers=w["owner"]).json()["id"]
    db.projects.update_one({"id": w["pid"]}, {"$set": {"classes": ["a"]}})
    return w


def _image(db, world, iid, status="submitted"):
    db.images.delete_many({"id": iid})
    db.images.insert_one({"id": iid, "project_id": world["pid"], "review_status": status, "assigned_to": world["ur_ann_id"],
                          "annotated": True, "annotations": [BOX], "created_at": NOW})
    return iid


@pytest.mark.parametrize("decision,status", [("approve", "approved"), ("reject", "rejected")])
def test_undo_then_review_again(client, db, world, decision, status):
    iid = _image(db, world, f"ur-{decision}")
    assert client.post(f"/api/images/{iid}/review", json={"decision": decision, "notes": "x"}, headers=world["ur_rev"]).status_code == 200
    assert db.images.find_one({"id": iid})["review_status"] == status
    assert client.post(f"/api/images/{iid}/undo-review", headers=world["ur_rev"]).status_code == 200
    img = db.images.find_one({"id": iid})
    assert img["review_status"] == "submitted"
    assert img["review_notes"] == "" and img["reviewed_by"] is None and img["reviewed_at"] is None
    assert db.activity_log.find_one({"project_id": world["pid"], "action": "image_review_undone"})
    other = "reject" if decision == "approve" else "approve"
    assert client.post(f"/api/images/{iid}/review", json={"decision": other}, headers=world["ur_rev"]).status_code == 200


def test_only_approved_or_rejected(client, db, world):
    iid = _image(db, world, "ur-sub", "submitted")
    assert client.post(f"/api/images/{iid}/undo-review", headers=world["owner"]).status_code == 409


def test_permissions(client, db, world):
    iid = _image(db, world, "ur-perm")
    assert client.post(f"/api/images/{iid}/review", json={"decision": "approve"}, headers=world["ur_rev"]).status_code == 200
    assert client.post(f"/api/images/{iid}/undo-review", headers=world["ur_ann"]).status_code == 403
    assert client.post(f"/api/images/{iid}/undo-review", headers=world["ur_rev2"]).status_code == 403
    assert db.images.find_one({"id": iid})["review_status"] == "approved"
    assert client.post(f"/api/images/{iid}/undo-review", headers=world["owner"]).status_code == 200
