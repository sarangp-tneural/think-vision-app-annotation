"""Iteration 3 smoke tests: annotation shape types, improved exports (with images), video frame extraction, team owner auto-approval."""
import io
import os
import time
import uuid
import json
import zipfile
import tempfile
import xml.etree.ElementTree as ET

import cv2
import numpy as np
import pytest
import requests
from PIL import Image

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
API = f"{BASE_URL}/api"


# ---------- Helpers ---------- #
def _login_or_register(email: str, password: str, name: str) -> str:
    r = requests.post(f"{API}/auth/login", json={"email": email, "password": password}, timeout=30)
    if r.status_code == 200:
        return r.json()["token"]
    r = requests.post(f"{API}/auth/register", json={"email": email, "password": password, "name": name}, timeout=30)
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _make_png_bytes(color=(255, 0, 0), size=(80, 60)) -> bytes:
    img = Image.new("RGB", size, color)
    b = io.BytesIO()
    img.save(b, format="PNG")
    return b.getvalue()


def _make_mp4_bytes(frames=30, size=(320, 240)) -> bytes:
    tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
    tmp.close()
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(tmp.name, fourcc, 15.0, size)
    for i in range(frames):
        frame = np.full((size[1], size[0], 3), (i * 8 % 255, 100, 200), dtype=np.uint8)
        writer.write(frame)
    writer.release()
    with open(tmp.name, "rb") as f:
        data = f.read()
    os.unlink(tmp.name)
    return data


@pytest.fixture(scope="module")
def token():
    return _login_or_register("demo@vf.io", "demo1234", "Demo User")


