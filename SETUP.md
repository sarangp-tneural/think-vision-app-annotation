# Think Vision App — Setup Guide

This guide explains how to run the **main `think-vision-app` repository** on the current machine.

> **Important:** `cicd-pipeline-main/` is the old, now-superseded standalone ML pipeline. The new **Deploy Pipeline** functionality is integrated into the main application.

---

## 1. MongoDB

MongoDB is already running in Docker.

Check that the container is running:

```bash
docker ps --filter name=thinkvision-mongo
```

If it is stopped, start it with:

```bash
docker start thinkvision-mongo
```

---

## 2. Backend

The backend `.env` file is already configured at:

```text
backend/.env
```

### Create and activate the virtual environment

From the repository root:

```bash
cd backend

python -m venv .venv
source .venv/bin/activate
```

### Install dependencies

```bash
pip install -r requirements.txt \
  --extra-index-url https://d33sy5i8bnduwe.cloudfront.net/simple/ \
  --extra-index-url https://download.pytorch.org/whl/cpu
```

### Start the backend

```bash
uvicorn server:app --reload --host 0.0.0.0 --port 8000
```

Backend API:

```text
http://localhost:8000
```

Swagger API documentation:

```text
http://localhost:8000/docs
```

---

## 3. Frontend

The frontend `.env` file is already configured:

```text
frontend/.env
```

The backend URL is configured as:

```text
REACT_APP_BACKEND_URL=http://localhost:8000
```

`node_modules` is already installed.

### Start the frontend

Ports `3000` and `3001` are currently occupied by an unrelated project (`sanchalan`) on this machine.

**Do not stop those processes.**

Run the frontend on port `4321`:

```bash
cd frontend
PORT=4321 yarn start
```

Open the application at:

```text
http://localhost:4321
```

---

## 4. Demo Login

A demo admin account is already registered.

| Field | Value |
|---|---|
| Email | `admin@tneuralai.com` |
| Password | `tneural123` |

manager username:mnager
passsword:Tv-QnwTWfLG6Itr
  

The account already contains one seeded project:

```text
Seed Project
```

---

## 5. Quick Start

From the repository root, use three terminals.

### Terminal 1 — MongoDB

```bash
docker ps --filter name=thinkvision-mongo
```

If necessary:

```bash
docker start thinkvision-mongo
```

### Terminal 2 — Backend

```bash
cd backend
source .venv/bin/activate
uvicorn server:app --reload --host 0.0.0.0 --port 8000
```

### Terminal 3 — Frontend

```bash
cd frontend
PORT=4321 yarn start
```

Then open:

```text
http://localhost:4321
```

---

## 6. Services and Ports

| Service | Port | URL |
|---|---:|---|
| MongoDB | Docker-managed | `thinkvision-mongo` |
| Backend API | `8000` | http://localhost:8000 |
| Swagger Docs | `8000` | http://localhost:8000/docs |
| Frontend | `4321` | http://localhost:4321 |

---

## 7. Main App vs. Old Pipeline

### Main application

The application you should run is:

```text
think-vision-app/
├── backend/
├── frontend/
└── ...
```

The **Deploy Pipeline** functionality is part of this application.

### Old pipeline

The directory:

```text
cicd-pipeline-main/
```

contains the older GitHub-Actions-triggered ML pipeline.

It is a standalone script/pipeline setup with its own:

```text
requirements.txt
```

It is **not required to run the main Think Vision application**.

---

## 8. Troubleshooting

### MongoDB is not running

Check:

```bash
docker ps --filter name=thinkvision-mongo
```

Start it if necessary:

```bash
docker start thinkvision-mongo
```

### Backend dependencies are missing

Make sure the virtual environment is activated:

```bash
cd backend
source .venv/bin/activate
```

Then reinstall:

```bash
pip install -r requirements.txt \
  --extra-index-url https://d33sy5i8bnduwe.cloudfront.net/simple/ \
  --extra-index-url https://download.pytorch.org/whl/cpu
```

### Port 3000 is already in use

Do not kill the existing process.

Start the frontend on `4321`:

```bash
PORT=4321 yarn start
```

### Backend is running but frontend cannot connect

Check that the backend is running on:

```text
http://localhost:8000
```

and that `frontend/.env` contains:

```text
REACT_APP_BACKEND_URL=http://localhost:8000
```

---

## 9. Expected Running Setup

Once everything is started, the setup should look like:

```text
                    ┌─────────────────────┐
                    │    Think Vision App  │
                    │      Frontend        │
                    │     Port: 4321       │
                    └──────────┬──────────┘
                               │
                               │ HTTP API
                               ▼
                    ┌─────────────────────┐
                    │       Backend       │
                    │     Port: 8000      │
                    │      FastAPI        │
                    └──────────┬──────────┘
                               │
                               │ MongoDB
                               ▼
                    ┌─────────────────────┐
                    │       MongoDB       │
                    │ Docker Container     │
                    │ thinkvision-mongo    │
                    └─────────────────────┘
```
## Running tests safely

The smoke and pytest suites register `test_*` users and send `test_*` invites to the API they are pointed at, and they do not clean up. Point them at a throwaway database (set a separate `DB_NAME` in `backend/.env`, for example `thinkvision_test`), not your working one. To remove leftovers from a database:

```bash
cd backend
python scripts/cleanup_test_data.py          # dry run
python scripts/cleanup_test_data.py --apply  # deletes test_* users, their workspaces and pending test_* invites
```
