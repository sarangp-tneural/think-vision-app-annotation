import os
import io
import uuid
import base64
import json
import logging
import zipfile
import random
import math
import asyncio
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Optional, Any

import requests
import jwt as pyjwt
import bcrypt
import cv2
import resend
from fastapi import FastAPI, APIRouter, HTTPException, Depends, UploadFile, File, Form, Header, Query, BackgroundTasks
from fastapi.responses import Response, StreamingResponse
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, Field, EmailStr, ConfigDict
from dotenv import load_dotenv
from emergentintegrations.llm.chat import LlmChat, UserMessage, ImageContent

from pipeline_logic import SPLITS

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]
JWT_SECRET = os.environ["JWT_SECRET"]
EMERGENT_LLM_KEY = os.environ["EMERGENT_LLM_KEY"]
APP_NAME = os.environ.get("APP_NAME", "visionforge")  # kept for storage-path back-compat
BRAND_NAME = os.environ.get("BRAND_NAME", "ThinkVision")
ALLOWED_EMAIL_DOMAIN = os.environ.get("ALLOWED_EMAIL_DOMAIN", "").strip().lower()
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
SENDER_EMAIL = os.environ.get("SENDER_EMAIL", "onboarding@resend.dev")
APP_URL = os.environ.get("APP_URL", "")

if RESEND_API_KEY:
    resend.api_key = RESEND_API_KEY

STORAGE_URL = "https://integrations.emergentagent.com/objstore/api/v1/storage"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


# ----------------- Email ----------------- #
async def send_invite_email(to_email: str, team_name: str, inviter_name: str, role: str):
    """Send invitation email via Resend. Fails silently (invite record still created)."""
    if not RESEND_API_KEY:
        logger.warning("RESEND_API_KEY not set; skipping email send")
        return {"skipped": True, "reason": "no_api_key"}
    signup_url = f"{APP_URL}/auth" if APP_URL else "https://app"
    subject = f"{inviter_name} invited you to {team_name} on {BRAND_NAME}"
    html = f"""
<!DOCTYPE html>
<html>
<body style="font-family: -apple-system, Segoe UI, Roboto, Arial, sans-serif; background:#f5f5f7; margin:0; padding:24px;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px; margin:0 auto; background:#0a0a0a; color:#f5f5f7; border-radius:12px; overflow:hidden;">
    <tr>
      <td style="padding:32px; border-bottom:1px solid #27272A;">
        <div style="font-size:11px; letter-spacing:0.3em; color:#06B6D4; text-transform:uppercase;">{BRAND_NAME}</div>
        <h1 style="font-size:24px; margin:12px 0 0; color:#f5f5f7;">You're invited to join <span style="color:#06B6D4;">{team_name}</span></h1>
      </td>
    </tr>
    <tr>
      <td style="padding:28px 32px;">
        <p style="font-size:15px; line-height:1.6; color:#c4c4c8; margin:0 0 16px;">
          <strong style="color:#f5f5f7;">{inviter_name}</strong> has invited you to collaborate on <strong style="color:#f5f5f7;">{team_name}</strong> as a <strong style="color:#06B6D4;">{role}</strong>.
        </p>
        <p style="font-size:15px; line-height:1.6; color:#c4c4c8; margin:0 0 24px;">
          Sign up or log in with this email address to automatically join the team.
        </p>
        <table role="presentation" cellpadding="0" cellspacing="0">
          <tr>
            <td style="border-radius:8px; background:#06B6D4;">
              <a href="{signup_url}" style="display:inline-block; padding:12px 24px; font-size:14px; font-weight:600; color:#050505; text-decoration:none; letter-spacing:0.05em;">Accept Invitation</a>
            </td>
          </tr>
        </table>
      </td>
    </tr>
    <tr>
      <td style="padding:20px 32px; background:#050505; font-size:11px; color:#71717a; text-align:center;">
        {BRAND_NAME} · Computer vision datasets, annotation & training
      </td>
    </tr>
  </table>
</body>
</html>
""".strip()
    params = {"from": SENDER_EMAIL, "to": [to_email], "subject": subject, "html": html}
    try:
        result = await asyncio.to_thread(resend.Emails.send, params)
        logger.info(f"Invite email sent to {to_email}: {result.get('id')}")
        return {"sent": True, "email_id": result.get("id")}
    except Exception as e:
        logger.error(f"Failed to send invite email to {to_email}: {e}")
        return {"sent": False, "error": str(e)[:200]}


def send_tester_notification_email(to_emails: list, project_name: str, project_id: str,
                                    run_id: str, app_url: str) -> dict:
    """Send a deploy-pipeline testing-ready notification via Resend. Cloned
    from send_invite_email (same RESEND_API_KEY guard, HTML style, params
    shape, best-effort "fails silently" error handling) - plain sync, not
    async, since this is called from a background thread (deployment_worker.py)
    rather than an async route handler: there's no event loop there to
    protect, and a sync worker can't await an async function anyway."""
    if not RESEND_API_KEY:
        logger.warning("RESEND_API_KEY not set; skipping email send")
        return {"skipped": True, "reason": "no_api_key"}
    if not to_emails:
        return {"skipped": True, "reason": "no_recipients"}
    review_url = f"{app_url}/projects/{project_id}/deploy?run={run_id}" if app_url else "https://app"
    subject = f"A candidate model is ready for review in {project_name}"
    html = f"""
<!DOCTYPE html>
<html>
<body style="font-family: -apple-system, Segoe UI, Roboto, Arial, sans-serif; background:#f5f5f7; margin:0; padding:24px;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px; margin:0 auto; background:#0a0a0a; color:#f5f5f7; border-radius:12px; overflow:hidden;">
    <tr>
      <td style="padding:32px; border-bottom:1px solid #27272A;">
        <div style="font-size:11px; letter-spacing:0.3em; color:#06B6D4; text-transform:uppercase;">{BRAND_NAME}</div>
        <h1 style="font-size:24px; margin:12px 0 0; color:#f5f5f7;">A candidate model is ready for review in <span style="color:#06B6D4;">{project_name}</span></h1>
      </td>
    </tr>
    <tr>
      <td style="padding:28px 32px;">
        <p style="font-size:15px; line-height:1.6; color:#c4c4c8; margin:0 0 24px;">
          The deploy pipeline finished testing a retrained candidate model against the current baseline. Review the side-by-side comparison and approve or reject it.
        </p>
        <table role="presentation" cellpadding="0" cellspacing="0">
          <tr>
            <td style="border-radius:8px; background:#06B6D4;">
              <a href="{review_url}" style="display:inline-block; padding:12px 24px; font-size:14px; font-weight:600; color:#050505; text-decoration:none; letter-spacing:0.05em;">Review Candidate</a>
            </td>
          </tr>
        </table>
      </td>
    </tr>
    <tr>
      <td style="padding:20px 32px; background:#050505; font-size:11px; color:#71717a; text-align:center;">
        {BRAND_NAME} · Computer vision datasets, annotation &amp; training
      </td>
    </tr>
  </table>
</body>
</html>
""".strip()
    params = {"from": SENDER_EMAIL, "to": to_emails, "subject": subject, "html": html}
    try:
        result = resend.Emails.send(params)
        logger.info(f"Tester notification email sent to {to_emails}: {result.get('id')}")
        return {"sent": True, "email_id": result.get("id")}
    except Exception as e:
        logger.error(f"Failed to send tester notification email to {to_emails}: {e}")
        return {"sent": False, "error": str(e)[:200]}


client = AsyncIOMotorClient(MONGO_URL)
db = client[DB_NAME]

app = FastAPI(title="VisionForge API")
api_router = APIRouter(prefix="/api")

# ----------------- Storage ----------------- #
storage_key: Optional[str] = None

def init_storage() -> str:
    global storage_key
    if storage_key:
        return storage_key
    resp = requests.post(f"{STORAGE_URL}/init", json={"emergent_key": EMERGENT_LLM_KEY}, timeout=30)
    resp.raise_for_status()
    storage_key = resp.json()["storage_key"]
    return storage_key

