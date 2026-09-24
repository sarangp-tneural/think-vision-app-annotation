# VisionForge — Roboflow-like Computer Vision Platform

## Changelog (latest first)
### 2026-02 — Iteration 14
- **Rebrand VisionForge → ThinkVision**: navbar wordmark, auth page, landing footer, browser title, email templates, FastAPI title, `/api/` root response. New `BRAND_NAME` env var (default "ThinkVision"). Storage-path key `APP_NAME` kept as `"visionforge"` to preserve existing image URLs — invisible to users.
- **Domain restriction**: new `ALLOWED_EMAIL_DOMAIN` env var (currently `tneuralai.com`). Enforced in `auth/register`, `auth/login`, and `teams/{tid}/invite`. Empty value = open registration. Auth page fetches the domain from `/api/system/versions` and shows a hint under the email field. Existing users at other domains (e.g. `demo@vf.io`) are locked out until the env is cleared.
- **New demo account**: `admin@tneuralai.com / tneural123` — recorded in `/app/memory/test_credentials.md`.
- Regression: pytest 26/26 pass (test emails switched to `@tneuralai.com`).

### 2026-02 — Iteration 13
- **Full router split**: extracted 5 more router modules — `routers/auth.py` (register/login/me), `routers/teams.py` (CRUD + invites + members), `routers/collaboration.py` (comments + replies + resolve + activity + notifications), `routers/projects.py` (CRUD + classes + bulk classes + settings), `routers/images.py` (upload/list/get/delete/annotations/assign/submit/review + file download). Combined with the earlier `training.py` and `active_learning.py` — the backend now has **7 routers**.
- **server.py: 2385 → 1673 lines** (-30%). All router modules use the same `register(server_module)` late-binding pattern so they can share helpers, models, and Mongo client without circular imports.
- **Regression**: pytest 26/26 pass; auth/teams/projects/images/notifications/training-plan/AL-queue all verified via curl and UI smoke.

### 2026-02 — Iteration 12
- **Frame interval by seconds**: video extract mode picker (frames vs seconds); backend resolves seconds → frames using the video's FPS (`_extract_frames_sync` reads `cv2.CAP_PROP_FPS`).
- **Select All (bulk assign)**: new "Select All / Deselect All" button next to Clear in the images tab, respects the active status filter.
- **Edit annotation label**: annotation-panel rows in the Annotator now render an inline Select dropdown letting users switch a bbox's label without deleting/redrawing (previously only move/resize/rotate were supported).
- **Bulk class management**: new "Manage Classes" dialog on ProjectDetail with comma/newline-separated bulk add + one-click remove per class. Backend: `POST /api/projects/{pid}/classes/bulk`, `DELETE /api/projects/{pid}/classes/{label}`.
- **YOLO version + arch selector**: `/api/system/versions` endpoint exposes ultralytics/torch/GPU status; Deploy page shows the version banner and adds a YOLOv8n/s/m/l architecture dropdown for real training (previously hardcoded to yolov8n).
- Regression: pytest 26/26 pass.

### 2026-02 — Iteration 11
- **Retrain nudge**: after 30 approvals since the last successful training (`RETRAIN_NUDGE_THRESHOLD=30`), owner + admins get a `retrain_nudge` notification. Idempotent via `projects.last_retrain_nudge_count`, reset on training completion. Fires from `_maybe_send_retrain_nudge` inside `POST /api/images/{img_id}/review` when `decision=approve`.
- **Rotated bounding boxes (OBB)**: annotations accept optional `rotation` (radians). New rotate handle above selected bbox in the Annotator; CSS `transform: rotate(...)` around center. Export gains `format=yolo_obb` — writes 4-corner rotated format (`cls x1 y1 x2 y2 x3 y3 x4 y4`) when any bbox has non-zero rotation; falls back to standard YOLO for un-rotated boxes. New "YOLO OBB (Rotated)" option in Versions/Export UI.
- **Router split (partial)**: extracted `train-real`, `get_model`, `activate_model`, `cancel_model_training`, `training-plan` → `/app/backend/routers/training.py`; `active-learning-queue` → `/app/backend/routers/active_learning.py`. Uses `register(server_module)` pattern to share state cleanly. Remaining endpoints (auth, teams, projects, images, exports, comments, notifications) still in server.py — planned as next split phase.
- Tests: `/app/test_reports/iteration_11.json` (backend 15/15 pass).

