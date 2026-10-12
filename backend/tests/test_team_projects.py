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


# --- adding existing projects to a team --------------------------------------

@pytest.fixture(scope="module")
def mv(client, db):
    m = {}
    m["owner_id"], m["owner"] = _register(client, "mv_owner")
    m["mate_id"], m["mate"] = _register(client, "mv_mate")        # will be on the target team
    m["other_id"], m["other"] = _register(client, "mv_other")     # only on the source team
    src = client.post("/api/teams", json={"name": "Source"}, headers=m["owner"]).json()["id"]
    m["team"] = client.post("/api/teams", json={"name": "Target"}, headers=m["owner"]).json()["id"]
    for who in ("mv_mate", "mv_other"):
        assert client.post(f"/api/teams/{src}/invite", json={"email": f"{who}@example.com", "role": "annotator"}, headers=m["owner"]).status_code == 200
    assert client.post(f"/api/teams/{m['team']}/invite", json={"email": "mv_mate@example.com", "role": "annotator"}, headers=m["owner"]).status_code == 200
    mk = lambda name: client.post("/api/projects", json={"name": name, "team_id": src}, headers=m["owner"]).json()["id"]
    m["pid"], m["blocked"], m["in_target"] = mk("Movable"), mk("Blocked"), client.post(
        "/api/projects", json={"name": "Already", "team_id": m["team"]}, headers=m["owner"]).json()["id"]
    img = lambda i, pid, who, st: {"id": i, "project_id": pid, "assigned_to": who, "review_status": st,
                                   "boxes": [{"label": "a"}], "created_at": "2026-01-01", "storage_path": f"x/{i}"}
    db.images.insert_many([
        img("mv_keep", m["pid"], m["mate_id"], "assigned"),     # assignee is on the target team
        img("mv_reset", m["pid"], m["other_id"], "assigned"),   # assignee is not
        img("mv_done", m["pid"], m["other_id"], "approved"),    # finished work stays as is
        img("mv_wait", m["blocked"], m["mate_id"], "submitted"),
    ])
    db.project_assignments.insert_many([
        {"id": "mv_pa1", "project_id": m["pid"], "user_id": m["mate_id"]},
        {"id": "mv_pa2", "project_id": m["pid"], "user_id": m["other_id"]},
    ])
    return m


def _add(client, mv, ids, who="owner", dry=False, tid=None):
    return client.post(f"/api/teams/{tid or mv['team']}/projects/add", json={"project_ids": ids, "dry_run": dry}, headers=mv[who])


def test_dry_run_reports_without_changing_anything(client, db, mv):
    r = _add(client, mv, [mv["pid"], mv["blocked"], mv["in_target"]], dry=True)
    assert r.status_code == 200, r.text
    by = {x["id"]: x for x in r.json()["results"]}
    assert (by[mv["pid"]]["status"], by[mv["pid"]]["unassigned_images"], by[mv["pid"]]["removed_assignments"]) == ("ok", 1, 1)
    assert by[mv["blocked"]]["status"] == "blocked"
    assert by[mv["in_target"]]["status"] == "skipped"
    assert db.projects.find_one({"id": mv["pid"]})["team_id"] != mv["team"]
    assert db.images.find_one({"id": "mv_reset"})["assigned_to"] == mv["other_id"]


def test_only_admins_of_the_target_team_can_add(client, mv):
    assert _add(client, mv, [mv["pid"]], who="mate").status_code == 403


def test_move_keeps_work_and_cleans_up_people(client, db, mv):
    r = _add(client, mv, [mv["pid"], mv["blocked"]])
    by = {x["id"]: x["status"] for x in r.json()["results"]}
    assert by == {mv["pid"]: "moved", mv["blocked"]: "blocked"}
    assert db.projects.find_one({"id": mv["pid"]})["team_id"] == mv["team"]
    assert db.projects.find_one({"id": mv["blocked"]})["team_id"] != mv["team"]
    keep, reset, done = (db.images.find_one({"id": i}) for i in ("mv_keep", "mv_reset", "mv_done"))
    assert keep["assigned_to"] == mv["mate_id"] and keep["review_status"] == "assigned"
    assert reset["assigned_to"] is None and reset["review_status"] == "unassigned" and reset["boxes"]
    assert done["assigned_to"] == mv["other_id"] and done["review_status"] == "approved"
    assert {a["user_id"] for a in db.project_assignments.find({"project_id": mv["pid"]})} == {mv["mate_id"]}
    team = client.get(f"/api/teams/{mv['team']}", headers=mv["owner"]).json()
    assert mv["pid"] in {p["id"] for p in team["projects"]}


def test_not_admin_of_the_projects_current_team_is_forbidden(client, mv):
    # mate is an annotator on the source team, so cannot move its project; own a team to try it from
    own = client.post("/api/teams", json={"name": "Mate's"}, headers=mv["mate"]).json()["id"]
    r = _add(client, mv, [mv["blocked"]], who="mate", tid=own)
    assert r.json()["results"][0]["status"] == "forbidden"
