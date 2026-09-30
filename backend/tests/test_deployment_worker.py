"""Unit tests for deployment_worker.py: calls its _pipeline_*_sync functions
directly (no live server, no HTTP) with ssh_helper mocked (matching
test_ssh_helper.py's style) and, for uploading_data/downloading_model/testing,
a fake get_object_fn/put_object_fn/yolo_predict_fn (no real object storage or
Ultralytics - ultralytics isn't even installed in this dev environment, so
pipeline_logic.evaluate is mocked directly for the testing-stage tests) - but
against a REAL local MongoDB connection (matching test_deployment_pipeline.py's
style) to seed a projects/pipeline_runs doc beforehand and assert the actual
written fields afterward. The M8 deploying/rollback tests use a shared
in-memory {path: bytes} dict as a fake remote filesystem, with individual
ssh_helper functions patched directly (not paramiko's own SFTPClient) -
matching this file's existing per-function mocking granularity. This is the
natural continuation of test_deployment_pipeline.py's own stated split: pure/
no-live-route modules get their own mocked/direct-import test file, exactly
like ssh_helper.py and pipeline_logic.py already do - deployment_worker.py's
functions are invoked from a background thread, not a route handler, so there
is no live HTTP path to test them through anyway.
"""
import json
import os
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from dotenv import load_dotenv
from pymongo import MongoClient

import deployment_worker
import ssh_helper

load_dotenv("/app/backend/.env")
MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]


def _fake_get_object(storage_path):
    # A tiny, valid-enough JPEG isn't required here - _yolo_label_line/the
    # writer only care that bytes come back, not that they decode as an image.
    return b"\xff\xd8\xff\xe0FAKEJPEGBYTES", "image/jpeg"


def _seed_project_and_run(db, image_count=10):
    pid = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    db.projects.insert_one({
        "id": pid, "name": "TEST_worker_project", "classes": ["widget"],
        "created_at": now,
    })
    for i in range(image_count):
        db.images.insert_one({
            "id": str(uuid.uuid4()), "project_id": pid, "filename": f"seed_{i}.jpg",
            "storage_path": f"fake/{pid}/{i}.jpg", "annotated": True,
            "annotations": [{"type": "bbox", "label": "widget", "x": 0.1, "y": 0.1, "w": 0.3, "h": 0.3}],
            "review_status": "unassigned", "created_at": now,
        })
    rid = str(uuid.uuid4())
    db.pipeline_runs.insert_one({
        "id": rid, "project_id": pid, "pipeline_id": str(uuid.uuid4()),
        "run_type": "bootstrap", "status": "uploading_data", "busy": True,
        "stage_history": [{"stage": "uploading_data", "status": "running", "started_at": now}],
        "created_at": now, "updated_at": now,
    })
    return pid, rid


def _cleanup(db, pid, rid):
    db.projects.delete_one({"id": pid})
    db.images.delete_many({"project_id": pid})
    db.pipeline_runs.delete_one({"id": rid})


def test_uploading_data_populates_dataset_export_and_clears_busy():
    db = MongoClient(MONGO_URL)[DB_NAME]
    pid, rid = _seed_project_and_run(db, image_count=10)
    try:
        mock_client = MagicMock()
        with patch("deployment_worker.ssh_helper.connect") as mock_connect, \
             patch("deployment_worker.ssh_helper.remote_exists", return_value=False), \
             patch("deployment_worker.ssh_helper.upload_dir") as mock_upload_dir:
            mock_connect.return_value.__enter__.return_value = mock_client
            mock_connect.return_value.__exit__.return_value = False

            deployment_worker._pipeline_uploading_data_sync(
                rid, pid, 0,
                {"host": "example.com", "port": 22, "username": "u", "pem_key": None,
                 "password": "p", "remote_workdir": "/srv/work", "train_pct": 0.7,
                 "valid_pct": 0.2, "test_pct": 0.1},
                _fake_get_object,
            )

            mock_connect.assert_called_once_with("example.com", 22, "u", None, "p")
            mock_upload_dir.assert_called_once()
            assert mock_upload_dir.call_args.args[0] is mock_client
            assert mock_upload_dir.call_args.args[2] == "/srv/work/dataset"

        run = db.pipeline_runs.find_one({"id": rid})
        assert run["busy"] is False
        de = run["dataset_export"]
        assert de["train_count"] + de["valid_count"] + de["test_count"] == 10
        assert de["train_count"] == 7
        assert de["valid_count"] == 2
        assert de["test_count"] == 1
        assert len(de["test_image_ids"]) == de["test_count"]
        assert de["remote_upload_path"] == "/srv/work/dataset"
        assert run["stage_history"][0]["status"] == "succeeded"
    finally:
        _cleanup(db, pid, rid)


