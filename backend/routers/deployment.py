"""Deploy Pipeline endpoints (extracted to its own router per the existing
`register(server_module)` convention — see routers/training.py).

M0 stood up enough of a `pipeline_runs`/`deployment_pipelines` skeleton to
make the cross-run concurrency guard real and testable, with every stage
running through a generic stub. M3 wired in the first real (synchronous)
stage, `class_check`. M4 wired in `uploading_data`, the first stage to use a
real background-thread worker (`deployment_worker.py`, mirroring
`_train_yolo_sync`'s pattern). M5 wired in `training_remote`, which renders
and runs a remote training/eval script and streams live progress back by
regex-scraping its stdout. M6 wires in `downloading_model`, which SFTP-
downloads the trained weights + metrics and registers a `pipeline_candidate`
models doc. M7 wires in `testing`, which needs no SSH at all - it runs local
inference/evaluation against object storage and the currently-active model,
then notifies reviewers. M8 wires in the last stage, `deploying` (the
highest-risk milestone in the feature - destructive, non-idempotent remote
file rotation), plus the `approve`/`reject`/`rollback` endpoints that gate
it. Every stage in `VALID_STAGES` now has real logic - M0's generic
stub-stage fallback has been fully retired, not left as dead code.
run-history endpoints are deliberately not built here (M9's job) since their
semantics aren't designed yet.
"""
import asyncio
import posixpath
import shlex
import yaml
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Body, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import ValidationError

import deployment_worker
import pipeline_logic
import ssh_helper
from schemas.deployment import (
    ClassCheckRequest,
    DeployingRequest,
    DownloadingModelRequest,
    InspectRemoteRequest,
    RollbackRequest,
    TrainingRemoteRequest,
    UploadingDataRequest,
)

router = APIRouter()

VALID_STAGES = {
    "uploading_data",
    "class_check",
    "training_remote",
    "downloading_model",
    "testing",
    "deploying",
}


# Non-secret form fields saved on the run (and mirrored to the pipeline as the
# prefill for new runs). password / pem_key are deliberately NOT here - see the
# SECURITY note in schemas/deployment.py.
FORM_KEYS = {
    "host", "port", "username", "remote_workdir", "remote_data_yaml_path",
    "remote_base_model_path", "remote_production_model_path",
    "train_pct", "valid_pct", "test_pct", "dataset_mode", "existing_data_yaml_path",
    "extra_yaml_path", "epochs", "env_mode", "venv_path", "model_choice",
    "yolo_model", "start_from_path", "hyperparams",
    # The deploy target is a separate host from the training one.
    "deploy_host", "deploy_port", "deploy_username", "deploy_model_path", "deploy_model_id",
}

# Deploy-stage request fields -> the saved deploy_* form keys.
_DEPLOY_FORM_MAP = {
    "host": "deploy_host", "port": "deploy_port", "username": "deploy_username",
    "remote_production_model_path": "deploy_model_path", "model_id": "deploy_model_id",
}


def _form_from_body(body: dict) -> dict:
    return {k: v for k, v in (body or {}).items() if k in FORM_KEYS and v is not None}


def _fetch_remote_yaml_sync(req: ClassCheckRequest, yaml_path: str) -> str:
    """Blocking (paramiko) - always run via asyncio.to_thread, never called
    directly from an async def, matching this codebase's existing convention
    for blocking I/O (_train_yolo_sync, the resend email send)."""
    with ssh_helper.connect(req.host, req.port, req.username, req.pem_key, req.password) as client:
        yaml_path = ssh_helper.resolve_remote_path(client, yaml_path)
        cmd = f"cat {shlex.quote(yaml_path)}"
        exit_code, out, err = ssh_helper.exec_command(client, cmd)
        if exit_code != 0:
            raise ssh_helper.SSHConnectionError(
                f"cat {yaml_path} failed (exit {exit_code}): {err.strip()}"
            )
        return out


