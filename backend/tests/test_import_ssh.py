"""Import of a dataset that lives on a training server (SSH/SFTP faked in memory)."""
import contextlib
import io
import json
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402
import _inprocess  # noqa: E402
from _inprocess import fresh_db, get_client  # noqa: E402

import ssh_helper  # noqa: E402
from dataset_import import ssh_source  # noqa: E402

PASSWORD = "hunter2-very-secret"
KEY_LINE = "abcdefghijklmnopqrstuvwxyz0123456789SECRETKEYBODY"
PEM = f"-----BEGIN OPENSSH PRIVATE KEY-----\n{KEY_LINE}\n-----END OPENSSH PRIVATE KEY-----"
YAML = ("path: /root/myG/myG_combined_v4\n\ntrain: train/images\nval: valid/images\ntest: test/images\n\nnc: 3\n\n"
        "names:\n  0: person\n  1: cash\n  2: id_visible\n")
ROOT = "/data/myG_combined_v4"


def _png(color, size=(8, 8)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def remote_files():
    return {
        f"{ROOT}/data.yaml": YAML.encode(),
        f"{ROOT}/train/images/a_jpg.rf.1.jpg": _png((255, 0, 0)), f"{ROOT}/train/labels/a_jpg.rf.1.txt": b"0 0.5 0.5 0.2 0.4\n1 0.25 0.25 0.1 0.1\n",
        f"{ROOT}/train/images/b.jpg": _png((0, 255, 0)), f"{ROOT}/train/labels/b.txt": b"2 0.5 0.5 0.2 0.2\n",
        f"{ROOT}/valid/images/c.png": _png((0, 0, 255), (9, 7)), f"{ROOT}/valid/labels/c.txt": b"0 0.5 0.5 0.2 0.2\n",
        f"{ROOT}/test/images/d.jpg": _png((9, 9, 9)),
        f"{ROOT}/valid/images/readme.txt": b"not an image",
        f"{ROOT}/train/labels/orphan.txt": b"0 0.5 0.5 0.2 0.2\n",
    }


class FakeAttr:
    def __init__(self, name, size, is_dir):
        self.filename, self.st_size = name, size
        self.st_mode = stat.S_IFDIR if is_dir else stat.S_IFREG


class FakeSFTP:
    def __init__(self, files):
        self.files = files
        self.dirs = set()
        for p in files:
            d = p.rsplit("/", 1)[0]
            while d:
                self.dirs.add(d)
                d = d.rsplit("/", 1)[0] if "/" in d else ""
        self.dirs.add("/")

    def stat(self, p):
        if p in self.files or p in self.dirs:
            return object()
        raise FileNotFoundError(p)

    def listdir_attr(self, d):
        d = d.rstrip("/")
        if d not in self.dirs:
            raise FileNotFoundError(d)
        out = {}
        for p, data in self.files.items():
            if p.startswith(d + "/"):
                first, _, rest = p[len(d) + 1:].partition("/")
                out[first] = FakeAttr(first, len(data), bool(rest))
        return list(out.values())

    @contextlib.contextmanager
    def open(self, p, mode="rb"):
        if p not in self.files:
            raise FileNotFoundError(p)
        yield io.BytesIO(self.files[p])

    def getfo(self, p, fl, callback=None, prefetch=True):
        fl.write(self.files[p])

    def normalize(self, p):
        return "/home/trainer"

    def close(self):
        pass


class FakeClient:
    def __init__(self, files):
        self.files = files

    def open_sftp(self):
        return FakeSFTP(self.files)


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
    w["owner_id"], w["owner"] = _register(client, "sowner")
    w["team"] = next(t for t in client.get("/api/teams", headers=w["owner"]).json() if t["is_personal"])["id"]
    _, w["ann"] = _register(client, "sann")
    assert client.post(f"/api/teams/{w['team']}/invite", json={"email": "sann@example.com", "role": "annotator"}, headers=w["owner"]).status_code == 200
    return w


@pytest.fixture
def server(monkeypatch):
    """In-memory training server + a recording object store."""
    state = {"files": remote_files(), "connects": [], "puts": {}}

    @contextlib.contextmanager
    def fake_connect(host, port, username, pem_key_text=None, password=None, timeout=15):
        state["connects"].append((host, port, username))
        if state.get("connect_error"):
            raise ssh_helper.SSHConnectionError(state["connect_error"])
        yield FakeClient(state["files"])

    monkeypatch.setattr(ssh_helper, "connect", fake_connect)
    monkeypatch.setattr(_inprocess.server, "put_object", lambda path, data, ct: state["puts"].setdefault(path, data) and {"path": path, "size": len(data)})
    return state


def new_project(client, world):
    return client.post("/api/projects", json={"name": "Ssh", "team_id": world["team"]}, headers=world["owner"]).json()["id"]


def body(**kw):
    return {"host": "train.example.com", "port": 22, "username": "trainer", "password": PASSWORD, "pem_key": PEM,
            "data_yaml_path": f"{ROOT}/data.yaml", "review_mode": "approved", **kw}


def start(client, world, pid, who="owner", **kw):
    return client.post(f"/api/projects/{pid}/imports/ssh", json=body(**kw), headers=world[who])


def batch(client, world, bid):
    return client.get(f"/api/imports/{bid}", headers=world["owner"]).json()


def assert_no_secrets_anywhere(db, extra=()):
    needles = [PASSWORD, "SECRETKEYBODY", *extra]
    for name in db.list_collection_names():
        dump = json.dumps(list(db[name].find()), default=str)
        for n in needles:
            assert n not in dump, f"secret found in collection {name}"


def test_ssh_import_stages_scans_commits_and_never_stores_credentials(client, db, world, server):
    pid = new_project(client, world)
    r = start(client, world, pid)
    assert r.status_code == 202, r.text
    assert PASSWORD not in r.text and "SECRETKEYBODY" not in r.text
    b = batch(client, world, r.json()["batch_id"])
    assert b["status"] == "ready", b
    assert b["origin"] == f"ssh:train.example.com:{ROOT}/data.yaml"  # host + path, no username
    rep = b["report"]
    assert rep["server_names"] == ["person", "cash", "id_visible"]
    assert rep["counts_by_split"] == {"train": 2, "valid": 1, "test": 1} and rep["importable"] == 4
    assert rep["images_without_labels"] == 1 and rep["corrupt"] == 0  # readme.txt ignored, orphan label ignored
    assert server["connects"] == [("train.example.com", 22, "trainer")]

    assert client.post(f"/api/imports/{b['id']}/commit", json={}, headers=world["owner"]).status_code == 202
    imgs = {i["filename"]: i for i in db.images.find({"project_id": pid})}
    assert sorted(imgs) == ["a_jpg.rf.1.jpg", "b.jpg", "c.png", "d.jpg"]
    assert {n: i["split"] for n, i in imgs.items()} == {"a_jpg.rf.1.jpg": "train", "b.jpg": "train", "c.png": "valid", "d.jpg": "test"}
    assert imgs["a_jpg.rf.1.jpg"]["review_status"] == "approved" and imgs["a_jpg.rf.1.jpg"]["annotations"][0]["label"] == "person"
    assert imgs["a_jpg.rf.1.jpg"]["provenance"]["origin"] == f"ssh:train.example.com:{ROOT}/data.yaml"

    proj = db.projects.find_one({"id": pid})
    assert proj["classes"] == ["person", "cash", "id_visible"] and proj["classes_locked"] is True
    link = proj["remote_dataset"]
    assert link["host"] == "train.example.com" and link["port"] == 22 and link["data_yaml_path"] == f"{ROOT}/data.yaml"
    assert "username" not in link and link["class_map"] == {"0": "person", "1": "cash", "2": "id_visible"}
    assert_no_secrets_anywhere(db)
    done = db.import_batches.find_one({"id": b["id"]})
    assert done["status"] == "completed" and not os.path.exists(os.path.dirname(done["zip_path"]))


def test_reimporting_the_same_server_dataset_creates_nothing_new(client, db, world, server):
    pid = new_project(client, world)
    b1 = batch(client, world, start(client, world, pid).json()["batch_id"])
    assert client.post(f"/api/imports/{b1['id']}/commit", json={}, headers=world["owner"]).status_code == 202
    ids = {i["id"] for i in db.images.find({"project_id": pid})}
    b2 = batch(client, world, start(client, world, pid).json()["batch_id"])
    assert b2["report"]["importable"] == 0 and b2["report"]["duplicates"]["already_imported"] == 4
    assert client.post(f"/api/imports/{b2['id']}/commit", json={}, headers=world["owner"]).status_code == 202
    assert {i["id"] for i in db.images.find({"project_id": pid})} == ids


def test_connection_errors_are_scrubbed_of_secrets_and_free_the_project(client, db, world, server):
    server["connect_error"] = f"Failed to connect to trainer@train.example.com:22: auth failed for {PASSWORD} / {KEY_LINE}"
    pid = new_project(client, world)
    r = start(client, world, pid)
    assert r.status_code == 202
    b = batch(client, world, r.json()["batch_id"])
    assert b["status"] == "failed" and b["active"] is False and "auth failed" in b["error"]
    assert PASSWORD not in b["error"] and "SECRETKEYBODY" not in b["error"] and "***" in b["error"]
    assert not os.path.exists(os.path.dirname(db.import_batches.find_one({"id": b["id"]})["zip_path"]))
    assert_no_secrets_anywhere(db)
    server.pop("connect_error")
    assert batch(client, world, start(client, world, pid).json()["batch_id"])["status"] == "ready"  # not blocked by the failure


@pytest.mark.parametrize("mutate,fragment", [
    (lambda f: f.pop(f"{ROOT}/data.yaml"), "Could not read"),
    (lambda f: f.update({f"{ROOT}/data.yaml": b"names: [a\n  bad: yaml: ["}), "not valid YAML"),
    (lambda f: f.update({f"{ROOT}/data.yaml": b"train: train/images\nnames:\n  0: a\n  2: c\n"}), "gaps"),
    (lambda f: f.update({f"{ROOT}/data.yaml": b"train: nowhere/images\nnames: [a]\n"}), "None of the train/val/test image folders"),
])
def test_dataset_problems_fail_with_a_clear_message(client, db, world, server, mutate, fragment):
    mutate(server["files"])
    pid = new_project(client, world)
    b = batch(client, world, start(client, world, pid).json()["batch_id"])
    assert b["status"] == "failed" and fragment in b["error"] and b["active"] is False


def test_missing_split_folder_oversize_files_and_val_alias_are_handled(client, db, world, server, monkeypatch):
    files = server["files"]
    for p in [p for p in files if "/test/" in p]:
        files.pop(p)  # the yaml lists test/images but it isn't on the server
    files[f"{ROOT}/data.yaml"] = YAML.replace("val:", "valid:").encode()  # some exporters spell the key `valid`
    monkeypatch.setattr(_inprocess.server, "put_object", lambda *a: {"path": a[0], "size": len(a[1])})
    import routers.imports as imp
    monkeypatch.setattr(imp, "MAX_IMAGE_BYTES", len(_png((0, 0, 255), (9, 7))) - 1)  # c.png is now "too large"
    pid = new_project(client, world)
    b = batch(client, world, start(client, world, pid).json()["batch_id"])
    assert b["status"] == "ready", b
    rep = b["report"]
    assert rep["counts_by_split"] == {"train": 2} and rep["too_large"] == 1
    assert any("test" in m for m in rep["missing_dirs"])


def test_not_enough_disk_space_is_reported_before_downloading(client, db, world, server, monkeypatch):
    class Usage:
        free = 10
    monkeypatch.setattr(ssh_source.shutil, "disk_usage", lambda p: Usage)
    pid = new_project(client, world)
    b = batch(client, world, start(client, world, pid).json()["batch_id"])
    assert b["status"] == "failed" and "Not enough free disk space" in b["error"]
    assert not os.path.exists(os.path.dirname(db.import_batches.find_one({"id": b["id"]})["zip_path"]))


def test_stager_stops_when_cancelled_and_can_be_checked_directly(db, world, tmp_path, monkeypatch):
    files = remote_files()
    for i in range(45):  # more than one progress/cancel checkpoint (every 20 files)
        files[f"{ROOT}/train/images/x{i}.png"] = _png((i, i, i))

    @contextlib.contextmanager
    def fake_connect(*a, **k):
        yield FakeClient(files)

    monkeypatch.setattr(ssh_helper, "connect", fake_connect)
    zip_path = str(tmp_path / "b" / "dataset.zip")
    db.import_batches.insert_one({"id": "stage-1", "project_id": "p", "zip_path": zip_path})
    creds = {"host": "h", "port": 22, "username": "u", "password": PASSWORD}
    done = ssh_source.stage_ssh_sync(db, "stage-1", creds, f"{ROOT}/data.yaml", lambda: True,
                                     max_image_bytes=10**7, max_total_bytes=10**9, max_files=1000)
    assert done is False
    done = ssh_source.stage_ssh_sync(db, "stage-1", creds, f"{ROOT}/data.yaml", lambda: False,
                                     max_image_bytes=10**7, max_total_bytes=10**9, max_files=1000)
    assert done is True and os.path.exists(zip_path)
    import zipfile
    with zipfile.ZipFile(zip_path) as zf:
        assert zf.getinfo("train/images/x0.png").compress_type == zipfile.ZIP_STORED
        assert "data.yaml" in zf.namelist() and "train/labels/b.txt" in zf.namelist()
    assert ssh_source.scrub(f"bad {PASSWORD} and {KEY_LINE}", {"password": PASSWORD, "pem_key": PEM}) == "bad *** and ***"


def test_inspect_endpoint_lists_datasets_scrubs_errors_and_checks_roles(client, db, world, monkeypatch):
    import routers.deployment as dep
    pid = new_project(client, world)
    req = {"host": "h", "username": "u", "password": PASSWORD, "remote_workdir": "/data"}
    canned = {"workdir": "/data", "found": True, "datasets": [{"data_yaml_path": f"{ROOT}/data.yaml", "names": ["a"], "splits": {}}],
              "models": [{"path": "/data/x.pt"}], "venv_ok": None}
    monkeypatch.setattr(dep, "_inspect_remote_sync", lambda body: canned)
    r = client.post(f"/api/projects/{pid}/imports/ssh/inspect", json=req, headers=world["owner"])
    assert r.status_code == 200 and r.json() == {"workdir": "/data", "found": True, "datasets": canned["datasets"]}  # no model list
    assert client.post(f"/api/projects/{pid}/imports/ssh/inspect", json=req, headers=world["ann"]).status_code == 403

    def boom(body):
        raise ssh_helper.SSHConnectionError(f"login failed with {PASSWORD}")
    monkeypatch.setattr(dep, "_inspect_remote_sync", boom)
    r = client.post(f"/api/projects/{pid}/imports/ssh/inspect", json=req, headers=world["owner"])
    assert r.status_code == 502 and PASSWORD not in r.text and "***" in r.text


def test_ssh_import_validation_roles_and_single_active_import(client, db, world, server):
    pid = new_project(client, world)
    assert start(client, world, pid, who="ann").status_code == 403
    assert start(client, world, pid, review_mode="bogus").status_code == 400
    assert start(client, world, pid, data_yaml_path="  ").status_code == 400
    assert client.post(f"/api/projects/{pid}/imports/ssh", json={"host": "h"}, headers=world["owner"]).status_code == 422
    first = start(client, world, pid)
    assert batch(client, world, first.json()["batch_id"])["status"] == "ready"
    assert start(client, world, pid).status_code == 409
    assert server["connects"] and len(server["connects"]) == 1  # nothing was fetched for rejected requests
