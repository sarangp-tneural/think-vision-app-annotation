"""Readers for the dataset layouts a zip can hold: YOLO (data.yaml + images/labels folders), COCO (annotation json) and
Pascal VOC (one xml per image). Each reader lists the images with their split and a reference to their labels, and
turns a label into the same shape-dicts the YOLO codec produces ({"cls", "kind": "bbox"|"polygon"|"obb", ...}), so
scanning and committing do not care where a label came from.

Class order: YOLO's data.yaml `names` and COCO's categories (sorted by id) define it. Pascal VOC has no class list, so
its classes are the object names in alphabetical order (the report says so; the mapping screen lets the user map them).

Everything is read from the zip by member name, one file at a time. XML is parsed only after refusing DOCTYPE/ENTITY
declarations (entity-expansion bombs); COCO json is size-capped because it is parsed in memory.
"""
import json
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

import yaml

import pipeline_logic
import yolo_codec
from dataset_import import class_map as class_map_mod
from dataset_import.common import (IMAGE_EXTS, MAX_COCO_JSON_BYTES, MAX_LABEL_BYTES, MAX_XML_BYTES, MAX_YAML_BYTES,
                                   ImportError_, ext_of, read_capped)

SOURCE_FORMATS = ("yolo", "coco", "voc")
EMPTY = yolo_codec.ParsedLabels([], [], [])


def _clamp(v: float) -> float:
    return min(1.0, max(0.0, v))


def split_from_path(path: str) -> Optional[str]:
    """train/valid/test from the nearest path component that names one (file name first, then folders)."""
    for part in reversed(path.lower().split("/")):
        if "trainval" in part:
            return None
        if re.search(r"(^|[^a-z])(train|training)", part):
            return "train"
        if re.search(r"(^|[^a-z])(val|valid|validation)([^a-z]|$)", part):
            return "valid"
        if re.search(r"(^|[^a-z])test", part):
            return "test"
    return None


def _image_index(names: List[str]):
    by_base: Dict[str, List[str]] = defaultdict(list)
    for n in names:
        if ext_of(n) in IMAGE_EXTS:
            by_base[posixpath.basename(n).lower()].append(n)
    return by_base


def _closest(candidates: List[str], near_dir: str) -> Optional[str]:
    """The candidate sharing the longest folder prefix with `near_dir`; ties keep zip order."""
    if not candidates:
        return None
    def shared(p):
        a, b = posixpath.dirname(p).split("/"), near_dir.split("/")
        n = 0
        for x, y in zip(a, b):
            if x != y:
                break
            n += 1
        return n
    return max(candidates, key=shared)


# ---------------------------------------------------------------- YOLO

def locate_yaml(names: List[str]) -> str:
    """Shallowest data.yaml / data.yml in the zip."""
    cands = [n for n in names if posixpath.basename(n) in ("data.yaml", "data.yml")]
    if not cands:
        raise ImportError_("No data.yaml found in the zip. It is required: its `names` list defines the class order.")
    return sorted(cands, key=lambda n: (n.count("/"), n))[0]


def load_yaml(zf: zipfile.ZipFile, member: str) -> dict:
    info = zf.getinfo(member)
    if info.file_size > MAX_YAML_BYTES:
        raise ImportError_("data.yaml is unreasonably large")
    try:
        cfg = yaml.safe_load(zf.read(member).decode("utf-8", errors="replace"))
    except yaml.YAMLError as e:
        raise ImportError_(f"data.yaml is not valid YAML: {e}")
    if not isinstance(cfg, dict):
        raise ImportError_("data.yaml is not a mapping")
    return cfg


def split_layout(names: List[str], yaml_member: str, cfg: dict) -> Dict[str, Tuple[str, str]]:
    """split -> (images_dir, labels_dir) inside the zip. Only splits the yaml actually lists (the yaml's absolute
    server `path:` is meaningless here and ignored). Roboflow's `../train/images` is repaired when needed."""
    yaml_dir = posixpath.dirname(yaml_member)
    cfg = dict(cfg)
    for alias in ("valid", "validation"):  # some exporters name the key differently from the usual `val`
        if alias in cfg and not cfg.get("val"):
            cfg["val"] = cfg[alias]
    resolved = pipeline_logic.resolve_yaml_split_dirs(cfg, yaml_dir, path_exists=lambda p: False)
    if not cfg.get("test"):
        resolved.pop("test", None)  # resolve_yaml_split_dirs falls back test -> val; here that would import val twice
    dirs_with_files = {posixpath.dirname(n) for n in names}
    out: Dict[str, Tuple[str, str]] = {}
    for split, (img_dir, lbl_dir) in resolved.items():
        img_dir, lbl_dir = _repair(img_dir, lbl_dir, yaml_dir, dirs_with_files)
        if img_dir in dirs_with_files:
            out[split] = (img_dir, lbl_dir)
    if not out:
        raise ImportError_("data.yaml's train/val/test folders were not found inside the zip")
    return out


