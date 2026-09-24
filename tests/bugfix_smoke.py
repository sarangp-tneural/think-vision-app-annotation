"""
Iteration 8 bug-fix smoke:
- MongoDB startup indexes ensured (log check)
- Invite auto-activation via get_current_user for existing user with pending invite
- Cancel training endpoint: queued->cancelled; auth (403 for member); 400 for terminal status
- Regression: /api/auth/me still works
"""
import os
import time
import uuid
import requests
import pytest
from pymongo import MongoClient

BASE = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
API = f"{BASE}/api"
MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.environ.get("DB_NAME", "test_database")


def _reg(email, name, pw="pass1234"):
    r = requests.post(f"{API}/auth/register", json={"email": email, "name": name, "password": pw})
    if r.status_code == 400:
        # already exists -> login
        r = requests.post(f"{API}/auth/login", json={"email": email, "password": pw})
    assert r.status_code == 200, r.text
    return r.json()["token"], r.json()["user"]


def _h(tok):
    return {"Authorization": f"Bearer {tok}"}


def test_indexes_log_present():
    # Log file may have rolled; if not present, skip softly
    found = False
    for p in ["/var/log/supervisor/backend.err.log", "/var/log/supervisor/backend.out.log"]:
        try:
            with open(p) as f:
                if "MongoDB indexes ensured" in f.read():
                    found = True
                    break
        except Exception:
            pass
    assert found, "MongoDB indexes ensured log not found"


def test_auth_me_regression():
    tok, u = _reg(f"reg_{uuid.uuid4().hex[:6]}@t.io", "Reg User")
    r = requests.get(f"{API}/auth/me", headers=_h(tok))
    assert r.status_code == 200
    assert r.json()["id"] == u["id"]


def test_invite_auto_activation_pending_flow():
    """Invite by email BEFORE userB registers, then userB registers/logs in and
    hits an authenticated endpoint. Membership should auto-activate."""
    ownerA_email = f"ownerA_{uuid.uuid4().hex[:6]}@t.io"
    userB_email = f"userB_{uuid.uuid4().hex[:6]}@t.io"

    tokA, uA = _reg(ownerA_email, "Owner A")

    # Create team
    r = requests.post(f"{API}/teams", json={"name": "Team A"}, headers=_h(tokA))
    assert r.status_code == 200, r.text
    tid = r.json()["id"]

    # Invite userB (does not exist yet) -> pending
    r = requests.post(f"{API}/teams/{tid}/invite",
                      json={"email": userB_email, "role": "annotator"},
                      headers=_h(tokA))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pending"

    # userB registers, obtains token
    tokB, uB = _reg(userB_email, "User B")

    # register itself calls _activate_pending_invites. Verify owner can see B as active
    r = requests.get(f"{API}/teams/{tid}", headers=_h(tokA))
    assert r.status_code == 200
    members = r.json()["members"]
    b_member = next((m for m in members if m.get("user_id") == uB["id"]), None)
    assert b_member is not None, f"User B not found in team members: {members}"
    assert b_member["status"] == "active"
    assert b_member["role"] == "annotator"


def test_invite_auto_activation_via_get_current_user():
    """The KEY fix: userB is already logged in (has token) BEFORE invite is created.
    After invite is issued, userB's next authenticated request should activate it
    via get_current_user calling _activate_pending_invites."""
    ownerA_email = f"ownerA2_{uuid.uuid4().hex[:6]}@t.io"
    userB_email = f"userB2_{uuid.uuid4().hex[:6]}@t.io"

    tokA, uA = _reg(ownerA_email, "Owner A2")
    tokB, uB = _reg(userB_email, "User B2")  # userB exists & has token BEFORE invite

    # Owner creates a NEW team (userB not yet a member)
    r = requests.post(f"{API}/teams", json={"name": "Team A2"}, headers=_h(tokA))
    assert r.status_code == 200
    tid = r.json()["id"]

    # Manually insert a PENDING invite by email (simulating an invite where userB's
    # user record wasn't linked). This is the scenario the fix targets.
    # The normal invite endpoint would have found userB and added as active, so we
    # directly inject a pending invite to test get_current_user auto-activation.
    mc = MongoClient(MONGO_URL)
    coll = mc[DB_NAME].team_members
    pending_id = str(uuid.uuid4())
    coll.insert_one({
        "id": pending_id,
        "team_id": tid,
        "invited_email": userB_email.lower(),
        "role": "annotator",
        "status": "pending",
        "invited_at": "2026-01-01T00:00:00+00:00",
    })

    # userB makes an authenticated request WITHOUT re-login
    r = requests.get(f"{API}/teams", headers=_h(tokB))
    assert r.status_code == 200, r.text

    # Now owner should see B as active
    r = requests.get(f"{API}/teams/{tid}", headers=_h(tokA))
    assert r.status_code == 200
    members = r.json()["members"]
    b = next((m for m in members if m.get("user_id") == uB["id"]), None)
    assert b is not None, f"userB not activated. members={members}"
    assert b["status"] == "active"
    assert b["role"] == "annotator"


