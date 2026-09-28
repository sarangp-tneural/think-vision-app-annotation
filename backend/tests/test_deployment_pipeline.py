"""Deploy Pipeline tests: pipeline run creation and the stage concurrency
guard (M0), the split-export format (M2), the class_check stage (M3), the
uploading_data stage's backgrounding/concurrency-guard/failure path (M4),
the training_remote stage's failure/concurrency-guard path (M5), and the
downloading_model stage's failure/validation path (M6), and the testing
stage's precondition-failure path (M7).

Live-HTTP integration tests, following the same idiom as
test_iteration9_new_endpoints.py / test_iteration11_routers_nudge_obb.py: real
requests against a running backend, real MongoDB for seeding/teardown, no
mocking. (ssh_helper.py's, pipeline_logic.py's, and deployment_worker.py's
mocked/pure unit tests live separately in test_ssh_helper.py /
test_pipeline_logic.py / test_deployment_worker.py, since none of those
modules has a live route of their own to hit. The class_check and
uploading_data failure-path tests below point at a real closed port instead
of mocking SSH - see their docstrings.)
"""
import os
import time
import uuid
import zipfile
from io import BytesIO

import pytest
import requests
import yaml
from dotenv import load_dotenv
from PIL import Image
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


def _create_merge_run(hdr, pid, db):
    """After M7, `deploying` is the only stage left as a generic stub, and
    it's blocked on bootstrap runs (M0's own guard) - every fresh project's
    first run defaults to run_type "bootstrap", so tests that want to use a
    stub stage as an arbitrary placeholder need a non-bootstrap run instead.
    Creates and completes a first run, then creates a second - the second's
    run_type is "merge" per the run-creation endpoint's own placeholder
    derivation (bootstrap iff no prior completed run exists)."""
    first = _create_run(hdr, pid)
    db.pipeline_runs.update_one({"id": first["id"]}, {"$set": {"status": "completed"}})
    run = _create_run(hdr, pid)
    assert run["run_type"] == "merge"
    return run


def test_create_pipeline_run(hdr, fresh_project):
    run = _create_run(hdr, fresh_project)
    assert run["status"] == "draft"
    assert run["busy"] is False
    assert run["run_type"] == "bootstrap"  # first run ever on a fresh project
    assert run["pipeline_id"]
    assert run["stage_history"] == []


def test_stage_call_marks_busy_then_clears(hdr, fresh_project, db):
    """Uses deploying (via _create_merge_run), the one stage still left as a
    generic stub as of M7 - every other stage graduated to a real one with
    its own request-body validation, so a still-generic stub stage (on a
    non-bootstrap run, since deploying is blocked on bootstrap runs) is
    needed here to exercise M0's original stub mechanism."""
    run = _create_merge_run(hdr, fresh_project, db)
    rid = run["id"]

    r = requests.post(f"{BASE_URL}/pipeline/runs/{rid}/stage/deploying", headers=hdr)
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


def test_concurrent_stage_call_rejected(hdr, fresh_project, db):
    """The M0 DoD test: two rapid stage calls on the same pipeline_id, second
    gets 409. Uses deploying (still a generic stub, via _create_merge_run)
    for the same reason as test_stage_call_marks_busy_then_clears above."""
    run = _create_merge_run(hdr, fresh_project, db)
    rid = run["id"]

    r1 = requests.post(f"{BASE_URL}/pipeline/runs/{rid}/stage/deploying", headers=hdr)
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


# --- M2: split-export format -------------------------------------------------

