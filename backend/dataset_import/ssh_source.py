"""Stage a dataset that lives on a training server into a local zip, so the zip scan/commit pipeline can be reused.

The stager reads the remote data.yaml, finds the train/val/test image + label folders it lists, and copies the
images and label files over one SFTP session into an UNCOMPRESSED zip on disk with a normalised layout:

    data.yaml                      (train: train/images, val: valid/images, test: test/images, names: [...])
    train/images/<file>  train/labels/<stem>.txt   (and valid/, test/)

Nothing is read into memory beyond one chunk. Credentials are used only inside this call: they are never
written to Mongo or logs, and error text is scrubbed of their values before it is stored or returned.
"""
import os
import posixpath
import shutil
import time
import zipfile
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

import yaml

import pipeline_logic
import ssh_helper
from dataset_import import class_map as class_map_mod
from dataset_import import scan as scan_mod

MAX_YAML_BYTES = scan_mod.MAX_YAML_BYTES
FREE_SPACE_MARGIN = 1.1


class StagingError(Exception):
    """User-facing problem with the remote dataset or the staging disk."""


def scrub(message: str, creds: dict) -> str:
    """Remove any credential value from an error message before it is stored or returned."""
    out = str(message)
    for key in ("password", "pem_key"):
        secret = creds.get(key)
        if secret:
            out = out.replace(secret, "***")
            for line in str(secret).splitlines():
                if len(line.strip()) >= 6:
                    out = out.replace(line.strip(), "***")
    return out


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_remote_yaml(sftp, yaml_path: str) -> dict:
    try:
        with sftp.open(yaml_path, "rb") as f:
            raw = f.read(MAX_YAML_BYTES + 1)
    except (FileNotFoundError, IOError):
        raise StagingError(f"Could not read {yaml_path} on the server")
    if len(raw) > MAX_YAML_BYTES:
        raise StagingError("data.yaml is unreasonably large")
    try:
        cfg = yaml.safe_load(raw.decode("utf-8", errors="replace"))
    except yaml.YAMLError as e:
        raise StagingError(f"data.yaml is not valid YAML: {e}")
    if not isinstance(cfg, dict):
        raise StagingError("data.yaml is not a mapping")
    return cfg


def plan_remote_layout(sftp, cfg: dict, yaml_dir: str, *, max_image_bytes: int, allow_missing: bool = False) -> dict:
    """Which remote files to copy: {split: {"images": [(name, size)], "labels": {stem: name}, "img_dir", "lbl_dir"}},
    plus counts of what was skipped. Only splits the yaml lists; a missing test key does not fall back to val."""
    cfg = dict(cfg)
    for alias in ("valid", "validation"):
        if alias in cfg and not cfg.get("val"):
            cfg["val"] = cfg[alias]
    def exists(p):
        try:
            sftp.stat(p)
            return True
        except (FileNotFoundError, IOError):
            return False
    dirs = pipeline_logic.resolve_yaml_split_dirs(cfg, yaml_dir, path_exists=exists)
    if not cfg.get("test"):
        dirs.pop("test", None)
    layout: Dict[str, dict] = {}
    skipped_too_large = 0
    missing_dirs: List[str] = []
    for split, (img_dir, lbl_dir) in dirs.items():
        files = ssh_helper.list_files(sftp, img_dir)
        if not files and not exists(img_dir):
            missing_dirs.append(f"{split}: {img_dir}")
            if not allow_missing:
                continue
        images = []
        for name, size in sorted(files):
            if scan_mod.ext_of(name) not in scan_mod.IMAGE_EXTS:
                continue
            if size > max_image_bytes:
                skipped_too_large += 1
                continue
            images.append((name, size))
        labels = {n.rsplit(".", 1)[0]: n for n, _s in ssh_helper.list_files(sftp, lbl_dir) if n.endswith(".txt")}
        layout[split] = {"images": images, "labels": labels, "img_dir": img_dir, "lbl_dir": lbl_dir}
    if not layout:
        raise StagingError("None of the train/val/test image folders listed in data.yaml exist on the server")
    return {"splits": layout, "skipped_too_large": skipped_too_large, "missing_dirs": missing_dirs}


