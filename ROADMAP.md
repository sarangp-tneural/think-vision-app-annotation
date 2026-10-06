# ThinkVision Deploy Pipeline — Implementation Roadmap

> Sequences the build of the **Deployment Pipeline Extension** (guided SSH/SCP remote
> training → testing → human approval → deploy, with one-click rollback) against the
> *actual current* ThinkVision codebase. This supersedes `cicd-pipeline-main/`'s
> git-triggered GitHub Actions pipeline: the `validate → baseline_eval → train → eval →
> decide` *logic* is kept and ported in-process; the git-push/CI *trigger* is replaced
> by a ThinkVision-native wizard.
>
> This document assumes the reader has not seen the original spec — everything needed
> to execute is here.

---

## 0. Current state (what already exists, what doesn't)

- **Backend** is a FastAPI monolith: `backend/server.py` (1677 lines; most Pydantic
  request models are declared inline at lines ~148-253) plus a `backend/routers/`
  package. Each router file exports `register(server_module)`, called from
  `server.py`'s router-registration block (~lines 1614-1639), which wires routes onto
  a shared `api_router` and gives each router access to shared state (`db`, auth
  deps, helper functions) via the passed-in module, referenced inside as `s`
  (e.g. `s.db`, `s._project_access_check`).
- **Database** is MongoDB via Motor (async) for request handlers. Long-running work
  (real YOLO training, video frame extraction) runs in a background thread using a
  **separate, synchronous** PyMongo client, because Motor isn't thread-safe — see
  `_train_yolo_sync` in `server.py` (~lines 1229-1427). This thread-owns-its-own-client
  pattern is what every new pipeline-stage worker below follows.
- **Frontend** is React 19 + `react-router-dom` v7 (CRA/CRACO), shadcn/ui + Tailwind,
  flat `frontend/src/pages/`, a single shared axios client (`frontend/src/lib/api.js`)
  with JWT bearer auth and a `fileUrl()` helper for authenticated image URLs. No
  Redux — each page manages its own `useState`/`useEffect`.
- **`/projects/:pid/deploy` already exists** → `frontend/src/pages/ModelDeploy.jsx`,
  already linked from the project page. Today it is **local-only**: two tabs, "Real
  Training" and "Simulated" (`ModelDeploy.jsx:160-168`), kicking off in-process YOLO
  training (`POST /projects/{pid}/train-real`) and polling `GET /models/{mid}` every
  5s (`ModelDeploy.jsx:58-89`). `POST /models/{mid}/activate`
  (`backend/routers/training.py:64-77`) just flips one model's `is_active` flag to
  power the app's existing in-app auto-labeling — there is no remote host, no SSH, no
  public inference endpoint, no weight export anywhere in the app today.
- **`backend/routers/training.py`** is the direct precedent for the new
  `deployment.py` router: same `register(s)` pattern, same
  create-doc/background-thread/poll-by-id shape used by `train_real_model` →
  `_train_yolo_sync` → `GET /models/{mid}`.
- **`cicd-pipeline-main/`** (repo root, standalone, separate `requirements.txt`) is
  the old pipeline being superseded:
  - `validate_data.py` — sanitizes `data.yaml`, checks `train/valid/test/{images,labels}`
    pairing, checks label class-ids are in bounds.
  - `baseline_eval.py` / `eval.py` — run `model.val()` for mAP50/mAP50-95.
  - `train.py` — full YOLO hyperparameter block (SGD, cosine LR schedule,
    mosaic/mixup/copy_paste augmentation) fine-tuning from a production checkpoint.
  - `decide.py` — the deploy gate: `if retrained.map50 >= baseline.map50: deploy`.
  - `notify.py` — Gmail SMTP alerts (to be replaced by the app's existing Resend
    integration, not reused as-is).
  - `.github/workflows/mlops.yaml` + `Dockerfile` — the `repository_dispatch`-triggered
    CI runner being fully dropped in favor of in-app SSH orchestration.
  - **No paramiko/SSH/SCP exists anywhere in this repo today** — the SSH layer is
    entirely new code.
- **`SPECIFICATION.md`** (repo root) is the reverse-engineered spec of the current
  app (explicitly excludes `cicd-pipeline-main/`). Its backlog/limitations sections
  (§12–§13) flag: no persistent job queue, no crash/restart recovery for background
  jobs, no protection against concurrent runs, no WebSocket/SSE (everything is
  interval polling). This roadmap does **not** attempt to fix those gaps wholesale —
  see §7 below for the one narrow exception this feature needs.

