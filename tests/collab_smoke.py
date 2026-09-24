"""Iteration 5 backend tests: team collaboration (reviewer/annotator, project assignments,
bulk assign, comments, activity, notifications, dashboards, e2e flow)."""
import io
import os
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
    a_email = f"test_ownerA_{uuid.uuid4().hex[:8]}@vf.io"
    b_email = f"test_annB_{uuid.uuid4().hex[:8]}@vf.io"
    c_email = f"test_revC_{uuid.uuid4().hex[:8]}@vf.io"
    d_email = f"test_outsD_{uuid.uuid4().hex[:8]}@vf.io"
    a_tok, a_user = _register(a_email, "Alice Owner")
    b_tok, b_user = _register(b_email, "Bob Annotator")
    c_tok, c_user = _register(c_email, "Carol Reviewer")
    d_tok, d_user = _register(d_email, "Dave Outsider")

    r = requests.post(f"{API}/teams", json={"name": "TEST_CollabTeam"}, headers=H(a_tok))
    assert r.status_code == 200, r.text
    tid = r.json()["id"]

    # invite annotator (auto-active since B is registered)
    r = requests.post(f"{API}/teams/{tid}/invite",
                      json={"email": b_email, "role": "annotator"}, headers=H(a_tok))
    assert r.status_code == 200 and r.json()["status"] == "active", r.text
    # invite reviewer
    r = requests.post(f"{API}/teams/{tid}/invite",
                      json={"email": c_email, "role": "reviewer"}, headers=H(a_tok))
    assert r.status_code == 200 and r.json()["status"] == "active", r.text

    # Create project on that team
    r = requests.post(f"{API}/projects",
                      json={"name": "TEST_CollabProject", "task_type": "object_detection", "team_id": tid},
                      headers=H(a_tok))
    assert r.status_code == 200, r.text
    pid = r.json()["id"]

    return {
        "a_tok": a_tok, "a_user": a_user,
        "b_tok": b_tok, "b_user": b_user,
        "c_tok": c_tok, "c_user": c_user,
        "d_tok": d_tok, "d_user": d_user,
        "tid": tid, "pid": pid,
    }


class TestRolesAndInvite:
    def test_invalid_role_400(self, ctx):
        r = requests.post(f"{API}/teams/{ctx['tid']}/invite",
                          json={"email": f"junk_{uuid.uuid4().hex[:6]}@vf.io", "role": "wizard"},
                          headers=H(ctx["a_tok"]))
        assert r.status_code == 400, r.text

    def test_patch_role_reviewer(self, ctx):
        # find Bob's membership id
        r = requests.get(f"{API}/teams/{ctx['tid']}", headers=H(ctx["a_tok"]))
        assert r.status_code == 200
        members = r.json().get("members", [])
        bob = next((m for m in members if m.get("user_id") == ctx["b_user"]["id"]), None)
        assert bob, f"Bob missing: {members}"
        mid = bob["id"]
        # change to reviewer
        r = requests.patch(f"{API}/teams/{ctx['tid']}/members/{mid}",
                           json={"role": "reviewer"}, headers=H(ctx["a_tok"]))
        assert r.status_code == 200, r.text
        # change back to annotator for downstream tests
        r = requests.patch(f"{API}/teams/{ctx['tid']}/members/{mid}",
                           json={"role": "annotator"}, headers=H(ctx["a_tok"]))
        assert r.status_code == 200, r.text


