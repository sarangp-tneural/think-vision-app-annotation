"""Per-stage background workers for the Deploy Pipeline feature.

Mirrors backend/server.py's _train_yolo_sync pattern exactly: each worker
runs inside a thread (via asyncio.to_thread from routers/deployment.py) and
owns its own synchronous pymongo client, since Motor (the app's async client)
isn't thread-safe. Dependencies genuinely owned by server.py (get_object) are
passed in as plain call-time arguments by routers/deployment.py's register(s)
closure, rather than imported here - see backend/pipeline_logic.py and
backend/ssh_helper.py for the same "no FastAPI/server.py imports" discipline.
"""
import logging
import os
import random
import re
import shlex
import shutil
import tempfile
from datetime import datetime, timezone
from typing import Optional

from pymongo import MongoClient

import pipeline_logic
import ssh_helper
from pipeline_logic import SPLITS

MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]

logger = logging.getLogger(__name__)

# Anchored to the start of the line: Ultralytics' per-epoch rows begin with a
# bare "N/Total" token (no literal word "Epoch" on that line - only the
# header row has that). Anchoring avoids false-matching other N/N-shaped
# columns (box counts, etc.) later in the same row.
_EPOCH_RE = re.compile(r"^\s*(\d+)/(\d+)\s")
# Roadmap doesn't specify a cap; an unbounded log for a long training run
# would grow the run doc without limit.
_LOG_TAIL_CAP = 4000


def _yolo_label_line(a: dict, cls_to_idx: dict) -> Optional[str]:
    """Annotation -> one YOLO detection line ("cls cx cy w h"), or None to
    skip. A third, deliberately independent copy of the same conversion
    logic in server.py's _yolo_label_line / _train_yolo_sync - this worker
    runs in its own thread with its own sync Mongo client and must not import
    anything from server.py (see module docstring), so duplicating this small
    pure helper is cheaper and safer than trying to share it."""
    label = a.get("label")
    if label not in cls_to_idx:
        return None
    idx = cls_to_idx[label]
    t = a.get("type", "bbox")
    if t == "polygon" and a.get("points"):
        pts = a["points"]
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        x, y = min(xs), min(ys)
        w, h = max(xs) - x, max(ys) - y
    elif t == "ellipse":
        cx0, cy0 = a.get("x", 0), a.get("y", 0)
        rx, ry = a.get("rx", 0), a.get("ry", 0)
        x, y, w, h = cx0 - rx, cy0 - ry, 2 * rx, 2 * ry
    else:  # bbox / polyline fallback (rotation, if any, is ignored)
        x, y, w, h = a.get("x", 0), a.get("y", 0), a.get("w", 0), a.get("h", 0)
    if w <= 0 or h <= 0:
        return None
    cx, cy = x + w / 2, y + h / 2
    return f"{idx} {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}"


