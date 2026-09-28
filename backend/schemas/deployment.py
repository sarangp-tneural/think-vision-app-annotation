"""Pydantic models for the Deploy Pipeline feature.

SECURITY: `SSHCredentials` instances carry raw secret material (private key
text and/or a password/passphrase). They must NEVER be logged, written to
Mongo, passed to `_log_activity`, or echoed back in any HTTP response body.
They exist only for the lifetime of a single request or background-job call
and should be passed by reference through function arguments, not serialized
or cached anywhere.
"""
from typing import Optional

from pydantic import BaseModel


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

    remote_data_yaml_path: str


class UploadingDataRequest(SSHCredentials):
    """Body for POST /pipeline/runs/{rid}/stage/uploading_data. Same
    rationale as ClassCheckRequest: remote_workdir (and the split
    percentages) come fresh per request, not from deployment_pipelines."""

    remote_workdir: str
    train_pct: float = 0.7
    valid_pct: float = 0.2
    test_pct: float = 0.1


class TrainingRemoteRequest(SSHCredentials):
    """Body for POST /pipeline/runs/{rid}/stage/training_remote. Same
    rationale as ClassCheckRequest/UploadingDataRequest:
    deployment_pipelines.remote_base_model_path/remote_production_model_path
    are still unconfigured (no flow before M9), so both come fresh per
    request. remote_production_model_path is only required for merge runs -
    checked in the worker, not here, since run_type isn't visible to this
    schema."""

    remote_workdir: str
    remote_base_model_path: str
    remote_production_model_path: Optional[str] = None
    epochs: int = 10


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


class RollbackRequest(SSHCredentials):
    """Body for POST /projects/{pid}/pipeline/rollback."""

    remote_production_model_path: str
