"""Annotators only see images assigned to them; owners and reviewers still see everything."""
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


@pytest.fixture(scope="module")
def w(client, db):
    w = {}
    w["owner_id"], w["owner"] = _register(client, "vis_owner")
    team = next(t for t in client.get("/api/teams", headers=w["owner"]).json() if t["is_personal"])["id"]
    for name, role in (("vis_ann", "annotator"), ("vis_rev", "reviewer")):
        w[name + "_id"], w[name] = _register(client, name)
        assert client.post(f"/api/teams/{team}/invite", json={"email": f"{name}@example.com", "role": role}, headers=w["owner"]).status_code == 200
    w["pid"] = client.post("/api/projects", json={"name": "Vis", "team_id": team}, headers=w["owner"]).json()["id"]
    db.images.insert_many([
        {"id": "mine", "project_id": w["pid"], "assigned_to": w["vis_ann_id"], "review_status": "assigned", "created_at": "2026-01-01T00:00:02", "storage_path": "x/mine"},
        {"id": "other", "project_id": w["pid"], "assigned_to": w["owner_id"], "review_status": "assigned", "created_at": "2026-01-01T00:00:01", "storage_path": "x/other"},
        {"id": "free", "project_id": w["pid"], "assigned_to": None, "review_status": "unassigned", "created_at": "2026-01-01T00:00:00", "storage_path": "x/free"},
    ])
    return w


def _ids(client, w, who, **params):
    r = client.get(f"/api/projects/{w['pid']}/images", params=params, headers=w[who])
    assert r.status_code == 200, r.text
    return {i["id"] for i in r.json()}


def test_annotator_list_only_has_own_images_even_if_asking_for_others(client, w):
    assert _ids(client, w, "vis_ann") == {"mine"}
    assert _ids(client, w, "vis_ann", assignee=w["owner_id"]) == {"mine"}


def test_owner_and_reviewer_still_see_everything(client, w):
    assert _ids(client, w, "owner") == {"mine", "other", "free"}
    assert _ids(client, w, "vis_rev") == {"mine", "other", "free"}


def test_annotator_cannot_open_images_that_are_not_theirs(client, w):
    ann = w["vis_ann"]
    assert client.get("/api/images/mine", headers=ann).status_code == 200
    for iid in ("other", "free"):
        assert client.get(f"/api/images/{iid}", headers=ann).status_code == 404
        assert client.get(f"/api/images/{iid}/comments", headers=ann).status_code == 404
        assert client.post(f"/api/images/{iid}/comments", json={"text": "hi"}, headers=ann).status_code == 404
    assert client.get("/api/files/x/other", headers=ann).status_code == 404
    assert client.get("/api/images/other", headers=w["vis_rev"]).status_code == 200


def test_annotator_queue_is_empty_after_submitting_last_image_but_list_keeps_it(client, db, w):
    r = client.put("/api/images/mine/annotations", headers=w["vis_ann"],
                   json={"boxes": [{"label": "a", "x": .1, "y": .1, "w": .2, "h": .2}], "submit": True})
    assert r.status_code == 200, r.text
    nxt = client.get(f"/api/projects/{w['pid']}/images/next", params={"mode": "annotate", "after": "mine"}, headers=w["vis_ann"])
    assert nxt.json()["id"] is None  # frontend then falls back to the visible list
    assert _ids(client, w, "vis_ann") == {"mine"}