def _build_split_dataset_dir_sync(sync_db, project_id: str, classes: list,
                                   train_pct: float, valid_pct: float, test_pct: float,
                                   get_object_fn) -> tuple:
    """Sync counterpart of server.py's _build_split_dataset_dir - same
    {split}/{images,labels}/ layout and shuffle/cut logic, but using a sync
    pymongo db handle and an injected get_object_fn instead of the module
    Motor db (which cannot safely be touched from this worker thread)."""
    images = list(sync_db.images.find({"project_id": project_id, "annotated": True}, {"_id": 0}).limit(2000))
    cls_to_idx = {c: i for i, c in enumerate(classes)}

    tmp_root = tempfile.mkdtemp(prefix=f"pipeline_upload_{project_id}_")
    split_dirs = {}
    for split in SPLITS:
        img_dir = os.path.join(tmp_root, split, "images")
        lbl_dir = os.path.join(tmp_root, split, "labels")
        os.makedirs(img_dir, exist_ok=True)
        os.makedirs(lbl_dir, exist_ok=True)
        split_dirs[split] = (img_dir, lbl_dir)

    random.shuffle(images)
    n_train = int(len(images) * train_pct)
    n_valid = int(len(images) * valid_pct)
    # The remainder (not int(len * test_pct)) becomes test, so no image is
    # lost to rounding.
    assignments = (
        [(img, "train") for img in images[:n_train]]
        + [(img, "valid") for img in images[n_train:n_train + n_valid]]
        + [(img, "test") for img in images[n_train + n_valid:]]
    )

    counts = {"train": 0, "valid": 0, "test": 0}
    test_image_ids = []
    for img, split in assignments:
        img_dir, lbl_dir = split_dirs[split]
        try:
            data, _ct = get_object_fn(img["storage_path"])
            if not data:
                raise Exception("empty image data")
            lines = [
                line for a in img.get("annotations", [])
                if (line := _yolo_label_line(a, cls_to_idx)) is not None
            ]
            ext = (img["filename"].rsplit(".", 1)[-1] if "." in img["filename"] else "jpg").lower()
            with open(os.path.join(img_dir, f"{img['id']}.{ext}"), "wb") as f:
                f.write(data)
            with open(os.path.join(lbl_dir, f"{img['id']}.txt"), "w") as f:
                f.write("\n".join(lines))
            counts[split] += 1
            if split == "test":
                test_image_ids.append(img["id"])
        except Exception:
            # Skip the whole pair on failure - a label with no image is
            # worse than useless for training (same as server.py's version).
            pass

    yaml_path = os.path.join(tmp_root, "data.yaml")
    with open(yaml_path, "w") as f:
        f.write(
            f"path: {tmp_root}\ntrain: train/images\nval: valid/images\ntest: test/images\n"
            f"nc: {len(classes)}\nnames: {classes}\n"
        )

    return tmp_root, {
        "train_count": counts["train"],
        "valid_count": counts["valid"],
        "test_count": counts["test"],
        "test_image_ids": test_image_ids,
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pipeline_uploading_data_sync(run_id: str, project_id: str, history_index: int,
                                   req: dict, get_object_fn) -> None:
    """Builds the split dataset dir (M2's layout) and SCPs it to
    {remote_workdir}/{run_id}/dataset, then populates dataset_export. Mirrors
    _train_yolo_sync's thread-owns-its-own-client shape exactly."""
    sync_client = MongoClient(MONGO_URL)
    sync_db = sync_client[DB_NAME]
    tmp_root = None
    try:
        project = sync_db.projects.find_one({"id": project_id})
        classes = (project or {}).get("classes", [])

        tmp_root, split_info = _build_split_dataset_dir_sync(
            sync_db, project_id, classes,
            req["train_pct"], req["valid_pct"], req["test_pct"], get_object_fn,
        )

        remote_upload_path = f"{req['remote_workdir']}/{run_id}/dataset"
        with ssh_helper.connect(req["host"], req["port"], req["username"],
                                 req.get("pem_key"), req.get("password")) as client:
            ssh_helper.upload_dir(client, tmp_root, remote_upload_path)

        finished = _now_iso()
        sync_db.pipeline_runs.update_one(
            {"id": run_id},
            {"$set": {
                "busy": False,
                "updated_at": finished,
                "dataset_export": {
                    "train_count": split_info["train_count"],
                    "valid_count": split_info["valid_count"],
                    "test_count": split_info["test_count"],
                    "test_image_ids": split_info["test_image_ids"],
                    "remote_upload_path": remote_upload_path,
                },
                f"stage_history.{history_index}.status": "succeeded",
                f"stage_history.{history_index}.finished_at": finished,
            }},
        )
    except Exception as e:
        finished = _now_iso()
        sync_db.pipeline_runs.update_one(
            {"id": run_id},
            {"$set": {
                "busy": False,
                "updated_at": finished,
                "status": "failed",
                "error": str(e)[:500],
                f"stage_history.{history_index}.status": "failed",
                f"stage_history.{history_index}.finished_at": finished,
            }},
        )
    finally:
        if tmp_root:
            shutil.rmtree(tmp_root, ignore_errors=True)
        sync_client.close()


def _pipeline_training_remote_sync(run_id: str, project_id: str, history_index: int, req: dict) -> None:
    """Renders the train/eval script (pipeline_logic.render_train_eval_script),
    SCPs it to the remote host (the data.yaml it references was already
    uploaded by _pipeline_uploading_data_sync - M4 - so it isn't re-uploaded
    here), runs it, and streams live progress back by regex-scraping the
    remote process's stdout. This is the SSH-log-scraping analogue of
    _train_yolo_sync's in-process Ultralytics callback - necessarily a
    different mechanism, since a remote process can only be observed through
    what it prints, not a Python callback registered in-process."""
    sync_client = MongoClient(MONGO_URL)
    sync_db = sync_client[DB_NAME]
    started_at = _now_iso()
    local_script_path = None
    try:
        run = sync_db.pipeline_runs.find_one({"id": run_id})
        if not run:
            raise Exception("pipeline run not found")

        dataset_export = run.get("dataset_export") or {}
        remote_upload_path = dataset_export.get("remote_upload_path")
        if not remote_upload_path:
            raise Exception("no dataset_export.remote_upload_path - run uploading_data first")
        data_yaml_path = f"{remote_upload_path}/data.yaml"

        # Branching (the M5 DoD's explicit test target): bootstrap/
        # fresh_production start from the base checkpoint; merge continues
        # from whatever's currently live on the remote host.
        if run.get("run_type") in ("bootstrap", "fresh_production"):
            checkpoint_path = req["remote_base_model_path"]
        else:
            checkpoint_path = req["remote_production_model_path"]
            if not checkpoint_path:
                raise Exception("merge run requires remote_production_model_path")

        project_dir = f"{req['remote_workdir']}/{run_id}/runs"
        run_name = "train"
        remote_script_path = f"{req['remote_workdir']}/{run_id}/train_eval.py"
        metrics_json_path = f"{req['remote_workdir']}/{run_id}/metrics.json"

        params = {
            "checkpoint_path": checkpoint_path,
            "data_yaml_path": data_yaml_path,
            "epochs": req["epochs"],
            "project_dir": project_dir,
            "run_name": run_name,
            "metrics_json_path": metrics_json_path,
        }
        script_text = pipeline_logic.render_train_eval_script(params)

        fd, local_script_path = tempfile.mkstemp(prefix=f"train_eval_{run_id}_", suffix=".py")
        with os.fdopen(fd, "w") as f:
            f.write(script_text)

        progress = {"current_epoch": 0, "log_tail": ""}

        def _on_output(chunk: str) -> None:
            progress["log_tail"] = (progress["log_tail"] + chunk)[-_LOG_TAIL_CAP:]
            for line in chunk.splitlines():
                m = _EPOCH_RE.match(line)
                if not m:
                    continue
                current, total = int(m.group(1)), int(m.group(2))
                if current == progress["current_epoch"]:
                    continue
                progress["current_epoch"] = current
                if total != req["epochs"]:
                    # Scraped stdout is observation, not a contract - log,
                    # don't raise, and keep using the scraped total for pct.
                    logger.warning(
                        "run %s: scraped total epochs %s != requested %s", run_id, total, req["epochs"]
                    )
                sync_db.pipeline_runs.update_one(
                    {"id": run_id},
                    {"$set": {
                        "training.current_epoch": current,
                        "training.progress_pct": round(100 * current / total) if total else 0,
                        "training.log_tail": progress["log_tail"],
                        "updated_at": _now_iso(),
                    }},
                )

        with ssh_helper.connect(req["host"], req["port"], req["username"],
                                 req.get("pem_key"), req.get("password")) as client:
            ssh_helper.upload_file(client, local_script_path, remote_script_path)
            exit_code, _out, _err = ssh_helper.exec_command_streaming(
                client, f"python3 {shlex.quote(remote_script_path)}", on_output=_on_output,
            )

        if exit_code != 0:
            raise Exception(f"remote training script exited {exit_code}: {progress['log_tail'][-500:]}")

        finished = _now_iso()
        sync_db.pipeline_runs.update_one(
            {"id": run_id},
            {"$set": {
                "busy": False,
                "updated_at": finished,
                "training": {
                    "base_checkpoint_ref": checkpoint_path,
                    "remote_run_dir": project_dir,
                    "hyperparams_used": params,
                    "progress_pct": 100,
                    "log_tail": progress["log_tail"],
                    "started_at": started_at,
                    "finished_at": finished,
                },
                f"stage_history.{history_index}.status": "succeeded",
                f"stage_history.{history_index}.finished_at": finished,
            }},
        )
    except Exception as e:
        finished = _now_iso()
        sync_db.pipeline_runs.update_one(
            {"id": run_id},
            {"$set": {
                "busy": False,
                "updated_at": finished,
                "status": "failed",
                "error": str(e)[:500],
                f"stage_history.{history_index}.status": "failed",
                f"stage_history.{history_index}.finished_at": finished,
            }},
        )
    finally:
        if local_script_path and os.path.exists(local_script_path):
            os.remove(local_script_path)
        sync_client.close()
