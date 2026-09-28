"""Unit tests for pipeline_logic.py: pure module, no live server, no SSH, no
mocking needed (mirrors test_ssh_helper.py's "pure module" precedent, not
test_deployment_pipeline.py's live-HTTP one - nothing here needs a running
backend or MongoDB).
"""
import os

import pipeline_logic as pl


# --- diff_classes() ---------------------------------------------------------

def test_diff_classes_exact_match():
    result = pl.diff_classes(["car", "truck"], "names: ['car', 'truck']\nnc: 2\n")
    assert result["match"] is True
    assert result["diff"] == {"missing_in_remote": [], "missing_in_local": [], "order_mismatch": False}


def test_diff_classes_same_set_different_order():
    result = pl.diff_classes(["car", "truck"], "names: ['truck', 'car']\nnc: 2\n")
    assert result["match"] is False
    assert result["diff"]["order_mismatch"] is True
    assert result["diff"]["missing_in_remote"] == []
    assert result["diff"]["missing_in_local"] == []


def test_diff_classes_missing_class_in_remote():
    result = pl.diff_classes(["car", "truck", "bus"], "names: ['car', 'truck']\nnc: 2\n")
    assert result["match"] is False
    assert result["diff"]["missing_in_remote"] == ["bus"]
    assert result["diff"]["missing_in_local"] == []


def test_diff_classes_extra_class_in_remote():
    result = pl.diff_classes(["car", "truck"], "names: ['car', 'truck', 'bus']\nnc: 3\n")
    assert result["match"] is False
    assert result["diff"]["missing_in_local"] == ["bus"]
    assert result["diff"]["missing_in_remote"] == []


def test_diff_classes_handles_dict_style_names():
    result = pl.diff_classes(["car", "truck"], "names:\n  0: car\n  1: truck\nnc: 2\n")
    assert result["match"] is True
    assert result["remote_classes"] == ["car", "truck"]


# --- validate_dataset_dir() -------------------------------------------------

def _write_split(root, split, pairs, nc, bad_line=None):
    images_dir = os.path.join(root, split, "images")
    labels_dir = os.path.join(root, split, "labels")
    os.makedirs(images_dir, exist_ok=True)
    os.makedirs(labels_dir, exist_ok=True)
    for name in pairs:
        open(os.path.join(images_dir, f"{name}.jpg"), "w").close()
        line = bad_line if bad_line else "0 0.5 0.5 0.2 0.2"
        with open(os.path.join(labels_dir, f"{name}.txt"), "w") as f:
            f.write(line + "\n")


def test_validate_dataset_dir_valid(tmp_path):
    root = str(tmp_path)
    for split in pl.SPLITS:
        _write_split(root, split, ["img1", "img2"], nc=2)
    assert pl.validate_dataset_dir(root, nc=2) == []


def test_validate_dataset_dir_missing_label(tmp_path):
    root = str(tmp_path)
    for split in pl.SPLITS:
        os.makedirs(os.path.join(root, split, "images"), exist_ok=True)
        os.makedirs(os.path.join(root, split, "labels"), exist_ok=True)
    open(os.path.join(root, "train", "images", "orphan.jpg"), "w").close()

    errors = pl.validate_dataset_dir(root, nc=2)
    assert any("missing label for image: orphan" in e for e in errors)


def test_validate_dataset_dir_missing_image(tmp_path):
    root = str(tmp_path)
    for split in pl.SPLITS:
        os.makedirs(os.path.join(root, split, "images"), exist_ok=True)
        os.makedirs(os.path.join(root, split, "labels"), exist_ok=True)
    open(os.path.join(root, "train", "labels", "orphan.txt"), "w").close()

    errors = pl.validate_dataset_dir(root, nc=2)
    assert any("missing image for label: orphan" in e for e in errors)


def test_validate_dataset_dir_class_id_out_of_range(tmp_path):
    root = str(tmp_path)
    for split in pl.SPLITS:
        _write_split(root, split, ["img1"], nc=2, bad_line="5 0.5 0.5 0.2 0.2")

    errors = pl.validate_dataset_dir(root, nc=2)
    assert any("class_id 5 out of range (nc=2)" in e for e in errors)


def test_validate_dataset_dir_wrong_field_count(tmp_path):
    root = str(tmp_path)
    for split in pl.SPLITS:
        _write_split(root, split, ["img1"], nc=2, bad_line="0 0.5 0.5 0.2")

    errors = pl.validate_dataset_dir(root, nc=2)
    assert any("expected 5 values, got 4" in e for e in errors)


# --- decide_deploy() ---------------------------------------------------------

def test_decide_deploy_deploys_when_retrained_better_or_equal():
    assert pl.decide_deploy(0.5, 0.5) is True
    assert pl.decide_deploy(0.5, 0.6) is True


def test_decide_deploy_rejects_when_retrained_worse():
    assert pl.decide_deploy(0.6, 0.5) is False


# --- render_train_eval_script() ---------------------------------------------

def test_render_train_eval_script_embeds_params():
    script = pl.render_train_eval_script({
        "checkpoint_path": "/models/base.pt",
        "data_yaml_path": "/data/data.yaml",
        "epochs": 10,
        "project_dir": "/runs",
        "run_name": "client1_retrained",
        "metrics_json_path": "/runs/metrics.json",
    })
    compile(script, "<generated>", "exec")  # must be valid Python
    assert "/models/base.pt" in script
    assert "/data/data.yaml" in script
    assert "epochs=10" in script
    assert "/runs/client1_retrained/weights/best.pt" in script
    assert "/runs/metrics.json" in script
    assert 'split="val"' in script
