"""Daily work log (mirrors the team's spreadsheet) with xlsx/csv import."""
import csv
import hashlib
import io
import uuid
from datetime import date as date_cls, datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

router = APIRouter()

# Normalised spreadsheet header -> field name.
HEADER_MAP = {
    "date": "date",
    "day": "day",
    "product": "product",
    "project": "project_name",
    "task": "task",
    "number of frames annotated today": "frames",
    "frames": "frames",
    "verified by": "verified_by",
    "notes (if any)": "notes",
    "notes": "notes",
}


class WorkLogIn(BaseModel):
    date: str  # YYYY-MM-DD
    product: str = ""
    project_name: str
    task: str = ""
    frames: int = Field(0, ge=0)
    verified_by: str = ""
    notes: str = ""


class WorkLogPatch(BaseModel):
    date: Optional[str] = None
    product: Optional[str] = None
    project_name: Optional[str] = None
    task: Optional[str] = None
    frames: Optional[int] = Field(None, ge=0)
    verified_by: Optional[str] = None
    notes: Optional[str] = None


def _parse_date(value) -> str:
    """Accepts datetime/date cells and ISO or day-first strings (18/9/2026)."""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date_cls):
        return value.isoformat()
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"unrecognised date '{text}'")


def _day_name(iso: str) -> str:
    return date_cls.fromisoformat(iso).strftime("%A")


def _read_rows(filename: str, data: bytes):
    """Yield (row_number, {field: value}) for every non-empty data row."""
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook
        ws = load_workbook(io.BytesIO(data), read_only=True, data_only=True).active
        raw = [list(r) for r in ws.iter_rows(values_only=True)]
    elif name.endswith(".csv"):
        raw = list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))
    else:
        raise HTTPException(status_code=400, detail="Upload an .xlsx or .csv file")
    header_idx = next(
        (i for i, r in enumerate(raw) if any(str(c or "").strip().lower() == "date" for c in r)), None
    )
    if header_idx is None:
        raise HTTPException(status_code=400, detail="No header row with a 'Date' column found")
    cols = {}
    for j, c in enumerate(raw[header_idx]):
        key = " ".join(str(c or "").split()).lower()  # header cells contain line breaks
        if key in HEADER_MAP:
            cols[j] = HEADER_MAP[key]
    for i, r in enumerate(raw[header_idx + 1:], start=header_idx + 2):
        row = {cols[j]: r[j] for j in cols if j < len(r)}
        yield i, row


def register(s):
    db = s.db
    get_current_user = s.get_current_user

    def _clean(d: dict) -> dict:
        iso = _parse_date(d["date"])
        return {
            "date": iso,
            "day": _day_name(iso),
            "product": str(d.get("product") or "").strip(),
            "project_name": str(d.get("project_name") or "").strip(),
            "task": str(d.get("task") or "").strip(),
            "frames": int(d.get("frames") or 0),
            "verified_by": str(d.get("verified_by") or "").strip(),
            "notes": str(d.get("notes") or "").strip(),
        }

    @router.post("/work-logs")
    async def create_work_log(payload: WorkLogIn, current=Depends(get_current_user)):
        try:
            doc = _clean(payload.model_dump())
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if not doc["project_name"]:
            raise HTTPException(status_code=400, detail="Project is required")
        doc.update({"id": str(uuid.uuid4()), "user_id": current["id"], "source": "manual",
                    "created_at": datetime.now(timezone.utc).isoformat()})
        await db.work_logs.insert_one(doc)
        doc.pop("_id", None)
        return doc

    @router.get("/work-logs")
    async def list_my_work_logs(current=Depends(get_current_user)):
        return await db.work_logs.find({"user_id": current["id"]}, {"_id": 0}).sort(
            [("date", -1), ("created_at", -1)]).to_list(2000)

    async def _own(log_id: str, current):
        log = await db.work_logs.find_one({"id": log_id, "user_id": current["id"]})
        if not log:
            raise HTTPException(status_code=404, detail="Not found")
        return log

    @router.patch("/work-logs/{log_id}")
    async def update_work_log(log_id: str, payload: WorkLogPatch, current=Depends(get_current_user)):
        log = await _own(log_id, current)
        merged = {**log, **payload.model_dump(exclude_none=True)}
        try:
            doc = _clean(merged)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        await db.work_logs.update_one({"id": log_id}, {"$set": doc})
        return {"ok": True}

    @router.delete("/work-logs/{log_id}")
    async def delete_work_log(log_id: str, current=Depends(get_current_user)):
        await _own(log_id, current)
        await db.work_logs.delete_one({"id": log_id})
        return {"ok": True}

    @router.post("/work-logs/import")
    async def import_work_logs(file: UploadFile = File(...), current=Depends(get_current_user)):
        data = await file.read()
        if len(data) > 5 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="File too large (max 5 MB)")
        imported, skipped, errors = 0, 0, []
        seen = {}  # occurrences of identical rows within this file are all kept
        for rownum, raw in _read_rows(file.filename, data):
            if not any(str(v or "").strip() for v in raw.values()):
                continue
            try:
                doc = _clean(raw)
                if not doc["project_name"]:
                    continue  # spacer rows carry only a date
            except (ValueError, TypeError) as e:
                errors.append({"row": rownum, "error": str(e)})
                continue
            sig = "|".join(str(doc[k]) for k in ("date", "project_name", "task", "frames", "verified_by"))
            seen[sig] = seen.get(sig, 0) + 1
            key = hashlib.sha1(f"{current['id']}|{sig}|{seen[sig]}".encode()).hexdigest()
            if await db.work_logs.find_one({"import_key": key}, {"_id": 1}):
                skipped += 1
                continue
            doc.update({"id": str(uuid.uuid4()), "user_id": current["id"], "source": "import",
                        "import_key": key, "created_at": datetime.now(timezone.utc).isoformat()})
            await db.work_logs.insert_one(doc)
            imported += 1
        return {"imported": imported, "skipped_duplicates": skipped, "errors": errors[:20]}
