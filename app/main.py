"""Employee Attendance & Analytics API — HROne assignment contract v2.0."""

import os

import re

import calendar

from datetime import date, datetime, time, timedelta, timezone

from decimal import Decimal, ROUND_HALF_UP

from typing import Optional, Literal

from dotenv import load_dotenv

from fastapi import FastAPI, HTTPException, Query, Path

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument

from pymongo.errors import DuplicateKeyError
from bson.timestamp import Timestamp
from bson.objectid import ObjectId

load_dotenv()  # Existing environment variables take precedence over .env.

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")

MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000, tz_aware=False)

db = client[MONGO_DB]

employees = db["employees"]

attendance = db["attendance_logs"]

IST = timezone(timedelta(hours=5, minutes=30))

UTC = timezone.utc

PRESENT_STATUSES = ("PRESENT", "WFH", "ON_DUTY")

ALL_STATUSES = ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY")

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

@app.on_event("startup")

def create_indexes() -> None:

    # Idempotent indexes; unique keys also protect concurrent requests.

    employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_emp_code")

    employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="ix_employee_department_joined")

    attendance.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_emp_date")

    attendance.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_emp")

    attendance.create_index([("date", ASCENDING), ("status", ASCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_status_emp")

# ---------- Helpers ----------

def utc_now_naive() -> datetime:

    return datetime.now(UTC).replace(tzinfo=None)

def as_utc_naive(value: datetime) -> datetime:

    if value.tzinfo is None:

        return value.replace(tzinfo=UTC)

    return value.astimezone(UTC)

def truncate_seconds(value: datetime) -> datetime:

    return value.replace(microsecond=0)

def epoch_ms(value: Optional[datetime]) -> Optional[int]:

    if value is None:

        return None

    return int(as_utc_naive(value).timestamp() * 1000)

def from_epoch_ms(value: Optional[int]) -> Optional[datetime]:

    if value is None:

        return None

    # Validation models ensure this is an integer epoch-millisecond value.

    return datetime.fromtimestamp(value / 1000, UTC).replace(tzinfo=None)

def api_doc(doc: dict) -> dict:

    """Convert stored BSON instants to epoch milliseconds; never expose Mongo _id."""

    result = {k: v for k, v in doc.items() if k != "_id"}

    for field in ("created_at", "punch_in", "punch_out"):

        if field in result:

            result[field] = epoch_ms(result[field])

    # Attendance-only fields must not leak into employee API responses.
    # Attendance records always have a calendar "date" field; employees do not.
    if "date" in result:
        result.setdefault("history", [])
        result.setdefault("late_minutes", 0)
        result.setdefault("overtime_minutes", 0)
        result.setdefault("half_day", False)

    if "history" in result:

        for entry in result["history"]:

            if "at" in entry:

                entry["at"] = epoch_ms(entry["at"])

            for change in entry.get("changes", {}).values():

                # Only punch instants are BSON dates in a history changes object.

                for side in ("from", "to"):

                    if isinstance(change.get(side), datetime):

                        change[side] = epoch_ms(change[side])

    return result

def rounded(value: float, places: int = 2) -> float:

    quantum = Decimal("1").scaleb(-places)

    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))

def valid_date(value: str) -> date:

    try:

        return date.fromisoformat(value)

    except (ValueError, TypeError):

        raise HTTPException(422, "Date must be YYYY-MM-DD")

def valid_month(value: str) -> tuple[date, date]:

    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value or ""):

        raise HTTPException(422, "month must be YYYY-MM")

    year, month = map(int, value.split("-"))

    first = date(year, month, 1)

    last = date(year, month, calendar.monthrange(year, month)[1])

    return first, last

def month_working_days(month: str) -> int:

    first, last = valid_month(month)

    return sum(1 for n in range((last - first).days + 1) if (first + timedelta(days=n)).weekday() < 5)

