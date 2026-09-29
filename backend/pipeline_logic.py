"""Ported/new ML-ops logic for the Deploy Pipeline feature.

Replaces cicd-pipeline-main/'s subprocess/Docker/CI orchestration with
in-process, importable functions - the validate/train/eval/decide *logic* is
kept, the git-push/CI *trigger* is not. No FastAPI/Mongo/SSH imports here:
this module is pure data/ML logic, orchestration lives in routers/deployment.py
and (from M4 onward) deployment_worker.py.
"""
import os

import yaml

# Shared with backend/server.py's _build_split_dataset_dir, so the writer (M2)
# and this module's validator can't silently drift on directory naming.
SPLITS = ("train", "valid", "test")

_LABEL_EXTS = (".jpg", ".jpeg", ".png")


def _validate_split_pairing(dataset_dir: str, split: str) -> list:
    """Ported from cicd-pipeline-main/validate_data.py's validate_split."""
    images_dir = os.path.join(dataset_dir, split, "images")
    labels_dir = os.path.join(dataset_dir, split, "labels")
    if not os.path.exists(images_dir):
        return [f"[{split}] images dir not found: {images_dir}"]
    if not os.path.exists(labels_dir):
        return [f"[{split}] labels dir not found: {labels_dir}"]

    image_files = {os.path.splitext(f)[0] for f in os.listdir(images_dir) if f.lower().endswith(_LABEL_EXTS)}
    label_files = {os.path.splitext(f)[0] for f in os.listdir(labels_dir) if f.endswith(".txt")}

    errors = []
    for name in image_files - label_files:
        errors.append(f"[{split}] missing label for image: {name}")
    for name in label_files - image_files:
        errors.append(f"[{split}] missing image for label: {name}")
    return errors


def _validate_split_labels(dataset_dir: str, split: str, nc: int) -> list:
    """Ported from cicd-pipeline-main/validate_data.py's validate_labels."""
    labels_dir = os.path.join(dataset_dir, split, "labels")
    if not os.path.exists(labels_dir):
        return []

    errors = []
    for fname in os.listdir(labels_dir):
        if not fname.endswith(".txt"):
            continue
        with open(os.path.join(labels_dir, fname)) as f:
            lines = f.readlines()
        for i, line in enumerate(lines):
            parts = line.strip().split()
            if not parts:
                continue
            if len(parts) != 5:
                errors.append(f"[{split}] {fname} line {i + 1}: expected 5 values, got {len(parts)}")
                continue
            class_id = int(parts[0])
            if class_id < 0 or class_id >= nc:
                errors.append(f"[{split}] {fname} line {i + 1}: class_id {class_id} out of range (nc={nc})")
    return errors


def validate_dataset_dir(dataset_dir: str, nc: int) -> list:
    """Validates a train/valid/test split directory (the shape
    _build_split_dataset_dir writes): image/label pairing and in-range class
    ids per split. Ported from cicd-pipeline-main/validate_data.py, minus its
    sanitize_data_yaml step (that existed only to patch Roboflow's buggy
    relative paths in a pre-existing data.yaml; nc is now passed explicitly
    instead of read out of one)."""
    errors = []
    for split in SPLITS:
        errors += _validate_split_pairing(dataset_dir, split)
        errors += _validate_split_labels(dataset_dir, split, nc)
    return errors


def _normalize_names(names) -> list:
    """A data.yaml's `names` can be a list (what this app's own writers
    produce) or a dict keyed by class index (some other tool's export,
    including a client's pre-existing remote data.yaml this app never wrote).
    Normalize both to an ordered list."""
    if isinstance(names, dict):
        return [names[k] for k in sorted(names.keys(), key=int)]
    return list(names or [])


def diff_classes(local_classes: list, remote_yaml_text: str) -> dict:
    """New logic (not ported): compares this project's class list against a
    remote data.yaml's `names`. `match` requires exact set AND order equality
    (order encodes class index, which training depends on) - `diff` still
    reports the specific discrepancy even when match is True (empty) so a
    same-set-different-order case is distinguishable from a missing/extra
    class case."""
    remote_config = yaml.safe_load(remote_yaml_text) or {}
    remote_classes = _normalize_names(remote_config.get("names"))

    local_set, remote_set = set(local_classes), set(remote_classes)
    missing_in_remote = [c for c in local_classes if c not in remote_set]
    missing_in_local = [c for c in remote_classes if c not in local_set]
    order_mismatch = (
        not missing_in_remote and not missing_in_local and local_classes != remote_classes
    )
    match = not missing_in_remote and not missing_in_local and not order_mismatch

    return {
        "local_classes": list(local_classes),
        "remote_classes": remote_classes,
        "match": match,
        "diff": {
            "missing_in_remote": missing_in_remote,
            "missing_in_local": missing_in_local,
            "order_mismatch": order_mismatch,
        },
    }


