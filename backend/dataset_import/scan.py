"""Scan an uploaded dataset zip (YOLO, COCO or Pascal VOC) into `import_items` rows and a dry-run report. Nothing is
created in the project.

The zip stays on disk and is never extracted: members are read by name one at a time, so member paths can
never escape anywhere (no zip-slip) and memory stays at one image. Runs in a worker thread with a sync
pymongo handle (same pattern as the video / training workers). How a layout is read lives in formats.py.
"""
import hashlib
import io
import posixpath
import uuid
import zipfile
from collections import Counter
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

from pymongo import UpdateOne

from dataset_import import class_map as class_map_mod
from dataset_import import formats
from dataset_import.common import (CONTENT_TYPES, IMAGE_EXTS, MAX_LABEL_BYTES, MAX_YAML_BYTES, ImportError_, clean as _clean,  # noqa: F401
                                   ext_of, read_capped)
from dataset_import.formats import iter_entries, load_yaml, locate_yaml, split_layout  # noqa: F401  (re-exported)

CHUNK = 1024 * 1024


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def origin_key(origin: str, rel_path: str) -> str:
    return hashlib.sha256(f"{origin}|{rel_path}".encode()).hexdigest()


def image_info(data: bytes) -> Tuple[int, int]:
    from PIL import Image
    with Image.open(io.BytesIO(data)) as im:
        w, h = im.size
        im.verify()
    if w <= 0 or h <= 0:
        raise ValueError("empty image")
    return w, h


def project_has_annotations(sync_db, project_id: str) -> bool:
    return sync_db.images.count_documents({"project_id": project_id, "annotations.0": {"$exists": True}}, limit=1) > 0