def shift_start_dt(attendance_day: str, shift_start: str, shift_end: str) -> datetime:

    d = valid_date(attendance_day)

    start_h, start_m = map(int, shift_start.split(":"))

    end_h, end_m = map(int, shift_end.split(":"))

    # A punch before shift_end on an overnight shift belongs to the previous day's shift.

    if (end_h, end_m) <= (start_h, start_m):

        start = datetime.combine(d, time(start_h, start_m), IST)

    else:

        start = datetime.combine(d, time(start_h, start_m), IST)

    return start.astimezone(UTC).replace(tzinfo=None)

def attendance_date_for(punch: datetime, shift_start: str, shift_end: str) -> str:

    local = as_utc_naive(punch).replace(tzinfo=UTC).astimezone(IST)

    start_h, start_m = map(int, shift_start.split(":"))

    end_h, end_m = map(int, shift_end.split(":"))

    # For overnight shifts, after midnight and before shift_end maps to yesterday.

    if (end_h, end_m) <= (start_h, start_m) and (local.hour, local.minute) < (end_h, end_m):

        local -= timedelta(days=1)

    return local.date().isoformat()

def shift_boundary_utc(attendance_day: str, shift_time: str, shift_start: str, shift_end: str, end: bool = False) -> datetime:

    d = valid_date(attendance_day)

    sh, sm = map(int, shift_start.split(":"))

    eh, em = map(int, shift_end.split(":"))

    th, tm = map(int, shift_time.split(":"))

    local_day = d

    if end and (eh, em) <= (sh, sm):

        local_day += timedelta(days=1)

    return datetime.combine(local_day, time(th, tm), IST).astimezone(UTC).replace(tzinfo=None)

def compute_late_minutes(punch_in: datetime, day: str, shift_start: str, shift_end: str) -> int:

    start = shift_boundary_utc(day, shift_start, shift_start, shift_end, end=False)

    seconds = (punch_in - start).total_seconds()

    return int(seconds // 60) if seconds > 600 else 0

def compute_overtime(punch_out: datetime, day: str, shift_start: str, shift_end: str) -> int:

    end = shift_boundary_utc(day, shift_end, shift_start, shift_end, end=True)

    seconds = (punch_out - end).total_seconds()

    minutes = int(seconds // 60)

    return minutes if minutes >= 30 else 0

def compute_derived(status: str, day: str, punch_in: Optional[datetime], punch_out: Optional[datetime], emp: dict) -> dict:

    if status not in PRESENT_STATUSES:

        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}

    late = compute_late_minutes(punch_in, day, emp["shift_start"], emp["shift_end"]) if punch_in else 0

    if punch_in is None or punch_out is None:

        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}

    hours = rounded((punch_out - punch_in).total_seconds() / 3600, 2)

    return {"work_hours": hours, "late_minutes": late,

            "overtime_minutes": compute_overtime(punch_out, day, emp["shift_start"], emp["shift_end"]),

            "half_day": hours < 4.50}

def check_epoch(value):

    if value is not None and (type(value) is not int or value < 100_000_000_000 or value > 4_102_444_800_000):

        raise ValueError("must be an integer epoch timestamp in milliseconds")

    return value

def serialize_change_value(field: str, value):

    return epoch_ms(value) if field in ("punch_in", "punch_out") and value is not None else value

# ---------- Request models ----------

class EmployeeCreate(BaseModel):

    model_config = ConfigDict(extra="forbid")

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")

    name: str = Field(min_length=1, max_length=100)

    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

    department: str = Field(min_length=1, max_length=50)

    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")

    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")

    joined_on: str

    @field_validator("joined_on")

    @classmethod

    def validate_joined_on(cls, v):

        date.fromisoformat(v)

        return v

    @model_validator(mode="after")

    def different_shift_times(self):

        if self.shift_start == self.shift_end:

            raise ValueError("shift_start must differ from shift_end")

        return self

class PunchInRequest(BaseModel):

    model_config = ConfigDict(extra="forbid")

    emp_code: str

    punched_at: Optional[int] = None

    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at")

    @classmethod

    def validate_punched_at(cls, v): return check_epoch(v)

class PunchOutRequest(BaseModel):

    model_config = ConfigDict(extra="forbid")

    emp_code: str

    punched_at: Optional[int] = None

    @field_validator("punched_at")

    @classmethod

    def validate_punched_at(cls, v): return check_epoch(v)

