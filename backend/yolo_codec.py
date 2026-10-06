"""Pure YOLO label conversion (no db, no I/O) shared by import and push-back.

Formats (`label_format`):
  detect  `cls cx cy w h`                      -> bbox
  seg     `cls x1 y1 x2 y2 ... xn yn` (n >= 3) -> polygon  (5-token lines are still read as boxes)
  obb     `cls x1 y1 ... x4 y4` (9 tokens)     -> bbox + rotation   (5-token lines are still read as boxes)
A 9-token line is both a 4-point polygon and an OBB, so the format must be stated (or the dataset has to make it
unambiguous); `resolve_format` does that. Lines that fit the chosen format but not any other are `unsupported`;
lines that are not labels at all are `malformed`. Neither is ever dropped on push-back (callers keep the raw lines).

App annotation format: normalized 0-1, `x`/`y` = TOP-LEFT of the box (not the centre), `w`/`h` size, polygons as
`points`, rotated boxes as an axis-aligned x/y/w/h plus `rotation` in radians, clockwise on screen (y down), applied
about the box centre IN PIXEL SPACE (that is how the annotator draws it, `rotate(<rad>rad)`), so converting an
oriented box needs the image size.

`canonical_hash` is the label fingerprint used to detect "labels edited since import": it is computed from
parsed values (server class index + coordinates rounded to 6 dp, sorted), never from raw file bytes, so
float formatting differences between our writer and the original file cannot cause false diffs.
"""
import hashlib
import math
from typing import Dict, Iterable, List, NamedTuple, Optional, Tuple

DETECT_TOKENS = 5
_EPS = 1e-6  # tolerance for values that overshoot [0, 1] by float noise


class ParsedLabels(NamedTuple):
    boxes: List[dict]          # {"cls": int, "x","y","w","h", "raw"}  (top-left, normalized; raw = original line)
    unsupported: List[str]     # raw lines in a format we don't handle yet (seg / OBB)
    malformed: List[str]       # raw lines that are not valid YOLO at all


def parse_detect_line(line: str) -> Optional[dict]:
    """One detect line -> {"cls","x","y","w","h"} (top-left, clamped to [0, 1]); raises ValueError if invalid."""
    parts = line.split()
    if len(parts) != DETECT_TOKENS:
        raise ValueError(f"expected {DETECT_TOKENS} tokens, got {len(parts)}")
    try:
        cls = int(parts[0])
    except ValueError:
        # "0.0" style class ids appear in some exporters
        f = float(parts[0])
        if f != int(f):
            raise ValueError("class id is not an integer")
        cls = int(f)
    if cls < 0:
        raise ValueError("negative class id")
    cx, cy, w, h = (float(p) for p in parts[1:])
    for v in (cx, cy, w, h):
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError("non-finite value")
    if w <= 0 or h <= 0:
        raise ValueError("non-positive size")
    if not (-_EPS <= cx <= 1 + _EPS and -_EPS <= cy <= 1 + _EPS and w <= 1 + _EPS and h <= 1 + _EPS):
        raise ValueError("coordinates outside 0-1 (not normalized)")
    x0, y0 = cx - w / 2, cy - h / 2
    x1, y1 = cx + w / 2, cy + h / 2
    x0, y0, x1, y1 = (min(1.0, max(0.0, v)) for v in (x0, y0, x1, y1))
    if x1 - x0 <= 0 or y1 - y0 <= 0:
        raise ValueError("box is empty after clamping")
    return {"cls": cls, "x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}


def _is_numeric_row(parts: List[str]) -> bool:
    try:
        for p in parts:
            float(p)
    except ValueError:
        return False
    return True


# ---------- geometry ----------

def _clamp01(v: float) -> float:
    return min(1.0, max(0.0, v))


def obb_from_corners(corners: List[Tuple[float, float]], width: int, height: int, tol: float = 0.02) -> Optional[dict]:
    """Four normalized corners (in order around the rectangle, either direction, any start) -> app box
    {"x","y","w","h","rotation"}, or None if they do not form a rectangle. Done in pixel space, because the annotator
    rotates the pixel-space box about its centre."""
    if not (width and height) or len(corners) != 4:
        return None
    pts = [(x * width, y * height) for x, y in corners]
    e = [(pts[(i + 1) % 4][0] - pts[i][0], pts[(i + 1) % 4][1] - pts[i][1]) for i in range(4)]
    w_px, h_px = math.hypot(*e[0]), math.hypot(*e[1])
    if w_px <= 0 or h_px <= 0:
        return None
    if abs(e[0][0] * e[1][0] + e[0][1] * e[1][1]) / (w_px * h_px) > tol:  # adjacent edges must be perpendicular
        return None
    if abs(math.hypot(*e[2]) - w_px) > tol * w_px or abs(math.hypot(*e[3]) - h_px) > tol * h_px:  # opposite edges equal
        return None
    cx = sum(p[0] for p in pts) / 4
    cy = sum(p[1] for p in pts) / 4
    return {"x": (cx - w_px / 2) / width, "y": (cy - h_px / 2) / height, "w": w_px / width, "h": h_px / height,
            "rotation": math.atan2(e[0][1], e[0][0])}


def obb_to_corners(x: float, y: float, w: float, h: float, rotation: float, width: int, height: int) -> List[Tuple[float, float]]:
    """App box (top-left x/y, size, rotation about the centre in pixel space) -> 4 normalized corners, clockwise from
    the box's own top-left; clamped to the image."""
    cx, cy = (x + w / 2) * width, (y + h / 2) * height
    hw, hh = w * width / 2, h * height / 2
    c, s = math.cos(rotation or 0.0), math.sin(rotation or 0.0)
    out = []
    for lx, ly in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)):
        out.append((_clamp01((cx + lx * c - ly * s) / width), _clamp01((cy + lx * s + ly * c) / height)))
    return out


