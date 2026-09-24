"""Iteration 9 tests: Resend invite, training-plan, active-learning-queue,
train-real validation paths, cancel-on-queued, auto-scale epochs."""
import os
import uuid
import time
import pytest
import requests
from dotenv import load_dotenv

load_dotenv("/app/frontend/.env")
load_dotenv("/app/backend/.env")
BASE_URL = os.environ.get("REACT_APP_BACKEND_URL").rstrip("/") + "/api"

DEMO_EMAIL = os.environ.get("TEST_DEMO_EMAIL", "admin@tneuralai.com")
DEMO_PASSWORD = os.environ.get("TEST_DEMO_PASSWORD", "tneural123")


@pytest.fixture(scope="module")
def token():
    r = requests.post(f"{BASE_URL}/auth/login", json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD})
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    return r.json()["token"]


@pytest.fixture(scope="module")
def hdr(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def team_id(hdr):
    r = requests.get(f"{BASE_URL}/teams", headers=hdr)
    assert r.status_code == 200, r.text
    teams = r.json()
    assert teams, "demo user has no teams"
    return teams[0]["id"]


@pytest.fixture(scope="module")
def project_id(hdr):
    r = requests.get(f"{BASE_URL}/projects", headers=hdr)
    assert r.status_code == 200, r.text
    projects = r.json()
    assert projects, "demo user has no projects"
    return projects[0]["id"]


# ---------- Regression: auth + projects ----------
def test_login_regression():
    r = requests.post(f"{BASE_URL}/auth/login", json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD})
    assert r.status_code == 200
    data = r.json()
    assert "token" in data and isinstance(data["token"], str) and len(data["token"]) > 10


def test_projects_regression(hdr):
    r = requests.get(f"{BASE_URL}/projects", headers=hdr)
    assert r.status_code == 200
    assert isinstance(r.json(), list)


