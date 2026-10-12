"""A team lists its own projects, and images in a team project can be assigned to the team's members."""
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
    w["owner_id"], w["owner"] = _register(client, "tp_owner")
    w["mate_id"], w["mate"] = _register(client, "tp_mate")
    w["team"] = client.post("/api/teams", json={"name": "Crew"}, headers=w["owner"]).json()["id"]
    assert client.post(f"/api/teams/{w['team']}/invite", json={"email": "tp_mate@example.com", "role": "annotator"}, headers=w["owner"]).status_code == 200
    w["pid"] = client.post("/api/projects", json={"name": "In team", "team_id": w["team"]}, headers=w["owner"]).json()["id"]
    w["other_pid"] = client.post("/api/projects", json={"name": "Personal"}, headers=w["owner"]).json()["id"]
    db.images.insert_many([
        {"id": "tp_img", "project_id": w["pid"], "assigned_to": None, "review_status": "unassigned", "created_at": "2026-01-01", "storage_path": "x/a"},
        {"id": "tp_img2", "project_id": w["pid"], "assigned_to": None, "review_status": "unassigned", "created_at": "2026-01-01", "storage_path": "x/b"},
        {"id": "tp_other", "project_id": w["other_pid"], "assigned_to": None, "review_status": "unassigned", "created_at": "2026-01-01", "storage_path": "x/c"},
    ])
    return w


def test_team_detail_lists_only_the_teams_projects(client, w):
    r = client.get(f"/api/teams/{w['team']}", headers=w["owner"])
    assert r.status_code == 200, r.text
    projects = r.json()["projects"]
    assert [(p["id"], p["image_count"]) for p in projects] == [(w["pid"], 2)]


def test_team_member_can_be_assigned_images_in_a_team_project(client, w):
    ok = client.post("/api/images/tp_img/assign", json={"user_id": w["mate_id"]}, headers=w["owner"])
    assert ok.status_code == 200, ok.text


def test_team_member_cannot_be_assigned_images_in_a_project_outside_the_team(client, w):
    r = client.post("/api/images/tp_other/assign", json={"user_id": w["mate_id"]}, headers=w["owner"])
    assert r.status_code == 400