def canonical_obb(cx: float, cy: float, w: float, h: float, theta: float,
                  size: Optional[Tuple[int, int]] = None) -> Tuple[float, float, float, float, float]:
    """A rotated rectangle has several equivalent descriptions (which edge is the width, theta vs theta+pi). Pick one
    so that the same rectangle always hashes the same, however the file happened to list its corners. The comparison
    is done in pixel units: on a non-square image, swapping the two axes is not a swap of the normalized w and h."""
    W, H = size or (1, 1)
    wp, hp = w * W, h * H
    if hp > wp + 1e-9:
        wp, hp, theta = hp, wp, theta + math.pi / 2
    period = math.pi / 2 if abs(wp - hp) < 1e-6 else math.pi
    theta = theta % period
    if round(theta, 6) == round(period, 6):
        theta = 0.0
    return (round(cx, 6), round(cy, 6), round(wp, 3), round(hp, 3), round(theta, 6))


def polygon_area(points: List[List[float]]) -> float:
    return abs(sum(points[i][0] * points[(i + 1) % len(points)][1] - points[(i + 1) % len(points)][0] * points[i][1]
                   for i in range(len(points)))) / 2


# ---------- parsing ----------

def _poly_points(parts: List[str]) -> List[List[float]]:
    vals = [float(p) for p in parts[1:]]
    if any(v != v or v in (float("inf"), float("-inf")) for v in vals):
        raise ValueError("non-finite value")
    if any(v < -_EPS or v > 1 + _EPS for v in vals):
        raise ValueError("coordinates outside 0-1 (not normalized)")
    pts = [[_clamp01(vals[i]), _clamp01(vals[i + 1])] for i in range(0, len(vals), 2)]
    if len(pts) < 3 or polygon_area(pts) <= 0:
        raise ValueError("degenerate polygon")
    return pts


def parse_poly_line(line: str, label_format: str, size: Optional[Tuple[int, int]]) -> dict:
    """A >5-token line under seg/obb rules -> {"cls","kind",...,"raw"}; raises ValueError if it is not valid."""
    parts = line.split()
    cls = int(float(parts[0]))
    if cls < 0 or float(parts[0]) != cls:
        raise ValueError("bad class id")
    if len(parts) % 2 == 0:
        raise ValueError("odd number of coordinates")
    if label_format == "obb":
        if len(parts) != 9:
            raise ValueError("an oriented box has exactly 4 corners")
        if not size:
            raise ValueError("image size needed for oriented boxes")
        pts = _poly_points(parts)
        box = obb_from_corners([tuple(p) for p in pts], size[0], size[1])
        if not box:
            raise ValueError("not a rectangle")
        return {"cls": cls, "kind": "obb", **box, "raw": line}
    pts = _poly_points(parts)
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return {"cls": cls, "kind": "polygon", "points": pts, "x": min(xs), "y": min(ys),
            "w": max(xs) - min(xs), "h": max(ys) - min(ys), "raw": line}