---

## 1. Overview

Add a **Deploy pipeline**: a per-project, human-gated, three-mode (`bootstrap` /
`fresh_production` / `merge`) run that uploads an annotated dataset to a
user-specified training server over SSH, trains or merge-trains a YOLO model there,
downloads the result, tests it locally against the run's own held-out test split
and the currently-active model, waits for in-app approve/reject, and — for
production runs — deploys the approved weights to a client's live server with
automatic backup rotation and one-click rollback. SSH/SCP credentials are supplied
fresh at each step that needs them and are never persisted.

This is additive to, not a replacement for, the existing local "Real Training" tab
in `ModelDeploy.jsx` — local training needs no remote host at all and stays exactly
as it is. The new pipeline becomes a third tab on the same, already-existing
`/projects/:pid/deploy` route.

---

## 2. Architectural decisions

| Decision | File(s) | Why |
|---|---|---|
| New router, same pattern as `training.py` | `backend/routers/deployment.py`, registered in `server.py`'s existing router block (~1614-1639) | Zero new wiring conventions; matches how every other feature-area router already attaches |
| Standalone SSH/SCP module, no FastAPI/Mongo imports | `backend/ssh_helper.py` | Credentials must never touch persistence or logging — keeping this module pure makes that an invariant, not a discipline |
| Ported ML-ops logic as importable functions | `backend/pipeline_logic.py` | Replaces `cicd-pipeline-main`'s subprocess/Docker/CI orchestration with in-process calls; keeps the validate/train/eval/decide *logic* everyone already trusts |
| Per-stage background workers, own sync Mongo client | `backend/deployment_worker.py` | Mirrors `_train_yolo_sync`'s existing thread-owns-its-client pattern exactly — no new concurrency model to learn |
| New `backend/schemas/` package for this feature's Pydantic models | `backend/schemas/deployment.py` | Scoped, justified exception to "everything inline in `server.py`": ~15 new models used only by this feature would make the already-large inline block (currently ~105 lines) materially harder to scan for no cross-file benefit. `deployment.py` still pulls `db`/helpers via `register(s)` like every other router — only the model *definitions* move out. |
| Extend `ModelDeploy.jsx`, don't replace or re-route | New `frontend/src/pages/DeployPipeline.jsx`, rendered as a third `<TabsContent value="pipeline">` inside `ModelDeploy.jsx`'s existing `<Tabs>` (alongside `real`/`simulated`, `ModelDeploy.jsx:160-168`) | `/projects/:pid/deploy` and its nav links already exist; local training is a distinct, still-needed capability, not something this feature supersedes |
| No generic wizard/stepper component | — | Grep confirms nothing like it exists in the codebase to extend. Build `DeployPipeline.jsx`'s steps keyed directly off `run.status` (the state machine *is* the UI step) rather than inventing a separate frontend step index — one source of truth |
| `paramiko` for SSH/SFTP, no extra `scp` package | `backend/requirements.txt` | `paramiko.SFTPClient.put()/get()` natively accept a `callback=` for byte-level progress, which the progress rail needs anyway — one fewer dependency |

---

## 3. Data model additions

### `deployment_pipelines` (one per project — the deploy target's persistent config)

| Field | Notes |
|---|---|
| `id`, `project_id`, `created_by`, `created_at`, `updated_at` | |
| `remote_host`, `remote_port` (default 22), `remote_username` | Connection metadata only — **never** password/key material |
| `remote_base_model_path` | Base checkpoint used by `bootstrap`/`fresh_production` |
| `remote_production_model_path` | The currently-live model file on the remote host |
| `remote_data_yaml_path` | Canonical server-side `data.yaml`, diffed against at `class_check` |
| `remote_workdir` | Scratch directory for per-run uploads |
| `last_run_id`, `last_deployed_run_id` | |

Backups are addressed by convention — `{remote_production_model_path}.bak1` /
`.bak2` — no separate field needed.

### `pipeline_runs` (one per execution of the wizard — run history)

