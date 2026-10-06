"""Dataset import endpoints (YOLO-detect zip): upload -> dry-run report -> class mapping -> commit.

All endpoints are owner/admin only. Import is the single place that may create images that are already
`approved`/`submitted` (see dataset_import/commit.py for the justification and safeguards). Heavy work runs
in a worker thread (BackgroundTasks + asyncio.to_thread, like video/training) and reports progress on the
`import_batches` document; the UI polls GET /imports/{id}.
"""
import asyncio
import os
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

from dataset_import import class_map as class_map_mod
from dataset_import import commit as commit_mod
from dataset_import import scan as scan_mod
from dataset_import import ssh_source
from schemas.deployment import InspectRemoteRequest, SSHCredentials
import ssh_helper

router = APIRouter()

IMPORT_TMP_DIR = os.environ.get("IMPORT_TMP_DIR") or os.path.join(tempfile.gettempdir(), "thinkvision_imports")
MAX_ZIP_BYTES = int(os.environ.get("IMPORT_MAX_ZIP_BYTES", str(20 * 1024 ** 3)))
MAX_IMAGE_BYTES = int(os.environ.get("IMPORT_MAX_IMAGE_BYTES", str(50 * 1024 ** 2)))
MAX_FILES = int(os.environ.get("IMPORT_MAX_FILES", "200000"))
MAX_TOTAL_BYTES = int(os.environ.get("IMPORT_MAX_TOTAL_BYTES", str(40 * 1024 ** 3)))
REVIEW_MODES = ("approved", "submitted")
SOURCE_FORMATS = ("auto", "yolo", "coco", "voc")
LABEL_FORMATS = ("auto", "detect", "seg", "obb")
ACTIVE_STATUSES = ("staging", "scanning", "ready", "committing", "interrupted", "partial")


class MappingEntry(BaseModel):
    server_idx: int
    action: str  # match | new | skip
    target: Optional[str] = None


class CommitIn(BaseModel):
    class_mapping: Optional[List[MappingEntry]] = None
    replace_project_classes: bool = False
    skip_duplicates: bool = True
    reimport_deleted: bool = False
    include_unsupported: Optional[bool] = None
    review_mode: Optional[str] = None


class SshImportIn(SSHCredentials):
    """Credentials are request-only: used by the staging job and then dropped, never stored or logged."""
    data_yaml_path: str
    review_mode: str = "approved"
    include_unsupported: bool = False
    label_format: str = "auto"  # detect | seg | obb | auto


class ApproveAllIn(BaseModel):
    confirm_count: int = Field(ge=0)


def posixpath_basename(p: str) -> str:
    return p.rstrip("/").rsplit("/", 1)[-1] or p


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _public(b: dict, with_report: bool = True) -> dict:
    out = {k: v for k, v in b.items() if k not in ("_id", "zip_path")}
    if not with_report:
        out.pop("report", None)
    return out


def _batch_dir(batch_id: str) -> str:
    return os.path.join(IMPORT_TMP_DIR, batch_id)


def _cleanup_dir(batch_id: str) -> None:
    shutil.rmtree(_batch_dir(batch_id), ignore_errors=True)


async def recover_interrupted(db) -> None:
    """Called at startup: a restart killed any running job. Uploads can't be resumed; scans and commits can."""
    await db.import_batches.update_many(
        {"status": "staging"},
        {"$set": {"status": "failed", "phase": "failed", "active": False, "error": "Transfer interrupted by a server restart. Start the import again."}})
    await db.import_batches.update_many(
        {"status": {"$in": ["scanning", "committing"]}},
        [{"$set": {"resume_phase": "$status", "status": "interrupted", "phase": "interrupted"}}])


