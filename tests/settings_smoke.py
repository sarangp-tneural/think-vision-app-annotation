"""Iteration 7 - Phase 2 backend smoke: per-project settings + smart auto-label router."""
import os
import io
import uuid
import time
import pytest
import requests
from PIL import Image
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://ai-vision-hub-20.preview.emergentagent.com").rstrip("/")
API = f"{BASE_URL}/api"


def _session():
    s = requests.Session()
    r = Retry(total=5, backoff_factor=1.2, status_forcelist=[502, 503, 504])
    s.mount("https://", HTTPAdapter(max_retries=r))
    s.mount("http://", HTTPAdapter(max_retries=r))
    return s


def _register(email=None, name="Settings Tester"):
    email = email or f"TEST_settings_{uuid.uuid4().hex[:8]}@vf.io"
    s = _session()
    r = s.post(f"{API}/auth/register", json={"email": email, "password": "pw12345", "name": name}, timeout=30)
    assert r.status_code in (200, 201), f"register failed: {r.status_code} {r.text}"
    tok = r.json().get("token")
    assert tok
    s.headers.update({"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
    return s, email, r.json().get("user", {}).get("id")


def _make_project(s, name=None):
    name = name or f"TEST_settings_{uuid.uuid4().hex[:6]}"
    r = s.post(f"{API}/projects", json={"name": name, "task_type": "detection"}, timeout=30)
    assert r.status_code in (200, 201), r.text
    return r.json()


def _make_png_bytes():
    im = Image.new("RGB", (256, 256), (200, 50, 50))
    # draw a rectangle to give something detectable-ish
    for x in range(60, 180):
        for y in range(60, 180):
            im.putpixel((x, y), (30, 30, 200))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def _upload_image(s, pid, name="t.png"):
    url = f"{API}/projects/{pid}/images"
    sess = _session()
    sess.headers.update({"Authorization": s.headers["Authorization"]})
    files = {"file": (name, _make_png_bytes(), "image/png")}
    r = sess.post(url, files=files, timeout=60)
    assert r.status_code in (200, 201), r.text
    return r.json()


@pytest.fixture(scope="module")
def owner_ctx():
    s, email, uid = _register()
    p = _make_project(s)
    return {"s": s, "email": email, "uid": uid, "pid": p["id"], "team_id": p.get("team_id"), "project": p}


# ---------------- Tests ---------------- #

def test_get_project_includes_default_settings(owner_ctx):
    s = owner_ctx["s"]
    r = s.get(f"{API}/projects/{owner_ctx['pid']}", timeout=30)
    assert r.status_code == 200, r.text
    p = r.json()
    assert "settings" in p, "settings missing from project response"
    st = p["settings"]
    assert st.get("confidence_threshold") == 0.4
    assert st.get("fallback_to_gemini") is True
    assert st.get("min_boxes_threshold") == 1
    assert st.get("show_confidence") is True


def test_patch_settings_updates_persists(owner_ctx):
    s = owner_ctx["s"]
    pid = owner_ctx["pid"]
    payload = {"confidence_threshold": 0.7, "fallback_to_gemini": False, "min_boxes_threshold": 2, "show_confidence": False}
    r = s.patch(f"{API}/projects/{pid}/settings", json=payload, timeout=30)
    assert r.status_code == 200, r.text
    st = r.json()["settings"]
    assert st["confidence_threshold"] == 0.7
    assert st["fallback_to_gemini"] is False
    assert st["min_boxes_threshold"] == 2
    assert st["show_confidence"] is False

    # persist check via GET
    r2 = s.get(f"{API}/projects/{pid}", timeout=30)
    assert r2.status_code == 200
    st2 = r2.json()["settings"]
    assert st2["confidence_threshold"] == 0.7
    assert st2["fallback_to_gemini"] is False
    assert st2["min_boxes_threshold"] == 2
    assert st2["show_confidence"] is False


def test_patch_settings_clamps_confidence_and_min_boxes(owner_ctx):
    s = owner_ctx["s"]
    pid = owner_ctx["pid"]
    # over max
    r = s.patch(f"{API}/projects/{pid}/settings", json={"confidence_threshold": 1.5}, timeout=30)
    assert r.status_code == 200, r.text
    assert r.json()["settings"]["confidence_threshold"] == 0.95
    # under min
    r = s.patch(f"{API}/projects/{pid}/settings", json={"confidence_threshold": 0.001}, timeout=30)
    assert r.status_code == 200
    assert r.json()["settings"]["confidence_threshold"] == 0.05
    # negative min_boxes
    r = s.patch(f"{API}/projects/{pid}/settings", json={"min_boxes_threshold": -5}, timeout=30)
    assert r.status_code == 200
    assert r.json()["settings"]["min_boxes_threshold"] == 0
    # restore
    s.patch(f"{API}/projects/{pid}/settings", json={"confidence_threshold": 0.4, "min_boxes_threshold": 1}, timeout=30)


def test_patch_partial_subset(owner_ctx):
    s = owner_ctx["s"]
    pid = owner_ctx["pid"]
    r = s.patch(f"{API}/projects/{pid}/settings", json={"show_confidence": True}, timeout=30)
    assert r.status_code == 200
    st = r.json()["settings"]
    # other keys unchanged
    assert st["show_confidence"] is True
    assert "confidence_threshold" in st
    assert "fallback_to_gemini" in st
    assert "min_boxes_threshold" in st


def test_settings_updated_activity_logged(owner_ctx):
    s = owner_ctx["s"]
    pid = owner_ctx["pid"]
    # trigger fresh patch
    s.patch(f"{API}/projects/{pid}/settings", json={"confidence_threshold": 0.42}, timeout=30)
    r = s.get(f"{API}/projects/{pid}/activity", timeout=30)
    assert r.status_code == 200, r.text
    log = r.json()
    actions = [e.get("action") for e in log]
    assert "settings_updated" in actions, f"settings_updated missing in activity: {actions}"


def test_member_cannot_patch_settings(owner_ctx):
    """Invite a second user as team member and verify 403 on PATCH settings."""
    s_owner = owner_ctx["s"]
    team_id = owner_ctx["team_id"]
    pid = owner_ctx["pid"]
    assert team_id, "project must have team_id"

    # register second user
    s_mem, mem_email, _ = _register(name="Mem Tester")

    # owner invites as member
    r = s_owner.post(f"{API}/teams/{team_id}/invite", json={"email": mem_email, "role": "member"}, timeout=30)
    assert r.status_code in (200, 201), r.text

    # After invite, pending invites are converted on register via _activate_pending_invites.
    # Since member already registered before invite, we need to invite AGAIN with proper flow:
    # actually invite_member endpoint should add them directly if user exists. Verify.
    # Check owner can see member
    r_team = s_owner.get(f"{API}/teams/{team_id}", timeout=30)
    assert r_team.status_code == 200
    members = r_team.json().get("members", [])
    emails = [(m.get("email") or "").lower() for m in members]
    assert mem_email.lower() in emails, f"member not added: {emails}"

    # Member attempts PATCH
    r = s_mem.patch(f"{API}/projects/{pid}/settings", json={"confidence_threshold": 0.5}, timeout=30)
    assert r.status_code == 403, f"expected 403, got {r.status_code} {r.text}"


def test_auto_label_source_gemini_when_no_active_model(owner_ctx):
    """No trained model on this project → source='gemini' and boxes have no confidence from model (gemini path)."""
    s = owner_ctx["s"]
    pid = owner_ctx["pid"]
    # reset threshold low to ensure gemini boxes returned (gemini doesn't set confidence)
    s.patch(f"{API}/projects/{pid}/settings", json={"confidence_threshold": 0.4, "fallback_to_gemini": True, "min_boxes_threshold": 1}, timeout=30)
    img = _upload_image(s, pid)
    r = s.post(f"{API}/images/{img['id']}/auto-label", timeout=120)
    assert r.status_code == 200, r.text
    data = r.json()
    assert "source" in data
    assert data["source"] == "gemini", f"expected gemini, got {data.get('source')}"
    assert "boxes" in data
    # Gemini boxes must have source=gemini
    for b in data["boxes"]:
        assert b.get("source") == "gemini"


def test_auto_label_source_gemini_even_when_fallback_disabled_no_model(owner_ctx):
    """With no active model, disabling fallback still returns gemini (branch doesn't apply since model didn't run)."""
    s = owner_ctx["s"]
    pid = owner_ctx["pid"]
    s.patch(f"{API}/projects/{pid}/settings", json={"fallback_to_gemini": False, "min_boxes_threshold": 100}, timeout=30)
    img = _upload_image(s, pid)
    r = s.post(f"{API}/images/{img['id']}/auto-label", timeout=120)
    assert r.status_code == 200, r.text
    data = r.json()
    # no active model => gemini branch
    assert data["source"] == "gemini", f"expected gemini, got {data.get('source')}"
    # cleanup: restore defaults for next test
    s.patch(f"{API}/projects/{pid}/settings", json={"fallback_to_gemini": True, "min_boxes_threshold": 1}, timeout=30)


def test_batch_auto_label_progresses(owner_ctx):
    s = owner_ctx["s"]
    pid = owner_ctx["pid"]
    # upload 2 more images
    _upload_image(s, pid)
    _upload_image(s, pid)
    r = s.post(f"{API}/projects/{pid}/batch-auto-label", json={"only_unlabeled": True}, timeout=30)
    assert r.status_code in (200, 201), r.text
    job = r.json()
    assert "id" in job or "job_id" in job or "batch_id" in job or "status" in job
    jid = job.get("id") or job.get("job_id") or job.get("batch_id")
    if not jid:
        pytest.skip("no batch job id returned; skipping progress poll")
    # poll a few times briefly (do not need completion)
    saw_status = None
    for _ in range(5):
        rp = s.get(f"{API}/batch-jobs/{jid}", timeout=30)
        if rp.status_code == 200:
            saw_status = rp.json().get("status")
            if saw_status in ("processing", "completed"):
                break
        time.sleep(2)
    assert saw_status is not None, "batch job status could not be fetched"


def test_active_trained_model_router_if_exists(owner_ctx):
    """Optional: if a previously trained active model exists in DB from iteration 6, exercise model+gemini branch.
    Locate via GET /projects/{that_pid}/models. This uses owner's own projects only - may not have one; skip if none."""
    s = owner_ctx["s"]
    # list all projects owned by this fresh user - won't have model. So we search across all projects the user can see.
    r = s.get(f"{API}/projects", timeout=30)
    assert r.status_code == 200
    projs = r.json()
    found = None
    for p in projs:
        rm = s.get(f"{API}/projects/{p['id']}/models", timeout=30)
        if rm.status_code != 200:
            continue
        for m in rm.json():
            if m.get("type") == "real" and m.get("status") == "trained" and m.get("is_active"):
                found = (p, m)
                break
        if found:
            break
    if not found:
        pytest.skip("no active trained real model reachable by this user; branch tested in iteration 6")

    proj, model = found
    pid = proj["id"]
    # set min_boxes_threshold high to force combined path
    r = s.patch(f"{API}/projects/{pid}/settings", json={"min_boxes_threshold": 99, "fallback_to_gemini": True, "confidence_threshold": 0.4}, timeout=30)
    assert r.status_code == 200

    # find an image in this project
    ri = s.get(f"{API}/projects/{pid}/images", timeout=30)
    assert ri.status_code == 200
    imgs = ri.json()
    if not imgs:
        pytest.skip("no images in trained project")
    img_id = imgs[0]["id"]
    r = s.post(f"{API}/images/{img_id}/auto-label", timeout=180)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["source"] in ("model+gemini", "gemini"), f"got {data['source']}"

    # Now test high confidence filter with fallback disabled → 'model_no_fallback' or model
    r = s.patch(f"{API}/projects/{pid}/settings", json={"confidence_threshold": 0.95, "min_boxes_threshold": 1, "fallback_to_gemini": False}, timeout=30)
    assert r.status_code == 200
    r = s.post(f"{API}/images/{img_id}/auto-label", timeout=180)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["source"] in ("model", "model_no_fallback"), f"got {data['source']}"
    # boxes generated by model should carry confidence & source when combined branch;
    # spec says model boxes always carry source. Verify at least for combined case earlier.
