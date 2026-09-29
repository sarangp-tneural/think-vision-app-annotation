"""Unit tests for ssh_helper.py, mocking paramiko.SSHClient throughout.

This is the first file in this test suite to use unittest.mock — every other
test hits a live backend over HTTP + real MongoDB. That's a deliberate,
flagged precedent (see ROADMAP.md M1): ssh_helper.py is a pure module with no
FastAPI/Mongo dependency, so there's nothing "live" to hit, and mocking
paramiko is the only way to exercise its branches without a real SSH server.
"""
import os
import stat
from unittest.mock import MagicMock, patch

import paramiko
import pytest

import ssh_helper


class FakeAttr:
    def __init__(self, filename, is_dir):
        self.filename = filename
        self.st_mode = stat.S_IFDIR if is_dir else stat.S_IFREG


# --- connect() -----------------------------------------------------------

@patch("ssh_helper._load_pkey")
@patch("ssh_helper.paramiko.SSHClient")
def test_connect_uses_pkey_auth_when_pem_key_given(mock_ssh_client_cls, mock_load_pkey):
    mock_client = MagicMock()
    mock_ssh_client_cls.return_value = mock_client
    mock_key = MagicMock()
    mock_load_pkey.return_value = mock_key

    with ssh_helper.connect("host", 22, "user", pem_key_text="PEM", password="passphrase") as client:
        assert client is mock_client

    mock_load_pkey.assert_called_once_with("PEM", "passphrase")
    mock_client.connect.assert_called_once_with(
        hostname="host", port=22, username="user",
        pkey=mock_key, password=None,
        timeout=15, allow_agent=False, look_for_keys=False,
    )
    mock_client.close.assert_called_once()


@patch("ssh_helper.paramiko.SSHClient")
def test_connect_uses_password_auth_when_no_pem_key(mock_ssh_client_cls):
    mock_client = MagicMock()
    mock_ssh_client_cls.return_value = mock_client

    with ssh_helper.connect("host", 22, "user", password="secret"):
        pass

    mock_client.connect.assert_called_once_with(
        hostname="host", port=22, username="user",
        pkey=None, password="secret",
        timeout=15, allow_agent=False, look_for_keys=False,
    )


@patch("ssh_helper.paramiko.SSHClient")
def test_connect_wraps_failures_as_ssh_connection_error(mock_ssh_client_cls):
    mock_client = MagicMock()
    mock_client.connect.side_effect = OSError("connection refused")
    mock_ssh_client_cls.return_value = mock_client

    with pytest.raises(ssh_helper.SSHConnectionError):
        with ssh_helper.connect("host", 22, "user", password="secret"):
            pass

    mock_client.close.assert_called_once()


@patch("ssh_helper.paramiko.SSHClient")
def test_connect_closes_client_on_context_exit_with_exception(mock_ssh_client_cls):
    mock_client = MagicMock()
    mock_ssh_client_cls.return_value = mock_client

    with pytest.raises(ValueError):
        with ssh_helper.connect("host", 22, "user", password="secret"):
            raise ValueError("boom")

    mock_client.close.assert_called_once()


# --- _load_pkey() ----------------------------------------------------------

def test_load_pkey_tries_multiple_key_types(monkeypatch):
    failing_cls = MagicMock()
    failing_cls.from_private_key.side_effect = paramiko.SSHException("bad rsa")
    succeeding_key = MagicMock()
    succeeding_cls = MagicMock()
    succeeding_cls.from_private_key.return_value = succeeding_key
    monkeypatch.setattr(ssh_helper, "_KEY_CLASSES", (failing_cls, succeeding_cls))

    result = ssh_helper._load_pkey("PEM TEXT", None)

    assert result is succeeding_key
    failing_cls.from_private_key.assert_called_once()
    succeeding_cls.from_private_key.assert_called_once()


def test_load_pkey_raises_when_no_type_matches(monkeypatch):
    failing_cls = MagicMock()
    failing_cls.from_private_key.side_effect = paramiko.SSHException("bad key")
    monkeypatch.setattr(ssh_helper, "_KEY_CLASSES", (failing_cls, failing_cls))

    with pytest.raises(ssh_helper.SSHConnectionError):
        ssh_helper._load_pkey("PEM TEXT", None)


