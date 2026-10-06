"""Create images from a scanned import batch. Idempotent and resumable: one image per `origin_key`
(unique partial index on images), and items already `imported` are skipped on a re-run.

Import is the one place that inserts non-`unassigned` images directly (the review workflow has no way to
represent already-trusted data). It is only reachable from owner/admin-only endpoints; every image is tagged
with its batch; `annotated_by`/`assigned_to` stay empty so imported work is never attributed to a person.
"""
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from pymongo.errors import DuplicateKeyError

import yolo_codec
from dataset_import import formats
from dataset_import import scan as scan_mod

PUT_WORKERS = 4
PUT_CHUNK = 16


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_image_doc(item: dict, *, batch: dict, project_id: str, user_id: str, img_id: str, storage_path: str,
                    parsed: yolo_codec.ParsedLabels, class_map: Dict[str, Optional[str]], nc: int, size: int, now: str) -> dict:
    """Pure: scanned item + its parsed labels -> the images document (no I/O).

    Shapes of any kind (box, polygon, oriented box) become app annotations. The label hash that later tells "edited
    here since import" is computed from those annotations through the same conversion the merge uses, so an unedited
    image can never look edited because of a representation difference."""
    boxes: List[dict] = []
    preserved: List[str] = list(parsed.unsupported) + list(parsed.malformed)
    for b in parsed.boxes:
        label = class_map.get(str(b["cls"])) if 0 <= b["cls"] < nc else None
        if label is None:
            preserved.append(b["raw"])  # skipped class / out-of-range id: kept verbatim so push-back never drops it
            continue
        boxes.append(yolo_codec.box_to_annotation(b, label))
    label_format = batch.get("resolved_label_format") or "detect"
    hash_format = label_format if label_format in ("detect", "seg", "obb") else "detect"  # coco/voc: compared as boxes
    dims = (item["width"], item["height"]) if item.get("width") and item.get("height") else None
    label_to_idx = {label: int(k) for k, label in class_map.items() if label is not None}
    items, _unknown, _lossy = yolo_codec.annotations_to_items(boxes, label_to_idx, hash_format, dims)

    has_unsupported = bool(parsed.unsupported)
    mode = batch["review_mode"]
    # Images with lines we couldn't convert are never trusted: partial labels must not reach training.
    trusted = bool(boxes) and not has_unsupported
    status = ("approved" if mode == "approved" else "submitted") if trusted else "unassigned"
    return {
        "id": img_id, "project_id": project_id, "user_id": user_id,
        "filename": item["filename"], "storage_path": storage_path,
        "content_type": scan_mod.CONTENT_TYPES.get(scan_mod.ext_of(item["filename"]), "image/jpeg"),
        "size": size, "annotations": boxes, "annotated": bool(boxes), "assigned_to": None,
        "review_status": status, "review_notes": "",
        "reviewed_by": user_id if status == "approved" else None,
        "reviewed_at": now if status == "approved" else None,
        "submitted_at": now if status == "submitted" else None,
        "created_at": now,
        "content_hash": item["sha256"], "width": item["width"], "height": item["height"],
        "split": item["split"], "source": "import", "group_key": None,
        "provenance": {
            "import_batch_id": batch["id"], "origin": batch["origin"], "origin_key": item["origin_key"],
            "remote_rel_path": item["rel_path"], "original_filename": item["filename"],
            "original_split": item["split"], "label_format": label_format,
            "label_hash_at_import": yolo_codec.canonical_hash(items, dims),
            "had_label_file": item["had_label_file"],
            "skipped_label_lines": preserved, "has_unsupported_lines": has_unsupported,
        },
    }