_TRAIN_HYPERPARAMS = {
    # Ported verbatim from cicd-pipeline-main/train.py's model.train() call.
    # No "device" key - ultralytics auto-selects CUDA on the remote host if
    # available, falling back to CPU; hardcoding "cpu" here previously
    # ignored the remote machine's own GPU regardless of what it had.
    "patience": 50, "imgsz": 640, "batch": 4, "workers": 2,
    "cache": False, "exist_ok": True, "pretrained": True, "seed": 42,
    "deterministic": True, "amp": False, "optimizer": "SGD", "lr0": 0.01,
    "lrf": 0.01, "momentum": 0.937, "weight_decay": 0.0005, "warmup_epochs": 3,
    "warmup_momentum": 0.8, "warmup_bias_lr": 0.1, "cos_lr": True,
    "close_mosaic": 20, "box": 7.5, "cls": 0.5, "dfl": 1.5, "hsv_h": 0.015,
    "hsv_s": 0.7, "hsv_v": 0.4, "degrees": 5, "translate": 0.1, "scale": 0.5,
    "shear": 2, "fliplr": 0.5, "mosaic": 1.0, "mixup": 0.20, "copy_paste": 0.60,
    "erasing": 0.4, "val": True, "plots": True, "save": True, "verbose": True,
}


def render_train_eval_script(params: dict) -> str:
    """Renders a standalone Python script, executed on the remote host over
    SSH (M5) - it can't call back into this repo's own code, so this text is
    the only place train.py's hyperparameters and eval.py's model.val() call
    actually meet. Trains from params['checkpoint_path'], then evaluates on
    the `val` split right after training (the "did training itself work"
    check - distinct from evaluate()'s later `test`-split call, below, used
    for the human-facing candidate-vs-baseline comparison in M7) and writes
    {map50, map50_95, precision, recall} to params['metrics_json_path'].

    Required params: checkpoint_path, data_yaml_path, epochs, project_dir,
    run_name, metrics_json_path.
    """
    train_kwargs = dict(_TRAIN_HYPERPARAMS)
    train_kwargs.update({
        "data": params["data_yaml_path"],
        "epochs": params["epochs"],
        "project": params["project_dir"],
        "name": params["run_name"],
    })
    train_kwargs_src = ",\n    ".join(f"{k}={v!r}" for k, v in train_kwargs.items())
    checkpoint_path = params["checkpoint_path"]
    data_yaml_path = params["data_yaml_path"]
    metrics_json_path = params["metrics_json_path"]
    best_pt_path = f'{params["project_dir"]}/{params["run_name"]}/weights/best.pt'

    return f'''\
import json
from ultralytics import YOLO

model = YOLO({checkpoint_path!r})
model.train(
    {train_kwargs_src},
)

eval_model = YOLO({best_pt_path!r})
results = eval_model.val(data={data_yaml_path!r}, split="val", verbose=False)
metrics = {{
    "map50": float(results.box.map50),
    "map50_95": float(results.box.map),
    "precision": float(results.box.mp),
    "recall": float(results.box.mr),
}}
with open({metrics_json_path!r}, "w") as f:
    json.dump(metrics, f)
'''


def evaluate(weights_path: str, data_yaml: str, split: str) -> dict:
    """Local equivalent of eval.py/baseline_eval.py's model.val() call,
    generalized to take `split` as a real parameter instead of hardcoding
    "val" - called from M7's deployment_worker.py against the pipeline's own
    held-out `test` split, for both the candidate and the currently-active
    (baseline) model. Adds precision/recall (results.box.mp/.mr), which
    neither ported script extracted."""
    from ultralytics import YOLO

    model = YOLO(weights_path)
    results = model.val(data=data_yaml, split=split, verbose=False)
    return {
        "mAP50": float(results.box.map50),
        "mAP50_95": float(results.box.map),
        "precision": float(results.box.mp),
        "recall": float(results.box.mr),
    }


def decide_deploy(baseline_map50: float, retrained_map50: float) -> bool:
    """Ported verbatim from decide.py's gate (line 30): a bare >=, no margin.
    Everything else in decide.py's decide() (copying the deployed weights,
    writing a diagnostic report, sys.exit) is CLI/file-I/O plumbing, left out
    of this pure comparison on purpose."""
    return retrained_map50 >= baseline_map50