def register(s):
    db = s.db
    get_current_user = s.get_current_user
    logger = s.logger

    def _sync_db():
        client = MongoClient(s.MONGO_URL)
        return client, client[s.DB_NAME]

    async def _owned_batch(bid: str, user_id: str) -> dict:
        b = await db.import_batches.find_one({"id": bid})
        if not b:
            raise HTTPException(status_code=404, detail="Import not found")
        await s._project_access_check(b["project_id"], user_id, roles=["owner", "admin"])
        return b

    async def _status_stop(bid: str) -> bool:
        b = await db.import_batches.find_one({"id": bid}, {"status": 1})
        return not b or b["status"] == "cancelled"

    def _stopper(bid: str):
        """Thread-safe 'was this cancelled?' check for the worker thread (its own sync client)."""
        client, sdb = _sync_db()

        def stop() -> bool:
            b = sdb.import_batches.find_one({"id": bid}, {"status": 1})
            return not b or b["status"] == "cancelled"

        return client, sdb, stop

    async def _scan_job(bid: str):
        client, sdb, stop = _stopper(bid)
        try:
            await asyncio.to_thread(scan_mod.scan_zip_sync, sdb, bid, stop, max_image_bytes=MAX_IMAGE_BYTES,
                                    max_files=MAX_FILES, max_total_bytes=MAX_TOTAL_BYTES)
        except Exception as e:
            logger.error(f"Import scan {bid} crashed: {e}")
            await db.import_batches.update_one({"id": bid}, {"$set": {
                "status": "interrupted", "resume_phase": "scanning", "error": f"Scan crashed: {str(e)[:200]}"}})
        finally:
            client.close()

    async def _commit_job(bid: str, user_id: str):
        client, sdb, stop = _stopper(bid)
        try:
            result = await asyncio.to_thread(commit_mod.commit_batch_sync, sdb, s.put_object, bid,
                                             user_id=user_id, app_name=s.APP_NAME, should_stop=stop)
            if result.get("stopped"):
                return
            batch = await db.import_batches.find_one({"id": bid})
            counts = result["counts"]
            if result["failed"]:
                await db.import_batches.update_one({"id": bid}, {"$set": {
                    "status": "partial", "phase": "partial", "counts": counts, "updated_at": _now(),
                    "error": f"{result['failed']} image(s) failed - resume to retry them"}})
                return
            await db.import_batches.update_one({"id": bid}, {"$set": {
                "status": "completed", "phase": "completed", "active": False, "counts": counts,
                "error": None, "completed_at": _now(), "updated_at": _now()}})
            _cleanup_dir(bid)
            await s._log_activity(batch["project_id"], user_id, "dataset_imported", {
                "batch_id": bid, "origin": batch["origin"], "review_mode": batch["review_mode"],
                "imported": counts["items"].get("imported", 0)})
        except Exception as e:
            logger.error(f"Import commit {bid} crashed: {e}")
            await db.import_batches.update_one({"id": bid}, {"$set": {
                "status": "interrupted", "resume_phase": "committing", "phase": "interrupted",
                "error": f"Import crashed: {str(e)[:200]}"}})
        finally:
            client.close()

    @router.post("/projects/{pid}/imports", status_code=202)
    async def create_import(pid: str, background: BackgroundTasks, file: UploadFile = File(...),
                            review_mode: str = Form("approved"), include_unsupported: bool = Form(False),
                            source_format: str = Form("auto"), label_format: str = Form("auto"),
                            current=Depends(get_current_user)):
        await s._require_not_reviewer(pid, current["id"])
        await s._project_access_check(pid, current["id"], roles=["owner", "admin"])
        if review_mode not in REVIEW_MODES:
            raise HTTPException(status_code=400, detail="review_mode must be 'approved' or 'submitted'")
        if source_format not in SOURCE_FORMATS or label_format not in LABEL_FORMATS:
            raise HTTPException(status_code=400, detail=f"source_format must be one of {SOURCE_FORMATS}, label_format one of {LABEL_FORMATS}")
        filename = os.path.basename(file.filename or "dataset.zip")
        if not filename.lower().endswith(".zip"):
            raise HTTPException(status_code=400, detail="Upload a .zip file")
        bid = str(uuid.uuid4())
        batch = {
            "id": bid, "project_id": pid, "user_id": current["id"], "source_type": "zip",
            "origin": f"zip:{filename}", "filename": filename, "zip_path": os.path.join(_batch_dir(bid), "dataset.zip"),
            "status": "staging", "phase": "staging", "active": True, "review_mode": review_mode,
            "include_unsupported": include_unsupported, "source_format": source_format, "label_format": label_format,
            "progress": {"done": 0, "total": 0},
            "report": None, "class_map": None, "counts": None, "error": None,
            "created_at": _now(), "updated_at": _now(),
        }
        try:
            await db.import_batches.insert_one(batch)  # the partial unique index allows one active import per project
        except DuplicateKeyError:
            raise HTTPException(status_code=409, detail="Another import is already in progress for this project. Finish or cancel it first.")
        try:
            os.makedirs(_batch_dir(bid), exist_ok=True)
            written = 0
            with open(batch["zip_path"], "wb") as out:
                while chunk := await file.read(1024 * 1024):
                    written += len(chunk)
                    if written > MAX_ZIP_BYTES:
                        raise HTTPException(status_code=413, detail=f"Zip is over the {MAX_ZIP_BYTES // 1024 ** 3} GB import limit")
                    await asyncio.to_thread(out.write, chunk)
        except HTTPException:
            await db.import_batches.update_one({"id": bid}, {"$set": {"status": "failed", "active": False, "error": "Upload too large"}})
            _cleanup_dir(bid)
            raise
        except OSError as e:
            await db.import_batches.update_one({"id": bid}, {"$set": {"status": "failed", "active": False, "error": f"Could not store the upload: {e.strerror or e}"}})
            _cleanup_dir(bid)
            raise HTTPException(status_code=507, detail="Could not store the upload (disk full?)")
        await db.import_batches.update_one({"id": bid}, {"$set": {"status": "scanning", "phase": "scanning", "updated_at": _now()}})
        background.add_task(_scan_job, bid)
        return {"batch_id": bid, "status": "scanning"}

    async def _ssh_job(bid: str, creds: dict, yaml_path: str):
        """Stage the remote dataset into the batch's zip, then scan it. `creds` lives only in this call's arguments."""
        client, sdb, stop = _stopper(bid)
        try:
            finished = await asyncio.to_thread(
                ssh_source.stage_ssh_sync, sdb, bid, creds, yaml_path, stop,
                max_image_bytes=MAX_IMAGE_BYTES, max_total_bytes=MAX_TOTAL_BYTES, max_files=MAX_FILES)
            if not finished:
                return  # cancelled while copying; the cancel endpoint already cleaned up
            moved = await db.import_batches.update_one(
                {"id": bid, "status": "staging"},
                {"$set": {"status": "scanning", "phase": "scanning", "progress": {"done": 0, "total": 0}, "updated_at": _now()}})
            if moved.matched_count == 0:
                return
            await asyncio.to_thread(scan_mod.scan_zip_sync, sdb, bid, stop, max_image_bytes=MAX_IMAGE_BYTES,
                                    max_files=MAX_FILES, max_total_bytes=MAX_TOTAL_BYTES)
        except Exception as e:
            message = ssh_source.scrub(str(e), creds)
            logger.error(f"SSH import {bid} failed: {message}")
            await db.import_batches.update_one({"id": bid, "status": {"$ne": "cancelled"}}, {"$set": {
                "status": "failed", "phase": "failed", "active": False, "error": message[:500], "updated_at": _now()}})
            _cleanup_dir(bid)
        finally:
            client.close()

    @router.post("/projects/{pid}/imports/ssh/inspect")
    async def inspect_ssh(pid: str, body: InspectRemoteRequest, current=Depends(get_current_user)):
        """Find datasets (data.yaml files) under a server directory so the user can pick one to import."""
        await s._project_access_check(pid, current["id"], roles=["owner", "admin"])
        from routers.deployment import _inspect_remote_sync
        creds = body.model_dump()
        try:
            result = await asyncio.to_thread(_inspect_remote_sync, body)
        except Exception as e:
            raise HTTPException(status_code=502, detail=ssh_source.scrub(str(e), creds)[:500])
        return {"workdir": result["workdir"], "found": result["found"], "datasets": result["datasets"]}

    @router.post("/projects/{pid}/imports/ssh", status_code=202)
    async def create_ssh_import(pid: str, body: SshImportIn, background: BackgroundTasks, current=Depends(get_current_user)):
        await s._require_not_reviewer(pid, current["id"])
        await s._project_access_check(pid, current["id"], roles=["owner", "admin"])
        if body.review_mode not in REVIEW_MODES:
            raise HTTPException(status_code=400, detail="review_mode must be 'approved' or 'submitted'")
        if body.label_format not in LABEL_FORMATS:
            raise HTTPException(status_code=400, detail=f"label_format must be one of {LABEL_FORMATS}")
        yaml_path = body.data_yaml_path.strip()
        if not yaml_path:
            raise HTTPException(status_code=400, detail="data_yaml_path is required")
        creds = {"host": body.host, "port": body.port, "username": body.username,
                 "pem_key": body.pem_key, "password": body.password}
        bid = str(uuid.uuid4())
        batch = {
            "id": bid, "project_id": pid, "user_id": current["id"], "source_type": "ssh",
            # host + yaml path only: no username, no secrets
            "origin": f"ssh:{body.host}:{yaml_path}", "filename": posixpath_basename(yaml_path),
            "zip_path": os.path.join(_batch_dir(bid), "dataset.zip"),
            "status": "staging", "phase": "staging", "active": True, "review_mode": body.review_mode,
            "include_unsupported": body.include_unsupported, "source_format": "yolo", "label_format": body.label_format,
            "progress": {"done": 0, "total": 0},
            "report": None, "class_map": None, "counts": None, "error": None,
            "created_at": _now(), "updated_at": _now(),
        }
        try:
            await db.import_batches.insert_one(batch)
        except DuplicateKeyError:
            raise HTTPException(status_code=409, detail="Another import is already in progress for this project. Finish or cancel it first.")
        background.add_task(_ssh_job, bid, creds, yaml_path)
        return {"batch_id": bid, "status": "staging"}

    @router.get("/projects/{pid}/imports")
    async def list_imports(pid: str, current=Depends(get_current_user)):
        await s._project_access_check(pid, current["id"], roles=["owner", "admin"])
        rows = await db.import_batches.find({"project_id": pid}, {"_id": 0, "zip_path": 0, "report": 0}).sort("created_at", -1).to_list(50)
        return rows

    @router.get("/imports/{bid}")
    async def get_import(bid: str, current=Depends(get_current_user)):
        b = await _owned_batch(bid, current["id"])
        out = _public(b)
        out["pending_count"] = await db.images.count_documents(
            {"project_id": b["project_id"], "provenance.import_batch_id": bid, "review_status": "submitted"})
        return out

    @router.get("/imports/{bid}/items")
    async def list_import_items(bid: str, status: Optional[str] = None, limit: int = 100, skip: int = 0,
                                current=Depends(get_current_user)):
        await _owned_batch(bid, current["id"])
        q = {"batch_id": bid}
        if status:
            q["status"] = status
        rows = await db.import_items.find(q, {"_id": 0, "id": 1, "rel_path": 1, "split": 1, "status": 1, "error": 1,
                                              "size": 1, "n_boxes": 1, "n_unsupported": 1, "n_malformed": 1,
                                              "duplicate_of": 1, "image_id": 1}).sort("rel_path", 1).skip(max(skip, 0)).to_list(min(max(limit, 1), 500))
        return rows

    @router.post("/imports/{bid}/commit", status_code=202)
    async def commit_import(bid: str, payload: CommitIn, background: BackgroundTasks, current=Depends(get_current_user)):
        b = await _owned_batch(bid, current["id"])
        review_mode = payload.review_mode or b["review_mode"]
        if review_mode not in REVIEW_MODES:
            raise HTTPException(status_code=400, detail="review_mode must be 'approved' or 'submitted'")
        # Claim the batch so two commits can't race; reverted below if anything is rejected.
        claimed = await db.import_batches.find_one_and_update(
            {"id": bid, "status": "ready"}, {"$set": {"status": "committing", "phase": "committing", "updated_at": _now()}})
        if not claimed:
            raise HTTPException(status_code=409, detail=f"Import is '{b['status']}', not ready to commit")

        async def _revert():
            await db.import_batches.update_one({"id": bid}, {"$set": {"status": "ready", "phase": "ready"}})

        try:
            project = await db.projects.find_one({"id": b["project_id"]})
            classes = project.get("classes", [])
            if payload.replace_project_classes and project.get("classes_locked"):
                raise HTTPException(status_code=409, detail="Classes are locked: they cannot be replaced")
            can_replace = await db.images.count_documents(
                {"project_id": b["project_id"], "annotations.0": {"$exists": True}}, limit=1) == 0
            try:
                resolved = class_map_mod.resolve_mapping(
                    b["server_names"], classes, [m.model_dump() for m in payload.class_mapping or []],
                    replace_project_classes=payload.replace_project_classes, can_replace=can_replace)
            except ValueError as e:
                raise HTTPException(status_code=422, detail=str(e))
            if resolved["project_classes_after"] != classes:
                # compare-and-set on the whole list: if someone changed classes since we read them, don't clobber it
                res = await db.projects.update_one({"id": b["project_id"], "classes": classes},
                                                   {"$set": {"classes": resolved["project_classes_after"]}})
                if res.matched_count == 0:
                    raise HTTPException(status_code=409, detail="Project classes changed while committing, review the mapping and try again")
            # Only a YOLO dataset has a class order to stay aligned with (its data.yaml, usually also on a server).
            # COCO / Pascal VOC have none, so those imports leave the class list unlocked and unlinked.
            is_yolo = (b.get("resolved_source_format") or "yolo") == "yolo"
            project_set = {"classes_locked": True} if is_yolo else {}
            if is_yolo and not project.get("classes_locked"):
                project_set["class_origin"] = {"source": "import", "data_yaml_path": b.get("data_yaml_member"), "locked_at": _now()}
            remote = project.get("remote_dataset")
            if is_yolo and (not remote or remote.get("server_names") == b["server_names"]):
                # Linked to one dataset at a time; a second import with different class names keeps the first link
                # (each batch keeps its own class_map, which is what push-back uses per image).
                link = {"origin": b["origin"], "server_names": b["server_names"], "label_format": b.get("resolved_label_format", "detect"),
                        "class_map": resolved["class_map"], "data_yaml_path": b.get("data_yaml_member")}
                if b.get("remote"):  # SSH import: record where the dataset lives (never credentials)
                    link.update(host=b["remote"]["host"], port=b["remote"]["port"], data_yaml_path=b["remote"]["data_yaml_path"])
                project_set["remote_dataset"] = link
            if project_set:
                await db.projects.update_one({"id": b["project_id"]}, {"$set": project_set})
        except HTTPException:
            await _revert()
            raise
        except Exception:
            await _revert()
            raise

        await db.import_batches.update_one({"id": bid}, {"$set": {
            "class_map": resolved["class_map"], "new_classes": resolved["new_classes"],
            "review_mode": review_mode, "skip_duplicates": payload.skip_duplicates,
            "reimport_deleted": payload.reimport_deleted,
            "include_unsupported": b.get("include_unsupported", False) if payload.include_unsupported is None else payload.include_unsupported,
            "error": None}})
        background.add_task(_commit_job, bid, current["id"])
        return {"batch_id": bid, "status": "committing", "project_classes": resolved["project_classes_after"]}

    @router.post("/imports/{bid}/resume", status_code=202)
    async def resume_import(bid: str, background: BackgroundTasks, current=Depends(get_current_user)):
        b = await _owned_batch(bid, current["id"])
        if b["status"] not in ("interrupted", "partial"):
            raise HTTPException(status_code=409, detail=f"Import is '{b['status']}', nothing to resume")
        if not os.path.exists(b["zip_path"]):
            raise HTTPException(status_code=410, detail="The staged zip is gone; start a new import")
        phase = b.get("resume_phase") if b["status"] == "interrupted" else "committing"
        if phase == "committing" and b.get("class_map"):
            await db.import_batches.update_one({"id": bid}, {"$set": {"status": "committing", "phase": "committing", "error": None}})
            background.add_task(_commit_job, bid, current["id"])
            return {"batch_id": bid, "status": "committing"}
        await db.import_batches.update_one({"id": bid}, {"$set": {"status": "scanning", "phase": "scanning", "error": None}})
        background.add_task(_scan_job, bid)
        return {"batch_id": bid, "status": "scanning"}

    @router.post("/imports/{bid}/cancel")
    async def cancel_import(bid: str, current=Depends(get_current_user)):
        b = await _owned_batch(bid, current["id"])
        if b["status"] in ("completed", "failed", "cancelled"):
            raise HTTPException(status_code=409, detail=f"Import is already {b['status']}")
        await db.import_batches.update_one({"id": bid}, {"$set": {"status": "cancelled", "phase": "cancelled", "active": False, "updated_at": _now()}})
        _cleanup_dir(bid)
        return {"ok": True}

    @router.delete("/imports/{bid}/images")
    async def rollback_import(bid: str, current=Depends(get_current_user)):
        """Remove the batch's images that nobody has touched since import. Anything annotated, reopened, assigned or
        rejected is kept. Stored image objects are not deleted (same as deleting an image)."""
        b = await _owned_batch(bid, current["id"])
        if b["status"] in ("scanning", "committing", "staging"):
            raise HTTPException(status_code=409, detail="Wait for the import to finish or cancel it first")
        q = {"project_id": b["project_id"], "provenance.import_batch_id": bid, "annotated_by": {"$exists": False},
             "assigned_to": None, "review_status": {"$in": ["unassigned", "submitted", "approved"]}}
        ids = [d["id"] for d in await db.images.find(q, {"_id": 0, "id": 1}).to_list(None)]
        total = await db.images.count_documents({"project_id": b["project_id"], "provenance.import_batch_id": bid})
        if ids:
            await db.images.delete_many({"id": {"$in": ids}})
            await db.import_items.update_many({"batch_id": bid, "image_id": {"$in": ids}}, {"$set": {"status": "rolled_back"}})
        await s._log_activity(b["project_id"], current["id"], "import_rolled_back", {"batch_id": bid, "deleted": len(ids), "kept": total - len(ids)})
        return {"deleted": len(ids), "kept": total - len(ids)}

    @router.post("/imports/{bid}/approve-all")
    async def approve_all(bid: str, payload: ApproveAllIn, current=Depends(get_current_user)):
        """Owner/admin approves every still-submitted image of the batch in one step. Same transition as
        POST /images/{id}/review (submitted -> approved); reviewers keep approving one by one."""
        b = await _owned_batch(bid, current["id"])
        q = {"project_id": b["project_id"], "provenance.import_batch_id": bid, "review_status": "submitted"}
        pending = await db.images.count_documents(q)
        if payload.confirm_count != pending:
            raise HTTPException(status_code=409, detail={"message": "The number of pending images changed, review and confirm again", "pending_count": pending})
        if pending == 0:
            return {"approved": 0}
        res = await db.images.update_many(q, {"$set": {
            "review_status": "approved", "review_notes": "", "reviewed_by": current["id"], "reviewed_at": _now()}})
        await s._log_activity(b["project_id"], current["id"], "import_batch_approved",
                              {"batch_id": bid, "count": res.modified_count})
        project = await db.projects.find_one({"id": b["project_id"]})
        await s._maybe_send_retrain_nudge(project, current["id"])
        return {"approved": res.modified_count}
