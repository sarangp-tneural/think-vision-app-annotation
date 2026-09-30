"""Deploy Pipeline tests: pipeline run creation and the stage concurrency
guard (M0), the split-export format (M2), the class_check stage (M3), the
uploading_data stage's backgrounding/concurrency-guard/failure path (M4),
the training_remote stage's failure/concurrency-guard path (M5), and the
downloading_model stage's failure/validation path (M6), the testing stage's
precondition-failure path (M7), and approve/reject/rollback (M8).

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
from datetime import datetime, timezone
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
    db.models.delete_many({"project_id": pid})
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


def test_concurrent_stage_call_rejected(hdr, fresh_project, db):
    """The M0 DoD test: a stage call is rejected while the pipeline is
    already busy. Directly seeds busy: True via Mongo rather than relying on
    a real stage call's own timing - as of M8, every stage is real (no
    generic stub left with an artificial delay), so there's no stage whose
    busy window is reliably slow enough to race a second HTTP call against.
    Seeding the state directly is simpler and doesn't depend on any stage's
    behavior at all - it only exercises the shared guard itself."""
    run = _create_run(hdr, fresh_project)
    rid = run["id"]
    db.pipeline_runs.update_one({"id": rid}, {"$set": {"busy": True}})

    r = requests.post(f"{BASE_URL}/pipeline/runs/{rid}/stage/class_check", headers=hdr)
    assert r.status_code == 409, r.text


def test_unknown_stage_rejected(hdr, fresh_project):
    run = _create_run(hdr, fresh_project)
    r = requests.post(f"{BASE_URL}/pipeline/runs/{run['id']}/stage/not_a_real_stage", headers=hdr)
    assert r.status_code == 400


def test_stage_on_missing_run_404(hdr):
    r = requests.post(f"{BASE_URL}/pipeline/runs/{uuid.uuid4()}/stage/uploading_data", headers=hdr)
    assert r.status_code == 404


def test_bootstrap_run_deploy_needs_approval(hdr, fresh_project):
    run = _create_run(hdr, fresh_project)
    assert run["run_type"] == "bootstrap"
    r = requests.post(f"{BASE_URL}/pipeline/runs/{run['id']}/stage/deploying", headers=hdr)
    assert r.status_code == 400
    assert "approved" in r.text.lower()


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
    load), and as of M8 no stage has an artificial delay left to race
    against anyway. Directly seeds busy: True instead (same fix as M0's own
    concurrency test above) - this confirms uploading_data's branch is
    dispatched after the shared guard check, not before it."""
    run = _create_run(hdr, fresh_project)
    rid = run["id"]
    db.pipeline_runs.update_one({"id": rid}, {"$set": {"busy": True}})

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
    """Same direct busy-seed mechanism as uploading_data's concurrency test above."""
    run = _create_run(hdr, fresh_project)
    rid = run["id"]
    db.pipeline_runs.update_one({"id": rid}, {"$set": {"busy": True}})

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

def test_testing_stage_requires_a_video_test(hdr, fresh_project):
    """Finishing the Test step needs at least one succeeded video test."""
    run = _create_run(hdr, fresh_project)
    r = requests.post(f"{BASE_URL}/pipeline/runs/{run['id']}/stage/testing", headers=hdr)
    assert r.status_code == 400
    assert "video test" in r.json()["detail"]


def test_test_video_without_candidate_model_rejected(hdr, fresh_project):
    run = _create_run(hdr, fresh_project)
    r = requests.post(
        f"{BASE_URL}/pipeline/runs/{run['id']}/test-video", headers=hdr,
        files={"file": ("t.mp4", b"x", "video/mp4")},
    )
    assert r.status_code == 400


# --- M8: approve / reject / rollback ------------------------------------------

def _seed_awaiting_approval_run(db, pid, rid, run_type="bootstrap", with_candidate=True):
    now = datetime.now(timezone.utc).isoformat()
    update = {"status": "awaiting_approval", "run_type": run_type, "updated_at": now}
    if with_candidate:
        mid = str(uuid.uuid4())
        weights_path = f"visionforge/projects/{pid}/pipeline_runs/{rid}/candidate.pt"
        db.models.insert_one({
            "id": mid, "project_id": pid, "type": "pipeline_candidate", "is_active": False,
            "weights_path": weights_path, "created_at": now,
        })
        update["candidate_model"] = {"local_model_id": mid, "weights_path": weights_path, "metrics": {}}
    db.pipeline_runs.update_one({"id": rid}, {"$set": update})
    return update.get("candidate_model", {}).get("local_model_id")


def test_approve_on_wrong_status_rejected(hdr, fresh_project):
    run = _create_run(hdr, fresh_project)  # status: draft
    r = requests.post(f"{BASE_URL}/pipeline/runs/{run['id']}/approve", headers=hdr)
    assert r.status_code == 409, r.text


def test_reject_on_wrong_status_rejected(hdr, fresh_project):
    run = _create_run(hdr, fresh_project)  # status: draft
    r = requests.post(f"{BASE_URL}/pipeline/runs/{run['id']}/reject", headers=hdr)
    assert r.status_code == 409, r.text


def test_bootstrap_approve_stays_open_for_deploy_without_activating(hdr, fresh_project, db):
    """No SSH involved at all for a bootstrap approval, so this can run live
    without a real remote host. Activation is manual, so approval leaves the model inactive."""
    run = _create_run(hdr, fresh_project)
    assert run["run_type"] == "bootstrap"
    mid = _seed_awaiting_approval_run(db, fresh_project, run["id"], run_type="bootstrap")

    r = requests.post(f"{BASE_URL}/pipeline/runs/{run['id']}/approve", headers=hdr)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "awaiting_approval"  # Deploy completes the run
    assert body["approval"]["decision"] == "approved"

    model = db.models.find_one({"id": mid})
    assert model["is_active"] is False