def test_uploading_data_connection_failure_marks_run_failed():
    db = MongoClient(MONGO_URL)[DB_NAME]
    pid, rid = _seed_project_and_run(db, image_count=5)
    try:
        with patch("deployment_worker.ssh_helper.connect") as mock_connect:
            mock_connect.side_effect = ssh_helper.SSHConnectionError("boom")

            deployment_worker._pipeline_uploading_data_sync(
                rid, pid, 0,
                {"host": "example.com", "port": 22, "username": "u", "pem_key": None,
                 "password": "p", "remote_workdir": "/srv/work", "train_pct": 0.7,
                 "valid_pct": 0.2, "test_pct": 0.1},
                _fake_get_object,
            )

        run = db.pipeline_runs.find_one({"id": rid})
        assert run["busy"] is False
        assert run["status"] == "failed"
        assert "boom" in run["error"]
        assert "dataset_export" not in run
        assert run["stage_history"][0]["status"] == "failed"
    finally:
        _cleanup(db, pid, rid)


# --- _pipeline_training_remote_sync() (M5) ----------------------------------

def _seed_run_with_dataset_export(db, run_type="bootstrap"):
    pid = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    db.projects.insert_one({"id": pid, "name": "TEST_worker_training", "classes": ["widget"], "created_at": now})
    rid = str(uuid.uuid4())
    db.pipeline_runs.insert_one({
        "id": rid, "project_id": pid, "pipeline_id": str(uuid.uuid4()),
        "run_type": run_type, "status": "training_remote", "busy": True,
        "dataset_export": {
            "train_count": 7, "valid_count": 2, "test_count": 1, "test_image_ids": [],
            "remote_upload_path": f"/srv/work/{rid}/dataset",
        },
        "stage_history": [{"stage": "training_remote", "status": "running", "started_at": now}],
        "created_at": now, "updated_at": now,
    })
    return pid, rid


_BASE_REQ = {
    "host": "example.com", "port": 22, "username": "u", "pem_key": None, "password": "p",
    "remote_workdir": "/srv/work", "remote_base_model_path": "/models/base.pt",
    "remote_production_model_path": None, "epochs": 3,
}


def test_training_remote_streams_progress_and_completes():
    db = MongoClient(MONGO_URL)[DB_NAME]
    pid, rid = _seed_run_with_dataset_export(db, run_type="bootstrap")
    try:
        mock_client = MagicMock()
        snapshots = []

        def fake_exec_streaming(client, cmd, on_output=None, **kwargs):
            for i in range(1, 4):
                on_output(f"{i}/3      0G   0.5   0.3   0.1        12       640\n")
                snapshots.append(db.pipeline_runs.find_one({"id": rid})["training"]["current_epoch"])
            return 0, "", ""

        with patch("deployment_worker.ssh_helper.connect") as mock_connect, \
             patch("deployment_worker.ssh_helper.upload_file") as mock_upload_file, \
             patch("deployment_worker.ssh_helper.exec_command_streaming", side_effect=fake_exec_streaming) as mock_exec:
            mock_connect.return_value.__enter__.return_value = mock_client
            mock_connect.return_value.__exit__.return_value = False

            deployment_worker._pipeline_training_remote_sync(rid, pid, 0, _BASE_REQ)

            mock_upload_file.assert_called_once()
            assert mock_upload_file.call_args.args[0] is mock_client
            assert mock_upload_file.call_args.args[2] == f"/srv/work/runs/run_{rid[:8]}/train_eval.py"
            mock_exec.assert_called_once()
            assert mock_exec.call_args.args[1] == f"python3 -u /srv/work/runs/run_{rid[:8]}/train_eval.py"

        # Each epoch line triggered its own, distinct Mongo write - not just
        # a single write at the very end.
        assert snapshots == [1, 2, 3]

        run = db.pipeline_runs.find_one({"id": rid})
        assert run["busy"] is False
        assert run["training"]["progress_pct"] == 100
        assert run["training"]["base_checkpoint_ref"] == "/models/base.pt"
        assert run["training"]["remote_run_dir"] == "/srv/work/runs"
        assert "3/3" in run["training"]["log_tail"]
        assert run["stage_history"][0]["status"] == "succeeded"
    finally:
        _cleanup(db, pid, rid)


