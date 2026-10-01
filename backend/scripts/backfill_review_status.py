"""Report (and optionally normalise) images affected by "annotated means approved".

Counts, versions, exports and training now use only images with review_status == "approved".
Images that already have boxes but were never reviewed stay as they are; they just stop counting
until they go through submit -> review. This prints, per project, how many images that affects.

Usage (from backend/):
    python scripts/backfill_review_status.py            # dry run: report only
    python scripts/backfill_review_status.py --apply    # also set a missing review_status to "unassigned"
                                                        # (backs up the affected ids to a JSON file first)
"""
import argparse
import asyncio
import json
from datetime import datetime
from pathlib import Path

from dotenv import dotenv_values
from motor.motor_asyncio import AsyncIOMotorClient

ENV = dotenv_values(Path(__file__).resolve().parent.parent / ".env")


async def main(apply: bool):
    db = AsyncIOMotorClient(ENV["MONGO_URL"])[ENV["DB_NAME"]]
    print(f"db={ENV['DB_NAME']}")
    print(f"{'project':32} {'images':>7} {'approved':>9} {'boxes, not approved':>20}")
    async for p in db.projects.find({}, {"_id": 0, "id": 1, "name": 1}):
        pid = p["id"]
        total = await db.images.count_documents({"project_id": pid})
        approved = await db.images.count_documents({"project_id": pid, "review_status": "approved"})
        pending = await db.images.count_documents(
            {"project_id": pid, "annotated": True, "review_status": {"$ne": "approved"}})
        print(f"{p['name'][:32]:32} {total:7} {approved:9} {pending:20}")
    missing = await db.images.find({"review_status": {"$exists": False}}, {"_id": 0, "id": 1}).to_list(None)
    print(f"\nimages with no review_status: {len(missing)}")
    if not apply or not missing:
        if missing:
            print("dry run: re-run with --apply to set them to 'unassigned'")
        return
    backup = Path(f"review_status_backup_{datetime.now():%Y%m%d_%H%M%S}.json")
    backup.write_text(json.dumps([m["id"] for m in missing]))
    res = await db.images.update_many({"review_status": {"$exists": False}}, {"$set": {"review_status": "unassigned"}})
    print(f"updated {res.modified_count} images (ids saved to {backup})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    asyncio.run(main(ap.parse_args().apply))
