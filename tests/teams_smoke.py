"""Teams collaboration backend tests for VisionForge."""
import os
import io
import uuid
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://ai-vision-hub-20.preview.emergentagent.com").rstrip("/")
API = f"{BASE_URL}/api"


def _headers(token):
    return {"Authorization": f"Bearer {token}"}


def _register(email, name, password="pass1234"):
    r = requests.post(f"{API}/auth/register", json={"email": email, "name": name, "password": password}, timeout=30)
    if r.status_code == 400:  # already exists -> login
        r = requests.post(f"{API}/auth/login", json={"email": email, "password": password}, timeout=30)
    assert r.status_code == 200, f"register/login failed: {r.status_code} {r.text}"
    j = r.json()
    return j["token"], j["user"]


def _login(email, password="pass1234"):
    r = requests.post(f"{API}/auth/login", json={"email": email, "password": password}, timeout=30)
    assert r.status_code == 200, r.text
    j = r.json()
    return j["token"], j["user"]


@pytest.fixture(scope="module")
def user_a():
    email = f"test_a_{uuid.uuid4().hex[:8]}@vf.io"
    token, u = _register(email, "Alice A")
    return {"token": token, "user": u, "email": email}


@pytest.fixture(scope="module")
def user_b():
    email = f"test_b_{uuid.uuid4().hex[:8]}@vf.io"
    token, u = _register(email, "Bob B")
    return {"token": token, "user": u, "email": email}


# ---------- Personal Workspace Auto-Creation ----------
class TestPersonalWorkspace:
    def test_register_creates_personal_team(self, user_a):
        r = requests.get(f"{API}/teams", headers=_headers(user_a["token"]))
        assert r.status_code == 200, r.text
        teams = r.json()
        personal = [t for t in teams if t.get("is_personal")]
        assert len(personal) == 1, f"expected 1 personal team, got {len(personal)}: {teams}"
        assert personal[0]["role"] == "owner"
        assert personal[0]["member_count"] == 1
        assert "Workspace" in personal[0]["name"]

    def test_login_is_idempotent(self, user_a):
        # login again and confirm still only 1 personal team
        tok, _ = _login(user_a["email"])
        r = requests.get(f"{API}/teams", headers=_headers(tok))
        assert r.status_code == 200
        personal = [t for t in r.json() if t.get("is_personal")]
        assert len(personal) == 1


# ---------- Team CRUD ----------
class TestTeamCRUD:
    def test_create_team(self, user_a):
        r = requests.post(f"{API}/teams", json={"name": "TEST_Team_Alpha"}, headers=_headers(user_a["token"]))
        assert r.status_code == 200, r.text
        t = r.json()
        assert t["name"] == "TEST_Team_Alpha"
        assert t["role"] == "owner"
        assert t["is_personal"] is False
        user_a["team_alpha_id"] = t["id"]

    def test_list_shows_new_team(self, user_a):
        r = requests.get(f"{API}/teams", headers=_headers(user_a["token"]))
        assert r.status_code == 200
        ids = [t["id"] for t in r.json()]
        assert user_a["team_alpha_id"] in ids

    def test_get_team_with_members(self, user_a):
        tid = user_a["team_alpha_id"]
        r = requests.get(f"{API}/teams/{tid}", headers=_headers(user_a["token"]))
        assert r.status_code == 200, r.text
        t = r.json()
        assert "members" in t
        assert len(t["members"]) == 1
        assert t["members"][0]["role"] == "owner"
        assert t["members"][0]["email"] == user_a["email"]

    def test_rename_team(self, user_a):
        tid = user_a["team_alpha_id"]
        r = requests.patch(f"{API}/teams/{tid}", json={"name": "TEST_Team_Renamed"}, headers=_headers(user_a["token"]))
        assert r.status_code == 200
        r2 = requests.get(f"{API}/teams/{tid}", headers=_headers(user_a["token"]))
        assert r2.json()["name"] == "TEST_Team_Renamed"

    def test_rename_by_non_owner_forbidden(self, user_a, user_b):
        tid = user_a["team_alpha_id"]
        r = requests.patch(f"{API}/teams/{tid}", json={"name": "hacked"}, headers=_headers(user_b["token"]))
        assert r.status_code == 403

    def test_cannot_delete_personal_workspace(self, user_a):
        teams = requests.get(f"{API}/teams", headers=_headers(user_a["token"])).json()
        personal_id = [t for t in teams if t["is_personal"]][0]["id"]
        r = requests.delete(f"{API}/teams/{personal_id}", headers=_headers(user_a["token"]))
        assert r.status_code == 400