_YAML_FIND_CMD = (
    "find {root} -maxdepth 5 -type f \\( -name '*.yaml' -o -name '*.yml' \\) -size -256k "
    "-not -path '*/venv/*' -not -path '*/.venv/*' -not -path '*/site-packages/*' "
    "-not -path '*/.git/*' -not -path '*/runs/*' -printf '%T@ %p\\n' 2>/dev/null | sort -rn | head -100"
)


_PT_FIND_CMD = (
    "find {root} -maxdepth 6 -type f -name '*.pt' -not -name 'last.pt' "
    "-not -path '*/venv/*' -not -path '*/.venv/*' -not -path '*/site-packages/*' "
    "-printf '%T@ %s %p\\n' 2>/dev/null | sort -rn | head -50"
)


def _describe_dataset_yaml(client, yaml_path: str):
    """Returns a dataset summary dict for a remote yaml, or None if it isn't
    a readable dataset config (needs `names` plus train/val)."""
    exit_code, out, _err = ssh_helper.exec_command(client, f"cat {shlex.quote(yaml_path)}")
    if exit_code != 0:
        return None
    try:
        cfg = yaml.safe_load(out)
    except yaml.YAMLError:
        return None
    if not pipeline_logic.looks_like_dataset_yaml(cfg):
        return None
    dirs = pipeline_logic.resolve_yaml_split_dirs(
        cfg, posixpath.dirname(yaml_path),
        path_exists=lambda p: ssh_helper.remote_exists(client, p),
    )
    return {
        "data_yaml_path": yaml_path,
        "names": pipeline_logic.names_as_list(cfg),
        "splits": {
            split: {"images_dir": img, "image_count": deployment_worker._count_remote_files(client, img)}
            for split, (img, _lbl) in dirs.items()
        },
    }


def _inspect_remote_sync(req: InspectRemoteRequest) -> dict:
    """Blocking (paramiko) - run via asyncio.to_thread. Finds dataset yamls
    anywhere under the workdir (any file name/depth up to 5, not just
    data.yaml), plus an optional explicit extra_yaml_path, and validates an
    optional venv."""
    datasets = []
    with ssh_helper.connect(req.host, req.port, req.username, req.pem_key, req.password) as client:
        workdir = ssh_helper.resolve_remote_path(client, req.remote_workdir)
        venv_path = ssh_helper.resolve_remote_path(client, req.venv_path) if req.venv_path else None
        extra_yaml = ssh_helper.resolve_remote_path(client, req.extra_yaml_path) if req.extra_yaml_path else None
        candidates = []
        if extra_yaml:
            candidates.append(extra_yaml)
        _code, out, _err = ssh_helper.exec_command(
            client, _YAML_FIND_CMD.format(root=shlex.quote(workdir))
        )
        for line in out.splitlines():
            _ts, _sp, path = line.partition(" ")
            if path and path not in candidates:
                candidates.append(path)
        for path in candidates:
            info = _describe_dataset_yaml(client, path)
            if info:
                datasets.append(info)
        _c, out, _e = ssh_helper.exec_command(client, _PT_FIND_CMD.format(root=shlex.quote(workdir)))
        models = []
        for line in out.splitlines():
            parts = line.split(" ", 2)
            if len(parts) == 3:
                try:
                    models.append({"path": parts[2], "size": int(parts[1]), "mtime": float(parts[0])})
                except ValueError:
                    pass
        venv_ok = None
        if venv_path:
            venv_dir = pipeline_logic.normalize_venv_dir(venv_path)
            venv_ok = ssh_helper.remote_exists(client, f"{venv_dir}/bin/activate")
    first = datasets[0] if datasets else {}
    return {
        "workdir": workdir,
        "found": bool(datasets),
        "datasets": datasets,
        # kept for compatibility with the single-dataset shape
        "data_yaml_path": first.get("data_yaml_path"),
        "names": first.get("names", []),
        "splits": first.get("splits", {}),
        "models": models,
        "venv_ok": venv_ok,
    }