### 2026-02 — Iteration 10
- **Bbox move/resize**: click bbox → select; drag body → move; 8 corner/edge handles → resize. Coord math accounts for zoom/pan. `Annotator.jsx`.
- **Resend email invites**: `POST /api/teams/{tid}/invite` now sends HTML email via Resend. Sender: `invites@tneuralai.com` (domain awaiting DNS verification in Resend dashboard — until verified, `email_result.sent=false` with clear error, but invite record is still created and auto-activates on signup). Env: `RESEND_API_KEY`, `SENDER_EMAIL`, `APP_URL`.
- **Auto-scale training epochs**: `epochs=0` → server picks 10/20/30/50 based on eligible image count. New endpoint `GET /api/projects/{pid}/training-plan` returns eligible count, recommended epochs, est. CPU duration. UI hint updates live in Deploy.
- **In-flight training cancel**: added `on_train_batch_end` callback + `KeyboardInterrupt` handling → cancel now takes effect between batches, not just epochs. Cancelled models persist `status='cancelled'` with `cancelled_at`.
- **Active-learning queue**: `GET /api/projects/{pid}/active-learning-queue?limit=N` ranks unlabeled/rejected images by uncertainty score. New `AL Queue` tab in ProjectDetail with uncertainty bars, click-to-annotate.
- Tests: `/app/test_reports/iteration_9.json` (backend 11/11), `iteration_10.json` (frontend 5/5).

## Original Problem Statement
Build an application like Roboflow.

## User Choices (defaults confirmed)
- Web-based app
- Full feature MVP (project mgmt + annotation + versioning + export + AI auto-label + train/deploy)
- JWT email/password auth
- Gemini vision auto-labeling
- Emergent object storage for images
- Designer-decided theme (dark developer aesthetic)

## Architecture
- **Frontend**: React 19 + React Router 7 + Shadcn UI + Tailwind + Recharts
- **Backend**: FastAPI + Motor (Mongo async) + bcrypt + PyJWT
- **Storage**: Emergent object storage (session-scoped key)
- **AI**: emergentintegrations `LlmChat` -> `gemini-2.5-flash` for vision auto-labeling
- **Font**: Outfit (headings) + JetBrains Mono (body) — dark #050505 with cyan #06B6D4 accents

## User Personas
- ML engineers building CV datasets
- Data annotation teams
- Solo developers prototyping vision models

## Core Requirements (static)
1. Auth: register/login, JWT persisted in localStorage
2. Projects: create/list/delete with task type (detection/classification/segmentation)
3. Images: upload to storage, list, delete, view via authed proxy
4. Annotator: click-drag bounding boxes, multi-class, undo, auto-label
5. Versions: snapshot annotated dataset
6. Export: YOLO / COCO / Pascal VOC as zip
7. Deploy: simulated training with mAP/loss/precision/recall curves + inference endpoint

## Implemented (2026-02)
- ✅ Full JWT auth (register/login/me) with bcrypt
- ✅ Project CRUD + class management
- ✅ Image upload/list/delete via Emergent storage; proxy download with ?auth= query
- ✅ Bounding-box annotator (click-drag, multi-class, sidebar boxlist)
- ✅ Gemini 2.5-flash auto-labeling
- ✅ Dataset versioning
- ✅ Export to YOLO / COCO / VOC (zip download)
- ✅ Simulated training with real-time metrics via Recharts
- ✅ Landing page + 5 authenticated views
- ✅ 22/22 backend E2E tests passing