# --- exec_command() ---------------------------------------------------------

def test_exec_command_returns_exit_code_and_streams():
    mock_client = MagicMock()
    mock_stdout = MagicMock()
    mock_stdout.channel.recv_exit_status.return_value = 0
    mock_stdout.read.return_value = b"hello"
    mock_stderr = MagicMock()
    mock_stderr.read.return_value = b""
    mock_client.exec_command.return_value = (MagicMock(), mock_stdout, mock_stderr)

    exit_code, out, err = ssh_helper.exec_command(mock_client, "echo hi", timeout=5)

    mock_client.exec_command.assert_called_once_with("echo hi", timeout=5)
    assert exit_code == 0
    assert out == "hello"
    assert err == ""


# --- exec_command_streaming() -----------------------------------------------

class FakeChannel:
    """Simulates paramiko.Channel's polling surface: exit_status_ready()
    flips true after `ready_after` calls, letting tests control whether
    streamed chunks arrive during the main poll loop or only show up in the
    post-loop drain (the exit_status_ready-before-buffer-flushed case)."""

    def __init__(self, stdout_chunks=(), stderr_chunks=(), exit_code=0, ready_after=1):
        self._stdout_chunks = list(stdout_chunks)
        self._stderr_chunks = list(stderr_chunks)
        self._exit_code = exit_code
        self._polls = 0
        self._ready_after = ready_after

    def exit_status_ready(self):
        self._polls += 1
        return self._polls > self._ready_after

    def recv_ready(self):
        return bool(self._stdout_chunks)

    def recv(self, _n):
        return self._stdout_chunks.pop(0)

    def recv_stderr_ready(self):
        return bool(self._stderr_chunks)

    def recv_stderr(self, _n):
        return self._stderr_chunks.pop(0)

    def recv_exit_status(self):
        return self._exit_code


def _mock_client_for_streaming(channel):
    mock_client = MagicMock()
    mock_stdout = MagicMock()
    mock_stdout.channel = channel
    mock_client.exec_command.return_value = (MagicMock(), mock_stdout, MagicMock())
    return mock_client


def test_exec_command_streaming_delivers_chunks_via_callback():
    channel = FakeChannel(stdout_chunks=[b"1/10\n", b"2/10\n", b"3/10\n"], exit_code=0, ready_after=5)
    mock_client = _mock_client_for_streaming(channel)
    received = []

    exit_code, out, err = ssh_helper.exec_command_streaming(
        mock_client, "python3 train.py", on_output=received.append, poll_interval=0.001,
    )

    assert received == ["1/10\n", "2/10\n", "3/10\n"]
    assert out == "1/10\n2/10\n3/10\n"
    assert exit_code == 0


def test_exec_command_streaming_drains_buffer_after_exit_ready():
    """exit_status_ready() true on the very first poll, but output is still
    sitting in the channel buffer - the post-loop drain must still pick it
    up (the paramiko gotcha this function's docstring calls out)."""
    channel = FakeChannel(stdout_chunks=[b"4/10\n", b"5/10\n"], exit_code=0, ready_after=0)
    mock_client = _mock_client_for_streaming(channel)
    received = []

    exit_code, out, err = ssh_helper.exec_command_streaming(
        mock_client, "python3 train.py", on_output=received.append, poll_interval=0.001,
    )

    assert received == ["4/10\n", "5/10\n"]
    assert out == "4/10\n5/10\n"


def test_exec_command_streaming_captures_stderr_and_nonzero_exit():
    channel = FakeChannel(stderr_chunks=[b"Traceback (most recent call last)\n"], exit_code=1, ready_after=0)
    mock_client = _mock_client_for_streaming(channel)

    exit_code, out, err = ssh_helper.exec_command_streaming(mock_client, "boom", poll_interval=0.001)

    assert exit_code == 1
    assert out == ""
    assert err == "Traceback (most recent call last)\n"