def _run_training_remote_capturing_script(db, pid, rid, req):
    """Runs _pipeline_training_remote_sync with SSH fully mocked, capturing
    the local script's text before the function's own cleanup deletes it."""
    captured = {}

    def fake_upload_file(client, local_path, remote_path, **kwargs):
        with open(local_path) as f:
            captured["text"] = f.read()

    def fake_exec_streaming(client, cmd, on_output=None, **kwargs):
        return 0, "", ""

    with patch("deployment_worker.ssh_helper.connect") as mock_connect, \
         patch("deployment_worker.ssh_helper.upload_file", side_effect=fake_upload_file), \
         patch("deployment_worker.ssh_helper.exec_command_streaming", side_effect=fake_exec_streaming):
        mock_connect.return_value.__enter__.return_value = MagicMock()
        mock_connect.return_value.__exit__.return_value = False
        deployment_worker._pipeline_training_remote_sync(rid, pid, 0, req)

    return captured.get("text", "")


def test_training_remote_bootstrap_starts_from_base_checkpoint():
    db = MongoClient(MONGO_URL)[DB_NAME]
    pid, rid = _seed_run_with_dataset_export(db, run_type="bootstrap")
    try:
        script_text = _run_training_remote_capturing_script(db, pid, rid, _BASE_REQ)
        assert "/models/base.pt" in script_text
    finally:
        _cleanup(db, pid, rid)


def test_training_remote_merge_starts_from_production_checkpoint():
    db = MongoClient(MONGO_URL)[DB_NAME]
    pid, rid = _seed_run_with_dataset_export(db, run_type="merge")
    try:
        req = {**_BASE_REQ, "remote_production_model_path": "/models/prod.pt"}
        script_text = _run_training_remote_capturing_script(db, pid, rid, req)
        assert "/models/prod.pt" in script_text
        assert "/models/base.pt" not in script_text
    finally:
        _cleanup(db, pid, rid)


def test_training_remote_merge_without_production_path_fails():
    db = MongoClient(MONGO_URL)[DB_NAME]
    pid, rid = _seed_run_with_dataset_export(db, run_type="merge")
    try:
        deployment_worker._pipeline_training_remote_sync(rid, pid, 0, _BASE_REQ)  # no production path
        run = db.pipeline_runs.find_one({"id": rid})
        assert run["status"] == "failed"
        assert "remote_production_model_path" in run["error"]
    finally:
        _cleanup(db, pid, rid)


def test_training_remote_nonzero_exit_marks_run_failed():
    db = MongoClient(MONGO_URL)[DB_NAME]
    pid, rid = _seed_run_with_dataset_export(db, run_type="bootstrap")
    try:
        def fake_exec_streaming(client, cmd, on_output=None, **kwargs):
            on_output("Traceback (most recent call last):\n")
            return 1, "", ""

        with patch("deployment_worker.ssh_helper.connect") as mock_connect, \
             patch("deployment_worker.ssh_helper.upload_file"), \
             patch("deployment_worker.ssh_helper.exec_command_streaming", side_effect=fake_exec_streaming):
            mock_connect.return_value.__enter__.return_value = MagicMock()
            mock_connect.return_value.__exit__.return_value = False
            deployment_worker._pipeline_training_remote_sync(rid, pid, 0, _BASE_REQ)

        run = db.pipeline_runs.find_one({"id": rid})
        assert run["status"] == "failed"
        assert "exited 1" in run["error"]
    finally:
        _cleanup(db, pid, rid)


# --- _pipeline_downloading_model_sync() (M6) --------------------------------

_DL_REQ = {"host": "example.com", "port": 22, "username": "u", "pem_key": None, "password": "p"}


def _seed_run_with_training(db, run_type="bootstrap"):
    pid = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    db.projects.insert_one({"id": pid, "name": "TEST_worker_download", "classes": ["widget"], "created_at": now})
    rid = str(uuid.uuid4())
    remote_run_dir = f"/srv/work/{rid}/runs"
    metrics_json_path = f"/srv/work/{rid}/metrics.json"
    db.pipeline_runs.insert_one({
        "id": rid, "project_id": pid, "pipeline_id": str(uuid.uuid4()),
        "run_type": run_type, "status": "training_remote", "busy": True,
        "training": {
            "base_checkpoint_ref": "/models/base.pt",
            "remote_run_dir": remote_run_dir,
            "hyperparams_used": {"run_name": "train", "metrics_json_path": metrics_json_path},
            "progress_pct": 100, "log_tail": "", "started_at": now, "finished_at": now,
        },
        "stage_history": [{"stage": "downloading_model", "status": "running", "started_at": now}],
        "created_at": now, "updated_at": now,
    })
    return pid, rid, remote_run_dir, metrics_json_path


def _cleanup_download_test(db, pid, rid):
    db.projects.delete_one({"id": pid})
    db.pipeline_runs.delete_one({"id": rid})
    db.models.delete_many({"pipeline_run_id": rid})