def test_reject_sets_terminal_status_and_makes_no_ssh_calls(hdr, fresh_project, db):
    """No credentials are supplied anywhere in this test - if reject made any
    SSH attempt, it would need connection details it was never given and
    would fail; instead it just succeeds, proving the zero-SSH-calls DoD
    structurally (no ssh_helper import exists in the reject handler at all)."""
    run = _create_run(hdr, fresh_project)
    _seed_awaiting_approval_run(db, fresh_project, run["id"], with_candidate=False)

    r = requests.post(f"{BASE_URL}/pipeline/runs/{run['id']}/reject", headers=hdr, json={"note": "not good enough"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "rejected"
    assert body["approval"]["decision"] == "rejected"
    assert body["approval"]["note"] == "not good enough"


def test_deploying_without_approval_rejected(hdr, fresh_project, db):
    run = _create_run(hdr, fresh_project)  # bootstrap by default
    # Force fresh_production so the bootstrap-block guard doesn't shadow the
    # approval-required guard this test is actually targeting.
    db.pipeline_runs.update_one({"id": run["id"]}, {"$set": {"run_type": "fresh_production"}})

    r = requests.post(
        f"{BASE_URL}/pipeline/runs/{run['id']}/stage/deploying",
        headers=hdr,
        json={"host": "127.0.0.1", "port": 1, "username": "nobody", "password": "x",
              "remote_production_model_path": "/srv/models/prod.pt"},
    )
    assert r.status_code == 400, r.text
    assert "approved" in r.text.lower()


def test_rollback_without_deployment_pipeline_fails(hdr, fresh_project):
    """A project that has never created a single pipeline run has no
    deployment_pipelines doc yet - nothing to roll back from."""
    r = requests.post(
        f"{BASE_URL}/projects/{fresh_project}/pipeline/rollback",
        headers=hdr,
        json={"host": "127.0.0.1", "port": 1, "username": "nobody", "password": "x",
              "remote_production_model_path": "/srv/models/prod.pt"},
    )
    assert r.status_code == 400, r.text


# --- M9: run history list (backend gap found while planning the frontend) ---

def test_list_pipeline_runs_empty_for_fresh_project(hdr, fresh_project):
    r = requests.get(f"{BASE_URL}/projects/{fresh_project}/pipeline/runs", headers=hdr)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["runs"] == []
    assert body["last_deployed_run_id"] is None


def test_list_pipeline_runs_returns_newest_first_with_last_deployed(hdr, fresh_project, db):
    run1 = _create_run(hdr, fresh_project)
    run2 = _create_run(hdr, fresh_project)

    pipeline = db.deployment_pipelines.find_one({"project_id": fresh_project})
    db.deployment_pipelines.update_one({"id": pipeline["id"]}, {"$set": {"last_deployed_run_id": run1["id"]}})

    r = requests.get(f"{BASE_URL}/projects/{fresh_project}/pipeline/runs", headers=hdr)
    assert r.status_code == 200, r.text
    body = r.json()
    ids_in_order = [run["id"] for run in body["runs"]]
    assert ids_in_order == [run2["id"], run1["id"]]  # newest first
    assert body["last_deployed_run_id"] == run1["id"]
    assert body["pipeline_id"] == pipeline["id"]


# --- per-run saved form --------------------------------------------------------

def test_run_form_saved_without_secrets_and_copied_to_next_run(hdr, fresh_project):
    run = _create_run(hdr, fresh_project)
    r = requests.put(
        f"{BASE_URL}/pipeline/runs/{run['id']}/form", headers=hdr,
        json={"host": "10.0.0.5", "username": "root", "remote_workdir": "/w",
              "password": "secret", "pem_key": "KEY", "bogus": 1},
    )
    assert r.status_code == 200, r.text
    assert r.json()["form"] == {"host": "10.0.0.5", "username": "root", "remote_workdir": "/w"}

    stored = requests.get(f"{BASE_URL}/pipeline/runs/{run['id']}", headers=hdr).json()
    assert stored["form"]["host"] == "10.0.0.5"
    assert "password" not in stored["form"] and "pem_key" not in stored["form"]

    nxt = _create_run(hdr, fresh_project)
    assert nxt["form"]["host"] == "10.0.0.5"


def test_deploy_rejects_model_that_is_not_server_trained(hdr, fresh_project, db):
    run = _create_run(hdr, fresh_project)
    _seed_awaiting_approval_run(db, fresh_project, run["id"], run_type="bootstrap")  # seeded model has no source=server
    assert requests.post(f"{BASE_URL}/pipeline/runs/{run['id']}/approve", headers=hdr).status_code == 200

    r = requests.post(
        f"{BASE_URL}/pipeline/runs/{run['id']}/stage/deploying", headers=hdr,
        json={"host": "127.0.0.1", "port": 1, "username": "nobody", "password": "x",
              "remote_production_model_path": "/srv/models/prod.pt", "model_id": str(uuid.uuid4())},
    )
    assert r.status_code == 400, r.text


def test_deploy_form_saved_under_deploy_keys_without_secrets(hdr, fresh_project):
    run = _create_run(hdr, fresh_project)
    r = requests.put(
        f"{BASE_URL}/pipeline/runs/{run['id']}/form", headers=hdr,
        json={"deploy_host": "10.1.1.1", "deploy_model_path": "/srv/prod.pt", "password": "x"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["form"] == {"deploy_host": "10.1.1.1", "deploy_model_path": "/srv/prod.pt"}
