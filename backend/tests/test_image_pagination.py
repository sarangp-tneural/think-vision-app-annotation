"""Paged image list, full id list and counts (no 10,000 cap in the project page)."""
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


def test_pagination_ids_counts(client, db):
    r = client.post("/api/auth/register", json={"email": "pg_owner@example.com", "password": "pw123456", "name": "pg"})
    assert r.status_code == 200, r.text
    h = {"Authorization": f"Bearer {r.json()['token']}"}
    team = next(t for t in client.get("/api/teams", headers=h).json() if t["is_personal"])["id"]
    pid = client.post("/api/projects", json={"name": "PG", "team_id": team}, headers=h).json()["id"]
    db.images.delete_many({})
    db.images.insert_many([
        {"id": f"p{i:04d}", "project_id": pid, "created_at": f"2026-01-01T00:00:{i % 60:02d}", "annotated": i % 2 == 0,
         "review_status": "approved" if i % 3 == 0 else "unassigned", "assigned_to": "u1" if i % 3 == 0 else None}
        for i in range(25)
    ])
    p1 = client.get(f"/api/projects/{pid}/images", params={"limit": 10}, headers=h)
    p2 = client.get(f"/api/projects/{pid}/images", params={"limit": 10, "skip": 10}, headers=h)
    p3 = client.get(f"/api/projects/{pid}/images", params={"limit": 10, "skip": 20}, headers=h)
    assert p1.headers["x-total-count"] == "25"
    ids = [i["id"] for p in (p1, p2, p3) for i in p.json()]
    assert len(ids) == 25 and len(set(ids)) == 25
    all_ids = client.get(f"/api/projects/{pid}/images/ids", headers=h).json()
    assert [i["id"] for i in all_ids] == ids  # same order as the pages
    appr = client.get(f"/api/projects/{pid}/images", params={"status": "approved", "limit": 50}, headers=h)
    assert appr.headers["x-total-count"] == "9"
    un = client.get(f"/api/projects/{pid}/images/ids", params={"assignee": "unassigned"}, headers=h).json()
    assert len(un) == 16
    c = client.get(f"/api/projects/{pid}/images/counts", headers=h).json()
    assert c["total"] == 25 and c["unlabeled"] == 12
    assert c["by_status"] == {"approved": 9, "unassigned": 16}
    assert c["by_assignee"] == {"u1": 9, "unassigned": 16}