# ---------- Invite Flows ----------
class TestInvites:
    def test_invite_existing_user_active_immediately(self, user_a, user_b):
        # user_a creates a team, invites user_b (already registered) -> immediate active
        r = requests.post(f"{API}/teams", json={"name": "TEST_InviteExisting"}, headers=_headers(user_a["token"]))
        tid = r.json()["id"]
        user_a["team_invite_id"] = tid

        r = requests.post(f"{API}/teams/{tid}/invite", json={"email": user_b["email"], "role": "member"},
                          headers=_headers(user_a["token"]))
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "active"

        # user_b should now see this team in their list
        r2 = requests.get(f"{API}/teams", headers=_headers(user_b["token"]))
        ids = [t["id"] for t in r2.json()]
        assert tid in ids

    def test_invite_new_email_pending_then_activates_on_register(self, user_a):
        # invite an email that hasn't registered
        pending_email = f"test_pending_{uuid.uuid4().hex[:8]}@vf.io"
        r = requests.post(f"{API}/teams", json={"name": "TEST_InvitePending"}, headers=_headers(user_a["token"]))
        tid = r.json()["id"]

        r = requests.post(f"{API}/teams/{tid}/invite", json={"email": pending_email, "role": "admin"},
                          headers=_headers(user_a["token"]))
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "pending"

        # Verify pending appears in team members
        r = requests.get(f"{API}/teams/{tid}", headers=_headers(user_a["token"]))
        members = r.json()["members"]
        pending_members = [m for m in members if m["status"] == "pending"]
        assert any(m["email"] == pending_email for m in pending_members)

        # Now register that email -> should auto-activate
        tok_new, u_new = _register(pending_email, "Pending User")
        r = requests.get(f"{API}/teams", headers=_headers(tok_new))
        ids = [t["id"] for t in r.json()]
        assert tid in ids, "Pending invite should have auto-activated on register"

        # role should be admin
        my_team = [t for t in r.json() if t["id"] == tid][0]
        assert my_team["role"] == "admin"

    def test_duplicate_invite_rejected(self, user_a, user_b):
        tid = user_a["team_invite_id"]
        r = requests.post(f"{API}/teams/{tid}/invite", json={"email": user_b["email"], "role": "member"},
                          headers=_headers(user_a["token"]))
        assert r.status_code == 400


# ---------- Member Role Management ----------
class TestMemberRoles:
    def test_change_role_and_remove_member(self, user_a, user_b):
        tid = user_a["team_invite_id"]
        # get member id of user_b
        r = requests.get(f"{API}/teams/{tid}", headers=_headers(user_a["token"]))
        mem_b = [m for m in r.json()["members"] if m.get("user_id") == user_b["user"]["id"]][0]
        mid_b = mem_b["id"]

        # promote to admin
        r = requests.patch(f"{API}/teams/{tid}/members/{mid_b}", json={"role": "admin"},
                           headers=_headers(user_a["token"]))
        assert r.status_code == 200, r.text

        # cannot change owner role
        owner_mem = [m for m in requests.get(f"{API}/teams/{tid}", headers=_headers(user_a["token"])).json()["members"]
                     if m["role"] == "owner"][0]
        r = requests.patch(f"{API}/teams/{tid}/members/{owner_mem['id']}", json={"role": "member"},
                           headers=_headers(user_a["token"]))
        assert r.status_code == 400

        # remove member
        r = requests.delete(f"{API}/teams/{tid}/members/{mid_b}", headers=_headers(user_a["token"]))
        assert r.status_code == 200

        # user_b should no longer see the team
        r = requests.get(f"{API}/teams", headers=_headers(user_b["token"]))
        assert tid not in [t["id"] for t in r.json()]

    def test_cannot_remove_owner(self, user_a):
        tid = user_a["team_invite_id"]
        owner_mem = [m for m in requests.get(f"{API}/teams/{tid}", headers=_headers(user_a["token"])).json()["members"]
                     if m["role"] == "owner"][0]
        r = requests.delete(f"{API}/teams/{tid}/members/{owner_mem['id']}", headers=_headers(user_a["token"]))
        assert r.status_code == 400


