"""Manager dashboard: role gating, work-log import, and merged frame counts.

Self-contained: runs the app in-process against a throwaway database (thinkvision_test),
which is dropped at the end. Does not need a running backend.
"""
import os
import sys
import uuid
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bcrypt  # noqa: E402
import pytest  # noqa: E402
from _inprocess import fresh_db, get_client  # noqa: E402

TODAY = date.today().isoformat()
YESTERDAY = (date.today() - timedelta(days=1)).isoformat()


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
    alice_id, alice = _register(client, "alice")
    bob_id, bob = _register(client, "bob")
    db.users.insert_one({
        "id": "mgr-1", "role": "manager", "email": "boss@tneuralai.com", "name": "Boss",
        "password": bcrypt.hashpw(b"managerpw1", bcrypt.gensalt()).decode(), "created_at": TODAY,
    })
    token = client.post("/api/auth/manager-login", json={"email": "Boss@TNeuralAI.com", "password": "managerpw1"}).json()["token"]
    pid = client.post("/api/projects", json={"name": "MyG"}, headers=alice).json()["id"]
    for i in range(3):  # alice annotated 3 frames today, bob 2 yesterday
        db.images.insert_one({"id": f"a{i}", "project_id": pid, "annotated": True, "annotated_by": alice_id,
                              "annotated_at": f"{TODAY}T10:00:00+00:00", "review_status": "approved",
                              "reviewed_at": f"{TODAY}T12:00:00+00:00"})
    for i in range(2):
        db.images.insert_one({"id": f"b{i}", "project_id": pid, "annotated": True, "annotated_by": bob_id,
                              "annotated_at": f"{YESTERDAY}T10:00:00+00:00", "review_status": "rejected",
                              "reviewed_at": f"{TODAY}T12:00:00+00:00"})
    return {"alice_id": alice_id, "bob_id": bob_id, "alice": alice, "bob": bob, "pid": pid,
            "mgr": {"Authorization": f"Bearer {token}"}}


def test_manager_endpoints_require_manager_role(client, world):
    assert client.get("/api/manager/overview").status_code == 401
    assert client.get("/api/manager/overview", headers=world["alice"]).status_code == 403


def test_manager_login(client, db, world):
    login = lambda e, p: client.post("/api/auth/manager-login", json={"email": e, "password": p})
    assert login("boss@tneuralai.com", "wrong").status_code == 400
    # regular users cannot sign in through the manager endpoint
    assert login("alice@example.com", "pw123456").status_code == 400
    # a manager row on a non-company domain is rejected even with the right password
    db.users.insert_one({"id": "mgr-2", "role": "manager", "email": "x@gmail.com", "name": "X",
                         "password": bcrypt.hashpw(b"managerpw1", bcrypt.gensalt()).decode()})
    assert login("x@gmail.com", "managerpw1").status_code == 400
    assert client.get("/api/auth/me", headers=world["mgr"]).json()["role"] == "manager"


def test_create_manager_script_enforces_domain():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from create_manager import validate_manager_email
    assert validate_manager_email(" Anu.A@TNeuralAI.com ", "tneuralai.com") == "anu.a@tneuralai.com"
    for bad in ("x@gmail.com", "x@tneuralai.com.evil.io", "x@sub.tneuralai.com", "@tneuralai.com", "tneuralai.com"):
        with pytest.raises(ValueError):
            validate_manager_email(bad, "tneuralai.com")


def test_annotation_save_records_author(client, db, world):
    iid = f"img-{uuid.uuid4()}"
    db.images.insert_one({"id": iid, "project_id": world["pid"], "annotated": False})
    r = client.put(f"/api/images/{iid}/annotations", headers=world["alice"],
                   json={"boxes": [{"label": "cat", "x": .1, "y": .1, "w": .2, "h": .2}]})
    assert r.status_code == 200, r.text
    img = db.images.find_one({"id": iid})
    assert img["annotated_by"] == world["alice_id"] and img["annotated_at"]