class TestProjectAssignments:
    def test_assign_project_to_annotator(self, ctx):
        r = requests.post(f"{API}/projects/{ctx['pid']}/assign",
                          json={"user_ids": [ctx["b_user"]["id"]]}, headers=H(ctx["a_tok"]))
        assert r.status_code == 200, r.text
        j = r.json()
        assert ctx["b_user"]["id"] in j["assigned"]
        assert j["count"] == 1

    def test_assign_idempotent(self, ctx):
        r = requests.post(f"{API}/projects/{ctx['pid']}/assign",
                          json={"user_ids": [ctx["b_user"]["id"]]}, headers=H(ctx["a_tok"]))
        assert r.status_code == 200, r.text
        assert r.json()["count"] == 0

    def test_assign_non_member_silently_skipped(self, ctx):
        r = requests.post(f"{API}/projects/{ctx['pid']}/assign",
                          json={"user_ids": [ctx["d_user"]["id"]]}, headers=H(ctx["a_tok"]))
        assert r.status_code == 200, r.text
        assert r.json()["count"] == 0

    def test_list_assignments(self, ctx):
        r = requests.get(f"{API}/projects/{ctx['pid']}/assignments", headers=H(ctx["a_tok"]))
        assert r.status_code == 200, r.text
        arr = r.json()
        assert any(a["user_id"] == ctx["b_user"]["id"] and a["user_email"] and a["user_name"] for a in arr)

    def test_annotator_sees_only_assigned_projects(self, ctx):
        # Create a 2nd project that Bob is NOT assigned to (once assignments exist on it, Bob won't see it — but we need at least one assignment).
        r = requests.post(f"{API}/projects",
                          json={"name": "TEST_UnassignedProj", "task_type": "object_detection", "team_id": ctx["tid"]},
                          headers=H(ctx["a_tok"]))
        assert r.status_code == 200
        pid2 = r.json()["id"]
        # assign to Carol (reviewer) so pid2 has assignments — Bob should NOT see pid2
        r = requests.post(f"{API}/projects/{pid2}/assign",
                          json={"user_ids": [ctx["c_user"]["id"]]}, headers=H(ctx["a_tok"]))
        assert r.status_code == 200
        # Bob's project list
        r = requests.get(f"{API}/projects", headers=H(ctx["b_tok"]))
        assert r.status_code == 200
        ids = [p["id"] for p in r.json()]
        assert ctx["pid"] in ids, "Bob should see assigned project"
        assert pid2 not in ids, "Bob should NOT see project with assignments to others"
        ctx["pid2"] = pid2

    def test_annotator_cannot_access_unassigned_project(self, ctx):
        r = requests.get(f"{API}/projects/{ctx['pid2']}", headers=H(ctx["b_tok"]))
        assert r.status_code == 403, r.text

    def test_owner_bypasses_assignment_filter(self, ctx):
        r = requests.get(f"{API}/projects", headers=H(ctx["a_tok"]))
        assert r.status_code == 200
        ids = [p["id"] for p in r.json()]
        assert ctx["pid"] in ids and ctx["pid2"] in ids

    def test_unassign(self, ctx):
        # add Carol to main project temporarily, then unassign
        r = requests.post(f"{API}/projects/{ctx['pid']}/assign",
                          json={"user_ids": [ctx["c_user"]["id"]]}, headers=H(ctx["a_tok"]))
        assert r.status_code == 200
        r = requests.delete(f"{API}/projects/{ctx['pid']}/assign/{ctx['c_user']['id']}",
                            headers=H(ctx["a_tok"]))
        assert r.status_code == 200, r.text
        # verify
        r = requests.get(f"{API}/projects/{ctx['pid']}/assignments", headers=H(ctx["a_tok"]))
        uids = [a["user_id"] for a in r.json()]
        assert ctx["c_user"]["id"] not in uids


class TestBulkAssignAndTaskMeta:
    def test_bulk_assign_and_notify(self, ctx):
        img1 = _upload(ctx["pid"], ctx["a_tok"], "bulk1.png")
        img2 = _upload(ctx["pid"], ctx["a_tok"], "bulk2.png", color=(50, 200, 50))
        ctx["img_ids"] = [img1["id"], img2["id"]]
        r = requests.post(f"{API}/projects/{ctx['pid']}/images/bulk-assign",
                          json={"image_ids": ctx["img_ids"], "user_id": ctx["b_user"]["id"],
                                "priority": "high", "due_date": "2026-12-31", "notes": "urgent batch"},
                          headers=H(ctx["a_tok"]))
        assert r.status_code == 200, r.text
        assert r.json()["updated"] == 2

    def test_bulk_assign_non_member_400(self, ctx):
        r = requests.post(f"{API}/projects/{ctx['pid']}/images/bulk-assign",
                          json={"image_ids": ctx["img_ids"], "user_id": ctx["d_user"]["id"]},
                          headers=H(ctx["a_tok"]))
        assert r.status_code == 400, r.text

    def test_bulk_assign_requires_owner_or_admin(self, ctx):
        r = requests.post(f"{API}/projects/{ctx['pid']}/images/bulk-assign",
                          json={"image_ids": ctx["img_ids"], "user_id": ctx["b_user"]["id"]},
                          headers=H(ctx["b_tok"]))
        assert r.status_code == 403, r.text

    def test_task_meta_patch(self, ctx):
        iid = ctx["img_ids"][0]
        r = requests.patch(f"{API}/images/{iid}/task",
                           json={"priority": "low", "notes": "revised note"},
                           headers=H(ctx["a_tok"]))
        assert r.status_code == 200, r.text
        r2 = requests.get(f"{API}/images/{iid}", headers=H(ctx["a_tok"])).json()
        assert r2.get("priority") == "low"
        assert r2.get("task_notes") == "revised note"


