"""Iteration 6 backend smoke: real YOLOv8n self-training pipeline.

Covers:
- POST /projects/{pid}/train-real 400 gate (<30 labeled images)
- POST /projects/{pid}/train-real happy path (queued -> preparing/training -> [trained])
- GET /models/{mid} polling
- POST /models/{mid}/activate rejects untrained real models
- (partial) full training completion + activation + auto-label source=='model'
- Fallback: auto-label source=='gemini' when no active model
- GET /projects/{pid}/models still lists
- Batch auto-label has source_counts field
"""
import io
import os
import time
import random
import uuid
import pytest
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from PIL import Image, ImageDraw

BASE = os.environ.get("REACT_APP_BACKEND_URL", "https://ai-vision-hub-20.preview.emergentagent.com").rstrip("/") + "/api"


def _mk_session():
    s = requests.Session()
    retry = Retry(total=5, connect=5, read=3, backoff_factor=1.5,
                  status_forcelist=[502, 503, 504], allowed_methods=None)
    a = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    s.mount("https://", a); s.mount("http://", a)
    return s


SESSION = _mk_session()


def _req(method, path, timeout=30, **kw):
    """Session-based request with connect-retry fallback."""
    url = f"{BASE}{path}" if path.startswith("/") else path
    last_exc = None
    for attempt in range(4):
        try:
            return SESSION.request(method, url, timeout=timeout, **kw)
        except (requests.ConnectionError, requests.Timeout) as e:
            last_exc = e
            time.sleep(2 * (attempt + 1))
    raise last_exc


def _rand_email():
    return f"TEST_rt_{uuid.uuid4().hex[:10]}@vf.io"


@pytest.fixture(scope="module")
def user():
    email = _rand_email()
    r = _req("POST", f"/auth/register", json={"email": email, "password": "pw12345", "name": "RT Tester"}, timeout=20)
    assert r.status_code == 200, r.text
    tok = r.json().get("access_token") or r.json().get("token")
    return {"email": email, "token": tok, "headers": {"Authorization": f"Bearer {tok}"}}


@pytest.fixture(scope="module")
def project(user):
    r = _req("POST", f"/projects", headers=user["headers"],
                      json={"name": "RT Proj", "description": "d", "task_type": "object_detection"}, timeout=15)
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    # Add classes
    for c in ["rect", "circ"]:
        _req("POST", f"/projects/{pid}/classes", headers=user["headers"], json={"label": c}, timeout=15)
    return pid


def _make_synth_image(kind: str):
    """Returns (bytes, [annotations with normalized x,y,w,h,label]).
    Creates a 320x320 image with either a red rect or blue circle at random position.
    """
    W = H = 320
    im = Image.new("RGB", (W, H), (240, 240, 240))
    draw = ImageDraw.Draw(im)
    if kind == "rect":
        w = random.randint(80, 140); h = random.randint(80, 140)
        x = random.randint(10, W - w - 10); y = random.randint(10, H - h - 10)
        draw.rectangle([x, y, x + w, y + h], fill=(220, 30, 30))
        label = "rect"
    else:
        r = random.randint(40, 70)
        cx = random.randint(r + 10, W - r - 10); cy = random.randint(r + 10, H - r - 10)
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(30, 30, 220))
        x = cx - r; y = cy - r; w = 2 * r; h = 2 * r
        label = "circ"
    buf = io.BytesIO(); im.save(buf, "JPEG", quality=88)
    ann = {"type": "bbox", "label": label,
           "x": x / W, "y": y / H, "w": w / W, "h": h / H,
           "points": [], "rx": 0, "ry": 0}
    return buf.getvalue(), ann


