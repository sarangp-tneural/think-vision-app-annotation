"""Deploy Pipeline (M0) tests: pipeline run creation and the stage
concurrency guard.

Live-HTTP integration tests, following the same idiom as
test_iteration9_new_endpoints.py / test_iteration11_routers_nudge_obb.py: real
requests against a running backend, real MongoDB for seeding/teardown, no
mocking. (ssh_helper.py's mocked-paramiko unit tests live separately in
test_ssh_helper.py, since that module has no live counterpart to hit.)
"""
import os
import time
import uuid

import pytest
import requests
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv("/app/frontend/.env")
load_dotenv("/app/backend/.env")
BASE_URL = os.environ.get("REACT_APP_BACKEND_URL").rstrip("/") + "/api"
MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]

DEMO_EMAIL = os.environ.get("TEST_DEMO_EMAIL", "admin@tneuralai.com")
DEMO_PASSWORD = os.environ.get("TEST_DEMO_PASSWORD", "tneural123")

# Mirrors routers/deployment.py's STUB_STAGE_DELAY_SECONDS; a real (not
# instant) busy window is what makes the concurrency test deterministic
# rather than a race, per M0's design.
STUB_STAGE_DELAY_SECONDS = 1.5
STAGE_WAIT_BUFFER = 1.0


@pytest.fixture(scope="module")
def db():
    return MongoClient(MONGO_URL)[DB_NAME]


@pytest.fixture(scope="module")
def token():
    r = requests.post(f"{BASE_URL}/auth/login", json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD})
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    return r.json()["token"]


@pytest.fixture(scope="module")
def hdr(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def fresh_project(hdr, db):
    """A dedicated project per test, so each test's deployment_pipelines /
    pipeline_runs docs (and busy state) are fully isolated from every other
    test in this module - the concurrency guard is keyed on pipeline_id,
    which is 1:1 with project, so sharing a project across tests would let
    one test's busy window leak into the next."""
    r = requests.post(
        f"{BASE_URL}/projects",
        headers=hdr,
        json={
            "name": f"TEST_deploy_pipeline_{uuid.uuid4().hex[:8]}",
            "description": "deploy pipeline M0 test",
            "task_type": "object_detection",
        },
    )
    assert r.status_code in (200, 201), r.text
    pid = r.json()["id"]
    yield pid
    db.projects.delete_one({"id": pid})
    pipeline = db.deployment_pipelines.find_one({"project_id": pid})
    if pipeline:
        db.pipeline_runs.delete_many({"pipeline_id": pipeline["id"]})
        db.deployment_pipelines.delete_one({"id": pipeline["id"]})


def _create_run(hdr, pid):
    r = requests.post(f"{BASE_URL}/projects/{pid}/pipeline/runs", headers=hdr)
    assert r.status_code == 200, r.text
    return r.json()


def test_create_pipeline_run(hdr, fresh_project):
    run = _create_run(hdr, fresh_project)
    assert run["status"] == "draft"
    assert run["busy"] is False
    assert run["run_type"] == "bootstrap"  # first run ever on a fresh project
    assert run["pipeline_id"]
    assert run["stage_history"] == []


def test_stage_call_marks_busy_then_clears(hdr, fresh_project):
    run = _create_run(hdr, fresh_project)
    rid = run["id"]

    r = requests.post(f"{BASE_URL}/pipeline/runs/{rid}/stage/uploading_data", headers=hdr)
    assert r.status_code == 200, r.text
    assert r.json()["busy"] is True

    r = requests.get(f"{BASE_URL}/pipeline/runs/{rid}", headers=hdr)
    assert r.status_code == 200
    assert r.json()["busy"] is True

    time.sleep(STUB_STAGE_DELAY_SECONDS + STAGE_WAIT_BUFFER)

    r = requests.get(f"{BASE_URL}/pipeline/runs/{rid}", headers=hdr)
    assert r.status_code == 200
    body = r.json()
    assert body["busy"] is False
    assert body["stage_history"][0]["status"] == "stub_complete"


def test_concurrent_stage_call_rejected(hdr, fresh_project):
    """The M0 DoD test: two rapid stage calls on the same pipeline_id, second gets 409."""
    run = _create_run(hdr, fresh_project)
    rid = run["id"]

    r1 = requests.post(f"{BASE_URL}/pipeline/runs/{rid}/stage/uploading_data", headers=hdr)
    assert r1.status_code == 200, r1.text

    r2 = requests.post(f"{BASE_URL}/pipeline/runs/{rid}/stage/class_check", headers=hdr)
    assert r2.status_code == 409, r2.text


def test_unknown_stage_rejected(hdr, fresh_project):
    run = _create_run(hdr, fresh_project)
    r = requests.post(f"{BASE_URL}/pipeline/runs/{run['id']}/stage/not_a_real_stage", headers=hdr)
    assert r.status_code == 400


def test_stage_on_missing_run_404(hdr):
    r = requests.post(f"{BASE_URL}/pipeline/runs/{uuid.uuid4()}/stage/uploading_data", headers=hdr)
    assert r.status_code == 404


def test_bootstrap_run_cannot_deploy(hdr, fresh_project):
    run = _create_run(hdr, fresh_project)
    assert run["run_type"] == "bootstrap"
    r = requests.post(f"{BASE_URL}/pipeline/runs/{run['id']}/stage/deploying", headers=hdr)
    assert r.status_code == 400
