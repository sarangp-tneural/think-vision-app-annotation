"""Model Training on a remote host over SSH (plain training, no deploy-pipeline stages).

Runs in a worker thread with its own sync pymongo client (same rule as server._train_yolo_sync) and writes
progress straight onto the `models` document the Model Training page already polls. SSH credentials live only
in the `req` dict for the lifetime of the call: never logged, stored or put in an error message.
"""
import logging
import os
import shlex
import shutil
import tempfile
import time
from datetime import datetime, timezone
import json

from pymongo import MongoClient

import pipeline_logic
import ssh_helper
from deployment_worker import (
    DB_NAME, MONGO_URL, _EPOCH_RE, _LOG_TAIL_CAP, _build_split_dataset_dir_sync, _prepare_python,
)

logger = logging.getLogger(__name__)


class _Cancelled(Exception):
    pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _scrub(text: str, req: dict) -> str:
    """Keep secrets out of anything stored or shown."""
    for key in ("password", "pem_key"):
        secret = req.get(key)
        if secret:
            text = text.replace(secret, "***")
    return text


def train_remote_sync(model_id: str, project_id: str, req: dict, get_object_fn, put_object_fn, app_name: str) -> None:
    """req: host, port, username, pem_key, password, remote_workdir, env_mode, venv_path, model_arch, epochs."""
    sync_client = MongoClient(MONGO_URL)
    db = sync_client[DB_NAME]
    local_dataset = local_script = local_weights = local_metrics = None
    run_name = f"train_{model_id[:8]}"
    workdir = None

    def cancelled() -> bool:
        m = db.models.find_one({"id": model_id}, {"cancel_requested": 1})
        return bool(m and m.get("cancel_requested"))

    last_check = {"t": 0.0, "v": False}

    def should_stop() -> bool:  # cheap: hits Mongo at most every 2s
        now = time.monotonic()
        if now - last_check["t"] >= 2:
            last_check["t"], last_check["v"] = now, cancelled()
        return last_check["v"]

    def check_cancel():
        if cancelled():
            raise _Cancelled()

    def set_model(**fields):
        db.models.update_one({"id": model_id}, {"$set": fields})

    try:
        project = db.projects.find_one({"id": project_id}) or {}
        classes = project.get("classes", [])
        epochs = req["epochs"]
        set_model(status="preparing", progress=0, current_epoch=0, log_tail="Building dataset…")
        check_cancel()

        local_dataset, counts = _build_split_dataset_dir_sync(
            db, project_id, classes, 0.8, 0.2, 0.0, get_object_fn, yaml_root_path=None,
        )

        with ssh_helper.connect(req["host"], req["port"], req["username"], req.get("pem_key"), req.get("password")) as client:
            workdir = ssh_helper.resolve_remote_path(client, req["remote_workdir"]).rstrip("/")
            run_dir = f"{workdir}/train_runs/{run_name}"
            remote_dataset = f"{run_dir}/dataset"
            # data.yaml must point at the remote copy of the dataset.
            yaml_path = os.path.join(local_dataset, "data.yaml")
            with open(yaml_path) as f:
                yaml_text = f.read().replace(f"path: {local_dataset}", f"path: {remote_dataset}", 1)
            with open(yaml_path, "w") as f:
                f.write(yaml_text)

            log = {"tail": "", "epoch": 0}

            def on_output(chunk: str) -> None:
                log["tail"] = (log["tail"] + chunk)[-_LOG_TAIL_CAP:]
                for line in chunk.splitlines():
                    m = _EPOCH_RE.match(line)
                    if m and int(m.group(1)) != log["epoch"]:
                        cur, total = int(m.group(1)), int(m.group(2))
                        log["epoch"] = cur
                        set_model(current_epoch=cur, progress=round(100 * cur / total) if total else 0, log_tail=_scrub(log["tail"], req))
                        return
                set_model(log_tail=_scrub(log["tail"], req))

            set_model(log_tail=f"Uploading {sum(counts[k] for k in ('train_count', 'valid_count'))} images to the host…")
            check_cancel()
            ssh_helper.upload_dir(client, local_dataset, remote_dataset)
            check_cancel()

            venv = ssh_helper.resolve_remote_path(client, req["venv_path"]) if req.get("venv_path") else None
            python_bin = _prepare_python(client, req.get("env_mode", "system"), venv, workdir, on_output)
            check_cancel()

            metrics_json = f"{run_dir}/metrics.json"
            script = pipeline_logic.render_train_eval_script({
                "checkpoint_path": f"{req['model_arch']}.pt",
                "data_yaml_path": f"{remote_dataset}/data.yaml",
                "epochs": epochs, "hyperparams": req.get("hyperparams") or {},
                "project_dir": run_dir, "run_name": "out", "metrics_json_path": metrics_json,
            })
            fd, local_script = tempfile.mkstemp(prefix=f"train_{model_id}_", suffix=".py")
            with os.fdopen(fd, "w") as f:
                f.write(script)
            remote_script = f"{run_dir}/train_eval.py"
            ssh_helper.upload_file(client, local_script, remote_script)

            set_model(status="training", log_tail="Starting training…")
            if python_bin.startswith("source "):
                cmd = f"bash -c {shlex.quote(f'{python_bin} -u {shlex.quote(remote_script)}')}"
            else:
                cmd = f"{shlex.quote(python_bin)} -u {shlex.quote(remote_script)}"
            code, _out, err = ssh_helper.exec_command_streaming(client, cmd, on_output=on_output, should_stop=should_stop)
            if code == -1 and cancelled():
                # Stop the remote process too, otherwise it keeps training after we hang up.
                ssh_helper.exec_command(client, f"pkill -f {shlex.quote(remote_script)} || true")
                raise _Cancelled()
            if code != 0:
                raise Exception(f"remote training exited {code}: {(err or '').strip()[-800:]} | {log['tail'][-300:]}")

            weights_dir = f"{run_dir}/out/weights"
            lfd, local_weights = tempfile.mkstemp(prefix=f"w_{model_id}_", suffix=".pt"); os.close(lfd)
            mfd, local_metrics = tempfile.mkstemp(prefix=f"m_{model_id}_", suffix=".json"); os.close(mfd)
            try:
                ssh_helper.download_file(client, f"{weights_dir}/best.pt", local_weights)
            except Exception:
                ssh_helper.download_file(client, f"{weights_dir}/last.pt", local_weights)
            ssh_helper.download_file(client, metrics_json, local_metrics)

        with open(local_weights, "rb") as f:
            weights_bytes = f.read()
        with open(local_metrics) as f:
            raw = json.load(f)
        metrics = {k: float(raw.get(v, 0)) for k, v in
                   (("mAP50", "map50"), ("mAP50_95", "map50_95"), ("precision", "precision"), ("recall", "recall"))}
        storage_path = f"{app_name}/projects/{project_id}/models/{model_id}.pt"
        put_object_fn(storage_path, weights_bytes, "application/octet-stream")
        set_model(
            status="trained", classes=classes, trained_on="server", source="server",
            weights_path=storage_path, weights_size=len(weights_bytes),
            final_mAP=metrics["mAP50"], final_loss=1 - metrics["mAP50_95"],
            precision=metrics["precision"], recall=metrics["recall"], metrics_full=metrics,
            progress=100, completed_at=_now_iso(),
        )
        db.projects.update_one({"id": project_id}, {"$set": {"last_retrain_nudge_count": 0}})
    except _Cancelled:
        set_model(status="cancelled", completed_at=_now_iso())
    except Exception as e:
        logger.error("remote training %s failed: %s", model_id, type(e).__name__)
        set_model(status="failed", error=_scrub(str(e), req)[:500], completed_at=_now_iso())
    finally:
        for p in (local_script, local_weights, local_metrics):
            if p and os.path.exists(p):
                os.remove(p)
        if local_dataset:
            shutil.rmtree(local_dataset, ignore_errors=True)
        sync_client.close()