def register(s):
    db = s.db
    get_current_user = s.get_current_user

    async def _get_or_create_pipeline(pid: str, user_id: str) -> dict:
        pipeline = await db.deployment_pipelines.find_one({"project_id": pid})
        if pipeline:
            return pipeline
        now = datetime.now(timezone.utc).isoformat()
        pipeline = {
            "id": str(uuid.uuid4()),
            "project_id": pid,
            "created_by": user_id,
            "created_at": now,
            "updated_at": now,
            # Connection metadata is configured elsewhere (not yet built as of
            # M0) - never password/key material, and never populated here.
            "remote_host": None,
            "remote_port": 22,
            "remote_username": None,
            "remote_base_model_path": None,
            "remote_production_model_path": None,
            "remote_data_yaml_path": None,
            "remote_workdir": None,
            "last_run_id": None,
            "last_deployed_run_id": None,
        }
        await db.deployment_pipelines.insert_one(pipeline)
        return pipeline

    @router.post("/projects/{pid}/pipeline/runs")
    async def create_pipeline_run(pid: str, current=Depends(get_current_user)):
        await s._project_access_check(pid, current["id"], roles=["owner", "admin"])
        pipeline = await _get_or_create_pipeline(pid, current["id"])

        prior_completed = await db.pipeline_runs.count_documents(
            {"pipeline_id": pipeline["id"], "status": "completed"}
        )
        # TODO(M8): replace with the real has_bootstrap_model/has_production_model
        # derivation once pipeline_candidate models exist to derive it from.
        run_type = "bootstrap" if prior_completed == 0 else "merge"

        now = datetime.now(timezone.utc).isoformat()
        rid = str(uuid.uuid4())
        run = {
            "id": rid,
            "project_id": pid,
            "pipeline_id": pipeline["id"],
            "run_type": run_type,
            "status": "draft",
            "busy": False,
            "stage_history": [],
            # Pre-filled from the previous run's (non-secret) form values.
            "form": dict(pipeline.get("last_form") or {}),
            "created_at": now,
            "updated_at": now,
        }
        await db.pipeline_runs.insert_one(run)
        await db.deployment_pipelines.update_one(
            {"id": pipeline["id"]}, {"$set": {"last_run_id": rid, "updated_at": now}}
        )
        run.pop("_id", None)
        await s._log_activity(
            pid, current["id"], "pipeline_run_created", {"run_id": rid, "run_type": run_type}
        )
        return run

    @router.get("/pipeline/runs/{rid}")
    async def get_pipeline_run(rid: str, current=Depends(get_current_user)):
        run = await db.pipeline_runs.find_one({"id": rid}, {"_id": 0})
        if not run:
            raise HTTPException(status_code=404, detail="Not found")
        await s._project_access_check(run["project_id"], current["id"])
        return run

    @router.delete("/pipeline/runs/{rid}")
    async def delete_pipeline_run(rid: str, current=Depends(get_current_user)):
        """Removes a run's history entry only - nothing on the remote host or
        in the models collection is touched. Refuses a busy run, and the run
        currently recorded as last deployed (the Rollback button hangs off it)."""
        run = await db.pipeline_runs.find_one({"id": rid})
        if not run:
            raise HTTPException(status_code=404, detail="Not found")
        await s._project_access_check(run["project_id"], current["id"], roles=["owner", "admin"])
        if run.get("busy"):
            raise HTTPException(status_code=409, detail="Run is in progress and cannot be deleted")
        pipeline = await db.deployment_pipelines.find_one({"id": run["pipeline_id"]})
        if pipeline and pipeline.get("last_deployed_run_id") == rid:
            raise HTTPException(status_code=409, detail="This is the last deployed run and cannot be deleted")
        await db.pipeline_runs.delete_one({"id": rid})
        if pipeline and pipeline.get("last_run_id") == rid:
            latest = await db.pipeline_runs.find_one(
                {"pipeline_id": pipeline["id"]}, {"id": 1}, sort=[("created_at", -1)]
            )
            await db.deployment_pipelines.update_one(
                {"id": pipeline["id"]}, {"$set": {"last_run_id": (latest or {}).get("id")}}
            )
        await s._log_activity(run["project_id"], current["id"], "pipeline_run_deleted", {"run_id": rid})
        return {"deleted": rid}

    @router.get("/projects/{pid}/pipeline/runs")
    async def list_pipeline_runs(pid: str, current=Depends(get_current_user)):
        """In ROADMAP.md's own §4 endpoint table but never implemented across
        M0-M8 - added here for M9's run-history list. Bundles
        last_deployed_run_id (off deployment_pipelines) into the same
        response rather than adding a second new endpoint just to gate the
        frontend's Rollback button, since §4 doesn't list one either."""
        await s._project_access_check(pid, current["id"])
        runs = await db.pipeline_runs.find({"project_id": pid}, {"_id": 0}).sort("created_at", -1).to_list(200)
        pipeline = await db.deployment_pipelines.find_one({"project_id": pid}, {"_id": 0})
        return {
            "runs": runs,
            "last_deployed_run_id": (pipeline or {}).get("last_deployed_run_id"),
            "pipeline_id": (pipeline or {}).get("id"),
            "remote_workdir": (pipeline or {}).get("remote_workdir"),
        }

    async def _save_form(run: dict, body: dict, stage: str = None) -> dict:
        """Stores the run's non-secret form values and mirrors them onto the
        pipeline so the next run starts pre-filled. A deploying-stage body
        carries the deploy host in host/port/username, which must not
        overwrite the training host - it is saved under deploy_* instead."""
        if stage == "deploying":
            body = {_DEPLOY_FORM_MAP[k]: v for k, v in (body or {}).items() if k in _DEPLOY_FORM_MAP}
        form = _form_from_body(body)
        if not form:
            return {}
        await db.pipeline_runs.update_one(
            {"id": run["id"]}, {"$set": {f"form.{k}": v for k, v in form.items()}}
        )
        await db.deployment_pipelines.update_one(
            {"id": run["pipeline_id"]}, {"$set": {f"last_form.{k}": v for k, v in form.items()}}
        )
        return form

    @router.put("/pipeline/runs/{rid}/form")
    async def save_run_form(rid: str, current=Depends(get_current_user),
                            body: dict = Body(default_factory=dict)):
        run = await db.pipeline_runs.find_one({"id": rid})
        if not run:
            raise HTTPException(status_code=404, detail="Not found")
        await s._project_access_check(run["project_id"], current["id"], roles=["owner", "admin"])
        return {"form": await _save_form(run, body)}

    async def _remember_workdir(pipeline_id: str, workdir: str) -> None:
        """Persists the (non-secret) project directory so the UI can prefill it."""
        workdir = (workdir or "").strip()
        if workdir:
            await db.deployment_pipelines.update_one(
                {"id": pipeline_id}, {"$set": {"remote_workdir": workdir}}
            )

    @router.post("/pipeline/runs/{rid}/inspect_remote")
    async def inspect_remote(rid: str, current=Depends(get_current_user),
                              body: dict = Body(default_factory=dict)):
        """Read-only probe (no busy flag / stage history): reports a
        pre-existing dataset in the remote workdir and venv validity so the
        UI can offer merge/reuse and venv choices."""
        run = await db.pipeline_runs.find_one({"id": rid})
        if not run:
            raise HTTPException(status_code=404, detail="Not found")
        await s._project_access_check(run["project_id"], current["id"], roles=["owner", "admin"])
        try:
            req = InspectRemoteRequest(**body)
        except ValidationError as e:
            raise HTTPException(status_code=400, detail=f"Invalid inspect_remote request: {e}")
        await _save_form(run, body)
        try:
            result = await asyncio.to_thread(_inspect_remote_sync, req)
            await _remember_workdir(run["pipeline_id"], result["workdir"])
            return result
        except ssh_helper.SSHConnectionError as e:
            raise HTTPException(status_code=502, detail=str(e)[:2000])

    @router.post("/pipeline/runs/{rid}/stage/{stage}")
    async def run_stage(
        rid: str,
        stage: str,
        background: BackgroundTasks,
        current=Depends(get_current_user),
        body: dict = Body(default_factory=dict),
    ):
        run = await db.pipeline_runs.find_one({"id": rid})
        if not run:
            raise HTTPException(status_code=404, detail="Not found")
        await s._project_access_check(run["project_id"], current["id"], roles=["owner", "admin"])

        if stage not in VALID_STAGES:
            raise HTTPException(status_code=400, detail=f"Unknown stage '{stage}'")

        if stage == "deploying" and run.get("approval", {}).get("decision") != "approved":
            raise HTTPException(status_code=400, detail="Run must be approved before deploying")

        if stage == "deploying":
            # Which model to upload: any server-trained model of this project
            # (default: this run's own downloaded model).
            model_id = body.get("model_id") or (run.get("candidate_model") or {}).get("local_model_id")
            model_doc = await db.models.find_one(
                {"id": model_id, "project_id": run["project_id"], "source": "server"}, {"id": 1}
            ) if model_id else None
            if not model_doc:
                raise HTTPException(status_code=400, detail="Choose a server-trained model of this project to upload")
            body = {**body, "model_id": model_id}

        if stage == "testing" and not any(t.get("status") == "succeeded" for t in run.get("video_tests", [])):
            raise HTTPException(status_code=400, detail="Run at least one video test first")

        busy_run = await db.pipeline_runs.find_one(
            {"pipeline_id": run["pipeline_id"], "busy": True}, {"id": 1}
        )
        if busy_run:
            raise HTTPException(
                status_code=409,
                detail=f"Pipeline busy: run {busy_run['id']} is currently in progress",
            )

        await _save_form(run, body, stage)

        now = datetime.now(timezone.utc).isoformat()
        history_index = len(run.get("stage_history", []))
        entry = {"stage": stage, "status": "running", "started_at": now}
        await db.pipeline_runs.update_one(
            {"id": rid},
            {
                "$set": {"busy": True, "status": stage, "updated_at": now},
                "$push": {"stage_history": entry},
            },
        )

        if stage == "class_check":
            try:
                req = ClassCheckRequest(**body)
            except ValidationError as e:
                await db.pipeline_runs.update_one({"id": rid}, {"$set": {"busy": False}})
                raise HTTPException(status_code=400, detail=f"Invalid class_check request: {e}")

            # Blank path -> the yaml the upload stage already recorded (older
            # runs only stored remote_upload_path, so derive from that).
            de = run.get("dataset_export") or {}
            yaml_path = (req.remote_data_yaml_path or "").strip() or de.get("data_yaml_path") or (
                f"{de['remote_upload_path']}/data.yaml" if de.get("remote_upload_path") else None
            )
            if not yaml_path:
                await db.pipeline_runs.update_one({"id": rid}, {"$set": {"busy": False}})
                raise HTTPException(status_code=400, detail="No remote data.yaml path given and none recorded by the upload stage")

            finished = datetime.now(timezone.utc).isoformat()
            try:
                remote_yaml_text = await asyncio.to_thread(_fetch_remote_yaml_sync, req, yaml_path)
                project = await db.projects.find_one({"id": run["project_id"]}, {"classes": 1})
                result = pipeline_logic.diff_classes(project.get("classes", []), remote_yaml_text)
                # status stays "class_check" on match (M0's "status = last
                # stage name, busy = is it running" convention - the next
                # stage call is what advances status); "failed" + run.error
                # on mismatch, per ROADMAP's explicit instruction.
                update = {
                    "busy": False,
                    "updated_at": finished,
                    "status": "class_check" if result["match"] else "failed",
                    "class_check": {**result, "checked_at": finished},
                    f"stage_history.{history_index}.status": "succeeded" if result["match"] else "failed",
                    f"stage_history.{history_index}.finished_at": finished,
                }
                if not result["match"]:
                    update["error"] = "Class mismatch between local project and remote data.yaml"
                await db.pipeline_runs.update_one({"id": rid}, {"$set": update})
            except ssh_helper.SSHConnectionError as e:
                await db.pipeline_runs.update_one(
                    {"id": rid},
                    {
                        "$set": {
                            "busy": False,
                            "updated_at": finished,
                            "status": "failed",
                            "error": str(e)[:2000],
                            f"stage_history.{history_index}.status": "failed",
                            f"stage_history.{history_index}.finished_at": finished,
                        }
                    },
                )
            return await db.pipeline_runs.find_one({"id": rid}, {"_id": 0})

        if stage == "uploading_data":
            try:
                req = UploadingDataRequest(**body)
            except ValidationError as e:
                await db.pipeline_runs.update_one({"id": rid}, {"$set": {"busy": False}})
                raise HTTPException(status_code=400, detail=f"Invalid uploading_data request: {e}")

            await _remember_workdir(run["pipeline_id"], req.remote_workdir)
            if abs(req.train_pct + req.valid_pct + req.test_pct - 1.0) > 1e-6:
                await db.pipeline_runs.update_one({"id": rid}, {"$set": {"busy": False}})
                raise HTTPException(status_code=400, detail="train_pct + valid_pct + test_pct must sum to 1.0")

            async def _job():
                await asyncio.to_thread(
                    deployment_worker._pipeline_uploading_data_sync,
                    rid, run["project_id"], history_index, req.model_dump(), s.get_object,
                )
            background.add_task(_job)
            return await db.pipeline_runs.find_one({"id": rid}, {"_id": 0})

        if stage == "training_remote":
            try:
                req = TrainingRemoteRequest(**body)
            except ValidationError as e:
                await db.pipeline_runs.update_one({"id": rid}, {"$set": {"busy": False}})
                raise HTTPException(status_code=400, detail=f"Invalid training_remote request: {e}")

            async def _job():
                await asyncio.to_thread(
                    deployment_worker._pipeline_training_remote_sync,
                    rid, run["project_id"], history_index, req.model_dump(),
                )
            background.add_task(_job)
            return await db.pipeline_runs.find_one({"id": rid}, {"_id": 0})

        if stage == "downloading_model":
            try:
                req = DownloadingModelRequest(**body)
            except ValidationError as e:
                await db.pipeline_runs.update_one({"id": rid}, {"$set": {"busy": False}})
                raise HTTPException(status_code=400, detail=f"Invalid downloading_model request: {e}")

            async def _job():
                await asyncio.to_thread(
                    deployment_worker._pipeline_downloading_model_sync,
                    rid, run["project_id"], history_index, req.model_dump(), s.put_object, s.APP_NAME,
                )
            background.add_task(_job)
            return await db.pipeline_runs.find_one({"id": rid}, {"_id": 0})

        if stage == "testing":
            # "Finish testing": the real work is the video tests run through
            # POST /pipeline/runs/{rid}/test-video; this just closes the step
            # once at least one succeeded and hands the run to human review.
            finished = datetime.now(timezone.utc).isoformat()
            await db.pipeline_runs.update_one(
                {"id": rid},
                {"$set": {
                    "busy": False, "updated_at": finished, "status": "awaiting_approval",
                    f"stage_history.{history_index}.status": "succeeded",
                    f"stage_history.{history_index}.finished_at": finished,
                }},
            )
            return await db.pipeline_runs.find_one({"id": rid}, {"_id": 0})

        # stage == "deploying" - the only remaining branch, guarded above by
        # the approval-required check and the model check.
        try:
            req = DeployingRequest(**body)
        except ValidationError as e:
            await db.pipeline_runs.update_one({"id": rid}, {"$set": {"busy": False}})
            raise HTTPException(status_code=400, detail=f"Invalid deploying request: {e}")

        async def _job():
            await asyncio.to_thread(
                deployment_worker._pipeline_deploying_sync,
                rid, run["project_id"], history_index, req.model_dump(), s.get_object,
            )
        background.add_task(_job)
        return await db.pipeline_runs.find_one({"id": rid}, {"_id": 0})

    @router.post("/pipeline/runs/{rid}/test-video")
    async def test_video(
        rid: str,
        background: BackgroundTasks,
        file: UploadFile = File(...),
        conf: float = Form(0.25),
        current=Depends(get_current_user),
    ):
        run = await db.pipeline_runs.find_one({"id": rid})
        if not run:
            raise HTTPException(status_code=404, detail="Not found")
        await s._project_access_check(run["project_id"], current["id"], roles=["owner", "admin"])
        candidate = run.get("candidate_model") or {}
        if not candidate.get("weights_path"):
            raise HTTPException(status_code=400, detail="No candidate model - run Download first")
        model_doc = await db.models.find_one(
            {"id": candidate.get("local_model_id"), "pipeline_run_id": rid}, {"id": 1}
        )
        if not model_doc:
            raise HTTPException(
                status_code=400,
                detail="The downloaded model for this run was deleted - run Download again",
            )
        if not 0 < conf <= 1:
            raise HTTPException(status_code=400, detail="conf must be between 0 and 1")
        ext = (file.filename.rsplit(".", 1)[-1] if "." in (file.filename or "") else "").lower()
        if ext not in ["mp4", "mov", "webm", "avi", "mkv"]:
            raise HTTPException(status_code=400, detail="Unsupported video type")
        content = await file.read()
        if len(content) > 200 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="Video too large (max 200MB)")

        busy_run = await db.pipeline_runs.find_one(
            {"pipeline_id": run["pipeline_id"], "busy": True}, {"id": 1}
        )
        if busy_run:
            raise HTTPException(status_code=409, detail=f"Pipeline busy: run {busy_run['id']} is currently in progress")

        test_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        await db.pipeline_runs.update_one(
            {"id": rid},
            {"$set": {"busy": True, "updated_at": now},
             "$push": {"video_tests": {"id": test_id, "input_name": file.filename, "conf": conf,
                                       "model_id": candidate.get("local_model_id"),
                                       "status": "running", "started_at": now}}},
        )

        async def _job():
            await asyncio.to_thread(
                deployment_worker._pipeline_test_video_sync,
                rid, run["project_id"], test_id, content, file.filename, conf,
                s.get_object, s.put_object, s.APP_NAME,
            )
        background.add_task(_job)
        return await db.pipeline_runs.find_one({"id": rid}, {"_id": 0})

    @router.get("/pipeline/runs/{rid}/test-video/{test_id}")
    async def get_test_video(rid: str, test_id: str, request: Request, current=Depends(get_current_user)):
        run = await db.pipeline_runs.find_one({"id": rid})
        if not run:
            raise HTTPException(status_code=404, detail="Not found")
        await s._project_access_check(run["project_id"], current["id"])
        entry = next((t for t in run.get("video_tests", []) if t["id"] == test_id), None)
        if not entry or not entry.get("output_path"):
            raise HTTPException(status_code=404, detail="Not found")
        data, _ct = await asyncio.to_thread(s.get_object, entry["output_path"])
        total = len(data)
        headers = {"Accept-Ranges": "bytes", "Cache-Control": "private, max-age=31536000, immutable"}
        rng = request.headers.get("range")
        if rng and rng.startswith("bytes="):
            start_s, _, end_s = rng[6:].partition("-")
            start = int(start_s) if start_s else 0
            end = int(end_s) if end_s else total - 1
            end = min(end, total - 1)
            if start > end:
                raise HTTPException(status_code=416, detail="Bad range")
            headers["Content-Range"] = f"bytes {start}-{end}/{total}"
            return Response(content=data[start:end + 1], status_code=206, media_type="video/mp4", headers=headers)
        return Response(content=data, media_type="video/mp4", headers=headers)

    @router.post("/pipeline/runs/{rid}/approve")
    async def approve_pipeline_run(rid: str, current=Depends(get_current_user),
                                    body: dict = Body(default_factory=dict)):
        run = await db.pipeline_runs.find_one({"id": rid})
        if not run:
            raise HTTPException(status_code=404, detail="Not found")
        await s._project_access_check(run["project_id"], current["id"], roles=["owner", "admin"])
        if run.get("status") != "awaiting_approval":
            raise HTTPException(status_code=409, detail=f"Run is not awaiting approval (status: {run.get('status')})")

        now = datetime.now(timezone.utc).isoformat()
        approval = {
            "decision": "approved", "decided_by": current["id"],
            "decided_at": now, "note": body.get("note"),
        }
        update = {"approval": approval, "updated_at": now}

        await db.pipeline_runs.update_one({"id": rid}, {"$set": update})
        await s._log_activity(run["project_id"], current["id"], "pipeline_run_approved", {"run_id": rid})
        return await db.pipeline_runs.find_one({"id": rid}, {"_id": 0})

    @router.post("/pipeline/runs/{rid}/reject")
    async def reject_pipeline_run(rid: str, current=Depends(get_current_user),
                                   body: dict = Body(default_factory=dict)):
        # Makes zero SSH calls by construction - no ssh_helper import needed
        # in this handler at all.
        run = await db.pipeline_runs.find_one({"id": rid})
        if not run:
            raise HTTPException(status_code=404, detail="Not found")
        await s._project_access_check(run["project_id"], current["id"], roles=["owner", "admin"])
        if run.get("status") != "awaiting_approval":
            raise HTTPException(status_code=409, detail=f"Run is not awaiting approval (status: {run.get('status')})")

        now = datetime.now(timezone.utc).isoformat()
        await db.pipeline_runs.update_one(
            {"id": rid},
            {"$set": {
                "status": "rejected",
                "approval": {
                    "decision": "rejected", "decided_by": current["id"],
                    "decided_at": now, "note": body.get("note"),
                },
                "updated_at": now,
            }},
        )
        await s._log_activity(run["project_id"], current["id"], "pipeline_run_rejected", {"run_id": rid})
        return await db.pipeline_runs.find_one({"id": rid}, {"_id": 0})

    @router.post("/projects/{pid}/pipeline/rollback")
    async def rollback_pipeline(pid: str, background: BackgroundTasks, current=Depends(get_current_user),
                                 body: dict = Body(default_factory=dict)):
        await s._project_access_check(pid, current["id"], roles=["owner", "admin"])
        try:
            req = RollbackRequest(**body)
        except ValidationError as e:
            raise HTTPException(status_code=400, detail=f"Invalid rollback request: {e}")

        pipeline = await db.deployment_pipelines.find_one({"project_id": pid})
        if not pipeline:
            raise HTTPException(status_code=400, detail="No deployment pipeline exists for this project yet")

        busy_run = await db.pipeline_runs.find_one(
            {"pipeline_id": pipeline["id"], "busy": True}, {"id": 1}
        )
        if busy_run:
            raise HTTPException(
                status_code=409,
                detail=f"Pipeline busy: run {busy_run['id']} is currently in progress",
            )

        now = datetime.now(timezone.utc).isoformat()
        rid = str(uuid.uuid4())
        # run_type forced directly, bypassing create_pipeline_run's
        # auto-detection (which doesn't know about rollback at all) - modeled
        # as its own pipeline_runs doc jumping straight to deploying, per
        # ROADMAP.md SS3.
        run = {
            "id": rid,
            "project_id": pid,
            "pipeline_id": pipeline["id"],
            "run_type": "rollback",
            "status": "deploying",
            "busy": True,
            "stage_history": [{"stage": "deploying", "status": "running", "started_at": now}],
            "created_at": now,
            "updated_at": now,
        }
        await db.pipeline_runs.insert_one(run)
        await db.deployment_pipelines.update_one(
            {"id": pipeline["id"]}, {"$set": {"last_run_id": rid, "updated_at": now}}
        )

        async def _job():
            await asyncio.to_thread(
                deployment_worker._pipeline_rollback_sync, rid, pid, 0, req.model_dump(),
            )
        background.add_task(_job)
        await s._log_activity(pid, current["id"], "pipeline_rollback_started", {"run_id": rid})
        run.pop("_id", None)
        return run