def test_exec_command_streaming_works_with_no_callback():
    channel = FakeChannel(stdout_chunks=[b"ok\n"], exit_code=0, ready_after=0)
    mock_client = _mock_client_for_streaming(channel)

    exit_code, out, err = ssh_helper.exec_command_streaming(mock_client, "echo ok", poll_interval=0.001)

    assert exit_code == 0
    assert out == "ok\n"


# --- remote_exists() / remote_remove() / remote_rename() -------------------

def test_remote_exists_true_when_stat_succeeds():
    mock_client = MagicMock()
    mock_sftp = MagicMock()
    mock_client.open_sftp.return_value = mock_sftp
    assert ssh_helper.remote_exists(mock_client, "/models/prod.pt") is True
    mock_sftp.stat.assert_called_once_with("/models/prod.pt")
    mock_sftp.close.assert_called_once()


def test_remote_exists_false_when_stat_raises_not_found():
    mock_client = MagicMock()
    mock_sftp = MagicMock()
    mock_sftp.stat.side_effect = FileNotFoundError()
    mock_client.open_sftp.return_value = mock_sftp
    assert ssh_helper.remote_exists(mock_client, "/models/prod.pt") is False


def test_remote_remove_calls_sftp_remove():
    mock_client = MagicMock()
    mock_sftp = MagicMock()
    mock_client.open_sftp.return_value = mock_sftp
    ssh_helper.remote_remove(mock_client, "/models/prod.pt.bak2")
    mock_sftp.remove.assert_called_once_with("/models/prod.pt.bak2")
    mock_sftp.close.assert_called_once()


def test_remote_rename_calls_sftp_rename():
    mock_client = MagicMock()
    mock_sftp = MagicMock()
    mock_client.open_sftp.return_value = mock_sftp
    ssh_helper.remote_rename(mock_client, "/models/prod.pt.bak1", "/models/prod.pt.bak2")
    mock_sftp.rename.assert_called_once_with("/models/prod.pt.bak1", "/models/prod.pt.bak2")
    mock_sftp.close.assert_called_once()


# --- upload_file() / download_file() ----------------------------------------

def test_upload_file_creates_remote_dirs_then_puts():
    mock_client = MagicMock()
    mock_sftp = MagicMock()
    mock_client.open_sftp.return_value = mock_sftp
    mock_sftp.stat.side_effect = FileNotFoundError()

    ssh_helper.upload_file(mock_client, "/local/model.pt", "/remote/run1/model.pt")

    mock_sftp.mkdir.assert_any_call("/remote")
    mock_sftp.mkdir.assert_any_call("/remote/run1")
    mock_sftp.put.assert_called_once_with("/local/model.pt", "/remote/run1/model.pt", callback=None)
    mock_sftp.close.assert_called_once()


def test_download_file_creates_local_dir_then_gets(tmp_path):
    mock_client = MagicMock()
    mock_sftp = MagicMock()
    mock_client.open_sftp.return_value = mock_sftp

    local_path = str(tmp_path / "nested" / "model.pt")
    ssh_helper.download_file(mock_client, "/remote/model.pt", local_path)

    assert os.path.isdir(os.path.dirname(local_path))
    mock_sftp.get.assert_called_once_with("/remote/model.pt", local_path, callback=None)
    mock_sftp.close.assert_called_once()


# --- upload_dir() / download_dir() ------------------------------------------

def test_upload_dir_walks_and_puts_each_file(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "sub" / "b.txt").write_text("b")

    mock_client = MagicMock()
    mock_sftp = MagicMock()
    mock_client.open_sftp.return_value = mock_sftp
    mock_sftp.stat.side_effect = FileNotFoundError()

    ssh_helper.upload_dir(mock_client, str(tmp_path), "/remote/dataset")

    put_calls = {call.args[:2] for call in mock_sftp.put.call_args_list}
    assert (str(tmp_path / "a.txt"), "/remote/dataset/a.txt") in put_calls
    assert (str(tmp_path / "sub" / "b.txt"), "/remote/dataset/sub/b.txt") in put_calls
    assert mock_sftp.put.call_count == 2


