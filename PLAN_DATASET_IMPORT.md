# PLAN: Dataset import and smart merge (ThinkVision)


## 0. Context

Datasets already live on our training servers. We need to (1) pull a copy into ThinkVision to view, annotate and review it, and (2) push back only what is new or edited, without overwriting anything unchanged.

Facts from the user's answers:
- Server datasets have **fixed splits** (`train/images`, `val: valid/images`, `test/images`, per `data.yaml`; note the key is `val` but the dir is `valid`). `names` is an **index-keyed dict**, `nc: 16`. `path:` is an absolute server path.
- Sample labels are **YOLO detect** (5 tokens). Roboflow-style filenames (`..._jpg.rf.<hash>.txt`). Plan supports seg/OBB/COCO/VOC too.
- Imported labels: **approved by default**, optional `submitted`.
- Scale: not provided. I design for ~50k images / several GB per dataset (staged on disk, streaming, bulk inserts). SSH credentials are supplied per action, never stored.

---

## 1. Verified current state (from code, not spec.md)

### 1.1 `uploading_data` (`backend/deployment_worker.py:374-510`, route `routers/deployment.py:471-489`)
- Export set: `images.find({project_id, annotated: True, review_status: "approved"}).limit(2000)` in `_build_split_dataset_dir_sync` (`:77-150`). **Silent 2000-image cap** (same in `server.py` `export_dataset` `.to_list(2000)`, versions, `_build_split_dataset_dir`).
- Naming: `{image.id}.{ext}` and `{image.id}.txt` (`:126-128`). Original filename is lost. Imported images would therefore never match remote files by name.
- Split: `random.shuffle` unseeded, 70/20/10 by request (`:100-109`). Nothing persisted; `images` has no `split` field. Same in `_build_split_dataset_dir` (`server.py:1397`) and `_train_yolo_sync` (own 80/20).
- Labels: `_yolo_label_line` (`:47`; duplicate in `server.py:1365`) writes axis-aligned `cls cx cy w h`; polygons/ellipses become boxes, rotation dropped, unknown labels skipped. `_validate_split_labels` (`pipeline_logic.py:41`) rejects anything but 5 tokens, so seg/OBB would fail class_check.
- **`new`**: target `{remote_workdir}/dataset`; existing dir is renamed `_old_{run_id[:8]}` (never deleted); full `upload_dir` (per-file SFTP put).
- **`merge`**: reads remote yaml, requires every local class in remote `names` (`:433-438`), resolves split dirs (`pipeline_logic.resolve_yaml_split_dirs`; missing `test` falls back to `val`), writes labels in remote class order (`label_classes=remote_names`), then `_upload_into_existing`.
- **`reuse`**: uploads nothing; counts remote files with `find | wc -l`; `test_image_ids` = sample of up to 50 local approved images (may overlap remote training data).
- **Bug found:** `deployment_worker.py` defines `_ensure_usable_yaml`, `_make_progress_reporter`, `_count_local_files`, `_read_remote_text`, `_count_remote_files`, `_upload_into_existing` **twice** (`:153-259` and `:261-349`); the second wins. The live `_upload_into_existing` (`:325`) does `sftp.stat` per exact filename and `put` only if missing: it **never overwrites**, but (a) skips by exact name only, (b) re-uploads an already-present image into a different random split, i.e. **split leakage**, (c) never propagates label edits, (d) is N+1 round trips. The docstring'd stem-matching/label-refresh version is dead code, and `test_deployment_worker.py::test_upload_into_existing_skips_known_images_refreshes_labels_and_keeps_split` (`:767`) currently **fails**.
- Answer to "does merge skip images already on server?": only by exact filename; since we name files by uuid, it effectively re-uploads everything the first time and skips only our own prior uploads. It cannot overwrite (good) but cannot update labels.

### 1.2 `inspect_remote` (`routers/deployment.py:341-361`, `_inspect_remote_sync :138-186`)
Takes `InspectRemoteRequest` (SSH creds + `remote_workdir`, `venv_path`, `extra_yaml_path`); saves non-secret form fields; `find` yaml/`*.pt` candidates. Returns `{workdir, found, venv_ok, datasets[{data_yaml_path, names[], splits{train|valid|test:{images_dir,image_count}}}], models[{path,size,mtime}], + legacy aliases}`. Frontend (`DeployPipeline.jsx:327-390`) stores it as `remoteInfo`, auto-switches `dataset_mode` new→merge when a dataset is found, lists datasets/models. It does not take the `busy` lock. Reusable for dataset discovery in the import dialog.

### 1.3 Classes
- `projects.classes: List[str]` (`server.py:240`); index = position. Names lowercased/trimmed on bulk add + rename (`.strip().lower()[:40]`), but single add does not normalize (`projects.py:96-106`).
- **Alphabetical re-sort:** `routers/images.py:197-200` (`sorted(existing | new_labels)` in `PUT /images/{id}/annotations`). This is the only index-shifting write on save. Other shifters: `DELETE /classes/{label}` (`$pull`, `projects.py:131`) and merge-via-rename (`PATCH`, `:142-176`). Rename without merge keeps position. Batch auto-label uses `$addToSet` (appends; `server.py:871`).
- Index consumers: export yolo/yolo_obb/coco (`server.py:1180-1362`; unknown labels fall back to **class 0** via `label_to_idx.get(..., 0)`), `_yolo_label_line`, `_train_yolo_sync` (snapshots `models.classes`), `deployment_worker` (`label_classes` override; `dataset_export.classes`), `pipeline_logic.diff_classes` (order-exact), auto-label (`active.get("classes") or classes`), frontend colors (`Annotator.jsx:127`, index-derived) and rollback `[...classes,label].sort()` in `ProjectDetail.jsx:164-168`.
- Rename gap: `models.classes` and `pipeline_runs.dataset_export.classes` are not updated by rename.

