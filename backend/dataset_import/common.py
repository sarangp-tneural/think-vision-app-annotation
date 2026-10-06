"""Small helpers shared by the dataset readers, the scanner and the committer (no I/O beyond reading zip members)."""
import posixpath
import zipfile
from typing import Optional

IMAGE_EXTS = {"jpg", "jpeg", "png", "webp", "gif"}
CONTENT_TYPES = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp", "gif": "image/gif"}
MAX_LABEL_BYTES = 5 * 1024 * 1024
MAX_YAML_BYTES = 1024 * 1024
MAX_XML_BYTES = 5 * 1024 * 1024
MAX_COCO_JSON_BYTES = 128 * 1024 * 1024  # parsed in memory, so bounded


class ImportError_(Exception):
    """A problem with the dataset itself (bad zip, no data.yaml, ...). Message is shown to the user."""


def clean(name: str) -> Optional[str]:
    """Normalised member path, or None for anything that must be ignored: absolute or parent-escaping paths
    (members are only ever read by name, never extracted, but they are still refused), hidden files, macOS metadata."""
    n = posixpath.normpath(name.replace("\\", "/"))
    if n.startswith("/") or n == "." or n.startswith("../") or "/../" in n:
        return None
    if any(part.startswith(".") or part == "__MACOSX" for part in n.split("/")):
        return None
    return n


def ext_of(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in posixpath.basename(name) else ""


def read_capped(zf: zipfile.ZipFile, member: str, limit: int) -> Optional[bytes]:
    if zf.getinfo(member).file_size > limit:
        return None
    return zf.read(member)
