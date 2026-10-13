"""submitted -> assigned 'withdraw': an annotator pulls a submission back to fix it, then resubmits."""
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
    w["owner_id"], w["owner"] = _register(client, "wd_owner")
    team = next(t for t in client.get("/api/teams", headers=w["owner"]).json() if t["is_personal"])["id"]
    for name, role in (("wd_ann", "annotator"), ("wd_ann2", "annotator"), ("wd_rev", "reviewer")):
        w[name + "_id"], w[name] = _register(client, name)
        assert client.post(f"/api/teams/{team}/invite", json={"email": f"{name}@example.com", "role": role}, headers=w["owner"]).status_code == 200
    w["pid"] = client.post("/api/projects", json={"name": "Withdraw", "team_id": team}, headers=w["owner"]).json()["id"]
    db.projects.update_one({"id": w["pid"]}, {"$set": {"classes": ["a", "b"]}})
    return w


def _image(db, world, iid, status):
    db.images.delete_many({"id": iid})
    db.images.insert_one({"id": iid, "project_id": world["pid"], "review_status": status, "assigned_to": world["wd_ann_id"],
                          "annotated": True, "annotations": [BOX], "created_at": NOW})
    return iid


def test_assignee_withdraws_edits_and_resubmits(client, db, world):
    iid = _image(db, world, "wd-1", "submitted")
    assert client.put(f"/api/images/{iid}/annotations", json={"boxes": [BOX]}, headers=world["wd_ann"]).status_code == 409
    assert client.post(f"/api/images/{iid}/withdraw", headers=world["wd_ann"]).status_code == 200
    assert db.images.find_one({"id": iid})["review_status"] == "assigned"
    assert client.put(f"/api/images/{iid}/annotations", json={"boxes": [{**BOX, "label": "b"}]}, headers=world["wd_ann"]).status_code == 200
    assert client.post(f"/api/images/{iid}/submit", headers=world["wd_ann"]).status_code == 200
    assert db.images.find_one({"id": iid})["review_status"] == "submitted"
    assert db.activity_log.find_one({"project_id": world["pid"], "action": "image_withdrawn"})


def test_only_the_assignee_or_a_manager_can_withdraw(client, db, world):
    iid = _image(db, world, "wd-2", "submitted")
    assert client.post(f"/api/images/{iid}/withdraw", headers=world["wd_ann2"]).status_code == 403
    assert client.post(f"/api/images/{iid}/withdraw", headers=world["wd_rev"]).status_code == 403
    assert db.images.find_one({"id": iid})["review_status"] == "submitted"
    assert client.post(f"/api/images/{iid}/withdraw", headers=world["owner"]).status_code == 200


def test_cannot_withdraw_once_reviewed_or_when_not_submitted(client, db, world):
    for status in ("approved", "rejected", "assigned"):
        iid = _image(db, world, f"wd-{status}", status)
        assert client.post(f"/api/images/{iid}/withdraw", headers=world["wd_ann"]).status_code == 409
        assert db.images.find_one({"id": iid})["review_status"] == status