### 1.4 Images and storage
- Only upload endpoint: `POST /projects/{pid}/images` (`images.py:18-46`): one file per request, whole file in RAM, 10 MB cap, stored via Emergent HTTP object store (`put_object`, path `…/projects/{pid}/{img_id}.{ext}`), served by `GET /files/{path}` by `storage_path` lookup. Frontend uploads sequentially (`ProjectDetail.jsx:251-269`).
- Image doc has no `split`, hash, width/height or thumbnail. No dedupe anywhere (only `images.storage_path` index). Annotations embedded in `images.annotations` as `AnnotationBox` (`extra="allow"`): `type, label(NAME), x,y,w,h (top-left, normalized), points, rx, ry, rotation (radians, clockwise about center)`.
- No importers exist. Reusable: `pipeline_logic.names_as_list/_normalize_names/resolve_yaml_split_dirs/effective_yaml_root/looks_like_dataset_yaml`; the YOLO-OBB corner math in `export_dataset :1244-1260` (inverse needed).

### 1.5 Review workflow (`server.py:509-556`, `images.py:69-152`)
- `LOCKED_STATUSES=("submitted","approved")`. `_image_edit_check`: reviewer 403; submitted/approved 409; owner/admin may edit only if `assigned_to` empty/self; others need `assigned_to==uid` and status `assigned|rejected`.
- Transitions: `/assign`, `/bulk-assign` (not on locked), claim-on-save, `/submit` (assignee only, needs ≥1 annotation, from assigned/rejected), `/review` (owner/admin/reviewer; **must be `submitted`**; reject needs notes; self-review blocked if another reviewer exists).
- **No endpoint sets `approved` directly, and no endpoint moves an approved image back.** Consequences: (1) importing as approved needs an explicit, justified bypass; (2) approved imported images are **uneditable**, so "edited imported labels" cannot happen without a new reopen transition (see Phase 2). `delete_image` is 409 on approved.
- Export/versions/training/deploy all filter `review_status=="approved"` (+`annotated: True`; training also `annotations.0`). Manager analytics use `annotated_by/annotated_at/reviewed_at`; retrain nudge counts `reviewed_at > last model created_at`.

### 1.6 Jobs, locks, secrets
- Background pattern: `BackgroundTasks` + `asyncio.to_thread` with sync pymongo (video, training, deploy) or pure async (batch auto-label); progress in a doc (`batch_jobs`, `videos`, `models`, `pipeline_runs.upload_progress`); UI polls (1 s for batch jobs). No restart recovery anywhere: orphaned `queued/processing` and `busy: True` stay forever.
- `busy` (`deployment.py:396-416`): `find_one` then `update_one`, **non-atomic**; no startup reset.
- Secrets: `SSHCredentials` in request body only; `_save_form` whitelists `FORM_KEYS`; `ssh_helper` has no db/logging. No public listing/stat helper exists (only `upload_*`, `download_*`, `exec_command*`, `remote_exists/remove/rename`, `resolve_remote_path`).
- Tests: `backend/tests/` with `_inprocess.py` (`get_client`, `fresh_db`, per-xdist-worker DB), `pytest -n 2` (use `-n 0` serial), run with `../.venv/bin/python`.
- Migration convention: `backend/scripts/*.py`, motor, docstring usage, `--apply` flag (default dry run), JSON backup before writes, idempotent filters (model: `backfill_review_status.py`). Indexes are created in the `@app.on_event("startup")` handler (`server.py:2001-2025`).

### 1.7 spec.md discrepancies (relevant ones)
1. §7.9/§12: omits `format=split` export (random split on demand) and says merge skips known images; that logic is dead code.
2. `/projects/:pid/deploy` renders `ModelDeploy.jsx`, which embeds `DeployPipeline` as a tab (`ModelDeploy.jsx:674`); spec calls ModelDeploy "older".
3. Omits `PATCH /projects/{pid}/classes/{label}` (rename/merge), `class_renamed` activity, `gemini_*` project settings, `labeled_count`.
4. Polling is 1 s (not 2.5 s); batch auto-label is a pure-async coroutine, not `to_thread`.
5. §14 "data-testid / testIds registry": registry exists but most pages use inline ids; `Versions.jsx:63` uses raw `fetch`.
6. `UploadingDataRequest` comment says `{workdir}/{run_id}/dataset`; code uses `{workdir}/dataset`.
7. Training can use CUDA (`server.py:1546`), "CPU only" is outdated.
8. Spec never mentions: no image hash/dimensions/split, 2000-image caps, duplicate worker helpers, failing test, non-atomic `busy`.

---

## 2. Design decisions (recommendation first)

### D1. Class locking
- **Recommended:** keep `classes: List[str]` (zero-break) and make it **append-only everywhere**; add `classes_locked: bool` and `class_origin: {source, data_yaml_path, locked_at}`. Alternative (explicit `{id,name}` objects) is cleaner long term but breaks every consumer; rejected for now. If ever needed, `class_ids` can be added alongside.
- Rules: (a) remove the sort at `images.py:197-200` for **all** projects (append new labels in first-seen order); (b) when `classes_locked`: save with unknown label → **422** `{unknown_labels:[…]}` (user must add the class explicitly, which appends); delete and merge-rename → **409**; plain rename allowed (index preserved) but flagged `class_name_drift` for the merge-back diff; (c) export/COCO/worker: unknown label → skip and count (no more silent class-0 fallback); (d) auto-label Gemini path may only emit allowed classes when locked.
- **Class mapping on import (decided):** never blocks. The server `names` (index-keyed dict sorted by int key; gaps rejected) is the authority for server-side indices; the project list is never re-sorted. The dry-run returns `class_mapping: [{server_idx, server_name, action: match|new|skip|replace, target}]` and the UI shows a mapping screen where each server class maps to (a) an existing project class, (b) a **new class** (appended to the end of `project.classes`), or (c) **skip these labels**. Default = auto-match by exact (normalized: strip + lowercase) name; unmatched default to "new".
  - If the project has **no annotated images / no boxes at all**, an extra option "replace project classes with the server's order" is offered: `project.classes` becomes the server list verbatim, so project index == server index.
  - Otherwise project order is preserved and the map is stored as `projects.remote_dataset.class_map` (`server_idx -> project label | null`). **Server files always use the server index**: push-back translates project label → server idx through this map (the existing merge already writes labels in remote order via `label_classes`). So "server order wins for index" holds on the wire and in the data.yaml, while the project list stays append-only.
  - **Skipped labels are preserved, not lost:** raw lines for skipped classes are kept in `provenance.skipped_label_lines` and re-emitted verbatim whenever the label file is rewritten on push, so editing an image in ThinkVision can never delete the server's skipped-class boxes. Images whose labels are all skipped import as `unassigned`, `annotated=False`.
  - After commit the project is `classes_locked=true`. The mapping is validated again at commit (project classes may have changed since the dry-run): mismatch → 409, re-run dry-run.
