"""Training endpoints (extracted from server.py).

Uses a `register(server_module)` pattern to attach routes to a shared APIRouter
using helpers from the server module. server.py imports & calls register(...)
after all its definitions are in place — avoiding circular import issues.
"""
import asyncio
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

import remote_training
from schemas.deployment import InspectRemoteRequest, TrainRemoteRequest

router = APIRouter()


def register(s):
    db = s.db
    get_current_user = s.get_current_user

    @router.post("/projects/{pid}/train-real")
    async def train_real_model(
        pid: str,
        background: BackgroundTasks,
        epochs: int = 0,
        model_arch: str = "yolov8n",
        current=Depends(get_current_user),
    ):
        await s._require_not_reviewer(pid, current["id"])
        if epochs < 0 or epochs > 200:
            raise HTTPException(status_code=400, detail="epochs must be between 0 (auto) and 200")
        approved = await db.images.count_documents({"project_id": pid, "review_status": "approved", "annotations.0": {"$exists": True}})
        annotated = approved  # only reviewed-and-approved images are used for training
        total_eligible = approved
        if total_eligible < s.MIN_TRAINING_IMAGES:
            raise HTTPException(status_code=400, detail=f"Need at least {s.MIN_TRAINING_IMAGES} approved images (have {total_eligible})")
        effective_epochs = s.auto_scale_epochs(total_eligible, requested=epochs if epochs > 0 else None)
        mid = str(uuid.uuid4())
        doc = {
            "id": mid, "project_id": pid, "user_id": current["id"], "type": "real",
            "name": None,
            "model_arch": model_arch, "epochs": effective_epochs,
            "epochs_requested": epochs, "epochs_auto_scaled": epochs == 0,
            "status": "queued", "is_active": False,
            "purpose": "assist",  # drives auto-label only; production deploys go through the pipeline
            "training_image_count": total_eligible,
            "progress": 0, "current_epoch": 0,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        await db.models.insert_one(doc)
        doc.pop("_id", None)

        async def _job():
            await asyncio.to_thread(s._train_yolo_sync, mid, pid, model_arch, effective_epochs)
        background.add_task(_job)
        await s._log_activity(pid, current["id"], "training_started", {"model_id": mid, "epochs": effective_epochs, "auto_scaled": epochs == 0})
        return doc

    # Non-secret fields remembered per project so the form is prefilled next time. Never credentials.
    _REMOTE_FORM_KEYS = ("host", "port", "username", "remote_workdir", "env_mode", "venv_path", "model_arch", "epochs")

    @router.post("/projects/{pid}/train-remote")
    async def train_remote_model(pid: str, payload: TrainRemoteRequest, background: BackgroundTasks, current=Depends(get_current_user)):
        """Same as train-real, but the training runs on a host server over SSH. Owner/admin only, like the deploy pipeline."""
        await s._project_access_check(pid, current["id"], roles=["owner", "admin"])
        approved = await db.images.count_documents({"project_id": pid, "review_status": "approved", "annotations.0": {"$exists": True}})
        if approved < s.MIN_TRAINING_IMAGES:
            raise HTTPException(status_code=400, detail=f"Need at least {s.MIN_TRAINING_IMAGES} approved images (have {approved})")
        effective_epochs = s.auto_scale_epochs(approved, requested=payload.epochs if payload.epochs > 0 else None)
        mid = str(uuid.uuid4())
        doc = {
            "id": mid, "project_id": pid, "user_id": current["id"], "type": "real",
            "name": None, "model_arch": payload.model_arch, "epochs": effective_epochs,
            "epochs_requested": payload.epochs, "epochs_auto_scaled": payload.epochs == 0,
            "status": "queued", "is_active": False, "purpose": "assist",
            "source": "server", "trained_on": "server",
            "training_image_count": approved, "progress": 0, "current_epoch": 0,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        await db.models.insert_one(doc)
        doc.pop("_id", None)
        form = payload.model_dump()
        await db.projects.update_one({"id": pid}, {"$set": {"remote_train_form": {k: form[k] for k in _REMOTE_FORM_KEYS if k in form}}})
        req = {**form, "epochs": effective_epochs}  # credentials stay in this dict only

        async def _job():
            await asyncio.to_thread(remote_training.train_remote_sync, mid, pid, req, s.get_object, s.put_object, s.APP_NAME)
        background.add_task(_job)
        await s._log_activity(pid, current["id"], "training_started", {"model_id": mid, "epochs": effective_epochs, "remote": True})
        return doc

    @router.post("/projects/{pid}/inspect-host")
    async def inspect_host(pid: str, payload: InspectRemoteRequest, current=Depends(get_current_user)):
        """Read-only probe of a host (venv present? existing .pt files?) for the Model Training form."""
        await s._project_access_check(pid, current["id"], roles=["owner", "admin"])
        from routers.deployment import _inspect_remote_sync
        try:
            return await asyncio.to_thread(_inspect_remote_sync, payload)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Could not inspect host: {type(e).__name__}")

    @router.get("/projects/{pid}/remote-train-form")
    async def remote_train_form(pid: str, current=Depends(get_current_user)):
        p = await s._project_access_check(pid, current["id"])
        return p.get("remote_train_form") or {}

    @router.get("/models/{mid}")
    async def get_model(mid: str, current=Depends(get_current_user)):
        m = await db.models.find_one({"id": mid}, {"_id": 0})
        if not m:
            raise HTTPException(status_code=404, detail="Not found")
        await s._project_access_check(m["project_id"], current["id"])
        return m

    @router.patch("/models/{mid}")
    async def rename_model(mid: str, payload: dict, current=Depends(get_current_user)):
        m = await db.models.find_one({"id": mid})
        if not m:
            raise HTTPException(status_code=404, detail="Not found")
        await s._project_access_check(m["project_id"], current["id"], roles=["owner", "admin"])
        name = (payload.get("name") or "").strip()[:100] or None
        await db.models.update_one({"id": mid}, {"$set": {"name": name}})
        return {"name": name}

    @router.delete("/models/{mid}")
    async def delete_model(mid: str, current=Depends(get_current_user)):
        m = await db.models.find_one({"id": mid})
        if not m:
            raise HTTPException(status_code=404, detail="Not found")
        await s._project_access_check(m["project_id"], current["id"], roles=["owner", "admin"])
        await db.models.delete_one({"id": mid})
        # Drop the in-memory copy too so a deleted model can't keep predicting.
        s._yolo_cache.pop(m.get("weights_path"), None)
        await s.purge_files([m.get("weights_path")], {"models": "weights_path"})
        await s._log_activity(m["project_id"], current["id"], "model_deleted", {"model_id": mid})
        return {"ok": True}

    @router.post("/models/{mid}/activate")
    async def activate_model(mid: str, current=Depends(get_current_user)):
        m = await db.models.find_one({"id": mid})
        if not m:
            raise HTTPException(status_code=404, detail="Not found")
        p = await s._project_access_check(m["project_id"], current["id"])
        role = await s._role_in_project(p, current["id"])
        purpose = m.get("purpose") or ("production" if m.get("source") else "assist")
        if role not in ("owner", "admin") and not (m.get("user_id") == current["id"] and purpose == "assist" and role != "reviewer"):
            raise HTTPException(status_code=403, detail="Only owners/admins, or the person who trained an assist model, can activate it")
        if m.get("type") == "real" and m.get("status") != "trained":
            raise HTTPException(status_code=400, detail="Model not yet trained")
        if m.get("type") == "real" and not m.get("weights_path"):
            raise HTTPException(status_code=400, detail="No weights available")
        await db.models.update_many({"project_id": m["project_id"]}, {"$set": {"is_active": False}})
        await db.models.update_one({"id": mid}, {"$set": {"is_active": True, "activated_at": datetime.now(timezone.utc).isoformat()}})
        await s._log_activity(m["project_id"], current["id"], "model_activated", {"model_id": mid})
        return {"ok": True}

    @router.post("/projects/{pid}/models/use-gemini")
    async def use_gemini(pid: str, current=Depends(get_current_user)):
        await s._project_access_check(pid, current["id"], roles=["owner", "admin"])
        await db.models.update_many({"project_id": pid, "is_active": True}, {"$set": {"is_active": False}})
        await s._log_activity(pid, current["id"], "gemini_selected", {})
        return {"ok": True}

    @router.post("/models/{mid}/cancel")
    async def cancel_model_training(mid: str, current=Depends(get_current_user)):
        m = await db.models.find_one({"id": mid})
        if not m:
            raise HTTPException(status_code=404, detail="Not found")
        await s._project_access_check(m["project_id"], current["id"], roles=["owner", "admin"])
        if m.get("status") not in ["queued", "preparing", "training"]:
            raise HTTPException(status_code=400, detail=f"Cannot cancel run in status '{m.get('status')}'")
        await db.models.update_one({"id": mid}, {"$set": {"cancel_requested": True}})
        if m.get("status") == "queued":
            await db.models.update_one({"id": mid}, {"$set": {"status": "cancelled"}})
        await s._log_activity(m["project_id"], current["id"], "training_cancelled", {"model_id": mid})
        return {"ok": True}

    @router.get("/projects/{pid}/training-plan")
    async def training_plan_preview(pid: str, current=Depends(get_current_user)):
        await s._project_access_check(pid, current["id"])
        approved = await db.images.count_documents({"project_id": pid, "review_status": "approved", "annotations.0": {"$exists": True}})
        annotated = approved  # only reviewed-and-approved images are used for training
        total_eligible = approved
        epochs = s.auto_scale_epochs(total_eligible)
        est_seconds = int(total_eligible * epochs * 2)
        return {
            "eligible_images": total_eligible,
            "approved": approved,
            "annotated": annotated,
            "min_required": s.MIN_TRAINING_IMAGES,
            "recommended_epochs": epochs,
            "estimated_seconds_cpu": est_seconds,
            "ready": total_eligible >= s.MIN_TRAINING_IMAGES,
        }
