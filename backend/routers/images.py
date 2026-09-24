"""Image endpoints: upload, list, get, delete, annotations, assign, submit, review, file download."""
import uuid
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import Response

router = APIRouter()


def register(s):
    db = s.db
    get_current_user = s.get_current_user
    APP_NAME = s.APP_NAME
    logger = s.logger

    @router.post("/projects/{pid}/images")
    async def upload_image(pid: str, file: UploadFile = File(...), current=Depends(get_current_user)):
        await s._verify_project(pid, current["id"])
        ext = (file.filename.rsplit(".", 1)[-1] if "." in file.filename else "bin").lower()
        if ext not in ["jpg", "jpeg", "png", "webp", "gif"]:
            raise HTTPException(status_code=400, detail="Unsupported file type")
        content = await file.read()
        if len(content) > 10 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="File too large (max 10MB)")
        img_id = str(uuid.uuid4())
        path = f"{APP_NAME}/projects/{pid}/{img_id}.{ext}"
        try:
            result = s.put_object(path, content, file.content_type or f"image/{ext}")
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
        }
        await db.images.insert_one(doc)
        doc.pop("_id", None)
        return doc

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
        imgs = await db.images.find(q, {"_id": 0}).to_list(2000)
        imgs.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        return imgs

    @router.post("/images/{img_id}/assign")
    async def assign_image(img_id: str, payload: s.AssignImageIn, current=Depends(get_current_user)):
        img = await db.images.find_one({"id": img_id})
        if not img:
            raise HTTPException(status_code=404, detail="Not found")
        await s._project_access_check(img["project_id"], current["id"])
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
        if not img.get("annotations"):
            raise HTTPException(status_code=400, detail="Add at least one annotation before submitting")
        await db.images.update_one({"id": img_id}, {"$set": {"review_status": "submitted", "submitted_at": datetime.now(timezone.utc).isoformat()}})
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
        await db.images.update_one({"id": img_id}, {"$set": {
            "review_status": "approved" if payload.decision == "approve" else "rejected",
            "review_notes": payload.notes or "",
            "reviewed_by": current["id"],
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
        }})
        await s._log_activity(img["project_id"], current["id"], f"image_{payload.decision}d", {"image_id": img_id, "notes": payload.notes or ""})
        if img.get("assigned_to") and img["assigned_to"] != current["id"]:
            kind = "review_completed" if payload.decision == "approve" else "rejected"
            title = "Your annotation was approved" if payload.decision == "approve" else "Your annotation was rejected"
            await s._notify(img["assigned_to"], kind, title, project_id=img["project_id"], image_id=img_id, meta={"notes": payload.notes or ""})
        if payload.decision == "approve":
            await s._maybe_send_retrain_nudge(project, current["id"])
        return {"ok": True}

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
            data, ct = s.get_object(path)
        except Exception as e:
            logger.error(f"Fetch failed: {e}")
            raise HTTPException(status_code=500, detail="File fetch failed")
        return Response(content=data, media_type=img.get("content_type", ct))

    @router.put("/images/{img_id}/annotations")
    async def save_annotations(img_id: str, payload: s.SaveAnnotationsIn, current=Depends(get_current_user)):
        img = await db.images.find_one({"id": img_id})
        if not img:
            raise HTTPException(status_code=404, detail="Not found")
        await s._project_access_check(img["project_id"], current["id"])
        boxes = [b.model_dump() for b in payload.boxes]
        await db.images.update_one({"id": img_id}, {"$set": {"annotations": boxes, "annotated": len(boxes) > 0}})
        project = await db.projects.find_one({"id": img["project_id"]})
        existing = set(project.get("classes", []))
        for b in boxes:
            existing.add(b["label"])
        await db.projects.update_one({"id": img["project_id"]}, {"$set": {"classes": sorted(list(existing))}})
        return {"ok": True, "count": len(boxes)}

    @router.delete("/images/{img_id}")
    async def delete_image(img_id: str, current=Depends(get_current_user)):
        img = await db.images.find_one({"id": img_id})
        if not img:
            raise HTTPException(status_code=404, detail="Not found")
        await s._project_access_check(img["project_id"], current["id"])
        await db.images.delete_one({"id": img_id})
        return {"ok": True}