- Migration `scripts/migrate_class_lock.py`: sets `classes_locked:false` where missing; never reorders. Existing projects are unaffected except the sort removal.

### D2. Provenance and image schema (additive; legacy images lack the fields)
Top-level (indexable): `content_hash` (sha256 hex), `split` (`train|valid|test|None`), `width`, `height`, `source` (`upload|video|import`), `group_key` (video id or None).
`provenance` (imports only): `{import_batch_id, origin, origin_key, remote_rel_path, original_filename, original_split, label_format (detect|seg|obb|coco|voc), label_hash_at_import, had_label_file}`.
`origin_key = sha256(origin_id + "|" + rel_path)`: stable idempotency key.
`label_hash` = sha256 of a **canonical** form (class idx, coords rounded to 6 dp, sorted lines) computed from annotations, never from raw bytes. Raw remote float formatting therefore can't cause false diffs.
New collections: `import_batches`, `import_items` (one row per source file: status `imported|duplicate|corrupt|skipped|failed`, `image_id`), `merge_plans`, `merge_plan_items`.

### D3. Review status on import (explicit bypass)
Import is the **only** code path allowed to insert non-`unassigned` images directly. Justification: the workflow has no way to represent "already trusted data". Safeguards: owner/admin only (`_require_not_reviewer` + role check); server-side only; every image carries `provenance.import_batch_id`; an `activity_log` entry `dataset_imported`; `annotated_by`/`assigned_to` left `None` (never attribute imported work to a person or skew manager analytics); `reviewed_by` = importing admin, `reviewed_at` = now (so retrain nudge counts it once; imported batch excluded from nudge via `source=="import"` check, to confirm).
- `approved` mode: `review_status=approved, annotated=True`.
- `submitted` mode: `review_status=submitted, submitted_at=now, assigned_to=None, annotated=True`. Locked as usual; any reviewer/admin can approve/reject via existing `/review` (no self-review issue since `annotated_by/assigned_to` are None). Rejected images then follow the normal `rejected → assigned → submit` loop.
- No label file or empty label file → `unassigned`, `annotated=False` (cannot satisfy submit/training rules; appears in the normal annotate queue).
- **Bulk approve (decided):** `POST /imports/{bid}/approve-all` `{confirm_count}`, **owner/admin only** (reviewers keep approving one by one via `/review`). Scope = images of that batch still `submitted`. The UI confirmation shows the batch size; the server rejects with 409 unless `confirm_count` equals the current number of pending images in the batch (so stale confirmations fail). It reuses the same transition code as `/review` (status check `submitted → approved`, sets `reviewed_by/reviewed_at`), processes in chunks, is idempotent, and writes one `activity_log` entry `import_batch_approved` `{batch_id, count}`. `GET /imports/{bid}` returns `pending_count`. Default import mode stays `approved`; `submitted` is the opt-in re-check path.
- **New transition `POST /images/{id}/reopen`** (owner/admin/reviewer, notes required): `approved → rejected` (+ optional `assigned_to`). Annotator edits, submits, reviewer approves as normal. Does not weaken existing rules; needed so imported labels can ever be edited.

### D4. Split policy
- Imported: `split = original_split` (normalize `val`→`valid`), never reassigned.
- New images: `split` assigned **at first push** and persisted: bucket = `int(sha256(group_key or content_hash)[:8],16)/2**32` against configured ratios (default = ratio of the linked remote dataset; fallback 0.7/0.2/0.1). Deterministic ⇒ identical bytes always same split; `group_key` (video id for frames) keeps all frames of one video in one split. Never moved after assignment.
- Imported images are matched on remote by hash first, so Roboflow-style `.rf.<hash>` names are irrelevant. Known limit: Roboflow augmentations are separate files with different hashes; we preserve their split but cannot detect near-duplicates (perceptual hashing out of scope).

### D5. Import architecture (stage → scan → commit)
- Stage to a server temp dir (`IMPORT_TMP_DIR`, default `/tmp/thinkvision_imports/{batch_id}`): zip streamed to disk (chunked, no `read()`); SSH files pulled via one SFTP session. Credentials live only in the running job's memory; resume asks for them again.
- Scan = dry-run; commit = create images. Commit does not need SSH (reads staging), so credentials are used once.
- Job runner follows the existing pattern (BackgroundTasks + `to_thread` + sync pymongo, like video/training) with progress in `import_batches`. One active import per project via `active: true` + partial unique index. Add startup recovery that flips orphaned `running` batches to `interrupted` (resumable). No queue rewrite.
- Idempotency: unique partial index on `images(project_id, provenance.origin_key)` plus `import_items` lookup; re-run skips already-imported items; deleted-after-import items stay skipped unless `reimport_deleted=true`.
- Image bytes: per-import cap `IMPORT_MAX_IMAGE_BYTES` (default 50 MB; upload endpoint keeps 10 MB); store dims via PIL while hashing; `put_object` through a small thread pool (4). Bulk `insert_many` in batches of 200.

### D6. Smart merge
- Remote hashing without downloading (decided): probe once per plan (`command -v sha256sum`, `command -v python3`). Order: `sha256sum` via `exec_command`; else `python3 -c hashlib`; else **weak mode**. Results cached in remote `.thinkvision_manifest.json` keyed `(relpath,size,mtime_ns)`. Added `ssh_helper.list_dir_attrs` (one sftp session).
  - **Weak mode** (neither tool available): images match by `(basename, size)` against `provenance.original_filename` + stored byte size; `plan.hash_mode = "weak"` and the preview shows a prominent warning that the check is weaker (a same-name/same-size file with different content is treated as present; our own new `{id}.{ext}` files cannot be recognised under another name, so identical content under a different name would be added). Label comparison needs no hash tool in either mode: label files are small, so they are read over SFTP and canonicalized locally. Weak mode requires the existing explicit confirmations and never relaxes the no-overwrite rule.