| Field | Notes |
|---|---|
| `id`, `project_id`, `pipeline_id` | |
| `run_type` | `bootstrap` \| `fresh_production` \| `merge` — decided automatically at run-start based on `deployment_pipelines.has_bootstrap_model`/`has_production_model` flags (or their equivalent derived from run history) |
| `status` | `draft → uploading_data → class_check → training_remote → downloading_model → testing → awaiting_approval → deploying → completed`, with `failed` reachable from any stage and `rejected` reachable from `awaiting_approval` |
| `busy` | `true` while a background thread currently owns this run. **Resolves an ambiguity in the source spec**, which uses one status value for both "this stage is running now" and "this stage finished, waiting for the next user action" — `busy` lets the frontend distinguish "uploading now" from "uploaded, click Next" without inventing extra status enum values |
| `stage_history` | `[{stage, status, started_at, finished_at, error?}]` — audit trail for the run-history UI |
| `dataset_export` | `{train_count, valid_count, test_count, test_image_ids, remote_upload_path}` |
| `class_check` | `{local_classes, remote_classes, match, diff, checked_at}` |
| `training` | `{base_checkpoint_ref, remote_run_dir, hyperparams_used, progress_pct, log_tail, started_at, finished_at}` |
| `candidate_model` | `{local_model_id, weights_path, metrics: {mAP50, mAP50_95, precision, recall}}` |
| `baseline_comparison` | `{baseline_model_id, baseline_metrics, delta_map50, decision_gate_passed}` |
| `testing` | `{sample_predictions: [{image_id, candidate_boxes, baseline_boxes, ground_truth}], tester_notified_at}` (capped at 20 images) |
| `approval` | `{decision, decided_by, decided_at, note}` |
| `deploy` | `{current_became_bak1, bak1_became_bak2, bak2_dropped, deployed_at, deployed_model_id}` |
| `error` | Populated on `failed`, truncated ~500 chars |

Rollback is modeled as its own `pipeline_runs` doc with `run_type: "rollback"`,
jumping straight to `deploying → completed`/`failed` (no upload/train/test/approve
stages) — this keeps it visible in run history without a parallel data shape.

### `models` collection — one new distinction

New value `type: "pipeline_candidate"`, shaped identically to today's `"real"`
model doc (`status`, `weights_path`, `final_mAP`, `precision`, `recall`,
`metrics_full`, `is_active`). Reusing the existing shape means the existing,
**unmodified** `POST /models/{mid}/activate` (`routers/training.py:64-77`) can
activate a pipeline candidate exactly like a locally-trained one — this is the
mechanism M8 relies on for local activation.

**Activation rule (confirmed):**
- A **bootstrap** run's approved candidate is activated **locally only** — it
  powers in-app auto-labeling for the rest of the seed set and never touches a
  client server. Bootstrap and production models are conceptually distinct; a
  bootstrap model must never be treated as good enough to deploy, and approving one
  must never silently overwrite the active auto-label model with something worse.
- A **fresh_production** or **merge** run's approved-and-deployed candidate is
  activated **both** remotely (SCP'd to the client server) **and** locally (via the
  same `activate` call) — so that if this client later sends another batch
  (a future `merge` run), that batch gets auto-labeled in ThinkVision using the
  best available model, not a stale bootstrap model.
- Rollback does not change local activation state — it only affects what's live on
  the remote server.

---

## 4. New endpoints

| Method & path | Behavior |
|---|---|
| `POST /projects/{pid}/pipeline/runs` | Decides `run_type` automatically from pipeline/run history, creates the `pipeline_runs` doc (`status: draft`), returns which type it picked for the wizard to confirm |
| `POST /pipeline/runs/{rid}/stage/{stage}` | Executes/retries one stage; body carries whatever that stage needs (SSH creds, epochs, tester email) — never persisted beyond the request. 409 if another run on the same `pipeline_id` has `busy: true` (see §7) |
| `GET /pipeline/runs/{rid}` | Poll run status/progress — same idiom as `GET /models/{mid}` |
| `GET /projects/{pid}/pipeline/runs` | Run history list |
| `POST /pipeline/runs/{rid}/approve` | Valid only when `status == awaiting_approval`; records the decision |
| `POST /pipeline/runs/{rid}/reject` | Valid only when `status == awaiting_approval`; sets `status: rejected`, terminal. **Must make zero SSH calls** |
| `POST /projects/{pid}/pipeline/rollback` | Body: SSH creds. Creates a new `run_type: "rollback"` doc and performs the swap |

Guard: `stage/deploying` returns 400 for any run with `run_type == "bootstrap"` —
bootstrap runs terminate at `awaiting_approval` per the state machine.

---