class RegularizeRequest(BaseModel):

    model_config = ConfigDict(extra="forbid")

    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None

    punch_in: Optional[int] = None

    punch_out: Optional[int] = None

    reason: str = Field(min_length=5, max_length=200)

    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")

    @classmethod

    def validate_punch_epoch(cls, v): return check_epoch(v)

# ---------- System / employee endpoints ----------

@app.get("/health")

def health():

    try:

        client.admin.command("ping")

        return {"status": "ok"}

    except Exception:

        raise HTTPException(503, "MongoDB unavailable")

@app.post("/employees", status_code=201)

def create_employee(body: EmployeeCreate):

    doc = body.model_dump()

    doc["created_at"] = utc_now_naive()

    try:

        employees.insert_one(doc)

    except DuplicateKeyError:

        raise HTTPException(409, "emp_code already exists")

    return api_doc(doc)

@app.get("/employees")

def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):

    query = {"department": department} if department is not None else {}

    total = employees.count_documents(query)

    docs = employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)

    return {"items": [api_doc(d) for d in docs], "total": total, "page": page, "page_size": page_size}

# ---------- Attendance endpoints ----------

@app.post("/attendance/punch-in", status_code=201)

def punch_in(body: PunchInRequest):

    emp = employees.find_one({"emp_code": body.emp_code}, {"_id": 0})

    if not emp:

        raise HTTPException(404, "Unknown employee")

    ts = truncate_seconds(from_epoch_ms(body.punched_at) if body.punched_at is not None else utc_now_naive())

    day = attendance_date_for(ts, emp["shift_start"], emp["shift_end"])

    doc = {"emp_code": body.emp_code, "date": day, "status": body.status, "punch_in": ts,

           "punch_out": None, "work_hours": None,

           "late_minutes": compute_late_minutes(ts, day, emp["shift_start"], emp["shift_end"]),

           "overtime_minutes": 0, "half_day": False, "history": []}

    try:

        attendance.insert_one(doc)

    except DuplicateKeyError:

        raise HTTPException(409, "Already punched in for this attendance date")

    return api_doc(doc)

@app.post("/attendance/punch-out")

