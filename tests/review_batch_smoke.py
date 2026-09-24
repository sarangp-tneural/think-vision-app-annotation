"""Iteration 4 backend tests: review workflow + batch auto-label."""
import os
import io
import time
import uuid
import pytest
import requests
from PIL import Image

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://ai-vision-hub-20.preview.emergentagent.com").rstrip("/")
API = f"{BASE_URL}/api"


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


def _register(email, name, password="pass1234"):
    r = requests.post(f"{API}/auth/register", json={"email": email, "name": name, "password": password}, timeout=30)
    if r.status_code == 400:
        r = requests.post(f"{API}/auth/login", json={"email": email, "password": password}, timeout=30)
    assert r.status_code == 200, r.text
    j = r.json()
    return j["token"], j["user"]


def _make_png(w=200, h=200, color=(200, 100, 50)):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


def _upload(pid, tok, name="t.png", color=(200, 100, 50)):
    files = {"file": (name, io.BytesIO(_make_png(color=color)), "image/png")}
    r = requests.post(f"{API}/projects/{pid}/images", files=files, headers=H(tok), timeout=30)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture(scope="module")
def ctx():
    # Owner A + Member B + Outsider C
    a_email = f"test_owner_{uuid.uuid4().hex[:8]}@vf.io"
    b_email = f"test_mem_{uuid.uuid4().hex[:8]}@vf.io"
    c_email = f"test_out_{uuid.uuid4().hex[:8]}@vf.io"
    a_tok, a_user = _register(a_email, "Alice Owner")
    b_tok, b_user = _register(b_email, "Bob Member")
    c_tok, c_user = _register(c_email, "Carol Outsider")

    # A creates a team, invites B
    r = requests.post(f"{API}/teams", json={"name": "TEST_ReviewTeam"}, headers=H(a_tok))
    assert r.status_code == 200, r.text
    tid = r.json()["id"]
    r = requests.post(f"{API}/teams/{tid}/invite",
                      json={"email": b_email, "role": "member"}, headers=H(a_tok))
    assert r.status_code == 200 and r.json()["status"] == "active", r.text

    # Create project on that team
    r = requests.post(f"{API}/projects",
                      json={"name": "TEST_ReviewProject", "task_type": "object_detection", "team_id": tid},
                      headers=H(a_tok))
    assert r.status_code == 200, r.text
    pid = r.json()["id"]

    return {
        "a_tok": a_tok, "a_user": a_user,
        "b_tok": b_tok, "b_user": b_user,
        "c_tok": c_tok, "c_user": c_user,
        "tid": tid, "pid": pid,
    }