def test_upload_dir_callback_receives_local_path(tmp_path):
    (tmp_path / "a.txt").write_text("a")
    mock_client = MagicMock()
    mock_sftp = MagicMock()
    mock_client.open_sftp.return_value = mock_sftp
    mock_sftp.stat.side_effect = FileNotFoundError()

    def fake_put(local, remote, callback=None):
        if callback:
            callback(10, 100)
    mock_sftp.put.side_effect = fake_put

    captured = []
    ssh_helper.upload_dir(
        mock_client, str(tmp_path), "/remote",
        callback=lambda local_path, sent, total: captured.append((local_path, sent, total)),
    )

    assert captured == [(str(tmp_path / "a.txt"), 10, 100)]


def test_download_dir_walks_remote_and_gets_each_file(tmp_path):
    mock_client = MagicMock()
    mock_sftp = MagicMock()
    mock_client.open_sftp.return_value = mock_sftp

    def fake_listdir_attr(path):
        if path == "/remote":
            return [FakeAttr("a.txt", is_dir=False), FakeAttr("sub", is_dir=True)]
        if path == "/remote/sub":
            return [FakeAttr("b.txt", is_dir=False)]
        return []
    mock_sftp.listdir_attr.side_effect = fake_listdir_attr

    local_dir = str(tmp_path / "out")
    ssh_helper.download_dir(mock_client, "/remote", local_dir)

    get_calls = {call.args[:2] for call in mock_sftp.get.call_args_list}
    assert ("/remote/a.txt", os.path.join(local_dir, "a.txt")) in get_calls
    assert ("/remote/sub/b.txt", os.path.join(local_dir, "sub", "b.txt")) in get_calls
    assert os.path.isdir(os.path.join(local_dir, "sub"))


# --- test_connection() -------------------------------------------------

@patch("ssh_helper.exec_command")
@patch("ssh_helper.connect")
def test_test_connection_returns_true_on_echo_ok(mock_connect, mock_exec):
    mock_client = MagicMock()
    mock_connect.return_value.__enter__.return_value = mock_client
    mock_connect.return_value.__exit__.return_value = False
    mock_exec.return_value = (0, "ok\n", "")

    ok, msg = ssh_helper.test_connection("host", 22, "user", password="secret")

    assert ok is True
    assert msg == "ok"


@patch("ssh_helper.exec_command")
@patch("ssh_helper.connect")
def test_test_connection_returns_false_on_unexpected_response(mock_connect, mock_exec):
    mock_client = MagicMock()
    mock_connect.return_value.__enter__.return_value = mock_client
    mock_connect.return_value.__exit__.return_value = False
    mock_exec.return_value = (1, "", "permission denied")

    ok, msg = ssh_helper.test_connection("host", 22, "user", password="secret")

    assert ok is False
    assert "permission denied" in msg


@patch("ssh_helper.connect")
def test_test_connection_returns_false_message_on_connect_failure(mock_connect):
    mock_connect.side_effect = ssh_helper.SSHConnectionError("boom")

    ok, msg = ssh_helper.test_connection("host", 22, "user", password="secret")

    assert ok is False
    assert msg == "boom"


def test_resolve_remote_path_makes_paths_absolute():
    sftp = MagicMock()
    sftp.normalize.return_value = "/root"
    client = MagicMock()
    client.open_sftp.return_value = sftp

    assert ssh_helper.resolve_remote_path(client, "testing_pipeline") == "/root/testing_pipeline"
    assert ssh_helper.resolve_remote_path(client, "~/a//b/") == "/root/a/b"
    assert ssh_helper.resolve_remote_path(client, "~") == "/root"
    assert ssh_helper.resolve_remote_path(client, "") == "/root"


def test_resolve_remote_path_absolute_needs_no_round_trip():
    client = MagicMock()
    assert ssh_helper.resolve_remote_path(client, "/srv//work/") == "/srv/work"
    client.open_sftp.assert_not_called()
