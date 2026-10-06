# ThinkVision: Product and Technical Specification

> **Purpose of this document.** This is a complete description of the ThinkVision application: what it does, how every feature works, the data model, the full API, the UI, and the end-to-end workflows. It is written so that someone who **cannot see the source code** (for example an AI assistant asked to design or implement changes) can still reason accurately about the system.
>
> **Scope.** The main application only: the `backend/` (FastAPI) and `frontend/` (React) folders. The separate `cicd-pipeline-main/` folder is **not** covered.
>
> **Conventions used below**
> - All API paths are prefixed with `/api`.
> - "Access" means the caller has passed the project access check (see §4.4).
> - Coordinates are always **normalized to 0–1** relative to image width and height.
> - Every document ID is a UUID4 string stored in the field `id`. MongoDB's `_id` is never exposed.
> - All timestamps are ISO-8601 UTC strings.

---

## Table of Contents
1. [Overview](#1-overview)
2. [Tech Stack & Architecture](#2-tech-stack--architecture)
3. [Setup & Configuration](#3-setup--configuration)
4. [Authentication & Authorization](#4-authentication--authorization)
5. [Data Model](#5-data-model)
6. [API Reference](#6-api-reference)
7. [Core Subsystems: How They Work](#7-core-subsystems--how-they-work)
8. [Frontend Specification](#8-frontend-specification)
9. [Design System](#9-design-system)
10. [End-to-End User Workflows](#10-end-to-end-user-workflows)
11. [Testing](#11-testing)
12. [Known Limitations, Bugs & Technical Debt](#12-known-limitations-bugs--technical-debt)
13. [Backlog & Future Ideas](#13-backlog--future-ideas)
14. [Guide for Requesting Modifications](#14-guide-for-requesting-modifications)

---

## 1. Overview

**ThinkVision** is a web platform for building computer-vision datasets and models, similar to Roboflow. It was originally named **VisionForge**. Traces of the old name remain in the code: the `vf_` localStorage keys, the `APP_NAME=visionforge` storage prefix, and the FastAPI title "VisionForge API". It was built on the Emergent platform and uses Emergent-hosted services for object storage and LLM access.

**Target users:** ML engineers building CV datasets, annotation teams (annotators and reviewers), and solo developers prototyping models.

**What a user can do:**
- **Organize work.** Sign up, get a personal workspace, create teams, and invite teammates with roles (owner, admin, member, reviewer, annotator).
- **Collect data.**
  - Create projects typed as object detection, classification or segmentation. The type is only a label.
  - Upload images, or upload videos and extract frames from them.
- **Annotate.**
  - Label images in a browser annotator with bounding boxes (including rotated boxes, or OBB), polygons, polylines, points and ellipses.
  - Zoom and pan, auto-save, and comment on images.
  - **AI auto-label:** use the project's own trained YOLO model if one is active, otherwise Google Gemini 2.5 Flash. This works per image or as a batch background job.
- **Review.** Assign images to people and run a review workflow: assigned → submitted → approved or rejected.
- **Manage the dataset.**
  - An **active-learning queue** ranks the images most worth labeling next.
  - **Dataset versions** save a snapshot of which images were annotated.
  - **Export** as YOLO, YOLO-seg, YOLO OBB, COCO or Pascal VOC ZIP archives, with images included.
- **Train and deploy.**
  - **Train real YOLOv8 models** (n, s, m or l) on the project's labeled images, on CPU, in the background, with live progress and cancellation.
  - **Activate** a trained model so it powers auto-labeling.
  - A **simulated training** mode (legacy) produces synthetic training curves.
  - A **retrain nudge** notifies admins when 30 or more newly approved images have accumulated.
- **Track progress.** Team and project dashboards, an activity log, and in-app notifications.

---

## 2. Tech Stack & Architecture

### 2.1 Backend
| Concern | Technology |
|---|---|
| Web framework | FastAPI 0.110 (Starlette 0.37), Uvicorn, Pydantic v2 |
| Database | MongoDB via **Motor** (async). Background threads use a separate synchronous **PyMongo** client. |
| Auth | PyJWT (HS256), bcrypt |
| ML | **Ultralytics 8.x (YOLOv8)**, PyTorch (CPU build), OpenCV (video frames), Pillow, NumPy |
| LLM | `emergentintegrations` `LlmChat` → **Gemini 2.5 Flash** (key: `EMERGENT_LLM_KEY`) |
| File storage | **Emergent object storage** (`https://integrations.emergentagent.com/objstore/api/v1/storage`), accessed with the `requests` library |
| Email | **Resend** (team invite emails only) |
| Background work | FastAPI `BackgroundTasks` and `asyncio.to_thread`, in the same process. There is no queue or worker. |

The `requirements.txt` file is a large frozen list that also includes unused packages: stripe, boto3, openai, google-generativeai, litellm, passlib, python-jose, pandas, and others.

### 2.2 Frontend
| Concern | Technology |
|---|---|
| Framework | React 19, Create React App (`react-scripts` 5) wrapped with **CRACO 7** |
| Routing | react-router-dom 7 (`BrowserRouter`) |
| HTTP | axios, one shared instance in `src/lib/api.js` |
| State | Local `useState` per page. Auth is kept in `localStorage`. A react-query provider is set up but unused. |
| UI kit | **shadcn/ui** ("new-york" style, JS) on Radix primitives, Tailwind CSS 3 |
| Icons | lucide-react |
| Charts | recharts |
| Toasts | sonner |
| Annotation canvas | **No canvas library.** Plain DOM: an `<img>`, an SVG overlay (`viewBox 0 0 100 100`) for polygons, lines, points and ellipses, and absolutely-positioned `<div>`s for boxes. |
| Package manager | **Yarn 1.x**. `npm install` fails with ERESOLVE because of a react-day-picker/date-fns peer conflict. |

### 2.3 Architecture diagram
```
┌──────────────────────────┐       HTTPS / JSON (Bearer JWT)       ┌──────────────────────────────┐
│  React SPA (frontend/)   │ ─────────────────────────────────────▶│  FastAPI (backend/server.py  │
│  - pages/*.jsx           │   <img src=/api/files/...?auth=JWT>   │  + routers/*.py), /api/*     │
│  - lib/api.js (axios)    │ ◀──────────────────────────────────── │                              │
│  - polling (no WS)       │                                       │  BackgroundTasks / threads:  │
└──────────────────────────┘                                       │   • video frame extraction   │
                                                                   │   • batch auto-label         │
                                                                   │   • YOLO training            │
                                                                   └───┬─────────┬─────────┬──────┘
                                                                       │         │         │
                                                   ┌───────────────────┘         │         └──────────────┐
                                                   ▼                             ▼                        ▼
                                          ┌────────────────┐        ┌───────────────────────┐   ┌──────────────────┐
                                          │   MongoDB      │        │ Emergent Object Store │   │ Gemini 2.5 Flash │
                                          │ (13 collections│        │ images, video, frames,│   │ (via Emergent    │
                                          │  metadata)     │        │ trained best.pt       │   │  LLM proxy)      │
                                          └────────────────┘        └───────────────────────┘   └──────────────────┘
                                                                                                  ┌──────────────┐
                                                                                                  │ Resend email │
                                                                                                  │ (invites)    │
                                                                                                  └──────────────┘
```

### 2.4 Repository layout
```
think-vision-app/
├── backend/
│   ├── server.py              # App setup, shared helpers, auth deps, storage, videos, auto-label,
│   │                          # batch jobs, assignments, dashboards, versions, export, training engine,
│   │                          # inference, AL scoring, system endpoints, router registration, startup
│   ├── routers/
│   │   ├── auth.py            # register / login / me
│   │   ├── teams.py           # teams, members, invites
│   │   ├── projects.py        # projects, classes, settings
│   │   ├── images.py          # upload, list, assign, submit, review, file proxy, annotations
│   │   ├── training.py        # train-real, model get/activate/cancel, training-plan
│   │   ├── active_learning.py # AL queue
│   │   └── collaboration.py   # comments, activity, notifications
│   ├── tests/                 # pytest integration tests (live HTTP)
│   ├── requirements.txt, pytest.ini
│   └── yolov8n.pt             # base checkpoint (git-ignored)
├── frontend/
│   ├── src/App.js             # routes
│   ├── src/lib/api.js         # axios instance, auth helpers, fileUrl()
│   ├── src/pages/             # Landing, Auth, Dashboard, ProjectDetail, Annotator, Versions,
│   │                          # ModelDeploy, Teams, TeamDetail, TeamDashboard, Activity
│   ├── src/components/        # Navbar, NotificationsBell, ui/* (shadcn)
│   ├── src/index.css          # design tokens & utilities
│   └── craco.config.js, tailwind.config.js, package.json
├── design_guidelines.json     # visual design rules
├── memory/PRD.md              # product requirements / changelog by iteration
├── tests/                     # smoke/integration test scripts (live HTTP)
└── test_reports/              # historical test run reports
```

**Router registration pattern.** Every file in `routers/` defines `router = APIRouter()` and a `register(server_module)` function. That function pulls `db`, helpers and Pydantic models off `server.py` and then declares the routes. At the bottom, `server.py` calls `register(sys.modules[__name__])` for each router and includes it under `/api`. This pattern avoids circular imports.

---

## 3. Setup & Configuration

### 3.1 Backend environment variables (`backend/.env`)
| Name | Required | Purpose |
|---|---|---|
| `MONGO_URL` | yes | MongoDB connection string |
| `DB_NAME` | yes | Database name |
| `JWT_SECRET` | yes | HS256 signing secret |
| `EMERGENT_LLM_KEY` | yes | Key for the Gemini proxy **and** the Emergent object storage |
| `APP_NAME` | no (default `visionforge`) | Prefix for storage object paths |
| `BRAND_NAME` | no (default `ThinkVision`) | Brand name used in API messages and emails |
| `ALLOWED_EMAIL_DOMAIN` | no | If set (e.g. `tneuralai.com`), only emails on this domain can register, log in or be invited |
| `RESEND_API_KEY` | no | Enables invite emails. Without it, emails are skipped. |
| `SENDER_EMAIL` | no (default `onboarding@resend.dev`) | From-address for invite emails |
| `APP_URL` | no | Base URL used in email links (`{APP_URL}/auth`) |
| `CORS_ORIGINS` | no (default `*`) | Comma-separated list of allowed origins |

### 3.2 Frontend environment variables (`frontend/.env`)
| Name | Purpose |
|---|---|
| `REACT_APP_BACKEND_URL` | Backend origin. The API base is `${REACT_APP_BACKEND_URL}/api`. |
| `WDS_SOCKET_PORT` | Dev-server websocket port |
| `ENABLE_HEALTH_CHECK` | When `"true"`, adds dev-server `/health*` routes |

### 3.3 Running locally (summary)
1. Start MongoDB, for example `docker run -d -p 27017:27017 -v thinkvision-mongo:/data/db mongo:7`. If Mongo is down, requests hang for about 30 seconds.
2. Backend:
   - Install with Python 3.12: `pip install -r requirements.txt`. This needs Emergent's extra package index (for `emergentintegrations`) and the PyTorch CPU index.
   - Run with `uvicorn server:app --reload --port 8001`.
3. Frontend: using Node 20 and Yarn 1.x, run `yarn install` and then `yarn start`.
4. The first real training run downloads the YOLOv8 base weights from Ultralytics.

### 3.4 Storage layout (Emergent object store)
| Object | Path |
|---|---|
| Uploaded image | `{APP_NAME}/projects/{project_id}/{image_id}.{ext}` |
| Extracted video frame | `{APP_NAME}/projects/{project_id}/{image_id}.jpg` |
| Uploaded video | `{APP_NAME}/projects/{project_id}/videos/{video_id}.{ext}` |
| Trained weights | `{APP_NAME}/projects/{project_id}/models/{model_id}.pt` |

On startup, the backend calls the storage `init` endpoint with `EMERGENT_LLM_KEY` and caches the returned `storage_key`. Every later PUT or GET to `/objects/{path}` sends it in the `X-Storage-Key` header.

### 3.5 Startup and shutdown
- **Startup:** initialize storage, then create MongoDB indexes (see §5.14). Failures are logged but do not stop the app.
- **Shutdown:** close the Mongo client.

---

## 4. Authentication & Authorization

### 4.1 Mechanism
- **Email and password.** Passwords are hashed with `bcrypt.hashpw(gensalt())`.
- **JWT:** HS256, payload `{sub: user_id, exp: now + 7 days}`.
  - There are no refresh tokens and no server-side logout.
  - There is no password reset, password change or email verification.
- **The token is accepted in two ways:**
  - the `Authorization: Bearer <token>` header;
  - the `?auth=<token>` query parameter. This exists so `<img src>` tags can load `/api/files/...`.
- Missing or invalid tokens, and unknown users, get **401**.
- **Email domain restriction:** if `ALLOWED_EMAIL_DOMAIN` is set:
  - register and login return **403** for other domains;
  - invites return **400** for other domains.
- **Invite auto-activation:** on every authenticated request (inside `get_current_user`), and on register and login, the server checks for pending invites that match the user's email and activates them.

### 4.2 Frontend auth handling
- `localStorage` keys:
  - `vf_token` holds the JWT;
  - `vf_user` holds a JSON string `{id, email, name}`.
- An axios request interceptor adds `Authorization: Bearer <vf_token>`.
- An axios response interceptor handles any **401** by clearing both keys. If the current path is not `/` and not `/auth*`, it hard-redirects to `/auth`.
- `fileUrl(storagePath)` returns `${API}/files/${storagePath}?auth=${token}`. Every image thumbnail and annotator image uses it.
- The `<Protected>` route wrapper only checks that a token exists. The server enforces everything else.
- "Exit" (logout) just clears localStorage and navigates to `/`.

### 4.3 Roles (per team, stored in `team_members.role`)
| Role | Meaning |
|---|---|
| `owner` | Team creator. There is exactly one per team. It can't be changed or removed. |
| `admin` | Manages projects, members (invite only), settings and training |
| `member` | A full contributor: creates projects, uploads, annotates, exports |
| `reviewer` | Approves or rejects submitted images. Sees only projects they're assigned to, if the project has assignments. |
| `annotator` | Labels images. Sees only projects they're assigned to, if the project has assignments. |

Invites and role changes accept `admin | member | annotator | reviewer`.

### 4.4 Project access check (`_project_access_check(pid, uid, roles?)`)
1. The project must exist.
2. **If the project has a `team_id`:**
   - The user must be an **active** member of that team.
   - If `roles` is given, the member's role must be in that list. Otherwise the check fails with 403.
   - If the member is an `annotator` or `reviewer` **and** the project has at least one `project_assignments` row, the user must have their own assignment row.
3. **Legacy projects with no `team_id`:** the user must be the project's `user_id`.

### 4.5 Permission matrix
| Action | Allowed |
|---|---|
| Create a project in a team | owner, admin, member |
| Delete a project; delete a class; update project settings | owner, admin |
| Assign or unassign users to a project; bulk-assign images | owner, admin |
| Start real training; activate a model; cancel training | owner, admin |
| Approve or reject an image | owner, admin, reviewer |
| View the team dashboard | owner, admin, reviewer |
| View team details (`GET /teams/{tid}`) | owner, admin, member. **Annotators and reviewers get 403.** |
| Rename or delete a team; change a member's role | owner |
| Invite members | owner, admin |
| Remove a member | owner or admin, or the member themselves (leaving). The owner can never be removed. |
| Everything else: upload, annotate, single-image assign, submit, delete an image, comments, export, versions, simulated training, auto-label, videos | any user who passes project access |

---

## 5. Data Model

MongoDB has 13 collections. In every collection, `id` is a UUID string, and `created_at` is an ISO timestamp.

### 5.1 `users`
`id, email (lowercased), name, password (bcrypt hash), created_at`

### 5.2 `teams`
`id, name, owner_id, is_personal (bool), created_at`
- Each user automatically gets a personal team named `"{name}'s Workspace"`. It can't be deleted.

### 5.3 `team_members` (members and pending invites)
- **Active member:** `id, team_id, user_id, role, status: "active", is_personal?, joined_at`
- **Pending invite:** `id, team_id, invited_email, role, status: "pending", invited_at` (no `user_id`).
  - On activation, `user_id` is set, `status` becomes `active`, and `invited_email` is removed.

### 5.4 `projects`
| Field | Type | Notes |
|---|---|---|
| `id, name, description` | str | |
| `team_id` | str | Owning team. Legacy projects may lack it. |
| `user_id` | str | Creator |
| `task_type` | `object_detection` \| `classification` \| `segmentation` | **Label only. No behavior depends on it.** |
| `classes` | [str] | Class labels. Their order defines class indices in export and training. |
| `settings` | object (optional) | `{confidence_threshold: float (0.05–0.95, default 0.4), fallback_to_gemini: bool (default true), min_boxes_threshold: int ≥0 (default 1), show_confidence: bool (default true)}` |
| `last_retrain_nudge_count` | int | Used by the retrain nudge (§7.8) |
| `created_at` | str | |

### 5.5 `project_assignments`
`id, project_id, user_id, assigned_by, created_at`: the project-level access list for annotators and reviewers.

### 5.6 `images`
| Field | Notes |
|---|---|
| `id, project_id, user_id (uploader), filename, storage_path, content_type, size` | |
| `annotations` | `[Annotation]` (§5.7) |
| `annotated` | bool, true when there is at least one annotation |
| `assigned_to` | user_id or null |
| `review_status` | `unassigned` \| `assigned` \| `submitted` \| `approved` \| `rejected` |
| `review_notes, reviewed_by, reviewed_at, submitted_at` | Review metadata |
| `priority` | `low` \| `medium` \| `high` (not validated) |
| `due_date, task_notes` | Task metadata |
| `from_video` | video_id, set when the image was extracted from a video |
| `created_at` | |

Frames extracted from videos currently lack the `review_status` and `assigned_to` fields.

### 5.7 Annotation object (inside `images.annotations`)
Pydantic `AnnotationBox` with `extra="allow"`, so unknown fields are kept.

| Field | Used by | Meaning |
|---|---|---|
| `type` | all | `bbox` \| `polygon` \| `polyline` \| `point` \| `ellipse`. A missing type is treated as `bbox`. |
| `label` | all | Class label string |
| `x, y` | bbox: **top-left** corner. ellipse and point: **center**. | normalized |
| `w, h` | bbox size. For an ellipse, `2·rx` and `2·ry`. For a point, 0. | normalized |
| `points` | polygon, polyline | `[[x, y], ...]` normalized |
| `rx, ry` | ellipse | radii, normalized |
| `rotation` | bbox | Radians, clockwise, around the box center. **This is how OBB (oriented boxes) is represented.** There is no separate OBB type. |
| `confidence` | AI output | 0–1 (set by model predictions) |
| `source` | AI output | `"model"` or `"gemini"` |

### 5.8 `videos`
`id, project_id, user_id, filename, storage_path, size, interval_frames, interval_seconds | null, status (queued|processing|completed|failed), extracted_count, processed_frames, total_frames, resolved_interval_frames, fps, error, completed_at, created_at`

### 5.9 `batch_jobs`
`id, project_id, user_id, type: "auto_label", status (queued|processing|completed), total, processed, labeled, failed, source_counts {model, gemini, "model+gemini", model_no_fallback}, created_at, completed_at`

### 5.10 `versions`
`id, project_id, name, notes, image_count, snapshot_image_ids: [str] (≤2000), created_at`
- A version is a **metadata snapshot only**: a list of image IDs. It does not copy annotations.

### 5.11 `models` (two shapes share the collection)
- **Simulated (legacy):** `id, project_id, version_id, model_arch, epochs, metrics: [{epoch, loss, mAP, precision, recall}], final_mAP, final_loss, status: "deployed", endpoint: "/api/inference/{id}", created_at`. There is no `type` field.
- **Real:**
  - Identity and configuration: `id, project_id, user_id, type: "real", model_arch, epochs, epochs_requested, epochs_auto_scaled`.
  - Status and progress: `status (queued|preparing|training|trained|failed|cancelled), is_active, training_image_count, progress (0–100), current_epoch`.
  - Cancellation and errors: `cancel_requested, cancelled_at, error`.
  - Results: `weights_path, weights_size, final_mAP (= mAP50), final_loss (= 1 − mAP50-95), precision, recall, metrics_full {mAP50, mAP50_95, precision, recall}`.
  - Timestamps: `completed_at, activated_at, created_at`.

### 5.12 `comments`
`id, image_id, project_id, user_id, user_name, text, parent_id | null, resolved, resolved_by?, resolved_at?, created_at`
- Threads are one level deep: a reply sets `parent_id` to the ID of a top-level comment.

### 5.13 `notifications` and `activity_log`
- **notifications:** `id, user_id, kind, title, project_id, image_id, meta, read (bool), created_at`
  - Kinds: `project_assigned, image_assigned, review_needed, review_completed, rejected, comment, retrain_nudge, training_ready`
- **activity_log:** `id, project_id, team_id, user_id, user_name, action, meta, created_at`
  - Actions: `project_assigned, project_unassigned, bulk_assigned, image_assigned, image_unassigned, image_submitted, image_approved, image_rejectd` (typo, see §12), `comment_added, settings_updated, training_started, model_activated, training_cancelled`

### 5.14 Indexes (created at startup)
| Collection | Indexes |
|---|---|
| images | (project_id, review_status, created_at desc); assigned_to; storage_path |
| activity_log | (project_id, created_at desc) |
| notifications | (user_id, created_at desc) |
| team_members | (team_id, user_id); (invited_email, status) |
| project_assignments | (project_id, user_id) |
| comments | (image_id, created_at) |
| models | (project_id, is_active) |

---

## 6. API Reference

There are 64 endpoints, all under `/api`. Unless marked **public**, every endpoint requires a JWT.

### 6.1 System
| Method & Path | Auth | Description |
|---|---|---|
| `GET /` | public | Returns `{message: "<BRAND> API", version: "1.0"}` |
| `GET /system/versions` | public | Returns `{app, ultralytics, torch, cuda_available, supported_archs: [yolov8n,s,m,l], brand, allowed_email_domain}` |

### 6.2 Auth (`routers/auth.py`)
| Method & Path | Body | Response / behavior |
|---|---|---|
| `POST /auth/register` | `{email, password, name}` | Lowercases the email and enforces the allowed domain. Returns 400 if the email is taken. Creates the user and a personal team, and activates pending invites. Returns `{token, user: {id, email, name}}`. |
| `POST /auth/login` | `{email, password}` | Returns 400 "Invalid credentials" on failure. Ensures the personal team exists (and migrates legacy projects into it) and activates invites. Returns `{token, user}`. |
| `GET /auth/me` | none | Returns `{id, email, name}` |

### 6.3 Teams (`routers/teams.py`)
| Method & Path | Roles | Body | Behavior |
|---|---|---|---|
| `GET /teams` | any | none | The user's active teams, each with `role, is_personal, member_count, project_count`. The personal team comes first. |
| `POST /teams` | any | `{name}` | Creates a team with the caller as owner |
| `GET /teams/{tid}` | owner, admin, member | none | The team plus `members[]`. Active members include email and name; pending invites show `invited_email` and the name "(pending)". |
| `PATCH /teams/{tid}` | owner | `{name}` | Renames the team |
| `DELETE /teams/{tid}` | owner | none | Returns 400 for a personal team. Deletes the team's projects and their images, versions and models, then the memberships and the team itself. |
| `POST /teams/{tid}/invite` | owner, admin | `{email, role}` | If the user exists, adds them as `active` immediately (400 if already a member). Otherwise creates a `pending` invite (400 if one is already pending). Sends a Resend email. Returns `{status, email, email_result}`, where `email_result` is `{skipped}`, `{sent, email_id}` or `{sent: false, error}`. |
| `PATCH /teams/{tid}/members/{mid}` | owner | `{role}` | Changes a member's role. The owner's role can't be changed. `mid` is the team_members document ID. |
| `DELETE /teams/{tid}/members/{mid}` | owner, admin, or self | none | Removes a member. The owner can't be removed. |

### 6.4 Projects (`routers/projects.py`)
| Method & Path | Roles | Body | Behavior |
|---|---|---|---|
| `POST /projects` | owner, admin, member | `{name, description?, task_type="object_detection", team_id?}` | With no `team_id`, uses the personal team |
| `GET /projects?team_id=` | any | none | Projects across the user's teams (and legacy ones). Annotators and reviewers see only projects they're assigned to (when the project has assignments). Items: `{id, name, description, task_type, classes, image_count, annotated_count, team_id, team_name, created_at}` |
| `GET /projects/{pid}` | access | none | Full project including `settings`, with defaults filled in |
| `DELETE /projects/{pid}` | owner, admin | none | Deletes the project and its images, versions and models |
| `POST /projects/{pid}/classes` | access | `{label}` | Appends the label if it's new |
| `POST /projects/{pid}/classes/bulk` | access | `{labels?: [], text?: "a,b\nc"}` | Splits on commas and newlines, then lowercases, trims, truncates to 40 characters and deduplicates. Returns `{classes, added}`. |
| `DELETE /projects/{pid}/classes/{label}` | owner, admin | none | Removes the label from `classes` only. Annotations that use it are untouched. |
| `PATCH /projects/{pid}/settings` | owner, admin | `{confidence_threshold?, fallback_to_gemini?, min_boxes_threshold?, show_confidence?}` | Partial update. The threshold is clamped to 0.05–0.95 and min_boxes to at least 0. Logs `settings_updated`. |

### 6.5 Images (`routers/images.py`)
| Method & Path | Roles | Body / params | Behavior |
|---|---|---|---|
| `POST /projects/{pid}/images` | access | multipart `file` | Accepts jpg, jpeg, png, webp or gif up to **10 MB**. Uploads to storage and inserts an image with `review_status: "unassigned"`. |
| `GET /projects/{pid}/images?status=&assignee=` | access | `status` = review_status, `assignee` = `me` or a user_id | Up to 2000 images, newest first. No pagination. |
| `GET /images/{img_id}` | access | none | The image document |
| `GET /files/{path}` | token (header or `?auth=`) | none | Streams the image bytes. Works only for storage paths of **images**. |
| `PUT /images/{img_id}/annotations` | access | `{boxes: Annotation[]}` | **Replaces** all annotations and sets `annotated`. Any new labels are merged into `project.classes`, which is then **sorted alphabetically**. Returns `{ok, count}`. Does not change `review_status`. |
| `DELETE /images/{img_id}` | access | none | Deletes the database document only |
| `POST /images/{img_id}/assign` | access (no role check) | `{user_id \| null}` | Sets `assigned_to` and `review_status` to `assigned` or `unassigned`. Logs the change and notifies the assignee (`image_assigned`). |
| `POST /images/{img_id}/submit` | access | none | Returns 400 if there are no annotations. Sets `submitted`, `submitted_at`, logs the change, and notifies all owners, admins and reviewers (`review_needed`). |
| `POST /images/{img_id}/review` | owner, admin, reviewer | `{decision: "approve" \| "reject", notes?}` | Sets `approved` or `rejected`, `review_notes`, `reviewed_by` and `reviewed_at`. Logs the change and notifies the assignee (`review_completed` or `rejected`). On approve, runs the retrain-nudge check. |

### 6.6 Videos (`server.py`)
| Method & Path | Body | Behavior |
|---|---|---|
| `POST /projects/{pid}/videos` | multipart `file`, `interval_frames` (default 30), `interval_seconds` (default 0) | Accepts mp4, mov, webm, avi or mkv up to **200 MB**. Stores the video, creates a `videos` document with status `queued`, and starts background frame extraction (§7.1). Returns the video document. |
| `GET /projects/{pid}/videos` | none | Up to 200 videos, newest first |
| `GET /videos/{vid}` | none | The video document, polled for status and progress |
| `DELETE /videos/{vid}` | none | Deletes the video document only. Extracted frames are kept. |

### 6.7 Auto-label and batch jobs (`server.py`)
| Method & Path | Body | Behavior |
|---|---|---|
| `POST /images/{img_id}/auto-label` | none | Runs the auto-label router (§7.3). Returns `{boxes, count, source}`. **Nothing is saved**; the client appends the boxes and then saves. |
| `POST /projects/{pid}/batch-auto-label` | `{only_unlabeled: true}` | Selects up to 5000 images, or returns 400 if none match. Creates a `batch_jobs` document and starts the background job (§7.4). |
| `GET /batch-jobs/{jid}` | none | The job document, polled for progress |
| `GET /projects/{pid}/batch-jobs` | none | The project's jobs, newest first |

### 6.8 Assignments and tasks (`server.py`)
| Method & Path | Roles | Body | Behavior |
|---|---|---|---|
| `POST /projects/{pid}/assign` | owner, admin | `{user_ids: []}` | Adds a project assignment for each active team member who isn't already assigned. Logs and notifies each one (`project_assigned`). Returns `{assigned, count}`. |
| `DELETE /projects/{pid}/assign/{user_id}` | owner, admin | none | Removes the assignment and logs `project_unassigned` |
| `GET /projects/{pid}/assignments` | access | none | Assignments with `user_name, user_email, role` added |
| `POST /projects/{pid}/images/bulk-assign` | owner, admin | `{image_ids, user_id?, priority?, due_date?, notes?}` | Sets `assigned_to`, `review_status`, `priority`, `due_date` and `task_notes` on many images. Returns 400 if the user isn't on the team or there is nothing to update. Logs `bulk_assigned` and notifies the assignee. Returns `{updated}`. |
| `PATCH /images/{img_id}/task` | access | `{priority?, due_date?, notes?}` | Updates the task metadata |

### 6.9 Dashboards (`server.py`)
| Method & Path | Roles | Response |
|---|---|---|
| `GET /projects/{pid}/dashboard` | access | `{total, annotated, counts: {unassigned, assigned, submitted, approved, rejected}, completion_pct (approved/total), annotation_pct, review_pct, members_perf: [{user_id, name, email, role, assigned_to_project, assigned_images, submitted, approved, rejected, last_active}]}` |
| `GET /teams/{tid}/dashboard` | owner, admin, reviewer | `{total_projects, total_images, total_annotated, projects: [{id, name, images, annotated, approved, submitted, completion_pct}]}`, sorted by image count |

### 6.10 Versions and export (`server.py`)
| Method & Path | Body / params | Behavior |
|---|---|---|
| `POST /projects/{pid}/versions` | `{name, notes?}` | Saves the IDs of up to 2000 currently annotated images, plus the count |
| `GET /projects/{pid}/versions` | none | Newest first |
| `GET /projects/{pid}/export?format=yolo\|yolo_obb\|coco\|voc` | none | Streams a ZIP of the **current** annotated images (§7.10). Any other format returns 400. The file name is `{project_name}_{format}.zip`. |

### 6.11 Training and models (`server.py` + `routers/training.py`)
| Method & Path | Roles | Body / params | Behavior |
|---|---|---|---|
| `GET /projects/{pid}/training-plan` | access | none | `{eligible_images, approved, annotated, min_required: 30, recommended_epochs, estimated_seconds_cpu (= eligible × epochs × 2), ready}` |
| `POST /projects/{pid}/train-real?epochs=0&model_arch=yolov8n` | owner, admin | **query params** | `epochs` must be 0–200 (0 means auto-scale). Returns 400 if there are fewer than 30 eligible images. Creates a real model with status `queued` and starts training in the background (§7.5). Logs `training_started`. `model_arch` is not validated. |
| `GET /models/{mid}` | access | none | The model document, polled for progress |
| `POST /models/{mid}/activate` | owner, admin | none | A real model must be `trained` and have weights. Sets `is_active: false` on all other models in the project and `true` on this one. Logs `model_activated`. |
| `POST /models/{mid}/cancel` | owner, admin | none | Only for `queued`, `preparing` or `training`. Sets `cancel_requested`; a queued model goes straight to `cancelled`. Logs `training_cancelled`. |
| `POST /projects/{pid}/train` | access | `{version_id, epochs=30, model_arch="yolov8n"}` | **Simulated** (§7.6). Returns immediately with synthetic curves and status `deployed`. |
| `GET /projects/{pid}/models` | access | none | All models (simulated and real), newest first |

### 6.12 Active learning (`routers/active_learning.py`)
| Method & Path | Params | Response |
|---|---|---|
| `GET /projects/{pid}/active-learning-queue` | `limit=20` | `{has_active_model, total_candidates, items: [{image_id, filename, storage_path, review_status, annotation_count, uncertainty (0–1), reason}]}`, sorted by uncertainty, highest first (§7.7) |

### 6.13 Collaboration (`routers/collaboration.py`)
| Method & Path | Body | Behavior |
|---|---|---|
| `POST /images/{img_id}/comments` | `{text}` | Creates a top-level comment, logs `comment_added`, and notifies the image's assignee (`comment`) |
| `POST /comments/{cid}/replies` | `{text}` | Creates a reply (`parent_id = cid`) and notifies the parent comment's author |
| `POST /comments/{cid}/resolve` | none | Marks the comment resolved. There is no unresolve, edit or delete. |
| `GET /images/{img_id}/comments` | none | A flat list of all comments and replies, oldest first. The client builds the threads. |
| `GET /projects/{pid}/activity?limit=200` | none | Activity log entries, newest first |
| `GET /notifications` | none | `{items (up to 200, newest first), unread}` |
| `POST /notifications/{nid}/read` | none | Marks one notification read |
| `POST /notifications/mark-all-read` | none | Returns `{updated}` |

---

## 7. Core Subsystems: How They Work

### 7.1 Image upload and video frame extraction
- **Images** are uploaded one file per request.
  - The server checks the extension (jpg, jpeg, png, webp or gif) and the size (up to 10 MB).
  - It PUTs the bytes to object storage and inserts the image document.
- **Videos:**
  - The whole file is read into memory, stored in object storage, and a `videos` document is created with status `queued`.
  - A background thread (`_extract_frames_sync`) then:
    1. Writes the video to a temp `.mp4` and opens it with OpenCV.
    2. Works out the frame step:
       - if `interval_seconds > 0`, the step is `round(interval_seconds × fps)`, and it records `resolved_interval_frames` and `fps`;
       - otherwise the step is `interval_frames`.
    3. Keeps every Nth frame. Each kept frame is JPEG-encoded (quality 85), uploaded, and inserted as an `images` document with `from_video: video_id`.
    4. Updates `processed_frames`, `total_frames` and `extracted_count` every 5 frames.
    5. Sets `completed`, or `failed` with an `error` message.

### 7.2 Annotation save semantics
- `PUT /images/{id}/annotations` **replaces** the whole list.
- The frontend auto-saves 2 seconds after the last change, and also on Ctrl/Cmd+S.
- Any labels in the saved boxes that aren't yet in `project.classes` are added, and the list is then **sorted alphabetically**. This can shift class indices (see §12).
- `annotated = len(boxes) > 0`.

### 7.3 Auto-label router (`_auto_label_router`)
The router reads these project settings:

| Setting | Default |
|---|---|
| `confidence_threshold` | 0.4 |
| `fallback_to_gemini` | true |
| `min_boxes_threshold` | 1 |

```
active = model with {project_id, is_active: true, status: "trained"}
if active has weights:
    model_boxes = YOLO predict(image, conf=confidence_threshold)
      → bboxes (normalized) with confidence and source="model"
      → the predicted class index is mapped to project.classes[idx] ("object" if out of range),
        so if the class list has changed since training, labels can come out wrong
    if len(model_boxes) >= min_boxes_threshold  → return (model_boxes, "model")
    if not fallback_to_gemini                   → return (model_boxes, "model_no_fallback")
    (otherwise fall through to Gemini)
Gemini 2.5 Flash:
    system prompt asks for a JSON array [{label, x, y, w, h}], where x,y is the top-left corner (normalized)
    the response is parsed tolerantly (strip ``` and "json", slice from "[" to "]")
    labels are lowercased and truncated to 40 chars; coordinates are clamped to 0–1; source="gemini"
    Gemini boxes have no confidence and are NOT restricted to the project's classes
if the model ran and returned boxes → return (model_boxes + gemini_boxes, "model+gemini")
else                                 → return (gemini_boxes, "gemini")
```
- Model loading: the weights are downloaded from storage to a temp file and loaded with `YOLO(path)`. The result is cached in the process-wide `_yolo_cache`, which is never evicted.
- If local inference fails, the error is logged and the router falls back to Gemini.

### 7.4 Batch auto-label job
1. `POST /projects/{pid}/batch-auto-label` selects up to 5000 images. With `only_unlabeled`, only images with `annotated: false` are included.
2. The background job processes the images one after another. For each image, it:
   - fetches the bytes and runs the router;
   - **appends** the predicted boxes to any existing annotations (no deduplication);
   - sets `annotated: true`;
   - merges the new labels into the classes (sorted);
   - increments `processed`, `labeled` or `failed`, and `source_counts[source]`.
3. At the end it sets `status: completed` and `completed_at`.
4. The frontend polls `GET /batch-jobs/{id}` every 2.5 seconds.

### 7.5 Real YOLOv8 training
**Eligibility**
- `eligible = max(approved images with annotations, annotated non-rejected images with annotations)`.
- There must be at least **30** eligible images (`MIN_TRAINING_IMAGES`).

**Epochs (`auto_scale_epochs`)**
- If the user passes `epochs > 0`, it is clamped to 1–200.
- Otherwise it's picked from the eligible image count:

| Eligible images | Epochs |
|---|---|
| fewer than 50 | 10 |
| fewer than 150 | 20 |
| fewer than 500 | 30 |
| 500 or more | 50 |

**Background thread (`_train_yolo_sync`)**
1. Loads the training images: approved images with annotations, topped up with annotated non-rejected images if there are fewer than 30. It fails if the project has no classes or there are still fewer than 30 images. The status becomes `preparing`.
2. Builds a temp dataset:
   - Shuffles the images and splits them **80/20 into train and val**.
   - Converts each image to RGB JPEG (quality 90).
   - Writes YOLO label files. The class index is the label's position in `project.classes`; labels that aren't in the list are skipped.
   - Every shape becomes an **axis-aligned detection box**:

| Shape | Converted to |
|---|---|
| polygon, ellipse | their bounding box |
| bbox, polyline | the stored x, y, w, h |
| bbox with `rotation` | the stored box; the rotation is ignored |
| point, zero-size box | skipped |

   - Writes `data.yaml`.
3. Calls `YOLO(f"{model_arch}.pt").train(data, epochs, imgsz=640, batch=8, device="cpu", plots=False)`. The status becomes `training`.
4. Callbacks:
   - `on_train_epoch_end` updates `current_epoch` and `progress`.
   - The epoch and batch callbacks check `cancel_requested` in Mongo. If it's set, they raise `KeyboardInterrupt`, which ends the run as `cancelled`.
5. On success:
   - Uploads `best.pt` (or `last.pt` as a fallback) to storage.
   - Saves the metrics: mAP50, mAP50-95, precision and recall. `final_mAP` is mAP50.
   - Sets the status to `trained` and resets `project.last_retrain_nudge_count` to 0.
6. On error: status `failed` with an `error` message (up to 500 characters). The temp dir is always removed.

**Status lifecycle:** `queued → preparing → training → trained`, or `failed` or `cancelled`.

**Deploy:** "deploying" means **activating** the model (only one active model per project). There is **no public inference endpoint** and no weight download. The active model is used only for auto-labeling.

### 7.6 Simulated training (legacy)
`POST /projects/{pid}/train` checks that the version exists and then generates random, plausible per-epoch curves: loss going down, and mAP, precision and recall going up. It stores a model with `status: "deployed"` and `endpoint: "/api/inference/{id}"`. **That endpoint does not exist.** Simulated models do nothing for auto-labeling.

### 7.7 Active-learning queue
- **Candidates:** up to 500 images, newest first, with `review_status` in {unassigned, assigned, rejected, null}.
- **With an active trained model:** the top `limit × 3` candidates are scored. **Inference is not re-run**; the image's stored annotations are scored instead.
  - An image with no annotations gets `uncertainty = 0.85` (reason `no_detections`).
  - Otherwise the score is `0.5 × (1 − avg_confidence) + 0.5 × (fraction of boxes whose confidence is within ±0.15 of the threshold)`. A box with no confidence counts as 0.5. The reason is `low_confidence`.
- **Without an active model:** the top `limit × 2` candidates are scored. Unlabeled images get 1.0 (`no_active_model`) and labeled images 0.5 (`labeled_no_model`).
- The results are sorted by score, highest first, and cut to `limit`.

### 7.8 Retrain nudge
This runs every time an image is **approved**:
1. Find the latest real, trained model and count the images approved after its `created_at`. If there is no such model, count all approved images.
2. Send a nudge if `count ≥ 30` **and** `count ≥ last_retrain_nudge_count + 30`, so at 30, 60, 90 and so on.
3. When a nudge is sent:
   - Store `last_retrain_nudge_count = count`.
   - Notify the team owners and admins. The code also tries `project.owner_id`, a field projects don't have.
   - The kind is `retrain_nudge` if a trained model exists, otherwise `training_ready`.
4. After a successful training run, the counter is reset to 0.

### 7.9 Dataset versions
A version records the IDs of the currently annotated images, plus a name and notes.
- **Export and real training ignore versions.** They always use the live data.
- Only simulated training requires a `version_id`.
- There is no augmentation, preprocessing or saved train/val/test split.

### 7.10 Export formats
A ZIP is built in memory from up to 2000 annotated images, with the image bytes fetched from storage. Everything goes under `dataset/`, which always includes `classes.txt`.

| Format | Contents |
|---|---|
| `yolo` | `data.yaml` (`train: images`, `val: images`, `nc`, `names`), `images/{id}.{ext}`, `labels/{id}.txt`. A **polygon** is written as a YOLO-seg line `cls x1 y1 x2 y2 …`. Other shapes are written as `cls cx cy w h`: a point becomes a 0.005 box and an ellipse its bounding box. |
| `yolo_obb` | Same as `yolo`, except a bbox with non-zero `rotation` is written as 4 rotated corners, `cls x1 y1 x2 y2 x3 y3 x4 y4`, clamped to 0–1 |
| `coco` | `annotations.json` with `images` (width and height read with PIL), `categories` (ids start at 0), and `annotations` (pixel `bbox`; polygons get a `segmentation`), plus `images/` |
| `voc` | `annotations/{id}.xml` per image with `<object><name><bndbox>`. Polygons also get a non-standard `<polygon><x1>…` block. |

Labels that aren't in `classes` are written as index 0.

### 7.11 Review workflow (per image)
```
            assign (single or bulk)         submit (needs ≥1 annotation)       review
unassigned ────────────────────▶ assigned ──────────────────────────▶ submitted ──────────▶ approved
     ▲                              │                                               └──────▶ rejected
     └──── unassign (user_id=null) ─┘                                        (rejected images can be edited
                                                                              and submitted again)
```
- Assigning sends a notification to the assignee.
- Submitting sends a notification to the team's owners, admins and reviewers.
- Reviewing sends a notification to the assignee. An approval also runs the retrain-nudge check.
- Saving annotations never changes `review_status`.
- Project-level visibility for annotators and reviewers is controlled by `project_assignments`.

### 7.12 Comments, notifications and activity
- **Comments** are threaded one level deep and can be resolved. They are notification-driven.
- **Notifications** are in-app only; the only email is the team invite. The frontend polls them every 15 seconds.
- **The activity log** is written by `_log_activity`, which never fails the request. It is shown on the Activity page.
- **There is no real-time channel** (no WebSocket or SSE). Everything uses polling.

### 7.13 Background job model
- Video extraction, batch auto-label and training all run inside the API process, using `BackgroundTasks` and `asyncio.to_thread` with their own sync PyMongo client.
- There is no persistent queue, no retries, and no protection against concurrent training runs.
- **A server restart leaves jobs stuck** in `queued`, `processing` or `training`.

---

## 8. Frontend Specification

### 8.1 Routes (`src/App.js`)
| Path | Page | Protected |
|---|---|---|
| `/` | Landing | no |
| `/auth` (`?mode=register`) | Auth | no |
| `/dashboard` | Dashboard | yes |
| `/teams` | Teams | yes |
| `/teams/:tid` | TeamDetail | yes |
| `/teams/:tid/dashboard` | TeamDashboard | yes |
| `/projects/:pid` | ProjectDetail | yes |
| `/projects/:pid/activity` | Activity | yes |
| `/projects/:pid/annotate/:imgId` | Annotator | yes |
| `/projects/:pid/versions` | Versions | yes |
| `/projects/:pid/deploy` (`?version=`) | ModelDeploy | yes |

There is no 404 route and there are no role-based route guards. Role checks happen inside the pages.

### 8.2 Shared components
- **Navbar** (every authenticated page):
  - Sticky, with a blurred `#050505` background.
  - The logo (a cyan square with the word "THINKVISION") links to `/dashboard`.
  - Optional breadcrumbs (`crumbs` prop: `[{label, to?}]`).
  - Links to Projects and Teams, the NotificationsBell, the user's name, and an **Exit** button (clears auth and goes to `/`).
- **NotificationsBell:**
  - Polls `GET /notifications` every **15 s**. An unread badge shows up to "99+".
  - The dropdown shows the latest 20 items. Unread items have a tinted background and a cyan dot.
  - Each item shows its title, kind and relative time.
  - Clicking an item marks it read and navigates:
    - to the annotator if the item has an `image_id`;
    - otherwise to the project if it has a `project_id`;
    - otherwise to the dashboard.
  - A "Mark all" button marks everything read.

### 8.3 Landing (`/`)
The public marketing page:
- **Header:** "Log In" and "Start Building" (register).
- **Hero:** "The computer vision operating system for production teams_", with a blinking cursor, "Start Free" and "Sign In" buttons, and tags (Object Detection, Instance Segmentation, Classification, Auto-Labeling).
- **Six feature cards:** Annotate, Auto-Label, Version, Export, Deploy, API.
- **Call to action:** "Your first dataset, in five minutes."
- **Footer.**

It makes no API calls.

### 8.4 Auth (`/auth`)
- A single login/register form with a toggle.
- **Fields:** Name (register only), Email, Password (min length 6).
- **On mount:**
  - redirects to `/dashboard` if a token already exists;
  - calls `GET /system/versions` to show a "Restricted to @domain addresses" hint and a matching placeholder.
- **Submit:** `POST /auth/login` or `/auth/register`. It then saves the token, shows a toast and goes to `/dashboard`.
- The left panel shows a decorative terminal mock-up.

### 8.5 Dashboard (`/dashboard`)
- Loads `GET /projects` and `GET /teams`.
- The **New Project** dialog has name, description and task type. It calls `POST /projects` with `team_id` set to the personal team (there is no team picker) and then opens the new project.
- **Project cards** show the task type, team name, name, description, image count and labeled count. Clicking a card opens the project.
- A delete icon appears on hover. It asks for confirmation and then calls `DELETE /projects/:id`.

### 8.6 ProjectDetail (`/projects/:pid`)
**Load:**
- `GET /projects/:pid`, `/images` and `/videos`.
- If the project has a team: `GET /teams/:team_id` (active members), `/projects/:pid/assignments`, and `GET /teams` to work out `myRole`.

`canManage` means `myRole` is owner or admin.

**Header actions:**
- **Assign Project** (managers only): a member picker that calls `POST /projects/:pid/assign`.
- **Versions** and **Deploy**: navigation links.
- **Settings** (gear icon, managers only): an Auto-Label Settings dialog that saves with `PATCH /projects/:pid/settings`. It has:
  - a confidence slider, 0.05–0.95;
  - a min-boxes slider, 0–10;
  - a "Fallback to Gemini" checkbox;
  - a "Show confidence" checkbox.

**Page sections:**
- **Training nudge banner:** shown when `annotated_count ≥ 30`, with a "Train Model →" link to Deploy.
- **Stats:** Total Images, Annotated, Classes, and a Progress % bar.
- **Class labels:** chips for each class. The **Manage Classes** dialog (managers only) has:
  - a bulk-add textarea (commas or newlines) that calls `POST /classes/bulk`;
  - a remove X on each chip that calls `DELETE /classes/:label`.

**Tabs:**
1. **Images (N)**
   - Status filter (done client-side): all, unassigned, assigned, In Review (submitted), approved, rejected.
   - **Bulk Assign** (managers only, with a selection): assignee, priority and notes, sent with `POST /images/bulk-assign`.
   - **Select All / Deselect All** and **Clear** (managers only).
   - **Activity**: a link to the Activity page.
   - **Batch Auto-Label**: asks for confirmation, calls `POST /batch-auto-label {only_unlabeled: true}`, then polls every 2.5 s. A progress panel shows processed/total, labeled, failed and the source counts.
   - **Image grid** cards show:
     - a thumbnail;
     - an "annotated" check;
     - a review status badge;
     - a "Video" badge;
     - a delete button on hover;
     - a select checkbox (managers only);
     - the filename and annotation count.
   - Clicking a card opens the annotator. If Shift is held, or a manager already has a selection, clicking toggles the selection instead.
2. **Upload**
   - A drag-and-drop zone that also opens a file picker (`image/*`, multiple).
   - Files upload **one at a time** with `POST /projects/:pid/images`.
   - A toast reports how many succeeded and failed.
3. **Video (N)**
   - Extract mode: "Every N frames" or "Every N seconds". Interval default 30.
   - Upload calls `POST /projects/:pid/videos`.
   - The list shows extracted count, status and a progress bar, with a delete button.
   - While any video is queued or processing, the **whole page reloads every 3 s**.
4. **AL Queue**
   - Loaded the first time the tab is opened, with `GET /active-learning-queue?limit=30`.
   - Shows a ranked list: thumbnail, filename, status, annotation count, reason, and an uncertainty bar (0–100).
   - Clicking an item opens the annotator. There is a Refresh button.

### 8.7 Annotator (`/projects/:pid/annotate/:imgId`)
**Layout:** three panes.

| Pane | Contents |
|---|---|
| Left sidebar | Classes, add class, shortcut legend |
| Center | Toolbar and canvas |
| Right sidebar | Review panel, annotation list, comments |

**Load:**
- `GET /images/:imgId`, `GET /projects/:pid`, and `GET /projects/:pid/images` (for Prev/Next).
- The team's members, if the project has a team.
- `GET /images/:imgId/comments`.
- Each box gets a client-only `uid`, which is removed before saving.

**Class colors:** each class gets a color from a 7-color palette `[#06B6D4, #D946EF, #EAB308, #22C55E, #EF4444, #F97316, #3B82F6]`, chosen by its position in the class list. The active class is picked in the left sidebar. "Add class" calls `POST /projects/:pid/classes`.

**Tools:**
| Tool | Key | Interaction |
|---|---|---|
| Box (`bbox`) | B | Drag to draw. Minimum size 0.005. |
| Polygon | P | Click to add vertices. Double-click or Enter to finish (at least 3 points). |
| Line (`polyline`) | L | Click to add vertices. Double-click or Enter to finish (at least 2 points). |
| Point | O | A single click |
| Ellipse | E | Drag from the center outward |

**Editing bounding boxes** (only bboxes can be selected or edited):
- Click to select, then drag to move.
- **Eight resize handles** (corners and edges).
- **Rotate handle:** a circle 22 px above the top-center. Dragging it sets `rotation` (this is the OBB). The label chip shows the angle in degrees.
- Positions are clamped to 0–1.

Polygons, lines, points and ellipses can't be moved or have their vertices edited. They can only be relabeled or deleted from the list.

**Zoom and pan:**
- Zoom range is 1×–8×, changing by a factor of 1.2 per step.
- **Ctrl/Cmd + mouse wheel** zooms around the cursor. **`+`/`=`** zooms in, **`-`** zooms out, and **`0`** resets. There are also toolbar buttons.
- **Hold Space and drag** to pan. A "PAN MODE" overlay appears while panning.

**Keyboard shortcuts** (ignored while typing in an input):
| Keys | Action |
|---|---|
| Ctrl/Cmd+S | Save |
| Ctrl/Cmd+Z | Undo (removes the **last** annotation) |
| Esc | Cancel the drawing in progress |
| Enter | Finish the polygon or line |
| Space (hold) | Pan |
| + / = / − / 0 | Zoom in / in / out / reset |
| B P L O E | Switch tool |

**Saving:**
- The Save button or Ctrl/Cmd+S calls `PUT /images/:imgId/annotations` and then refreshes the project's classes.
- **Auto-save** runs 2 s after the last change.
- A status line shows "Saving…", "· Unsaved" or "Saved · HH:MM".
- A browser `beforeunload` warning appears when there are unsaved changes.

**AI assist:**
- The **Auto-Label** button calls `POST /images/:imgId/auto-label` and **appends** the returned boxes.
- A toast names the source: trained model, Gemini, or model + Gemini.
- Box label chips show a confidence % (unless the `show_confidence` setting is false) and a source tag: ·M for model, ·G for Gemini.

**Toolbar:** Prev / `i / N` / Next (by position in the project's image list), the tool palette, save status, zoom controls, Undo, Auto-Label and Save.

**Right sidebar:**
1. **Review panel:**
   - A status badge and the reviewer's note.
   - An **Assigned to** dropdown that calls `POST /images/:id/assign`.
   - **Submit for Review**: shown to the assignee when the status isn't submitted or approved. It saves first if needed, then calls `POST /images/:id/submit`.
   - **Approve / Reject**: shown to owners, admins and reviewers when the status is submitted. Reject asks for a reason with a `window.prompt`. Both call `POST /images/:id/review`.
2. **Annotations list:** each row has a color swatch, an inline **label dropdown** (to relabel), the type, the size, and a delete button on hover. Clicking a bbox row selects it.
3. **Comments:**
   - Threaded (one level) with an unresolved count.
   - **Reply** calls `POST /comments/:id/replies` and **Resolve** calls `POST /comments/:id/resolve`.
   - The new-comment input calls `POST /images/:id/comments`.
   - Comments are re-fetched after each action; they are not polled.

### 8.8 Versions (`/projects/:pid/versions`)
- The **New Version** dialog takes a name and notes, and says "This will snapshot N annotated images". It calls `POST /projects/:pid/versions`.
- **Export panel:**
  - Formats: YOLO (Ultralytics), YOLO OBB (Rotated), COCO, Pascal VOC.
  - **Download Zip** uses `fetch` with a Bearer header and downloads the blob. It is disabled when there are no annotated images.
  - Export always covers the **current** data, not a chosen version.
- **Version history:** name, image count, date and notes, with a **Train Model →** link to `/deploy?version=<id>`. Versions can't be deleted.

### 8.9 ModelDeploy (`/projects/:pid/deploy`)
**Load:** the project, versions, models, `training-plan` and `system/versions`.

**Real Training tab (default):**
- **Runtime info:** ultralytics and torch versions, and GPU or CPU.
- **Inputs:**
  - an architecture selector: yolov8n, s, m or l;
  - an epochs input, where 0 means auto.
- **Train button:** calls `POST /train-real?epochs=&model_arch=`.
- **Hint:** "Requires ≥ 30 approved/annotated images". When ready, it also shows the eligible count, epochs, and an estimate of "~N min on CPU".
- **Progress card:**
  - polls `GET /models/:id` every 5 s and shows the current epoch, total epochs and progress %;
  - when training finishes, shows mAP@50, precision and recall;
  - buttons: **Set Active** (`POST /models/:id/activate`) and **Cancel** (asks for confirmation, then `POST /models/:id/cancel`).
- **Trained Models list:** arch, epochs, an Active badge, image count, date, status and metrics, with Set Active and Cancel buttons.

**Simulated tab:**
- Choose a dataset version, an architecture (yolov8n/s/m/l or rt-detr) and epochs (default 30), then **Start Training**, which calls `POST /train`.
- Shows mAP@0.5, final loss, architecture and status.
- A recharts **LineChart** plots mAP, loss, precision and recall against epoch.
- An inference endpoint panel shows `POST <backend>/api/inference/<id>` with a copy button. That endpoint doesn't exist.
- An "All runs" list.

### 8.10 Teams, TeamDetail, TeamDashboard, Activity
- **Teams (`/teams`):**
  - Team cards show a role badge, Personal or Team, the name, and member and project counts.
  - A **New Team** dialog calls `POST /teams`.
- **TeamDetail (`/teams/:tid`):**
  - **Header:** "N active members · created <date>", plus a **Dashboard** button for owners, admins and reviewers.
  - **Invite Member** (owners and admins): email and a role (annotator, reviewer, member or admin; the default is member). It calls `POST /teams/:tid/invite` and shows a toast saying either "Added" or "Invite sent".
  - **Delete Team** (owner only, not for personal teams).
  - **Member list:** avatar, name or email, a "pending" tag, and the role badge. Role colors: owner cyan with a crown, admin magenta with a shield, reviewer yellow with an eye, annotator green with a pen.
  - The **owner** gets a per-member menu:
    - change role, with `PATCH /teams/:tid/members/:mid`;
    - remove, with `DELETE`.
- **TeamDashboard (`/teams/:tid/dashboard`):**
  - Stat tiles: projects, images, annotated, and active members.
  - A recharts **BarChart** of images, annotated and approved counts per project.
  - A project selector that loads `GET /projects/:pid/dashboard`.
  - A **PieChart** of the status breakdown, colored: unassigned #71717A, assigned #06B6D4, submitted #EAB308, approved #22C55E, rejected #EF4444.
  - Annotation, review and approval progress bars.
  - A **Team Performance** table: member, email, role, assigned, submitted, approved, rejected, last active.
  - It does not poll.
- **Activity (`/projects/:pid/activity`):**
  - Shows `GET /projects/:pid/activity` as an audit list, with an icon for each action, the humanized action text, the user, the time, and the metadata as `k=v`.
  - There is no pagination or filtering.

### 8.11 Polling summary
| Where | Interval | Condition |
|---|---|---|
| NotificationsBell | 15 s | Always, while logged in |
| ProjectDetail (videos) | 3 s (whole page reload) | While any video is queued or processing |
| ProjectDetail (batch job) | 2.5 s | Until the job is `completed` |
| ModelDeploy (training) | 5 s | While the run it just started is still active |
| Annotator auto-save | 2 s debounce | After each change |

### 8.12 Other frontend notes
- `public/index.html` loads an Emergent platform script and **PostHog analytics** with session recording turned on.
- Interactive elements carry kebab-case `data-testid` attributes, which the automated tests use.

---

## 9. Design System

The source is `design_guidelines.json`, implemented in `src/index.css` and `tailwind.config.js`.

- **Theme:** dark only, with a developer-tool look. Tailwind has `darkMode: class` set, but no light theme is defined.
- **Fonts:**
  - **Outfit** for headings (h1–h6, `.font-heading`);
  - **JetBrains Mono** for body text, metrics, tables and navigation.
  - Both load from Google Fonts.
- **Type:** h1 is `text-4xl sm:text-5xl tracking-tight font-bold`. Labels are `text-[10px] sm:text-xs uppercase tracking-[0.2em] font-bold`.
- **Palette:**

| Token | Hex |
|---|---|
| background | `#050505` (never pure black) |
| foreground | `#FAFAFA` |
| surface / card | `#121212` |
| surface hover | `#1C1C1C` |
| border / muted / secondary | `#27272A` |
| muted foreground | `#A1A1AA` |
| **primary (cyan)** | `#06B6D4` (text on it is black) |
| accent magenta | `#D946EF` |
| accent yellow | `#EAB308` |
| success green | `#22C55E` |
| destructive | `#EF4444` |

- **Rules:**
  - No gradients. Use a "technical" grid look with 1 px borders, minimal shadows, and `rounded-sm` or `rounded-none` corners (`--radius: 0.25rem`).
  - Glass blur is used only on the sticky navbar.
  - Primary buttons are `bg-primary text-black hover:bg-cyan-400`.
  - Dropdowns have a solid surface background.
  - Avoid `transition-all`.
  - Every interactive element needs a `data-testid`.
- **Utilities:**
  - `.grid-bg`: a 32 px grid of lines at 3% white.
  - `.grid-bg-fade`: a radial mask.
  - `.panel` and `.panel-hover`: the surface color with a border.
  - `.cursor-blink` and `.fade-in-up`.
  - Also: cyan text selection and custom 8 px scrollbars.
- **Components:** shadcn/ui. The app pages use button, input, label, select, dialog, tabs, textarea, dropdown-menu and sonner toasts (top-right, dark).
- **Where the code departs from the guidelines:**
  - Icons are lucide (the guidelines say Phosphor).
  - Many hex colors are hardcoded inline instead of using tokens.
  - A few elements use `transition-all`.

---

## 10. End-to-End User Workflows

### 10.1 Onboarding and first project
1. The visitor opens `/` and clicks **Start Building**, which goes to `/auth?mode=register`.
2. They register with name, email and password (on the allowed domain, if one is configured). The backend creates the user and a personal workspace team, and returns a JWT.
3. On `/dashboard` they click **New Project**, enter a name, description and task type, and land on `/projects/:pid`.
4. In **Manage Classes** they bulk-add class labels, for example `car, person, bike`.
5. In the **Upload** tab they drag in images. Alternatively, in the **Video** tab they upload a video and choose to extract a frame every N frames or every N seconds.

### 10.2 Team setup
1. On `/teams` the user clicks **New Team**, and becomes its owner.
2. On `/teams/:tid` they click **Invite Member** and enter an email and a role.
   - If the invitee already has an account, they're added immediately.
   - Otherwise a pending invite is created and an email is sent through Resend. The invite activates automatically when that email registers or logs in.
3. The owner can change roles or remove members. Admins can invite.
4. Projects are created in, and visible to, the team. Annotators and reviewers see only the projects they're assigned to (via **Assign Project**), once a project has any assignments.

### 10.3 Annotation and review cycle
1. A manager (owner or admin) selects images in the Images tab and clicks **Bulk Assign**, choosing an assignee, priority and notes. The assignee gets a notification.
2. The annotator opens an image from the notification or the grid, picks a class and a tool, and draws. Changes auto-save every 2 s. Prev and Next move through the images.
3. The annotator clicks **Submit for Review**. Owners, admins and reviewers get a `review_needed` notification.
4. A reviewer opens the image and clicks **Approve**, or **Reject** with a reason. The assignee is notified. A rejected image can be fixed and submitted again.
5. Anyone can discuss an image with threaded **comments** and mark them resolved.
6. Progress is tracked on the **Team Dashboard** and in the **Activity** log.

### 10.4 AI-assisted labeling
1. **Per image:** click **Auto-Label** in the annotator. The boxes are appended, and the user can review and edit them before saving.
2. **In bulk:** click **Batch Auto-Label** in the Images tab. Every unlabeled image is processed in the background, with live progress and source counts.
3. The source is decided by the project settings: the active trained model first, then Gemini if the model finds fewer than `min_boxes_threshold` boxes and fallback is enabled.

### 10.5 Train → activate → auto-label loop (with active learning)
1. Collect at least **30** annotated or approved images. The project page shows a "Train Model" banner.
2. On **Deploy → Real Training**, pick an architecture and epochs (0 means auto) and click **Train**. Progress is polled; the run can be cancelled.
3. When the run finishes, review mAP@50, precision and recall, then click **Set Active**.
4. From then on, auto-labeling uses this model. Tune `confidence_threshold`, `min_boxes_threshold` and the Gemini fallback in **Settings**.
5. Use the **AL Queue** tab to label the most uncertain images first.
6. Once 30 or more new images have been approved since the last training, owners and admins get a **retrain nudge** notification. Retrain and repeat.

### 10.6 Version and export
1. On `/projects/:pid/versions`, click **New Version** (name and notes) to record which images are annotated.
2. Choose an export format (YOLO, YOLO OBB, COCO or VOC) and click **Download Zip**. The archive contains the images and labels for the current annotated data.
3. Optionally, click **Train Model →** on a version to open the simulated training tab with that version pre-selected.

---

## 11. Testing

- **Integration tests run over live HTTP.** They call a running backend at `REACT_APP_BACKEND_URL` and some also write directly to Mongo.
  - `tests/*.py`: smoke suites for:
    - backend basics;
    - teams;
    - annotator v2 (shapes, video, exports);
    - review and batch jobs;
    - collaboration (roles, assignments, comments, notifications, dashboards);
    - real training (the 30-image gate, the lifecycle, activation, the model-then-Gemini router);
    - settings;
    - bug fixes (cancel training, invite activation).
  - `backend/tests/*.py`: pytest suites covering the iteration 9–11 endpoints, including training-plan, train-real validation, the AL queue, cancel, the retrain nudge, and OBB export.
- `backend/pytest.ini` runs with xdist, `-n 2 --dist loadscope`.
- The test account is configured through `TEST_DEMO_EMAIL` and `TEST_DEMO_PASSWORD`. The credentials live in `memory/test_credentials.md` (git-ignored).
- Past run reports are in `test_reports/`. The latest backend and frontend iterations passed fully. An older batch auto-label run had failures because Gemini returned nothing for synthetic images.
- There are no frontend unit tests. Earlier iterations used Emergent's automated browser testing.

---

## 12. Known Limitations, Bugs & Technical Debt

### 12.1 Backend bugs and inconsistencies
1. **Activity action typo.** Rejections are logged as `image_rejectd`, because the action is built as `f"image_{decision}d"`. The frontend works around this.
2. **Class-index drift.**
   - Saving annotations and running batch auto-label re-sort `classes` alphabetically, while adding a class appends it.
   - Class indices in export and training can therefore change over time.
   - Export maps unknown labels to class 0, while training skips them.
3. **Deleting a class leaves annotations that still use that label.**
4. **The retrain nudge reads `project.owner_id`**, a field that doesn't exist. Only team owners and admins actually receive it.
5. **Frames extracted from video lack `review_status` and `assigned_to`**, so they're missing from dashboard status counts.
6. **Simulated models advertise `/api/inference/{id}`, which doesn't exist.** A simulated model can be "activated" but does nothing.
7. **`GET /teams/{tid}` returns 403 for annotators and reviewers**, so they can't see their own team page.
8. **Single-image assign has no role check.** Any member can assign.
9. **The reviewer role is inconsistent.** Reviewers receive `review_needed` notifications and can review. Some older docs say only owners and admins can review.

### 12.2 Data lifecycle
- Deleting a project or team doesn't delete its videos, batch jobs, comments, notifications, activity, assignments, or **any storage objects**.
- Deleting an image or video doesn't delete its storage object.

### 12.3 Performance and robustness
- Storage calls use synchronous `requests` inside async handlers, which **blocks the event loop** during upload, file proxying and export.
- There are N+1 count queries in the project list, dashboards and teams.
- There is no pagination anywhere. Lists are capped at 200, 500, 2000 or 5000 items.
- Background jobs run in the API process with no queue, retries or restart recovery, and nothing prevents concurrent training runs.
- Training is CPU only (`device="cpu"`). The model cache and inference temp files are never cleaned up.
- `get_current_user` runs an invite-activation query on every request.
- Export is built entirely in memory.

### 12.4 Security
- The JWT is placed in image URLs (`?auth=`), where it can leak through logs or the Referer header.
- There is no rate limiting, password policy (beyond a 6-character minimum in the UI), password reset, email verification, token refresh or revocation.
- CORS defaults to `*` with credentials allowed.

### 12.5 Frontend issues
1. **Annotator coordinates may be off when zoomed or panned.** `getPos` reads the bounding rectangle of the already-transformed container and then corrects for pan and zoom again. This is correct at 1×; it is suspected to be wrong when zoomed or panned.
2. **Middle-mouse panning is intended but can't work.** The handler returns early for any button other than 0.
3. **Undo only removes the last shape.** There is no redo, no edit history, no arrow-key nudge and no Delete-key shortcut.
4. **Only bboxes can be edited.** Polygons, lines, points and ellipses can't be moved, and their vertices can't be edited.
5. **Prev and Next within the 2 s auto-save window can lose unsaved edits.** The guard only covers full page unloads.
6. **The Dashboard has no team picker when creating a project**, so projects always go to the personal team. It also has an unused team filter.
7. **Batch auto-label:** the confirm dialog always says "with Gemini", and polling only stops on `completed`, so a failed job polls forever.
8. **Training progress isn't restored after a page reload** on ModelDeploy, and the models list isn't polled.
9. **Video rows always say "every N frames"**, even when seconds mode was used.
10. **Overlapping badges:** the "annotated" check and the select checkbox occupy the same corner of image cards.
11. **Admins can invite but can't change roles or remove members.** That is owner only, in both the UI and the API.
12. **Teams cards have no badge for reviewer or annotator roles**, and notification kinds only replace their first underscore.
13. **Unused code:** react-query is set up but unused, and many dependencies are installed but never imported (swr, framer-motion, zod, lodash, and others).
14. **Many `window.confirm` and `window.prompt` dialogs** are used instead of in-app dialogs.

### 12.6 Missing features
- There is no public inference or prediction API and no model weight download.
- There is no dataset import (YOLO, COCO, VOC or ZIP).
- There is no augmentation or preprocessing.
- There are no saved train/val/test splits.
- There is no per-version export.
- `task_type` has no functional effect. Classification projects still use the detection annotator.
- There is no real-time collaboration (WebSockets), no GPU training option, and no dedicated keypoint/skeleton tool.

---

## 13. Backlog & Future Ideas

**Open items from the PRD:**
- A preview of dataset augmentations.
- A public dataset marketplace and templates.
- A standalone, public inference endpoint for trained models.

**Natural next steps, suggested by §12:**
1. **Training:**
   - Move background work to a real job queue (for example Celery, RQ or Arq with Redis), with restart recovery, a GPU option and one training run per project at a time.
   - Train from a chosen **dataset version**, and save the train/val/test split with it.
2. **Data integrity:**
   - Keep the class order stable (append-only, or explicit class IDs), and migrate existing data.
   - Clean up everything that belongs to a deleted project, team, image or video, including storage objects.
3. **Annotator:**
   - A full undo/redo history.
   - Editing vertices of polygons and lines.
   - Moving and resizing ellipses and points.
   - Delete and arrow-key shortcuts.
   - Fixing the zoom coordinate maths.
   - Keypoint/skeleton tools.
   - Classification-mode tagging.
4. **Datasets:** import of existing YOLO, COCO and VOC datasets, and export of a specific version.
5. **Deployment:** a public inference API with API keys, and model weight download / ONNX export.
6. **Platform:**
   - Pagination on all lists.
   - Async storage client.
   - WebSocket or SSE push for notifications and job progress.
7. **Accounts:** password reset, email verification, refresh tokens and rate limiting.

---

## 14. Guide for Requesting Modifications

**Where things live:**
| Feature | Backend | Frontend |
|---|---|---|
| Auth | `routers/auth.py`; `get_current_user`, `create_token` in `server.py` | `pages/Auth.jsx`, `lib/api.js` |
| Teams, members, invites | `routers/teams.py`; invite email template, `_ensure_personal_team`, `_activate_pending_invites` in `server.py` | `pages/Teams.jsx`, `TeamDetail.jsx` |
| Projects, classes, settings | `routers/projects.py`; `_project_out`, `_project_access_check` in `server.py` | `pages/Dashboard.jsx`, `ProjectDetail.jsx` |
| Images, annotations, review | `routers/images.py`; `AnnotationBox` model in `server.py` | `pages/Annotator.jsx`, `ProjectDetail.jsx` |
| Videos | `server.py` (`_extract_frames_sync`, video endpoints) | `ProjectDetail.jsx` (Video tab) |
| Auto-label, batch | `server.py` (`_auto_label_router`, `_batch_autolabel_job`, `_yolo_predict_sync`) | `Annotator.jsx`, `ProjectDetail.jsx` |
| Training, models | `server.py` (`_train_yolo_sync`, `auto_scale_epochs`, simulated `/train`), `routers/training.py` | `pages/ModelDeploy.jsx` |
| Active learning | `routers/active_learning.py`, `_uncertainty_score` in `server.py` | `ProjectDetail.jsx` (AL Queue tab) |
| Versions, export | `server.py` (versions, `/export`) | `pages/Versions.jsx` |
| Comments, notifications, activity | `routers/collaboration.py`; `_notify`, `_log_activity` in `server.py` | `Annotator.jsx`, `NotificationsBell.jsx`, `pages/Activity.jsx` |
| Dashboards | `server.py` (`/projects/{pid}/dashboard`, `/teams/{tid}/dashboard`) | `pages/TeamDashboard.jsx` |
| Styling | none | `index.css`, `tailwind.config.js`, `design_guidelines.json` |

**Conventions to keep:**
- **New backend endpoints:** add them to the relevant `routers/*.py` inside `register(s)`, reading shared state from `s` (`s.db`, `s.get_current_user`, `s._project_access_check`, and so on). Everything is mounted under `/api`.
- **Documents:** use a UUID `id` field, exclude `_id` with `{"_id": 0}` in projections, and store timestamps as ISO strings from `datetime.now(timezone.utc).isoformat()`.
- **Permissions:** use `_project_access_check(pid, uid, roles=[...])` for project-scoped endpoints and `_require_team_role` for team-scoped ones. Log important actions with `_log_activity` and notify users with `_notify`.
- **Annotations:** coordinates are normalized to 0–1. A bbox's `x, y` is its top-left corner; an ellipse's and a point's `x, y` is its center; `rotation` is in radians.
- **Frontend:**
  - Call the API through the shared axios instance in `lib/api.js` (`api.get('/path')`), and load images with `fileUrl(storage_path)`.
  - Use shadcn components from `@/components/ui/*`, lucide icons and sonner `toast`.
  - Put a `data-testid` on every interactive element.
  - Follow the dark palette and fonts in §9.
- **Background work:** follow the existing pattern (`BackgroundTasks` + `asyncio.to_thread` with the sync PyMongo client, plus a status document the frontend polls), unless the change is to introduce a real job queue.
