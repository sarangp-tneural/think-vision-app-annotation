"""Image endpoints: upload, list, get, delete, annotations, assign, submit, review, file download."""
import asyncio
import hashlib
import io
import uuid
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import Response
from pydantic import BaseModel

router = APIRouter()


class ReopenIn(BaseModel):
    notes: str = ""
    assigned_to: Optional[str] = None


def _dimensions(content: bytes):
    """(width, height) of an image, or (None, None) when it can't be decoded (it is stored regardless)."""
    try:
        from PIL import Image
        with Image.open(io.BytesIO(content)) as im:
            return im.size
    except Exception:
        return None, None


def register(s):
    db = s.db
    get_current_user = s.get_current_user
    APP_NAME = s.APP_NAME
    logger = s.logger

    @router.post("/projects/{pid}/images")
    async def upload_image(pid: str, file: UploadFile = File(...), force: bool = False, current=Depends(get_current_user)):
        await s._require_not_reviewer(pid, current["id"])
        ext = (file.filename.rsplit(".", 1)[-1] if "." in file.filename else "bin").lower()
        if ext not in ["jpg", "jpeg", "png", "webp", "gif"]:
            raise HTTPException(status_code=400, detail="Unsupported file type")
        content = await file.read()
        if len(content) > 10 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="File too large (max 10MB)")
        content_hash = hashlib.sha256(content).hexdigest()
        if not force:
            # Identical bytes already in this project: warn instead of silently creating a second copy.
            dup = await db.images.find_one({"project_id": pid, "content_hash": content_hash},
                                           {"_id": 0, "id": 1, "filename": 1, "review_status": 1})
            if dup:
                raise HTTPException(status_code=409, detail={
                    "message": f'Identical to "{dup.get("filename")}" already in this project',
                    "duplicate_of": dup["id"], "filename": dup.get("filename"),
                    "review_status": dup.get("review_status")})
        img_id = str(uuid.uuid4())
        path = f"{APP_NAME}/projects/{pid}/{img_id}.{ext}"
        try:
            result = await asyncio.to_thread(s.put_object, path, content, file.content_type or f"image/{ext}")
        except Exception as e:
            logger.error(f"Storage upload failed: {e}")
            raise HTTPException(status_code=500, detail="Storage upload failed")
        doc = {
            "id": img_id, "project_id": pid, "user_id": current["id"],
            "filename": file.filename, "storage_path": result["path"],
            "content_type": file.content_type or f"image/{ext}",
            "size": result.get("size", len(content)),
            "annotations": [], "annotated": False, "assigned_to": None,
            "review_status": "unassigned", "review_notes": "",
            "reviewed_by": None, "reviewed_at": None, "submitted_at": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
            # fingerprint + size + origin, so duplicates and imports can be told apart later
            "content_hash": content_hash,
            "source": "upload",
        }
        doc["width"], doc["height"] = await asyncio.to_thread(_dimensions, content)
        await db.images.insert_one(doc)
        doc.pop("_id", None)
        return doc

    @router.get("/projects/{pid}/duplicates")
    async def list_duplicates(pid: str, current=Depends(get_current_user)):
        """Groups of images in the project with identical bytes (same content hash), largest groups first.
        Images uploaded before fingerprints existed have no hash and are not included until the backfill has run."""
        await s._verify_project(pid, current["id"])
        groups = await db.images.aggregate([
            {"$match": {"project_id": pid, "content_hash": {"$type": "string"}}},
            {"$group": {"_id": "$content_hash", "count": {"$sum": 1}, "images": {"$push": {
                "id": "$id", "filename": "$filename", "review_status": "$review_status", "source": "$source",
                "split": "$split", "created_at": "$created_at", "storage_path": "$storage_path"}}}},
            {"$match": {"count": {"$gt": 1}}},
            {"$sort": {"count": -1, "_id": 1}},
            {"$limit": 200},
        ]).to_list(200)
        unhashed = await db.images.count_documents({"project_id": pid, "content_hash": {"$exists": False}})
        out = []
        for g in groups:
            imgs = sorted(g["images"], key=lambda i: i.get("created_at") or "")
            out.append({"content_hash": g["_id"], "count": g["count"], "images": imgs})
        return {"groups": out, "unhashed_images": unhashed}

    @router.get("/projects/{pid}/images")
    async def list_images(pid: str, status: Optional[str] = None, assignee: Optional[str] = None, current=Depends(get_current_user)):
        await s._verify_project(pid, current["id"])
        q = {"project_id": pid}
        if status:
            q["review_status"] = status
        if assignee == "me":
            q["assigned_to"] = current["id"]
        elif assignee:
            q["assigned_to"] = assignee
        # Annotations are dropped from the list (they can be large); the list
        # only needs the count. Full annotations come from GET /images/{id}.
        pipeline = [
            {"$match": q},
            {"$sort": {"created_at": -1}},
            {"$limit": 10000},
            {"$addFields": {"annotation_count": {"$size": {"$ifNull": ["$annotations", []]}}}},
            {"$project": {"_id": 0, "annotations": 0}},
        ]
        return await db.images.aggregate(pipeline).to_list(10000)

    @router.post("/images/{img_id}/assign")
    async def assign_image(img_id: str, payload: s.AssignImageIn, current=Depends(get_current_user)):
        img = await db.images.find_one({"id": img_id})
        if not img:
            raise HTTPException(status_code=404, detail="Not found")
        await s._project_access_check(img["project_id"], current["id"], roles=["owner", "admin"])
        if (img.get("review_status") or "unassigned") in s.LOCKED_STATUSES:
            raise HTTPException(status_code=409, detail="Image is under review or approved and cannot be reassigned")
        project = await db.projects.find_one({"id": img["project_id"]})
        tid = project.get("team_id")
        target = payload.user_id
        if target and tid:
            m = await db.team_members.find_one({"team_id": tid, "user_id": target, "status": "active"})
            if not m:
                raise HTTPException(status_code=400, detail="User is not a member of this team")
        update = {"assigned_to": target, "review_status": "assigned" if target else "unassigned"}
        await db.images.update_one({"id": img_id}, {"$set": update})
        await s._log_activity(img["project_id"], current["id"], "image_assigned" if target else "image_unassigned", {"image_id": img_id, "user_id": target})
        if target and target != current["id"]:
            await s._notify(target, "image_assigned", "An image was assigned to you", project_id=img["project_id"], image_id=img_id)
        return {"ok": True, **update}

    @router.post("/images/{img_id}/submit")
    async def submit_image(img_id: str, current=Depends(get_current_user)):
        img = await db.images.find_one({"id": img_id})
        if not img:
            raise HTTPException(status_code=404, detail="Not found")
        await s._project_access_check(img["project_id"], current["id"])
        if img.get("assigned_to") != current["id"]:
            raise HTTPException(status_code=403, detail="Only the person this image is assigned to can submit it")
        if (img.get("review_status") or "unassigned") not in ("assigned", "rejected"):
            raise HTTPException(status_code=409, detail="Image has already been submitted or approved")
        if not img.get("annotations"):
            raise HTTPException(status_code=400, detail="Add at least one annotation before submitting")
        await db.images.update_one({"id": img_id}, {"$set": {
            "review_status": "submitted", "submitted_at": datetime.now(timezone.utc).isoformat(),
            "review_notes": "", "reviewed_by": None, "reviewed_at": None,  # previous verdict no longer applies
        }})
        await s._log_activity(img["project_id"], current["id"], "image_submitted", {"image_id": img_id})
        project = await db.projects.find_one({"id": img["project_id"]})
        if project and project.get("team_id"):
            reviewers = await db.team_members.find({"team_id": project["team_id"], "role": {"$in": ["owner", "admin", "reviewer"]}, "status": "active"}).to_list(50)
            for r in reviewers:
                if r["user_id"] != current["id"]:
                    await s._notify(r["user_id"], "review_needed", f"Image submitted for review in '{project['name']}'", project_id=img["project_id"], image_id=img_id)
        return {"ok": True}

    @router.post("/images/{img_id}/review")
    async def review_image(img_id: str, payload: s.ReviewImageIn, current=Depends(get_current_user)):
        img = await db.images.find_one({"id": img_id})
        if not img:
            raise HTTPException(status_code=404, detail="Not found")
        project = await db.projects.find_one({"id": img["project_id"]})
        tid = project.get("team_id")
        if tid:
            m = await s._get_membership(tid, current["id"])
            if not m or m["role"] not in ["owner", "admin", "reviewer"]:
                raise HTTPException(status_code=403, detail="Only owners/admins/reviewers can review")
        else:
            await s._project_access_check(img["project_id"], current["id"])
        if payload.decision not in ["approve", "reject"]:
            raise HTTPException(status_code=400, detail="Invalid decision")
        if img.get("review_status") != "submitted":
            raise HTTPException(status_code=409, detail="Only submitted images can be reviewed")
        if payload.decision == "reject" and not (payload.notes or "").strip():
            raise HTTPException(status_code=400, detail="Add a note explaining what to fix")
        # Nobody reviews their own work, unless they are the only person who could (solo workspace).
        if current["id"] in (img.get("annotated_by"), img.get("assigned_to")) and tid \
                and await s._has_other_reviewer(tid, current["id"]):
            raise HTTPException(status_code=403, detail="You cannot review your own annotations")
        await db.images.update_one({"id": img_id}, {"$set": {
            "review_status": "approved" if payload.decision == "approve" else "rejected",
            "review_notes": payload.notes or "",
            "reviewed_by": current["id"],
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
        }})
        await s._log_activity(img["project_id"], current["id"], "image_approved" if payload.decision == "approve" else "image_rejected", {"image_id": img_id, "notes": payload.notes or ""})
        if img.get("assigned_to") and img["assigned_to"] != current["id"]:
            kind = "review_completed" if payload.decision == "approve" else "rejected"
            title = "Your annotation was approved" if payload.decision == "approve" else "Your annotation was rejected"
            await s._notify(img["assigned_to"], kind, title, project_id=img["project_id"], image_id=img_id, meta={"notes": payload.notes or ""})
        if payload.decision == "approve":
            await s._maybe_send_retrain_nudge(project, current["id"])
        return {"ok": True}

    @router.post("/images/{img_id}/reopen")
    async def reopen_image(img_id: str, payload: ReopenIn, current=Depends(get_current_user)):
        """approved -> rejected, so an approved (e.g. imported) image can be corrected. It then follows the normal
        loop: the assignee edits and submits, a reviewer approves again. Notes are required, like any rejection."""
        img = await db.images.find_one({"id": img_id})
        if not img:
            raise HTTPException(status_code=404, detail="Not found")
        project = await db.projects.find_one({"id": img["project_id"]})
        tid = project.get("team_id")
        if tid:
            m = await s._get_membership(tid, current["id"])
            if not m or m["role"] not in ["owner", "admin", "reviewer"]:
                raise HTTPException(status_code=403, detail="Only owners/admins/reviewers can reopen an image")
        else:
            await s._project_access_check(img["project_id"], current["id"])
        if img.get("review_status") != "approved":
            raise HTTPException(status_code=409, detail="Only approved images can be reopened")
        if not (payload.notes or "").strip():
            raise HTTPException(status_code=400, detail="Add a note explaining what to fix")
        update = {"review_status": "rejected", "review_notes": payload.notes.strip(),
                  "reviewed_by": current["id"], "reviewed_at": datetime.now(timezone.utc).isoformat()}
        if payload.assigned_to:
            if tid and not await db.team_members.find_one({"team_id": tid, "user_id": payload.assigned_to, "status": "active"}):
                raise HTTPException(status_code=400, detail="User is not a member of this team")
            update["assigned_to"] = payload.assigned_to
        await db.images.update_one({"id": img_id}, {"$set": update})
        await s._log_activity(img["project_id"], current["id"], "image_reopened", {"image_id": img_id, "notes": update["review_notes"]})
        if update.get("assigned_to") and update["assigned_to"] != current["id"]:
            await s._notify(update["assigned_to"], "rejected", "An approved image was reopened for correction",
                            project_id=img["project_id"], image_id=img_id, meta={"notes": update["review_notes"]})
        return {"ok": True, **{k: update[k] for k in ("review_status", "review_notes")}}

    @router.get("/images/{img_id}")
    async def get_image_meta(img_id: str, current=Depends(get_current_user)):
        img = await db.images.find_one({"id": img_id}, {"_id": 0})
        if not img:
            raise HTTPException(status_code=404, detail="Not found")
        await s._project_access_check(img["project_id"], current["id"])
        return img

    @router.get("/files/{path:path}")
    async def download_file(path: str, current=Depends(get_current_user)):
        img = await db.images.find_one({"storage_path": path})
        if not img:
            raise HTTPException(status_code=404, detail="Not found")
        await s._project_access_check(img["project_id"], current["id"])
        try:
            data, ct = await asyncio.to_thread(s.get_object, path)
        except Exception as e:
            logger.error(f"Fetch failed: {e}")
            raise HTTPException(status_code=500, detail="File fetch failed")
        # storage paths are UUID-keyed and immutable, so browsers may cache indefinitely
        return Response(
            content=data,
            media_type=img.get("content_type", ct),
            headers={"Cache-Control": "private, max-age=31536000, immutable"},
        )

    @router.put("/images/{img_id}/annotations")
    async def save_annotations(img_id: str, payload: s.SaveAnnotationsIn, current=Depends(get_current_user)):
        img = await db.images.find_one({"id": img_id})
        if not img:
            raise HTTPException(status_code=404, detail="Not found")
        await s._image_edit_check(img, current["id"])
        boxes = [b.model_dump() for b in payload.boxes]
        project = await db.projects.find_one({"id": img["project_id"]}, {"classes": 1, "classes_locked": 1})
        existing = project.get("classes", [])
        # First-seen order, de-duplicated. Class order is the class index everywhere
        # (export, training, deployment), so new labels are only ever appended.
        new_labels = list(dict.fromkeys(b["label"] for b in boxes if b["label"] not in existing))
        if new_labels and project.get("classes_locked"):
            # Checked before anything is written so a rejected save changes nothing.
            raise HTTPException(
                status_code=422,
                detail={"message": "Unknown labels in a project with locked classes", "unknown_labels": new_labels},
            )
        changes = {
            "annotations": boxes, "annotated": len(boxes) > 0,
            "annotated_by": current["id"], "annotated_at": datetime.now(timezone.utc).isoformat(),
        }
        if not img.get("assigned_to"):  # owner/admin annotating an unassigned image claims it
            changes["assigned_to"] = current["id"]
            changes["review_status"] = "assigned"
        await db.images.update_one({"id": img_id}, {"$set": changes})
        if new_labels:
            # $addToSet/$each appends atomically, so concurrent saves can't overwrite each other's labels.
            await db.projects.update_one({"id": img["project_id"]}, {"$addToSet": {"classes": {"$each": new_labels}}})
            project = await db.projects.find_one({"id": img["project_id"]}, {"classes": 1})
        updated_classes = project.get("classes", [])
        # Returned so the frontend doesn't need a separate GET /projects/{pid}
        # just to pick up newly-introduced labels after every save.
        return {"ok": True, "count": len(boxes), "classes": updated_classes}

    @router.delete("/images/{img_id}")
    async def delete_image(img_id: str, current=Depends(get_current_user)):
        img = await db.images.find_one({"id": img_id})
        if not img:
            raise HTTPException(status_code=404, detail="Not found")
        await s._project_access_check(img["project_id"], current["id"], roles=["owner", "admin"])
        if img.get("review_status") == "approved":
            raise HTTPException(status_code=409, detail="Approved images cannot be deleted")
        await db.images.delete_one({"id": img_id})
        return {"ok": True}
