"""Build a merge plan against a server dataset, and execute it. Runs in worker threads with a sync pymongo handle
and an already-open SSH client (credentials are handled by the caller and never reach this module's storage).

The plan is a diff, shown to the user before anything is written:
  add            image not on the server -> upload it (new split chosen deterministically)
  update_label   image on the server, label edited here, server copy untouched since -> replace it (old one backed up)
  conflict       image on the server, label differs and cannot be attributed to us -> left alone
  unchanged      nothing to do (counted, not listed)
  name_collision a server file already has the name we would use -> left alone
  skipped_empty  approved but no usable box -> not pushed
Nothing on the server is ever deleted, and no file is overwritten except an `update_label` the user confirmed.
"""
import hashlib
import posixpath
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional

import yaml

import ssh_helper
import yolo_codec
from dataset_import import class_map as class_map_mod
from dataset_import import merge_plan as mp
from dataset_import import scan as scan_mod
from dataset_import import ssh_source

PLAN_TTL = timedelta(hours=24)
MAX_YAML_BYTES = scan_mod.MAX_YAML_BYTES
APPROVED_Q = {"review_status": "approved", "annotated": True}
WEAK_WARNING = ("sha256sum and python3 were not found on the server, so images are matched by file name and size. "
                "That is a weaker check: a different image with the same name and size would count as already present, "
                "and an image already there under another name would be added again.")


class MergeError(Exception):
    """A problem the user can act on (shown as the plan/run error)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ext(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else "jpg"


def _baselines(img: dict, yaml_path: str):
    """(local baseline, remote baseline): our label as of the last sync, and what the server's file parsed to then."""
    lp = img.get("last_push") or {}
    if lp.get("label_hash") and lp.get("data_yaml_path") == yaml_path:
        return lp["label_hash"], lp.get("remote_hash") or lp["label_hash"]
    base = (img.get("provenance") or {}).get("label_hash_at_import")
    return base, base


def _dims(img: dict):
    return (img["width"], img["height"]) if img.get("width") and img.get("height") else None


def _ensure_dims(sync_db, img: dict, get_object_fn) -> None:
    """Oriented boxes can only be converted with the image size; legacy images without one are measured once."""
    if _dims(img):
        return
    import io
    from PIL import Image
    data, _ct = get_object_fn(img["storage_path"])
    with Image.open(io.BytesIO(data)) as im:
        img["width"], img["height"] = im.size
    sync_db.images.update_one({"id": img["id"], "width": {"$exists": False}}, {"$set": {"width": img["width"], "height": img["height"]}})


def _dataset_format(sftp, layout: dict, requested: str, link: Optional[dict]) -> str:
    """The label format of the server's dataset: stated by the user, else recorded when it was imported, else read
    from a sample of its label files. Ambiguous (all 9-value lines) must be stated."""
    if requested in ("detect", "seg", "obb"):
        return requested
    if link and link.get("label_format") in ("detect", "seg", "obb"):
        return link["label_format"]
    counts, looked = set(), 0
    for split in mp.SPLIT_ORDER:
        info = layout.get(split)
        for stem, lbl_name in (info["labels"].items() if info else []):
            raw = ssh_helper.read_remote_bytes(sftp, f"{info['lbl_dir']}/{lbl_name}")
            for line in (raw or b"").decode("utf-8", errors="replace").splitlines():
                s = line.strip()
                if s and not s.startswith("#"):
                    counts.add(len(s.split()))
            looked += 1
            if looked >= 80:
                break
        if looked >= 80:
            break
    fmt, problem = yolo_codec.resolve_format(counts, "auto")
    if problem:
        raise MergeError(problem.replace("import again", "preview again"))
    return fmt


def _read_yaml(sftp, yaml_path: str):
    raw = ssh_helper.read_remote_bytes(sftp, yaml_path, MAX_YAML_BYTES)
    if raw is None:
        raise MergeError(f"Could not read {yaml_path} on the server")
    text = raw.decode("utf-8", errors="replace")
    try:
        cfg = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise MergeError(f"data.yaml is not valid YAML: {e}")
    if not isinstance(cfg, dict):
        raise MergeError("data.yaml is not a mapping")
    try:
        names = class_map_mod.server_names_from_yaml(cfg.get("names"))
    except ValueError as e:
        raise MergeError(str(e))
    return text, cfg, names


