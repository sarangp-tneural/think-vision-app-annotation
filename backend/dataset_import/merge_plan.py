"""Pure decision logic for merging ThinkVision images back into an existing server dataset (no db, no SSH).

Everything here is deterministic and unit-tested; merge_remote.py does the I/O around it.

Rules this module encodes
* An image already on the server (same content, or same name+size in weak mode) is never re-uploaded and never
  moves to another split.
* Its label file is only touched when the label was edited here since the last sync AND the server's copy is still
  what we last saw (otherwise it is a `conflict` and is left alone).
* New images get a split from a hash bucket, so identical content always lands in the same split and the choice
  never changes between runs.
"""
import hashlib
import json
import re
from typing import Dict, List, Optional, Tuple

import yaml

import yolo_codec

SPLIT_ORDER = ("train", "valid", "test")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_\-. ]+$")


# ---------- split assignment ----------

def bucket(key: str) -> float:
    """Stable position in [0, 1) for a string."""
    return int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) / 2 ** 32


def split_shares(train: float, valid: float, test: float, available: List[str]) -> List[Tuple[str, float]]:
    """Ratios restricted to the splits the remote dataset actually has, renormalised to sum to 1."""
    raw = {"train": train, "valid": valid, "test": test}
    chosen = [(s, max(raw[s], 0.0)) for s in SPLIT_ORDER if s in available]
    total = sum(v for _s, v in chosen)
    if not chosen:
        raise ValueError("the remote dataset has no train/val/test split to add images to")
    if total <= 0:  # e.g. only `test` available but its ratio is 0
        return [(s, 1.0 / len(chosen)) for s, _v in chosen]
    return [(s, v / total) for s, v in chosen]


def assign_split(group_key: str, shares: List[Tuple[str, float]]) -> str:
    """Deterministic split for a new image. `group_key` is the video id for frames (so one video never spans
    splits) or the content hash."""
    b = bucket(group_key)
    acc = 0.0
    for split, share in shares:
        acc += share
        if b < acc:
            return split
    return shares[-1][0]


# ---------- classes ----------

def class_maps(remote_names: List[str], project_classes: List[str], link: Optional[dict]) -> dict:
    """Server index <-> project label.

    The server's `names` order is the authority for indices. If the project was imported from this dataset, the
    stored class_map says which project label stands for each server class (the user may have mapped or renamed
    them); otherwise labels are matched by name. Returns idx_to_label, label_to_idx, name_drift, error."""
    idx_to_label: Dict[int, Optional[str]] = {i: n for i, n in enumerate(remote_names)}
    drift = []
    error = None
    if link and link.get("class_map") is not None and link.get("server_names"):
        linked = link["server_names"]
        if remote_names[:len(linked)] != linked:
            error = ("The server's class list no longer matches the one this project was imported from "
                     f"(imported: {linked}; now: {remote_names}). Nothing was changed.")
        else:
            for k, label in link["class_map"].items():
                i = int(k)
                if i < len(remote_names):
                    idx_to_label[i] = label  # may be None: a skipped class
                    if label is not None and label != remote_names[i]:
                        drift.append({"idx": i, "server_name": remote_names[i], "project_label": label})
    label_to_idx: Dict[str, int] = {}
    for i, label in idx_to_label.items():
        if label is not None and label not in label_to_idx:
            label_to_idx[label] = i
    return {"idx_to_label": idx_to_label, "label_to_idx": label_to_idx, "name_drift": drift, "error": error}


def new_class_slots(used_labels: List[str], project_classes: List[str], label_to_idx: Dict[str, int], nc: int) -> List[dict]:
    """Project classes that are used by images being pushed but have no server index yet get the next free
    indices, in project order (append-only)."""
    used = set(used_labels)
    slots = []
    for c in project_classes:
        if c in used and c not in label_to_idx:
            slots.append({"idx": nc + len(slots), "name": c})
    return slots


# ---------- label comparison ----------

def current_label(annotations: List[dict], label_to_idx: Dict[str, int], label_format: str = "detect",
                 size: Optional[Tuple[int, int]] = None) -> dict:
    """The project's annotations for one image, in the server dataset's label format."""
    items, unknown, lossy = yolo_codec.annotations_to_items(annotations, label_to_idx, label_format, size)
    return {"items": items, "hash": yolo_codec.canonical_hash(items, size), "unknown_labels": unknown, "lossy": lossy}


def remote_label_signature(text: str, idx_to_label: Dict[int, Optional[str]], label_format: str = "detect",
                           size: Optional[Tuple[int, int]] = None) -> dict:
    """What the server's label file says, in the same terms as `current_label`: the hash of the shapes we own, plus
    the lines we do not own (skipped classes, unknown ids, formats we cannot read, garbage), which must be left alone.
    The shapes go through the same annotation conversion as our own labels, so a box in a segmentation file and the
    4-point polygon we would write for it compare equal."""
    parsed = yolo_codec.parse_label_text(text or "", label_format, size)
    owned, others = [], list(parsed.unsupported) + list(parsed.malformed)
    label_to_idx: Dict[str, int] = {}
    for b in parsed.boxes:
        label = idx_to_label.get(b["cls"])
        if label is None:
            others.append(b["raw"])
            continue
        label_to_idx.setdefault(label, b["cls"])
        owned.append(yolo_codec.box_to_annotation(b, label))
    items, _unknown, _lossy = yolo_codec.annotations_to_items(owned, label_to_idx, label_format, size)
    return {"hash": yolo_codec.canonical_hash(items, size), "others": sorted(others)}


