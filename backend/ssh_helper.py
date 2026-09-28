"""Standalone SSH/SFTP helper for the Deploy Pipeline feature.

No FastAPI or Mongo imports here, by design (ROADMAP.md SS2): this module never
touches persistence or logging, so "never log/persist credentials" is
structural, not just a discipline followed by callers.
"""
import contextlib
import io
import os
import stat
from typing import Callable, Optional, Tuple

import paramiko

_KEY_CLASSES = (paramiko.RSAKey, paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.DSSKey)


class SSHConnectionError(Exception):
    """Raised on connection/auth failure, including private-key load failure."""


def _load_pkey(pem_key_text: str, passphrase: Optional[str]) -> paramiko.PKey:
    """PEM text alone doesn't declare its algorithm, so try each supported key
    type in turn. A fresh io.StringIO is required per attempt since paramiko
    consumes the stream on a failed parse."""
    last_err = None
    for cls in _KEY_CLASSES:
        try:
            return cls.from_private_key(io.StringIO(pem_key_text), password=passphrase)
        except paramiko.SSHException as e:
            last_err = e
    raise SSHConnectionError(f"Could not load private key as any supported type: {last_err}")


@contextlib.contextmanager
def connect(host: str, port: int, username: str, pem_key_text: Optional[str] = None,
            password: Optional[str] = None, timeout: int = 15):
    """Context manager yielding a connected paramiko.SSHClient; always closed on exit.

    If pem_key_text is given, `password` is treated as its passphrase (may be
    None for an unencrypted key) and key-based auth is used. Otherwise
    `password` is used for plain SSH password auth.
    """
    client = paramiko.SSHClient()
    # Accepts any host key with no pinning/persistence: there's no data-model
    # field for a stored host-key fingerprint, and credentials are explicitly
    # never persisted here, so there's currently no mechanism for TOFU/pinning
    # across runs. Accepted, open risk rather than a silent choice.
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    pkey = _load_pkey(pem_key_text, password) if pem_key_text else None
    try:
        client.connect(
            hostname=host, port=port, username=username,
            pkey=pkey, password=None if pem_key_text else password,
            timeout=timeout, allow_agent=False, look_for_keys=False,
        )
    except Exception as e:
        client.close()
        raise SSHConnectionError(f"Failed to connect to {username}@{host}:{port}: {e}") from e
    try:
        yield client
    finally:
        client.close()


def exec_command(client: paramiko.SSHClient, cmd: str,
                  timeout: Optional[float] = None) -> Tuple[int, str, str]:
    """Blocking single-shot command execution. Reads stdout/stderr to
    completion before returning, which is fine for short commands but not for
    a live-progress long-running remote job — that needs its own streaming
    variant polling `exit_status_ready()`, out of scope for this module."""
    _, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    exit_code = stdout.channel.recv_exit_status()
    return (
        exit_code,
        stdout.read().decode("utf-8", "replace"),
        stderr.read().decode("utf-8", "replace"),
    )


def _ensure_remote_dir(sftp: paramiko.SFTPClient, remote_dir: str) -> None:
    """paramiko's SFTPClient has no recursive mkdir; walk and create path segments."""
    if not remote_dir or remote_dir in (".", "/"):
        return
    is_abs = remote_dir.startswith("/")
    current = "/" if is_abs else ""
    for part in remote_dir.strip("/").split("/"):
        current = f"{current}{part}" if current in ("", "/") else f"{current}/{part}"
        try:
            sftp.stat(current)
        except FileNotFoundError:
            sftp.mkdir(current)


def upload_file(client: paramiko.SSHClient, local_path: str, remote_path: str,
                 callback: Optional[Callable[[int, int], None]] = None) -> None:
    sftp = client.open_sftp()
    try:
        _ensure_remote_dir(sftp, os.path.dirname(remote_path))
        sftp.put(local_path, remote_path, callback=callback)
    finally:
        sftp.close()


def download_file(client: paramiko.SSHClient, remote_path: str, local_path: str,
                   callback: Optional[Callable[[int, int], None]] = None) -> None:
    sftp = client.open_sftp()
    try:
        local_dir = os.path.dirname(local_path)
        if local_dir:
            os.makedirs(local_dir, exist_ok=True)
        sftp.get(remote_path, local_path, callback=callback)
    finally:
        sftp.close()


def upload_dir(client: paramiko.SSHClient, local_dir: str, remote_dir: str,
               callback: Optional[Callable[[str, int, int], None]] = None) -> None:
    """Recursive upload via os.walk + per-file sftp.put (paramiko has no
    native recursive put). `callback(local_path, bytes_sent, total_bytes)` is
    a 3-arg signature (not paramiko's native 2-arg per-file one) so a caller
    uploading a whole tree can tell which file is in flight."""
    sftp = client.open_sftp()
    try:
        _ensure_remote_dir(sftp, remote_dir)
        for root, _dirs, files in os.walk(local_dir):
            rel = os.path.relpath(root, local_dir)
            remote_root = remote_dir if rel == "." else f"{remote_dir}/{rel.replace(os.sep, '/')}"
            _ensure_remote_dir(sftp, remote_root)
            for fname in files:
                local_path = os.path.join(root, fname)
                remote_path = f"{remote_root}/{fname}"
                cb = (lambda sent, total, _lp=local_path: callback(_lp, sent, total)) if callback else None
                sftp.put(local_path, remote_path, callback=cb)
    finally:
        sftp.close()


def download_dir(client: paramiko.SSHClient, remote_dir: str, local_dir: str,
                  callback: Optional[Callable[[str, int, int], None]] = None) -> None:
    """Recursive download via sftp.listdir_attr + per-file sftp.get."""
    sftp = client.open_sftp()
    try:
        _download_dir_recursive(sftp, remote_dir, local_dir, callback)
    finally:
        sftp.close()


def _download_dir_recursive(sftp: paramiko.SFTPClient, remote_dir: str, local_dir: str,
                             callback: Optional[Callable[[str, int, int], None]]) -> None:
    os.makedirs(local_dir, exist_ok=True)
    for entry in sftp.listdir_attr(remote_dir):
        remote_path = f"{remote_dir}/{entry.filename}"
        local_path = os.path.join(local_dir, entry.filename)
        if stat.S_ISDIR(entry.st_mode):
            _download_dir_recursive(sftp, remote_path, local_path, callback)
        else:
            cb = (lambda sent, total, _lp=local_path: callback(_lp, sent, total)) if callback else None
            sftp.get(remote_path, local_path, callback=cb)


def test_connection(host: str, port: int, username: str, pem_key_text: Optional[str] = None,
                     password: Optional[str] = None, timeout: int = 15) -> Tuple[bool, str]:
    """Thin connect + `echo ok` round-trip. Returns (ok, message) rather than
    raising, since this is meant to back a UI "Test Connection" button where a
    friendly message beats a caught exception. `connect`/`exec_command` stay
    exception-raising since they're lower-level building blocks used
    internally by later stages that need to distinguish failure types."""
    try:
        with connect(host, port, username, pem_key_text, password, timeout) as client:
            exit_code, out, err = exec_command(client, "echo ok", timeout=timeout)
            if exit_code == 0 and out.strip() == "ok":
                return True, "ok"
            return False, f"unexpected response (exit={exit_code}): {(out or err).strip()}"
    except SSHConnectionError as e:
        return False, str(e)