### Feature update (2026-02): Phase 2 — Confidence Threshold + Fallback Rules
- ✅ **Per-project settings** stored on project doc: `confidence_threshold` (0.05–0.95, default 0.4), `fallback_to_gemini` (bool, default true), `min_boxes_threshold` (int, default 1), `show_confidence` (bool, default true)
- ✅ **`PATCH /api/projects/{pid}/settings`** — owner/admin only, values clamped, logged to activity
- ✅ **Smart router upgrade** — `_auto_label_router` now honors settings and returns richer `source`: `model` | `gemini` | `model+gemini` (combines when model finds too few) | `model_no_fallback` (when fallback disabled)
- ✅ Every model box carries `confidence` and `source: 'model'`; every Gemini box carries `source: 'gemini'`
- ✅ **Frontend Settings dialog** on Project Detail: sliders for confidence + min-boxes, toggles for fallback + show-confidence
- ✅ **Confidence badge** rendered next to labels on bboxes when `show_confidence` is on
- ✅ **Batch progress** now shows live `source_counts: model X / gemini Y` — you can literally watch Gemini dependency drop
- ✅ **9/9** settings tests pass

### Feature update (2026-02): Phase 1 — Real Self-Training Auto-Labeler (YOLOv8n)
- ✅ **Ultralytics YOLOv8n installed** (torch CPU-only build to save 5 GB disk)
- ✅ **Real training endpoint** `POST /api/projects/{pid}/train-real?epochs=` — 30-image gate, owner/admin only, epochs capped 1-200
- ✅ **Background job** compiles YOLO dataset from approved annotations (fallback to `annotated` when < 30 approved), 80/20 train/val split, uploads `best.pt` (~6 MB) to object storage
- ✅ **Real metrics** captured from Ultralytics `results.results_dict`: mAP@50, mAP@50-95, precision, recall
- ✅ **Model activation** `POST /api/models/{mid}/activate` — single-active constraint per project
- ✅ **Smart auto-label router** `_auto_label_router()`: if active trained model → local YOLO inference (fast, free); else → Gemini (fallback)
- ✅ **Batch auto-label** now uses router + tracks `source_counts: {model: N, gemini: M}`
- ✅ **Response includes `source`** field so UI knows which backend produced boxes
- ✅ **Frontend Deploy page**: "Real Training" tab (default) with train button, live progress, real metrics; separate "Simulated" tab kept for demo curves; "Active" badge on each trained model
- ✅ **8/8** end-to-end tests pass — full pipeline verified with real 3-epoch YOLOv8n training run in ~3m45s on CPU

### Feature update (2026-02): Roboflow-style Team Collaboration + Project Management
- ✅ **New roles**: `reviewer` (approve/reject only) + `annotator` (label-only) added alongside owner/admin/member
- ✅ **Project-level assignments**: `POST/GET/DELETE /api/projects/{pid}/assign` — assign whole projects to specific team members
- ✅ **Access enforcement**: annotator/reviewer roles can only see projects they're assigned to (owners/admins bypass)
- ✅ **Bulk image assignment**: `POST /api/projects/{pid}/images/bulk-assign` with priority/due_date/notes
- ✅ **Task metadata**: `PATCH /api/images/{id}/task` sets priority, due_date, notes on tasks
- ✅ **Comments with threads**: `POST /api/images/{id}/comments`, `POST /api/comments/{id}/replies`, `POST /api/comments/{id}/resolve`, `GET /api/images/{id}/comments`
- ✅ **Activity log**: auto-recorded on all key actions (assign, submit, review, comment, bulk); `GET /api/projects/{pid}/activity`
- ✅ **Notifications**: in-app inbox with unread count, `GET/POST /api/notifications` + `mark-all-read`; pushes on project_assigned, image_assigned, review_needed, review_completed, rejected, comment
- ✅ **Team Dashboard**: `/teams/:tid/dashboard` with real-time stats, per-project completion, and per-member performance table (assigned/submitted/approved/rejected/last_active)
- ✅ **Project Dashboard**: status pie + progress bars + per-member perf via `GET /api/projects/{pid}/dashboard`
- ✅ **Frontend**: Notifications bell in navbar, new /teams/:tid/dashboard page, /projects/:pid/activity page, Comments panel in Annotator, bulk-select checkboxes on Project Detail, Assign Project dialog, reviewer/annotator options in invite dialog
- ✅ **27/27** collaboration tests pass — full multi-user E2E lifecycle verified

