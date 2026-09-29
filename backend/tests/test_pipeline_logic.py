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


def test_resolve_yaml_split_dirs_relative_path_and_test_fallback():
    cfg = {"path": "../ds", "train": "train/images", "val": "valid/images"}
    dirs = pl.resolve_yaml_split_dirs(cfg, "/srv/work/sub")
    assert dirs["train"] == ("/srv/work/ds/train/images", "/srv/work/ds/train/labels")
    assert dirs["valid"] == ("/srv/work/ds/valid/images", "/srv/work/ds/valid/labels")
    # no test key -> falls back to val
    assert dirs["test"] == dirs["valid"]


def test_resolve_yaml_split_dirs_absolute_and_list_values():
    cfg = {"train": ["/data/a/images", "/data/b/images"], "val": "/data/v/images"}
    dirs = pl.resolve_yaml_split_dirs(cfg, "/srv/work")
    assert dirs["train"] == ("/data/a/images", "/data/a/labels")


def test_names_as_list_handles_dict_names():
    assert pl.names_as_list({"names": {1: "b", 0: "a"}}) == ["a", "b"]


def test_deployment_schemas_validate_env_and_dataset_mode():
    import pytest
    from pydantic import ValidationError
    from schemas.deployment import TrainingRemoteRequest, UploadingDataRequest

    base = dict(host="h", username="u", remote_workdir="/w")
    with pytest.raises(ValidationError):
        TrainingRemoteRequest(**base, remote_base_model_path="/m.pt", env_mode="existing")
    assert TrainingRemoteRequest(
        **base, remote_base_model_path="/m.pt", env_mode="existing", venv_path="/v"
    ).env_mode == "existing"
    with pytest.raises(ValidationError):
        UploadingDataRequest(**base, dataset_mode="merge")
    assert UploadingDataRequest(**base).dataset_mode == "new"


def test_effective_yaml_root_falls_back_when_absolute_path_missing():
    cfg = {"path": "/tmp/pipeline_upload_x", "train": "train/images", "val": "valid/images"}
    assert pl.effective_yaml_root(cfg, "/srv/ds", path_exists=lambda p: False) == "/srv/ds"
    assert pl.effective_yaml_root(cfg, "/srv/ds", path_exists=lambda p: True) == "/tmp/pipeline_upload_x"
    dirs = pl.resolve_yaml_split_dirs(cfg, "/srv/ds", path_exists=lambda p: False)
    assert dirs["train"][0] == "/srv/ds/train/images"


def test_resolve_yaml_split_dirs_swaps_images_segment_and_roboflow_relative():
    dirs = pl.resolve_yaml_split_dirs({"train": "images/train", "val": "images/val"}, "/d")
    assert dirs["train"] == ("/d/images/train", "/d/labels/train")
    rf = pl.resolve_yaml_split_dirs({"train": "../train/images", "val": "../valid/images"}, "/d/proj")
    assert rf["train"] == ("/d/train/images", "/d/train/labels")


def test_looks_like_dataset_yaml():
    assert pl.looks_like_dataset_yaml({"names": ["a"], "train": "t"})
    assert not pl.looks_like_dataset_yaml({"task": "detect", "data": "x"})  # ultralytics args.yaml
    assert not pl.looks_like_dataset_yaml(None)


def test_normalize_venv_dir():
    for raw in ("/v", "/v/", " /v ", "/v/bin/activate", "/v/bin/python", "/v/bin/python3", "/v/bin"):
        assert pl.normalize_venv_dir(raw) == "/v", raw
    assert pl.normalize_venv_dir("") == ""


def test_sanitize_hyperparams():
    import pytest

    assert pl.sanitize_hyperparams({"batch": "8", "amp": "false", "device": "0,1", "lr0": "", "optimizer": "SGD"}) == {
        "batch": 8, "amp": False, "device": [0, 1], "optimizer": "SGD",
    }
    assert pl.sanitize_hyperparams({"device": "cpu"}) == {"device": "cpu"}
    for bad in ({"model": "x"}, {"data": "y"}, {"batch": "abc"}, {"optimizer": "evil()"}, {"device": "0; rm"}):
        with pytest.raises(ValueError):
            pl.sanitize_hyperparams(bad)


def test_render_script_uses_recipe_defaults_and_user_overrides():
    params = {
        "checkpoint_path": "m.pt", "data_yaml_path": "/d/data.yaml", "epochs": 30,
        "project_dir": "/p/runs", "run_name": "r", "metrics_json_path": "/p/m.json",
    }
    script = pl.render_train_eval_script(params)
    for expected in ("imgsz=960", "batch=16", "optimizer='AdamW'", "lr0=0.001", "patience=7",
                     "device=0", "save_period=5", "mosaic=0.5", "epochs=30"):
        assert expected in script, expected
    assert "model.trainer.save_dir" in script

    script = pl.render_train_eval_script({**params, "hyperparams": {"batch": 8}})
    assert "batch=8" in script and "batch=16" not in script
    # pipeline-owned keys can't be smuggled in through hyperparams
    import pytest
    with pytest.raises(ValueError):
        pl.render_train_eval_script({**params, "hyperparams": {"project": "/etc"}})


def test_training_request_rejects_unknown_hyperparam():
    import pytest
    from pydantic import ValidationError
    from schemas.deployment import TrainingRemoteRequest

    base = dict(host="h", username="u", remote_workdir="/w")
    assert TrainingRemoteRequest(**base, hyperparams={"batch": "4"}).hyperparams == {"batch": 4}
    assert TrainingRemoteRequest(**base).epochs == 30
    with pytest.raises(ValidationError):
        TrainingRemoteRequest(**base, hyperparams={"data": "x"})