def build_plan(client, sync_db, plan_id: str, *, project: dict, req: dict, get_object_fn: Callable,
               progress: Optional[Callable[[str, int, int], None]] = None) -> dict:
    """Diff the project's approved images against the server dataset described by req["existing_data_yaml_path"].
    Writes the plan items and returns the fields to store on the plan document. Reads from the server, writes nothing there."""
    report = progress or (lambda *_a: None)
    pid = project["id"]
    sftp = client.open_sftp()
    try:
        yaml_path = ssh_helper.resolve_remote_path(client, req["existing_data_yaml_path"])
        text, cfg, names = _read_yaml(sftp, yaml_path)
        yaml_dir = posixpath.dirname(yaml_path)
        try:
            layout = ssh_source.plan_remote_layout(sftp, cfg, yaml_dir, max_image_bytes=10 ** 15, allow_missing=True)
        except ssh_source.StagingError as e:
            raise MergeError(str(e))
        splits = layout["splits"]

        maps = mp.class_maps(names, project.get("classes", []), project.get("remote_dataset"))
        if maps["error"]:
            raise MergeError(maps["error"])
        idx_to_label, label_to_idx = maps["idx_to_label"], dict(maps["label_to_idx"])
        fmt = _dataset_format(sftp, splits, req.get("label_format", "auto"), project.get("remote_dataset"))

        # ---- fingerprint the server's images (on the server: nothing is downloaded) ----
        tools = ssh_helper.probe_hash_tools(client)
        tool = "sha256sum" if tools["sha256sum"] else "python3" if tools["python3"] else None
        hash_mode = "sha256" if tool else "weak"
        remote_hash: Dict[tuple, str] = {}
        by_key: Dict[tuple, tuple] = {}
        by_hash: Dict[str, List[tuple]] = {}
        total_remote = sum(len(i["images"]) for i in splits.values())
        done = 0
        report("scanning server", 0, total_remote)
        for split in mp.SPLIT_ORDER:
            info = splits.get(split)
            if not info:
                continue
            hashes = ssh_helper.remote_sha256(client, info["img_dir"], tool) if tool and info["images"] else {}
            if tool and info["images"] and not any(n in hashes for n, _s in info["images"]):
                # Fingerprinting yielded nothing for a folder that has images. Carrying on would make every server
                # image look unknown and get added again, so stop here instead.
                raise MergeError(f"Could not fingerprint the images in {info['img_dir']} on the server ({tool} returned nothing). "
                                 "Check permissions on that folder.")
            for name, size in info["images"]:
                by_key[(name, size)] = (split, name)
                h = hashes.get(name)
                if h:
                    remote_hash[(split, name)] = h
                    by_hash.setdefault(h, []).append((split, name))
            done += len(info["images"])
            report("scanning server", done, total_remote)
        warnings: List[str] = []
        if hash_mode == "weak":
            warnings.append(WEAK_WARNING)
        dup_hashes = [h for h, hits in by_hash.items() if len({s for s, _n in hits}) > 1]
        if dup_hashes:
            warnings.append(f"{len(dup_hashes)} image(s) on the server exist in more than one split (leakage already present there); "
                            "they are left as they are.")
        if layout["missing_dirs"]:
            warnings.append("Folders listed in data.yaml but missing on the server: " + ", ".join(layout["missing_dirs"]))

        # ---- local images ----
        local = list(sync_db.images.find(
            {"project_id": pid, **APPROVED_Q},
            {"_id": 0, "id": 1, "filename": 1, "content_hash": 1, "size": 1, "storage_path": 1, "split": 1,
             "annotations": 1, "provenance": 1, "last_push": 1, "group_key": 1}).sort("id", 1))
        if hash_mode == "sha256":
            missing = [i for i in local if not i.get("content_hash")]
            for n, img in enumerate(missing, 1):
                report("fingerprinting local images", n, len(missing))
                data, _ct = get_object_fn(img["storage_path"])
                img["content_hash"] = hashlib.sha256(data).hexdigest()
                sync_db.images.update_one({"id": img["id"], "content_hash": {"$exists": False}}, {"$set": {"content_hash": img["content_hash"]}})

        # classes the server doesn't know yet: appended after its last index
        used = {a.get("label") for img in local for a in img.get("annotations", [])}
        slots = mp.new_class_slots(sorted(u for u in used if u), project.get("classes", []), label_to_idx, len(names))
        slot_names = {s["name"] for s in slots}
        for s_ in slots:
            label_to_idx[s_["name"]] = s_["idx"]
        if fmt == "obb":
            for img in local:
                _ensure_dims(sync_db, img, get_object_fn)
        shares = mp.split_shares(req.get("train_pct", 0.7), req.get("valid_pct", 0.2), req.get("test_pct", 0.1), list(splits))

        counts = {"add": 0, "update_label": 0, "unchanged": 0, "conflict": 0, "name_collision": 0,
                  "skipped_empty": 0, "needs_class_append": 0, "remote_only": 0, "skipped_rejected": 0, "lossy_labels": 0}
        adds_by_split = {s: 0 for s in splits}
        matched = set()
        all_remote_names = {n for info in splits.values() for n, _s in info["images"]}
        items: List[dict] = []
        report("comparing", 0, len(local))
        for n, img in enumerate(local, 1):
            cur = mp.current_label(img.get("annotations", []), label_to_idx, fmt, _dims(img))
            if cur["lossy"]:
                counts["lossy_labels"] += 1
            base_item = {"plan_id": plan_id, "image_id": img["id"], "filename": img.get("filename"), "new_hash": cur["hash"],
                         "needs_class_append": bool(slot_names & {a.get("label") for a in img.get("annotations", [])}),
                         "lossy": cur["lossy"]}
            if not cur["items"]:
                counts["skipped_empty"] += 1
                items.append({**base_item, "category": "skipped_empty", "reason": "no usable box to write"})
                continue
            hit = _find_match(img, hash_mode, remote_hash, by_hash, by_key, yaml_path)
            if hit:
                split, name = hit
                matched.add((split, name))
                baseline, remote_baseline = _baselines(img, yaml_path)
                expected_others = (img.get("provenance") or {}).get("skipped_label_lines") or []
                if not mp.needs_remote_label(baseline, cur["hash"]):
                    counts["unchanged"] += 1
                else:
                    stem = name.rsplit(".", 1)[0]
                    lbl_name = splits[split]["labels"].get(stem)
                    raw = ssh_helper.read_remote_bytes(sftp, f"{splits[split]['lbl_dir']}/{lbl_name}") if lbl_name else None
                    sig = mp.remote_label_signature((raw or b"").decode("utf-8", errors="replace"), idx_to_label, fmt, _dims(img))
                    category, reason = mp.decide_matched(baseline, cur["hash"], sig, expected_others, remote_baseline)
                    counts[category] += 1
                    if category != "unchanged":
                        items.append({**base_item, "category": category, "reason": reason, "split": split, "remote_name": name,
                                      "baseline": baseline, "remote_baseline": remote_baseline, "expected_others": expected_others})
                        counts["needs_class_append"] += 1 if (category == "update_label" and base_item["needs_class_append"]) else 0
            else:
                target_name = f"{img['id']}.{_ext(img.get('filename') or '')}"
                split = img.get("split") if img.get("split") in splits else mp.assign_split(
                    img.get("group_key") or img.get("content_hash") or img["id"], shares)
                if target_name in all_remote_names:
                    counts["name_collision"] += 1
                    items.append({**base_item, "category": "name_collision", "split": split, "remote_name": target_name,
                                  "reason": "a different file with this name is already on the server"})
                else:
                    counts["add"] += 1
                    adds_by_split[split] = adds_by_split.get(split, 0) + 1
                    counts["needs_class_append"] += 1 if base_item["needs_class_append"] else 0
                    items.append({**base_item, "category": "add", "split": split, "remote_name": target_name,
                                  "reason": "not on the server"})
            if n % 200 == 0:
                report("comparing", n, len(local))
        # Images here that are not approved (rejected, unreviewed, awaiting review) but whose bytes are already on the
        # server: the server copy is left untouched. They are not "only on the server" either.
        known_elsewhere = set()
        if hash_mode == "sha256" and by_hash:
            hs = list(by_hash)
            for i in range(0, len(hs), 1000):
                q = {"project_id": pid, "review_status": {"$ne": "approved"}, "content_hash": {"$in": hs[i:i + 1000]}}
                counts["skipped_rejected"] += sync_db.images.count_documents(q)
                known_elsewhere.update(sync_db.images.distinct("content_hash", q))
        counts["remote_only"] = sum(
            1 for info_split, info in splits.items() for name, _s in info["images"]
            if (info_split, name) not in matched and remote_hash.get((info_split, name)) not in known_elsewhere)

        yaml_change = None
        if slots and counts["needs_class_append"]:
            _new_text, reformatted = mp.append_class_names(text, slots)
            yaml_change = {"append": slots, "nc_before": len(names), "nc_after": len(names) + len(slots),
                           "reformatted": reformatted, "backup_path": f"{yaml_path}.bak.<timestamp>"}
            if reformatted:
                warnings.append("data.yaml will be re-written without its comments and original layout (it uses a form that "
                                "cannot be edited in place). A timestamped backup is kept.")

        for i in range(0, len(items), 500):
            sync_db.merge_plan_items.insert_many([dict(it) for it in items[i:i + 500]])
        return {
            "hash_mode": hash_mode, "label_format": fmt, "warnings": warnings, "counts": counts, "adds_by_split": adds_by_split,
            "yaml_change": yaml_change,
            "class_status": {"missing_in_remote": [s_["name"] for s_ in slots], "order_ok": True, "name_drift": maps["name_drift"]},
            "remote": {"data_yaml_path": yaml_path, "yaml_sha256": hashlib.sha256(text.encode()).hexdigest(), "names": names,
                       "split_counts": {s: len(i["images"]) for s, i in splits.items()}},
            "ratios": {s: v for s, v in shares},
            "expires_at": (datetime.now(timezone.utc) + PLAN_TTL).isoformat(),
        }
    finally:
        sftp.close()