def _repair(img_dir: str, lbl_dir: str, yaml_dir: str, dirs: set) -> Tuple[str, str]:
    if img_dir in dirs:
        return img_dir, lbl_dir
    stripped = img_dir
    while stripped.startswith("../"):
        stripped = stripped[3:]
    for cand in (stripped, posixpath.normpath(posixpath.join(yaml_dir, stripped))):
        if cand in dirs:
            parts = cand.split("/")
            for i in range(len(parts) - 1, -1, -1):
                if parts[i] == "images":
                    parts[i] = "labels"
                    break
            else:
                parts = parts[:-1] + ["labels"]
            return cand, "/".join(parts)
    return img_dir, lbl_dir


def iter_entries(zf: zipfile.ZipFile, layout: Dict[str, Tuple[str, str]], members: set):
    """Yield {split, rel_path, label_path|None, filename} for every image in a listed split dir, in zip order."""
    by_dir = defaultdict(list)
    for n in members:
        by_dir[posixpath.dirname(n)].append(n)
    for split in ("train", "valid", "test"):
        if split not in layout:
            continue
        img_dir, lbl_dir = layout[split]
        for rel in sorted(by_dir.get(img_dir, [])):
            fname = posixpath.basename(rel)
            if ext_of(fname) not in IMAGE_EXTS:
                continue
            stem = fname.rsplit(".", 1)[0]
            lbl = f"{lbl_dir}/{stem}.txt" if lbl_dir else f"{stem}.txt"
            yield {"split": split, "rel_path": rel, "filename": fname, "label_path": lbl if lbl in members else None}


def open_yolo(zf: zipfile.ZipFile, names: List[str], requested_label_format: str) -> dict:
    yaml_member = locate_yaml(names)
    cfg = load_yaml(zf, yaml_member)
    try:
        server_names = class_map_mod.server_names_from_yaml(cfg.get("names"))
    except ValueError as e:
        raise ImportError_(str(e))
    layout = split_layout(names, yaml_member, cfg)
    entries = list(iter_entries(zf, layout, set(names)))
    token_counts = set()
    for e in entries:
        if e["label_path"]:
            raw = read_capped(zf, e["label_path"], MAX_LABEL_BYTES)
            for line in (raw or b"").decode("utf-8", errors="replace").splitlines():
                s = line.strip()
                if s and not s.startswith("#"):
                    token_counts.add(len(s.split()))
    fmt, problem = yolo_codec.resolve_format(token_counts, requested_label_format)
    if problem:
        raise ImportError_(problem)
    for e in entries:
        e["label_ref"] = None
    ignored = len([n for n in names if ext_of(n) not in IMAGE_EXTS and not n.endswith(".txt")
                   and posixpath.basename(n) not in ("data.yaml", "data.yml")])
    return {"kind": "yolo", "names": server_names, "entries": entries, "label_format": fmt, "ignored": ignored,
            "data_yaml_member": yaml_member, "description": yaml_member, "notes": {}}


# ---------------------------------------------------------------- COCO

def _json_members(zf: zipfile.ZipFile, names: List[str]) -> List[str]:
    return [n for n in sorted(names, key=lambda n: (n.count("/"), n)) if ext_of(n) == "json"]


def load_coco_file(zf: zipfile.ZipFile, member: str) -> Optional[dict]:
    """The parsed json if it is a COCO annotation file (images + annotations + categories), else None."""
    if zf.getinfo(member).file_size > MAX_COCO_JSON_BYTES:
        return None
    try:
        data = json.loads(zf.read(member).decode("utf-8", errors="replace"))
    except ValueError:
        return None
    if isinstance(data, dict) and all(isinstance(data.get(k), list) for k in ("images", "annotations", "categories")):
        return data
    return None


def _is_placeholder(cat: dict) -> bool:
    return str(cat.get("supercategory") or "none").lower() == "none"


