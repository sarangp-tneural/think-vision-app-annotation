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
        return {**team, "members": enriched}

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
        await db.projects.delete_many({"team_id": tid})
        await db.team_members.delete_many({"team_id": tid})
        await db.teams.delete_one({"id": tid})
        return {"ok": True}

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