def _find_match(img: dict, hash_mode: str, remote_hash: dict, by_hash: dict, by_key: dict, yaml_path: str):
    """(split, name) of this image on the server, or None. Content hash first; name+size in weak mode (and for
    server files whose hash could not be computed)."""
    h = img.get("content_hash")
    if hash_mode == "sha256" and h and h in by_hash:
        # identical bytes in several server splits (leakage that already exists there): stay with the split this image
        # is recorded in, otherwise the first in train/valid/test order. Never "move" an image between splits.
        own = img.get("split") or (img.get("last_push") or {}).get("remote_split")
        hits = sorted(by_hash[h], key=lambda sn: (sn[0] != own, mp.SPLIT_ORDER.index(sn[0])))
        return hits[0]
    size = img.get("size")
    names = [(img.get("last_push") or {}).get("remote_name"), (img.get("provenance") or {}).get("original_filename"),
             f"{img['id']}.{_ext(img.get('filename') or '')}"]
    for name in names:
        if name and size is not None and (name, size) in by_key:
            hit = by_key[(name, size)]
            if hash_mode == "weak" or (hit not in remote_hash):  # hashed files are only matched by content
                return hit
    return None


def execute_plan(client, sync_db, plan: dict, *, run_id: str, project_id: str, confirm_label_updates: bool,
                 update_class_names: bool, get_object_fn: Callable,
                 progress: Optional[Callable[[str, int, int], None]] = None) -> dict:
    """Apply a ready plan. Order: re-check the server, (optionally) extend data.yaml names, add new images (label first,
    then image, each via temp file + rename), then confirmed label updates (old label backed up first, server copy
    re-checked right before writing). Returns counts for the run record."""
    report = progress or (lambda *_a: None)
    remote = plan["remote"]
    yaml_path = remote["data_yaml_path"]
    yaml_dir = posixpath.dirname(yaml_path)
    sftp = client.open_sftp()
    try:
        text, cfg, names = _read_yaml(sftp, yaml_path)
        if hashlib.sha256(text.encode()).hexdigest() != remote["yaml_sha256"]:
            raise MergeError("The server's data.yaml changed since the preview was made. Preview the changes again.")
        try:
            layout = ssh_source.plan_remote_layout(sftp, cfg, yaml_dir, max_image_bytes=10 ** 15, allow_missing=True)["splits"]
        except ssh_source.StagingError as e:
            raise MergeError(str(e))
        project = sync_db.projects.find_one({"id": project_id})
        maps = mp.class_maps(names, project.get("classes", []), project.get("remote_dataset"))
        if maps["error"]:
            raise MergeError(maps["error"])

        stats = {"added": 0, "labels_updated": 0, "skipped_class_append": 0, "skipped_label_updates": 0,
                 "skipped_changed": 0, "skipped_exists": 0, "skipped_conflict": 0, "yaml_backup": None,
                 "added_by_split": {s: 0 for s in layout}}

        yc = plan.get("yaml_change")
        fmt = plan.get("label_format", "detect")
        label_to_idx = dict(maps["label_to_idx"])
        idx_to_label = dict(maps["idx_to_label"])
        appended = False
        if update_class_names and yc:
            slots = yc["append"]
            if names != remote["names"]:
                raise MergeError("The server's class list changed since the preview was made. Preview the changes again.")
            new_text, _reformatted = mp.append_class_names(text, slots)
            after = class_map_mod.server_names_from_yaml(yaml.safe_load(new_text)["names"])
            if after != names + [s["name"] for s in slots]:
                raise MergeError("Refusing to write data.yaml: the edited file would not be the old class list plus the new classes.")
            backup = _free_name(sftp, f"{yaml_path}.bak.{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}")
            ssh_helper.write_remote_atomic(sftp, backup, text.encode())
            ssh_helper.write_remote_atomic(sftp, yaml_path, new_text.encode())
            reread = ssh_helper.read_remote_bytes(sftp, yaml_path) or b""
            if class_map_mod.server_names_from_yaml(yaml.safe_load(reread.decode())["names"]) != after:
                raise MergeError(f"data.yaml verification failed after writing; the original is at {backup}")
            stats["yaml_backup"] = backup
            appended = True
            for s in slots:
                label_to_idx[s["name"]] = s["idx"]
                idx_to_label[s["idx"]] = s["name"]

        todo = list(sync_db.merge_plan_items.find({"plan_id": plan["id"], "category": {"$in": ["add", "update_label"]}}, {"_id": 0}).sort("image_id", 1))
        total = len(todo)
        for n, item in enumerate(todo, 1):
            report("uploading", n - 1, total)
            if item["needs_class_append"] and not appended:
                stats["skipped_class_append"] += 1
                continue
            if item["category"] == "update_label" and not confirm_label_updates:
                stats["skipped_label_updates"] += 1
                continue
            img = sync_db.images.find_one({"id": item["image_id"], "project_id": project_id, **APPROVED_Q})
            if not img:
                stats["skipped_changed"] += 1
                continue
            if fmt == "obb":
                _ensure_dims(sync_db, img, get_object_fn)
            size = _dims(img)
            cur = mp.current_label(img.get("annotations", []), label_to_idx, fmt, size)
            if cur["hash"] != item["new_hash"] or not cur["items"]:
                stats["skipped_changed"] += 1  # edited / reopened since the preview
                continue
            info = layout.get(item["split"])
            if info is None:
                stats["skipped_changed"] += 1
                continue
            if item["category"] == "add":
                if _add_image(sftp, sync_db, img, item, info, cur, get_object_fn, run_id, yaml_path, fmt, size, idx_to_label):
                    stats["added"] += 1
                    stats["added_by_split"][item["split"]] = stats["added_by_split"].get(item["split"], 0) + 1
                else:
                    stats["skipped_exists"] += 1
            else:
                outcome = _update_label(sftp, sync_db, img, item, info, cur, idx_to_label, yaml_dir, run_id, yaml_path, fmt, size)
                stats["labels_updated" if outcome else "skipped_conflict"] += 1
        report("uploading", total, total)

        split_counts = {s: len(layout[s]["images"]) + stats["added_by_split"].get(s, 0) for s in layout}
        stats["split_counts"] = split_counts
        return stats
    finally:
        sftp.close()


