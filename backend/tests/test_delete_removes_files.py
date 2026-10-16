"""Deleting images / projects removes their stored files too, but keeps files another record still uses."""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402
import _inprocess  # noqa: E402
from _inprocess import fresh_db, get_client  # noqa: E402

NOW = datetime.now(timezone.utc).isoformat()
server = _inprocess.server


@pytest.fixture(scope="module")
def db():
    yield fresh_db()


@pytest.fixture(scope="module")
def client(db):
    return get_client()


@pytest.fixture()
def world(client, db, tmp_path, monkeypatch):
    monkeypatch.setattr(server, "STORAGE_DIR", str(tmp_path))
    r = client.post("/api/auth/register", json={"email": f"del{tmp_path.name[-6:]}@example.com", "password": "pw123456", "name": "d"})
    hdr = {"Authorization": f"Bearer {r.json()['token']}"}
    team = next(t for t in client.get("/api/teams", headers=hdr).json() if t["is_personal"])
    pid = client.post("/api/projects", json={"name": "Del", "team_id": team["id"]}, headers=hdr).json()["id"]
    return {"hdr": hdr, "pid": pid, "root": tmp_path}


def _add(db, world, img_id, path=None):
    path = path or f"{server.APP_NAME}/projects/{world['pid']}/{img_id}.jpg"
    server.put_object(path, b"x", "image/jpeg")
    db.images.insert_one({"id": img_id, "project_id": world["pid"], "storage_path": path, "review_status": "unassigned",
                          "annotations": [], "created_at": NOW})
    return path


def test_bulk_delete_removes_files(client, db, world):
    a, b = _add(db, world, "da"), _add(db, world, "db")
    r = client.post(f"/api/projects/{world['pid']}/images/bulk-delete", json={"image_ids": ["da"]}, headers=world["hdr"])
    assert r.status_code == 200, r.text
    assert not (world["root"] / a).exists()
    assert (world["root"] / b).exists()


def test_shared_file_is_kept_while_another_image_uses_it(client, db, world):
    shared = _add(db, world, "s1")
    db.images.insert_one({"id": "s2", "project_id": world["pid"], "storage_path": shared, "review_status": "unassigned",
                          "annotations": [], "created_at": NOW})
    client.post(f"/api/projects/{world['pid']}/images/bulk-delete", json={"image_ids": ["s1"]}, headers=world["hdr"])
    assert (world["root"] / shared).exists()


def test_delete_project_removes_its_folder_only(client, db, world):
    mine = _add(db, world, "p1")
    other = f"{server.APP_NAME}/projects/other-project/o.jpg"
    server.put_object(other, b"o", "image/jpeg")
    assert client.delete(f"/api/projects/{world['pid']}", headers=world["hdr"]).status_code == 200
    assert not (world["root"] / mine).exists()
    assert not (world["root"] / server.APP_NAME / "projects" / world["pid"]).exists()
    assert (world["root"] / other).exists()
