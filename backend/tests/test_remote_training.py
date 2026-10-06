"""Model Training on a remote host: worker behaviour (fake SSH) and the API guards."""
import contextlib
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402
from _inprocess import fresh_db, get_client  # noqa: E402

import remote_training  # noqa: E402
import ssh_helper  # noqa: E402

PASSWORD = "s3cret-pass"
NOW = datetime.now(timezone.utc).isoformat()
BOX = [{"type": "bbox", "label": "widget", "x": .1, "y": .1, "w": .3, "h": .3}]


@pytest.fixture(scope="module")
def db():
    yield fresh_db()


@pytest.fixture(scope="module")
def client(db):
    return get_client()


def _seed(db, n=4):
    pid, mid = str(uuid.uuid4()), str(uuid.uuid4())
    db.projects.insert_one({"id": pid, "name": "RT", "classes": ["widget"], "created_at": NOW})
    for i in range(n):
        db.images.insert_one({"id": f"{pid}-{i}", "project_id": pid, "filename": f"a{i}.jpg", "storage_path": f"x/{pid}/{i}",
                              "annotated": True, "review_status": "approved", "annotations": BOX, "created_at": NOW})
    db.models.insert_one({"id": mid, "project_id": pid, "type": "real", "status": "queued", "created_at": NOW})
    return pid, mid


def _req(**kw):
    return {"host": "h", "port": 22, "username": "u", "password": PASSWORD, "pem_key": None, "remote_workdir": "/w",
            "env_mode": "system", "venv_path": None, "model_arch": "yolov8n", "epochs": 3, **kw}


class _Fake:
    """Replaces the SSH layer; `stream` decides what the remote training run does."""
    def __init__(self, monkeypatch, stream):
        self.cmds, self.stored = [], {}
        monkeypatch.setattr(ssh_helper, "connect", contextlib.contextmanager(lambda *a, **k: (yield object())))
        monkeypatch.setattr(ssh_helper, "resolve_remote_path", lambda c, p: p)
        monkeypatch.setattr(ssh_helper, "upload_dir", lambda *a, **k: None)
        monkeypatch.setattr(ssh_helper, "upload_file", lambda *a, **k: None)
        monkeypatch.setattr(ssh_helper, "exec_command", lambda c, cmd, **k: (self.cmds.append(cmd), (0, "", ""))[1])
        monkeypatch.setattr(ssh_helper, "exec_command_streaming", stream)
        monkeypatch.setattr(remote_training, "_prepare_python", lambda *a, **k: "python3")

        def download(client, remote, local, **k):
            with open(local, "wb") as f:
                f.write(json.dumps({"map50": .8, "map50_95": .5, "precision": .7, "recall": .6}).encode() if remote.endswith(".json") else b"WEIGHTS")
        monkeypatch.setattr(ssh_helper, "download_file", download)

    def put(self, path, data, ct):
        self.stored[path] = data


def _run(db, mid, pid, fake, **kw):
    remote_training.train_remote_sync(mid, pid, _req(**kw), lambda p: (b"\xff\xd8JPEG", "image/jpeg"), fake.put, "app")
    return db.models.find_one({"id": mid})


def test_happy_path_registers_a_server_trained_model(db, monkeypatch):
    pid, mid = _seed(db)

    def stream(client, cmd, on_output=None, **k):
        on_output("  1/3 0G 1.2 0.5 0.4\n")
        on_output("  3/3 0G 1.0 0.4 0.3\n")
        return 0, "", ""
    fake = _Fake(monkeypatch, stream)
    m = _run(db, mid, pid, fake)
    assert m["status"] == "trained" and m["source"] == "server" and m["trained_on"] == "server"
    assert m["final_mAP"] == .8 and m["precision"] == .7 and m["classes"] == ["widget"] and m["progress"] == 100
    assert fake.stored[f"app/projects/{pid}/models/{mid}.pt"] == b"WEIGHTS"
    assert PASSWORD not in json.dumps(m, default=str)


def test_failure_marks_failed_and_never_leaks_the_password(db, monkeypatch):
    pid, mid = _seed(db)
    fake = _Fake(monkeypatch, lambda c, cmd, on_output=None, **k: (1, "", f"auth error for {PASSWORD}"))
    m = _run(db, mid, pid, fake)
    assert m["status"] == "failed" and "remote training exited 1" in m["error"]
    assert PASSWORD not in json.dumps(m, default=str)


def test_cancel_stops_the_stream_and_kills_the_remote_process(db, monkeypatch):
    pid, mid = _seed(db)

    def stream(client, cmd, on_output=None, should_stop=None, **k):
        db.models.update_one({"id": mid}, {"$set": {"cancel_requested": True}})  # user clicks Cancel mid-run
        assert should_stop()
        return -1, "", ""
    fake = _Fake(monkeypatch, stream)
    m = _run(db, mid, pid, fake)
    assert m["status"] == "cancelled"
    assert any(c.startswith("pkill -f") for c in fake.cmds)
    assert not fake.stored


# ---- API guards ----
def _register(client, name):
    r = client.post("/api/auth/register", json={"email": f"{name}@example.com", "password": "pw123456", "name": name})
    assert r.status_code == 200, r.text
    return r.json()["user"]["id"], {"Authorization": f"Bearer {r.json()['token']}"}


BODY = {"host": "127.0.0.1", "port": 1, "username": "u", "password": PASSWORD, "remote_workdir": "/w", "epochs": 1}


def test_api_guards_and_failed_connection_keeps_secrets_out(client, db):
    _, owner = _register(client, "rt_owner")
    _, ann = _register(client, "rt_ann")
    team = next(t for t in client.get("/api/teams", headers=owner).json() if t["is_personal"])["id"]
    assert client.post(f"/api/teams/{team}/invite", json={"email": "rt_ann@example.com", "role": "annotator"}, headers=owner).status_code == 200
    pid = client.post("/api/projects", json={"name": "RTAPI", "team_id": team}, headers=owner).json()["id"]

    assert client.post(f"/api/projects/{pid}/train-remote", json=BODY, headers=ann).status_code == 403
    r = client.post(f"/api/projects/{pid}/train-remote", json=BODY, headers=owner)
    assert r.status_code == 400 and "approved images" in r.json()["detail"]
    assert client.post(f"/api/projects/{pid}/train-remote", json={**BODY, "env_mode": "existing"}, headers=owner).status_code == 422

    db.images.insert_many([{"id": f"rt{i}", "project_id": pid, "filename": f"f{i}.jpg", "storage_path": f"x/{i}", "annotated": True,
                            "review_status": "approved", "annotations": BOX, "created_at": NOW} for i in range(30)])
    r = client.post(f"/api/projects/{pid}/train-remote", json=BODY, headers=owner)
    assert r.status_code == 200, r.text
    assert r.json()["source"] == "server" and PASSWORD not in r.text
    m = db.models.find_one({"id": r.json()["id"]})  # background job already ran: nothing listens on port 1
    assert m["status"] == "failed" and PASSWORD not in json.dumps(m, default=str)
    form = client.get(f"/api/projects/{pid}/remote-train-form", headers=owner).json()
    assert form["host"] == "127.0.0.1" and "password" not in form
