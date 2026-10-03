"""Rename / merge a class across a whole project."""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402
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
    _, w["owner"] = _register(client, "rowner")
    team = next(t for t in client.get("/api/teams", headers=w["owner"]).json() if t["is_personal"])
    w["pid"] = client.post("/api/projects", json={"name": "Rename", "team_id": team["id"]}, headers=w["owner"]).json()["id"]
    for name, role in (("rann", "annotator"), ("rrev", "reviewer")):
        _, w[name] = _register(client, name)
        assert client.post(f"/api/teams/{team['id']}/invite", json={"email": f"{name}@example.com", "role": role},
                           headers=w["owner"]).status_code == 200
    return w


def _box(label):
    return {"type": "bbox", "label": label, "x": .1, "y": .1, "w": .2, "h": .2}


def _setup(db, world, classes=("object", "dog", "cat")):
    db.projects.update_one({"id": world["pid"]}, {"$set": {"classes": list(classes), "settings.gemini_allowed_classes": ["object", "dog"]}})
    db.images.delete_many({"project_id": world["pid"]})
    for iid, status in (("r-draft", "assigned"), ("r-sub", "submitted"), ("r-app", "approved")):
        db.images.insert_one({"id": iid, "project_id": world["pid"], "review_status": status, "assigned_to": None,
                              "annotated": True, "annotated_by": "someone", "annotated_at": NOW,
                              "annotations": [_box("object"), _box("dog")], "created_at": NOW})


def _rename(client, who, pid, old, new):
    return client.patch(f"/api/projects/{pid}/classes/{old}", json={"new_name": new}, headers=who)


def test_rename_in_place_relabels_every_image_and_keeps_status(client, db, world):
    _setup(db, world)
    r = _rename(client, world["owner"], world["pid"], "object", " Car ")
    assert r.status_code == 200, r.text
    assert r.json() == {"classes": ["car", "dog", "cat"], "images_updated": 3, "merged": False}  # same position, normalised
    for iid, status in (("r-draft", "assigned"), ("r-sub", "submitted"), ("r-app", "approved")):
        img = db.images.find_one({"id": iid})
        assert [a["label"] for a in img["annotations"]] == ["car", "dog"]
        assert img["review_status"] == status and img["annotated_by"] == "someone" and img["annotated_at"] == NOW
    assert db.projects.find_one({"id": world["pid"]})["settings"]["gemini_allowed_classes"] == ["car", "dog"]
    assert db.activity_log.find_one({"project_id": world["pid"], "action": "class_renamed"})["meta"]["to"] == "car"


def test_rename_into_existing_class_merges(client, db, world):
    _setup(db, world)
    r = _rename(client, world["owner"], world["pid"], "object", "dog")
    assert r.status_code == 200 and r.json()["merged"] is True
    assert r.json()["classes"] == ["dog", "cat"]
    assert [a["label"] for a in db.images.find_one({"id": "r-draft"})["annotations"]] == ["dog", "dog"]
    assert db.projects.find_one({"id": world["pid"]})["settings"]["gemini_allowed_classes"] == ["dog"]


def test_rename_validation_and_permissions(client, db, world):
    _setup(db, world)
    pid = world["pid"]
    assert _rename(client, world["owner"], pid, "object", "   ").status_code == 400
    assert _rename(client, world["owner"], pid, "object", "object").status_code == 400
    assert _rename(client, world["owner"], pid, "ghost", "x").status_code == 404
    assert _rename(client, world["rann"], pid, "object", "car").status_code == 403
    assert _rename(client, world["rrev"], pid, "object", "car").status_code == 403
    assert db.projects.find_one({"id": pid})["classes"] == ["object", "dog", "cat"]  # nothing changed