# ---------- Projects with Teams ----------
class TestProjectsTeams:
    def test_create_project_in_team(self, user_a, user_b):
        # create a fresh team and invite B
        r = requests.post(f"{API}/teams", json={"name": "TEST_ProjectTeam"}, headers=_headers(user_a["token"]))
        tid = r.json()["id"]
        user_a["team_proj_id"] = tid
        requests.post(f"{API}/teams/{tid}/invite", json={"email": user_b["email"], "role": "member"},
                      headers=_headers(user_a["token"]))

        r = requests.post(f"{API}/projects",
                          json={"name": "TEST_TeamProject", "task_type": "object_detection", "team_id": tid},
                          headers=_headers(user_a["token"]))
        assert r.status_code == 200, r.text
        p = r.json()
        assert p["team_id"] == tid
        assert p["team_name"] == "TEST_ProjectTeam"
        user_a["team_project_id"] = p["id"]

    def test_create_project_defaults_to_personal(self, user_a):
        r = requests.post(f"{API}/projects", json={"name": "TEST_PersonalProject"},
                          headers=_headers(user_a["token"]))
        assert r.status_code == 200
        p = r.json()
        # personal workspace team
        teams = requests.get(f"{API}/teams", headers=_headers(user_a["token"])).json()
        personal = [t for t in teams if t.get("is_personal")][0]
        assert p["team_id"] == personal["id"]

    def test_list_projects_includes_all_teams(self, user_a):
        r = requests.get(f"{API}/projects", headers=_headers(user_a["token"]))
        assert r.status_code == 200
        pids = [p["id"] for p in r.json()]
        assert user_a["team_project_id"] in pids
        # each must have team_name
        for p in r.json():
            assert "team_name" in p

    def test_list_projects_filtered_by_team(self, user_a):
        tid = user_a["team_proj_id"]
        r = requests.get(f"{API}/projects?team_id={tid}", headers=_headers(user_a["token"]))
        assert r.status_code == 200
        for p in r.json():
            assert p["team_id"] == tid

    def test_member_can_access_shared_project(self, user_a, user_b):
        pid = user_a["team_project_id"]
        r = requests.get(f"{API}/projects/{pid}", headers=_headers(user_b["token"]))
        assert r.status_code == 200, f"team member B should access: {r.status_code} {r.text}"

    def test_member_can_upload_and_annotate(self, user_a, user_b):
        pid = user_a["team_project_id"]
        # Upload a tiny PNG
        png_bytes = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
                     b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\xff"
                     b"\xff?\x00\x05\xfe\x02\xfe\xa5\xb7\x9c\x8c\x00\x00\x00\x00IEND\xaeB`\x82")
        files = {"file": ("t.png", io.BytesIO(png_bytes), "image/png")}
        r = requests.post(f"{API}/projects/{pid}/images", files=files, headers=_headers(user_b["token"]))
        assert r.status_code == 200, f"B upload failed: {r.status_code} {r.text}"
        img_id = r.json()["id"]

        # annotate
        r = requests.put(f"{API}/images/{img_id}/annotations",
                        json={"boxes": [{"label": "cat", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}]},
                        headers=_headers(user_b["token"]))
        assert r.status_code == 200, r.text
        assert r.json()["count"] == 1

    def test_non_member_cannot_access(self, user_a):
        pid = user_a["team_project_id"]
        # create a fresh isolated user
        outsider_email = f"test_out_{uuid.uuid4().hex[:8]}@vf.io"
        tok, _ = _register(outsider_email, "Outsider")
        r = requests.get(f"{API}/projects/{pid}", headers=_headers(tok))
        assert r.status_code in (403, 404), f"outsider got {r.status_code}"

    def test_create_project_in_non_member_team_forbidden(self, user_a):
        outsider_email = f"test_out2_{uuid.uuid4().hex[:8]}@vf.io"
        tok, _ = _register(outsider_email, "Outsider2")
        r = requests.post(f"{API}/projects", json={"name": "hack", "team_id": user_a["team_proj_id"]},
                          headers=_headers(tok))
        assert r.status_code == 403


# ---------- Team Delete Cascade ----------
class TestTeamDeleteCascade:
    def test_delete_team_cascades_projects(self, user_a):
        # create dedicated team + project
        r = requests.post(f"{API}/teams", json={"name": "TEST_ToDelete"}, headers=_headers(user_a["token"]))
        tid = r.json()["id"]
        r = requests.post(f"{API}/projects", json={"name": "TEST_ToDeleteProject", "team_id": tid},
                          headers=_headers(user_a["token"]))
        pid = r.json()["id"]

        # delete team
        r = requests.delete(f"{API}/teams/{tid}", headers=_headers(user_a["token"]))
        assert r.status_code == 200, r.text

        # team is gone
        r = requests.get(f"{API}/teams/{tid}", headers=_headers(user_a["token"]))
        assert r.status_code in (403, 404)

        # project gone
        r = requests.get(f"{API}/projects/{pid}", headers=_headers(user_a["token"]))
        assert r.status_code in (403, 404)

    def test_delete_team_non_owner_forbidden(self, user_a, user_b):
        r = requests.post(f"{API}/teams", json={"name": "TEST_ForbidDel"}, headers=_headers(user_a["token"]))
        tid = r.json()["id"]
        requests.post(f"{API}/teams/{tid}/invite", json={"email": user_b["email"], "role": "member"},
                      headers=_headers(user_a["token"]))
        r = requests.delete(f"{API}/teams/{tid}", headers=_headers(user_b["token"]))
        assert r.status_code == 403


# ---------- Existing demo user compatibility ----------
class TestDemoUser:
    def test_demo_login_and_teams(self):
        r = requests.post(f"{API}/auth/login", json={"email": "demo@vf.io", "password": "demo1234"}, timeout=30)
        if r.status_code != 200:
            pytest.skip("demo user unavailable")
        tok = r.json()["token"]
        r = requests.get(f"{API}/teams", headers=_headers(tok))
        assert r.status_code == 200
        personal = [t for t in r.json() if t.get("is_personal")]
        assert len(personal) == 1

    def test_demo_projects_have_team_id(self):
        r = requests.post(f"{API}/auth/login", json={"email": "demo@vf.io", "password": "demo1234"}, timeout=30)
        if r.status_code != 200:
            pytest.skip("demo user unavailable")
        tok = r.json()["token"]
        r = requests.get(f"{API}/projects", headers=_headers(tok))
        assert r.status_code == 200
        for p in r.json():
            # legacy projects should have been migrated
            assert p.get("team_id"), f"legacy project missing team_id: {p}"