@pytest.fixture
def seeded_split_project(hdr, db):
    """A project seeded with real, fetchable annotated images - not the
    storage_path: "nonexistent/..." shortcut other test files use, since
    _build_split_dataset_dir skips the WHOLE image+label pair on a fetch
    failure (unlike export_dataset's other formats), so fake paths would
    silently empty every split and defeat the pairing assertions below."""
    r = requests.post(
        f"{BASE_URL}/projects",
        headers=hdr,
        json={
            "name": f"TEST_split_export_{uuid.uuid4().hex[:8]}",
            "description": "split export M2 test",
            "task_type": "object_detection",
        },
    )
    assert r.status_code in (200, 201), r.text
    pid = r.json()["id"]

    r = requests.post(f"{BASE_URL}/projects/{pid}/classes", headers=hdr, json={"label": "widget"})
    assert r.status_code == 200, r.text

    image_ids = []
    for _ in range(20):
        buf = BytesIO()
        Image.new("RGB", (8, 8), color=(120, 50, 200)).save(buf, format="JPEG")
        buf.seek(0)
        r = requests.post(
            f"{BASE_URL}/projects/{pid}/images",
            headers=hdr,
            files={"file": ("seed.jpg", buf, "image/jpeg")},
        )
        assert r.status_code == 200, r.text
        img_id = r.json()["id"]

        r = requests.put(
            f"{BASE_URL}/images/{img_id}/annotations",
            headers=hdr,
            json={"boxes": [{"type": "bbox", "label": "widget", "x": 0.1, "y": 0.1, "w": 0.3, "h": 0.3}]},
        )
        assert r.status_code == 200, r.text
        image_ids.append(img_id)

    yield pid, image_ids

    db.projects.delete_one({"id": pid})
    db.images.delete_many({"project_id": pid})


def _stem_set(names, prefix):
    return {n[len(prefix):].rsplit(".", 1)[0] for n in names if n.startswith(prefix)}


def test_split_export_structure(hdr, seeded_split_project):
    pid, image_ids = seeded_split_project
    r = requests.get(f"{BASE_URL}/projects/{pid}/export?format=split", headers=hdr)
    assert r.status_code == 200, r.text
    assert r.headers.get("content-type", "").startswith("application/zip")

    zf = zipfile.ZipFile(BytesIO(r.content))
    names = zf.namelist()

    total_images = 0
    for split in ("train", "valid", "test"):
        img_stems = _stem_set(names, f"dataset/{split}/images/")
        lbl_stems = _stem_set(names, f"dataset/{split}/labels/")
        assert img_stems, f"{split}: no images written"
        assert img_stems == lbl_stems, f"{split}: image/label mismatch: {img_stems ^ lbl_stems}"
        total_images += len(img_stems)

    assert total_images == len(image_ids)

    data_yaml = yaml.safe_load(zf.read("dataset/data.yaml").decode())
    assert data_yaml["train"] == "train/images"
    assert data_yaml["val"] == "valid/images"
    assert data_yaml["test"] == "test/images"
    assert data_yaml["nc"] == 1
    assert data_yaml["names"] == ["widget"]


def test_split_export_bad_percentages(hdr, seeded_split_project):
    pid, _ = seeded_split_project
    r = requests.get(
        f"{BASE_URL}/projects/{pid}/export?format=split&train_pct=0.5&valid_pct=0.5&test_pct=0.5",
        headers=hdr,
    )
    assert r.status_code == 400


# --- M3: class_check stage ---------------------------------------------------

