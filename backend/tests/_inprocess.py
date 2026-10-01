"""One in-process app + throwaway database shared by the self-contained API test modules.

The server module (and its Motor client) can only be bound to a single event loop per process,
so every module must reuse the same TestClient instead of creating its own.
"""
import atexit
import os
import sys
from pathlib import Path

os.environ["DB_NAME"] = "thinkvision_test_inprocess"
os.environ["ALLOWED_EMAIL_DOMAIN"] = ""
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
from pymongo import MongoClient  # noqa: E402

import server  # noqa: E402

_client = None


def get_client():
    global _client
    if _client is None:
        _client = TestClient(server.app)
        _client.__enter__()  # keeps one event loop (and runs startup) for the whole session
        atexit.register(_shutdown)
    return _client


def _shutdown():
    _client.__exit__(None, None, None)
    MongoClient(os.environ["MONGO_URL"]).drop_database(os.environ["DB_NAME"])


def fresh_db():
    """Empty the throwaway database; each module starts from nothing."""
    db = MongoClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"]]
    for name in db.list_collection_names():
        db[name].delete_many({})
    return db