def open_coco(zf: zipfile.ZipFile, names: List[str]) -> dict:
    members = [m for m in _json_members(zf, names)]
    files = {}
    for m in members:
        data = load_coco_file(zf, m)
        if data is not None:
            files[m] = data
    if not files:
        raise ImportError_("No COCO annotation file found (a .json with images, annotations and categories).")
    cats: Dict[int, str] = {}
    used = set()
    for m, data in files.items():
        for c in data["categories"]:
            try:
                cid, cname = int(c["id"]), str(c["name"]).strip()
            except (KeyError, TypeError, ValueError):
                raise ImportError_(f"{m} has a malformed category")
            if cid in cats and cats[cid] != cname:
                raise ImportError_(f'The annotation files disagree about category {cid}: "{cats[cid]}" vs "{cname}"')
            cats[cid] = cname
        used.update(a.get("category_id") for a in data["annotations"])
    placeholder = {int(c["id"]) for d in files.values() for c in d["categories"] if _is_placeholder(c)}
    # an exporter's catch-all category (supercategory "none") that no box uses is not a real class
    kept = sorted(cid for cid in cats if not (cid in placeholder and cid not in used))
    if not kept:
        raise ImportError_("The COCO file has no categories")
    cat_map = {str(cid): i for i, cid in enumerate(kept)}
    server_names = [cats[cid][:class_map_mod.MAX_NAME_LEN] for cid in kept]
    by_base = _image_index(names)
    name_set = set(names)
    entries, missing, seen = [], 0, set()
    for m, data in files.items():
        mdir = posixpath.dirname(m)
        for img in sorted(data["images"], key=lambda i: str(i.get("id"))):
            fn = str(img.get("file_name") or "")
            if not fn:
                missing += 1
                continue
            direct = [posixpath.normpath(posixpath.join(mdir, fn)), posixpath.normpath(posixpath.join(mdir, "images", fn)),
                      posixpath.normpath(posixpath.join(posixpath.dirname(mdir), "images", fn))]
            rel = next((p for p in direct if p in name_set), None) or _closest(by_base.get(posixpath.basename(fn).lower(), []), mdir)
            if not rel:
                missing += 1
                continue
            if rel in seen:
                continue
            seen.add(rel)
            entries.append({"split": split_from_path(m) or split_from_path(rel), "rel_path": rel, "filename": posixpath.basename(rel),
                            "label_path": None, "label_ref": {"kind": "coco", "json": m, "image_id": img.get("id")}})
    ignored = len(names) - len(seen) - len(files)
    return {"kind": "coco", "names": server_names, "entries": entries, "label_format": "coco", "ignored": max(ignored, 0),
            "data_yaml_member": None, "description": ", ".join(sorted(files)), "cat_map": cat_map,
            "notes": {"missing_files": missing, "names_order": "category id order"}}


