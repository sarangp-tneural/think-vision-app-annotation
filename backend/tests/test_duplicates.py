"""Duplicate detection on normal uploads, the duplicates listing, and the content-hash backfill script."""
import hashlib
import importlib.util
import io
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402
import _inprocess  # noqa: E402
from _inprocess import fresh_db, get_client  # noqa: E402

NOW = datetime.now(timezone.utc).isoformat()


def _png(color, size=(8, 8)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


RED, GREEN = _png((255, 0, 0)), _png((0, 255, 0), (10, 6))


@pytest.fixture(scope="module")
def db():
    yield fresh_db()


@pytest.fixture(scope="module")
def client(db):
    return get_client()


def _register(client, name):
    r = client.post("/api/auth/register", json={"email": f"{name}@example.com", "password": "pw123456", "name": name})
    assert r.status_code == 200, r.text
    return r.json()["user"]["id"], {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture(scope="module")
def world(client):
    w = {}
    _, w["owner"] = _register(client, "downer")
    team = next(t for t in client.get("/api/teams", headers=w["owner"]).json() if t["is_personal"])["id"]
    for name, role in (("dann", "annotator"), ("drev", "reviewer")):
        _, w[name] = _register(client, name)
        assert client.post(f"/api/teams/{team}/invite", json={"email": f"{name}@example.com", "role": role}, headers=w["owner"]).status_code == 200
    w["team"] = team
    return w


@pytest.fixture(autouse=True)
def storage(monkeypatch):
    monkeypatch.setattr(_inprocess.server, "put_object", lambda path, data, ct: {"path": path, "size": len(data)})


def project(client, world):
    return client.post("/api/projects", json={"name": "Dup", "team_id": world["team"]}, headers=world["owner"]).json()["id"]


def up(client, world, pid, data, name="x.png", who="owner", force=False):
    return client.post(f"/api/projects/{pid}/images" + ("?force=true" if force else ""),
                       files={"file": (name, data, "image/png")}, headers=world[who])


def test_second_upload_of_identical_bytes_is_refused_with_a_pointer_to_the_original(client, db, world):
    pid = project(client, world)
    first = up(client, world, pid, RED, "first.png")
    assert first.status_code == 200
    again = up(client, world, pid, RED, "renamed.png", who="dann")  # a different name doesn't matter, bytes do
    assert again.status_code == 409
    d = again.json()["detail"]
    assert d["duplicate_of"] == first.json()["id"] and d["filename"] == "first.png" and "first.png" in d["message"]
    assert db.images.count_documents({"project_id": pid}) == 1  # nothing was stored


def test_force_creates_the_copy_and_other_content_or_projects_are_unaffected(client, db, world):
    pid, other = project(client, world), project(client, world)
    assert up(client, world, pid, RED).status_code == 200
    assert up(client, world, pid, GREEN).status_code == 200  # different bytes
    assert up(client, world, other, RED).status_code == 200  # same bytes, different project
    forced = up(client, world, pid, RED, "copy.png", force=True)
    assert forced.status_code == 200
    hashes = [i["content_hash"] for i in db.images.find({"project_id": pid})]
    assert hashes.count(hashlib.sha256(RED).hexdigest()) == 2 and len(hashes) == 3


def test_reviewers_still_cannot_upload_and_unreadable_files_are_still_stored(client, db, world):
    pid = project(client, world)
    assert up(client, world, pid, RED, who="drev").status_code == 403
    assert up(client, world, pid, b"not an image", "junk.png").status_code == 200  # as before: stored without dimensions
    assert up(client, world, pid, b"not an image", "junk2.png").status_code == 409  # but its bytes still count


def test_duplicates_listing_groups_identical_images_oldest_first(client, db, world):
    pid = project(client, world)
    a = up(client, world, pid, RED, "a.png").json()["id"]
    up(client, world, pid, GREEN, "g.png")
    b = up(client, world, pid, RED, "b.png", force=True).json()["id"]
    c = up(client, world, pid, RED, "c.png", force=True).json()["id"]
    # an imported image with the same bytes belongs to the same group
    db.images.insert_one({"id": "imp", "project_id": pid, "filename": "i.jpg", "content_hash": hashlib.sha256(GREEN).hexdigest(),
                          "source": "import", "review_status": "approved", "created_at": "2099-01-01", "storage_path": "p/i"})
    db.images.insert_one({"id": "legacy", "project_id": pid, "filename": "old.png", "created_at": NOW})  # no hash yet
    r = client.get(f"/api/projects/{pid}/duplicates", headers=world["owner"])
    assert r.status_code == 200
    out = r.json()
    assert out["unhashed_images"] == 1
    assert [g["count"] for g in out["groups"]] == [3, 2]
    assert [i["id"] for i in out["groups"][0]["images"]] == [a, b, c]
    assert [i["source"] for i in out["groups"][1]["images"]] == ["upload", "import"]
    assert client.get(f"/api/projects/{pid}/duplicates", headers=world["drev"]).status_code == 200  # read-only, any member
    other = project(client, world)
    assert client.get(f"/api/projects/{other}/duplicates", headers=world["owner"]).json() == {"groups": [], "unhashed_images": 0}


def _load_script():
    spec = importlib.util.spec_from_file_location("backfill_content_hash", Path(__file__).resolve().parent.parent / "scripts" / "backfill_content_hash.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_backfill_hashes_missing_images_is_resumable_and_idempotent(db, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # the backup file lands here
    mod = _load_script()
    objects = {"s/a": RED, "s/b": RED, "s/c": GREEN}
    db.images.delete_many({})
    db.projects.delete_many({})
    db.projects.insert_one({"id": "p1", "name": "P1"})
    db.images.insert_many([
        {"id": "a", "project_id": "p1", "storage_path": "s/a"}, {"id": "b", "project_id": "p1", "storage_path": "s/b"},
        {"id": "c", "project_id": "p1", "storage_path": "s/c", "width": 99, "height": 99},
        {"id": "done", "project_id": "p1", "storage_path": "s/a", "content_hash": "keep-me"},
        {"id": "nostore", "project_id": "p1"},  # no storage path: ignored
    ])
    calls = []

    def get_object(path):
        calls.append(path)
        if path == "s/b" and not calls.count("fixed"):
            raise RuntimeError("storage down")
        return objects[path], "image/png"

    logs = []
    dry = mod.backfill(db, get_object, apply=False, log=logs.append)
    assert dry == {"missing": 3, "hashed": 0, "failed": 0} and calls == []
    assert db.images.count_documents({"content_hash": {"$exists": False}, "storage_path": {"$exists": True}}) == 3
    assert any("P1" in line for line in logs)

    res = mod.backfill(db, get_object, apply=True, workers=2, log=logs.append)
    assert res == {"missing": 3, "hashed": 2, "failed": 1}
    a, b, c = (db.images.find_one({"id": i}) for i in "abc")
    assert a["content_hash"] == hashlib.sha256(RED).hexdigest() and (a["width"], a["height"]) == (8, 8)
    assert "content_hash" not in b  # failed fetch: left for the next run
    assert c["content_hash"] == hashlib.sha256(GREEN).hexdigest() and (c["width"], c["height"]) == (99, 99)  # existing size kept
    assert db.images.find_one({"id": "done"})["content_hash"] == "keep-me"  # never rewritten
    assert list(tmp_path.glob("content_hash_backup_*.json"))

    calls.clear()
    calls.append("fixed")
    res = mod.backfill(db, get_object, apply=True, log=logs.append)  # resumes: only the failed one is retried
    assert res == {"missing": 1, "hashed": 1, "failed": 0} and calls == ["fixed", "s/b"]
    assert db.images.find_one({"id": "b"})["content_hash"] == hashlib.sha256(RED).hexdigest()
    assert mod.backfill(db, get_object, apply=True, log=logs.append)["missing"] == 0


def test_backfill_limit_stops_after_n_images(db, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    mod = _load_script()
    db.images.delete_many({})
    db.images.insert_many([{"id": f"l{i}", "project_id": "p", "storage_path": "s/a"} for i in range(5)])
    res = mod.backfill(db, lambda p: (RED, "image/png"), apply=True, limit=2, log=lambda *_: None)
    assert res["hashed"] == 2 and db.images.count_documents({"content_hash": {"$exists": True}}) == 2