def _fake_download_file_factory(content_by_remote_path, fail_paths=()):
    def _fake_download_file(client, remote_path, local_path, callback=None):
        if remote_path in fail_paths:
            raise FileNotFoundError(remote_path)
        data = content_by_remote_path[remote_path]
        with open(local_path, "wb") as f:
            f.write(data if isinstance(data, bytes) else data.encode())
    return _fake_download_file


def test_downloading_model_registers_candidate():
    db = MongoClient(MONGO_URL)[DB_NAME]
    pid, rid, remote_run_dir, metrics_json_path = _seed_run_with_training(db)
    try:
        best_pt_path = f"{remote_run_dir}/train/weights/best.pt"
        uploaded = {}

        def fake_put_object(path, data, content_type):
            uploaded["path"] = path
            uploaded["data"] = data
            uploaded["content_type"] = content_type
            return {"ok": True}

        fake_download = _fake_download_file_factory({
            best_pt_path: b"FAKEWEIGHTSBYTES",
            metrics_json_path: json.dumps({"map50": 0.8, "map50_95": 0.6, "precision": 0.7, "recall": 0.75}),
        })

        with patch("deployment_worker.ssh_helper.connect") as mock_connect, \
             patch("deployment_worker.ssh_helper.download_file", side_effect=fake_download):
            mock_connect.return_value.__enter__.return_value = MagicMock()
            mock_connect.return_value.__exit__.return_value = False

            deployment_worker._pipeline_downloading_model_sync(
                rid, pid, 0, _DL_REQ, fake_put_object, "visionforge",
            )

        assert uploaded["path"] == f"visionforge/projects/{pid}/pipeline_runs/{rid}/candidate.pt"
        assert uploaded["data"] == b"FAKEWEIGHTSBYTES"
        assert uploaded["content_type"] == "application/octet-stream"

        run = db.pipeline_runs.find_one({"id": rid})
        assert run["busy"] is False
        cm = run["candidate_model"]
        assert cm["weights_path"] == uploaded["path"]
        assert cm["metrics"] == {"mAP50": 0.8, "mAP50_95": 0.6, "precision": 0.7, "recall": 0.75}
        mid = cm["local_model_id"]

        model = db.models.find_one({"id": mid})
        assert model["type"] == "pipeline_candidate"
        assert model["is_active"] is False
        assert model["pipeline_run_id"] == rid
        assert model["weights_path"] == uploaded["path"]
        assert model["weights_size"] == len(b"FAKEWEIGHTSBYTES")
        assert model["final_mAP"] == 0.8
        assert model["final_loss"] == 1 - 0.6
        assert model["precision"] == 0.7
        assert model["recall"] == 0.75
        assert model["metrics_full"] == cm["metrics"]
        assert run["stage_history"][0]["status"] == "succeeded"
    finally:
        _cleanup_download_test(db, pid, rid)


def test_downloading_model_falls_back_to_last_pt():
    db = MongoClient(MONGO_URL)[DB_NAME]
    pid, rid, remote_run_dir, metrics_json_path = _seed_run_with_training(db)
    try:
        best_pt_path = f"{remote_run_dir}/train/weights/best.pt"
        last_pt_path = f"{remote_run_dir}/train/weights/last.pt"
        uploaded = {}

        def fake_put_object(path, data, content_type):
            uploaded["data"] = data
            return {"ok": True}

        fake_download = _fake_download_file_factory(
            {
                last_pt_path: b"LASTPTBYTES",
                metrics_json_path: json.dumps({"map50": 0.5, "map50_95": 0.4, "precision": 0.5, "recall": 0.5}),
            },
            fail_paths={best_pt_path},
        )

        with patch("deployment_worker.ssh_helper.connect") as mock_connect, \
             patch("deployment_worker.ssh_helper.download_file", side_effect=fake_download):
            mock_connect.return_value.__enter__.return_value = MagicMock()
            mock_connect.return_value.__exit__.return_value = False
            deployment_worker._pipeline_downloading_model_sync(rid, pid, 0, _DL_REQ, fake_put_object, "visionforge")

        assert uploaded["data"] == b"LASTPTBYTES"
        run = db.pipeline_runs.find_one({"id": rid})
        assert run["busy"] is False
        assert "candidate_model" in run
    finally:
        _cleanup_download_test(db, pid, rid)


def test_downloading_model_without_training_data_fails():
    db = MongoClient(MONGO_URL)[DB_NAME]
    pid = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    db.projects.insert_one({"id": pid, "name": "TEST_worker_no_training", "classes": [], "created_at": now})
    rid = str(uuid.uuid4())
    db.pipeline_runs.insert_one({
        "id": rid, "project_id": pid, "pipeline_id": str(uuid.uuid4()),
        "run_type": "bootstrap", "status": "downloading_model", "busy": True,
        "stage_history": [{"stage": "downloading_model", "status": "running", "started_at": now}],
        "created_at": now, "updated_at": now,
    })
    try:
        with patch("deployment_worker.ssh_helper.connect") as mock_connect:
            deployment_worker._pipeline_downloading_model_sync(
                rid, pid, 0, _DL_REQ, lambda *a: None, "visionforge",
            )
            mock_connect.assert_not_called()

        run = db.pipeline_runs.find_one({"id": rid})
        assert run["status"] == "failed"
        assert "training_remote" in run["error"]
    finally:
        _cleanup_download_test(db, pid, rid)


