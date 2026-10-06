"""Pydantic models for the Deploy Pipeline feature.

SECURITY: `SSHCredentials` instances carry raw secret material (private key
text and/or a password/passphrase). They must NEVER be logged, written to
Mongo, passed to `_log_activity`, or echoed back in any HTTP response body.
They exist only for the lifetime of a single request or background-job call
and should be passed by reference through function arguments, not serialized
or cached anywhere.
"""
from typing import Literal, Optional

from pydantic import BaseModel, field_validator, model_validator

import pipeline_logic


class SSHCredentials(BaseModel):
    host: str
    port: int = 22
    username: str
    pem_key: Optional[str] = None
    # If pem_key is set, this is its passphrase (may be None for an unencrypted
    # key). If pem_key is None, this is a plain SSH password instead.
    password: Optional[str] = None


class ClassCheckRequest(SSHCredentials):
    """Body for POST /pipeline/runs/{rid}/stage/class_check. No milestone
    before M9 builds a "configure remote host" flow, so this carries the
    remote data.yaml path alongside credentials - fully self-contained per
    request, never read from deployment_pipelines, never persisted."""

    # Blank/None -> the router falls back to the yaml the upload stage recorded.
    remote_data_yaml_path: Optional[str] = None


class UploadingDataRequest(SSHCredentials):
    """Body for POST /pipeline/runs/{rid}/stage/uploading_data. Same
    rationale as ClassCheckRequest: remote_workdir (and the split
    percentages) come fresh per request, not from deployment_pipelines."""

    remote_workdir: str
    train_pct: float = 0.7
    valid_pct: float = 0.2
    test_pct: float = 0.1
    # "new" uploads a fresh dataset to {workdir}/{run_id}/dataset. "merge"
    # adds our images/labels into the split dirs of an existing remote
    # dataset described by existing_data_yaml_path; "reuse" trains on it as-is.
    dataset_mode: Literal["new", "merge", "reuse"] = "new"
    existing_data_yaml_path: Optional[str] = None
    # merge only: execute a previewed plan (POST .../merge_plan). Without one, merge builds a plan itself and only
    # ADDS new images. Label updates and data.yaml class additions never happen unless confirmed here.
    plan_id: Optional[str] = None
    confirm_label_updates: bool = False
    update_class_names: bool = False

    @model_validator(mode="after")
    def _existing_yaml_required(self):
        if self.dataset_mode != "new" and not self.existing_data_yaml_path:
            raise ValueError("existing_data_yaml_path is required for dataset_mode merge/reuse")
        return self


class MergePlanRequest(SSHCredentials):
    """Body for POST /pipeline/runs/{rid}/merge_plan: a read-only preview of what a merge into an existing server
    dataset would add / update / skip. Credentials are request-only like everywhere else."""

    existing_data_yaml_path: str
    train_pct: float = 0.7
    valid_pct: float = 0.2
    test_pct: float = 0.1
    # Label format of the server's dataset (detect | seg | obb). "auto" uses what the import recorded, else samples the
    # server's labels, and asks when they are ambiguous (9 values per line can be a polygon or an oriented box).
    label_format: str = "auto"


class TrainingRemoteRequest(SSHCredentials):
    """Body for POST /pipeline/runs/{rid}/stage/training_remote. Same
    rationale as ClassCheckRequest/UploadingDataRequest:
    deployment_pipelines.remote_base_model_path/remote_production_model_path
    are still unconfigured (no flow before M9), so both come fresh per
    request. remote_production_model_path is only required for merge runs -
    checked in the worker, not here, since run_type isn't visible to this
    schema."""

    remote_workdir: str
    # Starting model, in priority order: an existing remote checkpoint the user
    # picked (start_from_path), else a fresh Ultralytics model name downloaded
    # on the server (yolo_model, e.g. "yolov8n.pt"), else the legacy
    # remote_base_model_path (bootstrap) / remote_production_model_path (merge).
    start_from_path: Optional[str] = None
    yolo_model: Optional[str] = None
    remote_base_model_path: Optional[str] = None
    remote_production_model_path: Optional[str] = None
    epochs: int = 30
    # Editable training settings (see pipeline_logic.TRAIN_PARAM_TYPES);
    # unknown keys are rejected since they end up in a generated script.
    hyperparams: dict = {}
    # "system": bare python3. "existing": {venv_path}/bin/python. "create":
    # build {remote_workdir}/venv and pip install dependencies into it.
    env_mode: Literal["system", "existing", "create"] = "system"
    venv_path: Optional[str] = None

    @field_validator("hyperparams")
    @classmethod
    def _check_hyperparams(cls, v):
        return pipeline_logic.sanitize_hyperparams(v)

    @model_validator(mode="after")
    def _venv_path_required(self):
        if self.env_mode == "existing" and not self.venv_path:
            raise ValueError("venv_path is required when env_mode is 'existing'")
        return self


class InspectRemoteRequest(SSHCredentials):
    """Body for POST /pipeline/runs/{rid}/inspect_remote - read-only probe
    for a pre-existing dataset (data.yaml) in the workdir and, optionally,
    whether venv_path holds a usable interpreter."""

    remote_workdir: str
    venv_path: Optional[str] = None
    # Explicit dataset yaml to inspect even if the workdir scan wouldn't find it.
    extra_yaml_path: Optional[str] = None


class DownloadingModelRequest(SSHCredentials):
    """Body for POST /pipeline/runs/{rid}/stage/downloading_model. No extra
    fields beyond SSHCredentials - the remote run dir, run name, and
    metrics.json path are all read back from what training_remote (M5)
    already stored on the run doc, not re-supplied here."""


class DeployingRequest(SSHCredentials):
    """Body for POST /pipeline/runs/{rid}/stage/deploying. Same
    self-contained-per-request rationale as every prior stage schema:
    deployment_pipelines.remote_production_model_path is still unconfigured
    (no flow before M9)."""

    remote_production_model_path: str
    # Which server-trained model to upload; blank = this run's own model.
    model_id: Optional[str] = None


class RollbackRequest(SSHCredentials):
    """Body for POST /projects/{pid}/pipeline/rollback."""

    remote_production_model_path: str


class TrainRemoteRequest(SSHCredentials):
    """Body for POST /projects/{pid}/train-remote (plain Model Training on a host over SSH, no pipeline run).
    Credentials are per request and never stored; the other fields are remembered on the project."""

    remote_workdir: str
    env_mode: Literal["system", "existing", "create"] = "system"
    venv_path: Optional[str] = None
    model_arch: Literal["yolov8n", "yolov8s", "yolov8m", "yolov8l"] = "yolov8n"
    epochs: int = 0  # 0 = auto, same as local training

    @model_validator(mode="after")
    def _check(self):
        if self.env_mode == "existing" and not (self.venv_path or "").strip():
            raise ValueError("venv_path is required when env_mode is 'existing'")
        if not self.remote_workdir.strip():
            raise ValueError("remote_workdir is required")
        if not 0 <= self.epochs <= 200:
            raise ValueError("epochs must be between 0 (auto) and 200")
        return self
