"""Bulk delete of images: roles, approved handling, project scoping, comment cleanup."""
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


@pytest.fixture()
def w(client, db):
    if not hasattr(w, "base"):
        base = {}
        base["owner_id"], base["owner"] = _register(client, "bd_owner")
        team = next(t for t in client.get("/api/teams", headers=base["owner"]).json() if t["is_personal"])["id"]
        for name, role in (("bd_ann", "annotator"), ("bd_rev", "reviewer")):
            base[name + "_id"], base[name] = _register(client, name)
            assert client.post(f"/api/teams/{team}/invite", json={"email": f"{name}@example.com", "role": role}, headers=base["owner"]).status_code == 200
        base["pid"] = client.post("/api/projects", json={"name": "BD", "team_id": team}, headers=base["owner"]).json()["id"]
        base["other"] = client.post("/api/projects", json={"name": "BD2", "team_id": team}, headers=base["owner"]).json()["id"]
        w.base = base
    b = w.base
    db.images.delete_many({}); db.comments.delete_many({})
    db.images.insert_many([
        {"id": "i1", "project_id": b["pid"], "review_status": "assigned"},
        {"id": "i2", "project_id": b["pid"], "review_status": "submitted"},
        {"id": "i3", "project_id": b["pid"], "review_status": "approved"},
        {"id": "x1", "project_id": b["other"], "review_status": "assigned"},
    ])
    db.comments.insert_one({"id": "c1", "image_id": "i1", "text": "t"})
    return b


def _bulk(client, w, who, ids, **kw):
    return client.post(f"/api/projects/{w['pid']}/images/bulk-delete", json={"image_ids": ids, **kw}, headers=w[who])


def test_owner_deletes_selected_and_skips_approved_by_default(client, db, w):
    r = _bulk(client, w, "owner", ["i1", "i2", "i3"])
    assert r.status_code == 200, r.text
    assert r.json() == {"deleted": 2, "skipped_approved": 1}
    assert {i["id"] for i in db.images.find({})} == {"i3", "x1"}
    assert db.comments.count_documents({}) == 0
    assert db.activity_log.count_documents({"action": "images_deleted"}) >= 1


def test_include_approved_deletes_them_too(client, db, w):
    r = _bulk(client, w, "owner", ["i3"], include_approved=True)
    assert r.json() == {"deleted": 1, "skipped_approved": 0}
    assert db.images.count_documents({"id": "i3"}) == 0


def test_other_project_images_are_ignored(client, db, w):
    assert _bulk(client, w, "owner", ["x1"]).json()["deleted"] == 0
    assert db.images.count_documents({"id": "x1"}) == 1


def test_only_owner_or_admin_may_delete(client, db, w):
    for who in ("bd_ann", "bd_rev"):
        assert _bulk(client, w, who, ["i1"]).status_code == 403
    assert db.images.count_documents({"id": "i1"}) == 1


def test_single_delete_needs_force_for_approved(client, db, w):
    assert client.delete("/api/images/i3", headers=w["owner"]).status_code == 409
    assert client.delete("/api/images/i3?force=true", headers=w["owner"]).status_code == 200
    assert client.delete("/api/images/i1", headers=w["owner"]).status_code == 200
    assert db.comments.count_documents({"image_id": "i1"}) == 0