def test_overview_counts_app_tracked_frames(client, world):
    r = client.get(f"/api/manager/overview?from={YESTERDAY}&to={TODAY}", headers=world["mgr"])
    assert r.status_code == 200
    k = r.json()["kpis"]
    assert k["tracked_frames"] >= 5 and k["active_people"] >= 2
    assert k["approved"] >= 3 and k["rejected"] >= 2


def test_work_log_import_dedupe_and_merge(client, db, world):
    csv_text = (
        "Date,Day,Product,Project,Task,Number of Frames Annotated Today,Verified by,Notes (if any)\n"
        f"{date.today().day}/{date.today().month}/{date.today().year},x,Sakshi,MyG,Data annotation,250,Anjana,\n"
        f"{date.today().day}/{date.today().month}/{date.today().year},x,Sakshi,Veranda,Data annotation,40,Anjana,\n"
        f"{date.today().day}/{date.today().month}/{date.today().year},,,,,,,\n"  # spacer row
        f"{date.today().day}/{date.today().month}/{date.today().year},x,Sakshi,Veranda,Data annotation,40,Anjana,\n"
    ).encode()
    files = {"file": ("log.csv", csv_text, "text/csv")}
    r = client.post("/api/work-logs/import", files=files, headers=world["alice"])
    assert r.json() == {"imported": 3, "skipped_duplicates": 0, "errors": []}  # identical rows in one file are kept
    r = client.post("/api/work-logs/import", files={"file": ("log.csv", csv_text, "text/csv")}, headers=world["alice"])
    assert r.json()["imported"] == 0 and r.json()["skipped_duplicates"] == 3  # re-import is idempotent

    r = client.get(f"/api/manager/people/{world['alice_id']}?from={TODAY}&to={TODAY}", headers=world["mgr"])
    by_project = {p["project"]: p["frames"] for p in r.json()["by_project"]}
    # MyG: alice already has app-tracked frames today, so the logged 250 must NOT be added
    assert by_project["MyG"] == 4  # 3 seeded + 1 saved in test_annotation_save_records_author
    assert by_project["Veranda"] == 80  # log-only project, both identical rows counted


def test_bad_date_rows_reported(client, world):
    bad = b"Date,Project,Number of Frames Annotated Today\nnot-a-date,MyG,5\n"
    r = client.post("/api/work-logs/import", files={"file": ("l.csv", bad, "text/csv")}, headers=world["bob"])
    assert r.json()["imported"] == 0 and len(r.json()["errors"]) == 1


def test_people_and_projects_views(client, world):
    people = client.get(f"/api/manager/people?from={YESTERDAY}&to={TODAY}", headers=world["mgr"]).json()["people"]
    names = [p["name"] for p in people]
    assert "alice" in names and "bob" in names and "Boss" not in names
    projects = client.get(f"/api/manager/projects?from={YESTERDAY}&to={TODAY}", headers=world["mgr"]).json()["projects"]
    assert {"MyG", "Veranda"} <= {p["name"] for p in projects}
    detail = client.get(f"/api/manager/projects/{world['pid']}?from={YESTERDAY}&to={TODAY}", headers=world["mgr"])
    assert detail.status_code == 200 and detail.json()["contributors"]


def test_csv_export_neutralises_formulas(client, world):
    client.post("/api/work-logs", headers=world["bob"],
                json={"date": TODAY, "project_name": "=HYPERLINK(1)", "frames": 1})
    text = client.get(f"/api/manager/export.csv?from={TODAY}&to={TODAY}", headers=world["mgr"]).text
    assert "'=HYPERLINK(1)" in text


def test_range_validation(client, world):
    assert client.get("/api/manager/overview?from=2026-02-01&to=2026-01-01", headers=world["mgr"]).status_code == 400
    assert client.get("/api/manager/overview?from=abc", headers=world["mgr"]).status_code == 400


def test_normal_login_returns_role_so_managers_reach_their_dashboard(client, world):
    r = client.post("/api/auth/login", json={"email": "boss@tneuralai.com", "password": "managerpw1"})
    assert r.status_code == 200 and r.json()["user"]["role"] == "manager"
    r = client.post("/api/auth/login", json={"email": "alice@example.com", "password": "pw123456"})
    assert r.json()["user"]["role"] == "user"
