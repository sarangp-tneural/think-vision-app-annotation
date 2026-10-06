"""Indexes for the dataset merge: plan lookups, and "at most one busy run per pipeline" enforced by the database.

The server creates these at startup too (and logs an error if the unique one can't be built). This script does it
explicitly and tells you why when it can't: the unique index fails if some pipeline already has two runs marked
`busy` (a flag left over from a crashed worker). It never clears those flags itself.

No documents are changed, only indexes are created, so there is nothing to back up. Idempotent.

Usage (from backend/):
    python scripts/migrate_merge_indexes.py            # dry run: report stuck busy runs, list indexes to create
    python scripts/migrate_merge_indexes.py --apply    # create the indexes
To clear a stuck flag by hand once you have checked the run really is dead:
    db.pipeline_runs.updateOne({id: "<run id>"}, {$set: {busy: false}})
"""
import argparse
import asyncio
from pathlib import Path

from dotenv import dotenv_values
from motor.motor_asyncio import AsyncIOMotorClient

ENV = dotenv_values(Path(__file__).resolve().parent.parent / ".env")

INDEXES = [
    ("merge_plans", [("run_id", 1), ("created_at", -1)], {}),
    ("merge_plans", [("id", 1)], {}),
    ("merge_plan_items", [("plan_id", 1), ("category", 1)], {}),
    ("pipeline_runs", [("pipeline_id", 1)], {"unique": True, "partialFilterExpression": {"busy": True}}),
]


async def main(apply: bool):
    db = AsyncIOMotorClient(ENV["MONGO_URL"])[ENV["DB_NAME"]]
    print(f"db={ENV['DB_NAME']}")
    stuck = await db.pipeline_runs.aggregate([
        {"$match": {"busy": True}},
        {"$group": {"_id": "$pipeline_id", "runs": {"$push": "$id"}, "n": {"$sum": 1}}},
        {"$match": {"n": {"$gt": 1}}},
    ]).to_list(None)
    for s in stuck:
        print(f"pipeline {s['_id']} has {s['n']} busy runs: {s['runs']}")
    if stuck:
        print("the unique 'one busy run per pipeline' index cannot be created until these are resolved")
    for coll, keys, opts in INDEXES:
        print(f"{'create' if apply else 'would create'}: {coll} {keys} {opts or ''}")
        if not apply:
            continue
        if opts.get("unique") and stuck:
            print("  skipped (see above)")
            continue
        print(f"  ok: {await db[coll].create_index(keys, **opts)}")
    if not apply:
        print("dry run: re-run with --apply")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    asyncio.run(main(ap.parse_args().apply))