def parse_label_text(text: str, label_format: str = "detect", size: Optional[Tuple[int, int]] = None) -> ParsedLabels:
    """Split a label file into boxes/polygons/oriented boxes, lines this format cannot represent, and garbage.
    `size` = (width, height) of the image, needed to read oriented boxes."""
    boxes: List[dict] = []
    unsupported: List[str] = []
    malformed: List[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) == DETECT_TOKENS:
            try:
                boxes.append({**parse_detect_line(line), "kind": "bbox", "raw": line})
            except ValueError:
                malformed.append(line)
            continue
        if len(parts) < DETECT_TOKENS or not _is_numeric_row(parts):
            malformed.append(line)
            continue
        if label_format not in ("seg", "obb"):
            unsupported.append(line)  # e.g. a polygon line in a dataset read as plain boxes
            continue
        if label_format == "obb" and len(parts) != 9:
            unsupported.append(line)
            continue
        try:
            boxes.append(parse_poly_line(line, label_format, size))
        except ValueError:
            malformed.append(line)
    return ParsedLabels(boxes, unsupported, malformed)


def resolve_format(token_counts: Iterable[int], requested: str = "auto") -> Tuple[Optional[str], Optional[str]]:
    """The dataset's label format from the token counts of all its label lines: (format, problem).
    An explicit `requested` format wins. `auto` says detect when every line has 5 tokens, seg when some polygon has
    a point count an oriented box cannot have, and otherwise (all polygons have exactly 4 points) asks."""
    if requested in ("detect", "seg", "obb"):
        return requested, None
    counts = set(token_counts)
    longer = {c for c in counts if c > DETECT_TOKENS}
    if not longer:
        return "detect", None
    if any(c != 9 for c in longer):
        return "seg", None
    return None, ("The labels have 9 values per line, which is either segmentation (4-point polygons) or OBB "
                  "(oriented boxes), and the files alone cannot tell which. Choose the label format and import again.")


def format_detect_line(cls: int, x: float, y: float, w: float, h: float) -> str:
    """App box (top-left x/y) -> YOLO detect line (centre format)."""
    return f"{cls} {x + w / 2:.6f} {y + h / 2:.6f} {w:.6f} {h:.6f}"


def _canonical_line(cls: int, x: float, y: float, w: float, h: float) -> str:
    return f"{int(cls)} {round(x + w / 2, 6):.6f} {round(y + h / 2, 6):.6f} {round(w, 6):.6f} {round(h, 6):.6f}"


def _item_line(it, size: Optional[Tuple[int, int]] = None) -> str:
    """Canonical text of one shape. Boxes keep the original (pre-segmentation) format so that hashes stored by earlier
    imports stay valid; polygons and oriented boxes get their own prefixes."""
    if isinstance(it, tuple):
        return _canonical_line(*it)
    kind = it.get("kind", "bbox")
    if kind == "polygon":
        return f"s {int(it['cls'])} " + " ".join(f"{round(x, 6):.6f} {round(y, 6):.6f}" for x, y in it["points"])
    if kind == "obb":
        cx, cy, w, h, th = canonical_obb(it["x"] + it["w"] / 2, it["y"] + it["h"] / 2, it["w"], it["h"], it.get("rotation", 0.0), size)
        return f"o {int(it['cls'])} {cx:.6f} {cy:.6f} {w:.3f} {h:.3f} {th:.6f}"
    return _canonical_line(it["cls"], it["x"], it["y"], it["w"], it["h"])


def canonical_hash(items: Iterable, size: Optional[Tuple[int, int]] = None) -> str:
    """sha256 over the sorted canonical lines of the shapes. An item is (server_class_idx, x, y, w, h) with TOP-LEFT
    x/y (a box), or a dict {"kind": "bbox"|"polygon"|"obb", "cls", ...}. `size` (width, height) is needed for
    oriented boxes."""
    return hashlib.sha256("\n".join(sorted(_item_line(i, size) for i in items)).encode()).hexdigest()


def boxes_to_canonical_items(boxes: Iterable[dict], label_to_idx: Dict[str, int]) -> List[Tuple[int, float, float, float, float]]:
    """Current app annotations -> canonical items, translating label -> server index. Boxes whose label has no
    server index (or that are not plain axis-aligned boxes) are ignored."""
    items = []
    for b in boxes:
        idx = label_to_idx.get(b.get("label"))
        if idx is None or b.get("type", "bbox") != "bbox" or b.get("rotation"):
            continue
        items.append((idx, float(b.get("x", 0)), float(b.get("y", 0)), float(b.get("w", 0)), float(b.get("h", 0))))
    return items


def box_to_annotation(b: dict, label: str) -> dict:
    """A parsed label shape ({"kind", "cls", ...}) -> an app annotation for `label`."""
    kind = b.get("kind", "bbox")
    if kind == "polygon":
        return {"type": "polygon", "label": label, "points": [list(p) for p in b["points"]]}
    box = {"type": "bbox", "label": label, "x": b["x"], "y": b["y"], "w": b["w"], "h": b["h"]}
    if kind == "obb":
        box["rotation"] = b["rotation"]
    return box


