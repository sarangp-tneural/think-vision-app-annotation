"""Server class -> project class mapping for dataset import (pure, no db).

The dataset's data.yaml `names` is the authority for the *server's* class indices. The project's class list
is append-only and is never re-sorted; the mapping translates between the two. Many-to-one mappings are
rejected: push-back has to turn a project label back into exactly one server index.
"""
from typing import Dict, List, Optional

MAX_NAME_LEN = 100


def server_names_from_yaml(names) -> List[str]:
    """data.yaml `names` (list, or dict keyed by class index) -> ordered list. Gaps / non-integer keys are errors."""
    if isinstance(names, dict):
        try:
            keys = sorted(int(k) for k in names)
        except (TypeError, ValueError):
            raise ValueError("data.yaml names has non-integer class ids")
        if keys != list(range(len(keys))):
            raise ValueError("data.yaml names has gaps or doesn't start at 0")
        by_int = {int(k): v for k, v in names.items()}
        out = [by_int[i] for i in keys]
    elif isinstance(names, (list, tuple)):
        out = list(names)
    else:
        raise ValueError("data.yaml has no usable `names`")
    if not out:
        raise ValueError("data.yaml names is empty")
    cleaned = []
    for n in out:
        s = str(n).strip()
        if not s:
            raise ValueError("data.yaml has an empty class name")
        cleaned.append(s[:MAX_NAME_LEN])
    return cleaned


def default_mapping(server_names: List[str], project_classes: List[str]) -> List[dict]:
    """Auto-match by exact name; everything else becomes a new (appended) class."""
    existing = set(project_classes)
    return [
        {"server_idx": i, "server_name": n,
         "action": "match" if n in existing else "new", "target": n}
        for i, n in enumerate(server_names)
    ]


def resolve_mapping(server_names: List[str], project_classes: List[str], mapping: Optional[List[dict]],
                    replace_project_classes: bool = False, can_replace: bool = False) -> dict:
    """Validate a user-edited mapping and return
    {"project_classes_after": [...], "class_map": {"0": "person" | None, ...}, "new_classes": [...]}.
    Missing entries fall back to the default; raises ValueError with a user-facing message."""
    n = len(server_names)
    if replace_project_classes:
        if not can_replace:
            raise ValueError("Project classes can only be replaced while no image has annotations")
        # project index == server index, nothing skipped (a skipped class would still need a slot)
        return {"project_classes_after": list(server_names),
                "class_map": {str(i): server_names[i] for i in range(n)},
                "new_classes": [c for c in server_names if c not in project_classes]}

    base = {m["server_idx"]: m for m in default_mapping(server_names, project_classes)}
    for m in mapping or []:
        try:
            idx = int(m.get("server_idx"))
        except (TypeError, ValueError):
            raise ValueError("Mapping entry without a valid server_idx")
        if not 0 <= idx < n:
            raise ValueError(f"Mapping refers to unknown server class {idx}")
        base[idx] = {"server_idx": idx, "server_name": server_names[idx],
                     "action": m.get("action"), "target": m.get("target")}

    class_map: Dict[str, Optional[str]] = {}
    new_classes: List[str] = []
    for idx in range(n):
        m = base[idx]
        action = m["action"]
        target = str(m.get("target") if m.get("target") is not None else server_names[idx]).strip()
        if action == "skip":
            class_map[str(idx)] = None
        elif action == "match":
            if target not in project_classes:
                raise ValueError(f'Server class "{server_names[idx]}" is mapped to "{target}", which is not a project class')
            class_map[str(idx)] = target
        elif action == "new":
            if not target or len(target) > MAX_NAME_LEN:
                raise ValueError(f'Invalid new class name for server class "{server_names[idx]}"')
            if target in project_classes:
                raise ValueError(f'"{target}" already exists in the project: map "{server_names[idx]}" to it instead of creating it')
            if target in new_classes:
                raise ValueError(f'Two server classes would create the new class "{target}"')
            new_classes.append(target)
            class_map[str(idx)] = target
        else:
            raise ValueError(f'Unknown mapping action "{action}" for server class "{server_names[idx]}"')

    seen: Dict[str, int] = {}
    for idx in range(n):
        label = class_map[str(idx)]
        if label is None:
            continue
        if label in seen:
            raise ValueError(
                f'Server classes "{server_names[seen[label]]}" and "{server_names[idx]}" both map to "{label}". '
                "Each server class needs its own project class so labels can be written back to the right index."
            )
        seen[label] = idx
    return {"project_classes_after": list(project_classes) + new_classes, "class_map": class_map, "new_classes": new_classes}
