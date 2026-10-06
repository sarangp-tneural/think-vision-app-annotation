"""YOLO-detect zip import: dry run, class mapping, commit, review modes, idempotency, resume, rollback."""
import asyncio
import io
import os
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402
import _inprocess  # noqa: E402
from _inprocess import fresh_db, get_client  # noqa: E402

import yolo_codec  # noqa: E402

NOW = datetime.now(timezone.utc).isoformat()
YAML = "path: /root/myG/myG_combined_v4\n\ntrain: train/images\nval: valid/images\ntest: test/images\n\nnc: 3\n\nnames:\n  0: person\n  1: cash\n  2: id_visible\n"


@pytest.fixture(scope="module")
def db():
    yield fresh_db()


@pytest.fixture(scope="module")
def client(db):
    return get_client()


def _register(client, name):
    r = client.post("/api/auth/register", json={"email": f"{name}@example.com", "password": "pw123456", "name": name})
    assert r.status_code == 200, r.text
    return r.json()["user"]["id"], {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture(scope="module")
def world(client, db):
    w = {}
    w["owner_id"], w["owner"] = _register(client, "iowner")
    w["team"] = next(t for t in client.get("/api/teams", headers=w["owner"]).json() if t["is_personal"])["id"]
    for name, role in (("iann", "annotator"), ("irev", "reviewer")):
        w[name + "_id"], w[name] = _register(client, name)
        assert client.post(f"/api/teams/{w['team']}/invite", json={"email": f"{name}@example.com", "role": role},
                           headers=w["owner"]).status_code == 200
    return w


@pytest.fixture(autouse=True)
def storage(monkeypatch):
    """No real object store: record what would have been uploaded."""
    puts = {}

    def fake_put(path, data, content_type):
        if puts.get("__fail__"):
            raise RuntimeError("storage down")
        puts[path] = data
        return {"path": path, "size": len(data)}

    monkeypatch.setattr(_inprocess.server, "put_object", fake_put)
    return puts


def _png(color, size=(8, 8)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


A, B, C, D = _png((255, 0, 0)), _png((0, 255, 0)), _png((0, 0, 255), (9, 7)), _png((9, 9, 9))


def make_zip(entries, yaml_text=YAML, yaml_at="data.yaml"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        if yaml_text is not None:
            zf.writestr(yaml_at, yaml_text)
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def standard():
    return {
        "train/images/a_jpg.rf.1.jpg": A, "train/labels/a_jpg.rf.1.txt": "0 0.5 0.5 0.2 0.4\n1 0.25 0.25 0.1 0.1\n",
        "train/images/b.jpg": B, "train/labels/b.txt": "2 0.5 0.5 0.2 0.2\n",
        "valid/images/c.png": C, "valid/labels/c.txt": "0 0.5 0.5 0.2 0.2\n",
        "test/images/d.jpg": D,
        "valid/images/bad.jpg": b"this is not an image",
        "valid/images/a_copy.jpg": A, "valid/labels/a_copy.txt": "0 0.5 0.5 0.2 0.4\n",
    }


def new_project(client, world, classes=()):
    pid = client.post("/api/projects", json={"name": "Imp", "team_id": world["team"]}, headers=world["owner"]).json()["id"]
    if classes:
        _inprocess.MongoClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"]].projects.update_one({"id": pid}, {"$set": {"classes": list(classes)}})
    return pid


def upload(client, world, pid, data, mode="approved", who="owner", include_unsupported=False, name="ds.zip", **form):
    return client.post(f"/api/projects/{pid}/imports", files={"file": (name, data, "application/zip")},
                       data={"review_mode": mode, "include_unsupported": str(include_unsupported).lower(), **form}, headers=world[who])


def scanned(client, world, pid, data, **kw):
    r = upload(client, world, pid, data, **kw)
    assert r.status_code == 202, r.text
    b = client.get(f"/api/imports/{r.json()['batch_id']}", headers=world["owner"]).json()
    assert b["status"] == "ready", b
    return b


def commit(client, world, bid, **body):
    return client.post(f"/api/imports/{bid}/commit", json=body, headers=world["owner"])


def imported(db, pid):
    return {i["filename"]: i for i in db.images.find({"project_id": pid, "source": "import"}, {"_id": 0})}


def test_dry_run_reports_everything_and_creates_nothing(client, db, world, storage):
    pid = new_project(client, world)
    b = scanned(client, world, pid, make_zip(standard()))
    rep = b["report"]
    assert rep["server_names"] == ["person", "cash", "id_visible"] and rep["nc"] == 3
    assert rep["total_images"] == 6 and rep["importable"] == 4
    assert rep["counts_by_split"] == {"train": 2, "valid": 1, "test": 1}
    assert rep["corrupt"] == 1 and rep["duplicates"]["in_batch"] == 1
    assert rep["images_without_labels"] == 1 and rep["unknown_class_indices"] == []
    assert {c["name"]: c["boxes"] for c in rep["class_distribution"]} == {"person": 2, "cash": 1, "id_visible": 1}
    assert [m["action"] for m in rep["class_mapping"]] == ["new", "new", "new"] and rep["can_replace_classes"] is True
    assert db.images.count_documents({"project_id": pid}) == 0 and storage == {}  # a dry run changes nothing
    assert db.projects.find_one({"id": pid})["classes"] == []
    assert "zip_path" not in b
    bad = client.get(f"/api/imports/{b['id']}/items?status=corrupt", headers=world["owner"]).json()
    assert [i["rel_path"] for i in bad] == ["valid/images/bad.jpg"]


def test_commit_creates_images_with_server_class_order_splits_and_provenance(client, db, world, storage):
    pid = new_project(client, world)
    b = scanned(client, world, pid, make_zip(standard()))
    r = commit(client, world, b["id"])
    assert r.status_code == 202, r.text
    assert r.json()["project_classes"] == ["person", "cash", "id_visible"]  # data.yaml order, not alphabetical

    proj = db.projects.find_one({"id": pid})
    assert proj["classes"] == ["person", "cash", "id_visible"] and proj["classes_locked"] is True
    assert proj["class_origin"]["source"] == "import"
    assert proj["remote_dataset"]["class_map"] == {"0": "person", "1": "cash", "2": "id_visible"}

    imgs = imported(db, pid)
    assert sorted(imgs) == ["a_jpg.rf.1.jpg", "b.jpg", "c.png", "d.jpg"]  # corrupt + duplicate skipped
    assert {n: i["split"] for n, i in imgs.items()} == {"a_jpg.rf.1.jpg": "train", "b.jpg": "train", "c.png": "valid", "d.jpg": "test"}
    a = imgs["a_jpg.rf.1.jpg"]
    assert a["review_status"] == "approved" and a["annotated"] is True
    assert a["reviewed_by"] == world["owner_id"] and a["reviewed_at"]
    assert "annotated_by" not in a and a["assigned_to"] is None  # imported work is never attributed to a person
    assert a["annotations"][0]["label"] == "person"
    assert (round(a["annotations"][0]["x"], 6), round(a["annotations"][0]["y"], 6)) == (0.4, 0.3)  # centre -> top-left
    assert a["width"] == 8 and a["height"] == 8 and len(a["content_hash"]) == 64
    p = a["provenance"]
    assert p["original_filename"] == "a_jpg.rf.1.jpg" and p["original_split"] == "train" and p["origin"] == "zip:ds.zip"
    assert p["import_batch_id"] == b["id"] and p["label_format"] == "detect" and p["skipped_label_lines"] == []
    assert imgs["d.jpg"]["review_status"] == "unassigned" and imgs["d.jpg"]["annotated"] is False  # no label file
    assert len(storage) == 4 and all(a["storage_path"] in storage for a in imgs.values())

    done = client.get(f"/api/imports/{b['id']}", headers=world["owner"]).json()
    assert done["status"] == "completed" and done["counts"]["items"]["imported"] == 4
    assert db.import_batches.find_one({"id": b["id"]})["active"] is False
    assert not os.path.exists(os.path.dirname(db.import_batches.find_one({"id": b["id"]})["zip_path"]))  # staging removed
    assert db.activity_log.find_one({"project_id": pid, "action": "dataset_imported"})["meta"]["imported"] == 4


def test_label_hash_at_import_matches_a_recomputation_from_the_app_annotations(client, db, world):
    pid = new_project(client, world)
    b = scanned(client, world, pid, make_zip(standard()))
    assert commit(client, world, b["id"]).status_code == 202
    a = imported(db, pid)["a_jpg.rf.1.jpg"]
    class_map = db.projects.find_one({"id": pid})["remote_dataset"]["class_map"]
    label_to_idx = {v: int(k) for k, v in class_map.items() if v}
    now = yolo_codec.canonical_hash(yolo_codec.boxes_to_canonical_items(a["annotations"], label_to_idx))
    assert now == a["provenance"]["label_hash_at_import"]  # so "unchanged" is detectable later


def test_importing_the_same_zip_again_creates_nothing_new(client, db, world, storage):
    pid = new_project(client, world)
    data = make_zip(standard())
    b1 = scanned(client, world, pid, data)
    assert commit(client, world, b1["id"]).status_code == 202
    first_ids = {i["id"] for i in db.images.find({"project_id": pid})}
    b2 = scanned(client, world, pid, data)
    assert b2["report"]["importable"] == 0 and b2["report"]["duplicates"]["already_imported"] == 4
    assert commit(client, world, b2["id"]).status_code == 202
    assert {i["id"] for i in db.images.find({"project_id": pid})} == first_ids and len(storage) == 4


def test_existing_project_classes_are_matched_by_name_and_never_resorted(client, db, world):
    pid = new_project(client, world, ["zebra", "cash"])
    b = scanned(client, world, pid, make_zip(standard()))
    assert [(m["action"], m["target"]) for m in b["report"]["class_mapping"]] == [("new", "person"), ("match", "cash"), ("new", "id_visible")]
    r = commit(client, world, b["id"])
    assert r.json()["project_classes"] == ["zebra", "cash", "person", "id_visible"]  # appended in server order
    # server idx 0 -> "person" even though the project index of "person" is 2: the map carries the translation
    assert db.projects.find_one({"id": pid})["remote_dataset"]["class_map"] == {"0": "person", "1": "cash", "2": "id_visible"}


def test_mapping_screen_choices_remap_and_skip_with_skipped_lines_preserved(client, db, world):
    pid = new_project(client, world, ["cash", "human"])
    b = scanned(client, world, pid, make_zip(standard()))
    r = commit(client, world, b["id"], class_mapping=[
        {"server_idx": 0, "action": "match", "target": "human"}, {"server_idx": 2, "action": "skip"}])
    assert r.status_code == 202, r.text
    assert db.projects.find_one({"id": pid})["classes"] == ["cash", "human"]
    imgs = imported(db, pid)
    assert [x["label"] for x in imgs["a_jpg.rf.1.jpg"]["annotations"]] == ["human", "cash"]
    only_skipped = imgs["b.jpg"]  # its only box is class 2, which was skipped
    assert only_skipped["annotations"] == [] and only_skipped["annotated"] is False and only_skipped["review_status"] == "unassigned"
    assert only_skipped["provenance"]["skipped_label_lines"] == ["2 0.5 0.5 0.2 0.2"]  # kept verbatim for push-back
    assert db.projects.find_one({"id": pid})["remote_dataset"]["class_map"] == {"0": "human", "1": "cash", "2": None}


def test_invalid_mapping_is_rejected_and_the_batch_can_be_committed_again(client, db, world):
    pid = new_project(client, world, ["a"])
    b = scanned(client, world, pid, make_zip(standard()))
    bad = commit(client, world, b["id"], class_mapping=[
        {"server_idx": 0, "action": "match", "target": "a"}, {"server_idx": 1, "action": "match", "target": "a"}])
    assert bad.status_code == 422 and "both map to" in bad.json()["detail"]
    assert commit(client, world, b["id"], class_mapping=[{"server_idx": 0, "action": "match", "target": "ghost"}]).status_code == 422
    assert db.projects.find_one({"id": pid})["classes"] == ["a"] and not db.projects.find_one({"id": pid}).get("classes_locked")
    assert client.get(f"/api/imports/{b['id']}", headers=world["owner"]).json()["status"] == "ready"
    assert commit(client, world, b["id"]).status_code == 202  # nothing was half-applied


def test_replace_project_classes_only_when_nothing_is_annotated(client, db, world):
    pid = new_project(client, world, ["zebra", "cash"])
    db.images.insert_one({"id": "pre", "project_id": pid, "annotations": [{"label": "zebra"}], "created_at": NOW})
    b = scanned(client, world, pid, make_zip(standard()))
    assert b["report"]["can_replace_classes"] is False
    r = commit(client, world, b["id"], replace_project_classes=True)
    assert r.status_code == 422 and "no image has annotations" in r.json()["detail"]
    db.images.delete_one({"id": "pre"})  # the check is re-evaluated at commit time, on the same (still ready) batch
    assert commit(client, world, b["id"], replace_project_classes=True).json()["project_classes"] == ["person", "cash", "id_visible"]


def test_submitted_mode_goes_through_the_normal_review_rules_and_bulk_approve(client, db, world):
    pid = new_project(client, world)
    b = scanned(client, world, pid, make_zip(standard()), mode="submitted")
    assert commit(client, world, b["id"]).status_code == 202
    imgs = imported(db, pid)
    assert [imgs[n]["review_status"] for n in ("a_jpg.rf.1.jpg", "b.jpg", "c.png")] == ["submitted"] * 3
    assert imgs["a_jpg.rf.1.jpg"]["submitted_at"] and imgs["a_jpg.rf.1.jpg"]["reviewed_by"] is None
    assert imgs["d.jpg"]["review_status"] == "unassigned"  # nothing to review
    # locked like any submitted image: even the owner cannot edit it
    one = imgs["a_jpg.rf.1.jpg"]["id"]
    assert client.put(f"/api/images/{one}/annotations", json={"boxes": []}, headers=world["owner"]).status_code == 409
    assert client.get(f"/api/imports/{b['id']}", headers=world["owner"]).json()["pending_count"] == 3
    # a reviewer approves one image through the existing endpoint
    assert client.post(f"/api/images/{one}/review", json={"decision": "approve"}, headers=world["irev"]).status_code == 200
    # bulk approve: owner/admin only, and the confirmed count must match the server's
    assert client.post(f"/api/imports/{b['id']}/approve-all", json={"confirm_count": 2}, headers=world["irev"]).status_code == 403
    stale = client.post(f"/api/imports/{b['id']}/approve-all", json={"confirm_count": 3}, headers=world["owner"])
    assert stale.status_code == 409 and stale.json()["detail"]["pending_count"] == 2
    ok = client.post(f"/api/imports/{b['id']}/approve-all", json={"confirm_count": 2}, headers=world["owner"])
    assert ok.status_code == 200 and ok.json() == {"approved": 2}
    assert {i["review_status"] for n, i in imported(db, pid).items() if n != "d.jpg"} == {"approved"}
    assert db.activity_log.find_one({"project_id": pid, "action": "import_batch_approved"})["meta"]["count"] == 2
    assert client.post(f"/api/imports/{b['id']}/approve-all", json={"confirm_count": 0}, headers=world["owner"]).json() == {"approved": 0}


def test_duplicates_of_existing_project_images_are_skipped_unless_asked(client, db, world):
    pid = new_project(client, world)
    import hashlib
    db.images.insert_one({"id": "old", "project_id": pid, "content_hash": hashlib.sha256(C).hexdigest(), "created_at": NOW})
    b = scanned(client, world, pid, make_zip(standard()))
    assert b["report"]["duplicates"]["in_project"] == 1 and b["report"]["importable"] == 3
    assert b["report"]["legacy_unhashed_images"] == 0
    assert commit(client, world, b["id"]).status_code == 202
    assert "c.png" not in imported(db, pid)
    b = scanned(client, world, pid, make_zip(standard()), name="again.zip")
    assert commit(client, world, b["id"], skip_duplicates=False).status_code == 202
    assert "c.png" in imported(db, pid)


def test_unsupported_label_lines_are_reported_skipped_by_default_and_never_trusted(client, db, world):
    entries = {**standard(), "train/labels/b.txt": "2 0.1 0.1 0.5 0.1 0.5 0.5 0.1 0.5\n0 0.5 0.5 0.2 0.2\n"}
    pid = new_project(client, world)
    # a 9-value line is ambiguous (segmentation or OBB), so "auto" asks; read as plain boxes (detect) it is unsupported
    ambiguous = upload(client, world, pid, make_zip(entries))
    failed = client.get(f"/api/imports/{ambiguous.json()['batch_id']}", headers=world["owner"]).json()
    assert failed["status"] == "failed" and "segmentation" in failed["error"] and failed["active"] is False
    b = scanned(client, world, pid, make_zip(entries), label_format="detect")
    assert b["report"]["unsupported_format"] == 1 and b["report"]["label_format"] == "detect"
    assert commit(client, world, b["id"]).status_code == 202
    assert "b.jpg" not in imported(db, pid)

    pid = new_project(client, world)
    b = scanned(client, world, pid, make_zip(entries), include_unsupported=True, label_format="detect")
    assert commit(client, world, b["id"]).status_code == 202
    img = imported(db, pid)["b.jpg"]
    assert img["review_status"] == "unassigned"  # partial labels must not reach training as "approved"
    assert img["provenance"]["has_unsupported_lines"] is True
    assert img["provenance"]["skipped_label_lines"] == ["2 0.1 0.1 0.5 0.1 0.5 0.5 0.1 0.5"]


def test_roboflow_style_yaml_with_parent_relative_paths_and_member_hygiene(client, db, world):
    yaml_text = "train: ../train/images\nval: ../valid/images\nnames: [person, cash, id_visible]\n"
    entries = {"train/images/a.jpg": A, "train/labels/a.txt": "0 0.5 0.5 0.2 0.2\n", "valid/images/c.png": C,
               "../escape.jpg": B, "__MACOSX/train/images/._a.jpg": D, "train/images/.hidden.jpg": D, "notes.md": "x"}
    pid = new_project(client, world)
    b = scanned(client, world, pid, make_zip(entries, yaml_text=yaml_text))
    assert b["report"]["counts_by_split"] == {"train": 1, "valid": 1}  # no test split listed, none invented
    assert b["report"]["total_images"] == 2  # traversal / metadata / hidden entries are ignored, never extracted
    assert commit(client, world, b["id"]).status_code == 202 and sorted(imported(db, pid)) == ["a.jpg", "c.png"]


def test_bad_uploads_fail_cleanly_and_free_the_project(client, db, world):
    pid = new_project(client, world)
    assert upload(client, world, pid, b"x", name="data.tar").status_code == 400
    assert upload(client, world, pid, b"x", mode="bogus").status_code == 400
    r = upload(client, world, pid, make_zip(standard(), yaml_text=None))
    b = client.get(f"/api/imports/{r.json()['batch_id']}", headers=world["owner"]).json()
    assert b["status"] == "failed" and "data.yaml" in b["error"] and b["active"] is False
    r = upload(client, world, pid, b"not really a zip")
    assert client.get(f"/api/imports/{r.json()['batch_id']}", headers=world["owner"]).json()["error"].startswith("The file is not a valid zip")
    gaps = "train: train/images\nnames:\n  0: a\n  2: c\n"
    r = upload(client, world, pid, make_zip({"train/images/a.jpg": A}, yaml_text=gaps))
    assert "gaps" in client.get(f"/api/imports/{r.json()['batch_id']}", headers=world["owner"]).json()["error"]
    assert scanned(client, world, pid, make_zip(standard()))["status"] == "ready"  # failed batches don't block the project


def test_one_active_import_per_project_cancel_frees_it_and_roles_are_enforced(client, db, world):
    pid = new_project(client, world)
    b = scanned(client, world, pid, make_zip(standard()))
    assert upload(client, world, pid, make_zip(standard())).status_code == 409
    assert upload(client, world, pid, make_zip(standard()), who="iann").status_code == 403
    assert upload(client, world, pid, make_zip(standard()), who="irev").status_code == 403
    assert client.get(f"/api/imports/{b['id']}", headers=world["iann"]).status_code == 403
    assert client.post(f"/api/imports/{b['id']}/cancel", headers=world["owner"]).status_code == 200
    assert client.post(f"/api/imports/{b['id']}/cancel", headers=world["owner"]).status_code == 409
    assert commit(client, world, b["id"]).status_code == 409  # cancelled batches can't be committed
    assert scanned(client, world, pid, make_zip(standard()))["status"] == "ready"


def test_failed_storage_leaves_a_partial_batch_that_resumes_without_duplicates(client, db, world, storage):
    pid = new_project(client, world)
    b = scanned(client, world, pid, make_zip(standard()))
    storage["__fail__"] = True
    assert commit(client, world, b["id"]).status_code == 202
    cur = client.get(f"/api/imports/{b['id']}", headers=world["owner"]).json()
    assert cur["status"] == "partial" and "failed" in cur["error"] and cur["active"] is True
    assert db.images.count_documents({"project_id": pid}) == 0
    storage["__fail__"] = False
    assert client.post(f"/api/imports/{b['id']}/resume", headers=world["owner"]).status_code == 202
    cur = client.get(f"/api/imports/{b['id']}", headers=world["owner"]).json()
    assert cur["status"] == "completed" and db.images.count_documents({"project_id": pid}) == 4
    assert client.post(f"/api/imports/{b['id']}/resume", headers=world["owner"]).status_code == 409


def test_interrupted_batches_resume_and_startup_recovery_marks_them(client, db, world):
    from motor.motor_asyncio import AsyncIOMotorClient
    pid = new_project(client, world)
    b = scanned(client, world, pid, make_zip(standard()))
    db.import_batches.update_one({"id": b["id"]}, {"$set": {"status": "scanning"}})
    db.import_batches.insert_one({"id": "up", "project_id": "other", "status": "staging", "active": True})

    async def recover():
        from routers import imports as imports_router
        await imports_router.recover_interrupted(AsyncIOMotorClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"]])

    asyncio.run(recover())
    cur = db.import_batches.find_one({"id": b["id"]})
    assert cur["status"] == "interrupted" and cur["resume_phase"] == "scanning"
    up = db.import_batches.find_one({"id": "up"})
    assert up["status"] == "failed" and up["active"] is False  # an upload can't be resumed
    r = client.post(f"/api/imports/{b['id']}/resume", headers=world["owner"])
    assert r.status_code == 202
    assert client.get(f"/api/imports/{b['id']}", headers=world["owner"]).json()["status"] == "ready"


def test_rollback_removes_only_untouched_images(client, db, world):
    pid = new_project(client, world)
    b = scanned(client, world, pid, make_zip(standard()))
    assert commit(client, world, b["id"]).status_code == 202
    imgs = imported(db, pid)
    db.images.update_one({"id": imgs["c.png"]["id"]}, {"$set": {"annotated_by": world["iann_id"]}})  # somebody worked on it
    r = client.delete(f"/api/imports/{b['id']}/images", headers=world["owner"])
    assert r.json() == {"deleted": 3, "kept": 1}
    assert sorted(imported(db, pid)) == ["c.png"]
    assert client.delete(f"/api/imports/{b['id']}/images", headers=world["irev"]).status_code == 403
    # a rolled-back image can be imported again later
    b2 = scanned(client, world, pid, make_zip(standard()), name="ds.zip")
    assert b2["report"]["importable"] == 3 and b2["report"]["duplicates"]["already_imported"] == 1