def scan_zip_sync(sync_db, batch_id: str, should_stop: Callable[[], bool], *, max_image_bytes: int,
                  max_files: int, max_total_bytes: int) -> None:
    """Populate import_items + report for a batch whose zip is already on disk. Sets status ready/failed."""
    batch = sync_db.import_batches.find_one({"id": batch_id})
    pid, origin = batch["project_id"], batch["origin"]

    def progress(done, total):
        sync_db.import_batches.update_one({"id": batch_id}, {"$set": {"progress": {"done": done, "total": total}, "updated_at": _now()}})

    try:
        sync_db.import_items.delete_many({"batch_id": batch_id})
        try:
            zf = zipfile.ZipFile(batch["zip_path"])
        except zipfile.BadZipFile:
            raise ImportError_("The file is not a valid zip archive")
        with zf:
            infos = [i for i in zf.infolist() if not i.is_dir()]
            if len(infos) > max_files:
                raise ImportError_(f"The zip contains {len(infos)} files (limit {max_files})")
            if sum(i.file_size for i in infos) > max_total_bytes:
                raise ImportError_("The zip's uncompressed size is over the import limit")
            names = [n for n in (_clean(i.filename) for i in infos) if n]
            source = formats.open_source(zf, names, batch.get("source_format") or "auto", batch.get("label_format") or "auto")
            server_names, entries = source["names"], source["entries"]
            # what the commit step must know to read labels exactly like this scan does
            resolved = {"resolved_source_format": source["kind"], "resolved_label_format": source["label_format"],
                        "server_names": server_names, "data_yaml_member": source["data_yaml_member"]}
            if source.get("cat_map") is not None:
                resolved["coco_cat_map"] = source["cat_map"]
            sync_db.import_batches.update_one({"id": batch_id}, {"$set": resolved})
            reader = formats.LabelReader(zf, {**batch, **resolved})
            total = len(entries)
            progress(0, total)

            seen_hash: Dict[str, str] = {}
            rows: List[dict] = []
            total_bytes = 0
            for n_done, e in enumerate(entries, 1):
                if n_done % 100 == 0 and should_stop():
                    return
                has_labels = bool(e.get("label_path") or e.get("label_ref"))
                row = {"id": str(uuid.uuid4()), "batch_id": batch_id, "project_id": pid,
                       "origin_key": origin_key(origin, e["rel_path"]), "rel_path": e["rel_path"],
                       "label_path": e.get("label_path"), "label_ref": e.get("label_ref"), "split": e["split"],
                       "filename": e["filename"], "status": "scanned", "sha256": None, "size": 0, "width": None,
                       "height": None, "had_label_file": has_labels, "n_boxes": 0, "class_counts": {},
                       "n_unsupported": 0, "n_malformed": 0, "error": None}
                size = zf.getinfo(e["rel_path"]).file_size
                row["size"] = size
                data = read_capped(zf, e["rel_path"], max_image_bytes)
                if data is None:
                    row.update(status="too_large", error=f"over the {max_image_bytes // (1024 * 1024)} MB per-image limit")
                else:
                    total_bytes += size
                    row["sha256"] = hashlib.sha256(data).hexdigest()
                    try:
                        row["width"], row["height"] = image_info(data)
                    except Exception as ex:  # PIL raises many types for broken files
                        row.update(status="corrupt", error=str(ex)[:200])
                    if row["status"] == "scanned":
                        if row["sha256"] in seen_hash:
                            row.update(status="duplicate_in_batch", duplicate_of=seen_hash[row["sha256"]])
                        else:
                            seen_hash[row["sha256"]] = e["rel_path"]
                if has_labels and row["status"] in ("scanned", "duplicate_in_batch"):
                    parsed = reader.read(row, (row["width"], row["height"]))
                    row["n_boxes"] = len(parsed.boxes)
                    row["class_counts"] = {str(k): v for k, v in Counter(b["cls"] for b in parsed.boxes).items()}
                    row["n_unsupported"] = len(parsed.unsupported)
                    row["n_malformed"] = len(parsed.malformed)
                rows.append(row)
                if len(rows) >= 500:
                    sync_db.import_items.insert_many(rows)
                    rows = []
                    progress(n_done, total)
            if rows:
                sync_db.import_items.insert_many(rows)
            progress(total, total)

        _mark_existing(sync_db, batch_id, pid)
        report = build_report(sync_db, batch, source, total_bytes)
        sync_db.import_batches.update_one({"id": batch_id}, {"$set": {
            "status": "ready", "phase": "ready", "report": report, "error": None, "updated_at": _now()}})
    except ImportError_ as e:
        sync_db.import_batches.update_one({"id": batch_id}, {"$set": {
            "status": "failed", "phase": "failed", "active": False, "error": str(e), "updated_at": _now()}})
    except Exception as e:
        sync_db.import_batches.update_one({"id": batch_id}, {"$set": {
            "status": "failed", "phase": "failed", "active": False, "error": f"Scan failed: {str(e)[:300]}", "updated_at": _now()}})


def _mark_existing(sync_db, batch_id: str, pid: str) -> None:
    """Flag items already in the project: same content hash, same origin already imported, or imported then deleted."""
    items = list(sync_db.import_items.find({"batch_id": batch_id, "status": {"$in": ["scanned", "duplicate_in_batch"]}},
                                           {"_id": 0, "id": 1, "sha256": 1, "origin_key": 1, "status": 1}))
    ops: List[UpdateOne] = []
    for i in range(0, len(items), 500):
        chunk = items[i:i + 500]
        hashes = list({it["sha256"] for it in chunk if it["sha256"]})
        by_hash = {d["content_hash"]: d["id"] for d in sync_db.images.find(
            {"project_id": pid, "content_hash": {"$in": hashes}}, {"_id": 0, "id": 1, "content_hash": 1})}
        keys = [it["origin_key"] for it in chunk]
        present_keys = {d["provenance"]["origin_key"] for d in sync_db.images.find(
            {"project_id": pid, "provenance.origin_key": {"$in": keys}}, {"_id": 0, "provenance.origin_key": 1})}
        gone_keys = {d["origin_key"] for d in sync_db.import_items.find(
            {"project_id": pid, "origin_key": {"$in": keys}, "status": "imported", "batch_id": {"$ne": batch_id}},
            {"_id": 0, "origin_key": 1})} - present_keys
        for it in chunk:
            if it["origin_key"] in present_keys:
                ops.append(UpdateOne({"id": it["id"]}, {"$set": {"status": "already_imported"}}))
            elif it["origin_key"] in gone_keys:
                ops.append(UpdateOne({"id": it["id"]}, {"$set": {"status": "previously_deleted"}}))
            elif it["status"] == "scanned" and it["sha256"] in by_hash:
                ops.append(UpdateOne({"id": it["id"]}, {"$set": {"status": "duplicate_in_project", "duplicate_of": by_hash[it["sha256"]]}}))
    if ops:
        sync_db.import_items.bulk_write(ops, ordered=False)


