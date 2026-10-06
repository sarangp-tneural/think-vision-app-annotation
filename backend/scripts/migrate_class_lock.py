"""Add the class-lock fields to existing projects. Never reorders or edits project.classes.

Class order is the class index used by export, training and deployment. The server now keeps that
list append-only. This script only sets `classes_locked: false` on projects that lack the field
(existing projects stay unlocked; a lock is applied later, e.g. by a dataset import).

Usage (from backend/):
    python scripts/migrate_class_lock.py            # dry run: report only
    python scripts/migrate_class_lock.py --apply    # set classes_locked=false where missing
                                                    # (backs up the affected project ids to a JSON file first)
Safe to re-run: it only touches projects where classes_locked does not exist.
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
    total = await db.projects.count_documents({})
    locked = await db.projects.count_documents({"classes_locked": True})
    missing = await db.projects.find(
        {"classes_locked": {"$exists": False}}, {"_id": 0, "id": 1, "name": 1, "classes": 1}
    ).to_list(None)
    print(f"projects: {total}, already locked: {locked}, missing the field: {len(missing)}")
    for p in missing:
        print(f"  {p['name'][:32]:32} classes={len(p.get('classes', []))}")
    if not apply or not missing:
        if missing:
            print("dry run: re-run with --apply to set classes_locked=false on them")
        return
    backup = Path(f"class_lock_backup_{datetime.now():%Y%m%d_%H%M%S}.json")
    backup.write_text(json.dumps([m["id"] for m in missing]))
    res = await db.projects.update_many(
        {"classes_locked": {"$exists": False}}, {"$set": {"classes_locked": False}}
    )
    print(f"updated {res.modified_count} projects (ids saved to {backup})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    asyncio.run(main(ap.parse_args().apply))