# --- _pipeline_deploying_sync() / _pipeline_rollback_sync() (M8) ------------
# The highest-risk worker in the feature (ROADMAP.md SS7): destructive,
# non-idempotent remote file rotation. These tests patch ssh_helper's
# individual module-level functions directly (matching every other test in
# this file's mocking granularity, not a deeper mock of paramiko's own
# SFTPClient/open_sftp()), each side_effect closing over one shared
# in-memory {path: bytes} dict so state persists across the multiple
# sequential calls within a test - the "mocked in-memory remote filesystem"
# the roadmap's own DoD asks for.

def _make_fake_remote_fs():
    fs = {}

    def fake_remote_exists(client, path):
        return path in fs

    def fake_remote_remove(client, path):
        if path not in fs:
            raise FileNotFoundError(path)
        del fs[path]

    def fake_remote_rename(client, old_path, new_path):
        if old_path not in fs:
            raise FileNotFoundError(old_path)
        if new_path in fs:
            # Mirrors real SFTP: plain rename cannot overwrite an existing
            # destination. A wrong operation ordering anywhere in the
            # implementation raises here instead of silently overwriting.
            raise OSError(f"{new_path} already exists")
        fs[new_path] = fs.pop(old_path)

    def fake_upload_file(client, local_path, remote_path, callback=None):
        with open(local_path, "rb") as f:
            fs[remote_path] = f.read()

    def fake_download_file(client, remote_path, local_path, callback=None):
        if remote_path not in fs:
            raise FileNotFoundError(remote_path)
        with open(local_path, "wb") as f:
            f.write(fs[remote_path])

    return fs, fake_remote_exists, fake_remote_remove, fake_remote_rename, fake_upload_file, fake_download_file


def _patch_ssh_fs(fs_fns):
    _fs, fake_exists, fake_remove, fake_rename, fake_upload, fake_download = fs_fns
    return (
        patch("deployment_worker.ssh_helper.remote_exists", side_effect=fake_exists),
        patch("deployment_worker.ssh_helper.remote_remove", side_effect=fake_remove),
        patch("deployment_worker.ssh_helper.remote_rename", side_effect=fake_rename),
        patch("deployment_worker.ssh_helper.upload_file", side_effect=fake_upload),
        patch("deployment_worker.ssh_helper.download_file", side_effect=fake_download),
    )


_DEPLOY_REQ_BASE = {"host": "h", "port": 22, "username": "u", "pem_key": None, "password": "p"}


def _seed_deployable_run(db, pid, run_type="fresh_production"):
    now = datetime.now(timezone.utc).isoformat()
    rid = str(uuid.uuid4())
    mid = str(uuid.uuid4())
    weights_path = f"visionforge/projects/{pid}/pipeline_runs/{rid}/candidate.pt"
    db.models.insert_one({
        "id": mid, "project_id": pid, "type": "pipeline_candidate", "is_active": False,
        "weights_path": weights_path, "created_at": now,
    })
    db.pipeline_runs.insert_one({
        "id": rid, "project_id": pid, "pipeline_id": str(uuid.uuid4()),
        "run_type": run_type, "status": "awaiting_approval", "busy": True,
        "approval": {"decision": "approved", "decided_by": "x", "decided_at": now, "note": None},
        "candidate_model": {"local_model_id": mid, "weights_path": weights_path, "metrics": {}},
        "stage_history": [{"stage": "deploying", "status": "running", "started_at": now}],
        "created_at": now, "updated_at": now,
    })
    return rid, mid, weights_path


