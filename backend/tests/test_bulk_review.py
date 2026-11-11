"""Bulk approve: same rules as single review, applied per image."""
import sys
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


def test_bulk_approve(client, db):
    owner_id, owner = _register(client, "br_owner")
    team = next(t for t in client.get("/api/teams", headers=owner).json() if t["is_personal"])["id"]
    people = {}
    for name, role in (("br_ann", "annotator"), ("br_rev", "reviewer"), ("br_rev2", "reviewer")):
        people[name] = _register(client, name)
        assert client.post(f"/api/teams/{team}/invite", json={"email": f"{name}@example.com", "role": role}, headers=owner).status_code == 200
    pid = client.post("/api/projects", json={"name": "BR", "team_id": team}, headers=owner).json()["id"]
    ann_id, ann = people["br_ann"]
    rev_id, rev = people["br_rev"]
    rev2_id, _ = people["br_rev2"]
    db.images.insert_many([
        {"id": "s1", "project_id": pid, "review_status": "submitted", "assigned_to": ann_id},
        {"id": "s2", "project_id": pid, "review_status": "submitted", "assigned_to": ann_id},
        {"id": "s3", "project_id": pid, "review_status": "submitted", "assigned_to": ann_id, "reviewer_id": rev2_id},
        {"id": "s4", "project_id": pid, "review_status": "submitted", "assigned_to": rev_id},
        {"id": "a1", "project_id": pid, "review_status": "assigned"},
    ])
    url = f"/api/projects/{pid}/images/bulk-review"
    # annotators can't approve
    assert client.post(url, json={"image_ids": ["s1"]}, headers=ann).status_code == 403
    # reviewer: s1, s2 ok; s3 belongs to another reviewer; s4 is their own; a1 not submitted; "zz" doesn't exist
    r = client.post(url, json={"image_ids": ["s1", "s2", "s3", "s4", "a1", "zz"]}, headers=rev)
    assert r.status_code == 200, r.text
    assert r.json() == {"approved": 2, "skipped_not_submitted": 2, "skipped_other_reviewer": 1, "skipped_own": 1}
    assert {d["id"] for d in db.images.find({"review_status": "approved"})} == {"s1", "s2"}
    # owner can approve another reviewer's image
    r = client.post(url, json={"image_ids": ["s3"]}, headers=owner)
    assert r.json()["approved"] == 1
    assert db.activity_log.count_documents({"project_id": pid, "action": "bulk_approved"}) == 2