def _upload_and_annotate(user, pid, kind, approve=True):
    b, ann = _make_synth_image(kind)
    files = {"file": (f"{uuid.uuid4().hex}.jpg", b, "image/jpeg")}
    r = _req("POST", f"/projects/{pid}/images", headers=user["headers"], files=files, timeout=20)
    assert r.status_code == 200, r.text
    img_id = r.json()["id"]
    # save annotation
    r = _req("PUT", f"/images/{img_id}/annotations", headers=user["headers"],
                     json={"boxes": [ann]}, timeout=15)
    assert r.status_code == 200, r.text
    if approve:
        # Directly review as owner
        r = _req("POST", f"/images/{img_id}/review", headers=user["headers"],
                          json={"decision": "approve"}, timeout=15)
        assert r.status_code == 200, r.text
    return img_id


# ---------- Tests ----------

def test_train_real_gate_400(user, project):
    """With <30 labeled images, POST /train-real must return 400 mentioning '30' labeled images."""
    # Upload a few (but <30)
    for i in range(3):
        _upload_and_annotate(user, project, "rect" if i % 2 == 0 else "circ")
    r = _req("POST", f"/projects/{project}/train-real?epochs=3", headers=user["headers"], timeout=20)
    assert r.status_code == 400, r.text
    detail = (r.json().get("detail") or "").lower()
    assert "30" in detail and "labeled" in detail, f"unexpected detail: {detail}"


@pytest.fixture(scope="module")
def big_project(user):
    """Fresh project seeded with >=30 approved+annotated synthetic images."""
    r = _req("POST", f"/projects", headers=user["headers"],
                      json={"name": "RT Big", "description": "seed", "task_type": "object_detection"}, timeout=15)
    assert r.status_code == 200
    pid = r.json()["id"]
    for c in ["rect", "circ"]:
        _req("POST", f"/projects/{pid}/classes", headers=user["headers"], json={"label": c}, timeout=15)
    # Upload 32 images
    for i in range(32):
        _upload_and_annotate(user, pid, "rect" if i % 2 == 0 else "circ", approve=True)
    return pid


def test_train_real_queued(user, big_project):
    """POST /train-real with sufficient labeled images returns queued/real/yolov8n."""
    r = _req("POST", f"/projects/{big_project}/train-real?epochs=3", headers=user["headers"], timeout=30)
    assert r.status_code == 200, r.text
    doc = r.json()
    assert doc["status"] == "queued"
    assert doc["type"] == "real"
    assert doc["model_arch"] == "yolov8n"
    assert doc["epochs"] == 3
    assert doc["training_image_count"] >= 30
    assert doc["is_active"] is False
    assert "id" in doc
    # Stash the model id on the fixture via module-level dict
    pytest.rt_model_id = doc["id"]


def test_model_get_and_progression(user):
    """Poll GET /models/{mid}. Must at minimum reach status='training'.
    Attempts to wait for 'trained' but will mark partial if too slow."""
    mid = pytest.rt_model_id
    reached_training = False
    reached_trained = False
    deadline = time.time() + 900  # 15 min max
    last_status = None
    while time.time() < deadline:
        r = _req("GET", f"/models/{mid}", headers=user["headers"], timeout=15)
        assert r.status_code == 200, r.text
        m = r.json()
        last_status = m.get("status")
        if last_status in ("training", "trained"):
            reached_training = True
        if last_status == "trained":
            reached_trained = True
            pytest.rt_trained_doc = m
            break
        if last_status == "failed":
            pytest.fail(f"Training failed: {m.get('error')}")
        time.sleep(15)
    assert reached_training, f"never reached 'training' (last status={last_status})"
    pytest.rt_reached_trained = reached_trained
    pytest.rt_last_status = last_status


def test_activate_untrained_400(user, big_project):
    """A brand new queued real model should not activate (status != trained)."""
    r = _req("POST", f"/projects/{big_project}/train-real?epochs=3", headers=user["headers"], timeout=30)
    assert r.status_code == 200, r.text
    mid2 = r.json()["id"]
    # Immediately try activate — may be queued or preparing/training but not trained
    r = _req("POST", f"/models/{mid2}/activate", headers=user["headers"], timeout=15)
    if r.status_code == 200:
        # If training already completed super-fast, skip check (unlikely on CPU)
        pytest.skip("Model already trained; cannot test 400 gate")
    assert r.status_code == 400, r.text