def punch_out(body: PunchOutRequest):

    emp = employees.find_one({"emp_code": body.emp_code}, {"_id": 0})

    if not emp:

        raise HTTPException(404, "Unknown employee")

    ts = truncate_seconds(from_epoch_ms(body.punched_at) if body.punched_at is not None else utc_now_naive())

    # Search the latest open punch not later than the supplied punch-out instant.

    rec = attendance.find_one({"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}, "punch_out": None}, sort=[("punch_in", DESCENDING)])

    if not rec:

        closed = attendance.find_one({"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}, "punch_out": {"$ne": None}}, sort=[("punch_in", DESCENDING)])

        if closed:

            raise HTTPException(409, "That attendance record is already punched out")

        raise HTTPException(404, "No punch-in found")

    if ts <= rec["punch_in"] or ts > rec["punch_in"] + timedelta(hours=24):

        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours")

    derived = compute_derived(rec["status"], rec["date"], rec["punch_in"], ts, emp)

    updated = attendance.find_one_and_update(

        {"_id": rec["_id"], "punch_out": None},

        {"$set": {"punch_out": ts, **derived}}, return_document=ReturnDocument.AFTER)

    if updated is None:

        raise HTTPException(409, "Concurrent punch-out already completed")

    return api_doc(updated)

@app.get("/attendance")

def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,

                    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,

                    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):

    if date_from: valid_date(date_from)

    if date_to: valid_date(date_to)

    if date_from and date_to and date_from > date_to:

        raise HTTPException(422, "date_from must be <= date_to")

    query = {}

    if emp_code: query["emp_code"] = emp_code

    if status: query["status"] = status

    if date_from or date_to:

        query["date"] = {}

        if date_from: query["date"]["$gte"] = date_from

        if date_to: query["date"]["$lte"] = date_to

    total = attendance.count_documents(query)

    docs = attendance.find(query, {"_id": 0}).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size)

    return {"items": [api_doc(d) for d in docs], "total": total, "page": page, "page_size": page_size}

@app.patch("/attendance/{emp_code}/{date}")

def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):

    valid_date(date)

    emp = employees.find_one({"emp_code": emp_code}, {"_id": 0})

    if not emp:

        raise HTTPException(404, "Unknown employee")

    rec = attendance.find_one({"emp_code": emp_code, "date": date})

    if not rec:

        raise HTTPException(404, "Attendance record not found")

    status = body.status if body.status is not None else rec["status"]

    if status in ("ABSENT", "LEAVE") and (body.punch_in is not None or body.punch_out is not None):

        raise HTTPException(422, "ABSENT and LEAVE cannot have punch times")

    pin = from_epoch_ms(body.punch_in) if body.punch_in is not None else rec.get("punch_in")

    pout = from_epoch_ms(body.punch_out) if body.punch_out is not None else rec.get("punch_out")

    if status in ("ABSENT", "LEAVE"):

        pin = pout = None

    if status in PRESENT_STATUSES and pin is None:

        raise HTTPException(422, "Presence status requires punch_in")

    if pin is not None:

        pin = truncate_seconds(pin)

        if attendance_date_for(pin, emp["shift_start"], emp["shift_end"]) != date:

            raise HTTPException(422, "punch_in must belong to the record attendance date")

    if pout is not None:

        pout = truncate_seconds(pout)

        if pin is None or pout <= pin or pout > pin + timedelta(hours=24):

            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    derived = compute_derived(status, date, pin, pout, emp)

    final_values = {"status": status, "punch_in": pin, "punch_out": pout, **derived}

    changes = {}

    for key, new_value in final_values.items():

        old_value = rec.get(key)

        if old_value != new_value:

            changes[key] = {"from": old_value, "to": new_value}

    if not changes:

        raise HTTPException(422, "Request does not change this record")

    history_entry = {"at": utc_now_naive(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # Optimistic compare-and-swap prevents lost concurrent updates/history.

    selector = {"_id": rec["_id"]}

    selector.update({k: rec.get(k) for k in ("status", "punch_in", "punch_out")})

    updated = attendance.find_one_and_update(selector, {"$set": final_values, "$push": {"history": history_entry}}, return_document=ReturnDocument.AFTER)

    if updated is None:

        raise HTTPException(409, "Record changed concurrently; retry the correction")

    return api_doc(updated)

# ---------- Analytics: all analytics are MongoDB aggregation pipelines ----------

def monthly_match(month: str, emp_code: Optional[str] = None):

    first, last = valid_month(month)

    q = {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}

    if emp_code is not None: q["emp_code"] = emp_code

    return q

@app.get("/analytics/employees/{emp_code}/monthly")

def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):

    if not employees.find_one({"emp_code": emp_code}, {"_id": 1}):

        raise HTTPException(404, "Unknown employee")

    pipeline = [

        {"$match": monthly_match(month, emp_code)},

        {"$group": {"_id": None,

            "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENT_STATUSES)]}, {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]}]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}},

            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},

            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},

            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},

            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}}}

    ]

    rows = list(attendance.aggregate(pipeline))

    r = rows[0] if rows else {}

    working = month_working_days(month)

    present = float(r.get("present_days", 0))

    return {"emp_code": emp_code, "month": month, "working_days": working, "present_days": present,

            "leave_days": int(r.get("leave_days", 0)), "late_count": int(r.get("late_count", 0)),

            "total_late_minutes": int(r.get("total_late_minutes", 0)),

            "total_overtime_minutes": int(r.get("total_overtime_minutes", 0)),

            "attendance_pct": rounded(present / working * 100, 2) if working else None}

@app.get("/analytics/departments/summary")

