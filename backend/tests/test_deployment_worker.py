"""Unit tests for deployment_worker.py: calls _pipeline_uploading_data_sync
directly (no live server, no HTTP) with ssh_helper mocked (matching
test_ssh_helper.py's style) and a fake get_object_fn (no real object
storage), but against a REAL local MongoDB connection (matching
test_deployment_pipeline.py's style) to seed a projects/pipeline_runs doc
beforehand and assert the actual written fields afterward. This is the
natural continuation of test_deployment_pipeline.py's own stated split: pure/
no-live-route modules get their own mocked/direct-import test file, exactly
like ssh_helper.py and pipeline_logic.py already do - deployment_worker.py's
functions are invoked from a background thread, not a route handler, so there
is no live HTTP path to test them through anyway.
"""
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
            assert mock_upload_dir.call_args.args[2] == f"/srv/work/{rid}/dataset"

        run = db.pipeline_runs.find_one({"id": rid})
        assert run["busy"] is False
        de = run["dataset_export"]
        assert de["train_count"] + de["valid_count"] + de["test_count"] == 10
        assert de["train_count"] == 7
        assert de["valid_count"] == 2
        assert de["test_count"] == 1
        assert len(de["test_image_ids"]) == de["test_count"]
        assert de["remote_upload_path"] == f"/srv/work/{rid}/dataset"
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