def test_two_deploys_plus_rollback_rotate_backups_correctly():
    db = MongoClient(MONGO_URL)[DB_NAME]
    pid = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    db.projects.insert_one({"id": pid, "name": "TEST_worker_deploy", "classes": ["widget"], "created_at": now})

    prod = "/srv/models/prod.pt"
    fs_fns = _make_fake_remote_fs()
    fs = fs_fns[0]
    fs[prod] = b"ORIGINAL_LIVE_MODEL"
    object_store = {}

    def fake_get_object(storage_path):
        return object_store[storage_path], "application/octet-stream"

    patchers = _patch_ssh_fs(fs_fns)
    try:
        for p in patchers:
            p.start()
        with patch("deployment_worker.ssh_helper.connect") as mock_connect:
            mock_connect.return_value.__enter__.return_value = MagicMock()
            mock_connect.return_value.__exit__.return_value = False

            # --- Deploy A: nothing backed up yet ---
            rid_a, mid_a, weights_a = _seed_deployable_run(db, pid)
            object_store[weights_a] = b"MODEL_A"
            deployment_worker._pipeline_deploying_sync(
                rid_a, pid, 0, {**_DEPLOY_REQ_BASE, "remote_production_model_path": prod}, fake_get_object,
            )
            assert fs[prod] == b"MODEL_A"
            assert fs[f"{prod}.bak1"] == b"ORIGINAL_LIVE_MODEL"
            assert f"{prod}.bak2" not in fs
            run_a = db.pipeline_runs.find_one({"id": rid_a})
            assert run_a["status"] == "completed"
            assert run_a["deploy"]["current_became_bak1"] is True
            assert run_a["deploy"]["bak1_became_bak2"] is False
            assert run_a["deploy"]["bak2_dropped"] is False
            assert db.models.find_one({"id": mid_a})["is_active"] is True

            # --- Deploy B: full three-way rotation ---
            rid_b, mid_b, weights_b = _seed_deployable_run(db, pid)
            object_store[weights_b] = b"MODEL_B"
            deployment_worker._pipeline_deploying_sync(
                rid_b, pid, 0, {**_DEPLOY_REQ_BASE, "remote_production_model_path": prod}, fake_get_object,
            )
            assert fs[prod] == b"MODEL_B"
            assert fs[f"{prod}.bak1"] == b"MODEL_A"
            assert fs[f"{prod}.bak2"] == b"ORIGINAL_LIVE_MODEL"
            run_b = db.pipeline_runs.find_one({"id": rid_b})
            assert run_b["deploy"]["current_became_bak1"] is True
            assert run_b["deploy"]["bak1_became_bak2"] is True
            assert run_b["deploy"]["bak2_dropped"] is False
            # B's activation deactivates A
            assert db.models.find_one({"id": mid_a})["is_active"] is False
            assert db.models.find_one({"id": mid_b})["is_active"] is True

            # --- Rollback: single swap, no .bak2 cascade ---
            rid_r = str(uuid.uuid4())
            db.pipeline_runs.insert_one({
                "id": rid_r, "project_id": pid, "pipeline_id": str(uuid.uuid4()),
                "run_type": "rollback", "status": "deploying", "busy": True,
                "stage_history": [{"stage": "deploying", "status": "running", "started_at": now}],
                "created_at": now, "updated_at": now,
            })
            deployment_worker._pipeline_rollback_sync(
                rid_r, pid, 0, {**_DEPLOY_REQ_BASE, "remote_production_model_path": prod},
            )
            assert fs[prod] == b"MODEL_A"  # restored to what B overwrote
            assert fs[f"{prod}.bak1"] == b"MODEL_B"  # what was overwritten is now backed up
            assert fs[f"{prod}.bak2"] == b"ORIGINAL_LIVE_MODEL"  # untouched - no cascade
            run_r = db.pipeline_runs.find_one({"id": rid_r})
            assert run_r["status"] == "completed"
            assert run_r["deploy"] == {"rolled_back": True, "deployed_at": run_r["deploy"]["deployed_at"]}
            # Rollback does not touch local activation state (ROADMAP.md SS3)
            assert db.models.find_one({"id": mid_b})["is_active"] is True
    finally:
        for p in patchers:
            p.stop()
        db.projects.delete_one({"id": pid})
        db.models.delete_many({"project_id": pid})
        db.pipeline_runs.delete_many({"project_id": pid})


def test_deploying_without_candidate_model_fails():
    db = MongoClient(MONGO_URL)[DB_NAME]
    pid = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    db.projects.insert_one({"id": pid, "name": "TEST_worker_deploy_nocand", "classes": [], "created_at": now})
    rid = str(uuid.uuid4())
    db.pipeline_runs.insert_one({
        "id": rid, "project_id": pid, "pipeline_id": str(uuid.uuid4()),
        "run_type": "fresh_production", "status": "awaiting_approval", "busy": True,
        "stage_history": [{"stage": "deploying", "status": "running", "started_at": now}],
        "created_at": now, "updated_at": now,
    })
    try:
        with patch("deployment_worker.ssh_helper.connect") as mock_connect:
            deployment_worker._pipeline_deploying_sync(
                rid, pid, 0, {**_DEPLOY_REQ_BASE, "remote_production_model_path": "/srv/models/prod.pt"},
                lambda *a: (b"x", "application/octet-stream"),
            )
            mock_connect.assert_not_called()
        run = db.pipeline_runs.find_one({"id": rid})
        assert run["status"] == "failed"
        assert "downloading_model" in run["error"]
    finally:
        db.projects.delete_one({"id": pid})
        db.pipeline_runs.delete_one({"id": rid})