def test_class_check_stage_connection_failure_marks_run_failed(hdr, fresh_project):
    """Points at a real closed port instead of mocking ssh_helper - there's no
    existing precedent for mocking inside a live-HTTP test (the server is an
    already-running separate process), and a real refused connection exercises
    the exact same SSHConnectionError path with zero mocking, deterministically
    fast (an OS-level refusal, not a timeout)."""
    run = _create_run(hdr, fresh_project)
    r = requests.post(
        f"{BASE_URL}/pipeline/runs/{run['id']}/stage/class_check",
        headers=hdr,
        json={
            "host": "127.0.0.1",
            "port": 1,
            "username": "nobody",
            "password": "irrelevant",
            "remote_data_yaml_path": "/data/data.yaml",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "failed"
    assert body["busy"] is False
    assert body["error"]


def test_class_check_stage_invalid_body_rejected(hdr, fresh_project):
    run = _create_run(hdr, fresh_project)
    r = requests.post(
        f"{BASE_URL}/pipeline/runs/{run['id']}/stage/class_check",
        headers=hdr,
        json={"host": "127.0.0.1", "username": "nobody"},  # missing remote_data_yaml_path
    )
    assert r.status_code == 400


# --- M4: uploading_data stage -------------------------------------------------

def test_uploading_data_stage_returns_immediately_then_fails(hdr, fresh_project):
    """Points at a real closed port, same idiom as the class_check failure
    test - there's still no way to mock SSH inside an already-running,
    separate server process. Unlike class_check (synchronous), this stage's
    work is genuinely backgrounded, so the POST itself must return quickly
    with busy still True (proving the SSH/upload work isn't blocking the
    request), and the run eventually resolves to failed with an error."""
    run = _create_run(hdr, fresh_project)
    rid = run["id"]

    start = time.monotonic()
    r1 = requests.post(
        f"{BASE_URL}/pipeline/runs/{rid}/stage/uploading_data",
        headers=hdr,
        json={
            "host": "127.0.0.1",
            "port": 1,
            "username": "nobody",
            "password": "irrelevant",
            "remote_workdir": "/srv/work",
        },
    )
    elapsed = time.monotonic() - start
    assert r1.status_code == 200, r1.text
    assert r1.json()["busy"] is True
    assert elapsed < 5, f"stage/uploading_data should return immediately, took {elapsed:.2f}s"

    time.sleep(3)
    r = requests.get(f"{BASE_URL}/pipeline/runs/{rid}", headers=hdr)
    body = r.json()
    assert body["busy"] is False
    assert body["status"] == "failed"
    assert body["error"]


def test_uploading_data_stage_respects_concurrency_guard(hdr, fresh_project, db):
    """A real closed-port connection refusal on loopback can resolve in
    under a millisecond - racing it against a second HTTP round-trip from
    this test process is not reliably observable (confirmed: flaked under
    load). Instead, use the same reliable mechanism M0's own concurrency
    test already established: the generic stub's fixed delay gives a wide,
    deterministic busy window. Calling uploading_data as the SECOND stage
    (rather than the first, like M0's test) confirms uploading_data's new
    branch is dispatched after the shared guard check, not before it.
    Uses deploying (via _create_merge_run) as the stub-stage placeholder -
    see _create_merge_run's docstring for why a plain bootstrap run won't do."""
    run = _create_merge_run(hdr, fresh_project, db)
    rid = run["id"]

    r1 = requests.post(f"{BASE_URL}/pipeline/runs/{rid}/stage/deploying", headers=hdr)
    assert r1.status_code == 200, r1.text

    r2 = requests.post(
        f"{BASE_URL}/pipeline/runs/{rid}/stage/uploading_data",
        headers=hdr,
        json={"host": "127.0.0.1", "port": 1, "username": "nobody",
              "password": "irrelevant", "remote_workdir": "/srv/work"},
    )
    assert r2.status_code == 409, r2.text


# --- M5: training_remote stage -----------------------------------------------

def test_training_remote_stage_without_dataset_export_fails(hdr, fresh_project):
    """This environment has no real SSH server to test against live, so
    (mirroring uploading_data's failure test, which uses a real closed port)
    this exercises a different, equally real failure mode instead: calling
    training_remote before uploading_data has ever populated dataset_export.
    The worker checks this and fails fast, before attempting any SSH
    connection at all."""
    run = _create_run(hdr, fresh_project)
    rid = run["id"]

    start = time.monotonic()
    r1 = requests.post(
        f"{BASE_URL}/pipeline/runs/{rid}/stage/training_remote",
        headers=hdr,
        json={
            "host": "127.0.0.1",
            "port": 1,
            "username": "nobody",
            "password": "irrelevant",
            "remote_workdir": "/srv/work",
            "remote_base_model_path": "/models/base.pt",
        },
    )
    elapsed = time.monotonic() - start
    assert r1.status_code == 200, r1.text
    assert r1.json()["busy"] is True
    assert elapsed < 5, f"stage/training_remote should return immediately, took {elapsed:.2f}s"

    time.sleep(1)
    r = requests.get(f"{BASE_URL}/pipeline/runs/{rid}", headers=hdr)
    body = r.json()
    assert body["busy"] is False
    assert body["status"] == "failed"
    assert "uploading_data" in body["error"] or "dataset_export" in body["error"]


def test_training_remote_stage_respects_concurrency_guard(hdr, fresh_project, db):
    """Same reliable mechanism as uploading_data's concurrency test: the
    generic stub's fixed delay gives a wide, deterministic busy window.
    Uses deploying (via _create_merge_run) as the stub-stage placeholder."""
    run = _create_merge_run(hdr, fresh_project, db)
    rid = run["id"]

    r1 = requests.post(f"{BASE_URL}/pipeline/runs/{rid}/stage/deploying", headers=hdr)
    assert r1.status_code == 200, r1.text

    r2 = requests.post(
        f"{BASE_URL}/pipeline/runs/{rid}/stage/training_remote",
        headers=hdr,
        json={"host": "127.0.0.1", "port": 1, "username": "nobody", "password": "irrelevant",
              "remote_workdir": "/srv/work", "remote_base_model_path": "/models/base.pt"},
    )
    assert r2.status_code == 409, r2.text


def test_training_remote_stage_invalid_body_rejected(hdr, fresh_project):
    run = _create_run(hdr, fresh_project)
    r = requests.post(
        f"{BASE_URL}/pipeline/runs/{run['id']}/stage/training_remote",
        headers=hdr,
        json={"host": "127.0.0.1", "username": "nobody"},  # missing remote_workdir, remote_base_model_path
    )
    assert r.status_code == 400


def test_uploading_data_stage_bad_percentages_rejected(hdr, fresh_project):
    run = _create_run(hdr, fresh_project)
    r = requests.post(
        f"{BASE_URL}/pipeline/runs/{run['id']}/stage/uploading_data",
        headers=hdr,
        json={
            "host": "127.0.0.1", "port": 1, "username": "nobody", "password": "x",
            "remote_workdir": "/srv/work", "train_pct": 0.5, "valid_pct": 0.5, "test_pct": 0.5,
        },
    )
    assert r.status_code == 400


# --- M6: downloading_model stage ---------------------------------------------

def test_downloading_model_stage_without_training_data_fails(hdr, fresh_project):
    """Same reasoning as training_remote's precondition-failure test: no real
    SSH server exists in this environment, so the meaningful automatable
    failure mode is calling downloading_model before training_remote has
    ever populated the run's training field. The worker checks this and
    fails fast, before attempting any SSH connection at all."""
    run = _create_run(hdr, fresh_project)
    rid = run["id"]

    start = time.monotonic()
    r1 = requests.post(
        f"{BASE_URL}/pipeline/runs/{rid}/stage/downloading_model",
        headers=hdr,
        json={"host": "127.0.0.1", "port": 1, "username": "nobody", "password": "irrelevant"},
    )
    elapsed = time.monotonic() - start
    assert r1.status_code == 200, r1.text
    assert r1.json()["busy"] is True
    assert elapsed < 5, f"stage/downloading_model should return immediately, took {elapsed:.2f}s"

    time.sleep(1)
    r = requests.get(f"{BASE_URL}/pipeline/runs/{rid}", headers=hdr)
    body = r.json()
    assert body["busy"] is False
    assert body["status"] == "failed"
    assert "training_remote" in body["error"]


def test_downloading_model_stage_invalid_body_rejected(hdr, fresh_project):
    run = _create_run(hdr, fresh_project)
    r = requests.post(
        f"{BASE_URL}/pipeline/runs/{run['id']}/stage/downloading_model",
        headers=hdr,
        json={"host": "127.0.0.1"},  # missing username
    )
    assert r.status_code == 400


# --- M7: testing stage --------------------------------------------------------

def test_testing_stage_without_candidate_model_fails(hdr, fresh_project):
    """testing needs no SSH or real Ultralytics run to reach this failure -
    calling it before downloading_model has ever populated candidate_model
    fails fast, mirroring M5/M6's precondition-failure tests exactly."""
    run = _create_run(hdr, fresh_project)
    rid = run["id"]

    start = time.monotonic()
    r1 = requests.post(f"{BASE_URL}/pipeline/runs/{rid}/stage/testing", headers=hdr)
    elapsed = time.monotonic() - start
    assert r1.status_code == 200, r1.text
    assert r1.json()["busy"] is True
    assert elapsed < 5, f"stage/testing should return immediately, took {elapsed:.2f}s"

    time.sleep(1)
    r = requests.get(f"{BASE_URL}/pipeline/runs/{rid}", headers=hdr)
    body = r.json()
    assert body["busy"] is False
    assert body["status"] == "failed"
    assert "downloading_model" in body["error"]
