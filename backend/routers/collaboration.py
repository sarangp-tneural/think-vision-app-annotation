"""Comments (add, reply, resolve, list) + notifications + activity endpoints."""
import uuid
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException

router = APIRouter()


def register(s):
    db = s.db
    get_current_user = s.get_current_user

    @router.post("/images/{img_id}/comments")
    async def add_comment(img_id: str, payload: s.CommentIn, current=Depends(get_current_user)):
        img = await db.images.find_one({"id": img_id})
        if not img:
            raise HTTPException(status_code=404, detail="Not found")
        await s._project_access_check(img["project_id"], current["id"])
        cid = str(uuid.uuid4())
        doc = {
            "id": cid, "image_id": img_id, "project_id": img["project_id"],
            "user_id": current["id"], "user_name": current["name"], "text": payload.text,
            "parent_id": None, "resolved": False,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        await db.comments.insert_one(doc)
        await s._log_activity(img["project_id"], current["id"], "comment_added", {"image_id": img_id})
        if img.get("assigned_to") and img["assigned_to"] != current["id"]:
            await s._notify(img["assigned_to"], "comment", "New comment on your image in project", project_id=img["project_id"], image_id=img_id)
        doc.pop("_id", None)
        return doc

    @router.post("/comments/{cid}/replies")
    async def reply_comment(cid: str, payload: s.CommentIn, current=Depends(get_current_user)):
        parent = await db.comments.find_one({"id": cid})
        if not parent:
            raise HTTPException(status_code=404, detail="Comment not found")
        await s._project_access_check(parent["project_id"], current["id"])
        rid = str(uuid.uuid4())
        doc = {
            "id": rid, "image_id": parent["image_id"], "project_id": parent["project_id"],
            "user_id": current["id"], "user_name": current["name"], "text": payload.text,
            "parent_id": cid, "resolved": False,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        await db.comments.insert_one(doc)
        if parent["user_id"] != current["id"]:
            await s._notify(parent["user_id"], "comment", "Someone replied to your comment", project_id=parent["project_id"], image_id=parent["image_id"])
        doc.pop("_id", None)
        return doc

    @router.post("/comments/{cid}/resolve")
    async def resolve_comment(cid: str, current=Depends(get_current_user)):
        c = await db.comments.find_one({"id": cid})
        if not c:
            raise HTTPException(status_code=404, detail="Not found")
        await s._project_access_check(c["project_id"], current["id"])
        await db.comments.update_one({"id": cid}, {"$set": {"resolved": True, "resolved_by": current["id"], "resolved_at": datetime.now(timezone.utc).isoformat()}})
        return {"ok": True}

    @router.get("/images/{img_id}/comments")
    async def list_comments(img_id: str, current=Depends(get_current_user)):
        img = await db.images.find_one({"id": img_id})
        if not img:
            raise HTTPException(status_code=404, detail="Not found")
        await s._project_access_check(img["project_id"], current["id"])
        comments = await db.comments.find({"image_id": img_id}, {"_id": 0}).to_list(500)
        comments.sort(key=lambda x: x["created_at"])
        return comments

    @router.get("/projects/{pid}/activity")
    async def get_activity(pid: str, limit: int = 200, current=Depends(get_current_user)):
        await s._project_access_check(pid, current["id"])
        activity = await db.activity_log.find({"project_id": pid}, {"_id": 0}).sort([("created_at", -1)]).to_list(limit)
        return activity

    @router.get("/notifications")
    async def list_notifications(current=Depends(get_current_user)):
        notifs = await db.notifications.find({"user_id": current["id"]}, {"_id": 0}).sort([("created_at", -1)]).to_list(200)
        unread = sum(1 for n in notifs if not n.get("read"))
        return {"items": notifs, "unread": unread}

    @router.post("/notifications/{nid}/read")
    async def mark_notification_read(nid: str, current=Depends(get_current_user)):
        await db.notifications.update_one({"id": nid, "user_id": current["id"]}, {"$set": {"read": True}})
        return {"ok": True}

    @router.post("/notifications/mark-all-read")
    async def mark_all_read(current=Depends(get_current_user)):
        r = await db.notifications.update_many({"user_id": current["id"], "read": False}, {"$set": {"read": True}})
        return {"updated": r.modified_count}
