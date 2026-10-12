# ThinkVision

A Roboflow-style computer vision platform: upload images and video, annotate datasets as a team, auto-label with Gemini or a self-trained YOLOv8 model, version and export datasets, and train models.

## Features

- **Annotation**: bounding boxes (including rotated), polygons, polylines, points and ellipses, with zoom/pan, undo and auto-save
- **Auto-labeling**: Gemini 2.5 Flash, or your own trained YOLO model, with confidence thresholds and a Gemini fallback
- **Training**: real YOLOv8 (n/s/m/l) training on approved annotations, plus an active-learning queue that surfaces uncertain images
- **Video**: upload a video and extract frames by frame count or by seconds
- **Teams**: roles (owner, admin, member, reviewer, annotator), assignments, a review workflow, threaded comments, an activity log and notifications
- **Export**: YOLO, YOLO OBB, YOLO-seg, COCO and Pascal VOC as zip files

## Tech stack

| Layer    | Stack                                                        |
| -------- | ------------------------------------------------------------ |
| Frontend | React 19, React Router 7, Tailwind, shadcn/ui, Recharts, CRACO |
| Backend  | FastAPI, Motor (MongoDB), PyJWT, bcrypt                      |
| ML       | Ultralytics YOLOv8 (CPU PyTorch), OpenCV                     |
| Services | MongoDB, Emergent LLM + object storage, Resend (email)       |

## Project structure

```
backend/
  server.py          FastAPI app, shared helpers and models
  routers/           auth, teams, projects, images, collaboration, training, active_learning
  tests/             pytest suites
frontend/            React app
tests/               end-to-end smoke scripts
```

## Prerequisites

- Python 3.12
- Node.js 20 and Yarn 1.x
- A running MongoDB instance
- An `EMERGENT_LLM_KEY` (used for Gemini auto-labeling and image storage)

## MongoDB setup

The backend needs MongoDB at `MONGO_URL`. The easiest way to run it locally is Docker:

```bash
docker run -d --name thinkvision-mongo -p 27017:27017 -v thinkvision-mongo:/data/db mongo:7
```

The `thinkvision-mongo` volume keeps your data if the container is restarted or removed. After a reboot, start it again with `docker start thinkvision-mongo`.

If MongoDB isn't running, the backend still starts, but every request that touches the database (including sign-up and sign-in) hangs for about 30 seconds and then fails.

## Backend setup

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt \
  --extra-index-url https://d33sy5i8bnduwe.cloudfront.net/simple/ \
  --extra-index-url https://download.pytorch.org/whl/cpu
```

Both extra indexes are required:

- `emergentintegrations` is served from Emergent's private index, not PyPI.
- `torch==…+cpu` and `torchvision==…+cpu` are CPU-only builds from the PyTorch index.

Create `backend/.env`:

```env
MONGO_URL=mongodb://localhost:27017
DB_NAME=thinkvision
JWT_SECRET=change-me
EMERGENT_LLM_KEY=your-emergent-key
CORS_ORIGINS=*