def needs_remote_label(baseline: Optional[str], current_hash: str) -> bool:
    """False when the label is unchanged here since the last sync: the server copy is not even read."""
    return baseline is None or baseline != current_hash


def decide_matched(baseline: Optional[str], current_hash: str, remote: dict, expected_others: List[str],
                   remote_baseline: Optional[str] = None) -> Tuple[str, str]:
    """Category for an image that is already on the server, given the server's label signature.

    `baseline` is our label as of the last sync (detects a local edit); `remote_baseline` is what the server's file
    parsed to right after that sync (it can differ in the last decimal for rotated boxes, because corners are written
    with 6 decimals). Without a separate remote baseline the two are the same."""
    remote_baseline = remote_baseline or baseline
    if remote["hash"] == current_hash and remote["others"] == sorted(expected_others):
        return "unchanged", "server label already matches"
    if baseline is None:
        return "conflict", "no record of the last sync, and the server label differs from this one"
    if remote["hash"] in (baseline, remote_baseline) and remote["others"] == sorted(expected_others):
        return "update_label", "edited here since the last sync; server copy still as last seen"
    return "conflict", "the server label was changed since the last sync"


def label_file_text(items: List, preserved_lines: List[str], size: Optional[Tuple[int, int]] = None) -> str:
    lines = [yolo_codec.item_to_line(it, size) for it in items] + list(preserved_lines or [])
    return "\n".join(lines) + ("\n" if lines else "")


# ---------- data.yaml class append ----------

def _scalar(name: str) -> str:
    return name if _SAFE_NAME.match(name) else json.dumps(name)


def append_class_names(text: str, additions: List[dict]) -> Tuple[str, bool]:
    """New data.yaml text with `additions` ([{idx, name}], consecutive after the current last class) appended.
    Edits the original text in place when it uses the common block layout (`names:` followed by `N: name` or
    `- name` lines) so comments and formatting survive; returns (text, reformatted). Anything else (flow lists
    like `names: [a, b]`) is re-serialised, which drops comments, and reformatted=True says so."""
    cfg = yaml.safe_load(text)
    names = cfg.get("names")
    count = len(names)
    if not additions:
        return text, False
    expected = list(range(count, count + len(additions)))
    if [a["idx"] for a in additions] != expected:
        raise ValueError("new classes must continue the existing indices")
    lines = text.split("\n")
    start = next((i for i, l in enumerate(lines) if re.match(r"^names\s*:\s*(#.*)?$", l)), None)
    edited = None
    if start is not None:
        j = start + 1
        last = start
        style = None
        while j < len(lines):
            l = lines[j]
            if re.match(r"^\s+\d+\s*:", l) and style in (None, "dict"):
                style, last = "dict", j
            elif re.match(r"^\s+-\s", l) and style in (None, "list"):
                style, last = "list", j
            elif l.strip() == "" or re.match(r"^\s+#", l):
                pass
            else:
                break
            j += 1
        if style:
            indent = re.match(r"^(\s+)", lines[last]).group(1)
            new = [f"{indent}{a['idx']}: {_scalar(a['name'])}" if style == "dict" else f"{indent}- {_scalar(a['name'])}"
                   for a in additions]
            edited = lines[:last + 1] + new + lines[last + 1:]
            edited = "\n".join(edited)
            edited = re.sub(r"^(nc\s*:\s*)\d+", lambda m: f"{m.group(1)}{count + len(additions)}", edited, count=1, flags=re.M)
    if edited is not None:
        try:
            ok = _only_names_changed(cfg, yaml.safe_load(edited), additions)
        except yaml.YAMLError:
            ok = False
        if ok:
            return edited, False
    new_cfg = dict(cfg)
    if isinstance(names, dict):
        new_cfg["names"] = {**names, **{a["idx"]: a["name"] for a in additions}}
    else:
        new_cfg["names"] = list(names) + [a["name"] for a in additions]
    if "nc" in new_cfg:
        new_cfg["nc"] = count + len(additions)
    return yaml.safe_dump(new_cfg, sort_keys=False), True


def _only_names_changed(old: dict, new: dict, additions: List[dict]) -> bool:
    """The edited yaml must be the old one plus exactly the new names (and nc), nothing else."""
    from dataset_import.class_map import server_names_from_yaml
    try:
        before, after = server_names_from_yaml(old["names"]), server_names_from_yaml(new["names"])
    except ValueError:
        return False
    if after != before + [a["name"] for a in additions]:
        return False
    strip = lambda c: {k: v for k, v in c.items() if k not in ("names", "nc")}  # noqa: E731
    if strip(old) != strip(new):
        return False
    return "nc" not in new or new["nc"] == len(after)