def put_object(path: str, data: bytes, content_type: str) -> dict:
    key = init_storage()
    resp = requests.put(
        f"{STORAGE_URL}/objects/{path}",
        headers={"X-Storage-Key": key, "Content-Type": content_type},
        data=data,
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()

def get_object(path: str):
    key = init_storage()
    resp = requests.get(
        f"{STORAGE_URL}/objects/{path}",
        headers={"X-Storage-Key": key},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.content, resp.headers.get("Content-Type", "application/octet-stream")


# ----------------- Models ----------------- #
class UserRegister(BaseModel):
    email: EmailStr
    password: str
    name: str

class UserLogin(BaseModel):
    email: EmailStr
    password: str

class UserOut(BaseModel):
    id: str
    email: str
    name: str

class ProjectCreate(BaseModel):
    name: str
    description: Optional[str] = ""
    task_type: str = "object_detection"  # object_detection, classification, segmentation
    team_id: Optional[str] = None  # defaults to personal team

class ProjectOut(BaseModel):
    id: str
    name: str
    description: str
    task_type: str
    classes: List[str] = []
    image_count: int = 0
    annotated_count: int = 0
    created_at: str

class AnnotationBox(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: str = "bbox"  # bbox | polygon | polyline | point | ellipse
    label: str
    # bbox / ellipse: x,y is top-left (bbox) or center (ellipse)
    x: float = 0
    y: float = 0
    w: float = 0
    h: float = 0
    # polygon / polyline (normalized 0-1 pairs)
    points: List[List[float]] = Field(default_factory=list)
    # ellipse radii (normalized)
    rx: float = 0
    ry: float = 0

class SaveAnnotationsIn(BaseModel):
    boxes: List[AnnotationBox]

class AssignImageIn(BaseModel):
    user_id: Optional[str] = None  # None = unassign

class ReviewImageIn(BaseModel):
    decision: str  # 'approve' | 'reject'
    notes: Optional[str] = ""

class BatchAutoLabelIn(BaseModel):
    only_unlabeled: bool = True

class ProjectAssignIn(BaseModel):
    user_ids: List[str]

class BulkAssignIn(BaseModel):
    image_ids: List[str]
    user_id: Optional[str] = None
    priority: Optional[str] = None  # low | medium | high
    due_date: Optional[str] = None
    notes: Optional[str] = ""

class CommentIn(BaseModel):
    text: str

class TaskMetaIn(BaseModel):
    priority: Optional[str] = None
    due_date: Optional[str] = None
    notes: Optional[str] = None


class ProjectSettingsIn(BaseModel):
    confidence_threshold: Optional[float] = None  # 0.05 - 0.95
    fallback_to_gemini: Optional[bool] = None
    min_boxes_threshold: Optional[int] = None  # if model returns fewer than N boxes, fall back
    show_confidence: Optional[bool] = None
    gemini_restrict_to_classes: Optional[bool] = None
    gemini_allowed_classes: Optional[List[str]] = None  # None = not customized, use all project classes

class VersionCreate(BaseModel):
    name: str
    notes: Optional[str] = ""

class TrainRequest(BaseModel):
    version_id: str
    epochs: int = 30
    model_arch: str = "yolov8n"

class TeamCreate(BaseModel):
    name: str

class TeamUpdate(BaseModel):
    name: str

class InviteCreate(BaseModel):
    email: EmailStr
    role: str = "member"  # admin | member

class RoleUpdate(BaseModel):
    role: str  # admin | member


# ----------------- Auth ----------------- #
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())

def create_token(user_id: str) -> str:
    payload = {"sub": user_id, "exp": datetime.now(timezone.utc) + timedelta(days=7)}
    return pyjwt.encode(payload, JWT_SECRET, algorithm="HS256")

async def get_current_user(authorization: Optional[str] = Header(None), auth: Optional[str] = Query(None)):
    token = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization.split(" ", 1)[1]
    elif auth:
        token = auth
    if not token:
        raise HTTPException(status_code=401, detail="Missing token")
    try:
        payload = pyjwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        user_id = payload["sub"]
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")
    user = await db.users.find_one({"id": user_id})
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    # Auto-activate any pending invites so re-invites work without re-login
    try:
        await _activate_pending_invites(user["id"], user["email"])
    except Exception:
        pass
    return user


async def _ensure_personal_team(user_id: str, name: str) -> str:
    """Ensure user has a personal team + migrate any legacy user_id-only projects."""
    existing_owner = await db.team_members.find_one({"user_id": user_id, "role": "owner", "is_personal": True})
    if existing_owner:
        tid = existing_owner["team_id"]
    else:
        tid = str(uuid.uuid4())
        await db.teams.insert_one({
            "id": tid,
            "name": f"{name}'s Workspace",
            "owner_id": user_id,
            "is_personal": True,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        await db.team_members.insert_one({
            "id": str(uuid.uuid4()),
            "team_id": tid,
            "user_id": user_id,
            "role": "owner",
            "status": "active",
            "is_personal": True,
            "joined_at": datetime.now(timezone.utc).isoformat(),
        })
    # migrate legacy projects: user_id set, team_id missing
    await db.projects.update_many(
        {"user_id": user_id, "team_id": {"$exists": False}},
        {"$set": {"team_id": tid}},
    )
    return tid


async def _activate_pending_invites(user_id: str, email: str):
    """Convert pending invites for this email into active memberships."""
    pending = await db.team_members.find({"invited_email": email.lower(), "status": "pending"}).to_list(500)
    for p in pending:
        await db.team_members.update_one(
            {"id": p["id"]},
            {"$set": {
                "user_id": user_id,
                "status": "active",
                "joined_at": datetime.now(timezone.utc).isoformat(),
            }, "$unset": {"invited_email": ""}},
        )


# Auth endpoints (register, login, me) moved to routers/auth.py


# ----------------- Teams ----------------- #
async def _get_membership(team_id: str, user_id: str):
    return await db.team_members.find_one({"team_id": team_id, "user_id": user_id, "status": "active"})

async def _require_team_role(team_id: str, user_id: str, allowed: List[str]):
    m = await _get_membership(team_id, user_id)
    if not m or m["role"] not in allowed:
        raise HTTPException(status_code=403, detail="Insufficient team permissions")
    return m

async def _list_user_team_ids(user_id: str) -> List[str]:
    memberships = await db.team_members.find({"user_id": user_id, "status": "active"}).to_list(500)
    return [m["team_id"] for m in memberships]


# Team CRUD + invite + members endpoints moved to routers/teams.py


# ----------------- Projects ----------------- #
# Project CRUD + classes + settings endpoints moved to routers/projects.py
# _project_out and _project_access_check helpers remain here (shared with other endpoints)


async def _project_out(pid: str):
    p = await db.projects.find_one({"id": pid}, {"_id": 0})
    if not p:
        raise HTTPException(status_code=404, detail="Project not found")
    async def _team_name():
        if not p.get("team_id"):
            return ""
        t = await db.teams.find_one({"id": p["team_id"]}, {"_id": 0, "name": 1})
        return t["name"] if t else ""

    # Independent lookups run concurrently instead of sequentially.
    image_count, annotated_count, team_name, active_model = await asyncio.gather(
        db.images.count_documents({"project_id": pid}),
        db.images.count_documents({"project_id": pid, "annotated": True}),
        _team_name(),
        # Same lookup _auto_label_router uses to pick a provider - kept identical
        # so this field can never disagree with what auto-label actually does.
        db.models.find_one(
            {"project_id": pid, "is_active": True, "status": "trained"},
            {"_id": 0, "id": 1, "model_arch": 1, "type": 1, "final_mAP": 1, "activated_at": 1},
        ),
    )
    settings = p.get("settings", {})
    return {
        "id": p["id"],
        "name": p["name"],
        "description": p.get("description", ""),
        "task_type": p.get("task_type", "object_detection"),
        "classes": p.get("classes", []),
        "image_count": image_count,
        "annotated_count": annotated_count,
        "team_id": p.get("team_id"),
        "team_name": team_name,
        "settings": {
            "confidence_threshold": settings.get("confidence_threshold", 0.4),
            "fallback_to_gemini": settings.get("fallback_to_gemini", True),
            "min_boxes_threshold": settings.get("min_boxes_threshold", 1),
            "show_confidence": settings.get("show_confidence", True),
            "gemini_restrict_to_classes": settings.get("gemini_restrict_to_classes", False),
            "gemini_allowed_classes": settings.get("gemini_allowed_classes"),
        },
        "active_model": active_model,
        "created_at": p["created_at"],
    }

async def _project_access_check(pid: str, user_id: str, roles: Optional[List[str]] = None):
    """Returns the project if user has access via team membership (or legacy user_id)."""
    p = await db.projects.find_one({"id": pid})
    if not p:
        raise HTTPException(status_code=404, detail="Not found")
    tid = p.get("team_id")
    if tid:
        m = await _get_membership(tid, user_id)
        if not m:
            raise HTTPException(status_code=403, detail="Not a team member")
        if roles and m["role"] not in roles:
            raise HTTPException(status_code=403, detail="Insufficient team permissions")
        # For annotator/reviewer roles: must have project assignment (once any assignments exist)
        if m["role"] in ["annotator", "reviewer"]:
            has_any = await db.project_assignments.count_documents({"project_id": pid})
            if has_any > 0:
                assigned = await db.project_assignments.find_one({"project_id": pid, "user_id": user_id})
                if not assigned:
                    raise HTTPException(status_code=403, detail="Not assigned to this project")
        return p
    # legacy: user_id direct owner
    if p.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="Not project owner")
    return p


async def _log_activity(project_id: str, user_id: str, action: str, meta: dict = None):
    """Record an activity entry. Non-fatal on error."""
    try:
        project = await db.projects.find_one({"id": project_id}, {"team_id": 1})
        user = await db.users.find_one({"id": user_id}, {"name": 1, "email": 1})
        doc = {
            "id": str(uuid.uuid4()),
            "project_id": project_id,
            "team_id": project.get("team_id") if project else None,
            "user_id": user_id,
            "user_name": user.get("name") if user else "Unknown",
            "action": action,
            "meta": meta or {},
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        await db.activity_log.insert_one(doc)
    except Exception as e:
        logger.error(f"activity log failed: {e}")


async def _notify(user_id: str, kind: str, title: str, project_id: Optional[str] = None, image_id: Optional[str] = None, meta: dict = None):
    """Create an in-app notification."""
    try:
        await db.notifications.insert_one({
            "id": str(uuid.uuid4()),
            "user_id": user_id,
            "kind": kind,
            "title": title,
            "project_id": project_id,
            "image_id": image_id,
            "meta": meta or {},
            "read": False,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as e:
        logger.error(f"notify failed: {e}")


# Project CRUD + classes + settings endpoints moved to routers/projects.py


# ----------------- Images ----------------- #
async def _verify_project(pid: str, user_id: str):
    return await _project_access_check(pid, user_id)


RETRAIN_NUDGE_THRESHOLD = 30


async def _maybe_send_retrain_nudge(project: dict, actor_id: str):
    """Send a retrain nudge notification when >=30 new approved images exist since last training."""
    pid = project["id"]
    # Last successful training completion timestamp
    last_trained = await db.models.find_one(
        {"project_id": pid, "type": "real", "status": "trained"},
        sort=[("created_at", -1)],
    )
    since_iso = last_trained.get("created_at") if last_trained else None
    query = {"project_id": pid, "review_status": "approved"}
    if since_iso:
        query["reviewed_at"] = {"$gt": since_iso}
    new_approved = await db.images.count_documents(query)
    if new_approved < RETRAIN_NUDGE_THRESHOLD:
        return
    # Avoid spamming: only send once per nudge-cycle. Track last-nudge count on project.
    last_nudged = project.get("last_retrain_nudge_count", 0)
    if new_approved < last_nudged + RETRAIN_NUDGE_THRESHOLD:
        return
    await db.projects.update_one({"id": pid}, {"$set": {"last_retrain_nudge_count": new_approved}})
    # Notify owner + admins
    tid = project.get("team_id")
    recipients = set()
    if tid:
        mems = await db.team_members.find({"team_id": tid, "role": {"$in": ["owner", "admin"]}, "status": "active"}).to_list(50)
        for m in mems:
            if m.get("user_id"):
                recipients.add(m["user_id"])
    if project.get("owner_id"):
        recipients.add(project["owner_id"])
    title = "Retrain your model" if last_trained else "Ready to train your first model"
    msg_kind = "retrain_nudge" if last_trained else "training_ready"
    for uid in recipients:
        await _notify(uid, msg_kind, f"{title}: {new_approved} approved images in '{project['name']}'", project_id=pid, meta={"new_approved": new_approved, "threshold": RETRAIN_NUDGE_THRESHOLD})


# Image CRUD/annotations/assign/submit/review/file-download moved to routers/images.py


# ----------------- Video Upload & Frame Extraction ----------------- #
def _extract_frames_sync(video_bytes: bytes, video_id: str, project_id: str, user_id: str, interval_frames: int, interval_seconds: Optional[float] = None):
    """Blocking frame extraction using cv2. Uploads each frame to storage and creates image doc.
    Runs inside asyncio.to_thread. Uses PyMongo sync client for DB writes (safer inside thread).
    If interval_seconds is provided, it takes precedence over interval_frames — converted using video FPS."""
    from pymongo import MongoClient
    sync_client = MongoClient(MONGO_URL)
    sync_db = sync_client[DB_NAME]

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
        f.write(video_bytes)
        tmp_path = f.name

    try:
        cap = cv2.VideoCapture(tmp_path)
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        # If seconds mode requested, convert to frames using FPS
        if interval_seconds and interval_seconds > 0:
            interval = max(1, int(round(interval_seconds * fps)))
            sync_db.videos.update_one({"id": video_id}, {"$set": {"resolved_interval_frames": interval, "fps": round(fps, 2)}})
        else:
            interval = max(1, int(interval_frames))
        extracted = 0
        idx = 0
        sync_db.videos.update_one({"id": video_id}, {"$set": {"total_frames": total, "status": "processing"}})
        while True:
            ret, frame = cap.read()
            if not ret:
                break
            if idx % interval == 0:
                ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
                if ok:
                    img_id = str(uuid.uuid4())
                    path = f"{APP_NAME}/projects/{project_id}/{img_id}.jpg"
                    try:
                        result = put_object(path, buf.tobytes(), "image/jpeg")
                        sync_db.images.insert_one({
                            "id": img_id,
                            "project_id": project_id,
                            "user_id": user_id,
                            "filename": f"frame_{idx:06d}.jpg",
                            "storage_path": result["path"],
                            "content_type": "image/jpeg",
                            "size": result.get("size", len(buf)),
                            "annotations": [],
                            "annotated": False,
                            "from_video": video_id,
                            "created_at": datetime.now(timezone.utc).isoformat(),
                        })
                        extracted += 1
                    except Exception as e:
                        logger.error(f"Frame upload failed: {e}")
                # Update progress every 5 frames
                if extracted % 5 == 0:
                    sync_db.videos.update_one({"id": video_id}, {"$set": {"extracted_count": extracted, "processed_frames": idx}})
            idx += 1
        cap.release()
        sync_db.videos.update_one(
            {"id": video_id},
            {"$set": {"status": "completed", "extracted_count": extracted, "processed_frames": idx, "completed_at": datetime.now(timezone.utc).isoformat()}},
        )
    except Exception as e:
        logger.error(f"Video extraction failed: {e}")
        sync_db.videos.update_one({"id": video_id}, {"$set": {"status": "failed", "error": str(e)}})
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        sync_client.close()


@api_router.post("/projects/{pid}/videos")
async def upload_video(
    pid: str,
    file: UploadFile = File(...),
    interval_frames: int = Form(30),
    interval_seconds: float = Form(0),
    background: BackgroundTasks = None,
    current=Depends(get_current_user),
):
    await _verify_project(pid, current["id"])
    ext = (file.filename.rsplit(".", 1)[-1] if "." in file.filename else "mp4").lower()
    if ext not in ["mp4", "mov", "webm", "avi", "mkv"]:
        raise HTTPException(status_code=400, detail="Unsupported video type")
    content = await file.read()
    if len(content) > 200 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Video too large (max 200MB)")
    video_id = str(uuid.uuid4())
    storage_path = f"{APP_NAME}/projects/{pid}/videos/{video_id}.{ext}"
    try:
        result = put_object(storage_path, content, file.content_type or f"video/{ext}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Storage upload failed: {e}")
    doc = {
        "id": video_id,
        "project_id": pid,
        "user_id": current["id"],
        "filename": file.filename,
        "storage_path": result["path"],
        "size": result.get("size", len(content)),
        "interval_frames": interval_frames,
        "interval_seconds": interval_seconds if interval_seconds > 0 else None,
        "status": "queued",
        "extracted_count": 0,
        "processed_frames": 0,
        "total_frames": 0,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.videos.insert_one(doc)

    # Schedule background frame extraction
    async def _job():
        await asyncio.to_thread(_extract_frames_sync, content, video_id, pid, current["id"], interval_frames, interval_seconds if interval_seconds > 0 else None)

    if background is not None:
        background.add_task(_job)
    else:
        asyncio.create_task(_job())

    doc.pop("_id", None)
    return doc


@api_router.get("/projects/{pid}/videos")
async def list_videos(pid: str, current=Depends(get_current_user)):
    await _verify_project(pid, current["id"])
    videos = await db.videos.find({"project_id": pid}, {"_id": 0}).to_list(200)
    videos.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return videos


@api_router.get("/videos/{vid}")
async def get_video_status(vid: str, current=Depends(get_current_user)):
    v = await db.videos.find_one({"id": vid}, {"_id": 0})
    if not v:
        raise HTTPException(status_code=404, detail="Not found")
    await _project_access_check(v["project_id"], current["id"])
    return v


@api_router.delete("/videos/{vid}")
async def delete_video(vid: str, current=Depends(get_current_user)):
    v = await db.videos.find_one({"id": vid})
    if not v:
        raise HTTPException(status_code=404, detail="Not found")
    await _project_access_check(v["project_id"], current["id"])
    await db.videos.delete_one({"id": vid})
    return {"ok": True}


# ----------------- AI Auto-Labeling ----------------- #
@api_router.post("/images/{img_id}/auto-label")
async def auto_label(img_id: str, current=Depends(get_current_user)):
    img = await db.images.find_one({"id": img_id})
    if not img:
        raise HTTPException(status_code=404, detail="Not found")
    await _project_access_check(img["project_id"], current["id"])
    try:
        data, ct = await asyncio.to_thread(get_object, img["storage_path"])
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Image fetch failed: {e}")
    try:
        boxes, source = await _auto_label_router(img["project_id"], data)
    except Exception as e:
        logger.error(f"Auto-label failed: {e}")
        raise HTTPException(status_code=500, detail=f"AI request failed: {str(e)}")
    return {"boxes": boxes, "count": len(boxes), "source": source}


# ----------------- Batch Auto-Label ----------------- #
async def _batch_autolabel_job(job_id: str, project_id: str, image_ids: List[str]):
    """Iterate images and run auto-label via router (uses trained model if available)."""
    total = len(image_ids)
    processed = 0
    failed = 0
    labeled = 0
    skipped = 0
    source_counts = {"model": 0, "gemini": 0, "model+gemini": 0, "model_no_fallback": 0}
    await db.batch_jobs.update_one({"id": job_id}, {"$set": {"status": "processing", "total": total}})

    for img_id in image_ids:
        try:
            img = await db.images.find_one({"id": img_id})
            if not img:
                failed += 1
                processed += 1
                continue
            data, _ = await asyncio.to_thread(get_object, img["storage_path"])
            # Re-read the project per image so settings changed mid-job
            # (fallback, class restriction, threshold) apply like single-image labeling.
            project = await db.projects.find_one({"id": project_id})
            await db.batch_jobs.update_one({"id": job_id}, {"$set": {"current": {
                "image_id": img_id, "storage_path": img["storage_path"], "boxes": [], "source": None, "analyzing": True,
            }}})
            boxes, source = await _auto_label_router(project_id, data, project=project)
            source_counts[source] = source_counts.get(source, 0) + 1
            if source == "model_no_fallback" and not boxes:
                skipped += 1
            if boxes:
                existing = img.get("annotations", [])
                await db.images.update_one(
                    {"id": img_id},
                    {"$set": {"annotations": existing + boxes, "annotated": True}},
                )
                # Merge class labels
                await db.projects.update_one(
                    {"id": project_id},
                    {"$addToSet": {"classes": {"$each": sorted({b["label"] for b in boxes})}}},
                )
                labeled += 1
            processed += 1
            await db.batch_jobs.update_one({"id": job_id}, {"$set": {"processed": processed, "labeled": labeled, "failed": failed, "skipped": skipped, "source_counts": source_counts,
                "current": {"image_id": img_id, "storage_path": img["storage_path"], "boxes": boxes, "source": source, "analyzing": False}}})
        except Exception as e:
            logger.error(f"Batch label failed for {img_id}: {e}")
            failed += 1
            processed += 1
            await db.batch_jobs.update_one({"id": job_id}, {"$set": {"processed": processed, "failed": failed}})

    await db.batch_jobs.update_one({"id": job_id}, {"$set": {
        "status": "completed",
        "current": None,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }})


@api_router.post("/projects/{pid}/batch-auto-label")
async def batch_auto_label(
    pid: str,
    payload: BatchAutoLabelIn,
    background: BackgroundTasks,
    current=Depends(get_current_user),
):
    await _verify_project(pid, current["id"])
    query = {"project_id": pid}
    if payload.only_unlabeled:
        query["annotated"] = False
    images = await db.images.find(query, {"id": 1, "_id": 0}).to_list(5000)
    image_ids = [i["id"] for i in images]
    if not image_ids:
        raise HTTPException(status_code=400, detail="No matching images to label")

    proj = await db.projects.find_one({"id": pid}) or {}
    st = proj.get("settings", {}) or {}
    active = await db.models.find_one({"project_id": pid, "is_active": True, "status": "trained"})
    job_id = str(uuid.uuid4())
    doc = {
        "id": job_id,
        "project_id": pid,
        "user_id": current["id"],
        "type": "auto_label",
        "skipped": 0,
        "settings_snapshot": {
            "model": (active or {}).get("model_arch") or (active or {}).get("name"),
            "fallback_to_gemini": st.get("fallback_to_gemini", True),
            "restrict_to_classes": st.get("gemini_restrict_to_classes", False),
            "confidence_threshold": st.get("confidence_threshold"),
        },
        "status": "queued",
        "total": len(image_ids),
        "processed": 0,
        "labeled": 0,
        "failed": 0,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.batch_jobs.insert_one(doc)
    background.add_task(_batch_autolabel_job, job_id, pid, image_ids)
    doc.pop("_id", None)
    return doc


@api_router.get("/batch-jobs/{jid}")
async def get_batch_job(jid: str, current=Depends(get_current_user)):
    job = await db.batch_jobs.find_one({"id": jid}, {"_id": 0})
    if not job:
        raise HTTPException(status_code=404, detail="Not found")
    await _project_access_check(job["project_id"], current["id"])
    return job


@api_router.get("/projects/{pid}/batch-jobs")
async def list_batch_jobs(pid: str, current=Depends(get_current_user)):
    await _verify_project(pid, current["id"])
    jobs = await db.batch_jobs.find({"project_id": pid}, {"_id": 0}).to_list(200)
    jobs.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return jobs


# ----------------- Project Assignments ----------------- #
@api_router.post("/projects/{pid}/assign")
async def assign_project(pid: str, payload: ProjectAssignIn, current=Depends(get_current_user)):
    p = await _project_access_check(pid, current["id"], roles=["owner", "admin"])
    tid = p.get("team_id")
    added = []
    for uid in payload.user_ids:
        # Verify user is on the team
        if tid:
            m = await db.team_members.find_one({"team_id": tid, "user_id": uid, "status": "active"})
            if not m:
                continue
        existing = await db.project_assignments.find_one({"project_id": pid, "user_id": uid})
        if existing:
            continue
        await db.project_assignments.insert_one({
            "id": str(uuid.uuid4()),
            "project_id": pid,
            "user_id": uid,
            "assigned_by": current["id"],
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        added.append(uid)
        await _log_activity(pid, current["id"], "project_assigned", {"assigned_user_id": uid})
        await _notify(uid, "project_assigned", f"You were assigned to project '{p['name']}'", project_id=pid)
    return {"assigned": added, "count": len(added)}


@api_router.delete("/projects/{pid}/assign/{user_id}")
async def unassign_project(pid: str, user_id: str, current=Depends(get_current_user)):
    await _project_access_check(pid, current["id"], roles=["owner", "admin"])
    await db.project_assignments.delete_one({"project_id": pid, "user_id": user_id})
    await _log_activity(pid, current["id"], "project_unassigned", {"user_id": user_id})
    return {"ok": True}


@api_router.get("/projects/{pid}/assignments")
async def list_project_assignments(pid: str, current=Depends(get_current_user)):
    await _project_access_check(pid, current["id"])
    assignments = await db.project_assignments.find({"project_id": pid}, {"_id": 0}).to_list(500)
    enriched = []
    for a in assignments:
        u = await db.users.find_one({"id": a["user_id"]}, {"_id": 0, "password": 0})
        m = None
        p = await db.projects.find_one({"id": pid}, {"team_id": 1})
        if p.get("team_id"):
            m = await db.team_members.find_one({"team_id": p["team_id"], "user_id": a["user_id"]}, {"role": 1})
        enriched.append({**a, "user_name": u["name"] if u else "?", "user_email": u["email"] if u else "", "role": m["role"] if m else "member"})
    return enriched


# ----------------- Bulk Assign / Task Metadata ----------------- #
@api_router.post("/projects/{pid}/images/bulk-assign")
async def bulk_assign_images(pid: str, payload: BulkAssignIn, current=Depends(get_current_user)):
    p = await _project_access_check(pid, current["id"], roles=["owner", "admin"])
    tid = p.get("team_id")
    if payload.user_id and tid:
        m = await db.team_members.find_one({"team_id": tid, "user_id": payload.user_id, "status": "active"})
        if not m:
            raise HTTPException(status_code=400, detail="User not on team")
    update = {}
    if payload.user_id is not None:
        update["assigned_to"] = payload.user_id
        update["review_status"] = "assigned" if payload.user_id else "unassigned"
    if payload.priority:
        update["priority"] = payload.priority
    if payload.due_date is not None:
        update["due_date"] = payload.due_date
    if payload.notes is not None:
        update["task_notes"] = payload.notes
    if not update:
        raise HTTPException(status_code=400, detail="Nothing to update")
    result = await db.images.update_many({"id": {"$in": payload.image_ids}, "project_id": pid}, {"$set": update})
    await _log_activity(pid, current["id"], "bulk_assigned", {"count": result.modified_count, "user_id": payload.user_id})
    if payload.user_id:
        await _notify(payload.user_id, "image_assigned", f"{result.modified_count} image(s) assigned to you in '{p['name']}'", project_id=pid)
    return {"updated": result.modified_count}


@api_router.patch("/images/{img_id}/task")
async def update_image_task(img_id: str, payload: TaskMetaIn, current=Depends(get_current_user)):
    img = await db.images.find_one({"id": img_id})
    if not img:
        raise HTTPException(status_code=404, detail="Not found")
    await _project_access_check(img["project_id"], current["id"])
    update = {}
    if payload.priority is not None:
        update["priority"] = payload.priority
    if payload.due_date is not None:
        update["due_date"] = payload.due_date
    if payload.notes is not None:
        update["task_notes"] = payload.notes
    if update:
        await db.images.update_one({"id": img_id}, {"$set": update})
    return {"ok": True}


# ----------------- Comments / Notifications / Activity ----------------- #
# Endpoints moved to routers/collaboration.py


# ----------------- Dashboards ----------------- #
@api_router.get("/projects/{pid}/dashboard")
async def project_dashboard(pid: str, current=Depends(get_current_user)):
    await _project_access_check(pid, current["id"])
    total = await db.images.count_documents({"project_id": pid})
    counts = {}
    for status in ["unassigned", "assigned", "submitted", "approved", "rejected"]:
        counts[status] = await db.images.count_documents({"project_id": pid, "review_status": status})
    annotated = await db.images.count_documents({"project_id": pid, "annotated": True})
    # Per-member perf
    assignments = await db.project_assignments.find({"project_id": pid}).to_list(500)
    project = await db.projects.find_one({"id": pid}, {"team_id": 1})
    tid = project.get("team_id") if project else None
    team_members = []
    if tid:
        team_members = await db.team_members.find({"team_id": tid, "status": "active"}).to_list(200)
    members_perf = []
    assigned_uids = set(a["user_id"] for a in assignments)
    for m in team_members:
        uid = m["user_id"]
        u = await db.users.find_one({"id": uid}, {"_id": 0, "password": 0})
        if not u:
            continue
        assigned_c = await db.images.count_documents({"project_id": pid, "assigned_to": uid})
        submitted_c = await db.images.count_documents({"project_id": pid, "assigned_to": uid, "review_status": "submitted"})
        approved_c = await db.images.count_documents({"project_id": pid, "assigned_to": uid, "review_status": "approved"})
        rejected_c = await db.images.count_documents({"project_id": pid, "assigned_to": uid, "review_status": "rejected"})
        # last active from activity_log
        last_act = await db.activity_log.find_one({"project_id": pid, "user_id": uid}, sort=[("created_at", -1)])
        members_perf.append({
            "user_id": uid,
            "name": u["name"],
            "email": u["email"],
            "role": m["role"],
            "assigned_to_project": uid in assigned_uids,
            "assigned_images": assigned_c,
            "submitted": submitted_c,
            "approved": approved_c,
            "rejected": rejected_c,
            "last_active": last_act["created_at"] if last_act else None,
        })
    return {
        "total": total,
        "annotated": annotated,
        "counts": counts,
        "completion_pct": round(counts["approved"] / total * 100, 1) if total else 0,
        "annotation_pct": round(annotated / total * 100, 1) if total else 0,
        "review_pct": round((counts["submitted"] + counts["approved"] + counts["rejected"]) / total * 100, 1) if total else 0,
        "members_perf": members_perf,
    }


@api_router.get("/teams/{tid}/dashboard")
async def team_dashboard(tid: str, current=Depends(get_current_user)):
    await _require_team_role(tid, current["id"], ["owner", "admin", "reviewer"])
    projects = await db.projects.find({"team_id": tid}, {"_id": 0}).to_list(500)
    total_projects = len(projects)
    total_images = 0
    total_annotated = 0
    project_summaries = []
    for p in projects:
        pid = p["id"]
        imgs = await db.images.count_documents({"project_id": pid})
        ann = await db.images.count_documents({"project_id": pid, "annotated": True})
        approved = await db.images.count_documents({"project_id": pid, "review_status": "approved"})
        submitted = await db.images.count_documents({"project_id": pid, "review_status": "submitted"})
        total_images += imgs
        total_annotated += ann
        project_summaries.append({
            "id": pid, "name": p["name"],
            "images": imgs, "annotated": ann,
            "approved": approved, "submitted": submitted,
            "completion_pct": round(approved / imgs * 100, 1) if imgs else 0,
        })
    project_summaries.sort(key=lambda x: x["images"], reverse=True)
    return {
        "total_projects": total_projects,
        "total_images": total_images,
        "total_annotated": total_annotated,
        "projects": project_summaries,
    }


# ----------------- Versions & Export ----------------- #
@api_router.post("/projects/{pid}/versions")
async def create_version(pid: str, payload: VersionCreate, current=Depends(get_current_user)):
    await _verify_project(pid, current["id"])
    images = await db.images.find({"project_id": pid, "annotated": True}, {"_id": 0}).to_list(2000)
    vid = str(uuid.uuid4())
    doc = {
        "id": vid,
        "project_id": pid,
        "name": payload.name,
        "notes": payload.notes or "",
        "image_count": len(images),
        "snapshot_image_ids": [i["id"] for i in images],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.versions.insert_one(doc)
    doc.pop("_id", None)
    return doc


@api_router.get("/projects/{pid}/versions")
async def list_versions(pid: str, current=Depends(get_current_user)):
    await _verify_project(pid, current["id"])
    versions = await db.versions.find({"project_id": pid}, {"_id": 0}).to_list(500)
    versions.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return versions


@api_router.get("/projects/{pid}/export")
async def export_dataset(
    pid: str,
    format: str = "yolo",
    train_pct: float = 0.7,
    valid_pct: float = 0.2,
    test_pct: float = 0.1,
    current=Depends(get_current_user),
):
    project = await _verify_project(pid, current["id"])
    images = await db.images.find({"project_id": pid, "annotated": True}, {"_id": 0}).to_list(2000)
    classes = project.get("classes", [])
    label_to_idx = {c: i for i, c in enumerate(classes)}

    def _ann_type(a):
        return a.get("type", "bbox")

    def _bbox_of(a):
        """Return (x, y, w, h) for any annotation - fallback for formats that need bbox."""
        t = _ann_type(a)
        if t == "polygon" or t == "polyline":
            pts = a.get("points", [])
            if not pts:
                return 0, 0, 0, 0
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            return min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)
        if t == "point":
            return a.get("x", 0), a.get("y", 0), 0.005, 0.005
        if t == "ellipse":
            cx, cy = a.get("x", 0), a.get("y", 0)
            rx, ry = a.get("rx", 0), a.get("ry", 0)
            return cx - rx, cy - ry, 2 * rx, 2 * ry
        return a.get("x", 0), a.get("y", 0), a.get("w", 0), a.get("h", 0)

    def _fetch_image(img):
        try:
            data, ct = get_object(img["storage_path"])
            return data, ct
        except Exception as e:
            logger.error(f"Fetch image {img['id']} failed: {e}")
            return None, None

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # classes.txt (universal)
        zf.writestr("dataset/classes.txt", "\n".join(classes) + "\n")

        if format == "yolo" or format == "yolo_obb":
            zf.writestr("dataset/data.yaml", f"path: ./\ntrain: images\nval: images\nnames: {classes}\nnc: {len(classes)}\n")
            for img in images:
                # image bytes
                data, _ = _fetch_image(img)
                ext = (img["filename"].rsplit(".", 1)[-1] if "." in img["filename"] else "jpg").lower()
                img_name = f"{img['id']}.{ext}"
                if data:
                    zf.writestr(f"dataset/images/{img_name}", data)
                # labels
                lines = []
                for a in img.get("annotations", []):
                    idx = label_to_idx.get(a["label"], 0)
                    t = _ann_type(a)
                    if t == "polygon" and a.get("points"):
                        # YOLO segmentation format: cls x1 y1 x2 y2 ...
                        flat = " ".join(f"{p[0]:.6f} {p[1]:.6f}" for p in a["points"])
                        lines.append(f"{idx} {flat}")
                    elif format == "yolo_obb" and t in ("bbox", None) and a.get("rotation"):
                        # YOLO OBB format: cls x1 y1 x2 y2 x3 y3 x4 y4 (all normalized)
                        x, y, w, h = _bbox_of(a)
                        cx, cy = x + w / 2, y + h / 2
                        theta = float(a.get("rotation", 0.0))  # radians
                        cos_t, sin_t = math.cos(theta), math.sin(theta)
                        hw, hh = w / 2, h / 2
                        # Corners in bbox-local space (unrotated) → rotated around (cx, cy)
                        local = [(-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)]
                        corners = []
                        for lx, ly in local:
                            rx = cx + lx * cos_t - ly * sin_t
                            ry = cy + lx * sin_t + ly * cos_t
                            corners.append(f"{max(0, min(1, rx)):.6f} {max(0, min(1, ry)):.6f}")
                        lines.append(f"{idx} " + " ".join(corners))
                    else:
                        x, y, w, h = _bbox_of(a)
                        cx = x + w / 2
                        cy = y + h / 2
                        lines.append(f"{idx} {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}")
                zf.writestr(f"dataset/labels/{img['id']}.txt", "\n".join(lines))

        elif format == "coco":
            coco = {
                "info": {"description": project["name"], "version": "1.0",
                         "date_created": datetime.now(timezone.utc).isoformat()},
                "categories": [{"id": i, "name": c, "supercategory": "object"} for i, c in enumerate(classes)],
                "images": [],
                "annotations": [],
            }
            ann_id = 1
            for i, img in enumerate(images):
                data, _ = _fetch_image(img)
                ext = (img["filename"].rsplit(".", 1)[-1] if "." in img["filename"] else "jpg").lower()
                img_name = f"{img['id']}.{ext}"
                # Determine actual dimensions
                width, height = 1, 1
                if data:
                    try:
                        from PIL import Image as PILImage
                        with PILImage.open(io.BytesIO(data)) as pim:
                            width, height = pim.size
                    except Exception:
                        width, height = 1, 1
                    zf.writestr(f"dataset/images/{img_name}", data)
                coco["images"].append({"id": i, "file_name": img_name, "width": width, "height": height})
                for a in img.get("annotations", []):
                    t = _ann_type(a)
                    x, y, w, h = _bbox_of(a)
                    coco_ann = {
                        "id": ann_id,
                        "image_id": i,
                        "category_id": label_to_idx.get(a["label"], 0),
                        "bbox": [x * width, y * height, w * width, h * height],
                        "area": (w * width) * (h * height),
                        "iscrowd": 0,
                    }
                    if t == "polygon" and a.get("points"):
                        seg = []
                        for p in a["points"]:
                            seg.append(p[0] * width)
                            seg.append(p[1] * height)
                        coco_ann["segmentation"] = [seg]
                    coco["annotations"].append(coco_ann)
                    ann_id += 1
            zf.writestr("dataset/annotations.json", json.dumps(coco, indent=2))

        elif format == "voc":
            for img in images:
                data, _ = _fetch_image(img)
                ext = (img["filename"].rsplit(".", 1)[-1] if "." in img["filename"] else "jpg").lower()
                img_name = f"{img['id']}.{ext}"
                width, height = 1, 1
                if data:
                    try:
                        from PIL import Image as PILImage
                        with PILImage.open(io.BytesIO(data)) as pim:
                            width, height = pim.size
                    except Exception:
                        pass
                    zf.writestr(f"dataset/images/{img_name}", data)
                xml = f'<?xml version="1.0"?>\n<annotation>\n  <folder>images</folder>\n  <filename>{img_name}</filename>\n'
                xml += f"  <size>\n    <width>{width}</width>\n    <height>{height}</height>\n    <depth>3</depth>\n  </size>\n"
                for a in img.get("annotations", []):
                    x, y, w, h = _bbox_of(a)
                    xmin = int(x * width); ymin = int(y * height)
                    xmax = int((x + w) * width); ymax = int((y + h) * height)
                    xml += f"  <object>\n    <name>{a['label']}</name>\n    <pose>Unspecified</pose>\n    <truncated>0</truncated>\n    <difficult>0</difficult>\n"
                    xml += f"    <bndbox>\n      <xmin>{xmin}</xmin>\n      <ymin>{ymin}</ymin>\n      <xmax>{xmax}</xmax>\n      <ymax>{ymax}</ymax>\n    </bndbox>\n"
                    if _ann_type(a) == "polygon" and a.get("points"):
                        xml += "    <polygon>\n"
                        for pi, p in enumerate(a["points"]):
                            xml += f"      <x{pi+1}>{int(p[0]*width)}</x{pi+1}>\n      <y{pi+1}>{int(p[1]*height)}</y{pi+1}>\n"
                        xml += "    </polygon>\n"
                    xml += "  </object>\n"
                xml += "</annotation>\n"
                zf.writestr(f"dataset/annotations/{img['id']}.xml", xml)
        elif format == "split":
            if abs(train_pct + valid_pct + test_pct - 1.0) > 1e-6:
                raise HTTPException(status_code=400, detail="train_pct + valid_pct + test_pct must sum to 1.0")
            local_tmp_dir, _test_image_ids = await _build_split_dataset_dir(pid, project, train_pct, valid_pct, test_pct)
            try:
                for root, _dirs, files in os.walk(local_tmp_dir):
                    for fname in files:
                        abs_path = os.path.join(root, fname)
                        rel_path = os.path.relpath(abs_path, local_tmp_dir)
                        zf.write(abs_path, f"dataset/{rel_path.replace(os.sep, '/')}")
            finally:
                import shutil
                shutil.rmtree(local_tmp_dir, ignore_errors=True)
        else:
            raise HTTPException(status_code=400, detail="Unsupported format")

    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={project['name']}_{format}.zip"},
    )


def _yolo_label_line(a: dict, cls_to_idx: dict) -> Optional[str]:
    """Annotation -> one YOLO detection line ("cls cx cy w h"), or None to
    skip. Deliberately mirrors _train_yolo_sync's conversion (skip unknown
    labels, skip degenerate zero/negative-size boxes) rather than
    export_dataset's own _bbox_of/_ann_type closures (which lack both
    guards) - this feeds a plain-detection remote training run, so it always
    reduces to an axis-aligned box (no OBB, no YOLO-seg polygon lines), same
    as _train_yolo_sync's own output. Neither existing implementation is
    touched; this is a new, independent helper."""
    label = a.get("label")
    if label not in cls_to_idx:
        return None
    idx = cls_to_idx[label]
    t = a.get("type", "bbox")
    if t == "polygon" and a.get("points"):
        pts = a["points"]
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        x, y = min(xs), min(ys)
        w, h = max(xs) - x, max(ys) - y
    elif t == "ellipse":
        cx0, cy0 = a.get("x", 0), a.get("y", 0)
        rx, ry = a.get("rx", 0), a.get("ry", 0)
        x, y, w, h = cx0 - rx, cy0 - ry, 2 * rx, 2 * ry
    else:  # bbox / polyline fallback (rotation, if any, is ignored)
        x, y, w, h = a.get("x", 0), a.get("y", 0), a.get("w", 0), a.get("h", 0)
    if w <= 0 or h <= 0:
        return None
    cx, cy = x + w / 2, y + h / 2
    return f"{idx} {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}"


async def _build_split_dataset_dir(pid: str, project: dict, train_pct: float = 0.7,
                                    valid_pct: float = 0.2, test_pct: float = 0.1):
    """Writes a real train/valid/test split directory to a temp dir (unlike
    export_dataset's in-memory ZIP) and returns (local_tmp_dir,
    test_image_ids). Layout is {tmp_root}/{split}/{images,labels}/ - split
    first, not type-first like _train_yolo_sync's images/{split}/ - because
    pipeline_logic.validate_dataset_dir (ported from cicd-pipeline-main)
    hardcodes exactly this shape. Used both by export_dataset's "split"
    format (manual download) and, from M4 onward, the pipeline's own
    uploading_data stage (SCP'd directly, no ZIP involved)."""
    images = await db.images.find({"project_id": pid, "annotated": True}, {"_id": 0}).to_list(2000)
    classes = project.get("classes", [])
    cls_to_idx = {c: i for i, c in enumerate(classes)}

    tmp_root = tempfile.mkdtemp(prefix=f"split_export_{pid}_")
    split_dirs = {}
    for split in SPLITS:
        img_dir = os.path.join(tmp_root, split, "images")
        lbl_dir = os.path.join(tmp_root, split, "labels")
        os.makedirs(img_dir, exist_ok=True)
        os.makedirs(lbl_dir, exist_ok=True)
        split_dirs[split] = (img_dir, lbl_dir)

    random.shuffle(images)
    n_train = int(len(images) * train_pct)
    n_valid = int(len(images) * valid_pct)
    # The remainder (not int(len * test_pct)) becomes test, so no image is
    # lost to rounding.
    assignments = (
        [(img, "train") for img in images[:n_train]]
        + [(img, "valid") for img in images[n_train:n_train + n_valid]]
        + [(img, "test") for img in images[n_train + n_valid:]]
    )

    test_image_ids = []
    for img, split in assignments:
        img_dir, lbl_dir = split_dirs[split]
        try:
            data, _ct = get_object(img["storage_path"])
            if not data:
                raise Exception("empty image data")
            lines = [
                line for a in img.get("annotations", [])
                if (line := _yolo_label_line(a, cls_to_idx)) is not None
            ]
            ext = (img["filename"].rsplit(".", 1)[-1] if "." in img["filename"] else "jpg").lower()
            with open(os.path.join(img_dir, f"{img['id']}.{ext}"), "wb") as f:
                f.write(data)
            with open(os.path.join(lbl_dir, f"{img['id']}.txt"), "w") as f:
                f.write("\n".join(lines))
            if split == "test":
                test_image_ids.append(img["id"])
        except Exception as ie:
            # Skip the WHOLE pair on failure (unlike export_dataset's other
            # formats, which still write an orphan label) - a label with no
            # image is worse than useless for training.
            logger.warning(f"Skip image {img['id']} from split export: {ie}")

    yaml_path = os.path.join(tmp_root, "data.yaml")
    with open(yaml_path, "w") as f:
        f.write(
            f"path: {tmp_root}\ntrain: train/images\nval: valid/images\ntest: test/images\n"
            f"nc: {len(classes)}\nnames: {classes}\n"
        )

    return tmp_root, test_image_ids


# ----------------- Training (Simulated) ----------------- #
@api_router.post("/projects/{pid}/train")
async def train_model(pid: str, payload: TrainRequest, current=Depends(get_current_user)):
    await _verify_project(pid, current["id"])
    version = await db.versions.find_one({"id": payload.version_id, "project_id": pid})
    if not version:
        raise HTTPException(status_code=404, detail="Version not found")

    # Generate synthetic but realistic training curves
    metrics = []
    loss = 2.5
    mAP = 0.05
    for epoch in range(1, payload.epochs + 1):
        loss = max(0.05, loss * (0.93 + random.random() * 0.03))
        mAP = min(0.97, mAP + (0.97 - mAP) * (0.08 + random.random() * 0.04))
        metrics.append({
            "epoch": epoch,
            "loss": round(loss, 4),
            "mAP": round(mAP, 4),
            "precision": round(min(0.99, mAP + random.uniform(0.02, 0.05)), 4),
            "recall": round(min(0.99, mAP - random.uniform(0.01, 0.04)), 4),
        })

    mid = str(uuid.uuid4())
    doc = {
        "id": mid,
        "project_id": pid,
        "version_id": payload.version_id,
        "model_arch": payload.model_arch,
        "epochs": payload.epochs,
        "metrics": metrics,
        "final_mAP": metrics[-1]["mAP"],
        "final_loss": metrics[-1]["loss"],
        "status": "deployed",
        "endpoint": f"/api/inference/{mid}",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.models.insert_one(doc)
    doc.pop("_id", None)
    return doc


@api_router.get("/projects/{pid}/models")
async def list_models(pid: str, current=Depends(get_current_user)):
    await _verify_project(pid, current["id"])
    models = await db.models.find({"project_id": pid}, {"_id": 0}).to_list(200)
    models.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return models


# ----------------- Real Model Training (YOLOv8n) ----------------- #
MIN_TRAINING_IMAGES = 30
CONFIDENCE_THRESHOLD = 0.4


def auto_scale_epochs(image_count: int, requested: Optional[int] = None) -> int:
    """Return a sensible epoch count based on dataset size.
    - < 50 imgs → 10 epochs (fast small-data)
    - 50-150 imgs → 20 epochs
    - 150-500 imgs → 30 epochs
    - > 500 imgs → 50 epochs
    User-requested value overrides but is capped 1-200.
    """
    if requested is not None and requested > 0:
        return max(1, min(200, requested))
    if image_count < 50:
        return 10
    if image_count < 150:
        return 20
    if image_count < 500:
        return 30
    return 50


def _train_yolo_sync(job_id: str, project_id: str, model_arch: str, epochs: int):
    """Blocking YOLOv8 training in a thread."""
    from pymongo import MongoClient
    from ultralytics import YOLO
    from PIL import Image as PILImage
    import torch
    device = "cuda" if torch.cuda.is_available() else "cpu"
    sync_client = MongoClient(MONGO_URL)
    sync_db = sync_client[DB_NAME]

    tmp_root = tempfile.mkdtemp(prefix=f"yolo_train_{job_id}_")
    try:
        images_dir = os.path.join(tmp_root, "images")
        labels_dir = os.path.join(tmp_root, "labels")
        os.makedirs(images_dir); os.makedirs(labels_dir)

        # Fetch project
        project = sync_db.projects.find_one({"id": project_id})
        classes = project.get("classes", [])
        cls_to_idx = {c: i for i, c in enumerate(classes)}
        if not classes:
            raise Exception("Project has no classes")

        # Pull approved images with bbox annotations
        docs = list(sync_db.images.find({
            "project_id": project_id,
            "review_status": "approved",
            "annotations.0": {"$exists": True},
        }))
        # Also include annotated-but-not-yet-reviewed if too few approved
        if len(docs) < MIN_TRAINING_IMAGES:
            extra = list(sync_db.images.find({
                "project_id": project_id,
                "annotated": True,
                "review_status": {"$ne": "rejected"},
                "annotations.0": {"$exists": True},
            }))
            seen = set(d["id"] for d in docs)
            for e in extra:
                if e["id"] not in seen:
                    docs.append(e); seen.add(e["id"])

        if len(docs) < MIN_TRAINING_IMAGES:
            raise Exception(f"Need at least {MIN_TRAINING_IMAGES} labeled images (have {len(docs)})")

        sync_db.models.update_one({"id": job_id}, {"$set": {"status": "preparing", "training_image_count": len(docs)}})

        # Write dataset (80/20 train/val split)
        random.shuffle(docs)
        split = max(1, int(len(docs) * 0.8))
        for i, img in enumerate(docs):
            try:
                data, ct = get_object(img["storage_path"])
                pim = PILImage.open(io.BytesIO(data)).convert("RGB")
                subset = "train" if i < split else "val"
                sub_img_dir = os.path.join(images_dir, subset)
                sub_lbl_dir = os.path.join(labels_dir, subset)
                os.makedirs(sub_img_dir, exist_ok=True); os.makedirs(sub_lbl_dir, exist_ok=True)
                base = img["id"]
                pim.save(os.path.join(sub_img_dir, f"{base}.jpg"), "JPEG", quality=90)
                lines = []
                for a in img.get("annotations", []):
                    t = a.get("type", "bbox")
                    label = a.get("label")
                    if label not in cls_to_idx:
                        continue
                    idx = cls_to_idx[label]
                    if t == "polygon" and a.get("points"):
                        pts = a["points"]
                        xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
                        x = min(xs); y = min(ys); w = max(xs) - x; h = max(ys) - y
                    elif t == "ellipse":
                        cx, cy = a.get("x", 0), a.get("y", 0)
                        rx, ry = a.get("rx", 0), a.get("ry", 0)
                        x = cx - rx; y = cy - ry; w = 2 * rx; h = 2 * ry
                    else:  # bbox / polyline fallback
                        x = a.get("x", 0); y = a.get("y", 0)
                        w = a.get("w", 0); h = a.get("h", 0)
                    if w <= 0 or h <= 0:
                        continue
                    cx = x + w / 2; cy = y + h / 2
                    lines.append(f"{idx} {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}")
                with open(os.path.join(sub_lbl_dir, f"{base}.txt"), "w") as f:
                    f.write("\n".join(lines))
            except Exception as ie:
                logger.warning(f"Skip image {img['id']}: {ie}")

        # Write data.yaml
        yaml_path = os.path.join(tmp_root, "data.yaml")
        with open(yaml_path, "w") as f:
            f.write(f"path: {tmp_root}\ntrain: images/train\nval: images/val\nnc: {len(classes)}\nnames: {classes}\n")

        sync_db.models.update_one({"id": job_id}, {"$set": {"status": "training"}})

        # Cooperative-cancellation callback (also update progress)
        cancel_state = {"cancelled": False}

        def _check_cancel(trainer, phase: str):
            m = sync_db.models.find_one({"id": job_id}, {"cancel_requested": 1})
            if m and m.get("cancel_requested"):
                cancel_state["cancelled"] = True
                trainer.epoch = trainer.epochs
                raise KeyboardInterrupt(f"Cancelled at {phase}")

        def _on_train_epoch_end(trainer):
            # Update progress
            try:
                cur_epoch = int(getattr(trainer, "epoch", 0)) + 1
                total_epochs = int(getattr(trainer, "epochs", epochs))
                progress = min(100, int((cur_epoch / max(1, total_epochs)) * 100))
                sync_db.models.update_one({"id": job_id}, {"$set": {"current_epoch": cur_epoch, "progress": progress}})
            except Exception:
                pass
            _check_cancel(trainer, "epoch_end")

        def _on_train_batch_end(trainer):
            # More responsive cancel check between batches (won't stop mid-batch but between)
            _check_cancel(trainer, "batch_end")

        model = YOLO(f"{model_arch}.pt")
        try:
            model.add_callback("on_train_epoch_end", _on_train_epoch_end)
            model.add_callback("on_train_batch_end", _on_train_batch_end)
        except Exception:
            pass
        results = model.train(
            data=yaml_path,
            epochs=epochs,
            imgsz=640,
            batch=8,
            device=device,
            project=tmp_root,
            name="run",
            exist_ok=True,
            verbose=False,
            plots=False,
        )

        # Locate best.pt
        run_dir = os.path.join(tmp_root, "run")
        weights_path = os.path.join(run_dir, "weights", "best.pt")
        if not os.path.exists(weights_path):
            weights_path = os.path.join(run_dir, "weights", "last.pt")

        with open(weights_path, "rb") as f:
            weights_bytes = f.read()

        storage_path = f"{APP_NAME}/projects/{project_id}/models/{job_id}.pt"
        put_object(storage_path, weights_bytes, "application/octet-stream")

        # Extract metrics
        metrics = {}
        try:
            m = results.results_dict if hasattr(results, "results_dict") else {}
            metrics = {
                "mAP50": float(m.get("metrics/mAP50(B)", 0)),
                "mAP50_95": float(m.get("metrics/mAP50-95(B)", 0)),
                "precision": float(m.get("metrics/precision(B)", 0)),
                "recall": float(m.get("metrics/recall(B)", 0)),
            }
        except Exception:
            metrics = {"mAP50": 0.0, "mAP50_95": 0.0, "precision": 0.0, "recall": 0.0}

        sync_db.models.update_one({"id": job_id}, {"$set": {
            "status": "trained",
            "classes": classes,  # snapshot at train time - inference must decode against this, not the live project list
            "trained_on": device,
            "weights_path": storage_path,
            "weights_size": len(weights_bytes),
            "final_mAP": metrics["mAP50"],
            "final_loss": 1 - metrics["mAP50_95"],
            "precision": metrics["precision"],
            "recall": metrics["recall"],
            "metrics_full": metrics,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }})
        # Reset retrain nudge counter — next nudge fires after 30 more approvals
        sync_db.projects.update_one({"id": project_id}, {"$set": {"last_retrain_nudge_count": 0}})

    except KeyboardInterrupt as e:
        logger.info(f"Training {job_id} cancelled by user (KeyboardInterrupt)")
        sync_db.models.update_one({"id": job_id}, {"$set": {"status": "cancelled", "cancelled_at": datetime.now(timezone.utc).isoformat()}})
    except Exception as e:
        # Check if it was a cancel
        cancelled = False
        try:
            m = sync_db.models.find_one({"id": job_id}, {"cancel_requested": 1})
            cancelled = bool(m and m.get("cancel_requested"))
        except Exception:
            pass
        if cancelled:
            logger.info(f"Training {job_id} cancelled by user")
            sync_db.models.update_one({"id": job_id}, {"$set": {"status": "cancelled", "cancelled_at": datetime.now(timezone.utc).isoformat()}})
        else:
            logger.error(f"Training {job_id} failed: {e}")
            sync_db.models.update_one({"id": job_id}, {"$set": {"status": "failed", "error": str(e)[:500]}})
    finally:
        import shutil
        try:
            shutil.rmtree(tmp_root, ignore_errors=True)
        except Exception:
            pass
        sync_client.close()


# train-real, get_model, activate_model, cancel_model_training moved to routers/training.py


# ---- Local YOLO inference cache ----
_yolo_cache: dict = {}   # weights_path -> loaded YOLO model


def _load_yolo(weights_path: str):
    from ultralytics import YOLO
    if weights_path in _yolo_cache:
        return _yolo_cache[weights_path]
    data, _ = get_object(weights_path)
    tmp = tempfile.NamedTemporaryFile(suffix=".pt", delete=False)
    tmp.write(data); tmp.close()
    m = YOLO(tmp.name)
    _yolo_cache[weights_path] = m
    return m


def _yolo_predict_sync(weights_path: str, image_bytes: bytes, classes: List[str], confidence: float):
    from PIL import Image as PILImage
    import numpy as np
    model = _load_yolo(weights_path)
    pim = PILImage.open(io.BytesIO(image_bytes)).convert("RGB")
    w, h = pim.size
    arr = np.array(pim)
    res = model.predict(arr, conf=confidence, verbose=False)
    boxes = []
    if not res:
        return boxes
    r = res[0]
    if r.boxes is None or len(r.boxes) == 0:
        return boxes
    for b in r.boxes:
        try:
            xyxy = b.xyxy[0].tolist()
            cls = int(b.cls[0].item())
            conf = float(b.conf[0].item())
            label = classes[cls] if 0 <= cls < len(classes) else "object"
            x1, y1, x2, y2 = xyxy
            boxes.append({
                "type": "bbox",
                "label": label,
                "x": max(0.0, min(1.0, x1 / w)),
                "y": max(0.0, min(1.0, y1 / h)),
                "w": max(0.0, min(1.0, (x2 - x1) / w)),
                "h": max(0.0, min(1.0, (y2 - y1) / h)),
                "confidence": max(0.0, min(1.0, conf)),
                "source": "model",
            })
        except Exception:
            continue
    return boxes


async def _auto_label_router(project_id: str, image_bytes: bytes, project=None) -> tuple:
    """Returns (boxes, source). source in {'model', 'gemini', 'model+gemini', 'model_no_fallback'}.
    Applies per-project settings: confidence_threshold, fallback_to_gemini, min_boxes_threshold."""
    if project is None:
        project = await db.projects.find_one({"id": project_id})
    classes = project.get("classes", []) if project else []
    settings = project.get("settings", {}) if project else {}
    conf = settings.get("confidence_threshold", CONFIDENCE_THRESHOLD)
    fallback_enabled = settings.get("fallback_to_gemini", True)
    min_boxes = settings.get("min_boxes_threshold", 1)

    active = await db.models.find_one({"project_id": project_id, "is_active": True, "status": "trained"})
    model_boxes = []
    model_ran = False
    if active and active.get("weights_path"):
        # Decode against the model's OWN training-time class list, not the
        # live project list - the project's classes keep growing after
        # training (new labels appended on every save + by Gemini itself),
        # so a stale index->label mapping here silently mislabels boxes.
        # Older model docs predating this field fall back to the live list.
        model_classes = active.get("classes") or classes
        try:
            model_boxes = await asyncio.to_thread(_yolo_predict_sync, active["weights_path"], image_bytes, model_classes, conf)
            model_ran = True
        except Exception as e:
            logger.error(f"Local inference failed: {e}")

    if model_ran:
        # Decide whether to fall back
        if len(model_boxes) >= min_boxes:
            return model_boxes, "model"
        if not fallback_enabled:
            return model_boxes, "model_no_fallback"
        # Fall through to Gemini + combine
    # Gemini
    try:
        b64 = base64.b64encode(image_bytes).decode()
        restrict_to_classes = settings.get("gemini_restrict_to_classes", False)
        # None = user hasn't manually curated a subset -> restrict to the
        # full project class list (old behavior); a list (even empty) means
        # they picked specific classes via the Settings UI checkboxes.
        allowed_classes = settings.get("gemini_allowed_classes")
        effective_classes = allowed_classes if allowed_classes is not None else classes
        restricting = bool(restrict_to_classes and effective_classes)
        class_constraint = (
            f" Only use labels from this exact list, spelled exactly as shown: {effective_classes}. "
            "Skip any object that doesn't match one of these labels."
            if restricting else ""
        )
        # Case-insensitive lookup back to the list's exact spelling - Gemini
        # doesn't reliably preserve casing (and the box-building step below
        # otherwise force-lowercases every label), so without this a
        # restricted label like "Doctor" comes back as "doctor" and silently
        # creates a duplicate class instead of matching the intended one.
        allowed_lookup = {c.lower(): c for c in effective_classes} if restricting else None
        chat = LlmChat(
            api_key=EMERGENT_LLM_KEY,
            session_id=f"autolabel-{uuid.uuid4()}",
            system_message=(
                "You are a precise computer vision annotator. Detect all distinct objects in the image. "
                f"Return ONLY a JSON array with objects having keys: label ({'from the list below' if restricting else 'short lowercase noun'}), "
                "x, y, w, h (all normalized 0-1, where x,y is top-left corner of bounding box)."
                f"{class_constraint} "
                "No markdown, no prose."
            ),
        ).with_model("gemini", "gemini-2.5-flash")
        image = ImageContent(image_base64=b64)
        msg = UserMessage(text="Detect all objects. Return the JSON array of bounding boxes only.", file_contents=[image])
        response_text = await chat.send_message(msg)
        raw = response_text.strip().strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        if "[" in raw and "]" in raw:
            raw = raw[raw.index("["): raw.rindex("]") + 1]
        try:
            parsed = json.loads(raw)
            if not isinstance(parsed, list):
                parsed = []
        except Exception:
            parsed = []
        gemini_boxes = []
        for b in parsed:
            try:
                raw_label = str(b.get("label", "object")).strip()[:40]
                if allowed_lookup is not None:
                    label = allowed_lookup.get(raw_label.lower())
                    if label is None:
                        continue  # not in the allowed list - Gemini ignored the constraint, drop it
                else:
                    label = raw_label.lower()
                gemini_boxes.append({
                    "type": "bbox",
                    "label": label,
                    "x": max(0.0, min(1.0, float(b.get("x", 0)))),
                    "y": max(0.0, min(1.0, float(b.get("y", 0)))),
                    "w": max(0.0, min(1.0, float(b.get("w", 0)))),
                    "h": max(0.0, min(1.0, float(b.get("h", 0)))),
                    "source": "gemini",
                })
            except Exception:
                continue
    except Exception as e:
        logger.error(f"Gemini fallback failed: {e}")
        gemini_boxes = []

    if model_ran and model_boxes:
        # Combine: model boxes (kept) + gemini (as extras)
        for b in model_boxes:
            b["source"] = "model"
        return model_boxes + gemini_boxes, "model+gemini"
    return gemini_boxes, "gemini"


# ----------------- Active-Learning Queue ----------------- #

def _uncertainty_score(boxes: List[dict], min_conf: float = 0.4) -> float:
    """Higher score = more uncertain/valuable to label next.
    Combines: 1) low avg confidence, 2) low box count (image might be missed), 3) borderline-confidence boxes (near threshold).
    Returns score in [0, 1]."""
    if not boxes:
        return 0.9  # no detections = highly uncertain (needs manual look)
    confs = [b.get("confidence", 0.5) for b in boxes]
    avg_conf = sum(confs) / len(confs)
    # Boxes near the decision threshold are the most informative
    borderline = sum(1 for c in confs if abs(c - min_conf) < 0.15) / len(confs)
    inv_conf = 1.0 - avg_conf
    # Combine
    return max(0.0, min(1.0, 0.5 * inv_conf + 0.5 * borderline))


# active-learning-queue endpoint moved to routers/active_learning.py
# training-plan endpoint moved to routers/training.py


# ----------------- Register router & middleware ----------------- #
@api_router.get("/")
async def root():
    return {"message": f"{BRAND_NAME} API", "version": "1.0"}


@api_router.get("/system/versions")
async def system_versions():
    """Return runtime versions (visible to logged-in and public — useful for support/debugging)."""
    versions = {"app": "1.0"}
    try:
        import ultralytics
        versions["ultralytics"] = getattr(ultralytics, "__version__", "unknown")
    except Exception:
        versions["ultralytics"] = None
    try:
        import torch
        versions["torch"] = torch.__version__
        versions["cuda_available"] = torch.cuda.is_available()
    except Exception:
        versions["torch"] = None
    versions["supported_archs"] = ["yolov8n", "yolov8s", "yolov8m", "yolov8l"]
    versions["brand"] = BRAND_NAME
    versions["allowed_email_domain"] = ALLOWED_EMAIL_DOMAIN or None
    return versions


# Wire up split-out routers (attach their routes to api_router which has /api prefix)
import sys as _sys
_this_module = _sys.modules[__name__]
from routers import training as _training_router
from routers import active_learning as _al_router
from routers import auth as _auth_router
from routers import teams as _teams_router
from routers import collaboration as _collab_router
from routers import projects as _projects_router
from routers import images as _images_router
from routers import deployment as _deployment_router
_training_router.register(_this_module)
_al_router.register(_this_module)
_auth_router.register(_this_module)
_teams_router.register(_this_module)
_collab_router.register(_this_module)
_projects_router.register(_this_module)
_images_router.register(_this_module)
_deployment_router.register(_this_module)
api_router.include_router(_training_router.router)
api_router.include_router(_al_router.router)
api_router.include_router(_auth_router.router)
api_router.include_router(_teams_router.router)
api_router.include_router(_collab_router.router)
api_router.include_router(_projects_router.router)
api_router.include_router(_images_router.router)
api_router.include_router(_deployment_router.router)

app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup():
    try:
        init_storage()
        logger.info("Storage initialized")
    except Exception as e:
        logger.error(f"Storage init failed: {e}")
    # Performance indexes (idempotent)
    try:
        await db.images.create_index([("project_id", 1), ("review_status", 1), ("created_at", -1)])
        await db.images.create_index([("assigned_to", 1)])
        await db.images.create_index([("storage_path", 1)])
        await db.activity_log.create_index([("project_id", 1), ("created_at", -1)])
        await db.notifications.create_index([("user_id", 1), ("created_at", -1)])
        await db.team_members.create_index([("team_id", 1), ("user_id", 1)])
        await db.team_members.create_index([("invited_email", 1), ("status", 1)])
        await db.project_assignments.create_index([("project_id", 1), ("user_id", 1)])
        await db.comments.create_index([("image_id", 1), ("created_at", 1)])
        await db.models.create_index([("project_id", 1), ("is_active", 1)])
        await db.models.create_index([("project_id", 1), ("status", 1)])
        await db.images.create_index([("id", 1)])
        await db.images.create_index([("project_id", 1), ("annotated", 1)])
        await db.users.create_index([("id", 1)])
        await db.projects.create_index([("id", 1)])
        await db.teams.create_index([("id", 1)])
        await db.videos.create_index([("id", 1)])
        await db.videos.create_index([("project_id", 1)])
        await db.batch_jobs.create_index([("project_id", 1)])
        await db.versions.create_index([("project_id", 1)])
        await db.comments.create_index([("id", 1)])
        logger.info("MongoDB indexes ensured")
    except Exception as e:
        logger.error(f"Index creation failed: {e}")


@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()