## 5. Milestones (dependency-ordered — no calendar estimates)

### M0 — Concurrency guard
One narrow addition beyond the base spec: reject (409) a `stage/{stage}` call if
another run on the same `pipeline_id` is currently `busy`.
**Why here, why now:** unlike the rest of the app's already-accepted "no crash
recovery" gap (a stuck training job just sits at `training`), `deploying`/rollback
perform **destructive, non-idempotent** remote file rotation — two concurrent
deploys against the same target can corrupt `.bak1`/`.bak2` state in a way nothing
else in this app risks. This is a single extra Mongo query, not a queue rewrite.
- **Files:** `backend/routers/deployment.py` (guard helper), `pipeline_runs.busy`/`stage_history`.
- **DoD:** `backend/tests/test_deployment_pipeline.py::test_concurrent_stage_call_rejected` — two rapid stage calls on the same `pipeline_id`, second gets 409.

### M1 — `ssh_helper.py`
Foundational — every later stage depends on it, and it's the only piece whose
correctness needs a real remote target, so start its manual-verification loop early.
- **File:** `backend/ssh_helper.py` — `connect(host, port, username, pem_key_text, password=None, timeout=15)` (context manager), `exec_command(client, cmd, timeout=None) -> (exit_code, stdout, stderr)`, `upload_file`/`download_file`, `upload_dir`/`download_dir` (recursive SFTP, `callback=` for progress), `test_connection(...)`.
- **File:** `backend/requirements.txt` — add `paramiko`.
- **File:** `backend/schemas/deployment.py` — `SSHCredentials` model (host, port=22, username, pem_key, password=None); document prominently that instances must never be logged or persisted.
- **DoD (automated):** `backend/tests/test_ssh_helper.py`, mocking `paramiko.SSHClient`, asserting correct connect/exec/upload/download call shapes.
- **DoD (manual, required):** point `ssh_helper` at a throwaway `sshd` (local container or spare VM); confirm connect/exec/upload/download round-trip real bytes.
- **Do in parallel:** prototype and unit-test M8's backup-rotation swap logic against a fake in-memory remote filesystem right after this lands — it is the highest-risk code in the whole feature (see §7), and there's no reason to wait until the end of the sequence to find an ordering bug in it.

### M2 — Split-export mode
- **File:** `backend/server.py` — extend `export_dataset` (~lines 988-1179) with a `train/valid/test` split format; extract a shared `_build_split_dataset_dir(pid, project, train_pct, valid_pct, test_pct) -> (local_tmp_dir, test_image_ids)` that writes real files to a temp directory (the pipeline needs an actual directory tree to SCP, not the existing in-memory ZIP stream used by manual export).
- **File:** `frontend/src/pages/Versions.jsx` — surface the new split format as an export option (this, not `ProjectDetail.jsx`, is where the export-format UI actually lives).
- **DoD:** `test_deployment_pipeline.py::test_split_export_structure` — all three split dirs exist, image/label pairing is 1:1 in each, `test_image_ids` matches what was actually written to `test/`.

### M3 — `pipeline_logic.py` + `class_check` stage
- **File:** `backend/pipeline_logic.py`:
  - `validate_dataset_dir(dataset_dir, nc) -> list[str]` — ported from `cicd-pipeline-main/validate_data.py`'s pairing + label-bounds checks (drop the Roboflow-path-sanitizing part; our own exporter already writes correct paths).
  - `diff_classes(local_classes, remote_yaml_text) -> dict` — new logic: **exact match on both set and order** (order encodes class index, which training depends on).
  - `render_train_eval_script(params) -> str` — combines `train.py`'s hyperparameter block (SGD, cosine LR, augmentation) with `eval.py`'s `model.val()` call, parameterized by starting checkpoint.
  - `evaluate(weights_path, data_yaml, split) -> {mAP50, mAP50_95}` — ported from `eval.py`/`baseline_eval.py`.
  - `decide_deploy(baseline_map50, retrained_map50) -> bool` — ported verbatim from `decide.py`'s gate.
- **File:** `backend/routers/deployment.py` — `stage/class_check` handler: SSH `cat` the remote `data.yaml`, parse (PyYAML), call `diff_classes`. Mismatch → `status: failed` with the diff in the response.
- **DoD (automated):** exact match passes; same-set-different-order fails; missing/extra class fails with correct diff payload.
- **DoD (manual):** real remote host with a deliberately mismatched `data.yaml` → run lands in `failed` with a readable message.