- Match local↔remote by hash (filename only as a tiebreaker/report). Categories per local approved image: `unchanged` (hash on remote, label hash equal) · `add` (hash not on remote) · `update_label` (imported/pushed image, local `label_hash != label_hash_at_import/last_push`, and remote label hash still equals the recorded one) · `conflict` (remote label changed independently, skipped by default) · `name_collision` (remote file with our target name but different hash, skipped). Remote-only files: reported, never touched.
- Execute rules: images only `put` to a **temp name then `posix_rename`** after verifying target absent; label updates copy the old label to `{remote_root}/.thinkvision_backups/{run_id}/{split}/labels/<name>` first (outside `labels/`), then atomic replace; unchanged files are never written; manifest rewritten last. Labels use the image's `label_format` via the codec. v1 ships **detect only**; the `label_format` field and codec interface exist from day one so seg/OBB (Phase 6) plug in without a schema change. Images with any non-detect label line are never rewritten on push in v1 (reported as `unsupported_format`).
- `dataset_mode` integration: **`new` and `reuse` unchanged. `merge` is re-implemented on the plan** (this also removes the dead duplicate code and the leakage bug). `UploadingDataRequest` gains optional `plan_id`, `confirm_label_updates=false`, `update_class_names=false`. Without `plan_id` merge builds a plan inline and, by default, only adds new images (a strict improvement over today). Label updates and `data.yaml` names extension happen only when explicitly confirmed.
- **`data.yaml` class append (decided):** only when `update_class_names=true` (UI: explicit confirmation checkbox, unchecked by default). Safeguards: (1) back up first to a sibling `data.yaml.bak.<UTC YYYYMMDDTHHMMSSZ>` (never overwrites an existing backup); (2) **append-only**: existing names/indices are never reordered or renamed, new project classes get the next indices; works for list-form and index-keyed dict `names`, updates `nc`; (3) the plan preview shows the exact change (`+16: foo`, `nc: 16 → 17`); (4) written via temp file + atomic rename, re-read and verified (existing names unchanged) afterward, and the run aborts with the backup path in the error if verification fails; (5) the plan is invalidated if the remote yaml changed since planning. Without the checkbox, a project class missing from the remote aborts the merge as today.
- Rejected/deleted images: never pushed, never delete or modify the remote copy; shown in the preview as "still on remote, untouched".
- `busy` lock: make acquisition atomic (`find_one_and_update` with filter `busy != True`), and plan-building also holds it. Plans are verified again at execute time (remote still lacks the file, label hashes still match) and expire after 24 h. Secrets stay request-only; plans store ids/paths/hashes only.

---

## 3. Roadmap (each phase shippable)

### Phase 1: Class locking + hygiene (M)
**User can:** rely on class indices never shifting; see unknown-label errors instead of silent re-sorts.
- Files: `routers/images.py:197-200`, `routers/projects.py`, `server.py` (export + COCO unknown-label handling, `ProjectOut`), `frontend/src/pages/ProjectDetail.jsx:164-168`, `Annotator.jsx` (show 422), `lib/renameClass.js`, `deployment_worker.py` (delete the duplicate first-copy helpers; keep one), `tests/test_deployment_worker.py:767`.
- New fields: `projects.classes_locked`, `class_origin`. No new indexes.
- API: `POST /projects/{pid}/classes/lock` `{source, data_yaml_path?}` → `{classes, classes_locked}` (owner/admin, used by import and manually); `PUT /images/{id}/annotations` may now return `422 {detail:{unknown_labels:[...]}}` when locked; delete/merge → 409 when locked. All existing request shapes unchanged.
- Migration: `scripts/migrate_class_lock.py` (dry run, `--apply`, backup JSON).
- Tests: `test_class_lock.py` (append-only on save for unlocked, 422 when locked, delete/merge 409, rename keeps index, export skips unknown); fix/replace the failing worker test.

### Phase 2: Import from zip (L)
**User can:** upload a **YOLO detect** zip, review the dry-run report and class-mapping screen, import with original splits as approved (default) or submitted, bulk-approve a submitted batch (owner/admin), then view/annotate/review. Seg/OBB/COCO/VOC are deferred to Phase 6; non-detect label lines are reported in the dry-run as `unsupported_format` (image imported without labels only if the user opts in, otherwise skipped).
- New: `backend/yolo_codec.py` (pure; **detect parse/serialize + canonical `label_hash` now**, with a `FORMATS` registry so seg/OBB register later), `backend/dataset_import/{__init__,scan.py,commit.py,class_map.py}`, `backend/routers/imports.py` (`register(s)`), `frontend/src/components/ImportDatasetDialog.jsx` (+ `ClassMappingStep`), `frontend/src/constants/testIds/imports.js`.
- Modify: `server.py` (register router, startup indexes + orphan recovery, remove `2000` caps in export/versions/`_build_split_dataset_dir` by streaming with a cursor + stable sort), `routers/images.py` (new `reopen`; set `content_hash/width/height` on upload now), `deployment_worker._build_split_dataset_dir_sync` (cap), `frontend/src/pages/ProjectDetail.jsx` (Import button + batch list + progress poll 1 s), `lib/api.js` consumers only.
- Fields/indexes: image fields from D2; `images (project_id, content_hash)`, `images (project_id, split)`, unique partial `images (project_id, provenance.origin_key)`, `import_batches (project_id, created_at)`, unique partial `import_batches (project_id)` where `active:true`, `import_items (batch_id, status)`, `import_items (project_id, origin_key)`.
- API (all owner/admin, `_require_not_reviewer`):
  - `POST /projects/{pid}/imports` multipart `{file, review_mode: approved|submitted (default approved), source_format: yolo}` → `202 {batch_id}`; job stages `staging → scanning → ready`.
  - `GET /projects/{pid}/imports` / `GET /imports/{bid}` → `{status, phase, progress{done,total}, pending_count, report?}`; report = `{counts_by_split, class_distribution, class_mapping[], can_replace_classes, unsupported_format, images_without_labels, corrupt, duplicates{in_project,in_batch}, total_bytes}`.
  - `POST /imports/{bid}/commit` `{class_mapping[] (user-edited), replace_project_classes:false, skip_duplicates:true}` → `202`; `POST /imports/{bid}/resume`; `POST /imports/{bid}/cancel` (removes staging; leaves already-created images; `DELETE /imports/{bid}/images` rolls back a batch only for still-imported, unedited images).
  - `POST /imports/{bid}/approve-all` `{confirm_count}` → `{approved}` (owner/admin; see D3).