def _free_name(sftp, path: str) -> str:
    candidate, i = path, 0
    while True:
        try:
            sftp.stat(candidate)
        except (FileNotFoundError, IOError):
            return candidate
        i += 1
        candidate = f"{path}-{i}"


def _exists(sftp, path: str) -> bool:
    try:
        sftp.stat(path)
        return True
    except (FileNotFoundError, IOError):
        return False


def _add_image(sftp, sync_db, img: dict, item: dict, info: dict, cur: dict, get_object_fn, run_id: str, yaml_path: str,
               fmt: str, size, idx_to_label: dict) -> bool:
    name = item["remote_name"]
    stem = name.rsplit(".", 1)[0]
    img_path, lbl_path = f"{info['img_dir']}/{name}", f"{info['lbl_dir']}/{stem}.txt"
    if _exists(sftp, img_path):
        return False  # never overwrite: something with this name appeared since the preview
    data, _ct = get_object_fn(img["storage_path"])
    if not data:
        raise MergeError(f"image {img['id']} could not be read from storage")
    ssh_helper._ensure_remote_dir(sftp, info["img_dir"])
    ssh_helper._ensure_remote_dir(sftp, info["lbl_dir"])
    text = mp.label_file_text(cur["items"], [], size)
    # Label first, image last: if the run is interrupted, a stray label (ignored by trainers) is left behind rather
    # than an image with no label (which a trainer would read as "no objects").
    # The label name is built from this image's own uuid, so replacing a leftover from an interrupted run is safe.
    ssh_helper.write_remote_atomic(sftp, lbl_path, text.encode())
    ssh_helper.write_remote_atomic(sftp, img_path, data)
    _record_push(sync_db, img, item, run_id, yaml_path, cur["hash"], _written_hash(text, idx_to_label, fmt, size))
    return True


