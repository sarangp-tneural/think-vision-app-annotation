"""Projects endpoints: create, list, get, delete, classes (add/bulk/delete), settings."""
import uuid
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException

router = APIRouter()


def register(s):
    db = s.db
    get_current_user = s.get_current_user

    @router.post("/projects")
    async def create_project(payload: s.ProjectCreate, current=Depends(get_current_user)):
        team_id = payload.team_id
        if team_id:
            await s._require_team_role(team_id, current["id"], ["owner", "admin", "member"])
        else:
            team_id = await s._ensure_personal_team(current["id"], current["name"])
        pid = str(uuid.uuid4())
        doc = {
            "id": pid, "team_id": team_id, "user_id": current["id"],
            "name": payload.name, "description": payload.description or "",
            "task_type": payload.task_type, "classes": [],
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        await db.projects.insert_one(doc)
        return await s._project_out(pid)

    @router.get("/projects")
    async def list_projects(team_id: Optional[str] = None, current=Depends(get_current_user)):
        memberships = await db.team_members.find({"user_id": current["id"], "status": "active"}).to_list(500)
        team_ids = [m["team_id"] for m in memberships]
        role_by_team = {m["team_id"]: m["role"] for m in memberships}
        if team_id:
            if team_id not in team_ids:
                raise HTTPException(status_code=403, detail="Not a team member")
            query = {"team_id": team_id}
        else:
            query = {"team_id": {"$in": team_ids}}
        projects = await db.projects.find(query, {"_id": 0}).to_list(1000)
        legacy = await db.projects.find({"user_id": current["id"], "team_id": {"$exists": False}}, {"_id": 0}).to_list(200)
        projects.extend(legacy)

        my_assigned_pids = set(
            a["project_id"]
            for a in await db.project_assignments.find({"user_id": current["id"]}).to_list(2000)
        )
        filtered = []
        for p in projects:
            tid = p.get("team_id")
            role = role_by_team.get(tid) if tid else "owner"
            if role in ["annotator", "reviewer"]:
                any_assigned = await db.project_assignments.count_documents({"project_id": p["id"]}) > 0
                if any_assigned and p["id"] not in my_assigned_pids:
                    continue
            filtered.append(p)
        projects = filtered

        result = []
        team_name_map = {}
        for p in projects:
            pid = p["id"]
            image_count = await db.images.count_documents({"project_id": pid})
            annotated_count = await db.images.count_documents({"project_id": pid, "annotated": True})
            tid = p.get("team_id")
            if tid and tid not in team_name_map:
                t = await db.teams.find_one({"id": tid}, {"_id": 0})
                team_name_map[tid] = t["name"] if t else "Unknown"
            result.append({
                "id": pid, "name": p["name"], "description": p.get("description", ""),
                "task_type": p.get("task_type", "object_detection"),
                "classes": p.get("classes", []),
                "image_count": image_count, "annotated_count": annotated_count,
                "team_id": tid, "team_name": team_name_map.get(tid, ""),
                "created_at": p["created_at"],
            })
        result.sort(key=lambda x: x["created_at"], reverse=True)
        return result

    @router.get("/projects/{pid}")
    async def get_project(pid: str, current=Depends(get_current_user)):
        await s._project_access_check(pid, current["id"])
        return await s._project_out(pid)

    @router.delete("/projects/{pid}")
    async def delete_project(pid: str, current=Depends(get_current_user)):
        await s._project_access_check(pid, current["id"], roles=["owner", "admin"])
        await db.projects.delete_one({"id": pid})
        await db.images.delete_many({"project_id": pid})
        await db.versions.delete_many({"project_id": pid})
        await db.models.delete_many({"project_id": pid})
        return {"ok": True}

    @router.post("/projects/{pid}/classes")
    async def add_class(pid: str, payload: dict, current=Depends(get_current_user)):
        p = await s._project_access_check(pid, current["id"])
        label = (payload.get("label") or "").strip()
        if not label:
            raise HTTPException(status_code=400, detail="Label required")
        classes = p.get("classes", [])
        if label not in classes:
            classes.append(label)
            await db.projects.update_one({"id": pid}, {"$set": {"classes": classes}})
        return {"classes": classes}

    @router.post("/projects/{pid}/classes/bulk")
    async def add_classes_bulk(pid: str, payload: dict, current=Depends(get_current_user)):
        p = await s._project_access_check(pid, current["id"])
        raw_labels = payload.get("labels") or []
        if isinstance(payload.get("text"), str):
            raw_labels = raw_labels + [t.strip() for t in payload["text"].replace("\n", ",").split(",")]
        cleaned = []
        for label in raw_labels:
            v = str(label).strip().lower()[:40]
            if v and v not in cleaned:
                cleaned.append(v)
        if not cleaned:
            raise HTTPException(status_code=400, detail="No valid labels provided")
        classes = p.get("classes", [])
        added = []
        for label in cleaned:
            if label not in classes:
                classes.append(label)
                added.append(label)
        if added:
            await db.projects.update_one({"id": pid}, {"$set": {"classes": classes}})
        return {"classes": classes, "added": added}

    @router.delete("/projects/{pid}/classes/{label}")
    async def delete_class(pid: str, label: str, current=Depends(get_current_user)):
        await s._project_access_check(pid, current["id"], roles=["owner", "admin"])
        # $pull is atomic per-document - safe when several deletes for
        # different labels run concurrently (e.g. clicking through multiple
        # class chips quickly). A read-then-$set-the-whole-list here would
        # let a slower concurrent request overwrite a faster one's removal.
        await db.projects.update_one({"id": pid}, {"$pull": {"classes": label}})
        p = await db.projects.find_one({"id": pid}, {"_id": 0, "classes": 1})
        return {"classes": p.get("classes", [])}

    @router.patch("/projects/{pid}/settings")
    async def update_project_settings(pid: str, payload: s.ProjectSettingsIn, current=Depends(get_current_user)):
        p = await s._project_access_check(pid, current["id"], roles=["owner", "admin"])
        settings = p.get("settings", {})
        if payload.confidence_threshold is not None:
            val = max(0.05, min(0.95, float(payload.confidence_threshold)))
            settings["confidence_threshold"] = val
        if payload.fallback_to_gemini is not None:
            settings["fallback_to_gemini"] = bool(payload.fallback_to_gemini)
        if payload.min_boxes_threshold is not None:
            settings["min_boxes_threshold"] = max(0, int(payload.min_boxes_threshold))
        if payload.show_confidence is not None:
            settings["show_confidence"] = bool(payload.show_confidence)
        if payload.gemini_restrict_to_classes is not None:
            settings["gemini_restrict_to_classes"] = bool(payload.gemini_restrict_to_classes)
        if payload.gemini_allowed_classes is not None:
            settings["gemini_allowed_classes"] = list(payload.gemini_allowed_classes)
        await db.projects.update_one({"id": pid}, {"$set": {"settings": settings}})
        await s._log_activity(pid, current["id"], "settings_updated", settings)
        return {"settings": settings}
