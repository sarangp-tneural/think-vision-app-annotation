"""Profile endpoints: account info + activity stats, rename, change password, avatar."""
import asyncio
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import Response
from pydantic import BaseModel

router = APIRouter()

AVATAR_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
AVATAR_MAX_BYTES = 2 * 1024 * 1024
MIN_PASSWORD_LEN = 8


class ProfileUpdate(BaseModel):
    name: str


class PasswordChange(BaseModel):
    current_password: str
    new_password: str


def register(s):
    db = s.db
    get_current_user = s.get_current_user

    def _public(u: dict) -> dict:
        return {"id": u["id"], "email": u["email"], "name": u["name"], "role": u.get("role", "user"),
                "created_at": u.get("created_at"), "avatar_path": u.get("avatar_path")}

    async def _stats(user_id: str) -> dict:
        team_ids = await s._list_user_team_ids(user_id)
        return {
            "teams": len(team_ids),
            "projects": await db.projects.count_documents({"team_id": {"$in": team_ids}}),
            "images_annotated": await db.images.count_documents({"annotated_by": user_id}),
            "images_reviewed": await db.images.count_documents({"reviewed_by": user_id}),
            "images_assigned": await db.images.count_documents({"assigned_to": user_id}),
        }

    @router.get("/profile")
    async def get_profile(current=Depends(get_current_user)):
        return {**_public(current), "stats": await _stats(current["id"])}

    @router.patch("/profile")
    async def update_profile(payload: ProfileUpdate, current=Depends(get_current_user)):
        name = " ".join(payload.name.split())
        if not 1 <= len(name) <= 80:
            raise HTTPException(status_code=400, detail="Name must be 1-80 characters")
        await db.users.update_one({"id": current["id"]}, {"$set": {"name": name}})
        return _public({**current, "name": name})

    @router.post("/profile/change-password")
    async def change_password(payload: PasswordChange, current=Depends(get_current_user)):
        # 400, not 401: the frontend logs out on any 401.
        if not s.verify_password(payload.current_password, current["password"]):
            raise HTTPException(status_code=400, detail="Current password is incorrect")
        if len(payload.new_password) < MIN_PASSWORD_LEN:
            raise HTTPException(status_code=400, detail=f"New password must be at least {MIN_PASSWORD_LEN} characters")
        if payload.new_password == payload.current_password:
            raise HTTPException(status_code=400, detail="New password must be different from the current one")
        await db.users.update_one({"id": current["id"]}, {"$set": {
            "password": s.hash_password(payload.new_password),
            "password_changed_at": int(datetime.now(timezone.utc).timestamp()),
        }})
        # Fresh token keeps this session alive; tokens issued earlier are now rejected.
        return {"token": s.create_token(current["id"]), "user": _public(current)}

    def _remove_avatar_file(user: dict):
        if user.get("avatar_path"):
            s.delete_object(user["avatar_path"])

    @router.post("/profile/avatar")
    async def upload_avatar(file: UploadFile = File(...), current=Depends(get_current_user)):
        ext = AVATAR_TYPES.get((file.content_type or "").lower())
        if not ext:
            raise HTTPException(status_code=400, detail="Avatar must be a PNG, JPEG or WebP image")
        data = await file.read(AVATAR_MAX_BYTES + 1)
        if len(data) > AVATAR_MAX_BYTES:
            raise HTTPException(status_code=400, detail="Avatar must be 2 MB or smaller")
        if not data:
            raise HTTPException(status_code=400, detail="Empty file")
        path = f"avatars/{current['id']}.{ext}"
        if current.get("avatar_path") and current["avatar_path"] != path:
            _remove_avatar_file(current)
        s.put_object(path, data, file.content_type)
        await db.users.update_one({"id": current["id"]}, {"$set": {"avatar_path": path}})
        return _public({**current, "avatar_path": path})

    @router.delete("/profile/avatar")
    async def delete_avatar(current=Depends(get_current_user)):
        _remove_avatar_file(current)
        await db.users.update_one({"id": current["id"]}, {"$unset": {"avatar_path": ""}})
        return _public({**current, "avatar_path": None})

    @router.get("/users/{user_id}/avatar")
    async def get_avatar(user_id: str, current=Depends(get_current_user)):
        """Any signed-in user can see another's avatar (shown next to names). `?auth=` works for <img> tags."""
        u = await db.users.find_one({"id": user_id}, {"avatar_path": 1})
        if not u or not u.get("avatar_path"):
            raise HTTPException(status_code=404, detail="No avatar")
        try:
            data, ct = await asyncio.to_thread(s.get_object, u["avatar_path"])
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="No avatar")
        # Replaceable, so no long-lived caching (the client adds ?v= to bust it).
        return Response(content=data, media_type=ct, headers={"Cache-Control": "private, max-age=300"})
