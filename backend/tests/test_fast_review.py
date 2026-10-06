"""Fast annotate/review flow: save+submit in one request, next-image lookup, background side effects.

Self-contained: runs the app in-process against a throwaway database.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402
from _inprocess import fresh_db, get_client  # noqa: E402

BOX = {"label": "cat", "x": .1, "y": .1, "w": .2, "h": .2}


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
    uid, hdr = _register(client, "solo")
    pid = client.post("/api/projects", json={"name": "Fast"}, headers=hdr).json()["id"]
    for i in range(3):  # created_at descending order is c2, c1, c0
        db.images.insert_one({"id": f"c{i}", "project_id": pid, "annotated": False, "created_at": f"2026-01-01T00:00:0{i}"})
    return {"uid": uid, "hdr": hdr, "pid": pid}


def test_save_and_submit_in_one_request(client, db, world):
    r = client.put("/api/images/c0/annotations", headers=world["hdr"], json={"boxes": [BOX], "submit": True})
    assert r.status_code == 200, r.text
    assert r.json()["submitted"] is True
    img = db.images.find_one({"id": "c0"})
    assert img["review_status"] == "submitted" and img["annotations"][0]["label"] == "cat"
    assert db.activity_log.count_documents({"project_id": world["pid"], "action": "image_submitted"}) == 1  # background task ran


def test_submit_without_boxes_saves_nothing(client, db, world):
    r = client.put("/api/images/c1/annotations", headers=world["hdr"], json={"boxes": [], "submit": True})
    assert r.status_code == 400
    assert db.images.find_one({"id": "c1"}).get("review_status") in (None, "unassigned")


def test_save_only_does_not_submit(client, db, world):
    r = client.put("/api/images/c1/annotations", headers=world["hdr"], json={"boxes": [BOX]})
    assert r.status_code == 200 and "submitted" not in r.json()
    assert db.images.find_one({"id": "c1"})["review_status"] == "assigned"


def test_next_for_annotate_and_review(client, world):
    get = lambda **kw: client.get(f"/api/projects/{world['pid']}/images/next", params=kw, headers=world["hdr"]).json()["id"]
    # c0 is submitted, c1 assigned to me, c2 unassigned (owner may take it); order is c2, c1, c0
    assert get(mode="annotate") == "c2"
    assert get(mode="annotate", after="c2") == "c1"
    assert get(mode="annotate", after="c1") == "c2"  # wraps around
    assert get(mode="review") == "c0"  # solo workspace: may review own work
    assert client.get(f"/api/projects/{world['pid']}/images/next", params={"mode": "x"}, headers=world["hdr"]).status_code == 400


def test_review_then_queue_empty(client, db, world):
    r = client.post("/api/images/c0/review", headers=world["hdr"], json={"decision": "approve", "notes": ""})
    assert r.status_code == 200, r.text
    assert db.images.find_one({"id": "c0"})["review_status"] == "approved"
    assert db.activity_log.count_documents({"project_id": world["pid"], "action": "image_approved"}) == 1
    got = client.get(f"/api/projects/{world['pid']}/images/next", params={"mode": "review"}, headers=world["hdr"]).json()
    assert got["id"] is None


def test_admin_with_empty_review_queue_still_has_images_to_annotate(client, world):
    """Frontend falls through review -> annotate for owners/admins once nothing is waiting for review."""
    h = world["hdr"]
    url = f"/api/projects/{world['pid']}/images/next"
    assert client.get(url, params={"mode": "review", "after": "c0"}, headers=h).json()["id"] is None
    assert client.get(url, params={"mode": "annotate", "after": "c0"}, headers=h).json()["id"] == "c2"
