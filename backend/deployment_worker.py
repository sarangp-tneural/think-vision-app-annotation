"""Per-stage background workers for the Deploy Pipeline feature.

Mirrors backend/server.py's _train_yolo_sync pattern exactly: each worker
runs inside a thread (via asyncio.to_thread from routers/deployment.py) and
owns its own synchronous pymongo client, since Motor (the app's async client)
isn't thread-safe. Dependencies genuinely owned by server.py (get_object) are
passed in as plain call-time arguments by routers/deployment.py's register(s)
closure, rather than imported here - see backend/pipeline_logic.py and
backend/ssh_helper.py for the same "no FastAPI/server.py imports" discipline.
"""
import json
import logging
import os
import posixpath
import random
import re
import shlex
import shutil
import tempfile
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

import yaml
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
                                   get_object_fn, label_classes: Optional[list] = None,
                                   yaml_root_path: Optional[str] = None,
                                   on_progress=None) -> tuple:
    """Sync counterpart of server.py's _build_split_dataset_dir - same
    {split}/{images,labels}/ layout and shuffle/cut logic, but using a sync
    pymongo db handle and an injected get_object_fn instead of the module
    Motor db (which cannot safely be touched from this worker thread)."""
    images = list(sync_db.images.find({"project_id": project_id, "annotated": True}, {"_id": 0}).limit(2000))
    # label_classes overrides the index order used in label files (merge mode
    # writes indices in the *remote* dataset's class order).
    cls_to_idx = {c: i for i, c in enumerate(label_classes if label_classes is not None else classes)}

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
    for n_done, (img, split) in enumerate(assignments):
        if on_progress:
            on_progress(n_done, len(assignments))
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
            f"path: {yaml_root_path or tmp_root}\ntrain: train/images\nval: valid/images\ntest: test/images\n"
            f"nc: {len(classes)}\nnames: {classes}\n"
        )

    return tmp_root, {
        "train_count": counts["train"],
        "valid_count": counts["valid"],
        "test_count": counts["test"],
        "test_image_ids": test_image_ids,
    }


def _ensure_usable_yaml(client, yaml_cfg: dict, yaml_path: str, yaml_dir: str, path_exists) -> str:
    """If the yaml's `path:` doesn't point at where its data really is (older
    uploads wrote a local temp dir there), write a corrected copy beside it
    (`data.fixed.yaml`) and return that path so training can resolve the
    splits. The original file is never modified. Returns yaml_path unchanged
    when no fix is needed."""
    if not yaml_cfg.get("path"):
        return yaml_path
    root = pipeline_logic.effective_yaml_root(yaml_cfg, yaml_dir, path_exists)
    declared = str(yaml_cfg["path"]).rstrip("/")
    declared_abs = declared if declared.startswith("/") else posixpath.normpath(posixpath.join(yaml_dir, declared))
    if root == declared_abs:
        return yaml_path
    fixed_path = f"{yaml_dir}/data.fixed.yaml"
    fixed_cfg = {**yaml_cfg, "path": root}
    sftp = client.open_sftp()
    try:
        with sftp.open(fixed_path, "w") as f:
            f.write(yaml.safe_dump(fixed_cfg, sort_keys=False))
    finally:
        sftp.close()
    return fixed_path


def _make_progress_reporter(sync_db, run_id: str, min_interval: float = 0.5):
    """Returns report(phase, done, total) that writes run.upload_progress,
    throttled so a 2000-image upload doesn't hammer Mongo (the final
    done==total update is always written)."""
    last = {"t": 0.0}

    def report(phase: str, done: int, total: int) -> None:
        now = time.monotonic()
        if done < total and now - last["t"] < min_interval:
            return
        last["t"] = now
        sync_db.pipeline_runs.update_one(
            {"id": run_id},
            {"$set": {"upload_progress": {"phase": phase, "done": done, "total": total}}},
        )

    return report


def _count_local_files(root: str) -> int:
    return sum(len(files) for _r, _d, files in os.walk(root))


def _read_remote_text(client, path: str) -> str:
    exit_code, out, err = ssh_helper.exec_command(client, f"cat {shlex.quote(path)}")
    if exit_code != 0:
        raise Exception(f"cat {path} failed (exit {exit_code}): {err.strip()}")
    return out


