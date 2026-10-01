"""Remove leftover test data (test_* users, their workspaces and memberships, test_* pending invites).

Usage (from backend/):
    python scripts/cleanup_test_data.py            # dry run, prints what would be deleted
    python scripts/cleanup_test_data.py --apply    # backs up to a JSON file, then deletes

Matches users and pending invites whose email starts with "test_". Teams are only
deleted when they are personal workspaces owned by a matched user and hold no projects.
"""
import argparse
import asyncio
import json
from datetime import datetime
from pathlib import Path

from dotenv import dotenv_values
from motor.motor_asyncio import AsyncIOMotorClient

ENV = dotenv_values(Path(__file__).resolve().parent.parent / ".env")
TEST_EMAIL = {"$regex": "^test_", "$options": "i"}


async def main(apply: bool):
    db = AsyncIOMotorClient(ENV["MONGO_URL"])[ENV["DB_NAME"]]
    users = await db.users.find({"email": TEST_EMAIL}, {"_id": 0}).to_list(None)
    user_ids = [u["id"] for u in users]
    pending = await db.team_members.find(
        {"status": "pending", "invited_email": TEST_EMAIL}, {"_id": 0}
    ).to_list(None)
    memberships = await db.team_members.find({"user_id": {"$in": user_ids}}, {"_id": 0}).to_list(None)
    teams = await db.teams.find(
        {"owner_id": {"$in": user_ids}, "is_personal": True}, {"_id": 0}
    ).to_list(None)
    teams = [t for t in teams if not await db.projects.count_documents({"team_id": t["id"]})]
    team_ids = [t["id"] for t in teams]
    team_rows = await db.team_members.find({"team_id": {"$in": team_ids}}, {"_id": 0}).to_list(None)

    print(f"db={ENV['DB_NAME']}: {len(users)} users, {len(teams)} teams, "
          f"{len(pending)} pending invites, {len(memberships)} user memberships")
    if not apply:
        print("dry run; pass --apply to delete")
        return

    backup = Path(f"cleanup_backup_{datetime.now():%Y%m%d_%H%M%S}.json")
    backup.write_text(json.dumps(
        {"users": users, "teams": teams, "pending": pending,
         "memberships": memberships, "team_rows": team_rows}, default=str, indent=1))
    print("backup:", backup.resolve())

    await db.team_members.delete_many({"status": "pending", "invited_email": TEST_EMAIL})
    await db.team_members.delete_many({"user_id": {"$in": user_ids}})
    await db.team_members.delete_many({"team_id": {"$in": team_ids}})
    await db.teams.delete_many({"id": {"$in": team_ids}})
    await db.users.delete_many({"id": {"$in": user_ids}})
    print("deleted")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    asyncio.run(main(ap.parse_args().apply))
