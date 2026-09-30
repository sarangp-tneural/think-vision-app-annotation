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
    """Merges tmp_root/{split}/{images,labels}/* into a pre-existing remote
    dataset. Images already present in ANY split dir (matched by file stem -
    ids are stable uuids) are not re-uploaded and never moved to another split
    (that would leak an image into both train and val); their label file is
    refreshed in the split where the image already lives, so edited
    annotations propagate. New images go to the split they were assigned."""
    sftp = client.open_sftp()
    try:
        existing = {}  # image stem -> split it already lives in
        for split, (remote_img, _lbl) in split_dirs.items():
            try:
                for name in sftp.listdir(remote_img):
                    existing.setdefault(name.rsplit(".", 1)[0], split)
            except FileNotFoundError:
                pass

        total = _count_local_files(tmp_root)
        done = 0
        for split in os.listdir(tmp_root):
            img_local = os.path.join(tmp_root, split, "images")
            lbl_local = os.path.join(tmp_root, split, "labels")
            if not os.path.isdir(img_local) or split not in split_dirs:
                continue
            for fname in os.listdir(img_local):
                stem = fname.rsplit(".", 1)[0]
                target = existing.get(stem, split)
                remote_img, remote_lbl = split_dirs[target]
                if stem not in existing:
                    ssh_helper._ensure_remote_dir(sftp, remote_img)
                    sftp.put(os.path.join(img_local, fname), f"{remote_img}/{fname}")
                    existing[stem] = target
                done += 1
                lbl_name = f"{stem}.txt"
                if os.path.exists(os.path.join(lbl_local, lbl_name)):
                    ssh_helper._ensure_remote_dir(sftp, remote_lbl)
                    sftp.put(os.path.join(lbl_local, lbl_name), f"{remote_lbl}/{lbl_name}")
                    done += 1
                if on_progress:
                    on_progress(done, total)
    finally:
        sftp.close()


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


_PATH_KEYS = (
    "remote_workdir", "venv_path", "start_from_path", "existing_data_yaml_path",
    "remote_base_model_path", "remote_production_model_path",
)


def _resolve_req_paths(req: dict, keys=_PATH_KEYS) -> dict:
    """Returns req with every non-absolute remote path in `keys` resolved
    against the remote home (see ssh_helper.resolve_remote_path). Opens an
    SSH session only if something actually needs resolving."""
    todo = [k for k in keys if (req.get(k) or "").strip() and not req[k].strip().startswith("/")]
    if not todo:
        return req
    with ssh_helper.connect(req["host"], req["port"], req["username"],
                             req.get("pem_key"), req.get("password")) as client:
        return {**req, **{k: ssh_helper.resolve_remote_path(client, req[k]) for k in todo}}