### Feature update (2026-02): Review Workflow + Batch Auto-Label + Zoom/Pan
- ✅ Per-image assignment: `POST /api/images/{id}/assign` (assign to team member, validated)
- ✅ Submit for review: `POST /api/images/{id}/submit` (requires annotations)
- ✅ Approve/Reject workflow: `POST /api/images/{id}/review` (owner/admin only, notes captured)
- ✅ Review status lifecycle: unassigned → assigned → submitted → approved/rejected
- ✅ Image query filters: `?status=` and `?assignee=me|<user_id>`
- ✅ Batch auto-label endpoint: `POST /api/projects/{pid}/batch-auto-label` with BackgroundTasks, polls via `/api/batch-jobs/{jid}`
- ✅ Batch job progress: total/processed/labeled/failed counts, status polling every 2.5s
- ✅ Zoom (Ctrl/Cmd+wheel, +/-, 0 reset) and Pan (hold Space + drag) on Annotator canvas
- ✅ Review panel in Annotator right sidebar: status badge, assignee dropdown, Submit/Approve/Reject actions
- ✅ ProjectDetail: status filter tabs (All/Unassigned/Assigned/In Review/Approved/Rejected) + colored status badges on image cards
- ✅ 11/11 review workflow tests pass; batch auto-label manually verified end-to-end (Gemini detected 5 objects in real image)

### Feature update (2026-02): Annotation Tool Enhancement
- ✅ Multi-shape annotation types: bounding box, polygon, polyline, point, ellipse (SVG-rendered)
- ✅ Annotation toolbar with tool selection + keyboard shortcuts (B/P/L/O/E)
- ✅ Save (⌘S), Undo (⌘Z), Finish polygon (Enter), Cancel (Esc)
- ✅ Auto-save (debounced 2s after last change) + "Unsaved" indicator + beforeunload guard
- ✅ Annotation count per image on cards + type badges in annotation list
- ✅ Backward-compat with legacy bbox annotations (default type='bbox')
- ✅ Video upload endpoint (`POST /api/projects/{pid}/videos`) with configurable frame interval
- ✅ Async frame extraction via `cv2` (BackgroundTasks + asyncio.to_thread) with live progress polling
- ✅ Extracted frames appear as regular images (with `from_video` marker)
- ✅ Export now includes actual image bytes + proper folder structure (dataset/images/, dataset/labels/, classes.txt, data.yaml)
- ✅ Export supports polygon segmentation (YOLO-seg format, COCO segmentation JSON) + polygon element in VOC
- ✅ Team creator auto-approved (status="active", role="owner") — verified

### Feature update (2026-02): Multi-user Teams & Collaboration
- ✅ Auto-created "Personal Workspace" on register/login (idempotent + legacy project migration)
- ✅ Team CRUD (`POST/GET/PATCH/DELETE /api/teams`) with cascade delete
- ✅ Roles: owner / admin / member with permission gates
- ✅ Email invites — active immediately if user exists, else auto-activated on signup
- ✅ Member management (promote/demote/remove) via dropdown menu
- ✅ Project ↔ team scoping; projects list shows team_name badge; team filter dropdown
- ✅ Multi-user access: shared projects visible to all active team members
- ✅ 25/25 teams collaboration backend tests passing

## MOCKED Integrations
- `POST /api/projects/{pid}/train` — returns synthetic mAP/loss curves (no real training). Intentional MVP.

## Prioritized Backlog
### P1
- Polygon / segmentation mask annotation tool
- Real training via cloud GPU or Ultralytics YOLO local
- Multi-user teams / collaboration & role-based permissions
- Inference endpoint that actually runs the trained model

### P2
- Dataset augmentation preview (rotation/blur/noise)
- Active learning queue (surface low-confidence images)
- Public dataset marketplace / templates
- Batch auto-label (whole project in one click)

## Next Tasks
1. Add polygon annotation mode
2. Batch auto-label endpoint that iterates project images
3. Add REAL inference endpoint using the last-trained model
