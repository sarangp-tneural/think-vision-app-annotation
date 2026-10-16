"""Local-disk storage: round trip, content type, path traversal and missing files."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402
import _inprocess  # noqa: E402

server = _inprocess.server


@pytest.fixture()
def storage(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "STORAGE_DIR", str(tmp_path))
    server.init_storage()
    return tmp_path


def test_round_trip_and_content_type(storage):
    res = server.put_object("app/projects/p1/a.jpg", b"abc", "image/jpeg")
    assert res == {"path": "app/projects/p1/a.jpg", "size": 3}
    data, ct = server.get_object("app/projects/p1/a.jpg")
    assert data == b"abc" and ct == "image/jpeg"
    assert (storage / "app/projects/p1/a.jpg").read_bytes() == b"abc"


def test_unknown_extension_falls_back_to_octet_stream(storage):
    server.put_object("app/m/w.pt", b"w", "application/octet-stream")
    assert server.get_object("app/m/w.pt")[1] == "application/octet-stream"


def test_overwrite_replaces_file_and_leaves_no_temp(storage):
    server.put_object("a/b.txt", b"one", "text/plain")
    server.put_object("a/b.txt", b"two", "text/plain")
    assert server.get_object("a/b.txt")[0] == b"two"
    assert [p.name for p in (storage / "a").iterdir()] == ["b.txt"]


@pytest.mark.parametrize("bad", ["../x", "a/../../x", "/etc/passwd", "", "a\x00b"])
def test_rejects_unsafe_paths(storage, bad):
    with pytest.raises(ValueError):
        server.put_object(bad, b"x", "text/plain")
    with pytest.raises(ValueError):
        server.get_object(bad)


def test_missing_file_raises(storage):
    with pytest.raises(FileNotFoundError):
        server.get_object("nope/missing.jpg")


def test_delete_object_and_prefix(storage):
    server.put_object("app/projects/p1/a.jpg", b"a", "image/jpeg")
    server.put_object("app/projects/p1/videos/v.mp4", b"v", "video/mp4")
    server.put_object("app/projects/p2/b.jpg", b"b", "image/jpeg")
    assert server.delete_object("app/projects/p1/a.jpg") is True
    assert server.delete_object("app/projects/p1/a.jpg") is False  # already gone
    assert server.delete_prefix("app/projects/p1") is True
    assert not (storage / "app/projects/p1").exists()
    assert (storage / "app/projects/p2/b.jpg").exists()  # other projects untouched


def test_delete_prefix_never_removes_storage_root(storage):
    server.put_object("keep/x.jpg", b"x", "image/jpeg")
    for bad in ("", ".", "keep/.."):
        try:
            assert server.delete_prefix(bad) is False
        except ValueError:
            pass
    assert (storage / "keep/x.jpg").exists()