def commit_batch_sync(sync_db, put_object_fn: Callable, batch_id: str, *, user_id: str, app_name: str,
                      should_stop: Callable[[], bool]) -> dict:
    """Create images for every pending item of the batch; returns the counts. Ends the batch as `completed`
    (zip removed by the caller) or `partial` (some items failed; resume retries them)."""
    batch = sync_db.import_batches.find_one({"id": batch_id})
    pid = batch["project_id"]
    class_map = batch["class_map"]
    nc = len(batch["server_names"])
    include_unsupported = batch.get("include_unsupported", False)
    importable = ["scanned", "failed"]
    if not batch.get("skip_duplicates", True):
        importable += ["duplicate_in_project", "duplicate_in_batch"]
    if batch.get("reimport_deleted"):
        importable.append("previously_deleted")

    sync_db.import_items.update_many({"batch_id": batch_id, "status": "failed"}, {"$set": {"status": "scanned", "error": None}})
    if not include_unsupported:
        sync_db.import_items.update_many({"batch_id": batch_id, "status": {"$in": importable}, "n_unsupported": {"$gt": 0}},
                                         {"$set": {"status": "skipped_unsupported"}})
    query = {"batch_id": batch_id, "status": {"$in": [s for s in importable if s != "failed"]}}
    total = sync_db.import_items.count_documents(query) + sync_db.import_items.count_documents({"batch_id": batch_id, "status": "imported"})
    done = sync_db.import_items.count_documents({"batch_id": batch_id, "status": "imported"})
    sync_db.import_batches.update_one({"id": batch_id}, {"$set": {"progress": {"done": done, "total": total}}})

    pending = list(sync_db.import_items.find(query, {"_id": 0}).sort("rel_path", 1))
    failed = 0
    with zipfile.ZipFile(batch["zip_path"]) as zf, ThreadPoolExecutor(PUT_WORKERS) as pool:
        reader = formats.LabelReader(zf, batch)
        for i in range(0, len(pending), PUT_CHUNK):
            if should_stop():
                return {"stopped": True}
            prepared = []
            for item in pending[i:i + PUT_CHUNK]:
                try:
                    data = scan_mod.read_capped(zf, item["rel_path"], 1 << 40)
                    if data is None or _sha(data) != item["sha256"]:
                        raise ValueError("file content changed since the scan")
                    size = (item["width"], item["height"]) if item.get("width") and item.get("height") else None
                    prepared.append((item, data, reader.read(item, size)))
                except Exception as e:
                    failed += 1
                    sync_db.import_items.update_one({"id": item["id"]}, {"$set": {"status": "failed", "error": str(e)[:200]}})

            def _put(args):
                item, data, _label = args
                img_id = str(uuid.uuid4())
                ext = scan_mod.ext_of(item["filename"]) or "jpg"
                path = f"{app_name}/projects/{pid}/{img_id}.{ext}"
                try:
                    result = put_object_fn(path, data, scan_mod.CONTENT_TYPES.get(ext, "image/jpeg"))
                    return img_id, result, None
                except Exception as e:  # storage hiccup: this item fails, the batch carries on
                    return img_id, None, str(e)[:200]

            for (item, data, parsed), (img_id, result, err) in zip(prepared, pool.map(_put, prepared)):
                if err:
                    failed += 1
                    sync_db.import_items.update_one({"id": item["id"]}, {"$set": {"status": "failed", "error": err}})
                    continue
                doc = build_image_doc(item, batch=batch, project_id=pid, user_id=user_id, img_id=img_id,
                                      storage_path=result["path"], parsed=parsed, class_map=class_map, nc=nc,
                                      size=result.get("size", len(data)), now=_now())
                try:
                    sync_db.images.insert_one(doc)
                    new_id = img_id
                except DuplicateKeyError:  # same origin imported concurrently / on a previous run: keep the existing one
                    existing = sync_db.images.find_one({"project_id": pid, "provenance.origin_key": item["origin_key"]}, {"id": 1})
                    new_id = existing["id"] if existing else None
                sync_db.import_items.update_one({"id": item["id"]}, {"$set": {"status": "imported", "image_id": new_id}})
                done += 1
            sync_db.import_batches.update_one({"id": batch_id}, {"$set": {
                "progress": {"done": done, "total": total}, "updated_at": _now()}})

    counts = summarize(sync_db, batch_id)
    return {"stopped": False, "failed": failed, "counts": counts}


def _sha(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


def summarize(sync_db, batch_id: str) -> dict:
    counts: Dict[str, int] = {}
    for row in sync_db.import_items.aggregate([{"$match": {"batch_id": batch_id}}, {"$group": {"_id": "$status", "n": {"$sum": 1}}}]):
        counts[row["_id"]] = row["n"]
    by_review: Dict[str, int] = {}
    for row in sync_db.images.aggregate([{"$match": {"provenance.import_batch_id": batch_id}},
                                         {"$group": {"_id": "$review_status", "n": {"$sum": 1}}}]):
        by_review[row["_id"]] = row["n"]
    return {"items": counts, "images_by_review_status": by_review}
