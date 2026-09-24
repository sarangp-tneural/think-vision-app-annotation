"""Iteration 11 tests:
- Router split regression (training-plan, active-learning-queue, train-real, cancel)
- Retrain nudge notifications
- YOLO OBB export format
- Basic regressions (auth/me, teams, projects)
"""
import io
import os
import uuid
import zipfile
from datetime import datetime, timezone

import pytest
import requests
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv("/app/frontend/.env")
load_dotenv("/app/backend/.env")
BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/") + "/api"
MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]

DEMO_EMAIL = os.environ.get("TEST_DEMO_EMAIL", "admin@tneuralai.com")
DEMO_PASSWORD = os.environ.get("TEST_DEMO_PASSWORD", "tneural123")


@pytest.fixture(scope="module")
def db():
    return MongoClient(MONGO_URL)[DB_NAME]


@pytest.fixture(scope="module")
def token():
    r = requests.post(f"{BASE_URL}/auth/login", json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD})
    assert r.status_code == 200, r.text
    return r.json()["token"]


@pytest.fixture(scope="module")
def hdr(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def me(hdr):
    r = requests.get(f"{BASE_URL}/auth/me", headers=hdr)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture(scope="module")
def project_id(hdr):
    r = requests.get(f"{BASE_URL}/projects", headers=hdr)
    assert r.status_code == 200
    projects = r.json()
    assert projects
    return projects[0]["id"]


# ---------- Regressions on unmoved endpoints ----------
def test_auth_me(me):
    assert "id" in me and me["email"] == DEMO_EMAIL


def test_list_teams(hdr):
    r = requests.get(f"{BASE_URL}/teams", headers=hdr)
    assert r.status_code == 200
    assert isinstance(r.json(), list) and len(r.json()) > 0


def test_list_projects(hdr):
    r = requests.get(f"{BASE_URL}/projects", headers=hdr)
    assert r.status_code == 200 and isinstance(r.json(), list)


def test_invite_email_result_present(hdr):
    # Find a team where demo user is owner
    r = requests.get(f"{BASE_URL}/teams", headers=hdr)
    teams = r.json()
    tid = teams[0]["id"]
    email = f"TEST_iter11_{uuid.uuid4().hex[:8]}@tneuralai.com"
    r = requests.post(f"{BASE_URL}/teams/{tid}/invite", headers=hdr,
                      json={"email": email, "role": "member"})
    # 200 with email_result key regardless of send success
    assert r.status_code in (200, 201), r.text
    body = r.json()
    assert "email_result" in body, f"missing email_result: {body}"


# ---------- Router split: training-plan ----------
def test_training_plan_shape(hdr, project_id):
    r = requests.get(f"{BASE_URL}/projects/{project_id}/training-plan", headers=hdr)
    assert r.status_code == 200, r.text
    body = r.json()
    for k in ["eligible_images", "approved", "annotated", "min_required",
              "recommended_epochs", "estimated_seconds_cpu", "ready"]:
        assert k in body, f"missing key {k}"
    assert isinstance(body["ready"], bool)
    assert isinstance(body["recommended_epochs"], int)


# ---------- Router split: active-learning-queue ----------
def test_active_learning_queue_shape(hdr, project_id):
    r = requests.get(f"{BASE_URL}/projects/{project_id}/active-learning-queue?limit=5", headers=hdr)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "has_active_model" in body
    assert "total_candidates" in body
    assert "items" in body and isinstance(body["items"], list)
    assert len(body["items"]) <= 5
    # descending uncertainty
    uncs = [i["uncertainty"] for i in body["items"]]
    assert uncs == sorted(uncs, reverse=True)


# ---------- Router split: train-real validation ----------
def test_train_real_bad_epochs_negative(hdr, project_id):
    r = requests.post(f"{BASE_URL}/projects/{project_id}/train-real?epochs=-1", headers=hdr)
    assert r.status_code == 400


def test_train_real_bad_epochs_too_many(hdr, project_id):
    r = requests.post(f"{BASE_URL}/projects/{project_id}/train-real?epochs=201", headers=hdr)
    assert r.status_code == 400


def test_train_real_insufficient_images(hdr, project_id):
    r = requests.post(f"{BASE_URL}/projects/{project_id}/train-real?epochs=0", headers=hdr)
    assert r.status_code == 400
    assert "at least" in r.text.lower() or "30" in r.text


# ---------- Router split: cancel on queued ----------
def test_cancel_queued_model(hdr, db, project_id, me):
    mid = str(uuid.uuid4())
    db.models.insert_one({
        "id": mid, "project_id": project_id, "user_id": me["id"], "type": "real",
        "model_arch": "yolov8n", "epochs": 10, "status": "queued", "is_active": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    try:
        r = requests.post(f"{BASE_URL}/models/{mid}/cancel", headers=hdr)
        assert r.status_code == 200, r.text
        m = db.models.find_one({"id": mid})
        assert m["status"] == "cancelled", m
    finally:
        db.models.delete_one({"id": mid})


# ---------- Retrain nudge ----------
@pytest.fixture(scope="module")
def nudge_project(hdr, db, me):
    """Create a fresh project for nudge test. Seeded images + a real approved API image."""
    r = requests.post(f"{BASE_URL}/projects", headers=hdr,
                      json={"name": f"TEST_iter11_nudge_{uuid.uuid4().hex[:6]}",
                            "description": "nudge test", "task_type": "object_detection"})
    assert r.status_code in (200, 201), r.text
    pid = r.json()["id"]
    yield pid
    # cleanup
    db.projects.delete_one({"id": pid})
    db.images.delete_many({"project_id": pid})
    db.notifications.delete_many({"project_id": pid})


def _seed_approved_images(db, pid, reviewer_id, count, base_time_iso):
    docs = []
    for i in range(count):
        docs.append({
            "id": str(uuid.uuid4()),
            "project_id": pid,
            "filename": f"TEST_img_{i}.jpg",
            "storage_path": f"nonexistent/{i}.jpg",
            "annotated": True,
            "annotations": [{"type": "bbox", "label": "car", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}],
            "review_status": "approved",
            "reviewed_by": reviewer_id,
            "reviewed_at": base_time_iso,
            "created_at": base_time_iso,
        })
    if docs:
        db.images.insert_many(docs)
    return [d["id"] for d in docs]


def test_retrain_nudge_fires_and_is_idempotent(hdr, db, me, nudge_project):
    pid = nudge_project
    # Baseline notifications
    r = requests.get(f"{BASE_URL}/notifications", headers=hdr)
    assert r.status_code == 200
    baseline = [n for n in r.json().get("items", []) if n.get("project_id") == pid and n.get("kind") in ("retrain_nudge", "training_ready")]
    baseline_count = len(baseline)

    # Seed 30 already-approved images directly in DB
    base_iso = datetime.now(timezone.utc).isoformat()
    _seed_approved_images(db, pid, me["id"], 30, base_iso)

    # Now create one more image via DB (assigned to demo user) then approve it via API
    img_id = str(uuid.uuid4())
    db.images.insert_one({
        "id": img_id,
        "project_id": pid,
        "filename": "TEST_trigger.jpg",
        "storage_path": "nonexistent/trigger.jpg",
        "annotated": True,
        "annotations": [{"type": "bbox", "label": "car", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}],
        "review_status": "assigned",
        "assigned_to": me["id"],
        "created_at": base_iso,
    })
    r = requests.post(f"{BASE_URL}/images/{img_id}/review", headers=hdr,
                      json={"decision": "approve", "notes": ""})
    assert r.status_code == 200, r.text

    # Check notifications
    r = requests.get(f"{BASE_URL}/notifications", headers=hdr)
    assert r.status_code == 200
    after1 = [n for n in r.json().get("items", []) if n.get("project_id") == pid and n.get("kind") in ("retrain_nudge", "training_ready")]
    assert len(after1) > baseline_count, f"Retrain nudge did not fire. baseline={baseline_count}, after={len(after1)}"
    latest = after1[0]
    assert "approved images" in latest.get("title", "").lower()

    # Idempotency: approve one more image → should NOT increase count
    img_id2 = str(uuid.uuid4())
    db.images.insert_one({
        "id": img_id2,
        "project_id": pid,
        "filename": "TEST_trigger2.jpg",
        "storage_path": "nonexistent/trigger2.jpg",
        "annotated": True,
        "annotations": [{"type": "bbox", "label": "car", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}],
        "review_status": "assigned",
        "assigned_to": me["id"],
        "created_at": base_iso,
    })
    r = requests.post(f"{BASE_URL}/images/{img_id2}/review", headers=hdr,
                      json={"decision": "approve", "notes": ""})
    assert r.status_code == 200

    r = requests.get(f"{BASE_URL}/notifications", headers=hdr)
    after2 = [n for n in r.json().get("items", []) if n.get("project_id") == pid and n.get("kind") in ("retrain_nudge", "training_ready")]
    assert len(after2) == len(after1), f"Nudge fired again (not idempotent): {len(after1)} -> {len(after2)}"


# ---------- YOLO OBB export ----------
@pytest.fixture(scope="module")
def obb_project(hdr, db):
    r = requests.post(f"{BASE_URL}/projects", headers=hdr,
                      json={"name": f"TEST_iter11_obb_{uuid.uuid4().hex[:6]}",
                            "description": "obb export test", "task_type": "object_detection"})
    assert r.status_code in (200, 201)
    pid = r.json()["id"]
    # Add class 'car' + a rotated annotation and a non-rotated one
    db.projects.update_one({"id": pid}, {"$set": {"classes": ["car"]}})
    now_iso = datetime.now(timezone.utc).isoformat()
    rotated_img_id = str(uuid.uuid4())
    plain_img_id = str(uuid.uuid4())
    db.images.insert_many([
        {"id": rotated_img_id, "project_id": pid, "filename": "TEST_rot.jpg",
         "storage_path": "nonexistent/rot.jpg", "annotated": True,
         "annotations": [{"type": "bbox", "label": "car", "x": 0.3, "y": 0.3,
                          "w": 0.2, "h": 0.2, "rotation": 0.7854}],
         "review_status": "approved", "created_at": now_iso},
        {"id": plain_img_id, "project_id": pid, "filename": "TEST_plain.jpg",
         "storage_path": "nonexistent/plain.jpg", "annotated": True,
         "annotations": [{"type": "bbox", "label": "car", "x": 0.1, "y": 0.1,
                          "w": 0.2, "h": 0.2}],
         "review_status": "approved", "created_at": now_iso},
    ])
    yield pid, rotated_img_id, plain_img_id
    db.projects.delete_one({"id": pid})
    db.images.delete_many({"project_id": pid})


def test_export_yolo_still_works(hdr, obb_project):
    pid, _, _ = obb_project
    r = requests.get(f"{BASE_URL}/projects/{pid}/export?format=yolo", headers=hdr)
    assert r.status_code == 200
    assert r.headers.get("content-type", "").startswith("application/zip")
    assert "attachment" in r.headers.get("content-disposition", "").lower()


def test_export_coco_still_works(hdr, obb_project):
    pid, _, _ = obb_project
    r = requests.get(f"{BASE_URL}/projects/{pid}/export?format=coco", headers=hdr)
    assert r.status_code == 200
    assert r.headers.get("content-type", "").startswith("application/zip")


def test_export_voc_still_works(hdr, obb_project):
    pid, _, _ = obb_project
    r = requests.get(f"{BASE_URL}/projects/{pid}/export?format=voc", headers=hdr)
    assert r.status_code == 200
    assert r.headers.get("content-type", "").startswith("application/zip")


def test_export_yolo_obb_content(hdr, obb_project):
    pid, rotated_id, plain_id = obb_project
    r = requests.get(f"{BASE_URL}/projects/{pid}/export?format=yolo_obb", headers=hdr)
    assert r.status_code == 200
    assert r.headers.get("content-type", "").startswith("application/zip")
    assert "attachment" in r.headers.get("content-disposition", "").lower()

    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = zf.namelist()
    rot_label = f"dataset/labels/{rotated_id}.txt"
    plain_label = f"dataset/labels/{plain_id}.txt"
    assert rot_label in names, f"missing rotated label: {names}"
    assert plain_label in names, f"missing plain label: {names}"

    rot_content = zf.read(rot_label).decode().strip()
    plain_content = zf.read(plain_label).decode().strip()
    # rotated: 8 corner values → 9 tokens (cls + 8)
    rot_tokens = rot_content.split()
    assert len(rot_tokens) == 9, f"expected 9 tokens for OBB rotated label, got {len(rot_tokens)}: {rot_content}"
    assert rot_tokens[0] == "0"
    # plain: 5 tokens (cls cx cy w h)
    plain_tokens = plain_content.split()
    assert len(plain_tokens) == 5, f"expected 5 tokens for standard bbox, got {len(plain_tokens)}: {plain_content}"
    assert plain_tokens[0] == "0"
