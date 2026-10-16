"""Teams + invites + members endpoints."""
import uuid
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException

router = APIRouter()


def register(s):
    db = s.db
    get_current_user = s.get_current_user

    @router.get("/teams")
    async def list_teams(current=Depends(get_current_user)):
        memberships = await db.team_members.find({"user_id": current["id"], "status": "active"}).to_list(500)
        team_ids = [m["team_id"] for m in memberships]
        teams = await db.teams.find({"id": {"$in": team_ids}}, {"_id": 0}).to_list(500)
        m_by_tid = {m["team_id"]: m for m in memberships}
        result = []
        for t in teams:
            member_count = await db.team_members.count_documents({"team_id": t["id"], "status": "active"})
            project_count = await db.projects.count_documents({"team_id": t["id"]})
            result.append({
                **t,
                "role": m_by_tid[t["id"]]["role"],
                "is_personal": t.get("is_personal", False),
                "member_count": member_count,
                "project_count": project_count,
            })
        result.sort(key=lambda x: (not x.get("is_personal", False), x.get("created_at", "")))
        return result

    @router.post("/teams")
    async def create_team(payload: s.TeamCreate, current=Depends(get_current_user)):
        tid = str(uuid.uuid4())
        await db.teams.insert_one({
            "id": tid, "name": payload.name, "owner_id": current["id"],
            "is_personal": False, "created_at": datetime.now(timezone.utc).isoformat(),
        })
        await db.team_members.insert_one({
            "id": str(uuid.uuid4()), "team_id": tid, "user_id": current["id"],
            "role": "owner", "status": "active", "is_personal": False,
            "joined_at": datetime.now(timezone.utc).isoformat(),
        })
        return {"id": tid, "name": payload.name, "role": "owner", "is_personal": False, "member_count": 1, "project_count": 0}

    @router.get("/teams/{tid}")
    async def get_team(tid: str, current=Depends(get_current_user)):
        await s._require_team_role(tid, current["id"], ["owner", "admin", "member"])
        team = await db.teams.find_one({"id": tid}, {"_id": 0})
        if not team:
            raise HTTPException(status_code=404, detail="Team not found")
        members = await db.team_members.find({"team_id": tid}, {"_id": 0}).to_list(500)
        enriched = []
        for m in members:
            if m["status"] == "active":
                u = await db.users.find_one({"id": m["user_id"]}, {"_id": 0, "password": 0})
                if u:
                    enriched.append({**m, "email": u["email"], "name": u["name"]})
            else:
                enriched.append({**m, "email": m.get("invited_email", ""), "name": "(pending)"})
        projects = await db.projects.find({"team_id": tid}, {"_id": 0, "id": 1, "name": 1, "task_type": 1}).to_list(500)
        for p in projects:
            p["image_count"] = await db.images.count_documents({"project_id": p["id"]})
        return {**team, "members": enriched, "projects": projects}

    @router.patch("/teams/{tid}")
    async def rename_team(tid: str, payload: s.TeamUpdate, current=Depends(get_current_user)):
        await s._require_team_role(tid, current["id"], ["owner"])
        await db.teams.update_one({"id": tid}, {"$set": {"name": payload.name}})
        return {"ok": True}

    @router.delete("/teams/{tid}")
    async def delete_team(tid: str, current=Depends(get_current_user)):
        team = await db.teams.find_one({"id": tid})
        if not team:
            raise HTTPException(status_code=404, detail="Not found")
        if team.get("is_personal"):
            raise HTTPException(status_code=400, detail="Cannot delete personal workspace")
        await s._require_team_role(tid, current["id"], ["owner"])
        projects = await db.projects.find({"team_id": tid}).to_list(500)
        for p in projects:
            await db.images.delete_many({"project_id": p["id"]})
            await db.versions.delete_many({"project_id": p["id"]})
            await db.models.delete_many({"project_id": p["id"]})
            await s.purge_project_files(p["id"])
        await db.projects.delete_many({"team_id": tid})
        await db.team_members.delete_many({"team_id": tid})
        await db.teams.delete_one({"id": tid})
        return {"ok": True}

    async def _plan_move(p: dict, tid: str, uid: str, member_ids: set) -> dict:
        """What adding project `p` to team `tid` would do - the same numbers the real run applies."""
        res = {"id": p["id"], "name": p["name"], "status": "ok", "unassigned_images": 0,
               "removed_assignments": 0, "people_affected": 0}
        src = p.get("team_id")
        m = await s._get_membership(src, uid) if src else None
        if (src and (not m or m["role"] not in ("owner", "admin"))) or (not src and p.get("user_id") != uid):
            return {**res, "status": "forbidden", "reason": "You need to be owner or admin of the project's current team"}
        if src == tid:
            return {**res, "status": "skipped", "reason": "Already in this team"}
        if await db.images.count_documents({"project_id": p["id"], "review_status": "submitted"}):
            return {**res, "status": "blocked", "reason": "Images are waiting for review - finish reviewing first"}
        stale = {"project_id": p["id"], "review_status": {"$nin": list(s.LOCKED_STATUSES)},
                 "assigned_to": {"$nin": [None, ""] + list(member_ids)}}
        gone = {"project_id": p["id"], "user_id": {"$nin": list(member_ids)}}
        assignees = await db.images.distinct("assigned_to", stale)
        removed = await db.project_assignments.distinct("user_id", gone)
        return {**res, "unassigned_images": await db.images.count_documents(stale),
                "removed_assignments": len(removed), "people_affected": len(set(assignees) | set(removed))}

    @router.post("/teams/{tid}/projects/add")
    async def add_projects_to_team(tid: str, payload: s.TeamAddProjects, current=Depends(get_current_user)):
        """Moves existing projects into this team (a project has exactly one team)."""
        await s._require_team_role(tid, current["id"], ["owner", "admin"])
        members = await db.team_members.find({"team_id": tid, "status": "active"}).to_list(500)
        member_ids = {m["user_id"] for m in members}
        results = []
        for pid in dict.fromkeys(payload.project_ids):
            p = await db.projects.find_one({"id": pid})
            if not p:
                results.append({"id": pid, "name": "", "status": "forbidden", "reason": "Project not found"})
                continue
            plan = await _plan_move(p, tid, current["id"], member_ids)
            if plan["status"] == "ok" and not payload.dry_run:
                await db.images.update_many(
                    {"project_id": pid, "review_status": {"$nin": list(s.LOCKED_STATUSES)},
                     "assigned_to": {"$nin": [None, ""] + list(member_ids)}},
                    {"$set": {"assigned_to": None, "review_status": "unassigned"}},
                )
                await db.project_assignments.delete_many({"project_id": pid, "user_id": {"$nin": list(member_ids)}})
                await db.projects.update_one({"id": pid}, {"$set": {"team_id": tid}})
                await s._log_activity(pid, current["id"], "project_moved", {
                    "from_team_id": p.get("team_id"), "to_team_id": tid,
                    "unassigned_images": plan["unassigned_images"], "removed_assignments": plan["removed_assignments"],
                })
                plan["status"] = "moved"
            results.append(plan)
        totals = {k: sum(r.get(k, 0) for r in results) for k in ("unassigned_images", "removed_assignments")}
        return {"results": results, "totals": totals, "dry_run": payload.dry_run}

    @router.post("/teams/{tid}/invite")
    async def invite_member(tid: str, payload: s.InviteCreate, current=Depends(get_current_user)):
        await s._require_team_role(tid, current["id"], ["owner", "admin"])
        if payload.role not in ["admin", "member", "annotator", "reviewer"]:
            raise HTTPException(status_code=400, detail="Invalid role")
        email = payload.email.lower()
        allowed = getattr(s, "ALLOWED_EMAIL_DOMAIN", "") or ""
        if allowed and not email.endswith("@" + allowed):
            raise HTTPException(status_code=400, detail=f"Only @{allowed} email addresses can be invited")
        team = await db.teams.find_one({"id": tid})
        team_name = team.get("name", "your team") if team else "your team"
        user = await db.users.find_one({"email": email})
        if user:
            existing = await db.team_members.find_one({"team_id": tid, "user_id": user["id"]})
            if existing:
                raise HTTPException(status_code=400, detail="User already a member")
            await db.team_members.insert_one({
                "id": str(uuid.uuid4()), "team_id": tid, "user_id": user["id"],
                "role": payload.role, "status": "active",
                "joined_at": datetime.now(timezone.utc).isoformat(),
            })
            email_result = await s.send_invite_email(email, team_name, current["name"], payload.role)
            return {"status": "active", "email": email, "email_result": email_result}
        existing_pending = await db.team_members.find_one({"team_id": tid, "invited_email": email, "status": "pending"})
        if existing_pending:
            raise HTTPException(status_code=400, detail="Invite already pending")
        await db.team_members.insert_one({
            "id": str(uuid.uuid4()), "team_id": tid, "invited_email": email,
            "role": payload.role, "status": "pending",
            "invited_at": datetime.now(timezone.utc).isoformat(),
        })
        email_result = await s.send_invite_email(email, team_name, current["name"], payload.role)
        return {"status": "pending", "email": email, "email_result": email_result}

    @router.patch("/teams/{tid}/members/{mid}")
    async def update_member_role(tid: str, mid: str, payload: s.RoleUpdate, current=Depends(get_current_user)):
        await s._require_team_role(tid, current["id"], ["owner"])
        if payload.role not in ["admin", "member", "annotator", "reviewer"]:
            raise HTTPException(status_code=400, detail="Invalid role")
        m = await db.team_members.find_one({"id": mid, "team_id": tid})
        if not m:
            raise HTTPException(status_code=404, detail="Member not found")
        if m["role"] == "owner":
            raise HTTPException(status_code=400, detail="Cannot change owner role")
        await db.team_members.update_one({"id": mid}, {"$set": {"role": payload.role}})
        return {"ok": True}

    @router.delete("/teams/{tid}/members/{mid}")
    async def remove_member(tid: str, mid: str, current=Depends(get_current_user)):
        m = await db.team_members.find_one({"id": mid, "team_id": tid})
        if not m:
            raise HTTPException(status_code=404, detail="Member not found")
        if m["role"] == "owner":
            raise HTTPException(status_code=400, detail="Cannot remove owner")
        caller = await s._get_membership(tid, current["id"])
        if not caller:
            raise HTTPException(status_code=403, detail="Not a member")
        if caller["role"] not in ["owner", "admin"] and m.get("user_id") != current["id"]:
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        await db.team_members.delete_one({"id": mid})
        return {"ok": True}
