"""Create or update a company manager account (global, read-only dashboard role).

Usage (from backend/):
    python scripts/create_manager.py --email you@tneuralai.com --name "Company Manager"
The email must belong to the company domain (ALLOWED_EMAIL_DOMAIN in .env, default
tneuralai.com). The password is prompted (never passed on the command line). Re-running for
an existing manager email resets its password.
"""
import argparse
import asyncio
import getpass
import uuid
from datetime import datetime, timezone
from pathlib import Path

import bcrypt
from dotenv import dotenv_values
from motor.motor_asyncio import AsyncIOMotorClient

ENV = dotenv_values(Path(__file__).resolve().parent.parent / ".env")
DOMAIN = (ENV.get("ALLOWED_EMAIL_DOMAIN") or "").strip().lower() or "tneuralai.com"


def validate_manager_email(email: str, domain: str = DOMAIN) -> str:
    """Return the normalised email, or raise ValueError if it is not a company address."""
    email = email.strip().lower()
    local, _, host = email.partition("@")
    if not local or host != domain:
        raise ValueError(f"Manager email must be an @{domain} address")
    return email


async def main(email: str, name: str, password: str):
    db = AsyncIOMotorClient(ENV["MONGO_URL"])[ENV["DB_NAME"]]
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    existing = await db.users.find_one({"email": email})
    if existing:
        if existing.get("role") != "manager":
            raise SystemExit(f"{email} is already a regular team account; use a different email")
        await db.users.update_one({"id": existing["id"]}, {"$set": {"password": hashed, "name": name}})
        print(f"Updated manager {email}")
        return
    await db.users.insert_one({
        "id": str(uuid.uuid4()), "role": "manager", "email": email, "name": name, "password": hashed,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    print(f"Created manager {email}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True)
    ap.add_argument("--name", default="Manager")
    args = ap.parse_args()
    try:
        email = validate_manager_email(args.email)
    except ValueError as e:
        raise SystemExit(str(e))
    pw = getpass.getpass("Password: ")
    if len(pw) < 8:
        raise SystemExit("Password must be at least 8 characters")
    if pw != getpass.getpass("Confirm: "):
        raise SystemExit("Passwords do not match")
    asyncio.run(main(email, args.name, pw))
