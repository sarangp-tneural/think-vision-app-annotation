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
import shlex
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException
from pydantic import ValidationError

import deployment_worker
import pipeline_logic
import ssh_helper
from schemas.deployment import (
    ClassCheckRequest,
    DeployingRequest,
    DownloadingModelRequest,
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


def _fetch_remote_yaml_sync(req: ClassCheckRequest) -> str:
    """Blocking (paramiko) - always run via asyncio.to_thread, never called
    directly from an async def, matching this codebase's existing convention
    for blocking I/O (_train_yolo_sync, the resend email send)."""
    with ssh_helper.connect(req.host, req.port, req.username, req.pem_key, req.password) as client:
        cmd = f"cat {shlex.quote(req.remote_data_yaml_path)}"
        exit_code, out, err = ssh_helper.exec_command(client, cmd)
        if exit_code != 0:
            raise ssh_helper.SSHConnectionError(
                f"cat {req.remote_data_yaml_path} failed (exit {exit_code}): {err.strip()}"
            )
        return out


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
        }

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

        if stage == "deploying" and run.get("run_type") == "bootstrap":
            raise HTTPException(status_code=400, detail="Bootstrap runs cannot deploy")

        if stage == "deploying" and run.get("approval", {}).get("decision") != "approved":
            raise HTTPException(status_code=400, detail="Run must be approved before deploying")

        busy_run = await db.pipeline_runs.find_one(
            {"pipeline_id": run["pipeline_id"], "busy": True}, {"id": 1}
        )
        if busy_run:
            raise HTTPException(
                status_code=409,
                detail=f"Pipeline busy: run {busy_run['id']} is currently in progress",
            )

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

            finished = datetime.now(timezone.utc).isoformat()
            try:
                remote_yaml_text = await asyncio.to_thread(_fetch_remote_yaml_sync, req)
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
                            "error": str(e)[:500],
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
            # No request body needed - unlike every prior real stage, testing
            # only touches things already local or in object storage.
            async def _job():
                await asyncio.to_thread(
                    deployment_worker._pipeline_testing_sync,
                    rid, run["project_id"], history_index,
                    s._yolo_predict_sync, s.get_object, s.send_tester_notification_email, s.APP_URL,
                )
            background.add_task(_job)
            return await db.pipeline_runs.find_one({"id": rid}, {"_id": 0})

        # stage == "deploying" - the only remaining branch, guarded above by
        # both the bootstrap-block (M0) and the approval-required check.
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

        if run.get("run_type") == "bootstrap":
            # stage/deploying is permanently blocked for bootstrap runs (M0's
            # guard) - this is the only path to a terminal state for one, so
            # the local-only activation happens here, not in a deploying
            # stage call. Replicates POST /models/{mid}/activate's exact
            # writes (routers/training.py:64-77), not an HTTP self-call - see
            # deployment_worker._pipeline_deploying_sync's docstring for why.
            candidate = run.get("candidate_model") or {}
            local_model_id = candidate.get("local_model_id")
            if not local_model_id:
                raise HTTPException(status_code=400, detail="No candidate_model on run - run downloading_model first")
            await db.models.update_many({"project_id": run["project_id"]}, {"$set": {"is_active": False}})
            await db.models.update_one({"id": local_model_id}, {"$set": {"is_active": True, "activated_at": now}})
            update["status"] = "completed"

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
