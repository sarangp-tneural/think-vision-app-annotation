"""Read-only, company-wide analytics for the global `manager` role.

Frames come from two sources that are merged into one record list:
  * app-tracked: images with `annotated_by` / `annotated_at` (set on every annotation save)
  * work log:    rows people enter or import from the daily spreadsheet
When the app tracked work for the same (person, day, project), the app number wins and the
logged row is dropped, so the same frames are never counted twice.
"""
import csv
import io
from collections import defaultdict
from datetime import date as date_cls, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

router = APIRouter()

TEST_EMAIL = {"$regex": "^test_", "$options": "i"}


def _range(date_from: Optional[str], date_to: Optional[str]):
    try:
        end = date_cls.fromisoformat(date_to) if date_to else date_cls.today()
        start = date_cls.fromisoformat(date_from) if date_from else end - timedelta(days=29)
    except ValueError:
        raise HTTPException(status_code=400, detail="Dates must be YYYY-MM-DD")
    if start > end:
        raise HTTPException(status_code=400, detail="'from' must not be after 'to'")
    if (end - start).days > 731:
        raise HTTPException(status_code=400, detail="Range too large (max 2 years)")
    return start.isoformat(), end.isoformat(), (end + timedelta(days=1)).isoformat()


def _days(start: str, end: str):
    d, last = date_cls.fromisoformat(start), date_cls.fromisoformat(end)
    while d <= last:
        yield d.isoformat()
        d += timedelta(days=1)