def _pipeline_uploading_data_sync(run_id: str, project_id: str, history_index: int,
                                   req: dict, get_object_fn) -> None:
    """Builds the split dataset dir (M2's layout) and SCPs it to
    {remote_workdir}/dataset, then populates dataset_export. Mirrors
    _train_yolo_sync's thread-owns-its-own-client shape exactly."""
    sync_client = MongoClient(MONGO_URL)
    sync_db = sync_client[DB_NAME]
    tmp_root = None
    try:
        project = sync_db.projects.find_one({"id": project_id})
        classes = (project or {}).get("classes", [])

        req = _resolve_req_paths(req)
        _run_doc = sync_db.pipeline_runs.find_one({"id": run_id}, {"pipeline_id": 1}) or {}
        if _run_doc.get("pipeline_id") and req.get("remote_workdir"):
            sync_db.deployment_pipelines.update_one(
                {"id": _run_doc["pipeline_id"]}, {"$set": {"remote_workdir": req["remote_workdir"]}}
            )
        dataset_mode = req.get("dataset_mode", "new")
        report = _make_progress_reporter(sync_db, run_id)
        report("preparing", 0, 1)
        connect_args = (req["host"], req["port"], req["username"],
                        req.get("pem_key"), req.get("password"))

        if dataset_mode == "new":
            workdir = req["remote_workdir"].rstrip("/")
            remote_upload_path = f"{workdir}/dataset"
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
                if ssh_helper.remote_exists(client, remote_upload_path):
                    # A fresh upload must not blend with (or overwrite) what's
                    # there - move it aside rather than delete it.
                    backup = f"{remote_upload_path}_old_{run_id[:8]}"
                    exit_code, _o, err = ssh_helper.exec_command(
                        client, f"mv {shlex.quote(remote_upload_path)} {shlex.quote(backup)}"
                    )
                    if exit_code != 0:
                        raise Exception(f"could not move existing dataset aside: {err.strip()[-300:]}")
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
                "error": str(e)[:2000],
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
        venv_dir = pipeline_logic.normalize_venv_dir(venv_path)
        activate = f"{venv_dir}/bin/activate"
        if not ssh_helper.remote_exists(client, activate):
            raise Exception(
                f"no activate script at {activate} - give the venv folder or its bin/activate"
            )
        source = f"source {shlex.quote(activate)}"

        def _in_venv(cmd: str):
            return ssh_helper.exec_command(client, f"bash -c {shlex.quote(f'{source} && {cmd}')}")

        exit_code, _o, err = _in_venv("command -v python")
        if exit_code != 0:
            raise Exception(f"activating {activate} failed (exit {exit_code}): {err.strip()[-300:]}")
        exit_code, _o, _e = _in_venv("python -c 'import ultralytics'")
        if exit_code != 0:
            raise Exception(
                f"ultralytics is not installed in {venv_dir} - install it there "
                f"or choose 'Create venv'"
            )
        return f"{source} && python"

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
        req = _resolve_req_paths(req)
        run = sync_db.pipeline_runs.find_one({"id": run_id})
        if not run:
            raise Exception("pipeline run not found")

        dataset_export = run.get("dataset_export") or {}
        remote_upload_path = dataset_export.get("remote_upload_path")
        if not remote_upload_path:
            raise Exception("no dataset_export.remote_upload_path - run uploading_data first")
        data_yaml_path = dataset_export.get("data_yaml_path") or f"{remote_upload_path}/data.yaml"

        # An explicit choice from the UI wins: continue from a model already
        # on the server, or start fresh from a named YOLO model (Ultralytics
        # downloads it there). Otherwise fall back to the legacy branching:
        # bootstrap/fresh_production start from the base checkpoint; merge
        # continues from whatever's currently live on the remote host.
        if (req.get("start_from_path") or "").strip():
            checkpoint_path = req["start_from_path"].strip()
        elif (req.get("yolo_model") or "").strip():
            checkpoint_path = req["yolo_model"].strip()
        elif run.get("run_type") in ("bootstrap", "fresh_production"):
            checkpoint_path = (req.get("remote_base_model_path") or "").strip()
            if not checkpoint_path:
                raise Exception("no starting model chosen - pick an existing model or a YOLO model")
        else:
            checkpoint_path = req.get("remote_production_model_path")
            if not checkpoint_path:
                raise Exception("merge run requires remote_production_model_path")

        # Everything lives directly under the project dir: dataset/, models/,
        # venv/, and runs/run_<id>/ (Ultralytics output + script + metrics).
        workdir = req["remote_workdir"].rstrip("/")
        project_dir = f"{workdir}/runs"
        run_name = f"run_{run_id[:8]}"
        remote_script_path = f"{project_dir}/{run_name}/train_eval.py"
        metrics_json_path = f"{project_dir}/{run_name}/metrics.json"

        params = {
            "checkpoint_path": checkpoint_path,
            "data_yaml_path": data_yaml_path,
            "epochs": req["epochs"],
            "hyperparams": req.get("hyperparams") or {},
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
            if python_bin.startswith("source "):
                # `source` is a shell builtin (and the SSH login shell may not
                # be bash), so an activated venv runs under an explicit bash -c.
                run_cmd = f"bash -c {shlex.quote(f'{python_bin} -u {shlex.quote(remote_script_path)}')}"
            else:
                run_cmd = f"{shlex.quote(python_bin)} -u {shlex.quote(remote_script_path)}"
            exit_code, _out, run_err = ssh_helper.exec_command_streaming(
                client, run_cmd, on_output=_on_output,
            )

            if exit_code == 0:
                # Keep the trained weights where the model scan looks first.
                # Best-effort: a failed copy must not fail a finished training.
                best = f"{project_dir}/{run_name}/weights/best.pt"
                ssh_helper.exec_command(
                    client,
                    f"mkdir -p {shlex.quote(workdir + '/models')} && "
                    f"cp {shlex.quote(best)} {shlex.quote(f'{workdir}/models/{run_name}_best.pt')}",
                )

        if exit_code != 0:
            # The traceback is on stderr, which the stdout-scraped log_tail
            # never contains - include both so the failure is diagnosable.
            raise Exception(
                f"remote training script exited {exit_code}: "
                f"{(run_err or '').strip()[-1500:]} | {progress['log_tail'][-500:]}"
            )

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
                "error": str(e)[:2000],
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
        model_label = os.path.splitext(posixpath.basename(training.get("base_checkpoint_ref") or "") or "yolo")[0]
        sync_db.models.insert_one({
            "id": mid, "project_id": project_id, "type": "pipeline_candidate",
            "pipeline_run_id": run_id,
            "status": "trained", "is_active": False,
            # Shown in Model Training with a "Trained from server" tag.
            "source": "server", "trained_on": "server",
            "name": f"Server-trained · {model_label} · {finished[:10]}",
            "model_arch": model_label,
            "epochs": hyperparams_used.get("epochs"),
            "training_image_count": (run.get("dataset_export") or {}).get("train_count"),
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
            },
            # A new download means a new model: earlier tests/approval were of
            # the old one and must not carry over.
            "$unset": {"video_tests": "", "test_progress": "", "approval": ""}},
        )
    except Exception as e:
        finished = _now_iso()
        sync_db.pipeline_runs.update_one(
            {"id": run_id},
            {"$set": {
                "busy": False,
                "updated_at": finished,
                "status": "failed",
                "error": str(e)[:2000],
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


def _pipeline_test_video_sync(run_id: str, project_id: str, test_id: str, video_bytes: bytes,
                               filename: str, conf: float, get_object_fn, put_object_fn,
                               app_name: str) -> None:
    """Video test for the Test step - the in-app version of the team's manual
    yolo_video_infer.py: run the candidate weights over every frame of an
    uploaded video, draw the detections (results[0].plot()), and store the
    annotated video so a human can watch it and judge the model. Never marks
    the run failed - a bad video only fails its own video_tests entry."""
    import subprocess
    import cv2
    from ultralytics import YOLO

    sync_client = MongoClient(MONGO_URL)
    sync_db = sync_client[DB_NAME]
    tmp_paths = []

    def _tmp(suffix):
        fd, path = tempfile.mkstemp(prefix=f"videotest_{run_id}_", suffix=suffix)
        os.close(fd)
        tmp_paths.append(path)
        return path

    def _set(fields):
        sync_db.pipeline_runs.update_one(
            {"id": run_id}, {"$set": {f"video_tests.$[t].{k}": v for k, v in fields.items()}},
            array_filters=[{"t.id": test_id}],
        )

    try:
        run = sync_db.pipeline_runs.find_one({"id": run_id}) or {}
        weights_path = (run.get("candidate_model") or {}).get("weights_path")
        if not weights_path:
            raise Exception("no candidate_model on run - run downloading_model first")

        in_path = _tmp(".mp4")
        with open(in_path, "wb") as f:
            f.write(video_bytes)
        weights_local = _download_weights_to_tempfile(get_object_fn, weights_path)
        tmp_paths.append(weights_local)
        model = YOLO(weights_local)

        cap = cv2.VideoCapture(in_path)
        if not cap.isOpened():
            raise Exception("could not open the video file")
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or None
        raw_out = _tmp(".mp4")
        out = cv2.VideoWriter(raw_out, cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))

        done = 0
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            results = model(frame, conf=conf, verbose=False)
            out.write(results[0].plot())
            done += 1
            if done % 10 == 0:
                sync_db.pipeline_runs.update_one(
                    {"id": run_id}, {"$set": {"test_progress": {"done": done, "total": total}}},
                )
        cap.release()
        out.release()
        if done == 0:
            raise Exception("no frames could be read from the video")

        # OpenCV's mp4v isn't playable in browsers - re-encode to H.264.
        web_out = _tmp(".mp4")
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            try:
                import imageio_ffmpeg
                ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
            except Exception:
                ffmpeg = None
        final_path = raw_out
        if ffmpeg:
            proc = subprocess.run(
                [ffmpeg, "-y", "-i", raw_out, "-c:v", "libx264", "-pix_fmt", "yuv420p",
                 "-movflags", "+faststart", "-an", web_out],
                capture_output=True, timeout=1800,
            )
            if proc.returncode == 0:
                final_path = web_out
            else:
                logger.warning("ffmpeg re-encode failed for %s: %s", run_id, proc.stderr[-500:])

        with open(final_path, "rb") as f:
            out_bytes = f.read()
        storage_path = f"{app_name}/projects/{project_id}/pipeline_runs/{run_id}/test_{test_id}.mp4"
        put_object_fn(storage_path, out_bytes, "video/mp4")
        _set({"status": "succeeded", "output_path": storage_path, "frames": done,
              "finished_at": _now_iso()})
    except Exception as e:
        _set({"status": "failed", "error": str(e)[:1000], "finished_at": _now_iso()})
    finally:
        sync_db.pipeline_runs.update_one(
            {"id": run_id}, {"$set": {"busy": False, "updated_at": _now_iso()}, "$unset": {"test_progress": ""}},
        )
        for p in tmp_paths:
            if p and os.path.exists(p):
                os.remove(p)
        sync_client.close()


def _pipeline_deploying_sync(run_id: str, project_id: str, history_index: int,
                              req: dict, get_object_fn) -> None:
    """The highest-risk worker in the feature (ROADMAP.md SS7): destructive,
    non-idempotent remote file rotation. Order matters and is deliberate -
    each step clears the exact destination the next step's rename needs,
    since plain SFTP rename (unlike a POSIX mv) cannot overwrite an existing
    destination. It does not activate the model locally - that stays a manual
    choice in Model Training."""
    sync_client = MongoClient(MONGO_URL)
    sync_db = sync_client[DB_NAME]
    local_candidate_pt = None
    try:
        req = _resolve_req_paths(req)
        run = sync_db.pipeline_runs.find_one({"id": run_id})
        if not run:
            raise Exception("pipeline run not found")

        # Upload the model picked in the UI (any server-trained model of the
        # project); default to this run's own downloaded model.
        candidate = run.get("candidate_model") or {}
        local_model_id = req.get("model_id") or candidate.get("local_model_id")
        model_doc = sync_db.models.find_one({"id": local_model_id, "project_id": project_id}) if local_model_id else None
        candidate_weights_path = (model_doc or {}).get("weights_path")
        if not candidate_weights_path:
            raise Exception("no model to upload - pick a server-trained model")

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

        # Activation is manual (Model Training) - deploying only uploads.
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
                "error": str(e)[:2000],
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
        req = _resolve_req_paths(req)
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
                "error": str(e)[:2000],
                f"stage_history.{history_index}.status": "failed",
                f"stage_history.{history_index}.finished_at": finished,
            }},
        )
    finally:
        if local_bak1_pt and os.path.exists(local_bak1_pt):
            os.remove(local_bak1_pt)
        sync_client.close()
