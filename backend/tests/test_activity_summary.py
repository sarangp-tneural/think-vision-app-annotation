"""Project activity summary: per-person assigned vs finished, rejection rate, overdue, and who may see whom."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402
from _inprocess import fresh_db, get_client  # noqa: E402

NOW = datetime.now(timezone.utc)
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
    w["owner_id"], w["owner"] = _register(client, "as_owner")
    team = next(t for t in client.get("/api/teams", headers=w["owner"]).json() if t["is_personal"])["id"]
    for name in ("as_a", "as_b"):
        w[name + "_id"], w[name] = _register(client, name)
        assert client.post(f"/api/teams/{team}/invite", json={"email": f"{name}@example.com", "role": "annotator"}, headers=w["owner"]).status_code == 200
    _, w["outsider"] = _register(client, "as_out")
    w["pid"] = client.post("/api/projects", json={"name": "Summary", "team_id": team}, headers=w["owner"]).json()["id"]

    def img(i, uid, status, **extra):
        db.images.insert_one({"id": f"as-{i}", "project_id": w["pid"], "assigned_to": uid, "review_status": status,
                              "annotations": [BOX, BOX], "annotated_at": NOW.isoformat(), "created_at": NOW.isoformat(), **extra})

    past = (NOW - timedelta(days=3)).isoformat()
    sub, rev = NOW - timedelta(hours=10), NOW - timedelta(hours=4)
    # A: 4 assigned -> 2 approved, 1 rejected, 1 still assigned and overdue
    img(1, w["as_a_id"], "approved", submitted_at=sub.isoformat(), reviewed_at=rev.isoformat(), reviewed_by=w["owner_id"])
    img(2, w["as_a_id"], "approved", submitted_at=sub.isoformat(), reviewed_at=rev.isoformat(), reviewed_by=w["owner_id"])
    img(3, w["as_a_id"], "rejected", submitted_at=sub.isoformat(), reviewed_at=rev.isoformat(), reviewed_by=w["owner_id"])
    img(4, w["as_a_id"], "assigned", due_date=past)
    # B: 1 submitted; plus one unassigned image
    img(5, w["as_b_id"], "submitted")
    img(6, None, "unassigned")
    return w


def test_owner_sees_everyone(client, world):
    r = client.get(f"/api/projects/{world['pid']}/activity/summary", headers=world["owner"])
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["scope"] == "team" and d["total"] == 6
    assert d["counts"] == {"unassigned": 1, "assigned": 1, "submitted": 1, "approved": 2, "rejected": 1}
    assert d["completion_pct"] == round(2 / 6 * 100, 1)
    rows = {p["user_id"]: p for p in d["people"]}
    a = rows[world["as_a_id"]]
    assert (a["assigned"], a["approved"], a["rejected"], a["in_progress"], a["overdue"]) == (4, 2, 1, 1, 1)
    assert a["done"] == 2 and a["done_pct"] == 50.0 and a["rejection_rate"] == 33.3
    assert a["avg_review_hours"] == 6.0 and a["boxes"] == 4
    b = rows[world["as_b_id"]]
    assert (b["submitted"], b["done"], b["done_pct"]) == (1, 1, 100.0) and b["rejection_rate"] is None
    rev = next(x for x in d["reviewers"] if x["user_id"] == world["owner_id"])
    assert (rev["approved"], rev["rejected"]) == (2, 1)
    assert len(d["daily"]) == 30


def test_annotator_sees_only_self(client, world):
    d = client.get(f"/api/projects/{world['pid']}/activity/summary", headers=world["as_b"]).json()
    assert d["scope"] == "self"
    assert [p["user_id"] for p in d["people"]] == [world["as_b_id"]]
    assert d["reviewers"] == []


def test_outsider_forbidden(client, world):
    assert client.get(f"/api/projects/{world['pid']}/activity/summary", headers=world["outsider"]).status_code == 403