def normalised_yaml(layout: dict, names: List[str]) -> str:
    cfg = {}
    for split, key in (("train", "train"), ("valid", "val"), ("test", "test")):
        if split in layout["splits"]:
            cfg[key] = f"{split}/images"
    cfg["names"] = names
    return yaml.safe_dump(cfg, sort_keys=False)


def stage_ssh_sync(sync_db, batch_id: str, creds: dict, yaml_path: str, should_stop: Callable[[], bool], *,
                   max_image_bytes: int, max_total_bytes: int, max_files: int) -> bool:
    """Copy the remote dataset into the batch's zip. Returns True when finished, False if cancelled.
    Raises StagingError for problems with the dataset; any other exception is a connection/transfer failure."""
    batch = sync_db.import_batches.find_one({"id": batch_id})
    zip_path = batch["zip_path"]
    os.makedirs(os.path.dirname(zip_path), exist_ok=True)

    def progress(done: int, total: int) -> None:
        sync_db.import_batches.update_one({"id": batch_id}, {"$set": {"progress": {"done": done, "total": total}, "updated_at": _now()}})

    with ssh_helper.connect(creds["host"], creds.get("port", 22), creds["username"], creds.get("pem_key"), creds.get("password")) as client:
        yaml_abs = ssh_helper.resolve_remote_path(client, yaml_path)
        sftp = client.open_sftp()
        try:
            cfg = read_remote_yaml(sftp, yaml_abs)
            try:
                names = class_map_mod.server_names_from_yaml(cfg.get("names"))
            except ValueError as e:
                raise StagingError(str(e))
            layout = plan_remote_layout(sftp, cfg, posixpath.dirname(yaml_abs), max_image_bytes=max_image_bytes)
            jobs = []  # (remote_path, arcname)
            total_bytes = 0
            for split, info in layout["splits"].items():
                for name, size in info["images"]:
                    jobs.append((f"{info['img_dir']}/{name}", f"{split}/images/{name}"))
                    total_bytes += size
                    stem = name.rsplit(".", 1)[0]
                    if stem in info["labels"]:
                        jobs.append((f"{info['lbl_dir']}/{info['labels'][stem]}", f"{split}/labels/{stem}.txt"))
            if sum(1 for j in jobs if "/images/" in j[1]) > max_files:
                raise StagingError(f"The dataset has more than {max_files} images (import limit)")
            if total_bytes > max_total_bytes:
                raise StagingError("The dataset is larger than the import size limit")
            free = shutil.disk_usage(os.path.dirname(zip_path)).free
            if total_bytes * FREE_SPACE_MARGIN > free:
                raise StagingError(
                    f"Not enough free disk space to stage this dataset ({total_bytes // 2**20} MB needed, {free // 2**20} MB free)")

            sync_db.import_batches.update_one({"id": batch_id}, {"$set": {
                "remote": {"host": creds["host"], "port": creds.get("port", 22), "data_yaml_path": yaml_abs},
                "staging": {"skipped_too_large": layout["skipped_too_large"], "missing_dirs": layout["missing_dirs"],
                            "files": len(jobs), "bytes": total_bytes}}})
            total = len(jobs)
            progress(0, total)
            try:
                with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
                    zf.writestr("data.yaml", normalised_yaml(layout, names))
                    for i, (remote_path, arcname) in enumerate(jobs, 1):
                        if i % 20 == 0:
                            if should_stop():
                                return False
                            progress(i, total)
                        zinfo = zipfile.ZipInfo(arcname, date_time=time.localtime()[:6])
                        zinfo.compress_type = zipfile.ZIP_STORED
                        with zf.open(zinfo, "w", force_zip64=True) as dest:
                            sftp.getfo(remote_path, dest)
                progress(total, total)
            except BaseException:
                try:
                    os.remove(zip_path)  # a half-written zip is useless; staging restarts from scratch
                except OSError:
                    pass
                raise
        finally:
            sftp.close()
    return True