def register(s):
    db = s.db
    require_manager = s.require_manager

    async def _test_user_ids():
        rows = await db.users.find({"email": TEST_EMAIL}, {"id": 1}).to_list(None)
        return [r["id"] for r in rows]

    async def _records(start: str, end: str, end_excl: str, user_id=None, project=None):
        """Merged frame records: [{date, user_id, project, project_id, frames, source}]."""
        excluded = await _test_user_ids()
        match = {"annotated_by": {"$exists": True, "$nin": excluded}, "annotated_at": {"$gte": start, "$lt": end_excl}}
        if user_id:
            match["annotated_by"] = user_id
        if project:
            match["project_id"] = project
        app_rows = await db.images.aggregate([
            {"$match": match},
            {"$group": {"_id": {"u": "$annotated_by", "p": "$project_id", "d": {"$substr": ["$annotated_at", 0, 10]}},
                        "n": {"$sum": 1}}},
        ]).to_list(None)
        projects = {p["id"]: p["name"] for p in await db.projects.find({}, {"id": 1, "name": 1}).to_list(None)}
        records, app_keys = [], set()
        for r in app_rows:
            name = projects.get(r["_id"]["p"], "(deleted project)")
            app_keys.add((r["_id"]["u"], r["_id"]["d"], name.lower()))
            records.append({"date": r["_id"]["d"], "user_id": r["_id"]["u"], "project": name,
                            "project_id": r["_id"]["p"], "frames": r["n"], "source": "app"})
        log_match = {"date": {"$gte": start, "$lte": end}, "user_id": {"$nin": excluded}}
        if user_id:
            log_match["user_id"] = user_id
        for w in await db.work_logs.find(log_match, {"_id": 0}).to_list(None):
            if (w["user_id"], w["date"], w["project_name"].lower()) in app_keys:
                continue
            pid = next((k for k, v in projects.items() if v.lower() == w["project_name"].lower()), None)
            if project and pid != project:
                continue
            records.append({"date": w["date"], "user_id": w["user_id"], "project": w["project_name"],
                            "project_id": pid, "frames": w.get("frames", 0), "source": "log"})
        return records

    async def _users(ids=None):
        q = {"role": {"$ne": "manager"}, "email": {"$not": TEST_EMAIL}}
        if ids is not None:
            q["id"] = {"$in": list(ids)}
        return {u["id"]: u for u in await db.users.find(q, {"_id": 0, "id": 1, "name": 1, "email": 1, "created_at": 1}).to_list(None)}

    async def _review_stats(start: str, end_excl: str, user_id=None, project=None):
        """{owner_id: {approved, rejected, submitted}} for reviews/submissions inside the range."""
        out = defaultdict(lambda: {"approved": 0, "rejected": 0, "submitted": 0})
        owner = {"$ifNull": ["$annotated_by", "$assigned_to"]}

        def base(field):
            m = {field: {"$gte": start, "$lt": end_excl}}
            if project:
                m["project_id"] = project
            return m

        for r in await db.images.aggregate([
            {"$match": {**base("reviewed_at"), "review_status": {"$in": ["approved", "rejected"]}}},
            {"$group": {"_id": {"o": owner, "st": "$review_status"}, "n": {"$sum": 1}}},
        ]).to_list(None):
            if r["_id"]["o"]:
                out[r["_id"]["o"]][r["_id"]["st"]] += r["n"]
        for r in await db.images.aggregate([
            {"$match": base("submitted_at")},
            {"$group": {"_id": owner, "n": {"$sum": 1}}},
        ]).to_list(None):
            if r["_id"]:
                out[r["_id"]]["submitted"] += r["n"]
        if user_id:
            return {user_id: out[user_id]}
        return out

    def _rate(a, r):
        return round(100 * a / (a + r), 1) if (a + r) else None

    @router.get("/manager/overview")
    async def overview(date_from: Optional[str] = Query(None, alias="from"), date_to: Optional[str] = Query(None, alias="to"),
                       _=Depends(require_manager)):
        start, end, end_excl = _range(date_from, date_to)
        recs = await _records(start, end, end_excl)
        users = await _users({r["user_id"] for r in recs})
        recs = [r for r in recs if r["user_id"] in users]
        daily = defaultdict(int)
        per_user, per_project = defaultdict(int), defaultdict(int)
        for r in recs:
            daily[r["date"]] += r["frames"]
            per_user[r["user_id"]] += r["frames"]
            per_project[r["project"]] += r["frames"]
        reviews = await _review_stats(start, end_excl)
        approved = sum(v["approved"] for v in reviews.values())
        rejected = sum(v["rejected"] for v in reviews.values())
        pipeline = {r["_id"] or "unassigned": r["n"] for r in await db.images.aggregate(
            [{"$group": {"_id": "$review_status", "n": {"$sum": 1}}}]).to_list(None)}
        return {
            "range": {"from": start, "to": end},
            "kpis": {
                "frames": sum(daily.values()),
                "active_people": len(per_user),
                "active_projects": len(per_project),
                "total_people": await db.users.count_documents({"role": {"$ne": "manager"}, "email": {"$not": TEST_EMAIL}}),
                "total_projects": await db.projects.count_documents({}),
                "approval_rate": _rate(approved, rejected),
                "approved": approved, "rejected": rejected,
                "logged_frames": sum(r["frames"] for r in recs if r["source"] == "log"),
                "tracked_frames": sum(r["frames"] for r in recs if r["source"] == "app"),
            },
            "daily": [{"date": d, "frames": daily.get(d, 0)} for d in _days(start, end)],
            "top_people": sorted(({"user_id": u, "name": users[u]["name"], "frames": n} for u, n in per_user.items()),
                                 key=lambda x: -x["frames"])[:8],
            "top_projects": sorted(({"project": p, "frames": n} for p, n in per_project.items()),
                                   key=lambda x: -x["frames"])[:8],
            "review_pipeline": pipeline,
        }

    @router.get("/manager/people")
    async def people(date_from: Optional[str] = Query(None, alias="from"), date_to: Optional[str] = Query(None, alias="to"),
                     project: Optional[str] = None, _=Depends(require_manager)):
        start, end, end_excl = _range(date_from, date_to)
        recs = await _records(start, end, end_excl, project=project)
        users = await _users()
        reviews = await _review_stats(start, end_excl, project=project)
        last_seen = {r["_id"]: r["t"] for r in await db.activity_log.aggregate(
            [{"$group": {"_id": "$user_id", "t": {"$max": "$created_at"}}}]).to_list(None)}
        for r in await db.images.aggregate([
            {"$match": {"annotated_by": {"$exists": True}}},
            {"$group": {"_id": "$annotated_by", "t": {"$max": "$annotated_at"}}}]).to_list(None):
            if r["t"] > last_seen.get(r["_id"], ""):
                last_seen[r["_id"]] = r["t"]
        agg = defaultdict(lambda: {"frames": 0, "days": set(), "projects": set()})
        for r in recs:
            a = agg[r["user_id"]]
            a["frames"] += r["frames"]
            a["days"].add(r["date"])
            a["projects"].add(r["project"])
        out = []
        for uid, u in users.items():
            a, rv = agg.get(uid), reviews.get(uid, {"approved": 0, "rejected": 0, "submitted": 0})
            roles = await db.team_members.distinct("role", {"user_id": uid, "status": "active"})
            out.append({
                "user_id": uid, "name": u["name"], "email": u["email"],
                "roles": sorted(roles),
                "frames": a["frames"] if a else 0,
                "active_days": len(a["days"]) if a else 0,
                "avg_per_active_day": round(a["frames"] / len(a["days"])) if a and a["days"] else 0,
                "projects": sorted(a["projects"]) if a else [],
                "submitted": rv["submitted"], "approved": rv["approved"], "rejected": rv["rejected"],
                "approval_rate": _rate(rv["approved"], rv["rejected"]),
                "last_active": last_seen.get(uid),
            })
        out.sort(key=lambda x: (-x["frames"], x["name"].lower()))
        return {"range": {"from": start, "to": end}, "people": out}

    @router.get("/manager/people/{uid}")
    async def person_detail(uid: str, date_from: Optional[str] = Query(None, alias="from"),
                            date_to: Optional[str] = Query(None, alias="to"), _=Depends(require_manager)):
        start, end, end_excl = _range(date_from, date_to)
        users = await _users([uid])
        if uid not in users:
            raise HTTPException(status_code=404, detail="Person not found")
        recs = await _records(start, end, end_excl, user_id=uid)
        daily, by_project = defaultdict(int), defaultdict(int)
        for r in recs:
            daily[r["date"]] += r["frames"]
            by_project[r["project"]] += r["frames"]
        rv = (await _review_stats(start, end_excl, user_id=uid))[uid]
        memberships = await db.team_members.find({"user_id": uid, "status": "active"}, {"_id": 0}).to_list(100)
        teams = {t["id"]: t["name"] for t in await db.teams.find({"id": {"$in": [m["team_id"] for m in memberships]}}).to_list(100)}
        logs = await db.work_logs.find({"user_id": uid, "date": {"$gte": start, "$lte": end}}, {"_id": 0, "import_key": 0}
                                       ).sort([("date", -1), ("created_at", -1)]).to_list(1000)
        activity = await db.activity_log.find({"user_id": uid}, {"_id": 0, "meta": 0}).sort("created_at", -1).to_list(30)
        return {
            "range": {"from": start, "to": end},
            "user": users[uid],
            "teams": [{"team": teams.get(m["team_id"], "?"), "role": m["role"]} for m in memberships],
            "frames": sum(daily.values()),
            "active_days": len([d for d, n in daily.items() if n]),
            "daily": [{"date": d, "frames": daily.get(d, 0)} for d in _days(start, end)],
            "by_project": sorted(({"project": p, "frames": n} for p, n in by_project.items()), key=lambda x: -x["frames"]),
            "review": {**rv, "approval_rate": _rate(rv["approved"], rv["rejected"])},
            "work_logs": logs,
            "recent_activity": activity,
        }

    @router.get("/manager/projects")
    async def projects(date_from: Optional[str] = Query(None, alias="from"), date_to: Optional[str] = Query(None, alias="to"),
                       _=Depends(require_manager)):
        start, end, end_excl = _range(date_from, date_to)
        recs = await _records(start, end, end_excl)
        frames, people = defaultdict(int), defaultdict(set)
        for r in recs:
            key = r["project"].lower()
            frames[key] += r["frames"]
            people[key].add(r["user_id"])
        status = defaultdict(dict)
        for r in await db.images.aggregate([{"$group": {"_id": {"p": "$project_id", "st": "$review_status"}, "n": {"$sum": 1}}}]).to_list(None):
            status[r["_id"]["p"]][r["_id"].get("st") or "unassigned"] = r["n"]
        annotated = {r["_id"]: r["n"] for r in await db.images.aggregate(
            [{"$match": {"review_status": "approved"}}, {"$group": {"_id": "$project_id", "n": {"$sum": 1}}}]).to_list(None)}
        teams = {t["id"]: t["name"] for t in await db.teams.find({}, {"id": 1, "name": 1}).to_list(None)}
        active_models = {m["project_id"]: m for m in await db.models.find(
            {"is_active": True, "status": "trained"}, {"_id": 0, "project_id": 1, "name": 1, "model_arch": 1, "final_mAP": 1}).to_list(None)}
        out, known = [], set()
        for p in await db.projects.find({}, {"_id": 0}).to_list(None):
            key = p["name"].lower()
            known.add(key)
            st = status.get(p["id"], {})
            total = sum(st.values())
            out.append({
                "project_id": p["id"], "name": p["name"], "team": teams.get(p.get("team_id"), ""),
                "task_type": p.get("task_type", ""), "total_images": total,
                "annotated": annotated.get(p["id"], 0),
                "progress": round(100 * annotated.get(p["id"], 0) / total) if total else 0,
                "review": st, "active_model": active_models.get(p["id"]),
                "frames_in_range": frames.get(key, 0), "contributors": len(people.get(key, ())),
            })
        for key in frames:  # projects that only exist in the work log
            if key not in known:
                name = next(r["project"] for r in recs if r["project"].lower() == key)
                out.append({"project_id": None, "name": name, "team": "", "task_type": "", "total_images": 0,
                            "annotated": 0, "progress": 0, "review": {}, "active_model": None,
                            "frames_in_range": frames[key], "contributors": len(people[key])})
        out.sort(key=lambda x: -x["frames_in_range"])
        return {"range": {"from": start, "to": end}, "projects": out}

    @router.get("/manager/projects/{pid}")
    async def project_detail(pid: str, date_from: Optional[str] = Query(None, alias="from"),
                             date_to: Optional[str] = Query(None, alias="to"), _=Depends(require_manager)):
        start, end, end_excl = _range(date_from, date_to)
        p = await db.projects.find_one({"id": pid}, {"_id": 0})
        if not p:
            raise HTTPException(status_code=404, detail="Project not found")
        recs = await _records(start, end, end_excl, project=pid)
        users = await _users({r["user_id"] for r in recs})
        daily, per_user = defaultdict(int), defaultdict(int)
        for r in recs:
            daily[r["date"]] += r["frames"]
            per_user[r["user_id"]] += r["frames"]
        st = {r["_id"] or "unassigned": r["n"] for r in await db.images.aggregate(
            [{"$match": {"project_id": pid}}, {"$group": {"_id": "$review_status", "n": {"$sum": 1}}}]).to_list(None)}
        total = sum(st.values())
        annotated = await db.images.count_documents({"project_id": pid, "review_status": "approved"})
        reviews = await _review_stats(start, end_excl, project=pid)
        contributors = [{"user_id": u, "name": users[u]["name"], "frames": n,
                         "approved": reviews[u]["approved"] if u in reviews else 0,
                         "rejected": reviews[u]["rejected"] if u in reviews else 0}
                        for u, n in per_user.items() if u in users]
        team = await db.teams.find_one({"id": p.get("team_id")}, {"_id": 0, "name": 1})
        models = await db.models.find({"project_id": pid}, {"_id": 0, "name": 1, "model_arch": 1, "status": 1,
                                                            "is_active": 1, "final_mAP": 1, "created_at": 1}
                                      ).sort("created_at", -1).to_list(10)
        runs = await db.pipeline_runs.find({"project_id": pid}, {"_id": 0, "id": 1, "status": 1, "run_type": 1, "created_at": 1}
                                         ).sort("created_at", -1).to_list(10)
        return {
            "range": {"from": start, "to": end},
            "project": {"id": pid, "name": p["name"], "classes": p.get("classes", []), "team": (team or {}).get("name", ""),
                        "task_type": p.get("task_type", ""), "created_at": p.get("created_at")},
            "total_images": total, "annotated": annotated,
            "progress": round(100 * annotated / total) if total else 0,
            "review_pipeline": st,
            "daily": [{"date": d, "frames": daily.get(d, 0)} for d in _days(start, end)],
            "contributors": sorted(contributors, key=lambda x: -x["frames"]),
            "models": models, "deploy_runs": runs,
        }

    @router.get("/manager/work-logs")
    async def all_work_logs(date_from: Optional[str] = Query(None, alias="from"), date_to: Optional[str] = Query(None, alias="to"),
                            user_id: Optional[str] = None, _=Depends(require_manager)):
        start, end, _x = _range(date_from, date_to)
        q = {"date": {"$gte": start, "$lte": end}, "user_id": {"$nin": await _test_user_ids()}}
        if user_id:
            q["user_id"] = user_id
        rows = await db.work_logs.find(q, {"_id": 0, "import_key": 0}).sort([("date", -1), ("created_at", -1)]).to_list(5000)
        users = await _users()
        for r in rows:
            r["person"] = users.get(r["user_id"], {}).get("name", "Unknown")
        return {"range": {"from": start, "to": end}, "work_logs": rows}

    @router.get("/manager/export.csv")
    async def export_csv(date_from: Optional[str] = Query(None, alias="from"), date_to: Optional[str] = Query(None, alias="to"),
                         _=Depends(require_manager)):
        start, end, end_excl = _range(date_from, date_to)
        recs = await _records(start, end, end_excl)
        users = await _users()
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Date", "Person", "Project", "Frames", "Source"])
        for r in sorted(recs, key=lambda r: (r["date"], r["user_id"])):
            if r["user_id"] in users:
                # prefix guards against spreadsheet formula injection from user-entered text
                cell = lambda v: f"'{v}" if isinstance(v, str) and v[:1] in "=+-@" else v
                w.writerow([r["date"], cell(users[r["user_id"]]["name"]), cell(r["project"]), r["frames"], r["source"]])
        return Response(buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="thinkvision_{start}_{end}.csv"'})