def _rect_polygon(x: float, y: float, w: float, h: float, rotation: float, size: Optional[Tuple[int, int]]) -> List[List[float]]:
    if rotation and size:
        return [list(c) for c in obb_to_corners(x, y, w, h, rotation, size[0], size[1])]
    return [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]


def _ellipse_polygon(cx: float, cy: float, rx: float, ry: float, n: int = 24) -> List[List[float]]:
    return [[_clamp01(cx + rx * math.cos(2 * math.pi * i / n)), _clamp01(cy + ry * math.sin(2 * math.pi * i / n))] for i in range(n)]


def annotations_to_items(annotations: Iterable[dict], label_to_idx: Dict[str, int], label_format: str = "detect",
                         size: Optional[Tuple[int, int]] = None):
    """App annotations -> (items, unknown_labels, lossy_count) for a label file of `label_format`.
    detect: every shape becomes its bounding box. seg: shapes become polygons (a box is a 4-point polygon, an
    ellipse a 24-gon). obb: shapes become oriented boxes (polygons/ellipses fall back to their bounding box, unrotated).
    `lossy_count` counts the shapes that lose information that way. Labels without a server index are returned in
    `unknown_labels` (sorted); empty shapes are dropped. Items feed canonical_hash and items_to_lines."""
    items: List = []
    unknown = set()
    lossy = 0
    for a in annotations:
        label = a.get("label")
        idx = label_to_idx.get(label)
        if idx is None:
            unknown.add(label)
            continue
        t = a.get("type", "bbox")
        pts = a.get("points") or []
        if t in ("polygon", "polyline") and len(pts) >= 3:
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            bx, by, bw, bh = min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)
            shape = "polygon"
            lossy += 1 if (t == "polyline" or label_format != "seg") else 0
        elif t == "ellipse":
            bx, by, bw, bh = a.get("x", 0) - a.get("rx", 0), a.get("y", 0) - a.get("ry", 0), 2 * a.get("rx", 0), 2 * a.get("ry", 0)
            shape = "ellipse"
            lossy += 1
        elif t == "bbox" or t is None:
            bx, by, bw, bh = a.get("x", 0), a.get("y", 0), a.get("w", 0), a.get("h", 0)
            shape = "bbox"
            if a.get("rotation") and label_format != "obb" and not (label_format == "seg" and size):
                lossy += 1
        else:  # point, polyline with < 3 points, ...
            lossy += 1
            continue
        if bw <= 0 or bh <= 0:
            continue
        rot = float(a.get("rotation") or 0.0) if shape == "bbox" else 0.0
        if label_format == "seg":
            if shape == "polygon":
                points = [[float(p[0]), float(p[1])] for p in pts]
            elif shape == "ellipse":
                points = _ellipse_polygon(a.get("x", 0), a.get("y", 0), a.get("rx", 0), a.get("ry", 0))
            else:
                points = _rect_polygon(float(bx), float(by), float(bw), float(bh), rot, size)
            items.append({"kind": "polygon", "cls": idx, "points": points})
        elif label_format == "obb":
            items.append({"kind": "obb", "cls": idx, "x": float(bx), "y": float(by), "w": float(bw), "h": float(bh), "rotation": rot})
        else:
            items.append((idx, float(bx), float(by), float(bw), float(bh)))
    return items, sorted(u for u in unknown if u is not None), lossy


def item_to_line(it, size: Optional[Tuple[int, int]] = None) -> str:
    """One shape -> one label-file line (6 decimals)."""
    if isinstance(it, tuple):
        return format_detect_line(*it)
    kind = it.get("kind", "bbox")
    if kind == "polygon":
        return f"{it['cls']} " + " ".join(f"{_clamp01(x):.6f} {_clamp01(y):.6f}" for x, y in it["points"])
    if kind == "obb":
        if not size:
            raise ValueError("image size needed to write oriented boxes")
        corners = obb_to_corners(it["x"], it["y"], it["w"], it["h"], it.get("rotation", 0.0), size[0], size[1])
        return f"{it['cls']} " + " ".join(f"{x:.6f} {y:.6f}" for x, y in corners)
    return format_detect_line(it["cls"], it["x"], it["y"], it["w"], it["h"])


# Registry of parsers by dataset format: name -> parser(text, label_format, size) -> ParsedLabels
FORMATS = {"detect": parse_label_text, "seg": parse_label_text, "obb": parse_label_text}
