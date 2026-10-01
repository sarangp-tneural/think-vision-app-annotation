"""Annotate -> submit -> review -> approve workflow and who may train / deploy.

Self-contained: runs the app in-process against a throwaway database.
"""
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402
from _inprocess import fresh_db, get_client  # noqa: E402

BOX = {"label": "cat", "x": .1, "y": .1, "w": .2, "h": .2}
NOW = datetime.now(timezone.utc).isoformat()


@pytest.fixture(scope="module")
def db():
    yield fresh_db()


@pytest.fixture(scope="module")
def client(db):
    return get_client()


def _register(client, name):
    r = client.post("/api/auth/register", json={"email": f"{name}@example.com", "password": "pw123456", "name": name})
    assert r.status_code == 200, r.text
    return r.json()["user"]["id"], {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture(scope="module")
def world(client, db):
    w = {}
    w["owner_id"], w["owner"] = _register(client, "olivia")
    team = next(t for t in client.get("/api/teams", headers=w["owner"]).json() if t["is_personal"])
    w["pid"] = client.post("/api/projects", json={"name": "Flow", "team_id": team["id"]}, headers=w["owner"]).json()["id"]
    for name, role in (("ann", "annotator"), ("ann2", "annotator"), ("rev", "reviewer"), ("mem", "member")):
        w[name + "_id"], w[name] = _register(client, name)
        r = client.post(f"/api/teams/{team['id']}/invite", json={"email": f"{name}@example.com", "role": role}, headers=w["owner"])
        assert r.status_code == 200, r.text
    w["tid"] = team["id"]
    return w


_n = 0


def _img(db, world, **fields):
    global _n
    _n += 1
    doc = {"id": f"img{_n}", "project_id": world["pid"], "annotations": [], "annotated": False,
           "review_status": "unassigned", "assigned_to": None, "created_at": NOW}
    doc.update(fields)
    db.images.insert_one(doc)
    return doc["id"]


def _put(client, who, iid, boxes=(BOX,)):
    return client.put(f"/api/images/{iid}/annotations", json={"boxes": list(boxes)}, headers=who)


def test_annotator_edits_only_assigned_images(client, db, world):
    mine = _img(db, world, assigned_to=world["ann_id"], review_status="assigned")
    theirs = _img(db, world, assigned_to=world["ann2_id"], review_status="assigned")
    free = _img(db, world)
    assert _put(client, world["ann"], mine).status_code == 200
    assert _put(client, world["ann"], theirs).status_code == 403
    assert _put(client, world["ann"], free).status_code == 403
    # members annotate under the same rules
    memimg = _img(db, world, assigned_to=world["mem_id"], review_status="assigned")
    assert _put(client, world["mem"], memimg).status_code == 200


def test_reviewer_cannot_edit_or_change_images(client, db, world):
    iid = _img(db, world, assigned_to=world["rev_id"], review_status="assigned")
    assert _put(client, world["rev"], iid).status_code == 403
    assert client.post(f"/api/images/{iid}/auto-label", headers=world["rev"]).status_code == 403
    assert client.post(f"/api/projects/{world['pid']}/batch-auto-label", json={}, headers=world["rev"]).status_code == 403
    assert client.delete(f"/api/images/{iid}", headers=world["rev"]).status_code == 403


def test_full_reject_reannotate_resubmit_approve_cycle(client, db, world):
    iid = _img(db, world, assigned_to=world["ann_id"], review_status="assigned")
    # cannot submit an empty image or someone else's
    assert client.post(f"/api/images/{iid}/submit", headers=world["ann"]).status_code == 400
    assert _put(client, world["ann"], iid).status_code == 200
    assert client.post(f"/api/images/{iid}/submit", headers=world["ann2"]).status_code == 403
    assert client.post(f"/api/images/{iid}/submit", headers=world["ann"]).status_code == 200
    # locked while waiting for review
    assert _put(client, world["ann"], iid).status_code == 409
    assert client.post(f"/api/images/{iid}/submit", headers=world["ann"]).status_code == 409
    # annotators cannot review
    assert client.post(f"/api/images/{iid}/review", json={"decision": "approve"}, headers=world["ann"]).status_code == 403
    # reject needs a note
    assert client.post(f"/api/images/{iid}/review", json={"decision": "reject"}, headers=world["rev"]).status_code == 400
    r = client.post(f"/api/images/{iid}/review", json={"decision": "reject", "notes": "box too loose"}, headers=world["rev"])
    assert r.status_code == 200
    assert db.images.find_one({"id": iid})["review_status"] == "rejected"
    # annotator re-annotates and resubmits; the old verdict is cleared
    assert _put(client, world["ann"], iid, [BOX, {**BOX, "x": .5}]).status_code == 200
    assert client.post(f"/api/images/{iid}/submit", headers=world["ann"]).status_code == 200
    img = db.images.find_one({"id": iid})
    assert img["review_status"] == "submitted" and img["review_notes"] == "" and img["reviewed_by"] is None
    assert client.post(f"/api/images/{iid}/review", json={"decision": "approve"}, headers=world["rev"]).status_code == 200
    # approved images are locked for everyone
    assert _put(client, world["ann"], iid).status_code == 409
    assert _put(client, world["owner"], iid).status_code == 409
    assert client.delete(f"/api/images/{iid}", headers=world["owner"]).status_code == 409
    assert client.post(f"/api/images/{iid}/assign", json={"user_id": None}, headers=world["owner"]).status_code == 409


def test_review_requires_submitted_image(client, db, world):
    iid = _img(db, world, assigned_to=world["ann_id"], review_status="assigned", annotations=[BOX], annotated=True)
    assert client.post(f"/api/images/{iid}/review", json={"decision": "approve"}, headers=world["rev"]).status_code == 409


def test_nobody_reviews_their_own_work(client, db, world):
    # owner annotates an unassigned image (claims it), submits, then cannot approve it: a reviewer exists
    iid = _img(db, world)
    assert _put(client, world["owner"], iid).status_code == 200
    assert db.images.find_one({"id": iid})["assigned_to"] == world["owner_id"]
    assert client.post(f"/api/images/{iid}/submit", headers=world["owner"]).status_code == 200
    r = client.post(f"/api/images/{iid}/review", json={"decision": "approve"}, headers=world["owner"])
    assert r.status_code == 403
    assert client.post(f"/api/images/{iid}/review", json={"decision": "approve"}, headers=world["rev"]).status_code == 200


def test_solo_workspace_can_review_own_work(client, db):
    _, solo = _register(client, "solo")
    pid = client.post("/api/projects", json={"name": "Solo"}, headers=solo).json()["id"]
    db.images.insert_one({"id": "solo1", "project_id": pid, "annotations": [], "annotated": False,
                          "review_status": "unassigned", "assigned_to": None, "created_at": NOW})
    assert client.put("/api/images/solo1/annotations", json={"boxes": [BOX]}, headers=solo).status_code == 200
    assert client.post("/api/images/solo1/submit", headers=solo).status_code == 200
    assert client.post("/api/images/solo1/review", json={"decision": "approve"}, headers=solo).status_code == 200


def test_only_admins_assign_and_bulk_assign_skips_locked(client, db, world):
    open_img = _img(db, world, assigned_to=world["ann_id"], review_status="assigned")
    locked = _img(db, world, assigned_to=world["ann_id"], review_status="submitted", annotations=[BOX], annotated=True)
    assert client.post(f"/api/images/{open_img}/assign", json={"user_id": world["ann2_id"]}, headers=world["ann"]).status_code == 403
    assert client.patch(f"/api/images/{open_img}/task", json={"priority": "high"}, headers=world["ann"]).status_code == 403
    r = client.post(f"/api/projects/{world['pid']}/images/bulk-assign",
                    json={"image_ids": [open_img, locked], "user_id": world["ann2_id"]}, headers=world["owner"])
    assert r.json()["updated"] == 1
    assert db.images.find_one({"id": locked})["review_status"] == "submitted"


def test_annotated_means_approved_everywhere(client, db, world):
    pid = world["pid"]
    before = client.get(f"/api/projects/{pid}", headers=world["owner"]).json()
    _img(db, world, annotations=[BOX], annotated=True, review_status="submitted")
    _img(db, world, annotations=[BOX], annotated=True, review_status="rejected")
    ok = _img(db, world, annotations=[BOX], annotated=True, review_status="approved")
    after = client.get(f"/api/projects/{pid}", headers=world["owner"]).json()
    assert after["annotated_count"] == before["annotated_count"] + 1
    assert after["labeled_count"] == before["labeled_count"] + 3
    v = client.post(f"/api/projects/{pid}/versions", json={"name": "v1"}, headers=world["owner"]).json()
    assert ok in v["snapshot_image_ids"]
    states = {db.images.find_one({"id": i})["review_status"] for i in v["snapshot_image_ids"]}
    assert states == {"approved"}
    plan = client.get(f"/api/projects/{pid}/training-plan", headers=world["owner"]).json()
    assert plan["eligible_images"] == after["annotated_count"]


def test_training_and_deploy_permissions(client, world):
    pid = world["pid"]
    # annotators and members may start assist training (fails later only for lack of approved data)
    for who in ("ann", "mem"):
        r = client.post(f"/api/projects/{pid}/train-real", headers=world[who])
        assert r.status_code == 400 and "approved" in r.json()["detail"]
    assert client.post(f"/api/projects/{pid}/train-real", headers=world["rev"]).status_code == 403
    # the deploy pipeline and the mock trainer stay owner/admin only
    for who in ("ann", "mem", "rev"):
        assert client.post(f"/api/projects/{pid}/pipeline/runs", headers=world[who]).status_code == 403
    assert client.post(f"/api/projects/{pid}/train", json={"version_id": "x"}, headers=world["ann"]).status_code == 403


def test_assist_model_activation_rules(client, db, world):
    pid = world["pid"]
    for mid, creator in (("m-ann", world["ann_id"]), ("m-ann2", world["ann2_id"])):
        db.models.insert_one({"id": mid, "project_id": pid, "user_id": creator, "type": "real", "purpose": "assist",
                              "status": "trained", "weights_path": "/tmp/w.pt", "created_at": NOW})
    assert client.post("/api/models/m-ann/activate", headers=world["ann"]).status_code == 200
    assert client.post("/api/models/m-ann2/activate", headers=world["ann"]).status_code == 403  # not the creator
    assert client.post("/api/models/m-ann2/activate", headers=world["owner"]).status_code == 200
    db.models.insert_one({"id": "m-prod", "project_id": pid, "user_id": world["ann_id"], "type": "real",
                          "source": "pipeline", "status": "trained", "weights_path": "/tmp/w.pt", "created_at": NOW})
    assert client.post("/api/models/m-prod/activate", headers=world["ann"]).status_code == 403