class TestComments:
    def test_top_level_comment(self, ctx):
        iid = ctx["img_ids"][0]
        r = requests.post(f"{API}/images/{iid}/comments",
                          json={"text": "please double-check"}, headers=H(ctx["a_tok"]))
        assert r.status_code == 200, r.text
        c = r.json()
        assert c["parent_id"] is None
        assert c["text"] == "please double-check"
        assert c["resolved"] is False
        ctx["cid"] = c["id"]

    def test_reply(self, ctx):
        r = requests.post(f"{API}/comments/{ctx['cid']}/replies",
                          json={"text": "ok, done"}, headers=H(ctx["b_tok"]))
        assert r.status_code == 200, r.text
        rep = r.json()
        assert rep["parent_id"] == ctx["cid"]

    def test_resolve(self, ctx):
        r = requests.post(f"{API}/comments/{ctx['cid']}/resolve", headers=H(ctx["a_tok"]))
        assert r.status_code == 200, r.text

    def test_list_comments_sorted(self, ctx):
        iid = ctx["img_ids"][0]
        r = requests.get(f"{API}/images/{iid}/comments", headers=H(ctx["a_tok"]))
        assert r.status_code == 200
        arr = r.json()
        assert len(arr) >= 2
        # ascending order
        for i in range(1, len(arr)):
            assert arr[i - 1]["created_at"] <= arr[i]["created_at"]
        top = next(c for c in arr if c["id"] == ctx["cid"])
        assert top["resolved"] is True


class TestActivityAndNotifications:
    def test_activity_log_present(self, ctx):
        r = requests.get(f"{API}/projects/{ctx['pid']}/activity", headers=H(ctx["a_tok"]))
        assert r.status_code == 200, r.text
        arr = r.json()
        actions = {a["action"] for a in arr}
        # We've done: project_assigned, bulk_assigned, comment_added
        assert "project_assigned" in actions
        assert "bulk_assigned" in actions
        assert "comment_added" in actions
        # sorted most-recent first
        for i in range(1, len(arr)):
            assert arr[i - 1]["created_at"] >= arr[i]["created_at"]

    def test_notifications_bob(self, ctx):
        r = requests.get(f"{API}/notifications", headers=H(ctx["b_tok"]))
        assert r.status_code == 200
        data = r.json()
        assert "items" in data and "unread" in data
        kinds = {n["kind"] for n in data["items"]}
        # Bob got project_assigned + image_assigned (bulk)
        assert "project_assigned" in kinds
        assert "image_assigned" in kinds
        assert data["unread"] >= 2
        ctx["notif_id_for_read"] = data["items"][0]["id"]

    def test_mark_single_read(self, ctx):
        r = requests.post(f"{API}/notifications/{ctx['notif_id_for_read']}/read", headers=H(ctx["b_tok"]))
        assert r.status_code == 200

    def test_mark_all_read(self, ctx):
        r = requests.post(f"{API}/notifications/mark-all-read", headers=H(ctx["b_tok"]))
        assert r.status_code == 200
        # verify unread == 0
        r2 = requests.get(f"{API}/notifications", headers=H(ctx["b_tok"])).json()
        assert r2["unread"] == 0


class TestDashboards:
    def test_project_dashboard(self, ctx):
        r = requests.get(f"{API}/projects/{ctx['pid']}/dashboard", headers=H(ctx["a_tok"]))
        assert r.status_code == 200, r.text
        d = r.json()
        assert "total" in d and "counts" in d and "members_perf" in d
        assert "completion_pct" in d
        # counts has all keys
        for k in ["unassigned", "assigned", "submitted", "approved", "rejected"]:
            assert k in d["counts"]
        # bob should be in members_perf
        bob = next((m for m in d["members_perf"] if m["user_id"] == ctx["b_user"]["id"]), None)
        assert bob and bob["assigned_images"] >= 2

    def test_team_dashboard_owner(self, ctx):
        r = requests.get(f"{API}/teams/{ctx['tid']}/dashboard", headers=H(ctx["a_tok"]))
        assert r.status_code == 200, r.text
        d = r.json()
        assert "total_projects" in d and "projects" in d and "total_images" in d
        assert d["total_projects"] >= 2

    def test_team_dashboard_annotator_403(self, ctx):
        r = requests.get(f"{API}/teams/{ctx['tid']}/dashboard", headers=H(ctx["b_tok"]))
        assert r.status_code == 403, r.text

    def test_team_dashboard_reviewer_ok(self, ctx):
        r = requests.get(f"{API}/teams/{ctx['tid']}/dashboard", headers=H(ctx["c_tok"]))
        assert r.status_code == 200, r.text