def _update_label(sftp, sync_db, img: dict, item: dict, info: dict, cur: dict, idx_to_label: dict, yaml_dir: str,
                  run_id: str, yaml_path: str, fmt: str, size) -> bool:
    name = item["remote_name"]
    stem = name.rsplit(".", 1)[0]
    lbl_name = f"{stem}.txt"
    lbl_path = f"{info['lbl_dir']}/{lbl_name}"
    old = ssh_helper.read_remote_bytes(sftp, lbl_path)
    # re-check right before writing: the server copy must still be what the preview saw
    sig = mp.remote_label_signature((old or b"").decode("utf-8", errors="replace"), idx_to_label, fmt, size)
    if sig["hash"] not in (item["baseline"], item.get("remote_baseline")) or sig["others"] != sorted(item.get("expected_others") or []):
        return False
    if old is not None:
        backup = f"{yaml_dir}/.thinkvision_backups/{run_id}/{item['split']}/labels/{lbl_name}"
        ssh_helper._ensure_remote_dir(sftp, posixpath.dirname(backup))
        ssh_helper.write_remote_atomic(sftp, backup, old)
    preserved = item.get("expected_others") or []
    ssh_helper._ensure_remote_dir(sftp, info["lbl_dir"])
    text = mp.label_file_text(cur["items"], preserved, size)
    ssh_helper.write_remote_atomic(sftp, lbl_path, text.encode())
    _record_push(sync_db, img, item, run_id, yaml_path, cur["hash"], _written_hash(text, idx_to_label, fmt, size))
    return True


def _written_hash(text: str, idx_to_label: dict, fmt: str, size) -> str:
    """What the server's label file will parse to: the baseline later previews compare the server copy against."""
    return mp.remote_label_signature(text, idx_to_label, fmt, size)["hash"]


def _record_push(sync_db, img: dict, item: dict, run_id: str, yaml_path: str, label_hash: str, remote_hash: str) -> None:
    sets = {"last_push": {"run_id": run_id, "label_hash": label_hash, "remote_hash": remote_hash, "at": _now(),
                          "remote_name": item["remote_name"], "remote_split": item["split"], "data_yaml_path": yaml_path}}
    sync_db.images.update_one({"id": img["id"]}, {"$set": sets})
    # the split is decided once and never changes afterwards
    sync_db.images.update_one({"id": img["id"], "$or": [{"split": None}, {"split": {"$exists": False}}]},
                              {"$set": {"split": item["split"]}})
