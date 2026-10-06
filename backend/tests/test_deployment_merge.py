"""Smart merge: preview (plan) and execution against an in-memory server. Every write the server sees is recorded."""
import contextlib
import hashlib
import io
import re
import shlex
import stat
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402
import yaml  # noqa: E402
import _inprocess  # noqa: E402
from _inprocess import fresh_db, get_client  # noqa: E402

import ssh_helper  # noqa: E402
from dataset_import import merge_remote  # noqa: E402

PASSWORD = "hunter2-very-secret"
ROOT = "/data/myG"
YAML = ("# dataset config\npath: /root/myG/old\n\ntrain: train/images\nval: valid/images\ntest: test/images\n\nnc: 3\n\n"
        "names:\n  0: person\n  1: cash\n  2: id_visible\n")


def png(color, size=(8, 8)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


A, B, C, D, E = png((255, 0, 0)), png((0, 255, 0)), png((0, 0, 255), (9, 7)), png((9, 9, 9)), png((50, 60, 70), (6, 6))


def remote_files():
    return {
        f"{ROOT}/data.yaml": YAML.encode(),
        f"{ROOT}/train/images/a.png": A, f"{ROOT}/train/labels/a.txt": b"0 0.5 0.5 0.2 0.4\n1 0.25 0.25 0.1 0.1\n",
        f"{ROOT}/train/images/b.png": B, f"{ROOT}/train/labels/b.txt": b"2 0.5 0.5 0.2 0.2\n",
        f"{ROOT}/valid/images/c.png": C, f"{ROOT}/valid/labels/c.txt": b"0 0.5 0.5 0.2 0.2\n",
        f"{ROOT}/test/images/d.png": D,
        f"{ROOT}/test/images/e.png": E, f"{ROOT}/test/labels/e.txt": b"1 0.5 0.5 0.3 0.3\n",
    }


class FakeServer:
    """Files + the SFTP/SSH surface the merge code uses; records writes and renames."""

    def __init__(self, files):
        self.files = files
        self.extra_dirs = set()
        self.writes = []
        self.tools = {"sha256sum": True, "python3": True}

    @property
    def dirs(self):
        out = set(self.extra_dirs) | {"/"}
        for p in self.files:
            d = p.rsplit("/", 1)[0]
            while d:
                out.add(d)
                d = d.rsplit("/", 1)[0] if "/" in d else ""
        return out

    # --- sftp ---
    def stat(self, p):
        if p in self.files or p in self.dirs:
            return object()
        raise FileNotFoundError(p)

    def mkdir(self, p):
        self.extra_dirs.add(p)

    def listdir_attr(self, d):
        d = d.rstrip("/")
        if d not in self.dirs:
            raise FileNotFoundError(d)
        out = {}
        for p, data in self.files.items():
            if p.startswith(d + "/"):
                first, _, rest = p[len(d) + 1:].partition("/")
                a = type("A", (), {})()
                a.filename, a.st_size, a.st_mode = first, len(data), (stat.S_IFDIR if rest else stat.S_IFREG)
                out[first] = a
        return list(out.values())

    @contextlib.contextmanager
    def open(self, p, mode="rb"):
        server = self
        if mode == "rb":
            if p not in self.files:
                raise FileNotFoundError(p)
            yield io.BytesIO(self.files[p])
        else:
            class W:
                def __init__(s):
                    s.buf = io.BytesIO()

                def write(s, data):
                    s.buf.write(data.encode() if isinstance(data, str) else data)  # paramiko accepts both
            w = W()
            yield w
            server.files[p] = w.buf.getvalue()
            server.writes.append(("write", p))

    def posix_rename(self, src, dst):
        self.files[dst] = self.files.pop(src)
        self.writes.append(("rename", dst))

    def close(self):
        pass

    def normalize(self, p):
        return "/home/trainer"

    # --- ssh ---
    def open_sftp(self):
        return self

    def exec(self, client, cmd, timeout=None):
        if "command -v sha256sum" in cmd:
            out = "".join(f"{t}\n" for t, ok in self.tools.items() if ok)
            return 0, out, ""
        m = re.match(r"find (\S+) -maxdepth 1 -type f -print0 \| xargs -0 -r sha256sum", cmd)
        if m:
            d = shlex.split(m.group(1))[0]
            return 0, "".join(f"{hashlib.sha256(v).hexdigest()}  {k}\n" for k, v in self.files.items() if k.rsplit("/", 1)[0] == d), ""
        if cmd.startswith("python3 -c"):
            d = shlex.split(cmd)[-1]
            return 0, "".join(f"{hashlib.sha256(v).hexdigest()}\t{k.rsplit('/', 1)[1]}\n" for k, v in sorted(self.files.items()) if k.rsplit("/", 1)[0] == d), ""
        if cmd.startswith("cat "):
            p = shlex.split(cmd)[1]
            return (0, self.files[p].decode(), "") if p in self.files else (1, "", "No such file")
        raise AssertionError(f"unexpected remote command: {cmd}")


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
    w["owner_id"], w["owner"] = _register(client, "mowner")
    w["team"] = next(t for t in client.get("/api/teams", headers=w["owner"]).json() if t["is_personal"])["id"]
    return w


@pytest.fixture
def env(monkeypatch):
    """A fake training server and an object store, wired into ssh_helper and the app."""
    server = FakeServer(remote_files())
    objects = {}

    @contextlib.contextmanager
    def fake_connect(host, port, username, pem_key_text=None, password=None, timeout=15):
        yield server

    monkeypatch.setattr(ssh_helper, "connect", fake_connect)
    monkeypatch.setattr(ssh_helper, "exec_command", server.exec)
    monkeypatch.setattr(_inprocess.server, "put_object", lambda path, data, ct: objects.setdefault(path, data) and {"path": path, "size": len(data)})
    monkeypatch.setattr(_inprocess.server, "get_object", lambda path: (objects[path], "image/png"))
    return type("Env", (), {"server": server, "objects": objects, "get": staticmethod(lambda path: (objects[path], "image/png"))})


def import_dataset(client, world, files=None, mode="approved", **form):
    """Import the server's dataset into a new project (as the real flow would), returns pid."""
    files = files or remote_files()
    pid = client.post("/api/projects", json={"name": "Merge", "team_id": world["team"]}, headers=world["owner"]).json()["id"]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for path, data in files.items():
            zf.writestr(path[len(ROOT) + 1:], data)
    r = client.post(f"/api/projects/{pid}/imports", files={"file": ("ds.zip", buf.getvalue(), "application/zip")},
                    data={"review_mode": mode, **form}, headers=world["owner"])
    bid = r.json()["batch_id"]
    assert client.get(f"/api/imports/{bid}", headers=world["owner"]).json()["status"] == "ready"
    assert client.post(f"/api/imports/{bid}/commit", json={}, headers=world["owner"]).status_code == 202
    return pid


def add_local(db, env, pid, short, data, label="person", filename=None, **extra):
    """A new approved image in the project. `short` is its name here; the id is unique per project, and is also the
    name the image gets on the server (`<id>.png`)."""
    img_id = f"{short}-{pid[:8]}"
    path = f"app/projects/{pid}/{img_id}.png"
    env.objects[path] = data
    doc = {"id": img_id, "project_id": pid, "filename": filename or f"{short}.png", "storage_path": path, "size": len(data),
           "content_hash": hashlib.sha256(data).hexdigest(), "review_status": "approved", "annotated": True, "assigned_to": None,
           "annotations": [{"type": "bbox", "label": label, "x": .1, "y": .1, "w": .2, "h": .3}],
           "created_at": datetime.now(timezone.utc).isoformat(), "source": "upload", **extra}
    db.images.insert_one(doc)
    return doc


def rname(pid, short):
    """Name an added image gets on the server."""
    return f"{short}-{pid[:8]}.png"


def by_name(db, pid):
    return {i["filename"]: i for i in db.images.find({"project_id": pid})}


def build(db, env, pid, **req):
    project = db.projects.find_one({"id": pid})
    plan_id = f"plan-{db.merge_plans.count_documents({})}"
    db.merge_plans.insert_one({"id": plan_id, "run_id": "run1", "project_id": pid, "status": "building"})
    fields = merge_remote.build_plan(env.server, db, plan_id, project=project, get_object_fn=env.get,
                                     req={"existing_data_yaml_path": f"{ROOT}/data.yaml", "train_pct": .7, "valid_pct": .2, "test_pct": .1, **req})
    db.merge_plans.update_one({"id": plan_id}, {"$set": {**fields, "status": "ready"}})
    return db.merge_plans.find_one({"id": plan_id})


def run(db, env, plan, pid, confirm=False, names=False, run_id="run1"):
    return merge_remote.execute_plan(env.server, db, plan, run_id=run_id, project_id=pid, confirm_label_updates=confirm,
                                     update_class_names=names, get_object_fn=env.get)


def items(db, plan, category):
    return {i["filename"]: i for i in db.merge_plan_items.find({"plan_id": plan["id"], "category": category})}


def edit_labels(db, pid, filename, boxes):
    img = by_name(db, pid)[filename]
    db.images.update_one({"id": img["id"]}, {"$set": {"annotations": [{"type": "bbox", **b} for b in boxes]}})


def snapshot(server):
    return dict(server.files)


def test_plan_classifies_every_image_and_writes_nothing(client, db, world, env):
    pid = import_dataset(client, world)
    add_local(db, env, pid, "n1", png((1, 2, 3)))
    # a: edited here; b: edited here AND changed on the server; c and e untouched; d has no labels (imported unreviewed)
    edit_labels(db, pid, "a.png", [{"label": "person", "x": .1, "y": .1, "w": .3, "h": .3}, {"label": "cash", "x": .25, "y": .25, "w": .1, "h": .1}])
    edit_labels(db, pid, "b.png", [{"label": "id_visible", "x": .1, "y": .1, "w": .2, "h": .2}])
    env.server.files[f"{ROOT}/train/labels/b.txt"] = b"2 0.9 0.9 0.1 0.1\n"
    before = snapshot(env.server)

    plan = build(db, env, pid)
    c = plan["counts"]
    assert plan["hash_mode"] == "sha256" and plan["warnings"] == []
    assert c["add"] == 1 and c["update_label"] == 1 and c["conflict"] == 1  # n1 / a / b
    assert c["unchanged"] == 2 and c["remote_only"] == 0  # c, e
    assert c["skipped_rejected"] == 1  # d: its bytes are on the server but it is not approved here, so it stays untouched there
    assert env.server.files == before and env.server.writes == []  # a preview never writes
    assert items(db, plan, "update_label")["a.png"]["split"] == "train"
    assert "label was changed since" in items(db, plan, "conflict")["b.png"]["reason"]
    add = items(db, plan, "add")["n1.png"]
    assert add["remote_name"] == rname(pid, "n1") and add["split"] in ("train", "valid", "test")
    assert plan["adds_by_split"][add["split"]] == 1 and plan["remote"]["split_counts"] == {"train": 2, "valid": 1, "test": 2}
    # an image only the server has (not imported here) is reported, never touched
    env.server.files[f"{ROOT}/valid/images/zzz.png"] = png((200, 100, 50))
    assert build(db, env, pid)["counts"]["remote_only"] == 1


def test_execute_adds_new_images_only_and_never_touches_existing_files(client, db, world, env):
    pid = import_dataset(client, world)
    add_local(db, env, pid, "n1", png((1, 2, 3)))
    add_local(db, env, pid, "n2", png((4, 5, 6)))
    edit_labels(db, pid, "a.png", [{"label": "person", "x": .1, "y": .1, "w": .3, "h": .3}])
    before = snapshot(env.server)
    plan = build(db, env, pid)
    stats = run(db, env, plan, pid)  # no confirmation: label updates are skipped

    assert stats["added"] == 2 and stats["skipped_label_updates"] == 1 and stats["labels_updated"] == 0
    changed = {p for p in env.server.files if env.server.files[p] != before.get(p)}
    assert all(p.rsplit("/", 1)[1].startswith((rname(pid, "n1")[:-4], rname(pid, "n2")[:-4])) for p in changed) and len(changed) == 4  # 2 images + 2 labels
    assert all(env.server.files[p] == v for p, v in before.items())  # every pre-existing file is byte-identical
    assert not any(".tv-tmp" in p for p in env.server.files)  # temp files were renamed away
    n1 = by_name(db, pid)["n1.png"]
    split = n1["split"]
    assert f"{ROOT}/{split}/images/{rname(pid, 'n1')}" in env.server.files
    label = env.server.files[f"{ROOT}/{split}/labels/{rname(pid, 'n1')[:-4]}.txt"].decode()
    assert label == "0 0.200000 0.250000 0.200000 0.300000\n"  # class 0 = person, centre format
    assert n1["last_push"]["run_id"] == "run1" and n1["last_push"]["remote_split"] == split
    assert db.images.find_one({"id": f"n2-{pid[:8]}"})["split"] in ("train", "valid", "test")


def test_confirmed_label_update_backs_up_the_old_label_and_a_rerun_finds_nothing_to_do(client, db, world, env):
    pid = import_dataset(client, world)
    edit_labels(db, pid, "a.png", [{"label": "person", "x": .1, "y": .1, "w": .3, "h": .3}])
    old_label = env.server.files[f"{ROOT}/train/labels/a.txt"]
    plan = build(db, env, pid)
    stats = run(db, env, plan, pid, confirm=True, run_id="runA")
    assert stats["labels_updated"] == 1 and stats["added"] == 0
    assert env.server.files[f"{ROOT}/train/labels/a.txt"] == b"0 0.250000 0.250000 0.300000 0.300000\n"
    # server-side box of the skipped/unowned kind would be preserved; here the cash box was removed by the edit
    assert env.server.files[f"{ROOT}/.thinkvision_backups/runA/train/labels/a.txt"] == old_label
    assert env.server.files[f"{ROOT}/train/images/a.png"] == A  # the image itself is never rewritten

    again = build(db, env, pid)
    assert again["counts"]["add"] == 0 and again["counts"]["update_label"] == 0 and again["counts"]["conflict"] == 0
    assert again["counts"]["unchanged"] == 4  # a is now in sync (baseline = last push); b, c, e as before


def test_unowned_label_lines_survive_a_label_update(client, db, world, env):
    files = remote_files()
    files[f"{ROOT}/train/labels/a.txt"] = b"0 0.5 0.5 0.2 0.4\n9 0.1 0.1 0.1 0.1\n"  # class 9 does not exist: not ours
    env.server.files.update(files)
    pid = import_dataset(client, world, files)
    img = by_name(db, pid)["a.png"]
    assert img["provenance"]["skipped_label_lines"] == ["9 0.1 0.1 0.1 0.1"]
    edit_labels(db, pid, "a.png", [{"label": "person", "x": .3, "y": .3, "w": .2, "h": .2}])
    plan = build(db, env, pid)
    assert plan["counts"]["update_label"] == 1
    run(db, env, plan, pid, confirm=True)
    assert env.server.files[f"{ROOT}/train/labels/a.txt"].decode() == "0 0.400000 0.400000 0.200000 0.200000\n9 0.1 0.1 0.1 0.1\n"


def test_a_file_that_appears_after_the_preview_is_never_overwritten(client, db, world, env):
    pid = import_dataset(client, world)
    add_local(db, env, pid, "n1", png((1, 2, 3)))
    plan = build(db, env, pid)
    split = items(db, plan, "add")["n1.png"]["split"]
    env.server.files[f"{ROOT}/{split}/images/{rname(pid, 'n1')}"] = b"somebody else's file"
    stats = run(db, env, plan, pid)
    assert stats["added"] == 0 and stats["skipped_exists"] == 1
    assert env.server.files[f"{ROOT}/{split}/images/{rname(pid, 'n1')}"] == b"somebody else's file"
    assert f"{ROOT}/{split}/labels/{rname(pid, 'n1')[:-4]}.txt" not in env.server.files  # nothing half-written either


def test_server_label_changed_after_the_preview_is_not_overwritten(client, db, world, env):
    pid = import_dataset(client, world)
    edit_labels(db, pid, "a.png", [{"label": "person", "x": .1, "y": .1, "w": .3, "h": .3}])
    plan = build(db, env, pid)
    env.server.files[f"{ROOT}/train/labels/a.txt"] = b"0 0.1 0.1 0.1 0.1\n"  # a colleague edited it meanwhile
    stats = run(db, env, plan, pid, confirm=True)
    assert stats["labels_updated"] == 0 and stats["skipped_conflict"] == 1
    assert env.server.files[f"{ROOT}/train/labels/a.txt"] == b"0 0.1 0.1 0.1 0.1\n"
    assert not any("/.thinkvision_backups/" in p for p in env.server.files)


def test_changed_server_yaml_or_expired_state_blocks_execution(client, db, world, env):
    pid = import_dataset(client, world)
    add_local(db, env, pid, "n1", png((1, 2, 3)))
    plan = build(db, env, pid)
    env.server.files[f"{ROOT}/data.yaml"] = (YAML + "# edited\n").encode()
    with pytest.raises(merge_remote.MergeError, match="changed since the preview"):
        run(db, env, plan, pid)
    assert not any(p.endswith(rname(pid, "n1")) for p in env.server.files)


def test_an_image_edited_after_the_preview_is_skipped_not_pushed(client, db, world, env):
    pid = import_dataset(client, world)
    add_local(db, env, pid, "n1", png((1, 2, 3)))
    plan = build(db, env, pid)
    db.images.update_one({"id": f"n1-{pid[:8]}"}, {"$set": {"annotations": [{"type": "bbox", "label": "cash", "x": .5, "y": .5, "w": .1, "h": .1}]}})
    stats = run(db, env, plan, pid)
    assert stats["added"] == 0 and stats["skipped_changed"] == 1
    db.images.update_one({"id": f"n1-{pid[:8]}"}, {"$set": {"review_status": "rejected"}})  # reopened
    assert run(db, env, plan, pid)["skipped_changed"] == 1


def test_new_images_get_a_stable_split_and_identical_content_never_straddles_splits(client, db, world, env):
    pid = import_dataset(client, world)
    data = png((77, 88, 99))
    for i in range(6):
        add_local(db, env, pid, f"dup{i}", data)  # six copies of the same bytes
    for i in range(8):
        add_local(db, env, pid, f"frame{i}", png((i, 9, 9)), group_key="video-1")
    plan = build(db, env, pid)
    adds = items(db, plan, "add")
    assert len({adds[f"dup{i}.png"]["split"] for i in range(6)}) == 1
    assert len({adds[f"frame{i}.png"]["split"] for i in range(8)}) == 1  # a video never spans splits
    again = build(db, env, pid)
    assert {k: v["split"] for k, v in items(db, again, "add").items()} == {k: v["split"] for k, v in adds.items()}  # stable between previews


def test_an_imported_images_original_split_is_never_reassigned_even_if_the_server_has_a_copy_elsewhere(client, db, world, env):
    pid = import_dataset(client, world)
    assert by_name(db, pid)["c.png"]["split"] == "valid"
    # the same bytes also sit in the server's TRAIN folder: leakage that already exists there
    env.server.files[f"{ROOT}/train/images/c_copy.png"] = C
    plan = build(db, env, pid)
    assert plan["counts"]["add"] == 0 and plan["counts"]["unchanged"] == 4
    assert any("more than one split" in w for w in plan["warnings"])  # reported, not "fixed"
    env.server.files[f"{ROOT}/valid/images/c.png"] = b"gone"  # and if the valid copy is not it, the train copy is found
    assert build(db, env, pid)["counts"]["add"] == 0


def test_weak_mode_matches_by_name_and_size_and_says_so(client, db, world, env):
    pid = import_dataset(client, world)
    add_local(db, env, pid, "n1", png((1, 2, 3)))
    env.server.tools = {"sha256sum": False, "python3": False}
    plan = build(db, env, pid)
    assert plan["hash_mode"] == "weak" and any("weaker check" in w for w in plan["warnings"])
    assert plan["counts"]["add"] == 1 and plan["counts"]["unchanged"] == 4  # imported images found by original name + size
    # a same-named file of a different size is NOT the same image
    env.server.files[f"{ROOT}/valid/images/c.png"] = C + b"x"
    assert build(db, env, pid)["counts"]["add"] == 2


def test_a_fingerprinting_failure_stops_the_preview_instead_of_re_adding_everything(client, db, world, env, monkeypatch):
    pid = import_dataset(client, world)
    add_local(db, env, pid, "n1", png((1, 2, 3)))
    real = env.server.exec
    monkeypatch.setattr(ssh_helper, "exec_command", lambda c, cmd, timeout=None: (0, "", "") if "sha256sum" in cmd and "command -v" not in cmd else real(c, cmd))
    with pytest.raises(merge_remote.MergeError, match="Could not fingerprint"):
        build(db, env, pid)


def test_python_fallback_when_sha256sum_is_missing(client, db, world, env):
    pid = import_dataset(client, world)
    env.server.tools = {"sha256sum": False, "python3": True}
    plan = build(db, env, pid)
    assert plan["hash_mode"] == "sha256" and plan["counts"]["unchanged"] == 4


def test_new_classes_are_only_appended_to_data_yaml_with_a_backup_and_confirmation(client, db, world, env):
    pid = import_dataset(client, world)
    db.projects.update_one({"id": pid}, {"$push": {"classes": "mop"}})
    add_local(db, env, pid, "n1", png((1, 2, 3)), label="mop")
    add_local(db, env, pid, "n2", png((4, 5, 6)), label="person")
    plan = build(db, env, pid)
    assert plan["yaml_change"]["append"] == [{"idx": 3, "name": "mop"}] and plan["yaml_change"]["nc_after"] == 4
    assert plan["class_status"]["missing_in_remote"] == ["mop"] and plan["yaml_change"]["reformatted"] is False
    yaml_before = env.server.files[f"{ROOT}/data.yaml"]

    stats = run(db, env, plan, pid)  # no checkbox: the mop image is skipped, the person image is pushed
    assert stats["added"] == 1 and stats["skipped_class_append"] == 1 and env.server.files[f"{ROOT}/data.yaml"] == yaml_before

    plan = build(db, env, pid)
    stats = run(db, env, plan, pid, names=True)
    new_yaml = env.server.files[f"{ROOT}/data.yaml"].decode()
    assert stats["added"] == 1 and stats["yaml_backup"].startswith(f"{ROOT}/data.yaml.bak.")
    assert env.server.files[stats["yaml_backup"]] == yaml_before  # the backup is the exact original
    assert new_yaml.startswith("# dataset config\n") and "  3: mop\n" in new_yaml and "nc: 4" in new_yaml
    cfg = yaml.safe_load(new_yaml)
    assert [cfg["names"][i] for i in range(4)] == ["person", "cash", "id_visible", "mop"]  # existing order untouched
    split = by_name(db, pid)["n1.png"]["split"]
    assert env.server.files[f"{ROOT}/{split}/labels/{rname(pid, 'n1')[:-4]}.txt"].decode().startswith("3 ")  # mop = index 3


def test_renamed_project_class_still_writes_the_servers_index_without_touching_names(client, db, world, env):
    pid = import_dataset(client, world)
    r = client.patch(f"/api/projects/{pid}/classes/person", json={"new_name": "human"}, headers=world["owner"])
    assert r.status_code == 200
    assert db.projects.find_one({"id": pid})["remote_dataset"]["class_map"]["0"] == "human"
    add_local(db, env, pid, "n1", png((1, 2, 3)), label="human")
    plan = build(db, env, pid)
    assert plan["yaml_change"] is None and plan["class_status"]["name_drift"] == [{"idx": 0, "server_name": "person", "project_label": "human"}]
    run(db, env, plan, pid)
    split = by_name(db, pid)["n1.png"]["split"]
    assert env.server.files[f"{ROOT}/{split}/labels/{rname(pid, 'n1')[:-4]}.txt"].decode().startswith("0 ")
    assert yaml.safe_load(env.server.files[f"{ROOT}/data.yaml"].decode())["names"][0] == "person"


def test_a_server_whose_classes_changed_since_the_import_aborts_the_preview(client, db, world, env):
    pid = import_dataset(client, world)
    env.server.files[f"{ROOT}/data.yaml"] = YAML.replace("0: person", "0: human").encode()
    with pytest.raises(merge_remote.MergeError, match="no longer matches"):
        build(db, env, pid)


def test_images_without_a_usable_box_are_not_pushed(client, db, world, env):
    pid = import_dataset(client, world)
    add_local(db, env, pid, "n1", png((1, 2, 3)))
    db.images.update_one({"id": f"n1-{pid[:8]}"}, {"$set": {"annotations": [{"type": "bbox", "label": "person", "x": 0, "y": 0, "w": 0, "h": 0}]}})
    plan = build(db, env, pid)
    assert plan["counts"]["skipped_empty"] == 1 and plan["counts"]["add"] == 0


def test_local_images_without_a_fingerprint_are_hashed_on_the_fly(client, db, world, env):
    pid = import_dataset(client, world)
    doc = add_local(db, env, pid, "legacy", png((11, 12, 13)))
    db.images.update_one({"id": doc["id"]}, {"$unset": {"content_hash": ""}})
    plan = build(db, env, pid)
    assert plan["counts"]["add"] == 1
    assert db.images.find_one({"id": doc["id"]})["content_hash"] == hashlib.sha256(env.objects[doc["storage_path"]]).hexdigest()


# ---------------------------------------------------------------- through the real routes

CREDS = {"host": "train.example.com", "port": 22, "username": "trainer", "password": PASSWORD}


def new_run(client, world, pid):
    r = client.post(f"/api/projects/{pid}/pipeline/runs", headers=world["owner"])
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def make_plan(client, world, rid, **extra):
    r = client.post(f"/api/pipeline/runs/{rid}/merge_plan", json={**CREDS, "existing_data_yaml_path": f"{ROOT}/data.yaml", **extra}, headers=world["owner"])
    assert r.status_code == 202, r.text
    return client.get(f"/api/pipeline/runs/{rid}/merge_plan?plan_id={r.json()['plan_id']}", headers=world["owner"]).json()


def stage(client, world, rid, **extra):
    return client.post(f"/api/pipeline/runs/{rid}/stage/uploading_data", headers=world["owner"], json={
        **CREDS, "remote_workdir": "/data", "dataset_mode": "merge", "existing_data_yaml_path": f"{ROOT}/data.yaml", **extra})


def no_secrets(db):
    for name in db.list_collection_names():
        assert PASSWORD not in str(list(db[name].find())), f"password stored in {name}"


def test_preview_then_execute_through_the_api(client, db, world, env):
    pid = import_dataset(client, world)
    add_local(db, env, pid, "n1", png((1, 2, 3)))
    edit_labels(db, pid, "a.png", [{"label": "person", "x": .1, "y": .1, "w": .3, "h": .3}])
    rid = new_run(client, world, pid)
    before = snapshot(env.server)

    plan = make_plan(client, world, rid)
    assert plan["status"] == "ready" and plan["counts"]["add"] == 1 and plan["counts"]["update_label"] == 1
    assert env.server.files == before  # previewing writes nothing
    assert db.pipeline_runs.find_one({"id": rid})["busy"] is False
    cats = {i["category"] for i in client.get(f"/api/pipeline/runs/{rid}/merge_plan/items?plan_id={plan['id']}", headers=world["owner"]).json()}
    assert cats == {"add", "update_label"}
    only = client.get(f"/api/pipeline/runs/{rid}/merge_plan/items?plan_id={plan['id']}&category=add", headers=world["owner"]).json()
    assert [i["filename"] for i in only] == ["n1.png"]

    r = stage(client, world, rid, plan_id=plan["id"], confirm_label_updates=True)
    assert r.status_code == 200, r.text
    run_doc = db.pipeline_runs.find_one({"id": rid})
    assert run_doc["status"] != "failed" and run_doc["busy"] is False, run_doc.get("error")
    summary = run_doc["dataset_export"]["merge_summary"]
    assert summary["added"] == 1 and summary["labels_updated"] == 1
    assert run_doc["dataset_export"]["dataset_mode"] == "merge" and run_doc["dataset_export"]["test_count"] >= 2
    assert env.server.files[f"{ROOT}/train/labels/a.txt"] == b"0 0.250000 0.250000 0.300000 0.300000\n"
    assert db.merge_plans.find_one({"id": plan["id"]})["status"] == "executed"
    no_secrets(db)

    again = stage(client, world, rid, plan_id=plan["id"])
    assert again.status_code == 409 and "executed" in again.json()["detail"]  # a plan runs once


def test_merge_without_a_plan_only_adds_new_images(client, db, world, env):
    pid = import_dataset(client, world)
    add_local(db, env, pid, "n1", png((1, 2, 3)))
    edit_labels(db, pid, "a.png", [{"label": "person", "x": .1, "y": .1, "w": .3, "h": .3}])
    old = env.server.files[f"{ROOT}/train/labels/a.txt"]
    rid = new_run(client, world, pid)
    assert stage(client, world, rid).status_code == 200
    summary = db.pipeline_runs.find_one({"id": rid})["dataset_export"]["merge_summary"]
    assert summary["added"] == 1 and summary["labels_updated"] == 0 and summary["skipped_label_updates"] == 1
    assert env.server.files[f"{ROOT}/train/labels/a.txt"] == old  # edits are never pushed without confirmation


def test_unknown_expired_and_failed_plans_are_refused_and_the_pipeline_is_released(client, db, world, env):
    pid = import_dataset(client, world)
    rid = new_run(client, world, pid)
    assert stage(client, world, rid, plan_id="nope").status_code == 400
    plan = make_plan(client, world, rid)
    db.merge_plans.update_one({"id": plan["id"]}, {"$set": {"expires_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()}})
    assert client.get(f"/api/pipeline/runs/{rid}/merge_plan", headers=world["owner"]).json()["status"] == "expired"
    r = stage(client, world, rid, plan_id=plan["id"])
    assert r.status_code == 409 and "expired" in r.json()["detail"]
    assert db.pipeline_runs.find_one({"id": rid})["busy"] is False

    env.server.files[f"{ROOT}/data.yaml"] = YAML.replace("0: person", "0: human").encode()
    failed = make_plan(client, world, rid)
    assert failed["status"] == "failed" and "no longer matches" in failed["error"]
    assert db.pipeline_runs.find_one({"id": rid})["busy"] is False and db.merge_plan_items.count_documents({"plan_id": failed["id"]}) == 0
    no_secrets(db)


def test_connection_failures_are_scrubbed_and_release_the_pipeline(client, db, world, env, monkeypatch):
    pid = import_dataset(client, world)
    rid = new_run(client, world, pid)

    @contextlib.contextmanager
    def broken(*a, **k):
        raise ssh_helper.SSHConnectionError(f"login failed for trainer: {PASSWORD}")
        yield

    monkeypatch.setattr(ssh_helper, "connect", broken)
    plan = make_plan(client, world, rid)
    assert plan["status"] == "failed" and PASSWORD not in plan["error"] and "***" in plan["error"]
    assert db.pipeline_runs.find_one({"id": rid})["busy"] is False
    no_secrets(db)


def test_only_one_run_per_pipeline_can_be_busy_and_previews_respect_it(client, db, world, env):
    pid = import_dataset(client, world)
    r1, r2 = new_run(client, world, pid), new_run(client, world, pid)
    db.pipeline_runs.update_one({"id": r1}, {"$set": {"busy": True}})
    from pymongo.errors import DuplicateKeyError
    with pytest.raises(DuplicateKeyError):
        db.pipeline_runs.update_one({"id": r2}, {"$set": {"busy": True}})  # the database refuses a second busy run
    r = client.post(f"/api/pipeline/runs/{r2}/merge_plan", json={**CREDS, "existing_data_yaml_path": f"{ROOT}/data.yaml"}, headers=world["owner"])
    assert r.status_code == 409 and "busy" in r.json()["detail"].lower()
    assert stage(client, world, r2).status_code == 409
    db.pipeline_runs.update_one({"id": r1}, {"$set": {"busy": False}})
    assert make_plan(client, world, r2)["status"] == "ready"


def test_merge_plan_validation_and_permissions(client, db, world, env):
    pid = import_dataset(client, world)
    rid = new_run(client, world, pid)
    _, ann = _register(client, "mann")
    assert client.post(f"/api/teams/{world['team']}/invite", json={"email": "mann@example.com", "role": "annotator"}, headers=world["owner"]).status_code == 200
    body = {**CREDS, "existing_data_yaml_path": f"{ROOT}/data.yaml"}
    assert client.post(f"/api/pipeline/runs/{rid}/merge_plan", json=body, headers=ann).status_code == 403
    assert client.get(f"/api/pipeline/runs/{rid}/merge_plan", headers=ann).status_code == 403
    assert client.post(f"/api/pipeline/runs/{rid}/merge_plan", json={**body, "train_pct": 0.9}, headers=world["owner"]).status_code == 400
    assert client.post(f"/api/pipeline/runs/{rid}/merge_plan", json={"host": "h"}, headers=world["owner"]).status_code == 400
    assert client.get(f"/api/pipeline/runs/{rid}/merge_plan", headers=world["owner"]).status_code == 404  # none yet
    assert client.post("/api/pipeline/runs/nope/merge_plan", json=body, headers=world["owner"]).status_code == 404


# ---------------------------------------------------------------- segmentation and oriented-box datasets

import math  # noqa: E402

import yolo_codec as yc  # noqa: E402

WIDE = (100, 50)  # not square: rotation and axes only come out right in pixel space


def seg_files():
    return {f"{ROOT}/data.yaml": YAML.encode(),
            f"{ROOT}/train/images/p1.png": png((10, 0, 0), WIDE), f"{ROOT}/train/labels/p1.txt": b"0 0.1 0.1 0.5 0.1 0.3 0.6\n",
            f"{ROOT}/valid/images/p2.png": png((0, 10, 0), WIDE), f"{ROOT}/valid/labels/p2.txt": b"1 0.2 0.2 0.8 0.2 0.8 0.7 0.5 0.9 0.2 0.7\n"}


def obb_line(cls, x, y, w, h, rot, order=(0, 1, 2, 3)):
    c = yc.obb_to_corners(x, y, w, h, rot, WIDE[0], WIDE[1])
    return f"{cls} " + " ".join(f"{c[i][0]:.6f} {c[i][1]:.6f}" for i in order)


def obb_files():
    return {f"{ROOT}/data.yaml": YAML.encode(),
            f"{ROOT}/train/images/q1.png": png((10, 0, 0), WIDE), f"{ROOT}/train/labels/q1.txt": (obb_line(0, .2, .3, .4, .2, .5) + "\n").encode(),
            f"{ROOT}/valid/images/q2.png": png((0, 10, 0), WIDE), f"{ROOT}/valid/labels/q2.txt": (obb_line(1, .5, .1, .3, .4, -.3, order=(1, 2, 3, 0)) + "\n").encode()}


def use_remote(env, files):
    env.server.files.clear()
    env.server.files.update(files)


def poly_ann(label, pts):
    return {"type": "polygon", "label": label, "points": pts}


def test_segmentation_dataset_gets_polygon_lines_and_detects_edits_without_false_conflicts(client, db, world, env):
    use_remote(env, seg_files())
    pid = import_dataset(client, world, seg_files())
    plan = build(db, env, pid)
    assert plan["label_format"] == "seg" and plan["counts"]["unchanged"] == 2 and plan["counts"]["add"] == 0

    # a new polygon image and a new plain-box image: both are written as polygons (a box is a 4-point polygon)
    p = add_local(db, env, pid, "poly", png((1, 2, 3), WIDE))
    db.images.update_one({"id": p["id"]}, {"$set": {"annotations": [poly_ann("person", [[.1, .1], [.6, .1], [.4, .7]])]}})
    add_local(db, env, pid, "box", png((4, 5, 6), WIDE))
    # an imported polygon edited here (one vertex moved)
    db.images.update_one({"id": by_name(db, pid)["p1.png"]["id"]}, {"$set": {"annotations": [poly_ann("person", [[.1, .1], [.5, .1], [.35, .6]])]}})
    plan = build(db, env, pid)
    assert plan["counts"]["add"] == 2 and plan["counts"]["update_label"] == 1
    stats = run(db, env, plan, pid, confirm=True)
    assert stats["added"] == 2 and stats["labels_updated"] == 1
    assert env.server.files[f"{ROOT}/train/labels/p1.txt"].decode() == "0 0.100000 0.100000 0.500000 0.100000 0.350000 0.600000\n"
    split_poly, split_box = by_name(db, pid)["poly.png"]["split"], by_name(db, pid)["box.png"]["split"]
    assert env.server.files[f"{ROOT}/{split_poly}/labels/{rname(pid, 'poly')[:-4]}.txt"].decode() == "0 0.100000 0.100000 0.600000 0.100000 0.400000 0.700000\n"
    assert env.server.files[f"{ROOT}/{split_box}/labels/{rname(pid, 'box')[:-4]}.txt"].decode() == (
        "0 0.100000 0.100000 0.300000 0.100000 0.300000 0.400000 0.100000 0.400000\n")
    again = build(db, env, pid)  # our own writes must not look like edits or conflicts
    assert again["counts"]["add"] == 0 and again["counts"]["update_label"] == 0 and again["counts"]["conflict"] == 0 and again["counts"]["unchanged"] == 4
    env.server.files[f"{ROOT}/valid/labels/p2.txt"] = b"1 0.2 0.2 0.8 0.2 0.8 0.7 0.5 0.9 0.25 0.7\n"  # a colleague moves a vertex
    db.images.update_one({"id": by_name(db, pid)["p2.png"]["id"]}, {"$set": {"annotations": [poly_ann("cash", [[.3, .3], [.8, .2], [.8, .7]])]}})
    assert build(db, env, pid)["counts"]["conflict"] == 1


def test_oriented_box_dataset_is_written_in_pixel_space_and_round_trips_without_conflicts(client, db, world, env):
    use_remote(env, obb_files())
    pid = import_dataset(client, world, obb_files(), label_format="obb")
    plan = build(db, env, pid)
    assert plan["label_format"] == "obb" and plan["counts"]["unchanged"] == 2  # incl. q2, listed from another corner

    n = add_local(db, env, pid, "tilt", png((7, 7, 7), WIDE))
    db.images.update_one({"id": n["id"]}, {"$set": {"annotations": [{"type": "bbox", "label": "cash", "x": .3, "y": .2, "w": .4, "h": .2, "rotation": 0.7}]}})
    edited = by_name(db, pid)["q1.png"]
    db.images.update_one({"id": edited["id"]}, {"$set": {"annotations": [{"type": "bbox", "label": "person", "x": .3, "y": .3, "w": .3, "h": .15, "rotation": 1.1}]}})
    plan = build(db, env, pid)
    assert plan["counts"]["add"] == 1 and plan["counts"]["update_label"] == 1
    run(db, env, plan, pid, confirm=True)
    split = by_name(db, pid)["tilt.png"]["split"]
    written = env.server.files[f"{ROOT}/{split}/labels/{rname(pid, 'tilt')[:-4]}.txt"].decode().strip()
    assert written == obb_line(1, .3, .2, .4, .2, 0.7)  # class 1 = cash; corners computed on the 100x50 pixel grid
    assert len(written.split()) == 9
    q1 = env.server.files[f"{ROOT}/train/labels/q1.txt"].decode().strip()
    assert q1 == obb_line(0, .3, .3, .3, .15, 1.1)  # (kept inside the image: corners are clamped to it, as the exporter does)
    parsed = yc.parse_label_text(q1, "obb", WIDE).boxes[0]
    assert parsed["rotation"] == pytest.approx(1.1, abs=1e-4)
    again = build(db, env, pid)
    assert again["counts"]["add"] == 0 and again["counts"]["update_label"] == 0 and again["counts"]["conflict"] == 0 and again["counts"]["unchanged"] == 3


def test_a_nine_value_server_dataset_with_no_recorded_format_asks_which_one(client, db, world, env):
    use_remote(env, obb_files())
    pid = client.post("/api/projects", json={"name": "Plain", "team_id": world["team"]}, headers=world["owner"]).json()["id"]
    db.projects.update_one({"id": pid}, {"$set": {"classes": ["person", "cash", "id_visible"]}})  # not imported: nothing records the format
    add_local(db, env, pid, "n1", png((1, 2, 3), WIDE))
    with pytest.raises(merge_remote.MergeError, match="segmentation .* or OBB"):
        build(db, env, pid)
    plan = build(db, env, pid, label_format="obb")
    assert plan["label_format"] == "obb" and plan["counts"]["add"] == 1
    # a dataset whose polygons cannot be oriented boxes is recognised without being told
    use_remote(env, seg_files())
    assert build(db, env, pid)["label_format"] == "seg"
    use_remote(env, remote_files())
    assert build(db, env, pid)["label_format"] == "detect"


def test_a_label_format_given_in_the_request_overrides_what_was_recorded(client, db, world, env):
    use_remote(env, seg_files())
    pid = import_dataset(client, world, seg_files())
    assert build(db, env, pid)["label_format"] == "seg"
    assert build(db, env, pid, label_format="detect")["label_format"] == "detect"