- Frontend: dialog steps (source → options → dry-run report → progress → done), report table and class chart, testids via registry.
- Migration: `scripts/migrate_import_schema.py` creates the indexes idempotently (also created at startup) and sets `source:"upload"` on legacy images lacking it.
- Tests: `test_yolo_codec.py` (detect round trip, empty labels, malformed lines, non-detect lines flagged), `test_class_map.py` (auto-match by name, new/skip/replace, replace only when no annotations, project order never re-sorted, skipped lines preserved, stale mapping 409), `test_import_zip.py` (original splits kept, approved vs submitted, submitted → existing `/review` flow still enforced, re-run is idempotent, corrupt/duplicate handling, cancel/resume, interrupted recovery), `test_import_approve_all.py` (owner/admin only, reviewer 403, `confirm_count` mismatch 409, activity log entry, only `submitted` images change), `test_reopen.py`.

### Phase 3: Import over SSH (M)
**User can:** point at a server dataset (found via `inspect_remote`) and import it directly.
- New: `ssh_helper.list_dir_attrs(client, path)`, `ssh_helper.probe_hash_tools(client)` + `ssh_helper.sha256_remote(client, paths)` (sha256sum → python3 → weak), `dataset_import/ssh_source.py`; extract `frontend/src/components/SshCredentialsForm.jsx` from `DeployPipeline.jsx` (reuse, no behavior change).
- API: `POST /projects/{pid}/imports/ssh` `{credentials, remote_workdir, data_yaml_path, review_mode, label_format}` → `202 {batch_id}` (stages files to disk, then identical scan/commit/resume as Phase 2; resume requires credentials again). Secrets never saved or logged; `import_batches` stores `origin` = `host:path` only.
- Writes `projects.remote_dataset = {host, data_yaml_path, remote_root}` (no secrets) and the remote manifest is *not* written yet (read-only phase).
- Tests: fake `ssh_helper` (pattern in `test_deployment_worker.py`), secret-leak test (creds absent from every collection, response and log), yaml with `val`/`valid`, dict `names`, stale `path:`.

### Phase 4: Duplicate detection (S–M)
**User can:** be warned on every upload; see and clean duplicates.
- Modify `routers/images.py:18-46`: compute sha256 in memory; if `(project_id, content_hash)` exists → `409 {duplicate_of: image_id}` unless `?force=true`. `ProjectDetail.jsx:251-269` shows the duplicate list with "upload anyway". Same check in video-frame extraction is skipped (frames are unique by design).
- `scripts/backfill_content_hash.py`: streams `get_object` per legacy image, resumable by `content_hash:{$exists:false}`, `--apply`, backup of ids.
- `GET /projects/{pid}/duplicates` → groups by hash.
- Tests: upload twice → 409; force → 201; backfill idempotent; import flags in-project duplicates.

### Phase 5: Smart merge back to server (L)
**User can:** preview exactly what will be added/updated/skipped/conflicting, then push only that.
- New: `backend/dataset_import/merge_plan.py` (pure diff given local rows + remote manifest), `routers/deployment.py` endpoints, `frontend/src/components/MergePlanPreview.jsx` inside `DeployPipeline.jsx` (`uploadFields`, shown for `merge`), testids in `constants/testIds/deployPipeline.js`.
- Modify: `deployment_worker.py` (`merge` path replaced by plan executor; `_pipeline_uploading_data_sync`), `schemas/deployment.py` (`UploadingDataRequest` +`plan_id, confirm_label_updates, update_class_names`), `pipeline_logic.py` (format-aware label validation for seg/OBB; `diff_classes` aware of locked append-only), atomic `busy` acquisition in `routers/deployment.py:396-416`.
- Fields/indexes: `images.last_push {run_id, label_hash, at}`, `images.split` set on first push; `merge_plans (run_id)`, `merge_plan_items (plan_id, category)`; `pipeline_runs.merge_plan_id`.
- API:
  - `POST /pipeline/runs/{rid}/merge_plan` `{credentials, remote_workdir, existing_data_yaml_path, split_ratios?}` → `202`; job takes `busy`; result `GET /pipeline/runs/{rid}/merge_plan` → `{plan_id, expires_at, hash_mode: sha256|weak, warnings[], counts{add,update_label,unchanged,conflict,name_collision,remote_only,skipped_rejected,unsupported_format}, adds_by_split, class_status{missing_in_remote, order_ok}, yaml_change{append:[{idx,name}], nc_before, nc_after, backup_path}|null, items(paged)}`.
  - `POST /pipeline/runs/{rid}/stage/uploading_data` (existing) with `dataset_mode:"merge"`, `plan_id`, flags above → executes the plan.
- Safety: never overwrite unchanged; image put via temp+rename after absence check; label backup before replace; compare-and-swap on remote label hash; abort entire run on class mismatch; manifest written last; dry-run result identical to executed result (items re-verified at execute time).
- Migration: `scripts/migrate_merge_indexes.py` (indexes only; fields optional so legacy data is untouched).
- Frontend: preview shows the weak-hash warning banner, the yaml append diff, and the "update data.yaml class names" checkbox (required to enable Execute when `yaml_change` is present).
- Tests: `test_merge_plan.py` (pure diff matrix incl. conflicts, renamed remote files, weak mode matching + warning, class_map translation project label → server idx, skipped-label lines re-emitted, yaml append for list and dict `names`, backup name, no write without checkbox, leakage regression: image already in `train` is never re-sent to `valid`), `test_deployment_merge.py` (fake SSH; unchanged files get zero writes; label backup created; busy atomicity; secrets absent from plan), update `test_deployment_pipeline.py`.

