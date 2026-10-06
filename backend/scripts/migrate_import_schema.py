"""Prepare existing data for dataset import: indexes, plus `source` / `group_key` on legacy images.

* Indexes (also created at server startup; running this first is optional and safe):
  images (project_id, content_hash), images (project_id, split), unique-partial images (project_id,
  provenance.origin_key), import_batches / import_items lookups, and a unique-partial import_batches index
  that allows one in-flight import per project.
* Legacy images get `source` = "video" (frames, with `group_key` = their video id so frames of one video can
  later share a split) or "upload". `content_hash` is NOT set here: see scripts/backfill_content_hash.py.

Usage (from backend/):
    python scripts/migrate_import_schema.py            # dry run: report only
    python scripts/migrate_import_schema.py --apply    # create indexes, set the missing fields
                                                       # (backs up the affected image ids to a JSON file first)
Safe to re-run: only images without `source` are touched, and index creation is idempotent.
"""
import argparse
import asyncio
import json
from datetime import datetime
from pathlib import Path

from dotenv import dotenv_values
from motor.motor_asyncio import AsyncIOMotorClient

ENV = dotenv_values(Path(__file__).resolve().parent.parent / ".env")

INDEXES = [
    ("images", [("project_id", 1), ("content_hash", 1)], {}),
    ("images", [("project_id", 1), ("split", 1)], {}),
    ("images", [("project_id", 1), ("provenance.origin_key", 1)],
     {"unique": True, "partialFilterExpression": {"provenance.origin_key": {"$type": "string"}}}),
    ("import_batches", [("project_id", 1), ("created_at", -1)], {}),
    ("import_batches", [("id", 1)], {}),
    ("import_batches", [("project_id", 1)], {"unique": True, "partialFilterExpression": {"active": True}}),
    ("import_items", [("batch_id", 1), ("status", 1)], {}),
    ("import_items", [("project_id", 1), ("origin_key", 1)], {}),
]


async def main(apply: bool):
    db = AsyncIOMotorClient(ENV["MONGO_URL"])[ENV["DB_NAME"]]
    print(f"db={ENV['DB_NAME']}")
    missing_source = {"source": {"$exists": False}}
    frames = await db.images.count_documents({**missing_source, "from_video": {"$exists": True}})
    uploads = await db.images.count_documents({**missing_source, "from_video": {"$exists": False}})
    print(f"images without `source`: {frames} video frames, {uploads} uploads")
    if not apply:
        print("dry run: re-run with --apply to create the indexes and set the fields")
        return
    ids = [d["id"] async for d in db.images.find(missing_source, {"_id": 0, "id": 1})]
    if ids:
        backup = Path(f"import_schema_backup_{datetime.now():%Y%m%d_%H%M%S}.json")
        backup.write_text(json.dumps(ids))
        print(f"saved {len(ids)} image ids to {backup}")
    res = await db.images.update_many({**missing_source, "from_video": {"$exists": True}},
                                      [{"$set": {"source": "video", "group_key": "$from_video"}}])
    print(f"video frames updated: {res.modified_count}")
    res = await db.images.update_many({**missing_source, "from_video": {"$exists": False}}, {"$set": {"source": "upload"}})
    print(f"uploads updated: {res.modified_count}")
    for coll, keys, opts in INDEXES:
        name = await db[coll].create_index(keys, **opts)
        print(f"index ok: {coll}.{name}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    asyncio.run(main(ap.parse_args().apply))