def department_summary(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: Optional[str] = None):

    first, last = valid_month(month)

    emp_match = {"joined_on": {"$lte": last.isoformat()}}

    if department is not None: emp_match["department"] = department

    pipeline = [

        {"$match": emp_match},

        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [

            {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", first.isoformat()]}, {"$lte": ["$date", last.isoformat()]}]}}}

        ], "as": "logs"}},

        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},

        {"$group": {"_id": "$department", "headcount": {"$sum": {"$cond": [{"$eq": ["$logs", None]}, 0, 0]}},

            "codes": {"$addToSet": "$emp_code"},

            "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENT_STATUSES)]}, {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, [2, 3, 4, 5, 6]]}]}, {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1]}, 0]}},

            "work_hours": {"$push": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENT_STATUSES)]}, {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]}]}, "$logs.work_hours", "$$REMOVE"]}},

            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},

            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},

            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},

            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}}}},

        {"$project": {"department": "$_id", "headcount": {"$size": "$codes"}, "present_days": 1,

            "avg_work_hours": {"$cond": [{"$gt": [{"$size": "$work_hours"}, 0]}, {"$avg": "$work_hours"}, None]},

            "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1, "_id": 0}},

        {"$sort": {"department": 1}}

    ]

    rows = list(employees.aggregate(pipeline, allowDiskUse=True))

    # A person who joined after month-end was excluded above; employees with no logs remain in the department group.

    items = []

    for row in rows:

        if row["headcount"] <= 0: continue

        if row.get("avg_work_hours") is not None: row["avg_work_hours"] = rounded(row["avg_work_hours"], 2)

        row["present_days"] = float(row.get("present_days", 0))

        for k in ("late_count", "total_late_minutes", "leave_count", "on_duty_count"): row[k] = int(row.get(k, 0))

        items.append(row)

    return {"month": month, "items": items}

@app.get("/analytics/leaderboard/late")

def late_leaderboard(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),

                     limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):

    match = monthly_match(month)

    pipeline = [

        {"$match": {**match, "late_minutes": {"$gt": 0}}},

        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},

        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},

        {"$unwind": "$employee"},

    ]

    if department is not None: pipeline.append({"$match": {"employee.department": department}})

    pipeline += [

        {"$sort": {"total_late_minutes": -1, "_id": 1}},

        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},

        {"$match": {"rank": {"$lte": limit}}},

        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}},

        {"$sort": {"total_late_minutes": -1, "emp_code": 1}}

    ]

    return {"month": month, "items": list(attendance.aggregate(pipeline, allowDiskUse=True))}

@app.get("/analytics/departments/{department}/trend")