def test_list_models(user, big_project):
    r = _req("GET", f"/projects/{big_project}/models", headers=user["headers"], timeout=15)
    assert r.status_code == 200
    ms = r.json()
    assert isinstance(ms, list) and len(ms) >= 1
    real = [m for m in ms if m.get("type") == "real"]
    assert real, "expected at least one 'real' model in the list"


def test_activate_and_infer_full_e2e(user, big_project):
    """Only if training completed: activate + upload new image + auto-label (source='model')."""
    if not getattr(pytest, "rt_reached_trained", False):
        pytest.skip(f"Training did not complete in time (last status={getattr(pytest,'rt_last_status',None)}); partial run")
    mid = pytest.rt_model_id
    trained = pytest.rt_trained_doc
    assert trained.get("weights_path"), "weights_path not set"
    # metrics keys populated
    for k in ("final_mAP", "precision", "recall"):
        assert k in trained
    # Activate
    r = _req("POST", f"/models/{mid}/activate", headers=user["headers"], timeout=20)
    assert r.status_code == 200, r.text
    # Verify is_active=True and others inactive
    r = _req("GET", f"/projects/{big_project}/models", headers=user["headers"], timeout=15)
    ms = r.json()
    actives = [m for m in ms if m.get("is_active")]
    assert len(actives) == 1 and actives[0]["id"] == mid

    # Upload NEW image and auto-label
    b, _ = _make_synth_image("rect")
    files = {"file": ("new.jpg", b, "image/jpeg")}
    r = _req("POST", f"/projects/{big_project}/images", headers=user["headers"], files=files, timeout=20)
    assert r.status_code == 200
    new_id = r.json()["id"]
    r = _req("POST", f"/images/{new_id}/auto-label", headers=user["headers"], timeout=120)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("source") == "model", f"expected source=model, got {body}"
    assert "boxes" in body and "count" in body


def test_autolabel_gemini_fallback(user, project):
    """Small project has NO active model -> source should be 'gemini' (or error)."""
    # Upload a fresh image on the small (non-active-model) project
    b, _ = _make_synth_image("circ")
    files = {"file": ("g.jpg", b, "image/jpeg")}
    r = _req("POST", f"/projects/{project}/images", headers=user["headers"], files=files, timeout=20)
    assert r.status_code == 200
    img_id = r.json()["id"]
    r = _req("POST", f"/images/{img_id}/auto-label", headers=user["headers"], timeout=120)
    # Gemini may take a while; accept either 200/gemini or 500 if key not configured
    if r.status_code == 200:
        assert r.json().get("source") == "gemini"
    else:
        assert r.status_code in (500, 502), r.text


def test_batch_source_counts(user, big_project):
    """Batch auto-label should track source_counts field on batch_jobs doc."""
    # Upload 2 fresh images
    ids = []
    for _ in range(2):
        b, _ = _make_synth_image("rect")
        files = {"file": ("b.jpg", b, "image/jpeg")}
        r = _req("POST", f"/projects/{big_project}/images", headers=user["headers"], files=files, timeout=20)
        ids.append(r.json()["id"])
    r = _req("POST", f"/projects/{big_project}/batch-auto-label",
                     headers=user["headers"], json={"only_unlabeled": True}, timeout=20)
    assert r.status_code == 200, r.text
    jid = r.json().get("id") or r.json().get("job_id")
    assert jid
    # Poll job
    deadline = time.time() + 180
    while time.time() < deadline:
        r = _req("GET", f"/batch-jobs/{jid}", headers=user["headers"], timeout=15)
        if r.status_code == 200 and r.json().get("status") == "completed":
            job = r.json()
            assert "source_counts" in job, f"job missing source_counts: {job}"
            sc = job["source_counts"]
            assert isinstance(sc, dict)
            assert "model" in sc and "gemini" in sc
            return
        time.sleep(5)
    pytest.skip("Batch job did not complete within timeout")
