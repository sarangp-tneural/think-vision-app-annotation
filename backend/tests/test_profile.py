"""Profile: info + stats, rename, change password (signs out old sessions), avatar upload."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402
from _inprocess import fresh_db, get_client  # noqa: E402
import server  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


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


def test_profile_info_and_rename(client):
    uid, h = _register(client, "pf_info")
    r = client.get("/api/profile", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["email"] == "pf_info@example.com" and body["role"] == "user" and "password" not in body
    assert body["stats"]["teams"] >= 1 and body["stats"]["images_annotated"] == 0
    assert client.patch("/api/profile", json={"name": "  New   Name "}, headers=h).json()["name"] == "New Name"
    assert client.get("/api/auth/me", headers=h).json()["name"] == "New Name"
    assert client.patch("/api/profile", json={"name": "   "}, headers=h).status_code == 400
    assert client.patch("/api/profile", json={"name": "x" * 81}, headers=h).status_code == 400
    assert client.get("/api/profile").status_code == 401


def test_change_password(client):
    _, old = _register(client, "pf_pw")
    bad = [("wrong", "newpass123"), ("pw123456", "short"), ("pw123456", "pw123456")]
    for cur, new in bad:
        r = client.post("/api/profile/change-password", json={"current_password": cur, "new_password": new}, headers=old)
        assert r.status_code == 400, (cur, new, r.text)  # never 401, or the UI would log out
    time.sleep(1.1)  # token iat has 1s resolution
    r = client.post("/api/profile/change-password", json={"current_password": "pw123456", "new_password": "newpass123"}, headers=old)
    assert r.status_code == 200, r.text
    new_h = {"Authorization": f"Bearer {r.json()['token']}"}
    assert client.get("/api/profile", headers=new_h).status_code == 200
    assert client.get("/api/profile", headers=old).status_code == 401
    assert client.post("/api/auth/login", json={"email": "pf_pw@example.com", "password": "pw123456"}).status_code == 400
    assert client.post("/api/auth/login", json={"email": "pf_pw@example.com", "password": "newpass123"}).status_code == 200


def test_avatar(client, tmp_path, monkeypatch):
    monkeypatch.setattr(server, "STORAGE_DIR", str(tmp_path))
    uid, h = _register(client, "pf_av")
    _, other = _register(client, "pf_av_other")
    assert client.get(f"/api/users/{uid}/avatar", headers=h).status_code == 404
    up = lambda name, data, ct: client.post("/api/profile/avatar", files={"file": (name, data, ct)}, headers=h)
    assert up("a.gif", PNG, "image/gif").status_code == 400
    assert up("a.png", b"", "image/png").status_code == 400
    assert up("a.png", PNG + b"0" * (2 * 1024 * 1024), "image/png").status_code == 400
    r = up("a.png", PNG, "image/png")
    assert r.status_code == 200 and r.json()["avatar_path"] == f"avatars/{uid}.png"
    got = client.get(f"/api/users/{uid}/avatar", headers=other)
    assert got.status_code == 200 and got.content == PNG
    assert client.get("/api/auth/me", headers=h).json()["avatar_path"] == f"avatars/{uid}.png"
    r = up("a.jpg", b"\xff\xd8\xff" + b"1" * 32, "image/jpeg")  # replacing with another type drops the old file
    assert r.json()["avatar_path"] == f"avatars/{uid}.jpg"
    assert not (tmp_path / "avatars" / f"{uid}.png").exists() and (tmp_path / "avatars" / f"{uid}.jpg").exists()
    assert client.delete("/api/profile/avatar", headers=h).json()["avatar_path"] is None
    assert client.get(f"/api/users/{uid}/avatar", headers=h).status_code == 404
    assert not (tmp_path / "avatars" / f"{uid}.jpg").exists()
