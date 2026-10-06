"""Fingerprint images uploaded before content hashes existed, so duplicate detection can see them.

Fetches each image without a `content_hash` from object storage, stores its sha256 and (if missing) its
width/height. Resumable and idempotent: it only touches images where `content_hash` does not exist, so an
interrupted run just continues. A failed fetch is reported and skipped; re-run to retry it.

Usage (from backend/):
    python scripts/backfill_content_hash.py                     # dry run: count what is missing per project
    python scripts/backfill_content_hash.py --apply             # hash everything missing
    python scripts/backfill_content_hash.py --apply --limit 500 # hash at most 500 images, then stop
    python scripts/backfill_content_hash.py --apply --workers 8 # parallel storage fetches (default 4)
Before writing, --apply saves the ids it is about to touch to a JSON file in the current directory.
"""
import argparse
import hashlib
import io
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
MISSING = {"content_hash": {"$exists": False}, "storage_path": {"$type": "string"}}


def _dimensions(data: bytes):
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            return im.size
    except Exception:
        return None, None


def backfill(db, get_object, *, apply: bool, limit=None, workers: int = 4, log=print) -> dict:
    """Core of the script (db = sync pymongo database, get_object(path) -> (bytes, content_type))."""
    per_project = {}
    for row in db.images.aggregate([{"$match": MISSING}, {"$group": {"_id": "$project_id", "n": {"$sum": 1}}}]):
        per_project[row["_id"]] = row["n"]
    total = sum(per_project.values())
    log(f"images without a content hash: {total}")
    for pid, n in sorted(per_project.items(), key=lambda kv: -kv[1]):
        p = db.projects.find_one({"id": pid}, {"name": 1}) or {}
        log(f"  {str(p.get('name', pid))[:32]:32} {n:7}")
    result = {"missing": total, "hashed": 0, "failed": 0}
    if not apply or not total:
        if total:
            log("dry run: re-run with --apply to hash them")
        return result

    cursor = db.images.find(MISSING, {"_id": 0, "id": 1, "storage_path": 1})
    if limit:
        cursor = cursor.limit(limit)
    todo = list(cursor)
    backup = Path(f"content_hash_backup_{datetime.now():%Y%m%d_%H%M%S}.json")
    backup.write_text(json.dumps([d["id"] for d in todo]))
    log(f"saved {len(todo)} image ids to {backup}")

    def work(doc):
        try:
            data, _ct = get_object(doc["storage_path"])
        except Exception as e:  # storage hiccup / object gone: leave the image unhashed
            return doc["id"], None, str(e)[:120]
        w, h = _dimensions(data)
        return doc["id"], {"content_hash": hashlib.sha256(data).hexdigest(), "width": w, "height": h}, None

    with ThreadPoolExecutor(workers) as pool:
        for i, (img_id, fields, err) in enumerate(pool.map(work, todo), 1):
            if fields is None:
                result["failed"] += 1
                log(f"  failed {img_id}: {err}")
                continue
            # width/height only if the image had none (uploads before this feature never stored them)
            db.images.update_one({"id": img_id, "content_hash": {"$exists": False}}, {"$set": {"content_hash": fields["content_hash"]}})
            if fields["width"]:
                db.images.update_one({"id": img_id, "width": {"$exists": False}}, {"$set": {"width": fields["width"], "height": fields["height"]}})
            result["hashed"] += 1
            if i % 200 == 0:
                log(f"  {i}/{len(todo)}")
    log(f"hashed {result['hashed']}, failed {result['failed']} (re-run to retry failures)")
    return result


def main():
    from dotenv import dotenv_values
    from pymongo import MongoClient
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()
    env = dotenv_values(BACKEND / ".env")
    db = MongoClient(env["MONGO_URL"])[env["DB_NAME"]]
    print(f"db={env['DB_NAME']}")
    get_object = None
    if args.apply:
        sys.path.insert(0, str(BACKEND))
        import server  # reuses the app's storage client (init + get_object); no server is started
        server.init_storage()
        get_object = server.get_object
    backfill(db, get_object, apply=args.apply, limit=args.limit, workers=args.workers)


if __name__ == "__main__":
    main()
