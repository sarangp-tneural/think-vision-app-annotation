# ThinkVision: Product and Technical Specification

> **Purpose.** A complete description of the ThinkVision application: what it does, how each feature works, the data model, the API, the UI and the main workflows. It is written so that someone who cannot see the source (for example an AI assistant asked to design or implement a change) can still reason accurately about the system.
>
> **Status.** This document supersedes `SPECIFICATION.md`, which predates the deploy pipeline, the manager dashboard, work logs and server-side review enforcement. Setup steps live in `README.md` and `SETUP.md`; the deploy pipeline's build plan is in `ROADMAP.md` and `plan.md`.
>
> **Scope.** `backend/` (FastAPI) and `frontend/` (React). The legacy `cicd-pipeline-main/` folder is not covered. Its validate / baseline-eval / train / eval / decide logic was ported into `backend/pipeline_logic.py`.
>
> **Conventions**
> - All API paths are prefixed with `/api`.
> - "Access" means the caller passed the project access check (§4.4).
> - Coordinates are always **normalized to 0–1** relative to image width and height.
> - Every document ID is a UUID4 string in the field `id`. MongoDB's `_id` is never exposed.
> - All timestamps are ISO-8601 UTC strings.

---

## Table of Contents
1. [Overview](#1-overview)
2. [Tech Stack and Architecture](#2-tech-stack-and-architecture)
3. [Setup and Configuration](#3-setup-and-configuration)
4. [Authentication and Authorization](#4-authentication-and-authorization)
5. [Data Model](#5-data-model)
6. [API Reference](#6-api-reference)
7. [Core Subsystems](#7-core-subsystems)
8. [Frontend Specification](#8-frontend-specification)
9. [Design System](#9-design-system)
10. [End-to-End Workflows](#10-end-to-end-workflows)
11. [Testing](#11-testing)
12. [Known Limitations and Technical Debt](#12-known-limitations-and-technical-debt)
13. [Backlog](#13-backlog)
14. [Guide for Requesting Modifications](#14-guide-for-requesting-modifications)

---

## 1. Overview

**ThinkVision** is a web platform for building computer-vision datasets and models, similar to Roboflow. It was originally named **VisionForge**; traces of the old name remain (`vf_` localStorage keys, the `APP_NAME=visionforge` storage prefix, the FastAPI title). It was built on the Emergent platform and uses Emergent-hosted object storage and LLM access. It is operated by tneuralai.com (`ALLOWED_EMAIL_DOMAIN=tneuralai.com`).

**Users:** annotation teams (annotators, reviewers, admins), ML engineers, and company managers who need visibility into output.

**Capabilities**
- **Organize work.** Sign up, get a personal workspace, create teams and invite teammates with roles (owner, admin, member, reviewer, annotator).
- **Collect data.** Create projects (detection, classification or segmentation; the type is only a label). Upload images, or upload video and extract frames by count or by seconds.
- **Annotate.** Browser annotator with boxes (including rotated boxes / OBB), polygons, polylines, points and ellipses; zoom and pan; undo; auto-save; threaded comments.
- **AI auto-label.** Uses the project's active YOLO model, falling back to Gemini 2.5 Flash. Runs per image or as a batch job.
- **Review workflow (enforced on the server).** `unassigned → assigned → submitted → approved`, with rejection returning the image to its annotator. Only approved images count as annotated and only approved images feed datasets, training, exports and deploys.
- **Datasets and models.** Dataset version snapshots; YOLO / YOLO-seg / YOLO OBB / COCO / VOC exports; real YOLOv8 n/s/m/l training on CPU with progress and cancel; an active-learning queue.
- **Deploy pipeline.** A guided wizard over SSH/SCP: class check, data upload, remote training, model download, video test, human approval, deploy, and one-click rollback.
- **Manager dashboard.** A read-only, company-wide view of who annotated how much, built from automatic tracking plus a daily work log.
- **Track progress.** Team and project dashboards, an activity log and in-app notifications.

---

## 2. Tech Stack and Architecture

### 2.1 Backend
| Concern | Technology |
|---|---|
| Web framework | FastAPI, Uvicorn, Pydantic v2 |
| Database | MongoDB via **Motor** (async). Background threads use a separate synchronous **PyMongo** client. |
| Auth | PyJWT (HS256), bcrypt |
| ML | Ultralytics 8.x (YOLOv8), PyTorch (CPU), OpenCV, Pillow, NumPy |
| LLM | `emergentintegrations` `LlmChat` → Gemini 2.5 Flash (`EMERGENT_LLM_KEY`) |
| File storage | Emergent object storage, accessed with `requests` |
| Remote execution | `paramiko` (SSH/SFTP) in `ssh_helper.py` |
| Spreadsheets | `openpyxl` (work-log import) |
| Email | Resend (team invites only) |
| Background work | FastAPI `BackgroundTasks` + `asyncio.to_thread` in the same process. No queue or worker. |

### 2.2 Frontend
| Concern | Technology |
|---|---|
| Framework | React 19, Create React App wrapped with CRACO |
| Routing | react-router-dom 7 |
| HTTP | axios, one shared instance in `src/lib/api.js` |
| State | Local `useState` per page; auth in `localStorage` |
| UI kit | shadcn/ui on Radix, Tailwind CSS 3, lucide-react, recharts, sonner |
| Annotation canvas | No canvas library: an `<img>`, an SVG overlay (`viewBox 0 0 100 100`) and absolutely-positioned `<div>`s |
| Package manager | **Yarn 1.x** (`npm install` fails with ERESOLVE) |

### 2.3 Architecture
```
React SPA ──HTTPS/JSON (Bearer JWT)──▶ FastAPI (server.py + routers/*.py)  /api/*
 (polling, no WebSocket)                │
                                        ├─ MongoDB (metadata)
                                        ├─ Emergent object store (images, video, frames, best.pt)
                                        ├─ Gemini 2.5 Flash (via Emergent LLM proxy)
                                        ├─ Resend (invite email)
                                        └─ SSH/SFTP ──▶ remote GPU/training host(s) (deploy pipeline)
Background threads: video frame extraction · batch auto-label · YOLO training · pipeline stage workers
```

### 2.4 Repository layout
```
think-vision-app/
├── backend/
│   ├── server.py              # app setup, shared helpers, auth deps, storage, videos, auto-label,
│   │                          # batch jobs, assignments, dashboards, versions, export, training engine
│   ├── routers/
│   │   ├── auth.py            # register / login / manager-login / me
│   │   ├── teams.py           # teams, members, invites
│   │   ├── projects.py        # projects, classes, settings
│   │   ├── images.py          # upload, list, assign, submit, review, file proxy, annotations
│   │   ├── training.py        # train-real, model get/rename/delete/activate/cancel, training-plan
│   │   ├── active_learning.py # AL queue
│   │   ├── collaboration.py   # comments, activity, notifications
│   │   ├── deployment.py      # deploy pipeline endpoints
│   │   ├── manager.py         # manager analytics (read-only)
│   │   └── work_logs.py       # daily work log + xlsx/csv import
│   ├── deployment_worker.py   # per-stage background workers for the pipeline
│   ├── pipeline_logic.py      # dataset validation, class diff, script rendering, evaluate, decide_deploy
│   ├── ssh_helper.py          # paramiko wrapper: connect, exec, SFTP up/down, remote path helpers
│   ├── schemas/deployment.py  # Pydantic request models (carry secrets; never persisted)
│   ├── scripts/               # create_manager.py, backfill_review_status.py, cleanup_test_data.py
│   └── tests/                 # pytest integration tests (live HTTP)
├── frontend/src/
│   ├── App.js                 # routes
│   ├── lib/api.js             # axios, auth helpers, fileUrl()
│   ├── pages/                 # Landing, Auth, Dashboard, ProjectDetail, Annotator, Versions,
│   │                          # ModelDeploy, DeployPipeline, Teams, TeamDetail, TeamDashboard,
│   │                          # Activity, MyWorkLog, manager/*
│   └── components/            # Navbar, NotificationsBell, ui/* (shadcn)
├── cicd-pipeline-main/        # legacy GitHub-Actions pipeline (superseded)
├── memory/                    # PRD.md changelog, test_credentials.md (git-ignored)
├── tests/, test_reports/, design_guidelines.json
└── README.md, SETUP.md, ROADMAP.md, plan.md, SPECIFICATION.md (old), spec.md (this file)
```

**Router registration pattern.** Each file in `routers/` defines `router = APIRouter()` and `register(server_module)`. That function pulls `db`, helpers and models off `server.py` and declares routes. `server.py` calls `register(...)` for every router and includes them under `/api`. This avoids circular imports. Background workers (`deployment_worker.py`) deliberately import nothing from `server.py`; dependencies like `get_object` are passed in as arguments.

---

## 3. Setup and Configuration

### 3.1 Backend environment (`backend/.env`)
| Name | Required | Purpose |
|---|---|---|
| `MONGO_URL`, `DB_NAME` | yes | MongoDB connection |
| `JWT_SECRET` | yes | HS256 signing secret |
| `EMERGENT_LLM_KEY` | yes | Gemini proxy and object storage key |
| `APP_NAME` | no (`visionforge`) | Storage path prefix |
| `BRAND_NAME` | no (`ThinkVision`) | Name used in messages and emails |
| `ALLOWED_EMAIL_DOMAIN` | no | If set, only this domain may register, log in or be invited. Also the default `MANAGER_EMAIL_DOMAIN` (fallback `tneuralai.com`). |
| `RESEND_API_KEY`, `SENDER_EMAIL`, `APP_URL` | no | Invite emails and links |
| `CORS_ORIGINS` | no (`*`) | Allowed origins |

### 3.2 Frontend environment (`frontend/.env`)
`REACT_APP_BACKEND_URL` (API base is `${REACT_APP_BACKEND_URL}/api`), `WDS_SOCKET_PORT`, `ENABLE_HEALTH_CHECK`.

### 3.3 Running locally
See `SETUP.md`. In short: MongoDB (for example `mongo:7` in Docker), backend with Python 3.12 (`pip install -r requirements.txt`, then `uvicorn server:app --reload --port 8001`), frontend with Node 20 and Yarn 1.x (`yarn install && yarn start`). Create a manager with `python scripts/create_manager.py`. The first real training run downloads YOLOv8 base weights.

### 3.4 Storage layout (object store)
| Object | Path |
|---|---|
| Image | `{APP_NAME}/projects/{project_id}/{image_id}.{ext}` |
| Video frame | `{APP_NAME}/projects/{project_id}/{image_id}.jpg` |
| Video | `{APP_NAME}/projects/{project_id}/videos/{video_id}.{ext}` |
| Trained weights | `{APP_NAME}/projects/{project_id}/models/{model_id}.pt` |

At startup the backend calls the storage `init` endpoint and caches the returned `storage_key`, sent as `X-Storage-Key` on every later call.

### 3.5 Startup and shutdown
Startup initializes storage and creates indexes (§5.16); failures are logged but do not stop the app. Shutdown closes the Mongo client.

---

## 4. Authentication and Authorization

### 4.1 Mechanism
- Email and password; bcrypt hashing.
- JWT HS256, payload `{sub: user_id, exp: now + 7 days}`. No refresh tokens, server-side logout, password reset or email verification.
- The token is accepted as `Authorization: Bearer <token>` or as `?auth=<token>` (so `<img src>` can load `/api/files/...`).
- Missing or invalid tokens and unknown users get **401**.
- **Domain restriction.** If `ALLOWED_EMAIL_DOMAIN` is set, register and login return 403 for other domains; invites return 400.
- **Invite auto-activation.** Pending invites matching the user's email are activated on register, login and authenticated requests.
- **Manager login.** `POST /auth/manager-login` only accepts emails on `MANAGER_EMAIL_DOMAIN` whose user has `role: "manager"`. Managers are created with `backend/scripts/create_manager.py`, are not team members, and are excluded from people lists. `require_manager` guards all `/manager/*` routes.

### 4.2 Frontend auth handling
- `localStorage`: `vf_token` (JWT) and `vf_user` (`{id, email, name, role?}`).
- An axios request interceptor adds the Bearer header; a response interceptor clears both keys on 401 and redirects to `/auth` (unless already on `/` or `/auth*`).
- `fileUrl(storagePath)` returns `${API}/files/${storagePath}?auth=${token}`.
- `<Protected>` checks that a token exists; `<ManagerProtected>` additionally requires the manager role. The server enforces everything else.

### 4.3 Team roles (`team_members.role`)
| Role | Meaning |
|---|---|
| `owner` | Team creator; exactly one per team; cannot be changed or removed |
| `admin` | Manages projects, assigns work, uploads, deletes, trains and deploys |
| `member` | Contributor; annotates images assigned to them |
| `annotator` | Labels images assigned to them; sees only assigned projects once a project has assignments |
| `reviewer` | Approves or rejects submitted images. Cannot edit, upload, auto-label, train or delete. Sees only assigned projects once a project has assignments. |

Invites and role changes accept `admin | member | annotator | reviewer`. **Manager** is a separate global role on `users.role` (not a team role).

### 4.4 Project access check (`_project_access_check(pid, uid, roles?)`)
1. The project must exist.
2. If it has a `team_id`: the user must be an active member; if `roles` is given the member's role must be in it (else 403); annotators and reviewers must have their own `project_assignments` row once the project has any assignment.
3. Legacy projects with no `team_id`: the user must be the project's `user_id`.

Related helpers in `server.py`: `_require_not_reviewer` (access for anything that changes images), `_image_edit_check` (§7.11), `_has_other_reviewer`, `_role_in_project`.

### 4.5 Permission matrix
| Action | Allowed |
|---|---|
| Create a project in a team | owner, admin, member |
| Delete a project; delete a class; update settings | owner, admin |
| Assign or unassign users to a project; assign images (single or bulk) | owner, admin |
| Upload images or videos; run auto-label | any role except reviewer, with access |
| Annotate | The assignee while status is `assigned` or `rejected`; owner/admin also on unassigned images (claimed on save). Never reviewers; never `submitted` or `approved` images. |
| Submit an image | Only the person it is assigned to |
| Approve or reject | owner, admin, reviewer. Not your own work unless no other reviewer exists (solo workspace). Reject requires a note. |
| Delete an image | owner, admin; approved images cannot be deleted |
| Start real training | any role except reviewer; models are `purpose: "assist"` |
| Activate a model | owner/admin for any model; the trainer for their own assist model (not reviewers) |
| Cancel training; rename or delete a model | owner, admin |
| Deploy pipeline (all endpoints) | owner, admin |
| Team dashboard | owner, admin, reviewer |
| View team details | owner, admin, member (annotators and reviewers get 403) |
| Rename or delete a team; change a member's role | owner |
| Invite members | owner, admin |
| Remove a member | owner or admin, or the member themselves; owner never |
| Comments, exports, versions | any user with project access |
| Manager dashboard | global `manager` role only |
| Work log | any signed-in user, own rows only |

---

## 5. Data Model

MongoDB collections. In every collection `id` is a UUID string.

### 5.1 `users`
`id, email (lowercased), name, password (bcrypt), role ("manager" or absent), created_at`

### 5.2 `teams`
`id, name, owner_id, is_personal, created_at`. Each user gets a personal team ("{name}'s Workspace") that cannot be deleted.

### 5.3 `team_members`
- Active: `id, team_id, user_id, role, status: "active", joined_at`.
- Pending invite: `id, team_id, invited_email, role, status: "pending", invited_at`. Activation sets `user_id`, flips `status` and removes `invited_email`.

### 5.4 `projects`
`id, name, description, team_id, user_id, task_type (label only), classes [str], settings {confidence_threshold 0.05–0.95 (0.4), fallback_to_gemini (true), min_boxes_threshold ≥0 (1), show_confidence (true)}, last_retrain_nudge_count, created_at`. Class order defines class indices in export and training.

### 5.5 `project_assignments`
`id, project_id, user_id, assigned_by, created_at`: the access list for annotators and reviewers.

### 5.6 `images`
| Field | Notes |
|---|---|
| `id, project_id, user_id, filename, storage_path, content_type, size` | |
| `annotations` | `[Annotation]` (§5.7) |
| `annotated` | true when there is at least one annotation (a draft flag; "done" means approved) |
| `annotated_by, annotated_at` | Set on every annotation save; feeds the manager dashboard |
| `assigned_to` | user_id or null |
| `review_status` | `unassigned \| assigned \| submitted \| approved \| rejected` |
| `review_notes, reviewed_by, reviewed_at, submitted_at` | Review metadata |
| `priority, due_date, task_notes` | Task metadata |
| `from_video` | video_id, for extracted frames |
| `created_at` | |

`backend/scripts/backfill_review_status.py` fills `review_status` on older images and video frames that lack it.

### 5.7 Annotation (inside `images.annotations`)
`AnnotationBox` with `extra="allow"`.

| Field | Meaning |
|---|---|
| `type` | `bbox \| polygon \| polyline \| point \| ellipse` (missing = `bbox`) |
| `label` | Class label |
| `x, y` | bbox: top-left; ellipse and point: center |
| `w, h` | bbox size; ellipse `2rx, 2ry`; point 0 |
| `points` | `[[x, y], ...]` for polygon and polyline |
| `rx, ry` | ellipse radii |
| `rotation` | bbox rotation in radians, clockwise, about the center. **This is OBB.** |
| `confidence, source` | AI output: 0–1; `"model"` or `"gemini"` |

### 5.8 `videos`
`id, project_id, user_id, filename, storage_path, size, interval_frames, interval_seconds, status (queued|processing|completed|failed), extracted_count, processed_frames, total_frames, resolved_interval_frames, fps, error, completed_at, created_at`

### 5.9 `batch_jobs`
`id, project_id, user_id, type: "auto_label", status, total, processed, labeled, failed, source_counts, created_at, completed_at`

### 5.10 `versions`
`id, project_id, name, notes, image_count, snapshot_image_ids (≤2000), created_at`. A metadata snapshot only.

### 5.11 `models`
- **Simulated (legacy):** `id, project_id, version_id, model_arch, epochs, metrics[], final_mAP, final_loss, status: "deployed", endpoint, created_at`.
- **Real (`type: "real"`):** `id, project_id, user_id, model_arch, epochs, epochs_requested, epochs_auto_scaled, name, purpose ("assist"), status (queued|preparing|training|trained|failed|cancelled), is_active, training_image_count, progress, current_epoch, cancel_requested, error, weights_path, weights_size, final_mAP, final_loss, precision, recall, metrics_full, activated_at, completed_at, created_at`.
- **Pipeline models:** documents created by the deploy pipeline's `downloading_model` stage carry `source: "server"` (candidate models of a pipeline run). A model without `purpose` is treated as `production` if it has `source`, otherwise `assist`.

### 5.12 `comments`
`id, image_id, project_id, user_id, user_name, text, parent_id, resolved, resolved_by, resolved_at, created_at`. Threads are one level deep.

### 5.13 `notifications` and `activity_log`
- notifications: `id, user_id, kind, title, project_id, image_id, meta, read, created_at`. Kinds: `project_assigned, image_assigned, review_needed, review_completed, rejected, comment, retrain_nudge, training_ready` (plus pipeline notifications to reviewers after the test stage).
- activity_log: `id, project_id, team_id, user_id, user_name, action, meta, created_at`. Actions include `project_assigned, project_unassigned, bulk_assigned, image_assigned, image_unassigned, image_submitted, image_approved, image_rejected, comment_added, settings_updated, training_started, model_activated, model_deleted, training_cancelled, pipeline_run_created, pipeline_run_approved, pipeline_run_rejected, pipeline_rollback_started`.

### 5.14 `deployment_pipelines` (one per project)
`id, project_id, created_by, created_at, updated_at, remote_host, remote_port, remote_username, remote_base_model_path, remote_production_model_path, remote_data_yaml_path, remote_workdir, last_run_id, last_deployed_run_id, last_form`. Connection metadata only. **Never passwords or key material.**

### 5.15 `pipeline_runs`
`id, project_id, pipeline_id, run_type (bootstrap|merge|rollback), status, busy, stage_history [{stage, status, started_at, ...}], form (non-secret saved fields, see `FORM_KEYS`), approval {decision, decided_by, decided_at, note}, candidate_model {local_model_id, ...}, video_tests [{id, status, ...}], created_at, updated_at`.
- `status` is `draft`, then the name of the running stage, then `awaiting_approval`, `rejected`, and so on.
- A run is `bootstrap` if the pipeline has no completed run, otherwise `merge`. `rollback` runs are created by the rollback endpoint.
- `busy: true` is the per-pipeline concurrency lock.

### 5.16 `work_logs`
`id, user_id, date (YYYY-MM-DD), day, product, project_name, task, frames, verified_by, notes, source ("manual"|"import"), import_key (imports only; dedupe), created_at`

### 5.17 Indexes (created at startup)
images: (project_id, review_status, created_at desc), assigned_to, storage_path, (annotated_by, annotated_at desc); activity_log: (project_id, created_at desc); notifications: (user_id, created_at desc); team_members: (team_id, user_id), (invited_email, status); project_assignments: (project_id, user_id); comments: (image_id, created_at); models: (project_id, is_active).

---

## 6. API Reference

All paths are under `/api`. Unless marked **public**, a JWT is required.

### 6.1 System
| Endpoint | Notes |
|---|---|
| `GET /` (public) | `{message, version}` |
| `GET /system/versions` (public) | `{app, ultralytics, torch, cuda_available, supported_archs, brand, allowed_email_domain}` |

### 6.2 Auth
| Endpoint | Behavior |
|---|---|
| `POST /auth/register` `{email, password, name}` | Enforces domain; creates user and personal team; activates invites; returns `{token, user}` |
| `POST /auth/login` `{email, password}` | Returns `{token, user{id,email,name,role}}`; 400 "Invalid credentials" |
| `POST /auth/manager-login` `{email, password}` | Manager accounts on the manager domain only |
| `GET /auth/me` | `{id, email, name, role}` |

### 6.3 Teams
`GET /teams`, `POST /teams`, `GET /teams/{tid}`, `PATCH /teams/{tid}` (owner), `DELETE /teams/{tid}` (owner; not personal), `POST /teams/{tid}/invite` (owner, admin), `PATCH /teams/{tid}/members/{mid}` (owner), `DELETE /teams/{tid}/members/{mid}` (owner, admin, or self). Behavior as in §4.5; invites activate immediately for existing users, otherwise create a pending invite and send a Resend email.

### 6.4 Projects
`POST /projects`, `GET /projects?team_id=`, `GET /projects/{pid}`, `DELETE /projects/{pid}` (owner, admin), `POST /projects/{pid}/classes`, `POST /projects/{pid}/classes/bulk` (splits on commas and newlines; lowercases, trims, truncates to 40 chars, dedupes), `DELETE /projects/{pid}/classes/{label}` (owner, admin; annotations using the label are untouched), `PATCH /projects/{pid}/settings` (owner, admin).
- Project list items include `image_count` and `annotated_count`, where **`annotated_count` means approved by a reviewer**.

### 6.5 Images
| Endpoint | Rules |
|---|---|
| `POST /projects/{pid}/images` | multipart `file`; jpg/jpeg/png/webp/gif ≤ 10 MB; not reviewers; status `unassigned` |
| `GET /projects/{pid}/images?status=&assignee=` | `assignee` = `me` or a user_id. Up to 10000, newest first. **Annotations are omitted**; each item has `annotation_count`. |
| `GET /images/{img_id}` | Full document including annotations |
| `GET /files/{path}` | Streams image bytes; token via header or `?auth=`; cached `private, immutable` |
| `PUT /images/{img_id}/annotations` `{boxes}` | Replaces all annotations after `_image_edit_check` (§7.11). Sets `annotated_by/at`. An owner/admin saving an unassigned image claims it (`assigned_to` = self, status `assigned`). New labels are merged into `project.classes` (sorted). Returns `{ok, count, classes}`. |
| `DELETE /images/{img_id}` | owner, admin; 409 if approved |
| `POST /images/{img_id}/assign` `{user_id\|null}` | owner, admin; 409 if `submitted` or `approved`; assignee must be an active team member |
| `POST /images/{img_id}/submit` | Only the assignee; status must be `assigned` or `rejected` (409 otherwise); needs ≥1 annotation (400). Clears previous review fields. Notifies owners, admins and reviewers. |
| `POST /images/{img_id}/review` `{decision, notes?}` | owner, admin, reviewer. 409 unless `submitted`. A reject requires notes. 403 if reviewing your own work while another reviewer exists. Notifies the assignee. Approve runs the retrain-nudge check. |
| `PATCH /images/{img_id}/task` | Updates priority, due date, notes |

### 6.6 Videos
`POST /projects/{pid}/videos` (multipart `file`, `interval_frames`=30, `interval_seconds`=0; mp4/mov/webm/avi/mkv ≤ 200 MB), `GET /projects/{pid}/videos`, `GET /videos/{vid}`, `DELETE /videos/{vid}` (keeps extracted frames).

### 6.7 Auto-label and batch jobs
`POST /images/{img_id}/auto-label` (returns `{boxes, count, source}`, saves nothing; blocked for reviewers), `POST /projects/{pid}/batch-auto-label` `{only_unlabeled}` (up to 5000 images; skips images that are `submitted` or `approved`), `GET /batch-jobs/{jid}`, `GET /projects/{pid}/batch-jobs`.

### 6.8 Assignments and tasks
`POST /projects/{pid}/assign` `{user_ids}`, `DELETE /projects/{pid}/assign/{user_id}`, `GET /projects/{pid}/assignments`, `POST /projects/{pid}/images/bulk-assign` `{image_ids, user_id?, priority?, due_date?, notes?}` (owner, admin; does not reset images under review or approved), `PATCH /images/{img_id}/task`.

### 6.9 Dashboards
`GET /projects/{pid}/dashboard` (counts per status, completion %, member performance) and `GET /teams/{tid}/dashboard` (owner, admin, reviewer). Counts use review status; "annotated" means approved.

### 6.10 Versions and export
`POST /projects/{pid}/versions`, `GET /projects/{pid}/versions`, `GET /projects/{pid}/export?format=yolo|yolo_obb|coco|voc` (ZIP of current **approved** annotated images, up to 2000).

### 6.11 Training and models
| Endpoint | Rules |
|---|---|
| `GET /projects/{pid}/training-plan` | Eligible = approved images with annotations; `min_required: 30`; recommended epochs; CPU time estimate |
| `POST /projects/{pid}/train-real?epochs=0&model_arch=yolov8n` | Not reviewers. `epochs` 0–200 (0 = auto). 400 if fewer than 30 approved images. Creates a `purpose: "assist"` model. |
| `GET /models/{mid}` | Polled for progress |
| `PATCH /models/{mid}` `{name}` | owner, admin; rename (≤100 chars) |
| `DELETE /models/{mid}` | owner, admin; also evicts the in-memory YOLO cache entry |
| `POST /models/{mid}/activate` | See §4.5; deactivates all other models in the project |
| `POST /models/{mid}/cancel` | owner, admin; queued/preparing/training only |
| `POST /projects/{pid}/models/use-gemini` | owner, admin; deactivates the active model so auto-label uses Gemini |
| `POST /projects/{pid}/train` | Simulated legacy training (§7.6) |
| `GET /projects/{pid}/models` | All models, newest first |
| `GET /system/versions` | See §6.1 |

### 6.12 Active learning
`GET /projects/{pid}/active-learning-queue?limit=20`.

### 6.13 Collaboration
`POST /images/{img_id}/comments`, `POST /comments/{cid}/replies`, `POST /comments/{cid}/resolve`, `GET /images/{img_id}/comments`, `GET /projects/{pid}/activity?limit=200`, `GET /notifications`, `POST /notifications/{nid}/read`, `POST /notifications/mark-all-read`.

### 6.14 Deploy pipeline (owner/admin only; `routers/deployment.py`)
| Endpoint | Behavior |
|---|---|
| `POST /projects/{pid}/pipeline/runs` | Creates a `draft` run (bootstrap or merge) and pre-fills `form` from the pipeline's `last_form`; creates the `deployment_pipelines` doc on first use |
| `GET /projects/{pid}/pipeline/runs` | Run history |
| `GET /pipeline/runs/{rid}` | One run (polled for stage progress) |
| `DELETE /pipeline/runs/{rid}` | Removes a run |
| `PUT /pipeline/runs/{rid}/form` | Saves non-secret form fields (`FORM_KEYS`) |
| `POST /pipeline/runs/{rid}/inspect_remote` | Read-only SSH probe of the workdir: finds `data.yaml` datasets, `.pt` models, and whether a venv exists |
| `POST /pipeline/runs/{rid}/stage/{stage}` | Runs one stage; `stage` ∈ `uploading_data, class_check, training_remote, downloading_model, testing, deploying`. Body is the stage's request model (§7.14) and carries SSH credentials. 409 if any run of the pipeline is `busy`. |
| `POST /pipeline/runs/{rid}/test-video` | Upload a video to test the candidate model |
| `GET /pipeline/runs/{rid}/test-video/{test_id}` | Result / annotated video, with HTTP range support |
| `POST /pipeline/runs/{rid}/approve` · `/reject` `{note?}` | Only while `status == "awaiting_approval"` (409 otherwise); makes no SSH calls |
| `POST /projects/{pid}/pipeline/rollback` | Creates a `rollback` run that restores the previous production model on the target host; busy-locked |

Preconditions enforced in `run_stage`: `deploying` requires an approved run and a server-trained model of this project; `testing` requires at least one succeeded video test.

### 6.15 Manager (global `manager` role; `routers/manager.py`)
All accept `from` and `to` (YYYY-MM-DD, default last 30 days, max 2 years).
`GET /manager/overview` (KPIs, daily series, top people and projects, review pipeline counts), `GET /manager/people`, `GET /manager/people/{uid}`, `GET /manager/projects`, `GET /manager/projects/{pid}`, `GET /manager/work-logs`, `GET /manager/export.csv`. Accounts whose email starts with `test_` are excluded everywhere.

### 6.16 Work logs (`routers/work_logs.py`; own rows only)
`POST /work-logs`, `GET /work-logs` (up to 2000), `PATCH /work-logs/{id}`, `DELETE /work-logs/{id}`, `POST /work-logs/import` (multipart `.xlsx`/`.xlsm`/`.csv`, ≤ 5 MB; returns `{imported, skipped_duplicates, errors}`).

---

## 7. Core Subsystems

### 7.1 Image upload and video frame extraction
- Images: one file per request, extension and 10 MB check, bytes PUT to object storage, document inserted as `unassigned`.
- Videos: stored whole, then a background thread opens them with OpenCV, keeps every Nth frame (N = `round(interval_seconds × fps)` or `interval_frames`), JPEG-encodes (quality 85), uploads and inserts frames with `from_video`. Progress is written every 5 frames; ends `completed` or `failed`.

### 7.2 Annotation save semantics
`PUT /images/{id}/annotations` replaces the whole list. The frontend auto-saves 2 s after the last change and on Ctrl/Cmd+S. New labels are added to `project.classes`, which is then sorted alphabetically (can shift class indices, §12).

### 7.3 Auto-label router
1. Take the project's active model (`is_active` and `trained`). If it has weights, run YOLO at `confidence_threshold`; box labels are `project.classes[idx]`.
2. If the box count ≥ `min_boxes_threshold`, return `(boxes, "model")`. Else if `fallback_to_gemini` is false, return `(boxes, "model_no_fallback")`.
3. Otherwise call Gemini 2.5 Flash for a JSON array of `{label, x, y, w, h}`; labels are lowercased and truncated to 40 chars and coordinates clamped. Return `"gemini"`, or `"model+gemini"` if the model also produced boxes.
4. Local inference errors fall back to Gemini. Loaded models are cached in `_yolo_cache` and never evicted except on model delete.

### 7.4 Batch auto-label job
Selects up to 5000 images (only unlabeled if requested, skipping locked images), processes them in a background thread, **appends** boxes to existing annotations and merges labels into the classes. Counters and `source_counts` are updated per image; the frontend polls every 2.5 s.

### 7.5 Real YOLOv8 training
- **Eligibility:** at least 30 **approved** images with annotations.
- **Epochs:** requested value clamped to 1–200; auto: <50 images → 10, <150 → 20, <500 → 30, else 50.
- **Thread (`_train_yolo_sync`):** build a temp dataset (shuffle, 80/20 train/val, RGB JPEG q90, YOLO label files with the class index from `project.classes`; polygons and ellipses become their bounding box, rotation is ignored, points are skipped), write `data.yaml`, run `YOLO(model_arch.pt).train(imgsz=640, batch=8, device="cpu")`.
- **Progress and cancel:** epoch callbacks write `current_epoch` and `progress`; callbacks check `cancel_requested` and raise `KeyboardInterrupt`.
- **Result:** upload `best.pt` (else `last.pt`), store mAP50 / mAP50-95 / precision / recall, set `trained` and reset `last_retrain_nudge_count`. Errors set `failed` with a message.
- **Lifecycle:** `queued → preparing → training → trained`, or `failed` / `cancelled`.
- **Assist models.** Models trained here only drive auto-labeling. Production deployment goes through the pipeline (§7.14). There is no public inference endpoint.

### 7.6 Simulated training (legacy)
`POST /projects/{pid}/train` generates random plausible curves and stores a `deployed` model with an `endpoint` that does not exist. It has no effect on auto-label.

### 7.7 Active-learning queue
Candidates are up to 500 images with status unassigned/assigned/rejected. With an active model, the stored annotations of the top `limit × 3` are scored: no annotations → 0.85 (`no_detections`); else `0.5 × (1 − avg_confidence) + 0.5 × fraction of boxes within ±0.15 of the threshold`. Without a model: unlabeled → 1.0, labeled → 0.5. Sorted by score descending.

### 7.8 Retrain nudge
On every approval: count images approved since the latest real trained model (or all approved if none). If `count ≥ 30` and `count ≥ last_retrain_nudge_count + 30`, notify team owners and admins (`retrain_nudge`, or `training_ready` when no model exists yet) and store the count. A successful training run resets it.

### 7.9 Dataset versions
A version is a list of image IDs plus a name and notes. Export and real training ignore versions and use live approved data. Only simulated training takes a `version_id`. There is no augmentation or saved split.

### 7.10 Export formats
A ZIP is built in memory under `dataset/` (always including `classes.txt`) from up to 2000 approved images.

| Format | Contents |
|---|---|
| `yolo` | `data.yaml`, `images/`, `labels/`; polygons as YOLO-seg lines, other shapes as `cls cx cy w h` (points become a 0.005 box, ellipses their bounding box) |
| `yolo_obb` | As `yolo`, but rotated boxes are written as 4 corner points |
| `coco` | `annotations.json` (images, categories from 0, annotations with pixel bbox and polygon segmentation) plus `images/` |
| `voc` | One XML per image with `<object><name><bndbox>`; polygons add a non-standard `<polygon>` block |

Unknown labels are written as index 0.

### 7.11 Annotate → review → approve workflow (server-enforced)
```
            assign (owner/admin)         submit (assignee, ≥1 annotation)        review
unassigned ───────────────▶ assigned ───────────────────────────▶ submitted ─────────▶ approved
     ▲                         ▲   │                                    └──────────▶ rejected (note required)
     └─ unassign ──────────────┘   └───────────── annotator fixes and resubmits ◄────────┘
```
`_image_edit_check` is the single gate for changing annotations:
- Reviewers: never (403).
- `submitted` or `approved` images: never (409).
- Owner/admin: unassigned images (claimed on save) or images assigned to themselves; an image assigned to someone else is 403.
- Annotator/member: only images assigned to them, and only while `assigned` or `rejected`.

Other rules: an image is "annotated" (counts, dashboards, versions, export, training, deploy) only once **approved**; assign/bulk-assign/batch-auto-label never touch locked images; a reviewer cannot approve their own work if another reviewer exists; resubmitting clears the previous verdict. `backend/scripts/backfill_review_status.py` migrates older data.

### 7.12 Comments, notifications, activity
Comments are threaded one level and can be resolved. Notifications are in-app only (the only email is the invite) and are polled every 15 s. `_log_activity` never fails the request. There is no WebSocket or SSE channel.

### 7.13 Background job model
Frame extraction, batch auto-label, training and pipeline stages run inside the API process with `BackgroundTasks` + `asyncio.to_thread` and a sync PyMongo client; the frontend polls a status document. There is no queue, retry or restart recovery: a restart leaves jobs stuck in `queued`/`processing`/`training` (pipeline runs stay `busy`).

### 7.14 Deploy pipeline
A guided workflow to train on a remote host and promote a model to a deploy target. Route `/projects/:pid/deploy` (`DeployPipeline.jsx`); `ModelDeploy.jsx` is the older model/training page.

**Security model.** SSH credentials (`pem_key`, `password`) arrive in each stage request (`schemas/deployment.py`), live only for the call, and are never written to Mongo, logs, activity entries or responses. Only non-secret form fields (`FORM_KEYS`) are saved, on the run and mirrored to `deployment_pipelines.last_form` to prefill the next run. Run actions are audited through `_log_activity`.

**Concurrency.** `busy` on a run is a lock for the whole pipeline: starting a stage or rollback while any run of the same pipeline is busy returns 409.

**Run types.** `bootstrap` (first completed run) vs `merge` (later runs, which can merge into an existing production dataset) vs `rollback`.

**Stages** (each updates `stage_history` and the run `status`):
| Stage | What it does |
|---|---|
| `uploading_data` | Builds a split dataset from approved images (default 70/20/10 train/valid/test) and uploads it over SFTP. `dataset_mode`: `new` (fresh at `{workdir}/{run_id}/dataset`), `merge` (add into an existing remote dataset) or `reuse` (use it as-is). Background worker with progress. |
| `class_check` | Compares local project classes with the remote `data.yaml` names (`pipeline_logic.diff_classes`) |
| `training_remote` | Renders a train+eval script (`render_train_eval_script`) and runs it remotely, streaming progress by scraping stdout. Starting model: `start_from_path`, else `yolo_model` (downloaded on the server), else the legacy base/production paths. `env_mode`: `system`, `existing` venv, or `create` a venv. Hyperparameters are sanitized (`sanitize_hyperparams`) because they end up in a generated script. |
| `downloading_model` | SFTP-downloads weights and `metrics.json`, registers a candidate model (`source: "server"`) |
| `testing` | Runs local inference/evaluation against the candidate and the active model; needs at least one succeeded video test; notifies reviewers; the run moves to `awaiting_approval` |
| `deploying` | Needs an approved run and a chosen server-trained model; uploads it to the deploy host (`deploy_host`, `deploy_username`, `deploy_model_path`), rotating the previous production file so rollback is possible |

`decide_deploy(baseline_map50, retrained_map50)` in `pipeline_logic.py` carries over the legacy pipeline's promotion rule. The human approve/reject step gates `deploying`. **Rollback** (`POST /projects/{pid}/pipeline/rollback`) restores the previous production model on the target.

**Remote helpers** (`ssh_helper.py`): `connect`, `exec_command(_streaming)`, `upload/download_file`, `upload/download_dir`, `remote_exists/remove/rename`, `resolve_remote_path`, `test_connection`.

### 7.15 Manager analytics
- **Two data sources, merged into one record list.**
  1. **App tracking.** Images with `annotated_by` and `annotated_at` (set on every annotation save), grouped by person, project and day.
  2. **Work log.** Rows people enter or import.
- **No double counting.** If the app tracked a (person, day, project) combination, the app number wins and the logged row is dropped. Project matching for logs is by name (case-insensitive).
- **Review stats** per person: approved / rejected within the range by `reviewed_at`, submitted by `submitted_at`; the owner is `annotated_by` (else `assigned_to`). Approval rate = approved / (approved + rejected).
- **Exclusions.** Managers and users whose email starts with `test_` are excluded.
- **Export.** `/manager/export.csv` downloads the merged records.

### 7.16 Work-log import
Accepts `.xlsx`, `.xlsm` or `.csv`. The header row is the first row containing a "Date" cell. Recognized headers (case and whitespace insensitive): Date, Day, Product, Project, Task, "Number of Frames Annotated Today" (or Frames), Verified By, Notes. Dates accept ISO and day-first formats (`18/9/2026`). Rows without a project are skipped. Re-importing is safe: each row's `import_key` is a hash of user, date, project, task, frames, verified-by and its occurrence number within the file.

---

## 8. Frontend Specification

### 8.1 Routes (`src/App.js`)
| Path | Page | Guard |
|---|---|---|
| `/` | Landing | none |
| `/auth` (`?mode=register`) | Auth | none |
| `/dashboard` | Dashboard | Protected |
| `/work-log` | MyWorkLog | Protected |
| `/teams`, `/teams/:tid`, `/teams/:tid/dashboard` | Teams, TeamDetail, TeamDashboard | Protected |
| `/projects/:pid` | ProjectDetail | Protected |
| `/projects/:pid/activity` | Activity | Protected |
| `/projects/:pid/annotate/:imgId` | Annotator | Protected |
| `/projects/:pid/versions` | Versions | Protected |
| `/projects/:pid/deploy` | DeployPipeline (the older ModelDeploy page is still in the tree) | Protected |
| `/manager` (index) | ManagerOverview | ManagerProtected |
| `/manager/people`, `/manager/people/:uid` | ManagerPeople, ManagerPersonDetail | ManagerProtected |
| `/manager/projects`, `/manager/projects/:pid` | ManagerProjects, ManagerProjectDetail | ManagerProtected |
| `/manager/work-log` | ManagerWorkLog | ManagerProtected |

No 404 route. Role checks happen inside pages and on the server.

### 8.2 Shared components
- **Navbar** (authenticated pages): logo, optional breadcrumbs, Projects / Teams links, NotificationsBell, user name, Exit.
- **NotificationsBell:** polls every 15 s; unread badge; click marks read and navigates to the image, project or dashboard.

### 8.3 Auth, Landing, Dashboard
- **Landing:** public marketing page, no API calls.
- **Auth:** login/register toggle; shows the allowed-domain hint from `GET /system/versions`; manager sign-in uses `/auth/manager-login`.
- **Dashboard:** project cards (counts show approved as "labeled"); New Project dialog (always the personal team); delete with confirmation.

### 8.4 ProjectDetail
Header actions: Assign Project, Versions, Deploy, Settings (managers only: owner/admin). Stats, class chips with the Manage Classes dialog, a train nudge banner at ≥30 approved images. Tabs:
1. **Images:** status filter, Bulk Assign (owner/admin), select-all, Activity link, Batch Auto-Label with progress, image grid with review badges. Annotators see their assigned work.
2. **Upload:** drag-and-drop, one file per request (hidden for reviewers).
3. **Video:** every N frames or N seconds; list with progress.
4. **AL Queue:** ranked list with uncertainty bars.

### 8.5 Annotator (`/projects/:pid/annotate/:imgId`)
Three panes: left sidebar (classes, add class, shortcut legend), centre (toolbar and canvas), right sidebar (review panel, annotation list, comments).

**Tools**
| Tool | Key | Interaction |
|---|---|---|
| Box | B | Drag; minimum size 0.005 |
| Polygon | P | Click vertices; double-click or Enter to finish (≥3) |
| Line | L | Click vertices; double-click or Enter (≥2) |
| Point | O | Single click |
| Ellipse | E | Drag from the centre |

**Editing.** Only boxes can be selected: move, 8 resize handles, and a rotate handle (OBB). Other shapes can be relabeled or deleted from the list.

**Zoom and pan.** 1×–8×; Ctrl/Cmd+wheel, `+`/`-`/`0`; hold Space and drag to pan.

**Shortcuts.** Ctrl/Cmd+S save, Ctrl/Cmd+Z undo (last annotation only), Esc cancel drawing, Enter finish, B P L O E tools.

**Saving.** Auto-save 2 s after the last change; status line "Saving…" / "· Unsaved" / "Saved · HH:MM"; `beforeunload` warning.

**Workflow enforcement in the UI** (mirrors §7.11):
- A derived `lockReason` (from `review_status` and the user's role) puts the canvas in read-only mode: reviewers, submitted and approved images, and images assigned to someone else.
- **Review panel:** status badge and reviewer notes; assignee dropdown for owners and admins; **Submit for Review** (`POST /images/:id/submit`) shown to the assignee when status is `assigned` or `rejected`; **Approve / Reject** (`POST /images/:id/review {decision, notes}`) shown to owner, admin or reviewer on `submitted` images; Reject needs a note.
- Saving as owner/admin on an unassigned image assigns it to them.

**AI assist.** The Auto-Label button appends the returned boxes; chips show confidence % (unless `show_confidence` is off) and a source tag (·M model, ·G Gemini).

**Comments.** One-level threads with Reply and Resolve; refetched after each action.

### 8.6 Versions
New Version dialog (name, notes); export panel (YOLO, YOLO OBB, COCO, VOC) downloading the current approved data; version history with a Train Model link.

### 8.7 DeployPipeline (`/projects/:pid/deploy`)
A step-by-step wizard over a pipeline run: connection form (host, port, user, key or password; credentials are entered per use and never stored), dataset mode and splits, class check, remote workdir/venv inspection, remote training with live log and epoch progress, model download and registration, video test (upload and view result), human Approve / Reject, Deploy to a separate target host, and Rollback. Forms are saved per run (non-secret fields) and a model dropdown picks which server-trained model to upload. The page opens on every visit with its own host fields and keeps a run history.

### 8.8 ModelDeploy (older page)
Real Training tab (arch selector, epochs, Train, progress card polling every 5 s, Set Active, Cancel, trained-models list) and the Simulated tab (version, arch, epochs, recharts line chart, non-existent inference endpoint panel).

### 8.9 Teams, TeamDetail, TeamDashboard, Activity
Teams cards with role badges; TeamDetail with invite (owner, admin), delete team (owner), member list and an owner-only role menu; TeamDashboard with stat tiles, per-project bar chart, status pie chart and a team performance table; Activity as an audit list.

### 8.10 MyWorkLog (`/work-log`)
Per-user table of daily rows (Date, Project, Task, Frames, Verified By, Notes) with add, edit, delete, and import from `.xlsx` / `.csv` (result summary of imported / skipped / errors).

### 8.11 Manager dashboard (`/manager/*`)
Layout with a date-range filter and CSV export. Pages: **Overview** (KPIs: frames, active people and projects, approval rate, tracked vs logged frames; daily chart; top people and projects; review pipeline), **People** and **PersonDetail**, **Projects** and **ProjectDetail**, **WorkLog** (all logged rows). Read-only.

### 8.12 Polling summary
| Where | Interval | Condition |
|---|---|---|
| NotificationsBell | 15 s | Always when signed in |
| ProjectDetail (videos) | 3 s | While any video is queued/processing |
| ProjectDetail (batch job) | 2.5 s | Until the job completes |
| ModelDeploy (training) | 5 s | While the run is active |
| DeployPipeline (run) | polled | While a stage is running |
| Annotator auto-save | 2 s debounce | After each change |

### 8.13 Other frontend notes
`public/index.html` loads an Emergent platform script and PostHog analytics (session recording on). Interactive elements carry kebab-case `data-testid` attributes (`constants/testIds`).

---

## 9. Design System

Source: `design_guidelines.json`, implemented in `src/index.css` and `tailwind.config.js`.
- **Theme:** dark only. Fonts: **Outfit** (headings) and **JetBrains Mono** (body, metrics).
- **Palette:** background `#050505`, foreground `#FAFAFA`, surface `#121212` (hover `#1C1C1C`), border `#27272A`, muted text `#A1A1AA`, primary cyan `#06B6D4` (black text on it), magenta `#D946EF`, yellow `#EAB308`, green `#22C55E`, destructive `#EF4444`.
- **Rules:** no gradients; 1 px borders; minimal shadows; `rounded-sm` / `rounded-none`; glass blur only on the navbar; avoid `transition-all`; every interactive element gets a `data-testid`.
- **Utilities:** `.grid-bg`, `.grid-bg-fade`, `.panel`, `.panel-hover`, `.cursor-blink`, `.fade-in-up`.
- **Review status colors:** unassigned `#71717A`, assigned `#06B6D4`, submitted `#EAB308`, approved `#22C55E`, rejected `#EF4444`.

---

## 10. End-to-End Workflows

### 10.1 Onboarding
Register on the allowed domain → personal workspace created → New Project → add classes → upload images or video.

### 10.2 Team setup
New Team → Invite Member with a role (existing users are added immediately, others get an email and activate on sign-up) → Assign Project to annotators and reviewers.

### 10.3 Annotation and review cycle
1. An owner/admin bulk-assigns images; the assignee is notified.
2. The annotator draws (auto-saved) and clicks **Submit for Review**; the image locks and reviewers are notified.
3. A reviewer approves, or rejects with a required note. A rejected image returns to the annotator, who fixes and resubmits.
4. Approved images count toward progress, datasets, training, exports and deploys. Every 30 new approvals nudge owners/admins to retrain.

### 10.4 AI-assisted labeling
Per-image Auto-Label in the annotator, or Batch Auto-Label from the Images tab. Source order: active model, then Gemini per project settings. Results are edited and then submitted like any other work.

### 10.5 Train → activate → auto-label loop
≥30 approved images → Train (assist model) → review mAP / precision / recall → Set Active → auto-label uses it → AL queue prioritises uncertain images → retrain on nudge.

### 10.6 Production deploy
Owner/admin opens Deploy → New run → upload data → class check → remote training → download model → video test → testing → Approve → Deploy → Rollback if needed.

### 10.7 Manager reporting
Manager signs in on the manager login → picks a date range → reviews Overview / People / Projects → exports CSV. Team members keep the daily work log in `/work-log` (manual rows or spreadsheet import); app-tracked frames override logged rows for the same person, day and project.

---

## 11. Testing

- Integration tests run over live HTTP against a running backend (`REACT_APP_BACKEND_URL`); some also write directly to Mongo. They live in `tests/*.py` (smoke suites) and `backend/tests/*.py` (pytest). `backend/pytest.ini` uses xdist (`-n 2 --dist loadscope`).
- Test accounts come from `TEST_DEMO_EMAIL` / `TEST_DEMO_PASSWORD`; credentials are in `memory/test_credentials.md` (git-ignored).
- Test users are named with a `test_` email prefix so the manager dashboard excludes them; remove leftovers with `backend/scripts/cleanup_test_data.py`. Run tests only against a safe database (see `SETUP.md`).
- Past reports are in `test_reports/`. No frontend unit tests.

---

## 12. Known Limitations and Technical Debt

**Backend**
1. **Class-index drift.** Saving annotations re-sorts `classes` alphabetically while adding a class appends, so class indices in export and training can change. Export maps unknown labels to 0; training skips them.
2. Deleting a class leaves annotations that use it.
3. The retrain nudge also tries `project.owner_id`, a field projects do not have.
4. Simulated models advertise `/api/inference/{id}`, which does not exist.
5. `GET /teams/{tid}` returns 403 for annotators and reviewers.
6. `model_arch` on `train-real` is not validated.
7. The pipeline `run_type` derivation is a stub (`bootstrap` if no completed run, else `merge`; there is a TODO in `create_pipeline_run`).

**Data lifecycle.** Deleting a project or team does not delete videos, batch jobs, comments, notifications, activity, assignments or any storage objects.

**Performance and robustness.** Storage calls use synchronous `requests` inside async handlers; N+1 count queries; lists capped (200–10000) with no pagination; background jobs have no queue, retries or restart recovery; no limit on concurrent training runs (the deploy pipeline does have its `busy` lock); training is CPU only; export is built in memory.

**Security.** The JWT appears in image URLs (`?auth=`); no rate limiting, password policy beyond the UI's 6-character minimum, password reset, email verification or token revocation; CORS defaults to `*`. SSH credentials are transmitted per request, so the backend must be served over HTTPS.

**Frontend.** Annotator coordinates may be off when zoomed or panned; middle-mouse pan cannot work; undo removes only the last shape and there is no redo; only boxes are editable; Prev/Next within the auto-save window can lose edits; the Dashboard has no team picker; batch auto-label polling never stops on a failed job; training progress is not restored after reload; unused dependencies (react-query and others); many `window.confirm` / `window.prompt` dialogs.

**Naming.** Legacy VisionForge names remain (`vf_` keys, `APP_NAME`, API title).

**Missing features.** No public inference API or weight download; no dataset import; no augmentation or saved train/val/test splits; no per-version export; `task_type` has no effect; no real-time collaboration.

---

## 13. Backlog

- Real job queue with restart recovery and a GPU option; train from a chosen version with a saved split.
- Stable class order (append-only or explicit IDs) and cleanup on delete, including storage objects.
- Annotator: redo history, vertex editing for polygons and lines, move/resize for ellipses and points, Delete and arrow-key shortcuts, zoom coordinate fix, keypoints, classification tagging.
- Dataset import (YOLO, COCO, VOC) and per-version export.
- Public inference API with keys, weight download / ONNX export.
- Pagination, async storage client, WebSocket/SSE for notifications and job progress.
- Accounts: password reset, email verification, refresh tokens, rate limiting.
- Deploy pipeline: remember per-project connection profiles without storing secrets, scheduled retrain runs.

---

## 14. Guide for Requesting Modifications

| Feature | Backend | Frontend |
|---|---|---|
| Auth, manager login | `routers/auth.py`; `get_current_user`, `require_manager` in `server.py` | `Auth.jsx`, `lib/api.js` |
| Teams, invites | `routers/teams.py` | `Teams.jsx`, `TeamDetail.jsx` |
| Projects, classes, settings | `routers/projects.py`; `_project_out`, `_project_access_check` | `Dashboard.jsx`, `ProjectDetail.jsx` |
| Images, annotations, review | `routers/images.py`; `_image_edit_check`, `LOCKED_STATUSES`, `_require_not_reviewer` in `server.py` | `Annotator.jsx`, `ProjectDetail.jsx` |
| Videos, auto-label, batch | `server.py` (`_extract_frames_sync`, `_auto_label_router`, `_batch_autolabel_job`) | `Annotator.jsx`, `ProjectDetail.jsx` |
| Training, models, AL | `routers/training.py`, `server.py` (`_train_yolo_sync`), `routers/active_learning.py` | `ModelDeploy.jsx`, `ProjectDetail.jsx` |
| Versions, export | `server.py` | `Versions.jsx` |
| Deploy pipeline | `routers/deployment.py`, `deployment_worker.py`, `pipeline_logic.py`, `ssh_helper.py`, `schemas/deployment.py` | `DeployPipeline.jsx` |
| Manager dashboard | `routers/manager.py` | `pages/manager/*` |
| Work log | `routers/work_logs.py` | `MyWorkLog.jsx`, `manager/WorkLog` |
| Comments, notifications, activity | `routers/collaboration.py`; `_notify`, `_log_activity` | `NotificationsBell.jsx`, `Activity.jsx` |
| Styling | none | `index.css`, `tailwind.config.js`, `design_guidelines.json` |

**Conventions to keep**
- New endpoints go in the relevant `routers/*.py` inside `register(s)`, reading shared state from `s` (`s.db`, `s.get_current_user`, `s._project_access_check`). Everything mounts under `/api`.
- Documents use a UUID `id`, project out `_id` with `{"_id": 0}`, and store ISO timestamps from `datetime.now(timezone.utc).isoformat()`.
- Use `_project_access_check(pid, uid, roles=[...])` for project routes and `_require_team_role` for team routes. Anything that changes images must go through `_require_not_reviewer` or `_image_edit_check` so the review rules stay enforced on the server. Log with `_log_activity` and notify with `_notify`.
- Never persist, log or return SSH secrets (see `schemas/deployment.py`).
- Annotations: normalized 0–1; bbox `x, y` is the top-left; ellipse and point `x, y` is the centre; `rotation` in radians.
- Frontend: call the API through `lib/api.js`, load images with `fileUrl()`, use shadcn components, lucide icons and sonner toasts, and put a `data-testid` on every interactive element.
- Background work: follow the `BackgroundTasks` + `asyncio.to_thread` + sync PyMongo + polled status document pattern, unless introducing a real queue.