class TestE2EFlow:
    """End-to-end flow validating the full collaboration lifecycle."""
    def test_end_to_end(self, ctx):
        # Fresh isolated team for e2e clarity
        a_tok = ctx["a_tok"]
        # New annotator user
        e_email = f"test_e2e_ann_{uuid.uuid4().hex[:8]}@vf.io"
        e_tok, e_user = _register(e_email, "Eve Annotator")

        # A creates team
        r = requests.post(f"{API}/teams", json={"name": "TEST_E2ETeam"}, headers=H(a_tok))
        assert r.status_code == 200
        tid = r.json()["id"]
        # invite Eve as annotator
        r = requests.post(f"{API}/teams/{tid}/invite",
                          json={"email": e_email, "role": "annotator"}, headers=H(a_tok))
        assert r.status_code == 200 and r.json()["status"] == "active"
        # A creates project
        r = requests.post(f"{API}/projects",
                          json={"name": "TEST_E2EProj", "task_type": "object_detection", "team_id": tid},
                          headers=H(a_tok))
        pid = r.json()["id"]

        # A assigns project to Eve
        r = requests.post(f"{API}/projects/{pid}/assign",
                          json={"user_ids": [e_user["id"]]}, headers=H(a_tok))
        assert r.status_code == 200 and r.json()["count"] == 1

        # Eve sees the project
        r = requests.get(f"{API}/projects", headers=H(e_tok))
        assert r.status_code == 200
        assert pid in [p["id"] for p in r.json()], "Eve should see assigned project"

        # A uploads image
        img = _upload(pid, a_tok, "e2e.png")

        # A bulk-assigns to Eve
        r = requests.post(f"{API}/projects/{pid}/images/bulk-assign",
                          json={"image_ids": [img["id"]], "user_id": e_user["id"]},
                          headers=H(a_tok))
        assert r.status_code == 200 and r.json()["updated"] == 1

        # Eve should get image_assigned notification
        r = requests.get(f"{API}/notifications", headers=H(e_tok)).json()
        kinds = [n["kind"] for n in r["items"]]
        assert "image_assigned" in kinds, f"Eve notifications: {kinds}"

        # Eve annotates and submits
        r = requests.put(f"{API}/images/{img['id']}/annotations",
                         json={"boxes": [{"label": "obj", "x": 0.1, "y": 0.1, "w": 0.3, "h": 0.3}]},
                         headers=H(e_tok))
        assert r.status_code == 200
        r = requests.post(f"{API}/images/{img['id']}/submit", headers=H(e_tok))
        assert r.status_code == 200, r.text

        # A gets review_needed notification
        r = requests.get(f"{API}/notifications", headers=H(a_tok)).json()
        # Filter to this image
        a_kinds = [n["kind"] for n in r["items"] if n.get("image_id") == img["id"]]
        assert "review_needed" in a_kinds, f"Alice kinds for e2e img: {a_kinds}"

        # Eve cannot approve (403)
        r = requests.post(f"{API}/images/{img['id']}/review",
                          json={"decision": "approve"}, headers=H(e_tok))
        assert r.status_code == 403, r.text

        # A approves
        r = requests.post(f"{API}/images/{img['id']}/review",
                          json={"decision": "approve"}, headers=H(a_tok))
        assert r.status_code == 200, r.text

        # Eve gets review_completed
        r = requests.get(f"{API}/notifications", headers=H(e_tok)).json()
        e_kinds = [n["kind"] for n in r["items"] if n.get("image_id") == img["id"]]
        assert "review_completed" in e_kinds, f"Eve kinds for e2e img: {e_kinds}"

        # Activity log has all key actions
        r = requests.get(f"{API}/projects/{pid}/activity", headers=H(a_tok))
        assert r.status_code == 200
        actions = {a["action"] for a in r.json()}
        expected = {"project_assigned", "bulk_assigned", "image_submitted", "image_approved"}
        assert expected.issubset(actions), f"missing: {expected - actions}"