# Optional
BRAND_NAME=ThinkVision
APP_NAME=visionforge          # storage path prefix; keep as-is to preserve existing image URLs
ALLOWED_EMAIL_DOMAIN=         # e.g. tneuralai.com; leave empty for open registration (managers still require tneuralai.com)
RESEND_API_KEY=               # team invite emails are skipped if empty
SENDER_EMAIL=onboarding@resend.dev
APP_URL=http://localhost:3000 # used for links in invite emails
```

Start the server:

```bash
uvicorn server:app --reload --host 0.0.0.0 --port 8000
```

The API is served under `/api`, and interactive docs are at http://localhost:8000/docs.

## Manager dashboard

A read-only, company-wide dashboard for managers: work by person and by project, with a date-range filter and CSV export. Managers are a separate global role and are not members of any team.

**Create a manager account** (from `backend/`, with the venv active and MongoDB running):

```bash
python scripts/create_manager.py --email you@tneuralai.com --name "Your Name"
```

- The email must be on the company domain: `ALLOWED_EMAIL_DOMAIN` from `.env`, or `tneuralai.com` if that is empty. Any other domain is rejected, both by the script and at login.
- The script prompts for the password (minimum 8 characters). It is never passed on the command line.
- Run it again with the same email to reset that manager's password.

**Sign in:** open `/auth` and click "Company manager? Sign in here". You land on `/manager`, which has Overview, People, Projects and Work log pages. Managers cannot edit anything, and the API enforces the role on every `/manager/*` call.

**Where the numbers come from**

- *Automatic:* every annotation save records who saved it and when. This only covers saves made after the dashboard shipped.
- *Work log:* members add daily rows at `/work-log` (the **Work log** link in the top bar), or import the team spreadsheet as `.xlsx` or `.csv`. The header row needs `Date`, `Project`, `Task` and `Number of Frames Annotated Today`. `Product`, `Verified by` and `Notes (if any)` are optional. Dates can be `YYYY-MM-DD` or day-first (`18/9/2026`). Re-importing a file does not create duplicates.
- If the app tracked a person's frames for a project on a given day, that number is used and the logged row for the same day and project is ignored, so frames are never counted twice.

Tests: `pytest backend/tests/test_manager_dashboard.py -n 0` runs against a throwaway database and does not need a running backend.

## Annotation and review workflow

The server enforces these rules (the UI mirrors them):

| Role | Can do |
|---|---|
| Annotator, member | Annotate only images assigned to them, while the image is `assigned` or `rejected`, then submit. Start local training of an annotation-assist model and activate their own. |
| Reviewer | Approve or reject submitted images (a note is required to reject). Cannot edit annotations, upload, auto-label or delete. |
| Owner, admin | Assign work, upload, delete, manage models, and run the Deploy Pipeline. They can annotate unassigned images (the image is assigned to them on save) but cannot review their own work unless nobody else could (solo workspace). |

An image moves `unassigned -> assigned -> submitted -> approved`. A rejected image goes back to its annotator, who re-annotates and resubmits. Submitted and approved images are locked.

An image counts as **annotated** only once it is **approved**. Project counts, dashboards, dataset versions, exports, training and the Deploy Pipeline all use approved images only.

Annotators train an *assist* model that only drives auto-label. Production deploys go through the Deploy Pipeline, which only owners and admins can run.

To see how many existing images stop counting until reviewed: `python scripts/backfill_review_status.py` (dry run).

## Frontend setup

```bash
cd frontend
corepack enable            # provides the Yarn version pinned in package.json
yarn install --frozen-lockfile
```

Use Yarn, not npm. `npm install` fails with an `ERESOLVE` peer-dependency error (`react-day-picker@8` vs `date-fns@4`) because npm enforces peer dependencies strictly and ignores `yarn.lock`.

Create `frontend/.env`:

```env
REACT_APP_BACKEND_URL=http://localhost:8000
```

Start the dev server:

```bash
yarn start
```

The app runs at http://localhost:3000.

## Tests

```bash
cd backend
pytest          # runs with 2 xdist workers (see pytest.ini)
pytest -n 0     # run serially
```

The scripts in `tests/` are smoke tests that run against a live backend.

## Where data is stored

| Data | Location |
| ---- | -------- |
| Users (`id`, `email`, `name`, bcrypt-hashed `password`, `created_at`) | MongoDB, `users` collection |
| Teams, memberships and pending invites | MongoDB, `teams` and `team_members` collections |
| Projects, image metadata, annotations, versions, models, comments, notifications | Other collections in the same MongoDB database |
| Image files and trained `best.pt` weights | Emergent object storage (cloud), not MongoDB |
| Login token and current user | Browser `localStorage` (`vf_token`, `vf_user`) |

The database name comes from `DB_NAME` in `backend/.env`. The server doesn't store tokens. It checks the JWT's signature on each request, and tokens expire after 7 days.

A fresh local database is empty, so accounts from any other deployment (such as the Emergent-hosted one) don't exist locally. Sign up again. If `ALLOWED_EMAIL_DOMAIN` is set, only emails at that domain can register or sign in.

To inspect saved users (with password hashes hidden):

```bash
docker exec -it thinkvision-mongo mongosh <DB_NAME> --eval 'db.users.find({}, {password: 0})'
```

## External dependencies

The backend calls Emergent's hosted services in two places, both authenticated with `EMERGENT_LLM_KEY`:

- **Gemini auto-labeling** via `emergentintegrations.llm.chat.LlmChat`
- **Image and model storage** via `https://integrations.emergentagent.com/objstore`

Both stop working if the key expires or runs out of credit. To run fully self-hosted, replace them with the `google-genai` SDK and local or S3 storage.



For future branch changes
You can use this simple sequence:

cd ~/Desktop/think-vision

git checkout <branch-name>
git pull

cd frontend
yarn install
yarn build

sudo rm -rf /var/www/thinkvision/*
sudo cp -r build/* /var/www/thinkvision/
sudo chown -R www-data:www-data /var/www/thinkvision

sudo nginx -t
sudo systemctl reload nginx