### M4 — `uploading_data` stage
- **File:** `backend/deployment_worker.py` — `_pipeline_uploading_data_sync`: build the split dir (M2), `ssh_helper.upload_dir` to `{remote_workdir}/{run_id}/dataset`, populate `dataset_export`.
- **DoD (automated):** mocked-SSH test asserts `dataset_export` populated and `busy` flips correctly; exercises M0's 409 guard concretely.
- **DoD (manual):** real SCP to a throwaway host — confirm the directory tree lands correctly.

### M5 — `training_remote` stage
- **File:** `backend/deployment_worker.py` — `_pipeline_training_remote_sync`. Renders the script (M3), SCPs it + the uploaded `data.yaml`, `exec_command`s it remotely, streams `channel.recv()` while polling `exit_status_ready()`, regex-scrapes Ultralytics' `Epoch X/Y` lines into `training.progress_pct`/`log_tail` — the SSH-log-scraping analogue of `server.py`'s existing epoch-callback pattern inside `_train_yolo_sync`.
- **Branching:** `bootstrap`/`fresh_production` start from `deployment_pipelines.remote_base_model_path`; `merge` continues from `remote_production_model_path` (already on the remote host — no extra upload needed).
- **DoD (automated):** mock `exec_command` returning canned epoch-progress stdout, assert progress-parsing math; assert bootstrap/fresh_production vs merge render different starting-checkpoint args.
- **DoD (manual):** real short (1-2 epoch) training run on a real remote host — confirm `best.pt`/`metrics.json` land in the expected remote run dir.

### M6 — `downloading_model` stage + candidate registration
- **File:** `backend/deployment_worker.py` — `_pipeline_downloading_model_sync`: SFTP-download `best.pt` (fallback `last.pt`, mirroring the existing in-app pattern) + `metrics.json`; upload weights into object storage at `{APP_NAME}/projects/{project_id}/pipeline_runs/{run_id}/candidate.pt`; insert a `models` doc with `type: "pipeline_candidate"`, `is_active: false`.
- **DoD (automated):** mocked download/upload round-trip; `models` doc fields verified.
- **DoD (manual):** load the actually-downloaded `.pt` locally with `YOLO(path)` as a smoke check — a partial/corrupted SFTP transfer would otherwise only surface later during testing.

### M7 — `testing` stage
- **File:** `backend/deployment_worker.py` — `_pipeline_testing_sync`: run local inference (reuse the existing in-app inference call) with the candidate over the run's own held-out `test_image_ids`, and separately with the currently-active model over the same set; run `pipeline_logic.evaluate()` for both against the exported `test/data.yaml`; feed both mAP50s into `decide_deploy()` → `baseline_comparison.decision_gate_passed`. Cap `sample_predictions` at 20 images.
- **File:** `backend/server.py` — clone `send_invite_email` into `send_tester_notification_email(to_emails, project_name, run_id, app_url)` right beside it, reusing the existing owners/admins/reviewers recipient lookup already used for review notifications. Link to `{APP_URL}/projects/{pid}/deploy?run={rid}`.
- **DoD (automated):** mocked mAP comparisons both directions; mocked `resend.Emails.send` call asserted; sample cap enforced.
- **DoD (manual):** open the actual sent email once M9 exists, confirm the `?run=` link opens the right run.

