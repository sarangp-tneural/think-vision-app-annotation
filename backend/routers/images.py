"""Image endpoints: upload, list, get, delete, annotations, assign, submit, review, file download."""
import asyncio
import hashlib
import io
import uuid
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, UploadFile, File
from fastapi.responses import Response
from pydantic import BaseModel

router = APIRouter()


class BulkDeleteIn(BaseModel):
    image_ids: list[str]
    include_approved: bool = False  # approved images are only deleted when this is set


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
        _, role = await s._project_access(pid, current["id"])
        q = {"project_id": pid}
        if status:
            q["review_status"] = status
        if assignee == "me" or role in s.RESTRICTED_VIEW_ROLES:  # annotators only ever see their own images
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

    @router.get("/projects/{pid}/images/next")
    async def next_image(pid: str, mode: str, after: Optional[str] = None, current=Depends(get_current_user)):
        """Next image that still needs this user's action, in the same order as the image list.
        mode=annotate: my open images (owner/admin also unassigned ones); mode=review: submitted images
        I am allowed to review. Wraps around to the top; id is null when nothing is waiting."""
        project = await s._verify_project(pid, current["id"])
        me = current["id"]
        if mode == "annotate":
            role = await s._role_in_project(project, me)
            mine = {"assigned_to": me, "review_status": {"$in": ["assigned", "rejected"]}}
            q = {"project_id": pid, "$or": [mine, {"assigned_to": {"$in": [None, ""]}, "review_status": {"$in": [None, "", "unassigned"]}}]} \
                if role in ("owner", "admin") else {"project_id": pid, **mine}
        elif mode == "review":
            q = {"project_id": pid, "review_status": "submitted"}
            if await s._role_in_project(project, me) not in ("owner", "admin"):  # images given to another reviewer aren't mine
                q["$or"] = [{"reviewer_id": {"$in": [None, ""]}}, {"reviewer_id": me}]
            tid = project.get("team_id")
            if tid and await s._has_other_reviewer(tid, me):  # can't review own work, so don't land on it
                q["annotated_by"] = {"$ne": me}
                q["assigned_to"] = {"$ne": me}
        else:
            raise HTTPException(status_code=400, detail="mode must be annotate or review")
        order = [("created_at", -1), ("id", -1)]
        cur = await db.images.find_one({"id": after, "project_id": pid}, {"created_at": 1, "id": 1}) if after else None
        if cur:
            later = {**q, "$and": [{"$or": [
                {"created_at": {"$lt": cur.get("created_at") or ""}},
                {"created_at": cur.get("created_at") or "", "id": {"$lt": cur["id"]}},
            ]}]}
            hit = await db.images.find_one(later, {"id": 1}, sort=order)
            if hit:
                return {"id": hit["id"]}
            q = {**q, "id": {"$ne": after}}
        hit = await db.images.find_one(q, {"id": 1}, sort=order)
        return {"id": hit["id"] if hit else None}

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

    async def _after_submit(img: dict, actor_id: str):
        """Activity log + reviewer notifications; runs after the response so submitting stays instant."""
        await s._log_activity(img["project_id"], actor_id, "image_submitted", {"image_id": img["id"]})
        project = await db.projects.find_one({"id": img["project_id"]}, {"name": 1, "team_id": 1})
        if project and project.get("team_id"):
            reviewers = await db.team_members.find({"team_id": project["team_id"], "role": {"$in": ["owner", "admin", "reviewer"]}, "status": "active"}).to_list(50)
            if img.get("reviewer_id"):  # someone was picked to review it: don't ping the rest of the pool
                reviewers = [r for r in reviewers if r["user_id"] == img["reviewer_id"]]
            for r in reviewers:
                if r["user_id"] != actor_id:
                    await s._notify(r["user_id"], "review_needed", f"Image submitted for review in '{project['name']}'", project_id=img["project_id"], image_id=img["id"])

    def _submit_fields():
        return {
            "review_status": "submitted", "submitted_at": datetime.now(timezone.utc).isoformat(),
            "review_notes": "", "reviewed_by": None, "reviewed_at": None,  # previous verdict no longer applies
        }

    @router.post("/images/{img_id}/submit")
    async def submit_image(img_id: str, background: BackgroundTasks, current=Depends(get_current_user)):
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
        await db.images.update_one({"id": img_id}, {"$set": _submit_fields()})
        background.add_task(_after_submit, img, current["id"])
        return {"ok": True}

    async def _after_review(img: dict, project: dict, payload, actor_id: str):
        """Activity log, notification and retrain nudge; runs after the response so reviewing stays instant."""
        approved = payload.decision == "approve"
        await s._log_activity(img["project_id"], actor_id, "image_approved" if approved else "image_rejected", {"image_id": img["id"], "notes": payload.notes or ""})
        if img.get("assigned_to") and img["assigned_to"] != actor_id:
            kind = "review_completed" if approved else "rejected"
            title = "Your annotation was approved" if approved else "Your annotation was rejected"
            await s._notify(img["assigned_to"], kind, title, project_id=img["project_id"], image_id=img["id"], meta={"notes": payload.notes or ""})
        if approved:
            await s._maybe_send_retrain_nudge(project, actor_id)

    @router.post("/images/{img_id}/review")
    async def review_image(img_id: str, payload: s.ReviewImageIn, background: BackgroundTasks, current=Depends(get_current_user)):
        img = await db.images.find_one({"id": img_id})
        if not img:
            raise HTTPException(status_code=404, detail="Not found")
        project = await db.projects.find_one({"id": img["project_id"]})
        tid = project.get("team_id")
        if tid:
            m = await s._get_membership(tid, current["id"])
            if not m or m["role"] not in ["owner", "admin", "reviewer"]:
                raise HTTPException(status_code=403, detail="Only owners/admins/reviewers can review")
            if img.get("reviewer_id") and img["reviewer_id"] != current["id"] and m["role"] not in ["owner", "admin"]:
                raise HTTPException(status_code=403, detail="This image is assigned to another reviewer")
        else:
            await s._project_access_check(img["project_id"], current["id"])
        if payload.decision not in ["approve", "reject"]:
            raise HTTPException(status_code=400, detail="Invalid decision")
        if img.get("review_status") != "submitted":
            raise HTTPException(status_code=409, detail="Only submitted images can be reviewed")
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
        background.add_task(_after_review, img, project, payload, current["id"])
        return {"ok": True}

    @router.post("/images/{img_id}/withdraw")
    async def withdraw_image(img_id: str, current=Depends(get_current_user)):
        """submitted -> assigned, so the annotator can fix a mistake without waiting for a reviewer to reject it.
        They edit and submit again as usual. Approved images stay locked (see /reopen)."""
        img = await db.images.find_one({"id": img_id})
        if not img:
            raise HTTPException(status_code=404, detail="Not found")
        _, role = await s._project_access(img["project_id"], current["id"])
        if role == "reviewer":
            raise HTTPException(status_code=403, detail="Reviewers can't withdraw a submission")
        if img.get("assigned_to") != current["id"] and role not in ("owner", "admin"):
            raise HTTPException(status_code=403, detail="Only the person this image is assigned to can withdraw it")
        # Match on the status so this can't race a reviewer's approve/reject.
        res = await db.images.update_one({"id": img_id, "review_status": "submitted"},
                                         {"$set": {"review_status": "assigned", "submitted_at": None}})
        if not res.matched_count:
            raise HTTPException(status_code=409, detail="Image is no longer waiting for review")
        await s._log_activity(img["project_id"], current["id"], "image_withdrawn", {"image_id": img_id})
        return {"ok": True, "review_status": "assigned"}

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
        await s._image_view_check(img, current["id"])
        return img

    @router.get("/files/{path:path}")
    async def download_file(path: str, current=Depends(get_current_user)):
        img = await db.images.find_one({"storage_path": path})
        if not img:
            raise HTTPException(status_code=404, detail="Not found")
        await s._image_view_check(img, current["id"])
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
    async def save_annotations(img_id: str, payload: s.SaveAnnotationsIn, background: BackgroundTasks, current=Depends(get_current_user)):
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
        if payload.submit:
            # Save + submit in one request. The edit check already limited this to open, own images;
            # checked before the write so a refused submit saves nothing.
            if not boxes:
                raise HTTPException(status_code=400, detail="Add at least one annotation before submitting")
            changes.update(_submit_fields())
        await db.images.update_one({"id": img_id}, {"$set": changes})
        if payload.submit:
            background.add_task(_after_submit, img, current["id"])
        if new_labels:
            # $addToSet/$each appends atomically, so concurrent saves can't overwrite each other's labels.
            await db.projects.update_one({"id": img["project_id"]}, {"$addToSet": {"classes": {"$each": new_labels}}})
            project = await db.projects.find_one({"id": img["project_id"]}, {"classes": 1})
        updated_classes = project.get("classes", [])
        # Returned so the frontend doesn't need a separate GET /projects/{pid}
        # just to pick up newly-introduced labels after every save.
        return {"ok": True, "count": len(boxes), "classes": updated_classes,
                **({"submitted": True, "review_status": "submitted"} if payload.submit else {})}

    async def _delete_images(pid: str, ids: list, include_approved: bool, actor_id: str) -> dict:
        """Delete images of one project (and their comments); approved ones only when asked for."""
        found = await db.images.find({"id": {"$in": ids}, "project_id": pid}, {"id": 1, "review_status": 1}).to_list(len(ids) or 1)
        approved = [i["id"] for i in found if i.get("review_status") == "approved"]
        doomed = [i["id"] for i in found if include_approved or i["id"] not in set(approved)]
        if doomed:
            await db.images.delete_many({"id": {"$in": doomed}})
            await db.comments.delete_many({"image_id": {"$in": doomed}})
            await s._log_activity(pid, actor_id, "images_deleted", {"count": len(doomed)})
        return {"deleted": len(doomed), "skipped_approved": 0 if include_approved else len(approved)}

    @router.post("/projects/{pid}/images/bulk-delete")
    async def bulk_delete_images(pid: str, payload: BulkDeleteIn, current=Depends(get_current_user)):
        await s._project_access_check(pid, current["id"], roles=["owner", "admin"])
        return await _delete_images(pid, list(dict.fromkeys(payload.image_ids)), payload.include_approved, current["id"])

    @router.delete("/images/{img_id}")
    async def delete_image(img_id: str, force: bool = False, current=Depends(get_current_user)):
        img = await db.images.find_one({"id": img_id})
        if not img:
            raise HTTPException(status_code=404, detail="Not found")
        await s._project_access_check(img["project_id"], current["id"], roles=["owner", "admin"])
        if img.get("review_status") == "approved" and not force:
            raise HTTPException(status_code=409, detail="Approved images cannot be deleted")
        await _delete_images(img["project_id"], [img_id], force, current["id"])
        return {"ok": True}