def test_rollback_without_bak1_fails_and_writes_nothing():
    db = MongoClient(MONGO_URL)[DB_NAME]
    pid = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    db.projects.insert_one({"id": pid, "name": "TEST_worker_rollback_nobak", "classes": [], "created_at": now})
    rid = str(uuid.uuid4())
    db.pipeline_runs.insert_one({
        "id": rid, "project_id": pid, "pipeline_id": str(uuid.uuid4()),
        "run_type": "rollback", "status": "deploying", "busy": True,
        "stage_history": [{"stage": "deploying", "status": "running", "started_at": now}],
        "created_at": now, "updated_at": now,
    })
    fs_fns = _make_fake_remote_fs()
    fs = fs_fns[0]
    prod = "/srv/models/prod.pt"
    fs[prod] = b"CURRENT"
    patchers = _patch_ssh_fs(fs_fns)
    try:
        for p in patchers:
            p.start()
        with patch("deployment_worker.ssh_helper.connect") as mock_connect:
            mock_connect.return_value.__enter__.return_value = MagicMock()
            mock_connect.return_value.__exit__.return_value = False
            deployment_worker._pipeline_rollback_sync(
                rid, pid, 0, {**_DEPLOY_REQ_BASE, "remote_production_model_path": prod},
            )
        run = db.pipeline_runs.find_one({"id": rid})
        assert run["status"] == "failed"
        assert "roll back" in run["error"]
        assert fs[prod] == b"CURRENT"  # untouched
    finally:
        for p in patchers:
            p.stop()
        db.projects.delete_one({"id": pid})
        db.pipeline_runs.delete_one({"id": rid})


def _fake_client_for_env(existing_paths, import_exit=0):
    """Returns (client, commands) - commands collects every shell command run."""
    commands = []

    def _exec(client, cmd, timeout=None):
        commands.append(cmd)
        return (import_exit if "import ultralytics" in cmd else 0), "", ""

    def _stream(client, cmd, on_output=None, **kw):
        commands.append(cmd)
        return 0, "", ""

    return _exec, _stream, commands


def test_prepare_python_modes():
    client = MagicMock()
    assert deployment_worker._prepare_python(client, "system", None, "/w", lambda _c: None) == "python3"


def test_prepare_python_existing_sources_activate_for_folder_or_script():
    seen = []

    def _exec(client, cmd, timeout=None):
        seen.append(cmd)
        return 0, "/venvs/training/bin/python\n", ""

    for given in ("/venvs/training", "/venvs/training/bin/activate", "/venvs/training/"):
        with patch.object(ssh_helper, "remote_exists", return_value=True), \
             patch.object(ssh_helper, "exec_command", side_effect=_exec):
            py = deployment_worker._prepare_python(MagicMock(), "existing", given, "/w", lambda _c: None)
        assert py == "source /venvs/training/bin/activate && python"
    assert any("import ultralytics" in c and "source /venvs/training/bin/activate" in c for c in seen)


def test_prepare_python_existing_errors():
    client = MagicMock()
    with patch.object(ssh_helper, "remote_exists", return_value=False):
        try:
            deployment_worker._prepare_python(client, "existing", "/nope", "/w", lambda _c: None)
            assert False, "expected missing activate to raise"
        except Exception as e:
            assert "/nope/bin/activate" in str(e)

    def _no_ultralytics(c, cmd, timeout=None):
        return (1, "", "") if "import ultralytics" in cmd else (0, "python", "")

    with patch.object(ssh_helper, "remote_exists", return_value=True), \
         patch.object(ssh_helper, "exec_command", side_effect=_no_ultralytics):
        try:
            deployment_worker._prepare_python(client, "existing", "/v", "/w", lambda _c: None)
            assert False, "expected missing ultralytics to raise"
        except Exception as e:
            assert "ultralytics" in str(e)


def test_prepare_python_create_makes_venv_and_installs():
    exec_fn, stream_fn, commands = _fake_client_for_env(set(), import_exit=1)
    with patch.object(ssh_helper, "remote_exists", return_value=False), \
         patch.object(ssh_helper, "exec_command", side_effect=exec_fn), \
         patch.object(ssh_helper, "exec_command_streaming", side_effect=stream_fn):
        py = deployment_worker._prepare_python(MagicMock(), "create", None, "/w/", lambda _c: None)
    assert py == "/w/venv/bin/python"
    assert any("-m venv /w/venv" in c for c in commands)
    assert any("pip install" in c and "ultralytics" in c for c in commands)