def department_trend(department: str, from_date: str = Query(..., alias="from"), to_date: str = Query(..., alias="to")):

    start, end = valid_date(from_date), valid_date(to_date)

    if end < start: raise HTTPException(422, "to must be >= from")

    if (end - start).days + 1 > 92: raise HTTPException(422, "Date range must not exceed 92 days")

    if not employees.find_one({"department": department}, {"_id": 1}): raise HTTPException(404, "Unknown department")

    # Aggregation converts the stored calendar string to a BSON date, fills missing calendar rows with $densify,

    # computes daily counts and moving average in MongoDB (no Python date-by-date loop).

    pipeline = [

        {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},

        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "emp"}},

        {"$unwind": "$emp"}, {"$match": {"emp.department": department}},

        {"$set": {"dayDate": {"$dateFromString": {"dateString": "$date"}}}},

        {"$group": {"_id": "$dayDate",

            "present_count": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENT_STATUSES)]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}},

            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},

        {"$densify": {"field": "_id", "range": {"step": 1, "unit": "day", "bounds": [datetime.combine(start, time.min), datetime.combine(end, time.min)]}}},

        {"$set": {"present_count": {"$ifNull": ["$present_count", 0]}, "late_count": {"$ifNull": ["$late_count", 0]}, "dateStr": {"$dateToString": {"format": "%Y-%m-%d", "date": "$_id"}}}},

        {"$lookup": {"from": "employees", "let": {"d": "$dateStr"}, "pipeline": [

            {"$match": {"$expr": {"$and": [{"$eq": ["$department", department]}, {"$lte": ["$joined_on", "$$d"]}]}}},

            {"$count": "n"}

        ], "as": "hc"}},

        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]}, "is_working_day": {"$and": [{"$gte": [{"$dayOfWeek": "$_id"}, 2]}, {"$lte": [{"$dayOfWeek": "$_id"}, 6]}]}}},

        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, {"$round": [{"$divide": ["$present_count", "$headcount"]}, 4]}, None]}}},

        {"$setWindowFields": {"sortBy": {"_id": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},

        {"$project": {"_id": 0, "date": "$dateStr", "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": {"$round": ["$moving_avg_7d", 4]}}},

        {"$sort": {"date": 1}}

    ]

    rows = list(attendance.aggregate(pipeline, allowDiskUse=True))

    # MongoDB $dayOfWeek: Sunday=1, Saturday=7; Mon-Fri is 2..6.

    for row in rows:

        d = valid_date(row["date"])

        row["is_working_day"] = d.weekday() < 5

        if not row["is_working_day"] or row["headcount"] == 0: row["attendance_rate"] = None

        elif row["attendance_rate"] is not None: row["attendance_rate"] = rounded(row["attendance_rate"], 4)

        if row.get("moving_avg_7d") is not None: row["moving_avg_7d"] = rounded(row["moving_avg_7d"], 4)

    return {"department": department, "items": rows}

# ---------- Explain endpoint ----------

def explain_query(find_query, sort_spec, skip, limit):

    return attendance.find(find_query).sort(sort_spec).skip(skip).limit(limit).explain()

def json_safe(value):
    """Recursively convert BSON/PyMongo values into JSON-serializable values."""
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, datetime):
        return epoch_ms(value)
    if isinstance(value, ObjectId):
        return str(value)
    # Explain output includes BSON Timestamp objects, which FastAPI cannot encode.
    if isinstance(value, Timestamp):
        return {"time": int(value.time), "inc": int(value.inc)}
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    return value

@app.get("/admin/explain/{endpoint}")

def explain_endpoint(endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],

                     emp_code: Optional[str] = None, month: Optional[str] = None, department: Optional[str] = None,

                     limit: int = Query(10, ge=1, le=50), date_from: Optional[str] = None, date_to: Optional[str] = None,

                     status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,

                     from_date: Optional[str] = Query(None, alias="from"), to_date: Optional[str] = Query(None, alias="to"),

                     page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):

    if endpoint == "attendance_list":

        if date_from: valid_date(date_from)

        if date_to: valid_date(date_to)

        q = {}

        if emp_code: q["emp_code"] = emp_code

        if status: q["status"] = status

        if date_from or date_to:

            q["date"] = {}

            if date_from: q["date"]["$gte"] = date_from

            if date_to: q["date"]["$lte"] = date_to

        exp = explain_query(q, [("date", DESCENDING), ("emp_code", ASCENDING)], (page - 1) * page_size, page_size)

        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": json_safe(exp)}

    if endpoint == "employee_monthly":

        if not emp_code or not month: raise HTTPException(422, "emp_code and month are required")

        pipeline = [{"$match": monthly_match(month, emp_code)}, {"$group": {"_id": "$emp_code", "n": {"$sum": 1}}}]

        exp = attendance.database.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")

        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": json_safe(exp)}

    if endpoint == "department_summary":

        if not month: raise HTTPException(422, "month is required")

        first, last = valid_month(month)

        q = {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}

        if department:

            exp = attendance.find(q).explain()

        else:

            exp = attendance.find(q).explain()

        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": json_safe(exp)}

    if endpoint == "late_leaderboard":

        if not month: raise HTTPException(422, "month is required")

        first, last = valid_month(month)

        q = {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}

        exp = attendance.find(q).explain()

        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": json_safe(exp)}

    if endpoint == "department_trend":

        if not department or not from_date or not to_date: raise HTTPException(422, "department, from and to are required")

        start, end = valid_date(from_date), valid_date(to_date)

        if end < start or (end - start).days + 1 > 92: raise HTTPException(422, "Invalid trend range")

        q = {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}

        exp = attendance.find(q).explain()

        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": json_safe(exp)}

    raise HTTPException(422, "Unsupported endpoint")