### Phase 6: Segmentation, OBB, COCO and VOC (M–L, later)
**User can:** import and push back YOLO-seg, YOLO-OBB, COCO and VOC datasets losslessly.
- Register `seg` and `obb` in `yolo_codec.FORMATS` (OBB ↔ bbox + rotation radians, consistent with `Annotator.jsx` `atan2(dx,-dy)` and `server.py:1244`; seg ↔ polygon points); add `dataset_import/formats.py` for COCO/VOC zips; explicit `label_format` option (seg and OBB lines both have 9 tokens for 4-point polygons, so `auto` only auto-picks detect and otherwise asks).
- Make `pipeline_logic._validate_split_labels` format-aware (currently rejects non-5-token lines); push-back rewrites labels in each image's `label_format`.
- Tests: round trips for seg/OBB incl. rotation, 4-point ambiguity, COCO/VOC conversion both ways.
- No schema change needed (fields exist from Phase 2).

---

## 4. Risk register

| Risk | Mitigation |
|---|---|
| Class-index corruption | D1 append-only + lock; remove sort; export skips instead of class 0; `diff_classes` order-exact check aborts merge; migration never reorders; tests for every index shifter |
| Split leakage | Preserve original split; persisted `split`; deterministic hash/group bucket; merge never re-splits present images; regression test |
| Overwriting remote data | Image puts never overwrite (absence check + temp/rename); label updates need explicit confirm, backup, CAS on remote hash; yaml edits backed up; remote-only files never touched |
| Duplicate images | content hash + `origin_key` unique partial index + `import_items`; upload 409 + force; backfill script |
| Large datasets in memory | Zip on disk, per-member streaming, chunked hashing, bulk inserts, paged plan items, remove 2000 caps with cursors; `sha256sum` on the server instead of downloading |
| Interrupted imports/pushes | Per-item idempotency, `interrupted` status + startup recovery, resume endpoint, staged files retained until commit/cancel |
| Unreviewed trust bypass | Owner/admin only, batch-tagged, audited, `submitted` option, rollback-by-batch |
| Disk exhaustion from staging | Pre-check free space vs total_bytes, cleanup on cancel/complete, TTL sweep |
| Secrets leakage | Request-only creds, never in db/logs/responses, explicit leak tests; scrub paramiko errors before persisting `error` |
| Roboflow near-duplicates across splits | Out of scope (preserved as-is); report note |
| Seg vs OBB ambiguity (both 9 tokens) | Deferred to Phase 6; explicit `label_format` option; `auto` only auto-picks detect (5 tokens) and otherwise asks. v1 flags non-detect lines instead of guessing |
| Wrong class remap silently corrupts labels | Mapping screen with exact-name default; stored `class_map`; re-validated at commit; push translates through the map; locked after commit |
| Skipped-class boxes lost on push | Raw skipped lines kept in `provenance.skipped_label_lines` and re-emitted on every label rewrite |
| Weak (name+size) remote matching | Used only when no hash tool exists; warning in preview; never relaxes no-overwrite; labels compared by content either way |
| `data.yaml` damaged by class append | Opt-in checkbox, timestamped backup, append-only, atomic write + verify, plan invalidated if yaml changed |
| Bulk approve used carelessly | Owner/admin only, `confirm_count` must match server count, batch-scoped, audited |

## 5. Effort
P1 M · P2 L · P3 M · P4 S–M · P5 L · P6 M–L (later). Phases 1–5 ≈ 2–3 weeks for one engineer.

## 6. Decisions recorded and remaining questions
**Decided by the user:** class mapping screen with remap/new/skip and server order authoritative (D1); remote hashing with sha256sum/python and a weak name+size fallback with warning (D6); data.yaml append with backup, append-only, preview and explicit checkbox (D6); detect first, seg/OBB later (Phase 6); bulk approve for owner/admin with confirmation and activity log, default import mode `approved` (D3).

**Still unverified / open:**
1. **Server label formats.** I cannot reach the training server from here. The only evidence is the one sample label (5-token detect lines). Please confirm by running on the server, e.g. `cat <dataset>/*/labels/*.txt | awk '{print NF}' | sort | uniq -c`: only `5` means detect-only; any other count means seg/OBB exist and Phase 6 matters sooner. The Phase 2 dry-run reports non-detect lines, so a wrong assumption is caught safely rather than imported wrongly.
2. Free disk on the ThinkVision host for staging a dataset (≈ dataset size), and acceptable max per-image size on import (default 50 MB).
3. Who may import: owner/admin only (planned), or also members?
4. Roboflow augmented duplicates: acceptable to preserve as-is?

## 7. Verification
- Backend: `cd backend && ../.venv/bin/python -m pytest -n 0 tests/test_class_lock.py tests/test_yolo_codec.py tests/test_class_map.py tests/test_import_zip.py tests/test_import_approve_all.py tests/test_merge_plan.py tests/test_deployment_merge.py tests/test_deployment_worker.py tests/test_deployment_pipeline.py tests/test_review_workflow.py tests/test_class_rename.py`, then the full suite with default `-n 2`.
- Manual: build a zip from the `myG_combined_v4` layout (16 classes, three splits) → import (dry-run, commit) → verify class order equals yaml, splits preserved, status approved; re-run import → zero new images; annotate a new image + reopen/edit an imported one → merge preview shows 1 add + 1 update_label, rest unchanged; execute against a test server directory and confirm no unchanged file's mtime changed and backup exists.
- Frontend: run the app (`run` skill), exercise ImportDatasetDialog and MergePlanPreview, check `data-testid`s.

---

## 8. Implementation status

**Phase 1 (class locking + hygiene): done.** **Phase 2 (YOLO-detect zip import): done.** **Phase 3 (import over SSH): done.** **Phase 4 (duplicate detection): done.** **Phase 5 (smart merge back to the server): done.** **Phase 6 (segmentation, OBB, COCO, VOC): done.** All six phases are implemented.