def test_prepare_python_create_skips_install_when_present():
    exec_fn, stream_fn, commands = _fake_client_for_env(set(), import_exit=0)
    with patch.object(ssh_helper, "remote_exists", return_value=True), \
         patch.object(ssh_helper, "exec_command", side_effect=exec_fn), \
         patch.object(ssh_helper, "exec_command_streaming", side_effect=stream_fn):
        deployment_worker._prepare_python(MagicMock(), "create", None, "/w", lambda _c: None)
    assert not any("pip install" in c for c in commands)


def test_ensure_usable_yaml_writes_fixed_copy_when_path_is_stale():
    import yaml as _yaml

    written = {}

    class _F:
        def __init__(self, p): self.p = p
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def write(self, text): written[self.p] = text

    sftp = MagicMock()
    sftp.open.side_effect = lambda p, mode: _F(p)
    client = MagicMock()
    client.open_sftp.return_value = sftp

    cfg = {"path": "/tmp/pipeline_upload_x", "train": "train/images", "val": "valid/images", "names": ["a"]}
    out = deployment_worker._ensure_usable_yaml(client, cfg, "/srv/ds/data.yaml", "/srv/ds", lambda p: False)
    assert out == "/srv/ds/data.fixed.yaml"
    assert _yaml.safe_load(written[out])["path"] == "/srv/ds"

    # a healthy yaml is left alone
    assert deployment_worker._ensure_usable_yaml(
        client, cfg, "/srv/ds/data.yaml", "/srv/ds", lambda p: True
    ) == "/srv/ds/data.yaml"


def test_upload_into_existing_skips_known_images_refreshes_labels_and_keeps_split(tmp_path):
    # remote already has img "a" in train; local run assigned it to valid.
    for split in ("train", "valid"):
        (tmp_path / split / "images").mkdir(parents=True)
        (tmp_path / split / "labels").mkdir(parents=True)
    (tmp_path / "valid" / "images" / "a.jpg").write_bytes(b"x")
    (tmp_path / "valid" / "labels" / "a.txt").write_text("0 .5 .5 .1 .1")
    (tmp_path / "train" / "images" / "b.jpg").write_bytes(b"y")
    (tmp_path / "train" / "labels" / "b.txt").write_text("0 .5 .5 .2 .2")

    remote = {"/d/train/images": ["a.jpg"], "/d/valid/images": []}
    puts = []
    sftp = MagicMock()
    sftp.listdir.side_effect = lambda p: remote[p]
    sftp.put.side_effect = lambda local, dest: puts.append(dest)
    client = MagicMock()
    client.open_sftp.return_value = sftp
    split_dirs = {
        "train": ("/d/train/images", "/d/train/labels"),
        "valid": ("/d/valid/images", "/d/valid/labels"),
    }
    with patch.object(ssh_helper, "_ensure_remote_dir"):
        deployment_worker._upload_into_existing(client, str(tmp_path), split_dirs)

    assert "/d/train/images/a.jpg" not in puts and "/d/valid/images/a.jpg" not in puts  # not re-uploaded
    assert "/d/train/labels/a.txt" in puts  # label refreshed where the image already lives
    assert "/d/train/images/b.jpg" in puts and "/d/train/labels/b.txt" in puts


def test_training_remote_resolves_relative_workdir_and_reports_stderr():
    db = MongoClient(MONGO_URL)[DB_NAME]
    pid, rid = _seed_run_with_dataset_export(db, run_type="bootstrap")
    try:
        uploaded = {}

        def fake_stream(client, cmd, on_output=None, **kwargs):
            return 1, "", "FileNotFoundError: best.pt missing"

        sftp_client = MagicMock()
        sftp_client.open_sftp.return_value.normalize.return_value = "/root"
        with patch("deployment_worker.ssh_helper.connect") as mock_connect, \
             patch("deployment_worker.ssh_helper.upload_file",
                   side_effect=lambda c, local, remote, **kw: uploaded.update(script=remote)), \
             patch("deployment_worker.ssh_helper.exec_command_streaming", side_effect=fake_stream):
            mock_connect.return_value.__enter__.return_value = sftp_client
            mock_connect.return_value.__exit__.return_value = False
            deployment_worker._pipeline_training_remote_sync(
                rid, pid, 0, {**_BASE_REQ, "remote_workdir": "testing_pipeline"}
            )

        assert uploaded["script"] == f"/root/testing_pipeline/runs/run_{rid[:8]}/train_eval.py"
        run = db.pipeline_runs.find_one({"id": rid})
        assert run["status"] == "failed"
        assert "FileNotFoundError: best.pt missing" in run["error"]
    finally:
        _cleanup(db, pid, rid)