class TestReviewWorkflow:
    def test_upload_has_review_fields(self, ctx):
        img = _upload(ctx["pid"], ctx["a_tok"], "review1.png")
        assert img["review_status"] == "unassigned"
        assert img["assigned_to"] is None
        assert img["review_notes"] == ""
        assert img["reviewed_by"] is None
        ctx["img_id"] = img["id"]

    def test_assign_to_member(self, ctx):
        r = requests.post(f"{API}/images/{ctx['img_id']}/assign",
                          json={"user_id": ctx["b_user"]["id"]}, headers=H(ctx["a_tok"]))
        assert r.status_code == 200, r.text
        # Verify via GET
        r2 = requests.get(f"{API}/images/{ctx['img_id']}", headers=H(ctx["a_tok"]))
        assert r2.status_code == 200
        d = r2.json()
        assert d["assigned_to"] == ctx["b_user"]["id"]
        assert d["review_status"] == "assigned"

    def test_assign_to_non_member_400(self, ctx):
        r = requests.post(f"{API}/images/{ctx['img_id']}/assign",
                          json={"user_id": ctx["c_user"]["id"]}, headers=H(ctx["a_tok"]))
        assert r.status_code == 400, r.text

    def test_unassign(self, ctx):
        r = requests.post(f"{API}/images/{ctx['img_id']}/assign",
                          json={"user_id": None}, headers=H(ctx["a_tok"]))
        assert r.status_code == 200, r.text
        r2 = requests.get(f"{API}/images/{ctx['img_id']}", headers=H(ctx["a_tok"])).json()
        assert r2["assigned_to"] is None
        assert r2["review_status"] == "unassigned"
        # Reassign for later tests
        r = requests.post(f"{API}/images/{ctx['img_id']}/assign",
                          json={"user_id": ctx["b_user"]["id"]}, headers=H(ctx["a_tok"]))
        assert r.status_code == 200

    def test_submit_without_annotations_400(self, ctx):
        r = requests.post(f"{API}/images/{ctx['img_id']}/submit", headers=H(ctx["b_tok"]))
        assert r.status_code == 400, r.text

    def test_assignee_me_filter(self, ctx):
        r = requests.get(f"{API}/projects/{ctx['pid']}/images?assignee=me", headers=H(ctx["b_tok"]))
        assert r.status_code == 200, r.text
        ids = [i["id"] for i in r.json()]
        assert ctx["img_id"] in ids

    def test_annotate_and_submit(self, ctx):
        # B annotates
        r = requests.put(f"{API}/images/{ctx['img_id']}/annotations",
                         json={"boxes": [{"label": "cat", "x": 0.1, "y": 0.1, "w": 0.3, "h": 0.3}]},
                         headers=H(ctx["b_tok"]))
        assert r.status_code == 200, r.text
        # B submits
        r = requests.post(f"{API}/images/{ctx['img_id']}/submit", headers=H(ctx["b_tok"]))
        assert r.status_code == 200, r.text
        # verify
        d = requests.get(f"{API}/images/{ctx['img_id']}", headers=H(ctx["a_tok"])).json()
        assert d["review_status"] == "submitted"
        assert d["submitted_at"]

    def test_status_filter_submitted(self, ctx):
        r = requests.get(f"{API}/projects/{ctx['pid']}/images?status=submitted", headers=H(ctx["a_tok"]))
        assert r.status_code == 200
        ids = [i["id"] for i in r.json()]
        assert ctx["img_id"] in ids
        for i in r.json():
            assert i["review_status"] == "submitted"

    def test_member_cannot_review_403(self, ctx):
        r = requests.post(f"{API}/images/{ctx['img_id']}/review",
                          json={"decision": "approve"}, headers=H(ctx["b_tok"]))
        assert r.status_code == 403, r.text

    def test_owner_approve(self, ctx):
        r = requests.post(f"{API}/images/{ctx['img_id']}/review",
                          json={"decision": "approve"}, headers=H(ctx["a_tok"]))
        assert r.status_code == 200, r.text
        d = requests.get(f"{API}/images/{ctx['img_id']}", headers=H(ctx["a_tok"])).json()
        assert d["review_status"] == "approved"
        assert d["reviewed_by"] == ctx["a_user"]["id"]
        assert d["reviewed_at"]

    def test_reject_with_notes(self, ctx):
        # Upload+annotate+submit a second image
        img = _upload(ctx["pid"], ctx["a_tok"], "review2.png", color=(50, 200, 100))
        iid = img["id"]
        # assign to B
        requests.post(f"{API}/images/{iid}/assign",
                      json={"user_id": ctx["b_user"]["id"]}, headers=H(ctx["a_tok"]))
        # annotate & submit
        requests.put(f"{API}/images/{iid}/annotations",
                     json={"boxes": [{"label": "dog", "x": 0.2, "y": 0.2, "w": 0.2, "h": 0.2}]},
                     headers=H(ctx["b_tok"]))
        requests.post(f"{API}/images/{iid}/submit", headers=H(ctx["b_tok"]))
        # A rejects
        r = requests.post(f"{API}/images/{iid}/review",
                          json={"decision": "reject", "notes": "blurry"}, headers=H(ctx["a_tok"]))
        assert r.status_code == 200, r.text
        d = requests.get(f"{API}/images/{iid}", headers=H(ctx["a_tok"])).json()
        assert d["review_status"] == "rejected"
        assert d["review_notes"] == "blurry"


class TestBatchAutoLabel:
    def test_batch_flow(self, ctx):
        # Create dedicated project so unlabeled images are isolated
        r = requests.post(f"{API}/projects",
                          json={"name": "TEST_BatchProject", "task_type": "object_detection",
                                "team_id": ctx["tid"]},
                          headers=H(ctx["a_tok"]))
        assert r.status_code == 200
        pid = r.json()["id"]

        # Upload 2 tiny images (unlabeled)
        img1 = _upload(pid, ctx["a_tok"], "batch1.png", color=(200, 50, 50))
        img2 = _upload(pid, ctx["a_tok"], "batch2.png", color=(50, 50, 200))

        # Trigger batch
        r = requests.post(f"{API}/projects/{pid}/batch-auto-label",
                          json={"only_unlabeled": True}, headers=H(ctx["a_tok"]))
        assert r.status_code == 200, r.text
        job = r.json()
        assert job["status"] in ("queued", "processing")
        assert job["total"] == 2
        jid = job["id"]
        ctx["batch_pid"] = pid
        ctx["batch_jid"] = jid

        # Poll
        deadline = time.time() + 120
        last = None
        while time.time() < deadline:
            r = requests.get(f"{API}/batch-jobs/{jid}", headers=H(ctx["a_tok"]))
            assert r.status_code == 200, r.text
            last = r.json()
            if last["status"] == "completed":
                break
            time.sleep(3)
        assert last and last["status"] == "completed", f"job did not complete in time: {last}"
        assert last["processed"] == last["total"] == 2
        # We expect at least 1 labeled (Gemini should detect something)
        assert last["labeled"] >= 1, f"expected at least 1 labeled image, got {last}"

    def test_batch_no_unlabeled_400(self, ctx):
        # After batch completes, images are labeled -> only_unlabeled should return 400
        pid = ctx["batch_pid"]
        r = requests.post(f"{API}/projects/{pid}/batch-auto-label",
                          json={"only_unlabeled": True}, headers=H(ctx["a_tok"]))
        assert r.status_code == 400, r.text

    def test_list_batch_jobs(self, ctx):
        pid = ctx["batch_pid"]
        r = requests.get(f"{API}/projects/{pid}/batch-jobs", headers=H(ctx["a_tok"]))
        assert r.status_code == 200, r.text
        jobs = r.json()
        assert len(jobs) >= 1
        assert jobs[0]["id"] == ctx["batch_jid"]
        # newest first
        if len(jobs) > 1:
            assert jobs[0]["created_at"] >= jobs[1]["created_at"]