class CocoLabels:
    """Per-image annotations of the COCO files in a zip, loaded once."""

    def __init__(self, zf: zipfile.ZipFile, cat_map: Dict[str, int]):
        self.zf, self.cat_map, self.by_file = zf, cat_map, {}

    def anns(self, json_member: str, image_id) -> List[dict]:
        if json_member not in self.by_file:
            data = load_coco_file(self.zf, json_member) or {"annotations": []}
            grouped = defaultdict(list)
            for a in data["annotations"]:
                grouped[json.dumps(a.get("image_id"))].append(a)
            self.by_file[json_member] = grouped
        return self.by_file[json_member].get(json.dumps(image_id), [])

    def parse(self, json_member: str, image_id, size: Tuple[int, int]) -> yolo_codec.ParsedLabels:
        W, H = size
        boxes, unsupported, malformed = [], [], []
        for a in self.anns(json_member, image_id):
            raw = json.dumps(a, sort_keys=True)[:2000]
            cls = self.cat_map.get(str(a.get("category_id")))
            if cls is None:
                malformed.append(raw)
                continue
            seg = a.get("segmentation")
            if a.get("iscrowd") or isinstance(seg, dict):  # crowd regions and RLE masks have no box/polygon form here
                unsupported.append(raw)
                continue
            try:
                if isinstance(seg, list) and seg:
                    ring = seg[0] if len(seg) == 1 and isinstance(seg[0], list) else None
                    if ring is None or len(ring) < 6 or len(ring) % 2:
                        unsupported.append(raw)  # multi-part shapes
                        continue
                    pts = [[_clamp(float(ring[i]) / W), _clamp(float(ring[i + 1]) / H)] for i in range(0, len(ring), 2)]
                    if yolo_codec.polygon_area(pts) <= 0:
                        raise ValueError("degenerate polygon")
                    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
                    boxes.append({"cls": cls, "kind": "polygon", "points": pts, "x": min(xs), "y": min(ys),
                                  "w": max(xs) - min(xs), "h": max(ys) - min(ys), "raw": raw})
                    continue
                x, y, w, h = (float(v) for v in a["bbox"])
                x0, y0, x1, y1 = _clamp(x / W), _clamp(y / H), _clamp((x + w) / W), _clamp((y + h) / H)
                if x1 - x0 <= 0 or y1 - y0 <= 0:
                    raise ValueError("empty box")
                boxes.append({"cls": cls, "kind": "bbox", "x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0, "raw": raw})
            except (KeyError, TypeError, ValueError):
                malformed.append(raw)
        return yolo_codec.ParsedLabels(boxes, unsupported, malformed)


# ---------------------------------------------------------------- Pascal VOC

def parse_xml(data: bytes) -> Optional[ET.Element]:
    """Root of a VOC annotation, or None. Refuses documents that declare entities."""
    head = data[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in data.lower():
        return None
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None
    return root if root.tag == "annotation" else None


def _read_xml(zf: zipfile.ZipFile, member: str) -> Optional[ET.Element]:
    raw = read_capped(zf, member, MAX_XML_BYTES)
    return parse_xml(raw) if raw is not None else None


def open_voc(zf: zipfile.ZipFile, names: List[str]) -> dict:
    xmls = [n for n in sorted(names) if ext_of(n) == "xml"]
    docs, class_names = [], set()
    for m in xmls:
        root = _read_xml(zf, m)
        if root is None:
            continue
        for o in root.findall("object"):
            nm = (o.findtext("name") or "").strip()
            if nm:
                class_names.add(nm)
        docs.append((m, root))
    if not docs:
        raise ImportError_("No Pascal VOC annotation files found (.xml files whose root is <annotation>).")
    if not class_names:
        raise ImportError_("The VOC annotations contain no objects, so there are no classes to import.")
    server_names = [n[:class_map_mod.MAX_NAME_LEN] for n in sorted(class_names)]
    by_base = _image_index(names)
    by_stem = {(posixpath.dirname(n), posixpath.basename(n).rsplit(".", 1)[0]): n for n in names if ext_of(n) in IMAGE_EXTS}
    name_set = set(names)
    lists = {}  # split -> set of stems, from ImageSets/*/<split>.txt
    for n in names:
        s = split_from_path(n) if n.lower().endswith(".txt") and "/imagesets/" in "/" + n.lower() else None
        if s and posixpath.basename(n).lower() not in ("trainval.txt",):
            lists.setdefault(s, set()).update(l.strip().split()[0] for l in (read_capped(zf, n, MAX_LABEL_BYTES) or b"").decode("utf-8", "replace").splitlines() if l.strip())
    entries, missing, seen = [], 0, set()
    for m, root in docs:
        mdir = posixpath.dirname(m)
        fn = (root.findtext("filename") or "").strip()
        rel = None
        if fn:
            for cand in (posixpath.normpath(posixpath.join(mdir, fn)), posixpath.normpath(posixpath.join(posixpath.dirname(mdir), "JPEGImages", fn))):
                if cand in name_set:
                    rel = cand
                    break
            rel = rel or _closest(by_base.get(posixpath.basename(fn).lower(), []), mdir)
        stem = posixpath.basename(m).rsplit(".", 1)[0]
        rel = rel or by_stem.get((mdir, stem)) or by_stem.get((posixpath.join(posixpath.dirname(mdir), "JPEGImages"), stem))
        if not rel:
            missing += 1
            continue
        if rel in seen:
            continue
        seen.add(rel)
        istem = posixpath.basename(rel).rsplit(".", 1)[0]
        split = next((s for s, stems in lists.items() if istem in stems), None) or split_from_path(m) or split_from_path(rel)
        entries.append({"split": split, "rel_path": rel, "filename": posixpath.basename(rel), "label_path": None,
                        "label_ref": {"kind": "voc", "xml": m}})
    return {"kind": "voc", "names": server_names, "entries": entries, "label_format": "voc", "ignored": max(len(names) - len(seen) - len(docs), 0),
            "data_yaml_member": None, "description": f"{len(docs)} xml files",
            "notes": {"missing_files": missing, "names_order": "alphabetical (Pascal VOC has no class list)"}}


def parse_voc(zf: zipfile.ZipFile, xml_member: str, name_to_idx: Dict[str, int], size: Tuple[int, int]) -> yolo_codec.ParsedLabels:
    W, H = size
    root = _read_xml(zf, xml_member)
    if root is None:
        return yolo_codec.ParsedLabels([], [], [xml_member])
    boxes, unsupported, malformed = [], [], []
    for o in root.findall("object"):
        raw = ET.tostring(o, encoding="unicode")[:2000]
        cls = name_to_idx.get((o.findtext("name") or "").strip()[:class_map_mod.MAX_NAME_LEN])
        if cls is None:
            malformed.append(raw)
            continue
        try:
            poly = o.find("polygon")
            if poly is not None:  # this app's own VOC export writes <x1>,<y1>,... in pixels
                pts, i = [], 1
                while poly.find(f"x{i}") is not None and poly.find(f"y{i}") is not None:
                    pts.append([_clamp(float(poly.findtext(f"x{i}")) / W), _clamp(float(poly.findtext(f"y{i}")) / H)])
                    i += 1
                if len(pts) >= 3 and yolo_codec.polygon_area(pts) > 0:
                    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
                    boxes.append({"cls": cls, "kind": "polygon", "points": pts, "x": min(xs), "y": min(ys),
                                  "w": max(xs) - min(xs), "h": max(ys) - min(ys), "raw": raw})
                    continue
            bb = o.find("bndbox")
            x0, y0, x1, y1 = (float(bb.findtext(k)) for k in ("xmin", "ymin", "xmax", "ymax"))
            x0, y0, x1, y1 = _clamp(x0 / W), _clamp(y0 / H), _clamp(x1 / W), _clamp(y1 / H)
            if x1 - x0 <= 0 or y1 - y0 <= 0:
                raise ValueError("empty box")
            boxes.append({"cls": cls, "kind": "bbox", "x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0, "raw": raw})
        except (AttributeError, TypeError, ValueError):
            malformed.append(raw)
    return yolo_codec.ParsedLabels(boxes, unsupported, malformed)


# ---------------------------------------------------------------- choosing and reading

def detect_source(zf: zipfile.ZipFile, names: List[str]) -> str:
    if any(posixpath.basename(n) in ("data.yaml", "data.yml") for n in names):
        return "yolo"
    for m in _json_members(zf, names)[:20]:
        if load_coco_file(zf, m) is not None:
            return "coco"
    for m in [n for n in sorted(names) if ext_of(n) == "xml"][:20]:
        if _read_xml(zf, m) is not None:
            return "voc"
    raise ImportError_("Could not recognise the dataset. Expected a data.yaml (YOLO), a COCO annotation .json, or Pascal VOC .xml files.")


def open_source(zf: zipfile.ZipFile, names: List[str], source_format: str = "auto", label_format: str = "auto") -> dict:
    kind = source_format if source_format in SOURCE_FORMATS else detect_source(zf, names)
    if kind == "yolo":
        return open_yolo(zf, names, label_format)
    if kind == "coco":
        return open_coco(zf, names)
    return open_voc(zf, names)


class LabelReader:
    """Reads one image's labels, whatever the dataset layout. Built from the batch's resolved settings, so the commit
    step (which does not re-detect anything) reads labels exactly like the scan did."""

    def __init__(self, zf: zipfile.ZipFile, batch: dict):
        self.zf = zf
        self.kind = batch.get("resolved_source_format") or "yolo"
        self.label_format = batch.get("resolved_label_format") or "detect"
        names = batch.get("server_names") or []
        self.name_to_idx = {n: i for i, n in enumerate(names)}
        self.coco = CocoLabels(zf, batch.get("coco_cat_map") or {}) if self.kind == "coco" else None

    def read(self, item: dict, size: Optional[Tuple[int, int]]) -> yolo_codec.ParsedLabels:
        if self.kind == "yolo":
            if not item.get("label_path"):
                return EMPTY
            raw = read_capped(self.zf, item["label_path"], MAX_LABEL_BYTES)
            if raw is None:
                return yolo_codec.ParsedLabels([], [], ["<label file too large>"])
            return yolo_codec.parse_label_text(raw.decode("utf-8", errors="replace"), self.label_format, size)
        ref = item.get("label_ref")
        if not ref or not size:
            return EMPTY
        if self.kind == "coco":
            return self.coco.parse(ref["json"], ref["image_id"], size)
        return parse_voc(self.zf, ref["xml"], self.name_to_idx, size)
