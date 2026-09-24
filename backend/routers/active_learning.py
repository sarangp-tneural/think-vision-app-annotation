"""Active-learning queue endpoint (extracted from server.py)."""
from fastapi import APIRouter, Depends, HTTPException

router = APIRouter()


def register(s):
    db = s.db
    get_current_user = s.get_current_user

    @router.get("/projects/{pid}/active-learning-queue")
    async def active_learning_queue(pid: str, limit: int = 20, current=Depends(get_current_user)):
        """Return unlabeled/rejected images ranked by model uncertainty."""
        await s._project_access_check(pid, current["id"])
        project = await db.projects.find_one({"id": pid})
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")

        candidates = await db.images.find(
            {"project_id": pid, "review_status": {"$in": ["unassigned", "assigned", "rejected", None]}},
            {"_id": 0, "id": 1, "filename": 1, "storage_path": 1, "annotations": 1,
             "review_status": 1, "created_at": 1, "assigned_to": 1},
        ).sort("created_at", -1).limit(500).to_list(500)

        active = await db.models.find_one({"project_id": pid, "is_active": True, "status": "trained"})
        settings = project.get("settings", {})
        conf_threshold = settings.get("confidence_threshold", s.CONFIDENCE_THRESHOLD)

        scored = []
        if active and active.get("weights_path"):
            for img in candidates[:limit * 3]:
                existing = img.get("annotations", []) or []
                if existing:
                    score = s._uncertainty_score(existing, conf_threshold)
                else:
                    score = 0.85
                scored.append({
                    "image_id": img["id"],
                    "filename": img.get("filename", ""),
                    "storage_path": img.get("storage_path", ""),
                    "review_status": img.get("review_status") or "unassigned",
                    "annotation_count": len(existing),
                    "uncertainty": round(score, 3),
                    "reason": ("no_detections" if not existing else "low_confidence"),
                })
        else:
            for img in candidates[:limit * 2]:
                existing = img.get("annotations", []) or []
                score = 1.0 if not existing else 0.5
                scored.append({
                    "image_id": img["id"],
                    "filename": img.get("filename", ""),
                    "storage_path": img.get("storage_path", ""),
                    "review_status": img.get("review_status") or "unassigned",
                    "annotation_count": len(existing),
                    "uncertainty": round(score, 3),
                    "reason": "no_active_model" if not existing else "labeled_no_model",
                })

        scored.sort(key=lambda x: x["uncertainty"], reverse=True)
        return {
            "has_active_model": bool(active),
            "total_candidates": len(candidates),
            "items": scored[:limit],
        }