# ---------- Invite (Resend) ----------
def test_invite_unregistered_email_pending(hdr, team_id):
    unique = f"TEST_pending_{uuid.uuid4().hex[:8]}@tneuralai.com"
    r = requests.post(
        f"{BASE_URL}/teams/{team_id}/invite",
        headers=hdr,
        json={"email": unique, "role": "member"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "pending"
    assert data["email"] == unique.lower()
    assert "email_result" in data
    er = data["email_result"]
    # Expected: sent False due to unverified domain, but shape must be present
    assert isinstance(er, dict)
    assert ("sent" in er) or ("skipped" in er)
    if er.get("sent") is False:
        assert "error" in er


def test_invite_existing_user_active(hdr, team_id):
    # Register a new user
    email = f"TEST_active_{uuid.uuid4().hex[:8]}@tneuralai.com"
    pwd = "TestPass123!"
    reg = requests.post(f"{BASE_URL}/auth/register", json={"email": email, "password": pwd, "name": "TestUser"})
    assert reg.status_code == 200, reg.text

    r = requests.post(
        f"{BASE_URL}/teams/{team_id}/invite",
        headers=hdr,
        json={"email": email, "role": "member"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "active"
    assert data["email"] == email.lower()
    assert "email_result" in data

    # verify member listing (via team detail)
    members = requests.get(f"{BASE_URL}/teams/{team_id}", headers=hdr)
    assert members.status_code == 200, members.text
    team_data = members.json()
    active_emails = [m.get("email", "").lower() for m in team_data.get("members", []) if m.get("status") == "active"]
    assert email.lower() in active_emails, f"invited user not active: {team_data.get('members')}"


# ---------- Training plan preview ----------
def test_training_plan_preview(hdr, project_id):
    r = requests.get(f"{BASE_URL}/projects/{project_id}/training-plan", headers=hdr)
    assert r.status_code == 200, r.text
    data = r.json()
    for key in ["eligible_images", "approved", "annotated", "min_required",
                "recommended_epochs", "estimated_seconds_cpu", "ready"]:
        assert key in data, f"missing key {key}: {data}"
    assert data["min_required"] == 30
    assert isinstance(data["ready"], bool)
    if data["eligible_images"] == 0:
        assert data["ready"] is False
        assert data["recommended_epochs"] == 10


# ---------- train-real validation paths ----------
def test_train_real_epochs_out_of_range_negative(hdr, project_id):
    r = requests.post(f"{BASE_URL}/projects/{project_id}/train-real?epochs=-1", headers=hdr)
    assert r.status_code == 400, r.text
    assert "epochs" in r.text.lower()


def test_train_real_epochs_out_of_range_too_high(hdr, project_id):
    r = requests.post(f"{BASE_URL}/projects/{project_id}/train-real?epochs=201", headers=hdr)
    assert r.status_code == 400, r.text
    assert "epochs" in r.text.lower()


def test_train_real_insufficient_images(hdr, project_id):
    # We assume demo project has < 30 labeled images (validated by plan)
    plan = requests.get(f"{BASE_URL}/projects/{project_id}/training-plan", headers=hdr).json()
    if plan["eligible_images"] >= 30:
        pytest.skip(f"Project has {plan['eligible_images']} eligible images; can't test insufficient path")
    r = requests.post(f"{BASE_URL}/projects/{project_id}/train-real?epochs=0", headers=hdr)
    assert r.status_code == 400, r.text
    assert "at least 30" in r.text.lower() or "30 labeled" in r.text.lower()


# ---------- Active learning queue ----------
def test_active_learning_queue_shape(hdr, project_id):
    r = requests.get(f"{BASE_URL}/projects/{project_id}/active-learning-queue?limit=5", headers=hdr)
    assert r.status_code == 200, r.text
    data = r.json()
    for key in ["has_active_model", "total_candidates", "items"]:
        assert key in data
    assert isinstance(data["items"], list)
    assert len(data["items"]) <= 5
    for item in data["items"]:
        for k in ["image_id", "filename", "storage_path", "review_status",
                  "annotation_count", "uncertainty", "reason"]:
            assert k in item, f"missing {k} in item {item}"
        assert 0.0 <= item["uncertainty"] <= 1.0
    # Verify descending sort
    uncs = [i["uncertainty"] for i in data["items"]]
    assert uncs == sorted(uncs, reverse=True), f"not desc sorted: {uncs}"
    # Unlabeled images (annotation_count == 0) should be >=0.85
    for item in data["items"]:
        if item["annotation_count"] == 0:
            assert item["uncertainty"] >= 0.85, f"unlabeled item uncertainty low: {item}"


def test_active_learning_queue_limit_1(hdr, project_id):
    r = requests.get(f"{BASE_URL}/projects/{project_id}/active-learning-queue?limit=1", headers=hdr)
    assert r.status_code == 200
    data = r.json()
    assert len(data["items"]) <= 1


# ---------- Cancel queued model ----------
def _make_project_with_30_dummy_images(hdr, team_id):
    """Create a project and insert dummy image docs directly via API is not possible.
    Instead we test cancel by inserting model doc via minimal path — but train-real
    requires 30 images. We fall back to direct mongo insert of a queued model doc
    on an existing project the user owns."""
    return None


def test_cancel_queued_model(hdr, project_id):
    """Insert a fake queued model doc directly via mongo, then cancel it."""
    # Directly use mongo since we can't easily start a real training
    from pymongo import MongoClient
    mongo_url = os.environ.get("MONGO_URL")
    db_name = os.environ.get("DB_NAME")
    if not mongo_url or not db_name:
        # try loading .env
        try:
            from dotenv import load_dotenv
            load_dotenv("/app/backend/.env")
            mongo_url = os.environ.get("MONGO_URL")
            db_name = os.environ.get("DB_NAME")
        except Exception:
            pass
    assert mongo_url and db_name, "MONGO_URL/DB_NAME missing"
    client = MongoClient(mongo_url)
    db = client[db_name]
    mid = str(uuid.uuid4())
    # Look up demo user id
    user = db.users.find_one({"email": DEMO_EMAIL})
    assert user is not None
    db.models.insert_one({
        "id": mid,
        "project_id": project_id,
        "user_id": user["id"],
        "type": "real",
        "model_arch": "yolov8n",
        "epochs": 10,
        "epochs_requested": 0,
        "epochs_auto_scaled": True,
        "status": "queued",
        "is_active": False,
        "training_image_count": 30,
        "progress": 0,
        "current_epoch": 0,
        "created_at": "2026-01-01T00:00:00+00:00",
    })
    try:
        r = requests.post(f"{BASE_URL}/models/{mid}/cancel", headers=hdr)
        assert r.status_code == 200, r.text
        # Verify status
        m = db.models.find_one({"id": mid})
        assert m["status"] == "cancelled", f"expected cancelled, got {m['status']}"
        assert m.get("cancel_requested") is True
        # Also GET via API
        gm = requests.get(f"{BASE_URL}/models/{mid}", headers=hdr)
        assert gm.status_code == 200
        assert gm.json()["status"] == "cancelled"
    finally:
        db.models.delete_one({"id": mid})
        client.close()
