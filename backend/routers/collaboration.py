"""Comments (add, reply, resolve, list) + notifications + activity endpoints."""
import uuid
from datetime import datetime, timedelta, timezone
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
        await s._image_view_check(img, current["id"])
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
        await s._image_view_check(img, current["id"])
        comments = await db.comments.find({"image_id": img_id}, {"_id": 0}).to_list(500)
        comments.sort(key=lambda x: x["created_at"])
        return comments

    @router.get("/projects/{pid}/activity")
    async def get_activity(pid: str, limit: int = 200, current=Depends(get_current_user)):
        await s._project_access_check(pid, current["id"])
        activity = await db.activity_log.find({"project_id": pid}, {"_id": 0}).sort([("created_at", -1)]).to_list(limit)
        return activity

    def _hours_between(a, b):
        try:
            return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 3600
        except (TypeError, ValueError):
            return None

    @router.get("/projects/{pid}/activity/summary")
    async def activity_summary(pid: str, days: int = 30, current=Depends(get_current_user)):
        """Per-person workload and results for a project: assigned vs done, rejection rate, overdue, review time.
        `days` only limits the daily trend; the per-person numbers are current totals. Annotators see only themselves."""
        project, role = await s._project_access(pid, current["id"])
        sees_all = role in ("owner", "admin", "reviewer")
        days = max(1, min(days, 365))

        imgs = await db.images.aggregate([
            {"$match": {"project_id": pid}},
            {"$project": {"_id": 0, "assigned_to": 1, "review_status": 1, "reviewer_id": 1, "reviewed_by": 1,
                          "submitted_at": 1, "reviewed_at": 1, "due_date": 1, "annotated_at": 1,
                          "boxes": {"$size": {"$ifNull": ["$annotations", []]}}}},
        ]).to_list(None)

        totals = {k: 0 for k in ("unassigned", "assigned", "submitted", "approved", "rejected")}
        today = datetime.now(timezone.utc).date().isoformat()
        people, reviewers = {}, {}

        def person(uid):
            return people.setdefault(uid, {"user_id": uid, "assigned": 0, "in_progress": 0, "submitted": 0, "approved": 0,
                                           "rejected": 0, "overdue": 0, "boxes": 0, "_hours": [], "last_active": None})

        for im in imgs:
            st = im.get("review_status") or "unassigned"
            totals[st] = totals.get(st, 0) + 1
            uid = im.get("assigned_to")
            if uid:
                p = person(uid)
                p["assigned"] += 1
                p["in_progress" if st == "assigned" else st] += 1
                if st == "approved":
                    p["boxes"] += im["boxes"]
                if st != "approved" and (im.get("due_date") or "")[:10] and im["due_date"][:10] < today:
                    p["overdue"] += 1
                h = _hours_between(im.get("submitted_at"), im.get("reviewed_at")) if st in ("approved", "rejected") else None
                if h is not None and h >= 0:
                    p["_hours"].append(h)
                last = im.get("annotated_at") or im.get("submitted_at")
                if last and (p["last_active"] is None or last > p["last_active"]):
                    p["last_active"] = last
            rid = im.get("reviewed_by") if st in ("approved", "rejected") else im.get("reviewer_id")
            if rid:
                r = reviewers.setdefault(rid, {"user_id": rid, "approved": 0, "rejected": 0, "queue": 0})
                if st in ("approved", "rejected"):
                    r[st] += 1
                elif st == "submitted":
                    r["queue"] += 1

        if not sees_all:
            people = {k: v for k, v in people.items() if k == current["id"]}
            reviewers = {k: v for k, v in reviewers.items() if k == current["id"]}

        names = {u["id"]: u for u in await db.users.find(
            {"id": {"$in": list({*people, *reviewers})}, "role": {"$ne": "manager"}}, {"_id": 0, "id": 1, "name": 1}).to_list(500)}
        rows = []
        for uid, p in people.items():
            if uid not in names:
                continue
            hrs = p.pop("_hours")
            decided = p["approved"] + p["rejected"]
            # "Done" is the annotator's part: submitted for review or already approved. Rejected comes back to them.
            done = p["submitted"] + p["approved"]
            p.update(name=names[uid]["name"], done=done,
                     done_pct=round(done / p["assigned"] * 100, 1) if p["assigned"] else 0,
                     rejection_rate=round(p["rejected"] / decided * 100, 1) if decided else None,
                     avg_review_hours=round(sum(hrs) / len(hrs), 1) if hrs else None)
            rows.append(p)
        rows.sort(key=lambda r: r["assigned"], reverse=True)
        reviewer_rows = [{**r, "name": names[uid]["name"]} for uid, r in reviewers.items() if uid in names]
        reviewer_rows.sort(key=lambda r: r["approved"] + r["rejected"] + r["queue"], reverse=True)

        # Image fields only hold current state, so the trend comes from the activity log.
        start = (datetime.now(timezone.utc) - timedelta(days=days - 1)).date()
        trend = {(start + timedelta(days=i)).isoformat(): {"submitted": 0, "approved": 0, "rejected": 0} for i in range(days)}
        match = {"project_id": pid, "action": {"$in": ["image_submitted", "image_approved", "image_rejected"]},
                 "created_at": {"$gte": start.isoformat()}}
        if not sees_all:
            match["user_id"] = current["id"]
        for ev in await db.activity_log.find(match, {"_id": 0, "action": 1, "created_at": 1}).to_list(50000):
            day = trend.get(ev["created_at"][:10])
            if day is not None:
                day[ev["action"].replace("image_", "")] += 1

        total = sum(totals.values())
        return {
            "total": total, "counts": totals,
            "completion_pct": round(totals["approved"] / total * 100, 1) if total else 0,
            "people": rows, "reviewers": reviewer_rows,
            "daily": [{"date": d, **v} for d, v in trend.items()],
            "scope": "team" if sees_all else "self",
        }

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
