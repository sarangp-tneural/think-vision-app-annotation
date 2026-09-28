"""Deploy Pipeline endpoints (extracted to its own router per the existing
`register(server_module)` convention — see routers/training.py).

M0 scope only: enough of a `pipeline_runs`/`deployment_pipelines` skeleton to
make the cross-run concurrency guard real and testable. Stage handlers are
stubs (no SSH, no real training) until M2-M8 replace them one stage at a time;
approve/reject/rollback/run-history endpoints are deliberately not built here
(M8/M9's job) since their semantics aren't designed yet.
"""
import asyncio
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

router = APIRouter()

VALID_STAGES = {
    "uploading_data",
    "class_check",
    "training_remote",
    "downloading_model",
    "testing",
    "deploying",
}

# M0 placeholder only: no real stage logic exists yet, so a stage call has
# nothing blocking to do. This delay just gives the busy window enough width
# to be deterministically observable (see test_concurrent_stage_call_rejected).
# Replaced stage-by-stage by real BackgroundTasks/thread workers in M2-M8.
STUB_STAGE_DELAY_SECONDS = 1.5


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

    @router.post("/pipeline/runs/{rid}/stage/{stage}")
    async def run_stage(
        rid: str, stage: str, background: BackgroundTasks, current=Depends(get_current_user)
    ):
        run = await db.pipeline_runs.find_one({"id": rid})
        if not run:
            raise HTTPException(status_code=404, detail="Not found")
        await s._project_access_check(run["project_id"], current["id"], roles=["owner", "admin"])

        if stage not in VALID_STAGES:
            raise HTTPException(status_code=400, detail=f"Unknown stage '{stage}'")

        if stage == "deploying" and run.get("run_type") == "bootstrap":
            raise HTTPException(status_code=400, detail="Bootstrap runs cannot deploy")

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

        async def _stub_stage_job():
            await asyncio.sleep(STUB_STAGE_DELAY_SECONDS)
            finished = datetime.now(timezone.utc).isoformat()
            await db.pipeline_runs.update_one(
                {"id": rid},
                {
                    "$set": {
                        "busy": False,
                        "updated_at": finished,
                        f"stage_history.{history_index}.status": "stub_complete",
                        f"stage_history.{history_index}.finished_at": finished,
                    }
                },
            )

        background.add_task(_stub_stage_job)

        updated = await db.pipeline_runs.find_one({"id": rid}, {"_id": 0})
        return updated
