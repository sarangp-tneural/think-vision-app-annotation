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
