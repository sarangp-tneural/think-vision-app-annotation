"""Auth endpoints (register, login, me)."""
import uuid
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException

router = APIRouter()


def register(s):
    db = s.db
    get_current_user = s.get_current_user

    def _enforce_domain(email: str):
        allowed = getattr(s, "ALLOWED_EMAIL_DOMAIN", "") or ""
        if allowed and not email.lower().endswith("@" + allowed):
            raise HTTPException(status_code=403, detail=f"Only @{allowed} email addresses are allowed")

    @router.post("/auth/register")
    async def auth_register(payload: s.UserRegister):
        email = payload.email.lower()
        _enforce_domain(email)
        existing = await db.users.find_one({"email": email})
        if existing:
            raise HTTPException(status_code=400, detail="Email already registered")
        user_id = str(uuid.uuid4())
        doc = {
            "id": user_id, "email": email, "name": payload.name,
            "password": s.hash_password(payload.password),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        await db.users.insert_one(doc)
        await s._ensure_personal_team(user_id, payload.name)
        await s._activate_pending_invites(user_id, email)
        return {"token": s.create_token(user_id), "user": {"id": user_id, "email": doc["email"], "name": doc["name"]}}

    @router.post("/auth/login")
    async def auth_login(payload: s.UserLogin):
        email = payload.email.lower()
        _enforce_domain(email)
        user = await db.users.find_one({"email": email})
        if not user or not s.verify_password(payload.password, user["password"]):
            raise HTTPException(status_code=400, detail="Invalid credentials")
        await s._ensure_personal_team(user["id"], user["name"])
        await s._activate_pending_invites(user["id"], user["email"])
        return {"token": s.create_token(user["id"]), "user": {"id": user["id"], "email": user["email"], "name": user["name"]}}

    @router.get("/auth/me")
    async def auth_me(current=Depends(get_current_user)):
        return {"id": current["id"], "email": current["email"], "name": current["name"]}