def build_report(sync_db, batch: dict, source: dict, total_bytes: int) -> dict:
    bid, pid = batch["id"], batch["project_id"]
    server_names = source["names"]
    status_counts = Counter()
    split_counts = Counter()
    class_boxes = Counter()
    no_label = empty_label = unsupported_imgs = malformed_lines = 0
    for it in sync_db.import_items.find({"batch_id": bid}, {"_id": 0, "status": 1, "split": 1, "class_counts": 1, "had_label_file": 1,
                                                            "n_boxes": 1, "n_unsupported": 1, "n_malformed": 1}):
        status_counts[it["status"]] += 1
        if it["status"] != "scanned":
            continue
        split_counts[it["split"] or "unsplit"] += 1
        for k, v in it["class_counts"].items():
            class_boxes[int(k)] += v
        if not it["had_label_file"]:
            no_label += 1
        elif it["n_boxes"] == 0 and not it["n_unsupported"]:
            empty_label += 1
        if it["n_unsupported"]:
            unsupported_imgs += 1
        malformed_lines += it["n_malformed"]
    nc = len(server_names)
    project = sync_db.projects.find_one({"id": pid}, {"classes": 1}) or {}
    project_classes = project.get("classes", [])
    notes = source.get("notes") or {}
    return {
        "origin": batch["origin"], "data_yaml": source["description"], "server_names": server_names, "nc": nc,
        "source_format": source["kind"], "label_format": source["label_format"], "names_order": notes.get("names_order"),
        "missing_files": notes.get("missing_files", 0),
        "total_images": sum(status_counts.values()), "importable": status_counts["scanned"],
        "counts_by_split": dict(split_counts),
        "class_distribution": [{"idx": i, "name": server_names[i], "boxes": class_boxes.get(i, 0)} for i in range(nc)],
        "unknown_class_indices": [{"idx": i, "boxes": n} for i, n in sorted(class_boxes.items()) if i >= nc],
        "images_without_labels": no_label, "images_with_empty_labels": empty_label,
        "corrupt": status_counts["corrupt"],
        # over-size files found on the server are never copied, so they only show up in the staging notes
        "too_large": status_counts["too_large"] + (batch.get("staging") or {}).get("skipped_too_large", 0),
        "missing_dirs": (batch.get("staging") or {}).get("missing_dirs", []),
        "duplicates": {"in_project": status_counts["duplicate_in_project"], "in_batch": status_counts["duplicate_in_batch"],
                       "already_imported": status_counts["already_imported"],
                       "previously_deleted": status_counts["previously_deleted"]},
        "unsupported_format": unsupported_imgs, "malformed_lines": malformed_lines,
        "ignored_files": source["ignored"], "total_bytes": total_bytes,
        "legacy_unhashed_images": sync_db.images.count_documents({"project_id": pid, "content_hash": {"$exists": False}}),
        "project_classes": project_classes,
        "can_replace_classes": not project_has_annotations(sync_db, pid),
        "class_mapping": class_map_mod.default_mapping(server_names, project_classes),
    }