def _count_remote_files(client, path: str) -> int:
    exit_code, out, _err = ssh_helper.exec_command(
        client, f"find {shlex.quote(path)} -maxdepth 1 -type f 2>/dev/null | wc -l"
    )
    try:
        return int(out.strip()) if exit_code == 0 else 0
    except ValueError:
        return 0


def _upload_into_existing(client, tmp_root: str, split_dirs: dict, on_progress=None) -> None:
    """Uploads tmp_root/{split}/{images,labels}/* into the remote dirs of a
    pre-existing dataset. Never overwrites: a file already present remotely
    is left alone."""
    total = _count_local_files(tmp_root)
    done = 0
    sftp = client.open_sftp()
    try:
        for split, (remote_img, remote_lbl) in split_dirs.items():
            for sub, remote_dir in (("images", remote_img), ("labels", remote_lbl)):
                local_dir = os.path.join(tmp_root, split, sub)
                if not os.path.isdir(local_dir):
                    continue
                ssh_helper._ensure_remote_dir(sftp, remote_dir)
                for fname in os.listdir(local_dir):
                    remote_path = f"{remote_dir}/{fname}"
                    try:
                        sftp.stat(remote_path)
                    except FileNotFoundError:
                        sftp.put(os.path.join(local_dir, fname), remote_path)
                    done += 1
                    if on_progress:
                        on_progress(done, total)
    finally:
        sftp.close()


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

        dataset_mode = req.get("dataset_mode", "new")
        report = _make_progress_reporter(sync_db, run_id)
        report("preparing", 0, 1)
        connect_args = (req["host"], req["port"], req["username"],
                        req.get("pem_key"), req.get("password"))

        if dataset_mode == "new":
            remote_upload_path = f"{req['remote_workdir']}/{run_id}/dataset"
            tmp_root, split_info = _build_split_dataset_dir_sync(
                sync_db, project_id, classes,
                req["train_pct"], req["valid_pct"], req["test_pct"], get_object_fn,
                yaml_root_path=remote_upload_path,
                on_progress=lambda d, t: report("preparing", d, t),
            )
            data_yaml_path = f"{remote_upload_path}/data.yaml"
            total_files = _count_local_files(tmp_root)
            finished_files = set()

            def _on_file(local_path, sent, total):
                if sent >= total and local_path not in finished_files:
                    finished_files.add(local_path)
                    report("uploading", len(finished_files), total_files)

            report("uploading", 0, total_files)
            with ssh_helper.connect(*connect_args) as client:
                ssh_helper.upload_dir(client, tmp_root, remote_upload_path, callback=_on_file)
        else:
            data_yaml_path = req["existing_data_yaml_path"]
            remote_upload_path = posixpath.dirname(data_yaml_path)
            with ssh_helper.connect(*connect_args) as client:
                yaml_cfg = yaml.safe_load(_read_remote_text(client, data_yaml_path)) or {}
                remote_names = pipeline_logic.names_as_list(yaml_cfg)
                missing = [c for c in classes if c not in remote_names]
                if missing:
                    raise Exception(
                        f"classes missing from remote data.yaml names: {missing}"
                    )
                yaml_dir = posixpath.dirname(data_yaml_path)
                path_exists = lambda p: ssh_helper.remote_exists(client, p)  # noqa: E731
                split_dirs = pipeline_logic.resolve_yaml_split_dirs(yaml_cfg, yaml_dir, path_exists)
                data_yaml_path = _ensure_usable_yaml(
                    client, yaml_cfg, data_yaml_path, yaml_dir, path_exists
                )
                if dataset_mode == "merge":
                    if not split_dirs:
                        raise Exception("remote data.yaml has no train/val/test entries to merge into")
                    tmp_root, split_info = _build_split_dataset_dir_sync(
                        sync_db, project_id, classes,
                        req["train_pct"], req["valid_pct"], req["test_pct"], get_object_fn,
                        label_classes=remote_names,
                        on_progress=lambda d, t: report("preparing", d, t),
                    )
                    # A yaml with no distinct test dir folds the test split
                    # into val (resolve_yaml_split_dirs already did that).
                    _upload_into_existing(client, tmp_root, split_dirs,
                                          on_progress=lambda d, t: report("uploading", d, t))
                else:  # reuse: nothing uploaded
                    split_info = {
                        "train_count": _count_remote_files(client, split_dirs["train"][0]) if "train" in split_dirs else 0,
                        "valid_count": _count_remote_files(client, split_dirs["valid"][0]) if "valid" in split_dirs else 0,
                        "test_count": _count_remote_files(client, split_dirs["test"][0]) if "test" in split_dirs else 0,
                        # Local testing needs images we can fetch from storage;
                        # nothing was cut for the remote set, so use a local
                        # sample of annotated images (may overlap remote training data).
                        "test_image_ids": [
                            i["id"] for i in sync_db.images.find(
                                {"project_id": project_id, "annotated": True}, {"id": 1, "_id": 0}
                            ).limit(50)
                        ],
                    }

        finished = _now_iso()
        sync_db.pipeline_runs.update_one(
            {"id": run_id},
            {"$set": {
                "busy": False,
                "updated_at": finished,
                "upload_progress": None,
                "dataset_export": {
                    "train_count": split_info["train_count"],
                    "valid_count": split_info["valid_count"],
                    "test_count": split_info["test_count"],
                    "test_image_ids": split_info["test_image_ids"],
                    "remote_upload_path": remote_upload_path,
                    "data_yaml_path": data_yaml_path,
                    "dataset_mode": dataset_mode,
                    "classes": classes,
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


def _prepare_python(client, env_mode: str, venv_path: Optional[str], workdir: str,
                    on_output) -> str:
    """Returns the remote interpreter path to run the training script with,
    creating/validating a venv first when asked. Output of the setup steps is
    streamed through on_output so it shows in the run's log tail."""
    if env_mode == "system":
        return "python3"
    if env_mode == "existing":
        python_bin = f"{venv_path.rstrip('/')}/bin/python"
        if not ssh_helper.remote_exists(client, python_bin):
            raise Exception(f"no python interpreter at {python_bin} - check the venv path")
        return python_bin

    venv_dir = f"{workdir.rstrip('/')}/venv"
    python_bin = f"{venv_dir}/bin/python"
    if not ssh_helper.remote_exists(client, python_bin):
        on_output(f"Creating venv at {venv_dir}\n")
        exit_code, _o, err = ssh_helper.exec_command_streaming(
            client, f"mkdir -p {shlex.quote(workdir)} && python3 -m venv {shlex.quote(venv_dir)}",
            on_output=on_output,
        )
        if exit_code != 0:
            raise Exception(f"venv creation failed (exit {exit_code}): {err.strip()[-400:]}")
    exit_code, _o, _e = ssh_helper.exec_command(
        client, f"{shlex.quote(python_bin)} -c 'import ultralytics'"
    )
    if exit_code != 0:
        on_output("Installing ultralytics into venv (this can take a few minutes)\n")
        exit_code, _o, err = ssh_helper.exec_command_streaming(
            client, f"{shlex.quote(python_bin)} -m pip install --upgrade pip ultralytics",
            on_output=on_output,
        )
        if exit_code != 0:
            raise Exception(f"pip install failed (exit {exit_code}): {err.strip()[-400:]}")
    return python_bin


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
        data_yaml_path = dataset_export.get("data_yaml_path") or f"{remote_upload_path}/data.yaml"

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

        env_mode = req.get("env_mode", "system")
        with ssh_helper.connect(req["host"], req["port"], req["username"],
                                 req.get("pem_key"), req.get("password")) as client:
            python_bin = _prepare_python(client, env_mode, req.get("venv_path"),
                                          req["remote_workdir"], _on_output)
            ssh_helper.upload_file(client, local_script_path, remote_script_path)
            exit_code, _out, _err = ssh_helper.exec_command_streaming(
                client, f"{shlex.quote(python_bin)} -u {shlex.quote(remote_script_path)}",
                on_output=_on_output,
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
                    "env": {"mode": env_mode, "python": python_bin},
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


def _pipeline_downloading_model_sync(run_id: str, project_id: str, history_index: int,
                                      req: dict, put_object_fn, app_name: str) -> None:
    """SFTP-downloads the trained weights (best.pt, falling back to last.pt)
    and metrics.json that training_remote (M5) produced, uploads the weights
    into object storage, and registers a pipeline_candidate models doc so the
    existing, unmodified POST /models/{mid}/activate can activate it later
    (M8) exactly like a locally-trained model. No extra request fields are
    needed beyond SSH credentials - the remote run dir, run name, and
    metrics.json path were all already stored on the run doc by M5."""
    sync_client = MongoClient(MONGO_URL)
    sync_db = sync_client[DB_NAME]
    local_weights_path = None
    local_metrics_path = None
    try:
        run = sync_db.pipeline_runs.find_one({"id": run_id})
        if not run:
            raise Exception("pipeline run not found")

        training = run.get("training") or {}
        remote_run_dir = training.get("remote_run_dir")
        hyperparams_used = training.get("hyperparams_used") or {}
        run_name = hyperparams_used.get("run_name")
        metrics_json_path = hyperparams_used.get("metrics_json_path")
        if not remote_run_dir or not run_name or not metrics_json_path:
            raise Exception("no training data on run - run training_remote first")

        remote_weights_dir = f"{remote_run_dir}/{run_name}/weights"
        best_pt = f"{remote_weights_dir}/best.pt"
        last_pt = f"{remote_weights_dir}/last.pt"

        local_fd, local_weights_path = tempfile.mkstemp(prefix=f"candidate_{run_id}_", suffix=".pt")
        os.close(local_fd)
        local_metrics_fd, local_metrics_path = tempfile.mkstemp(prefix=f"metrics_{run_id}_", suffix=".json")
        os.close(local_metrics_fd)

        with ssh_helper.connect(req["host"], req["port"], req["username"],
                                 req.get("pem_key"), req.get("password")) as client:
            try:
                ssh_helper.download_file(client, best_pt, local_weights_path)
            except Exception:
                # Mirrors _train_yolo_sync's local best.pt-not-found ->
                # last.pt fallback (server.py:1489-1491), adapted for SFTP
                # where there's no cheap remote os.path.exists check.
                ssh_helper.download_file(client, last_pt, local_weights_path)
            ssh_helper.download_file(client, metrics_json_path, local_metrics_path)

        with open(local_weights_path, "rb") as f:
            weights_bytes = f.read()
        with open(local_metrics_path) as f:
            raw_metrics = json.load(f)
        # Rename to the mAP50/mAP50_95 casing ROADMAP.md's candidate_model
        # field and the "real" model shape both use - the remote script (M5)
        # intentionally kept eval.py's original lowercase map50/map50_95 keys.
        metrics = {
            "mAP50": float(raw_metrics.get("map50", 0)),
            "mAP50_95": float(raw_metrics.get("map50_95", 0)),
            "precision": float(raw_metrics.get("precision", 0)),
            "recall": float(raw_metrics.get("recall", 0)),
        }

        storage_path = f"{app_name}/projects/{project_id}/pipeline_runs/{run_id}/candidate.pt"
        put_object_fn(storage_path, weights_bytes, "application/octet-stream")

        # Snapshot from uploading_data (server.py:_train_yolo_sync's direct-
        # training counterpart to this same fix) - inference must decode this
        # candidate's predictions against the class list it was actually
        # trained with, not whatever the project's live class list has grown
        # to by the time someone auto-labels with it.
        training_classes = (run.get("dataset_export") or {}).get("classes", [])

        mid = str(uuid.uuid4())
        finished = _now_iso()
        sync_db.models.insert_one({
            "id": mid, "project_id": project_id, "type": "pipeline_candidate",
            "pipeline_run_id": run_id,
            "status": "trained", "is_active": False,
            "classes": training_classes,
            "weights_path": storage_path, "weights_size": len(weights_bytes),
            "final_mAP": metrics["mAP50"], "final_loss": 1 - metrics["mAP50_95"],
            "precision": metrics["precision"], "recall": metrics["recall"],
            "metrics_full": metrics,
            "created_at": finished, "completed_at": finished,
        })

        sync_db.pipeline_runs.update_one(
            {"id": run_id},
            {"$set": {
                "busy": False,
                "updated_at": finished,
                "candidate_model": {
                    "local_model_id": mid,
                    "weights_path": storage_path,
                    "metrics": metrics,
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
        for p in (local_weights_path, local_metrics_path):
            if p and os.path.exists(p):
                os.remove(p)
        sync_client.close()


def _download_weights_to_tempfile(get_object_fn, storage_path: str) -> str:
    data, _ct = get_object_fn(storage_path)
    fd, local_path = tempfile.mkstemp(prefix="eval_weights_", suffix=".pt")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return local_path


def _build_local_test_dataset_dir_sync(sync_db, test_image_ids: list, classes: list,
                                        get_object_fn) -> tuple:
    """Test-set-only local dataset builder - not a reuse of
    _build_split_dataset_dir_sync (M4), which cuts a fresh random three-way
    split; this one just materializes an already-decided list of
    test_image_ids locally so pipeline_logic.evaluate() has real files to
    read. train/val/test all point at the same directory since there's only
    one split here (see module docstring's ultralytics-not-installed caveat -
    deferred to the manual DoD)."""
    tmp_root = tempfile.mkdtemp(prefix="pipeline_test_eval_")
    img_dir = os.path.join(tmp_root, "test", "images")
    lbl_dir = os.path.join(tmp_root, "test", "labels")
    os.makedirs(img_dir, exist_ok=True)
    os.makedirs(lbl_dir, exist_ok=True)
    cls_to_idx = {c: i for i, c in enumerate(classes)}

    for img_id in test_image_ids:
        img = sync_db.images.find_one({"id": img_id})
        if not img:
            continue
        try:
            data, _ct = get_object_fn(img["storage_path"])
            if not data:
                raise Exception("empty image data")
            lines = [
                line for a in img.get("annotations", [])
                if (line := _yolo_label_line(a, cls_to_idx)) is not None
            ]
            ext = (img["filename"].rsplit(".", 1)[-1] if "." in img["filename"] else "jpg").lower()
            with open(os.path.join(img_dir, f"{img_id}.{ext}"), "wb") as f:
                f.write(data)
            with open(os.path.join(lbl_dir, f"{img_id}.txt"), "w") as f:
                f.write("\n".join(lines))
        except Exception as ie:
            logger.warning("Skip test image %s from local eval dataset: %s", img_id, ie)

    yaml_path = os.path.join(tmp_root, "data.yaml")
    with open(yaml_path, "w") as f:
        f.write(
            f"path: {yaml_root_path or tmp_root}\ntrain: test/images\nval: test/images\ntest: test/images\n"
            f"nc: {len(classes)}\nnames: {classes}\n"
        )
    return tmp_root, yaml_path


def _pipeline_testing_sync(run_id: str, project_id: str, history_index: int,
                            yolo_predict_fn, get_object_fn, send_tester_email_fn, app_url: str) -> None:
    """Runs local inference with the candidate model (M6) and the currently-
    active model over the run's own held-out test set, evaluates both
    (pipeline_logic.evaluate), feeds both mAP50s into decide_deploy(), and
    notifies reviewers. No SSH is needed here at all - unlike every prior
    real stage, everything this stage touches is already local or in object
    storage."""
    sync_client = MongoClient(MONGO_URL)
    sync_db = sync_client[DB_NAME]
    local_test_dir = None
    candidate_local_pt = None
    baseline_local_pt = None
    try:
        run = sync_db.pipeline_runs.find_one({"id": run_id})
        if not run:
            raise Exception("pipeline run not found")

        candidate = run.get("candidate_model") or {}
        candidate_weights_path = candidate.get("weights_path")
        if not candidate_weights_path:
            raise Exception("no candidate_model on run - run downloading_model first")

        test_image_ids = (run.get("dataset_export") or {}).get("test_image_ids") or []
        if not test_image_ids:
            raise Exception("no dataset_export.test_image_ids on run - run uploading_data first")

        project = sync_db.projects.find_one({"id": project_id}) or {}
        classes = project.get("classes", [])
        confidence = (project.get("settings") or {}).get("confidence_threshold", 0.4)

        local_test_dir, data_yaml_path = _build_local_test_dataset_dir_sync(
            sync_db, test_image_ids, classes, get_object_fn,
        )

        # No active model yet (e.g. this project's very first fresh_production
        # run) -> baseline is "beat nothing": all-zero metrics, gate trivially
        # passes since any real mAP50 is >= 0.
        baseline = sync_db.models.find_one({"project_id": project_id, "is_active": True, "status": "trained"})
        baseline_weights_path = baseline.get("weights_path") if baseline else None

        candidate_local_pt = _download_weights_to_tempfile(get_object_fn, candidate_weights_path)
        candidate_metrics = pipeline_logic.evaluate(candidate_local_pt, data_yaml_path, split="test")

        if baseline_weights_path:
            baseline_local_pt = _download_weights_to_tempfile(get_object_fn, baseline_weights_path)
            baseline_metrics = pipeline_logic.evaluate(baseline_local_pt, data_yaml_path, split="test")
        else:
            baseline_metrics = {"mAP50": 0.0, "mAP50_95": 0.0, "precision": 0.0, "recall": 0.0}

        decision_gate_passed = pipeline_logic.decide_deploy(baseline_metrics["mAP50"], candidate_metrics["mAP50"])

        # Each model decodes against its OWN training-time class list, not
        # the live project list - candidate and baseline may have been
        # trained at different times with different class snapshots.
        candidate_classes = (run.get("dataset_export") or {}).get("classes") or classes
        baseline_classes = (baseline.get("classes") if baseline else None) or classes

        sample_predictions = []
        for img_id in test_image_ids[:20]:
            img = sync_db.images.find_one({"id": img_id})
            if not img:
                continue
            data, _ct = get_object_fn(img["storage_path"])
            candidate_boxes = yolo_predict_fn(candidate_weights_path, data, candidate_classes, confidence)
            baseline_boxes = (
                yolo_predict_fn(baseline_weights_path, data, baseline_classes, confidence)
                if baseline_weights_path else []
            )
            sample_predictions.append({
                "image_id": img_id,
                "candidate_boxes": candidate_boxes,
                "baseline_boxes": baseline_boxes,
                "ground_truth": img.get("annotations", []),
            })

        # Best-effort: matches send_invite_email's own "fails silently"
        # philosophy - the stage's real job (inference + the gate) succeeds
        # regardless of email delivery.
        tester_notified_at = None
        if project.get("team_id"):
            try:
                members = list(sync_db.team_members.find({
                    "team_id": project["team_id"],
                    "role": {"$in": ["owner", "admin", "reviewer"]},
                    "status": "active",
                }))
                user_ids = [m["user_id"] for m in members]
                users = list(sync_db.users.find({"id": {"$in": user_ids}})) if user_ids else []
                to_emails = [u["email"] for u in users if u.get("email")]
                if to_emails:
                    send_tester_email_fn(to_emails, project.get("name", ""), project_id, run_id, app_url)
                    tester_notified_at = _now_iso()
            except Exception as ee:
                logger.warning("Tester notification email failed for run %s: %s", run_id, ee)

        finished = _now_iso()
        sync_db.pipeline_runs.update_one(
            {"id": run_id},
            {"$set": {
                "busy": False,
                "updated_at": finished,
                # awaiting_approval isn't itself a callable stage - nothing
                # else can produce it, so testing's own success is what
                # advances the run into the human-review state M8's
                # approve/reject endpoints are gated on.
                "status": "awaiting_approval",
                "testing": {
                    "sample_predictions": sample_predictions,
                    "tester_notified_at": tester_notified_at,
                },
                "baseline_comparison": {
                    "baseline_model_id": baseline.get("id") if baseline else None,
                    "baseline_metrics": baseline_metrics,
                    "delta_map50": candidate_metrics["mAP50"] - baseline_metrics["mAP50"],
                    "decision_gate_passed": decision_gate_passed,
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
        if local_test_dir:
            shutil.rmtree(local_test_dir, ignore_errors=True)
        for p in (candidate_local_pt, baseline_local_pt):
            if p and os.path.exists(p):
                os.remove(p)
        sync_client.close()


def _pipeline_deploying_sync(run_id: str, project_id: str, history_index: int,
                              req: dict, get_object_fn) -> None:
    """The highest-risk worker in the feature (ROADMAP.md SS7): destructive,
    non-idempotent remote file rotation. Order matters and is deliberate -
    each step clears the exact destination the next step's rename needs,
    since plain SFTP rename (unlike a POSIX mv) cannot overwrite an existing
    destination. Then activates the candidate locally, replicating
    POST /models/{mid}/activate's exact Mongo writes (routers/training.py) -
    not an HTTP self-call, since nothing in this codebase's background
    workers calls its own app's API; _log_activity is intentionally omitted,
    matching every prior sync worker's same already-accepted gap."""
    sync_client = MongoClient(MONGO_URL)
    sync_db = sync_client[DB_NAME]
    local_candidate_pt = None
    try:
        run = sync_db.pipeline_runs.find_one({"id": run_id})
        if not run:
            raise Exception("pipeline run not found")

        candidate = run.get("candidate_model") or {}
        candidate_weights_path = candidate.get("weights_path")
        local_model_id = candidate.get("local_model_id")
        if not candidate_weights_path or not local_model_id:
            raise Exception("no candidate_model on run - run downloading_model first")

        data, _ct = get_object_fn(candidate_weights_path)
        fd, local_candidate_pt = tempfile.mkstemp(suffix=".pt")
        with os.fdopen(fd, "wb") as f:
            f.write(data)

        prod = req["remote_production_model_path"]
        bak1, bak2 = f"{prod}.bak1", f"{prod}.bak2"
        deploy_info = {"current_became_bak1": False, "bak1_became_bak2": False, "bak2_dropped": False}

        with ssh_helper.connect(req["host"], req["port"], req["username"],
                                 req.get("pem_key"), req.get("password")) as client:
            if ssh_helper.remote_exists(client, bak2):
                ssh_helper.remote_remove(client, bak2)
                deploy_info["bak2_dropped"] = True
            if ssh_helper.remote_exists(client, bak1):
                ssh_helper.remote_rename(client, bak1, bak2)
                deploy_info["bak1_became_bak2"] = True
            if ssh_helper.remote_exists(client, prod):
                ssh_helper.remote_rename(client, prod, bak1)
                deploy_info["current_became_bak1"] = True
            ssh_helper.upload_file(client, local_candidate_pt, prod)

        # Activate locally - see docstring: replicates
        # POST /models/{mid}/activate's exact writes, doesn't call it.
        sync_db.models.update_many({"project_id": project_id}, {"$set": {"is_active": False}})
        finished = _now_iso()
        sync_db.models.update_one({"id": local_model_id}, {"$set": {"is_active": True, "activated_at": finished}})

        sync_db.deployment_pipelines.update_one(
            {"id": run["pipeline_id"]}, {"$set": {"last_deployed_run_id": run_id, "updated_at": finished}},
        )
        sync_db.pipeline_runs.update_one(
            {"id": run_id},
            {"$set": {
                "busy": False,
                "updated_at": finished,
                "status": "completed",
                "deploy": {
                    **deploy_info,
                    "deployed_at": finished,
                    "deployed_model_id": local_model_id,
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
        if local_candidate_pt and os.path.exists(local_candidate_pt):
            os.remove(local_candidate_pt)
        sync_client.close()


def _pipeline_rollback_sync(run_id: str, project_id: str, history_index: int, req: dict) -> None:
    """Download .bak1's bytes into a local buffer FIRST, before touching
    anything remotely - getting this ordering wrong (overwriting .bak1
    before reading it) is the single easiest bug in the whole feature.
    Single swap, no .bak2 cascade. Per ROADMAP.md SS3's activation rule,
    rollback does NOT touch models.is_active - only what's live remotely
    changes."""
    sync_client = MongoClient(MONGO_URL)
    sync_db = sync_client[DB_NAME]
    local_bak1_pt = None
    try:
        run = sync_db.pipeline_runs.find_one({"id": run_id})
        if not run:
            raise Exception("pipeline run not found")

        prod = req["remote_production_model_path"]
        bak1 = f"{prod}.bak1"

        with ssh_helper.connect(req["host"], req["port"], req["username"],
                                 req.get("pem_key"), req.get("password")) as client:
            if not ssh_helper.remote_exists(client, bak1):
                raise Exception(f"no {bak1} to roll back to")

            fd, local_bak1_pt = tempfile.mkstemp(suffix=".pt")
            os.close(fd)
            ssh_helper.download_file(client, bak1, local_bak1_pt)

            ssh_helper.remote_remove(client, bak1)
            if ssh_helper.remote_exists(client, prod):
                ssh_helper.remote_rename(client, prod, bak1)
            ssh_helper.upload_file(client, local_bak1_pt, prod)

        finished = _now_iso()
        sync_db.deployment_pipelines.update_one(
            {"id": run["pipeline_id"]}, {"$set": {"last_deployed_run_id": run_id, "updated_at": finished}},
        )
        sync_db.pipeline_runs.update_one(
            {"id": run_id},
            {"$set": {
                "busy": False,
                "updated_at": finished,
                "status": "completed",
                "deploy": {"rolled_back": True, "deployed_at": finished},
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
        if local_bak1_pt and os.path.exists(local_bak1_pt):
            os.remove(local_bak1_pt)
        sync_client.close()