Deviations from the plan above, made while implementing Phase 2:
- **No staging extraction.** The zip stays on disk and members are read by name, so there is no zip-slip surface and memory is one image at a time. Staging dir: `IMPORT_TMP_DIR` (default `<tmp>/thinkvision_imports/<batch_id>`), removed on completion/cancel. Not done: a TTL sweep for abandoned `failed`/`interrupted` batches.
- **Class map location:** `projects.remote_dataset = {origin, server_names, class_map, data_yaml_path}` is set on the first import (and updated by later imports with the same server names). Each batch also keeps its own `class_map`, which is what push-back must use per image.
- **2000-image caps:** removed for versions and the split dataset builders (disk-based). The in-memory zip export now returns **413** above `MAX_ZIP_EXPORT_IMAGES` (default 20000) instead of silently truncating.
- **Commit body** also accepts `review_mode` and `include_unsupported` (the review choice is usually made after seeing the report); `reimport_deleted` is supported.
- **Dry-run duplicate detection** only sees images that have a `content_hash`; the report shows `legacy_unhashed_images` so this is visible until Phase 4's backfill runs. Normal uploads, video frames and imports now record `content_hash`, `width`, `height`, `source` (+ `group_key` for frames).
- **Retrain nudge:** imported approved images count toward it like any approved image (decision made: no special exclusion).
- **Still open from Phase 2 scope:** `GET /projects/{pid}/images` still returns at most the newest 10000 images, so a project with more than 10000 images hides the oldest in the grid (needs pagination, not part of this plan).
- New files: `backend/yolo_codec.py`, `backend/dataset_import/{class_map,scan,commit}.py`, `backend/routers/imports.py`, `backend/scripts/migrate_{class_lock,import_schema}.py`, `frontend/src/components/ImportDatasetDialog.jsx`, `frontend/src/lib/importMapping.js`, `frontend/src/constants/testIds/imports.js`. New endpoints: `POST/GET /projects/{pid}/imports`, `GET /imports/{id}`, `GET /imports/{id}/items`, `POST /imports/{id}/{commit,resume,cancel,approve-all}`, `DELETE /imports/{id}/images`, `POST /images/{id}/reopen`, `POST /projects/{pid}/classes/lock`.