@pytest.fixture(scope="module")
def hdr(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def project(hdr):
    r = requests.post(f"{API}/projects", json={"name": f"TEST_v2_{uuid.uuid4().hex[:6]}", "task_type": "object_detection"}, headers=hdr, timeout=30)
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    yield pid
    requests.delete(f"{API}/projects/{pid}", headers=hdr, timeout=30)


@pytest.fixture(scope="module")
def image(hdr, project):
    files = {"file": ("test.png", _make_png_bytes(), "image/png")}
    r = requests.post(f"{API}/projects/{project}/images", files=files, headers=hdr, timeout=60)
    assert r.status_code == 200, r.text
    return r.json()["id"]


# ---------- Annotation shape types ---------- #
class TestAnnotationTypes:
    def test_bbox(self, hdr, image):
        boxes = [{"type": "bbox", "label": "car", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}]
        r = requests.put(f"{API}/images/{image}/annotations", json={"boxes": boxes}, headers=hdr, timeout=30)
        assert r.status_code == 200
        got = requests.get(f"{API}/images/{image}", headers=hdr, timeout=30).json()
        assert got["annotated"] is True
        assert got["annotations"][0]["type"] == "bbox"
        assert got["annotations"][0]["label"] == "car"

    def test_polygon(self, hdr, image):
        boxes = [{"type": "polygon", "label": "leaf", "points": [[0.1, 0.1], [0.5, 0.2], [0.4, 0.6]]}]
        r = requests.put(f"{API}/images/{image}/annotations", json={"boxes": boxes}, headers=hdr, timeout=30)
        assert r.status_code == 200
        got = requests.get(f"{API}/images/{image}", headers=hdr, timeout=30).json()
        assert got["annotations"][0]["type"] == "polygon"
        assert len(got["annotations"][0]["points"]) == 3

    def test_polyline(self, hdr, image):
        boxes = [{"type": "polyline", "label": "path", "points": [[0.0, 0.0], [0.3, 0.4], [0.9, 0.9]]}]
        r = requests.put(f"{API}/images/{image}/annotations", json={"boxes": boxes}, headers=hdr, timeout=30)
        assert r.status_code == 200
        got = requests.get(f"{API}/images/{image}", headers=hdr, timeout=30).json()
        assert got["annotations"][0]["type"] == "polyline"

    def test_point(self, hdr, image):
        boxes = [{"type": "point", "label": "dot", "x": 0.5, "y": 0.5}]
        r = requests.put(f"{API}/images/{image}/annotations", json={"boxes": boxes}, headers=hdr, timeout=30)
        assert r.status_code == 200
        got = requests.get(f"{API}/images/{image}", headers=hdr, timeout=30).json()
        assert got["annotations"][0]["type"] == "point"

    def test_ellipse(self, hdr, image):
        boxes = [{"type": "ellipse", "label": "ball", "x": 0.5, "y": 0.5, "rx": 0.1, "ry": 0.2}]
        r = requests.put(f"{API}/images/{image}/annotations", json={"boxes": boxes}, headers=hdr, timeout=30)
        assert r.status_code == 200
        got = requests.get(f"{API}/images/{image}", headers=hdr, timeout=30).json()
        assert got["annotations"][0]["type"] == "ellipse"
        assert got["annotations"][0]["rx"] == 0.1

    def test_backward_compat_no_type(self, hdr, image):
        # No 'type' field should default to bbox
        boxes = [{"label": "cat", "x": 0.05, "y": 0.05, "w": 0.2, "h": 0.2}]
        r = requests.put(f"{API}/images/{image}/annotations", json={"boxes": boxes}, headers=hdr, timeout=30)
        assert r.status_code == 200
        got = requests.get(f"{API}/images/{image}", headers=hdr, timeout=30).json()
        assert got["annotations"][0].get("type", "bbox") == "bbox"


# ---------- Exports include images with correct folder structure ---------- #
class TestExports:
    @pytest.fixture(scope="class")
    def export_project(self, hdr):
        r = requests.post(f"{API}/projects", json={"name": f"TEST_export_{uuid.uuid4().hex[:6]}"}, headers=hdr, timeout=30)
        pid = r.json()["id"]
        # upload one image
        files = {"file": ("e.png", _make_png_bytes(size=(100, 80)), "image/png")}
        r = requests.post(f"{API}/projects/{pid}/images", files=files, headers=hdr, timeout=60)
        img_id = r.json()["id"]
        # annotate with bbox + polygon
        boxes = [
            {"type": "bbox", "label": "car", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
            {"type": "polygon", "label": "road", "points": [[0.0, 0.5], [0.5, 0.5], [0.5, 1.0], [0.0, 1.0]]},
        ]
        requests.put(f"{API}/images/{img_id}/annotations", json={"boxes": boxes}, headers=hdr, timeout=30)
        yield pid, img_id
        requests.delete(f"{API}/projects/{pid}", headers=hdr, timeout=30)

    def test_yolo_export(self, hdr, export_project):
        pid, img_id = export_project
        r = requests.get(f"{API}/projects/{pid}/export", params={"format": "yolo"}, headers=hdr, timeout=60)
        assert r.status_code == 200
        z = zipfile.ZipFile(io.BytesIO(r.content))
        names = z.namelist()
        assert "dataset/classes.txt" in names
        assert "dataset/data.yaml" in names
        assert f"dataset/labels/{img_id}.txt" in names
        img_entries = [n for n in names if n.startswith("dataset/images/") and n.endswith((".png", ".jpg", ".jpeg"))]
        assert len(img_entries) >= 1, f"No image files in export: {names}"
        # image bytes should be a valid image
        img_bytes = z.read(img_entries[0])
        Image.open(io.BytesIO(img_bytes)).verify()
        # label file: should contain polygon-seg line (>= 6 tokens) and bbox line
        label_content = z.read(f"dataset/labels/{img_id}.txt").decode()
        lines = [l for l in label_content.split("\n") if l.strip()]
        assert len(lines) == 2
        # one line has 5 tokens (bbox), one has more (polygon seg)
        token_counts = sorted(len(l.split()) for l in lines)
        assert token_counts[0] == 5
        assert token_counts[1] > 5

    def test_coco_export(self, hdr, export_project):
        pid, img_id = export_project
        r = requests.get(f"{API}/projects/{pid}/export", params={"format": "coco"}, headers=hdr, timeout=60)
        assert r.status_code == 200
        z = zipfile.ZipFile(io.BytesIO(r.content))
        names = z.namelist()
        assert "dataset/annotations.json" in names
        img_entries = [n for n in names if n.startswith("dataset/images/")]
        assert len(img_entries) >= 1
        coco = json.loads(z.read("dataset/annotations.json"))
        assert "categories" in coco and "images" in coco and "annotations" in coco
        # width/height should be actual pixel dims (100x80), not 1x1
        assert coco["images"][0]["width"] == 100
        assert coco["images"][0]["height"] == 80
        # polygon has segmentation
        has_seg = any("segmentation" in a for a in coco["annotations"])
        assert has_seg, "No segmentation entry for polygon in COCO"

    def test_voc_export(self, hdr, export_project):
        pid, img_id = export_project
        r = requests.get(f"{API}/projects/{pid}/export", params={"format": "voc"}, headers=hdr, timeout=60)
        assert r.status_code == 200
        z = zipfile.ZipFile(io.BytesIO(r.content))
        names = z.namelist()
        assert f"dataset/annotations/{img_id}.xml" in names
        img_entries = [n for n in names if n.startswith("dataset/images/")]
        assert len(img_entries) >= 1
        xml_content = z.read(f"dataset/annotations/{img_id}.xml").decode()
        root = ET.fromstring(xml_content)
        size = root.find("size")
        assert int(size.find("width").text) == 100
        assert int(size.find("height").text) == 80
        # polygon element should exist for polygon annotation
        polygons = root.findall(".//polygon")
        assert len(polygons) >= 1


# ---------- Video frame extraction ---------- #
class TestVideo:
    def test_upload_and_extract(self, hdr, project):
        video_bytes = _make_mp4_bytes(frames=30, size=(320, 240))
        assert len(video_bytes) > 100, "mp4 generation failed"
        files = {"file": ("t.mp4", video_bytes, "video/mp4")}
        data = {"interval_frames": "5"}
        r = requests.post(f"{API}/projects/{project}/videos", files=files, data=data, headers=hdr, timeout=60)
        assert r.status_code == 200, r.text
        vdoc = r.json()
        assert vdoc["status"] in ("queued", "processing", "completed")
        vid = vdoc["id"]

        # Poll
        deadline = time.time() + 90
        status = vdoc["status"]
        last = vdoc
        while time.time() < deadline:
            g = requests.get(f"{API}/videos/{vid}", headers=hdr, timeout=30)
            assert g.status_code == 200
            last = g.json()
            status = last["status"]
            if status in ("completed", "failed"):
                break
            time.sleep(2)

        assert status == "completed", f"Video not completed: {last}"
        assert last["extracted_count"] > 0, f"No frames extracted: {last}"

        # Verify frames appear as images
        imgs = requests.get(f"{API}/projects/{project}/images", headers=hdr, timeout=30).json()
        from_video = [i for i in imgs if i.get("from_video") == vid]
        assert len(from_video) == last["extracted_count"]

        # list videos
        lv = requests.get(f"{API}/projects/{project}/videos", headers=hdr, timeout=30)
        assert lv.status_code == 200
        assert any(v["id"] == vid for v in lv.json())

        # delete video
        d = requests.delete(f"{API}/videos/{vid}", headers=hdr, timeout=30)
        assert d.status_code == 200
        g2 = requests.get(f"{API}/videos/{vid}", headers=hdr, timeout=30)
        assert g2.status_code == 404


# ---------- Team owner auto-approval ---------- #
class TestTeamAutoApproval:
    def test_creator_is_owner_active(self, hdr):
        r = requests.post(f"{API}/teams", json={"name": f"TEST_team_{uuid.uuid4().hex[:6]}"}, headers=hdr, timeout=30)
        assert r.status_code == 200, r.text
        tid = r.json()["id"]
        try:
            g = requests.get(f"{API}/teams/{tid}", headers=hdr, timeout=30)
            assert g.status_code == 200
            team = g.json()
            members = team["members"]
            assert len(members) == 1
            m = members[0]
            assert m["role"] == "owner"
            assert m["status"] == "active"
        finally:
            requests.delete(f"{API}/teams/{tid}", headers=hdr, timeout=30)