# ---------------- Cancel training endpoint ----------------

def _make_project(tok):
    r = requests.post(f"{API}/projects",
                      json={"name": f"P {uuid.uuid4().hex[:4]}", "task_type": "detection", "classes": ["a"]},
                      headers=_h(tok))
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _insert_fake_model(project_id, status="queued"):
    mc = MongoClient(MONGO_URL)
    mid = str(uuid.uuid4())
    mc[DB_NAME].models.insert_one({
        "id": mid,
        "project_id": project_id,
        "status": status,
        "created_at": "2026-01-01T00:00:00+00:00",
    })
    return mid


def test_cancel_queued_model():
    tok, _ = _reg(f"own_{uuid.uuid4().hex[:6]}@t.io", "Owner")
    pid = _make_project(tok)
    mid = _insert_fake_model(pid, status="queued")

    r = requests.post(f"{API}/models/{mid}/cancel", headers=_h(tok))
    assert r.status_code == 200, r.text

    # verify status is cancelled + cancel_requested True
    mc = MongoClient(MONGO_URL)
    doc = mc[DB_NAME].models.find_one({"id": mid})
    assert doc["status"] == "cancelled"
    assert doc.get("cancel_requested") is True


def test_cancel_training_status_sets_flag_only():
    tok, _ = _reg(f"own2_{uuid.uuid4().hex[:6]}@t.io", "Owner")
    pid = _make_project(tok)
    mid = _insert_fake_model(pid, status="training")

    r = requests.post(f"{API}/models/{mid}/cancel", headers=_h(tok))
    assert r.status_code == 200

    mc = MongoClient(MONGO_URL)
    doc = mc[DB_NAME].models.find_one({"id": mid})
    # Since no real thread is running, status stays 'training' but flag is set
    assert doc.get("cancel_requested") is True
    assert doc["status"] == "training"


def test_cancel_terminal_status_400():
    tok, _ = _reg(f"own3_{uuid.uuid4().hex[:6]}@t.io", "Owner")
    pid = _make_project(tok)
    for term in ["trained", "cancelled", "failed"]:
        mid = _insert_fake_model(pid, status=term)
        r = requests.post(f"{API}/models/{mid}/cancel", headers=_h(tok))
        assert r.status_code == 400, f"expected 400 for {term}, got {r.status_code}"


def test_cancel_not_found_404():
    tok, _ = _reg(f"own4_{uuid.uuid4().hex[:6]}@t.io", "Owner")
    r = requests.post(f"{API}/models/{uuid.uuid4()}/cancel", headers=_h(tok))
    assert r.status_code == 404


def test_cancel_forbidden_for_non_owner():
    # Owner creates project + fake model. Then invites userB as 'annotator'.
    # userB should get 403.
    tokA, uA = _reg(f"ownX_{uuid.uuid4().hex[:6]}@t.io", "Owner X")
    userB_email = f"annB_{uuid.uuid4().hex[:6]}@t.io"
    tokB, uB = _reg(userB_email, "Ann B")

    pid = _make_project(tokA)
    mid = _insert_fake_model(pid, status="queued")

    # get owner's project team_id
    r = requests.get(f"{API}/projects/{pid}", headers=_h(tokA))
    assert r.status_code == 200
    tid = r.json().get("team_id")
    assert tid

    # invite userB as annotator (userB exists so becomes active immediately)
    r = requests.post(f"{API}/teams/{tid}/invite",
                      json={"email": userB_email, "role": "annotator"},
                      headers=_h(tokA))
    assert r.status_code == 200, r.text

    r = requests.post(f"{API}/models/{mid}/cancel", headers=_h(tokB))
    assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text}"


# --------------- Regression basics ---------------

def test_projects_list_regression():
    tok, _ = _reg("demo@vf.io", "Demo User", pw="demo1234")
    r = requests.get(f"{API}/projects", headers=_h(tok))
    assert r.status_code == 200
    assert isinstance(r.json(), list)