Phase 3 (SSH import) notes and deviations:
- **Staging reuses the zip pipeline.** The stager (`dataset_import/ssh_source.py`) copies the dataset over one SFTP session into an uncompressed zip with a normalised layout (`data.yaml`, `{split}/images`, `{split}/labels`), so scan, mapping, commit, dedupe, approve-all and rollback are identical to a zip import. Only splits the yaml lists are copied (a missing `test` key never falls back to `val`; `valid:`/`validation:` keys are accepted); files over the per-image limit are never copied and are reported as `too_large`; free disk space is checked before copying.
- **Staging restarts, it does not resume.** A failed or interrupted copy deletes the partial zip and the batch fails ("start the import again"); resume applies to scan and commit (credentials are not needed for those). This replaces the plan's "resume asks for credentials again".
- **Secrets:** credentials exist only in the background job's arguments. Error text is scrubbed of the password and of each private-key line before it is stored or returned. `import_batches.origin` is `ssh:<host>:<yaml path>` and `projects.remote_dataset` gets `host`, `port` and the remote `data_yaml_path` (no username). Tests scan every collection for the secrets after success and after failure.
- **No `SshCredentialsForm` extraction** from `DeployPipeline.jsx` (its form is an inline component tied to that page's helpers); the import dialog has its own small form, so the deploy page is untouched.
- **Hash tools moved to Phase 5.** `probe_hash_tools`/`sha256_remote` are only needed by the merge preview, so they are built there. Phase 3 added only `ssh_helper.list_files(sftp, dir)`.
- New endpoints: `POST /projects/{pid}/imports/ssh/inspect` (reuses `_inspect_remote_sync`; returns datasets only) and `POST /projects/{pid}/imports/ssh`.
- Verified against the real server over HTTP (fake object store, throwaway DB): 300-image 16-class zip, and 6000 images (scan 1.9 s, commit 20 s, backend peak RSS 304 MB). Not verified: a real SSH server (the SFTP layer is faked in tests) and the UI in a browser.

Phase 4 (duplicate detection) notes:
- `POST /projects/{pid}/images` now returns **409** `{message, duplicate_of, filename, review_status}` when the project already holds identical bytes (same sha256), unless `?force=true`. The check runs before anything is stored. Scope is per project; the file name is irrelevant. The only client is `ProjectDetail.jsx`, which now collects the 409s after the batch and asks once ("N files are already in this project... upload anyway?"), then re-sends those with `force`.
- `GET /projects/{pid}/duplicates` (any project member) returns groups of identical images (oldest first, largest groups first, max 200) plus `unhashed_images`. `DuplicatesDialog.jsx` ("Duplicates" button, owner/admin) shows them with thumbnails and per-copy delete; approved copies can't be deleted (existing rule).
- `scripts/backfill_content_hash.py` fingerprints legacy images (needs object storage access: it reuses the app's storage client). Dry run by default; `--apply`, `--limit N`, `--workers N`; resumable (only images without a hash), failed fetches are skipped and retried on the next run, existing hashes are never rewritten, existing width/height are kept. **Not run against your real data.** Until it runs, duplicates of older images are not detected (the import report and the Duplicates dialog both say how many images are unhashed).
- Video frames are not duplicate-checked on extraction (they are hashed, so they show up in the Duplicates dialog).

Phase 5 (smart merge) notes and deviations:
- **How it works.** `POST /pipeline/runs/{rid}/merge_plan` (read-only, takes the pipeline's busy lock) fingerprints the server's images ON the server (`sha256sum`, else `python3`, else weak name+size matching with a warning), diffs them against the project's approved images and stores the result in `merge_plans` / `merge_plan_items`. The existing `uploading_data` stage with `dataset_mode: "merge"` then executes it (`plan_id`, `confirm_label_updates`, `update_class_names`). `new` and `reuse` are unchanged. Without a `plan_id`, merge builds a plan itself and only adds new images. A plan runs once and expires after 24 h.
- **Categories:** add / update_label / conflict / unchanged / name_collision / skipped_empty, plus counts for remote-only and "not approved here" images (left untouched on the server). Nothing on the server is ever deleted.
- **Safety rules, all covered by tests that record every write to a fake server:** existing files are never overwritten (add checks the target is absent right before writing); every write is temp file + rename; a label update needs the checkbox, backs the old label up under `<dataset>/.thinkvision_backups/<run>/<split>/labels/` and is skipped if the server copy changed since the preview; the server's data.yaml text must be unchanged since the preview; images edited/reopened since the preview are skipped; new-image label is written before the image so an interruption never leaves an unlabeled image.
- **Label ownership.** Lines the project doesn't own (skipped classes, unknown ids, seg/OBB, garbage) are compared and rewritten verbatim, never dropped. Baselines: `images.last_push.label_hash` (set on every push) else the import's `label_hash_at_import`; an image with no baseline whose server label differs is a conflict, not a guess.
- **Splits.** Existing server images keep their split (identical bytes in several server splits: the image's own recorded split wins, and the preview warns about the leakage that already exists there). New images: the split they already have if the server has it, else a deterministic hash bucket (video frames bucket by video id), restricted to the splits the server's data.yaml lists; persisted in `images.split` on first push.
- **Classes.** The server's `names` order is authoritative; the import's class_map translates project labels to server indices (and is kept in step by class rename). A server whose class list no longer matches the import aborts the preview. Project classes the server doesn't have are appended to data.yaml ONLY with the explicit checkbox: timestamped `data.yaml.bak.<UTC>` backup, append-only, in-place edit that keeps comments (re-serialised only for flow-style `names:`, and the preview says so), verified before and after writing.
- **Busy lock made atomic:** unique partial index on `pipeline_runs(pipeline_id)` where `busy` is true (a second claim gets 409). Startup creates it; `scripts/migrate_merge_indexes.py` creates it explicitly and reports stuck busy runs (which block the index) without clearing them.
- **Deviations:** no remote `.thinkvision_manifest.json` cache (the preview re-fingerprints the server each time; add the cache only if that proves slow); the dead `_upload_into_existing` was removed; the old "abort if a local class is missing on the server" rule is replaced by the append-with-confirmation flow above.
- **Behaviour change to be aware of:** because Phase 4 rejects identical uploads, the repo's live-server test `test_deployment_pipeline.py` seeds its 20 identical images with `?force=true` (updated).
- **Verified:** 33 new backend tests (pure logic, plan/execute against an in-memory server, and through the real routes incl. busy/expiry/secrets), the hashing shell commands against a real local shell, and the old live-server tests against a throwaway server (54 pass; the same 2 fail on the original HEAD). **Not verified:** a real SSH server, and the new preview UI in a browser.

Phase 6 (segmentation, OBB, COCO, VOC) notes and deviations:
- **Formats.** YOLO detect / seg / OBB, COCO and Pascal VOC zips. `source_format` (auto|yolo|coco|voc) and `label_format` (auto|detect|seg|obb) are options on `POST /projects/{pid}/imports` (and `label_format` on the SSH import). `auto` picks detect when every label line has 5 values, seg when some polygon has a point count an oriented box can't have, and **asks** when all non-box lines have exactly 9 values (a 4-point polygon and an OBB are the same text): the scan fails with a message and you re-upload with the format chosen. Source detection: data.yaml → YOLO, else a COCO json, else VOC xml.
- **Geometry.** Oriented boxes are converted in pixel space, matching how the annotator draws `rotation` (CSS `rotate()` about the centre), so they need each image's size (stored since Phase 2; legacy images are measured once). Corner order/start point does not matter: the canonical hash picks one description of a rotated rectangle (in pixel units, mod pi). Seg polygons are kept as polygons.
- **COCO.** Class order = category ids sorted; an unused catch-all category with supercategory "none" (Roboflow adds one) is dropped. A single-ring segmentation becomes a polygon, otherwise the bbox is used; crowd regions, RLE masks and multi-part shapes are unsupported (image imported only with `include_unsupported`, and then unreviewed). Splits come from folder / file names (train, val(id), test) and images are found next to the json, under `images/`, or by file name. COCO annotation files are capped at 128 MB (parsed in memory).
- **VOC.** No class list exists, so classes are alphabetical (the report says so; the mapping screen can remap). Splits from `ImageSets/**/{train,val,test}.txt`, else folder names; the app's own `<polygon>` export is read back as a polygon. XML with DOCTYPE/ENTITY declarations is refused (entity-expansion bombs).
- **COCO/VOC projects stay unlocked and unlinked.** Only a YOLO dataset has a class order to stay aligned with; locking or linking a COCO/VOC import would block later merges into a differently ordered server dataset. Images without a split get one (deterministically) at first push.
- **Merge is format-aware.** The dataset's format comes from the request, else what the import recorded, else a sample of the server's labels (ambiguity is reported); labels are written as polygons / oriented boxes / boxes accordingly (a plain box in a seg dataset becomes a 4-point polygon; lossy conversions are counted). After every write the image records `last_push.remote_hash` (what the server's file parses to), so rounding of corner coordinates can never turn our own push into a false conflict.
- **In-app OBB export fixed.** `export?format=yolo_obb` rotated corners in normalized space (right only for square images); it now uses pixel space, so export, import and merge agree. Output for square images is unchanged.
- **Other.** `pipeline_logic.validate_dataset_dir` accepts a `label_format`. Behaviour change: a 9-value label file no longer imports silently as "unsupported detect lines": it needs a format (`test_import_zip.py` updated accordingly).
- **Verified:** 40 new backend tests across codec geometry, the three readers through the real API, and seg/OBB merges (incl. no false conflicts after our own push); a real-server run (COCO 3000 images/9000 annotations: scan 0.8 s, commit 10.5 s; YOLO-seg 1500; VOC 1000; backend peak 269 MB); the old live-server tests (same 54 pass / same 2 pre-existing failures as the original code). **Not verified:** real datasets in these formats from your servers (only synthetic ones), and the UI in a browser.