### M8 — `awaiting_approval` / `deploying` / rollback
The highest-risk milestone in the feature — see §7 for why.
- **File:** `backend/routers/deployment.py` — `approve`/`reject` endpoints.
- **File:** `backend/deployment_worker.py` — `_pipeline_deploying_sync`:
  - **Bootstrap runs:** blocked with 400 before reaching this stage (per §4's guard). On approval, activate the candidate **locally only** via the existing, unmodified `POST /models/{mid}/activate`.
  - **fresh_production/merge runs:** on approval, over SSH: drop `.bak2` → move `.bak1`→`.bak2` → move current production file→`.bak1` → upload candidate as the new live file → update `deploy.*` + `deployment_pipelines.last_deployed_run_id` → **also** call the local `activate` endpoint so the deployed model becomes ThinkVision's active auto-label model.
- **File:** `backend/deployment_worker.py` — `_pipeline_rollback_sync`: **download `.bak1`'s bytes into a buffer first**, then rotate current→`.bak1` (single swap, no `.bak2` cascade), then upload the buffered old-`.bak1` bytes into the production path. Getting this ordering wrong — overwriting `.bak1` before reading it — is the single easiest bug in the whole feature.
- **DoD (automated):** unit tests against a mocked in-memory remote filesystem verifying exact byte-content placement after **two sequential deploys plus one rollback**; bootstrap-deploy-attempt → 400; approve on wrong status → 400/409; `reject` asserted to make **zero** SSH calls.
- **DoD (manual, required):** full real run against a throwaway host — deploy A, verify `.bak1 == old current`; deploy B, verify `.bak2 == A's old backup` and `.bak1 == A's deployed model`; rollback, verify current is restored to A's deployed model and `.bak1` holds what was overwritten. Walk this by hand with file hashes/timestamps.

### M9 — Frontend wizard (built last, against working endpoints)
- **File:** `frontend/src/pages/DeployPipeline.jsx`, rendered as a third `<TabsContent value="pipeline">` inside `ModelDeploy.jsx` (alongside the existing `real`/`simulated` tabs).
- Run-type banner + "Start New Run"; always-visible Rollback button (gated on `deployment_pipelines.last_deployed_run_id`); run history list (adapt the existing row idiom already used for training runs).
- Steps keyed 1:1 off `run.status` (not a separate frontend step index): SSH form (PEM textarea styled like the existing `font-mono` textarea used for bulk-classes input; host/port/username/password like the existing `Auth.jsx` password input); progress rail reusing the existing `h-1 bg-[#27272A]` + `bg-primary` fill idiom and 5s `setInterval` polling pattern (`ModelDeploy.jsx:58-89`); side-by-side testing view (new — two `fileUrl()` image panes with box overlays, a simplified read-only version of the annotator's rendering technique); approve/reject cloned from the existing annotation review pattern (green Approve / destructive-outline Reject with a rejection-reason prompt).
- **File:** `frontend/src/constants/testIds/deployPipeline.js`, re-exported per the existing convention.
- **DoD:** manual QA checklist (the repo has no frontend automated test framework — `tests/` is backend-pytest-only, don't introduce one just for this): bootstrap run cannot reach `deploying`; full fresh_production run end-to-end against a throwaway host; rollback appears as a new history entry; rejected run shows notes with no further actions; M0's 409 surfaces as a friendly toast, not a raw error dump.

---

## 6. Testing strategy summary

| Milestone | Automated (pytest, mocked SSH) | Manual (real SSH target required) |
|---|---|---|
| M0 | ✅ concurrency guard | — |
| M1 | ✅ call-shape assertions | ✅ real connect/exec/upload/download round-trip |
| M2 | ✅ split structure/pairing | — |
| M3 | ✅ class-diff cases | ✅ real mismatch → readable failure |
| M4 | ✅ upload state transitions | ✅ real directory upload |
| M5 | ✅ progress parsing, branch selection | ✅ real short training run |
| M6 | ✅ download/register round-trip | ✅ smoke-load the downloaded weights |
| M7 | ✅ mAP comparison, email call, sample cap | ✅ verify sent email + deep link |
| M8 | ✅ two-deploys-plus-rollback byte verification, guard rails | ✅ full real deploy/rollback cycle, hash-verified |
| M9 | — | ✅ full QA checklist |

New test file: `backend/tests/test_deployment_pipeline.py`, following the existing
precedent of `test_iteration9_new_endpoints.py` / `test_iteration11_routers_nudge_obb.py`.

---

## 7. Risk notes / explicitly out of scope

- **Highest risk in the feature is M8's backup rotation and rollback**, not
  upload/class_check as a purely dependency-ordered read might suggest — it's
  destructive, stateful, and non-idempotent against a real remote filesystem, with
  an easy-to-miss ordering bug (must read `.bak1` before overwriting it during
  rollback). Prototype and unit-test that swap logic early (right after M1), in
  parallel with M2-M7, rather than discovering issues only at the end.
- **This roadmap deliberately does not** add a persistent job queue or
  crash/restart recovery for pipeline runs — that matches the rest of the app's
  already-accepted background-job limitations (`SPECIFICATION.md` §12-§13: no
  queue, no restart recovery, no WebSocket, all interval polling). The **one**
  exception is M0's narrow concurrency guard, justified specifically because this
  feature — uniquely among the app's background jobs — performs destructive remote
  file rotation where a race condition causes real data loss, not just a stuck
  status.
