"""
Employee Attendance & Analytics API.

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB. A local .env is loaded for convenience;
      real environment variables take priority over it.

Layout (one file, as the assignment requires):
  1. configuration and time helpers
  2. business rules R1-R5 as pure functions
  3. request models
  4. aggregation pipeline builders (shared by the endpoints and /admin/explain)
  5. endpoints
"""
import calendar
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional, TypeAlias

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("attendance")

# --------------------------------------------------------------------------- #
# 1. Configuration and time helpers
# --------------------------------------------------------------------------- #
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

GRACE_SECONDS = 10 * 60            # R2: late only if strictly more than this after shift start
OVERTIME_MIN_MINUTES = 30          # R3
HALF_DAY_HOURS = Decimal("4.50")   # R5
MAX_PUNCH_GAP = timedelta(hours=24)
MAX_TREND_DAYS = 92

PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]           # R6
NON_PRESENCE = ["ABSENT", "LEAVE"]
WEEKDAY_DOW = [2, 3, 4, 5, 6]                      # MongoDB $dayOfWeek: 1 = Sunday ... 7 = Saturday
TIME_FIELDS = ("punch_in", "punch_out")
CHANGE_FIELDS = ("status", "punch_in", "punch_out", "work_hours",
                 "late_minutes", "overtime_minutes", "half_day")
EMPLOYEE_FIELDS = ("emp_code", "name", "email", "department",
                   "shift_start", "shift_end", "joined_on")

StatusT: TypeAlias = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceT: TypeAlias = Literal["PRESENT", "WFH", "ON_DUTY"]
ExplainT: TypeAlias = Literal["attendance_list", "employee_monthly", "department_summary",
                   "late_leaderboard", "department_trend"]

DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"
MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"
HHMM_PATTERN = r"^([01]\d|2[0-3]):[0-5]\d$"

# Contract: instants are epoch milliseconds; seconds-looking values are rejected.
EPOCH_MIN_MS = 100_000_000_000
EPOCH_MAX_MS = 4_102_444_800_000
EpochMs: TypeAlias = Annotated[StrictInt, Field(ge=EPOCH_MIN_MS, le=EPOCH_MAX_MS)]

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]


def reject(message: str, loc: str = "body") -> None:
    """Raise a 422 with the same shape as FastAPI's own validation errors."""
    raise HTTPException(422, [{"loc": [loc], "msg": message, "type": "value_error"}])


def aware(value: datetime) -> datetime:
    """PyMongo may hand back naive datetimes; they are always UTC."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def to_ms(value: Optional[datetime]) -> Optional[int]:
    return None if value is None else (aware(value) - EPOCH) // timedelta(milliseconds=1)


def instant(ms: int) -> datetime:
    """API epoch milliseconds -> UTC datetime truncated to whole seconds (R1)."""
    return EPOCH + timedelta(milliseconds=ms - ms % 1000)


def now_seconds() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def now_millis() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(microsecond=now.microsecond // 1000 * 1000)


def parse_day(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"'{text}' is not a valid calendar date") from exc


def day_or_422(text: str, loc: str = "query") -> date:
    try:
        return parse_day(text)
    except ValueError as exc:
        reject(str(exc), loc)


def month_bounds(month: str) -> tuple[str, str]:
    year, mon = map(int, month.split("-"))
    last = calendar.monthrange(year, mon)[1]
    return f"{month}-01", f"{month}-{last:02d}"


def round_half_up(value, places: int) -> Optional[float]:
    if value is None:
        return None
    return float(Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- #
# 2. Business rules (pure functions, unit-tested)
# --------------------------------------------------------------------------- #
def hhmm(text: str) -> time:
    hours, minutes = text.split(":")
    return time(int(hours), int(minutes))


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return hhmm(shift_end) <= hhmm(shift_start)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> date:
    """R1: the IST calendar date of the punch-in, with the overnight exception."""
    local = punch_in.astimezone(IST)
    day = local.date()
    if is_overnight(shift_start, shift_end) and local.time() < hhmm(shift_end):
        day -= timedelta(days=1)
    return day


def shift_start_at(day: date, shift_start: str) -> datetime:
    return datetime.combine(day, hhmm(shift_start), tzinfo=IST)


def shift_end_at(day: date, shift_start: str, shift_end: str) -> datetime:
    end_day = day + timedelta(days=1) if is_overnight(shift_start, shift_end) else day
    return datetime.combine(end_day, hhmm(shift_end), tzinfo=IST)


def late_minutes(punch_in: datetime, start: datetime) -> int:
    """R2: late only if strictly more than 10:00 after shift start; floor minutes since shift start."""
    seconds = int((punch_in - start).total_seconds())
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def overtime_minutes(punch_out: datetime, end: datetime) -> int:
    """R3: floor minutes after shift end, counted only when at least 30."""
    minutes = max(0, int((punch_out - end).total_seconds())) // 60
    return minutes if minutes >= OVERTIME_MIN_MINUTES else 0


def work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, rounded to 2 decimals half-up."""
    seconds = int((punch_out - punch_in).total_seconds())
    hours = (Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(hours)


def is_half_day(hours: float) -> bool:
    """R5: tested against the rounded value."""
    return Decimal(str(hours)) < HALF_DAY_HOURS


def derive(day: date, emp: dict, punch_in: Optional[datetime], punch_out: Optional[datetime]) -> dict:
    """The four stored derived fields for one record (R2-R5)."""
    if punch_in is None:
        return {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    late = late_minutes(punch_in, shift_start_at(day, emp["shift_start"]))
    if punch_out is None:
        return {"late_minutes": late, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    hours = work_hours(punch_in, punch_out)
    return {
        "late_minutes": late,
        "work_hours": hours,
        "overtime_minutes": overtime_minutes(punch_out, shift_end_at(day, emp["shift_start"], emp["shift_end"])),
        "half_day": is_half_day(hours),
    }


def working_days(first_iso: str, last_iso: str) -> int:
    """R7: Monday-Friday between two dates inclusive. Calendar arithmetic only, no data."""
    first, last = parse_day(first_iso), parse_day(last_iso)
    if first > last:
        return 0
    return sum(1 for n in range((last - first).days + 1) if (first + timedelta(n)).weekday() < 5)


# --------------------------------------------------------------------------- #
# 3. Request models
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM_PATTERN)
    shift_end: str = Field(default="18:30", pattern=HHMM_PATTERN)
    joined_on: str = Field(pattern=DATE_PATTERN)

    @field_validator("joined_on")
    @classmethod
    def joined_on_is_real_date(cls, value: str) -> str:
        parse_day(value)
        return value

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PresenceT = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeRequest(BaseModel):
    """Only these fields can be changed. Derived fields sent by a client are ignored."""
    status: Optional[StatusT] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# 4. Serialisation
# --------------------------------------------------------------------------- #
def serialize_employee(doc: dict) -> dict:
    return {**{f: doc[f] for f in EMPLOYEE_FIELDS}, "created_at": to_ms(doc["created_at"])}


def _change_value(field: str, value):
    return to_ms(value) if field in TIME_FIELDS else value


def serialize_history(entry: dict) -> dict:
    return {
        "at": to_ms(entry["at"]),
        "by": entry["by"],
        "reason": entry["reason"],
        "changes": {
            field: {"from": _change_value(field, c.get("from")), "to": _change_value(field, c.get("to"))}
            for field, c in entry["changes"].items()
        },
    }


def serialize_record(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")),
        "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": [serialize_history(h) for h in doc.get("history") or []],
    }


# --------------------------------------------------------------------------- #
# 5. Aggregation pipelines
#    Each builder is used by the endpoint AND by /admin/explain, so the explain
#    output describes exactly the query the endpoint runs.
# --------------------------------------------------------------------------- #
def _weekday_cond(date_expr) -> dict:
    """True when a 'YYYY-MM-DD' string falls on Monday-Friday."""
    return {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": date_expr}}}, WEEKDAY_DOW]}


def _presence_credit(status_expr, half_day_expr) -> dict:
    """0.5 for a half-day presence record, 1 for any other presence record, 0 otherwise."""
    return {"$cond": [
        {"$in": [status_expr, PRESENCE]},
        {"$cond": [{"$eq": [{"$ifNull": [half_day_expr, False]}, True]}, 0.5, 1]},
        0,
    ]}


def employee_monthly_pipeline(emp_code: str, start: str, end: str) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [
                _weekday_cond("$date"), _presence_credit("$status", "$half_day"), 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def department_summary_pipeline(start: str, end: str, department: Optional[str]) -> list:
    """Starts from employees (headcount per R9), then joins each employee's logs for the month."""
    match: dict = {"joined_on": {"$lte": end}}
    if department is not None:
        match["department"] = department
    return [
        {"$match": match},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [{"$match": {"date": {"$gte": start, "$lte": end}}}],
            "as": "logs",
        }},
        # preserveNull keeps employees with zero logs, so they still count in headcount.
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "codes": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {"$cond": [
                _weekday_cond("$logs.date"), _presence_credit("$logs.status", "$logs.half_day"), 0]}},
            "avg_work_hours": {"$avg": {"$cond": [
                {"$in": ["$logs.status", PRESENCE]}, "$logs.work_hours", None]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
        }},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": {"$size": "$codes"},
            "present_days": 1,
            "avg_work_hours": 1,
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1,
        }},
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"department": 1}},
    ]


def leaderboard_pipeline(start: str, end: str, limit: int, department: Optional[str]) -> list:
    pipeline: list = [
        {"$match": {"date": {"$gte": start, "$lte": end}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        }},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        # Logs whose employee does not exist are ignored (inner join).
        {"$unwind": "$emp"},
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline += [
        # Standard competition ranking: ties share a rank, the next rank is skipped (1, 2, 2, 4).
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        # limit applies after ranking, so every row tied at the cutoff is kept.
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": "$emp.name",
            "department": "$emp.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
    ]
    return pipeline


def department_trend_pipeline(department: str, start: date, end: date) -> list:
    """Runs at database level. $documents + $densify creates one row per calendar day, gaps included."""
    return [
        {"$documents": [
            {"day": datetime.combine(start, time(), tzinfo=timezone.utc)},
            {"day": datetime.combine(end, time(), tzinfo=timezone.utc)},
        ]},
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": "full"}}},
        {"$project": {
            "_id": 0,
            "day": 1,
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$day"}},
            "is_working_day": {"$in": [{"$dayOfWeek": "$day"}, WEEKDAY_DOW]},
        }},
        {"$lookup": {
            "from": "employees",
            "let": {"d": "$date"},
            "pipeline": [
                {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$d"]}}},
                {"$count": "n"},
            ],
            "as": "hc",
        }},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"d": "$date"},
            "pipeline": [
                {"$match": {"$expr": {"$eq": ["$date", "$$d"]}}},
                {"$lookup": {"from": "employees", "localField": "emp_code",
                             "foreignField": "emp_code", "as": "e"}},
                {"$unwind": "$e"},
                {"$match": {"e.department": department}},
                {"$project": {"_id": 0, "status": 1, "half_day": 1, "late_minutes": 1}},
            ],
            "as": "logs",
        }},
        {"$project": {
            "date": 1,
            "is_working_day": 1,
            "headcount": {"$ifNull": [{"$first": "$hc.n"}, 0]},
            # R7: present_days counts Monday-Friday only, so weekend presence adds nothing here.
            "present_count": {"$cond": [
                {"$eq": ["$is_working_day", True]},
                {"$sum": {"$map": {
                    "input": "$logs", "as": "l",
                    "in": _presence_credit("$$l.status", "$$l.half_day"),
                }}},
                0,
            ]},
            "late_count": {"$size": {"$filter": {
                "input": "$logs", "as": "l",
                "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]},
            }}},
        }},
        {"$project": {
            "date": 1,
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            # 4 decimals half-up: floor(x * 10^4 + 0.5) / 10^4. Null when not a working day or no headcount.
            "attendance_rate": {"$cond": [
                {"$and": [{"$eq": ["$is_working_day", True]}, {"$gt": ["$headcount", 0]}]},
                {"$divide": [
                    {"$floor": {"$add": [{"$multiply": [
                        {"$divide": ["$present_count", "$headcount"]}, 10000]}, 0.5]}},
                    10000,
                ]},
                None,
            ]},
        }},
        {"$sort": {"date": 1}},
        # Window of the current row and up to 6 preceding rows inside the range; nulls are ignored.
        {"$setWindowFields": {
            "sortBy": {"date": 1},
            "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}},
        }},
        {"$project": {
            "_id": 0,
            "date": 1,
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": 1,
            "moving_avg_7d": 1,
        }},
    ]


def attendance_query(emp_code, date_from, date_to, status, page, page_size):
    if date_from is not None:
        day_or_422(date_from)
    if date_to is not None:
        day_or_422(date_to)
    if date_from is not None and date_to is not None and date_from > date_to:
        reject("date_from must not be after date_to")
    query: dict = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if status is not None:
        query["status"] = status
    date_range: dict = {}
    if date_from is not None:
        date_range["$gte"] = date_from
    if date_to is not None:
        date_range["$lte"] = date_to
    if date_range:
        query["date"] = date_range
    sort = {"date": -1, "emp_code": 1}
    return query, sort, (page - 1) * page_size, page_size


def check_range(from_text: str, to_text: str) -> tuple[date, date]:
    start, end = day_or_422(from_text), day_or_422(to_text)
    if end < start:
        reject("'to' must not be before 'from'", "query")
    if (end - start).days + 1 > MAX_TREND_DAYS:
        reject(f"range exceeds {MAX_TREND_DAYS} days", "query")
    return start, end


# --------------------------------------------------------------------------- #
# 6. Indexes and application setup
# --------------------------------------------------------------------------- #
def ensure_indexes() -> None:
    """Idempotent. The unique indexes are what make concurrent inserts safe."""
    db.employees.create_index([("joined_on", ASCENDING)], name="joined_on")
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="emp_code_unique")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)],
                              name="department_emp_code")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)],
                                    unique=True, name="emp_code_date_unique")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)],
                                    name="emp_code_punch_in")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)],
                                    name="date_emp_code")
    db.attendance_logs.create_index([("status", ASCENDING), ("date", DESCENDING)],
                                    name="status_date")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError as exc:
        # The app still starts, so /health can report 503. Indexes are retried on the next start.
        log.error("could not create indexes at startup: %s", exc)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


def get_employee_or_404(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "unknown employee")
    return emp


def require(value, name: str) -> None:
    if value is None:
        reject(f"'{name}' is required for this endpoint", "query")


# --------------------------------------------------------------------------- #
# 7. Endpoints: system and employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.client.admin.command("ping")
    except PyMongoError:
        return JSONResponse(status_code=503, content={"detail": "database unavailable"})
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = {**body.model_dump(), "created_at": now_millis()}
    try:
        # The unique index is the only safe guard against two concurrent creates.
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    cursor = (db.employees.find(query, {"_id": 0})
              .sort("emp_code", ASCENDING)
              .skip((page - 1) * page_size)
              .limit(page_size))
    return {
        "items": [serialize_employee(d) for d in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# 8. Endpoints: attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = get_employee_or_404(body.emp_code)
    punched = instant(body.punched_at) if body.punched_at is not None else now_seconds()
    day = attendance_date(punched, emp["shift_start"], emp["shift_end"])
    derived = derive(day, emp, punched, None)
    doc = {
        "emp_code": emp["emp_code"],
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": punched,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": derived["late_minutes"],
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        # Race-safe: the unique (emp_code, date) index lets exactly one insert win.
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = get_employee_or_404(body.emp_code)
    at = instant(body.punched_at) if body.punched_at is not None else now_seconds()
    rec = db.attendance_logs.find_one(
        {"emp_code": emp["emp_code"], "status": {"$in": PRESENCE}, "punch_in": {"$lte": at}},
        sort=[("punch_in", DESCENDING)],
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    punched_in = aware(rec["punch_in"])
    if at <= punched_in:
        reject("punched_at must be after punch_in")
    if at - punched_in > MAX_PUNCH_GAP:
        reject("punch-out is more than 24 hours after punch-in")

    day = parse_day(rec["date"])
    derived = derive(day, emp, punched_in, at)
    # Conditional update: only one of two simultaneous punch-outs can match punch_out: null.
    result = db.attendance_logs.update_one(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {
            "punch_out": at,
            "work_hours": derived["work_hours"],
            "overtime_minutes": derived["overtime_minutes"],
            "half_day": derived["half_day"],
        }},
    )
    if result.matched_count == 0:
        raise HTTPException(409, "already punched out")
    return serialize_record(db.attendance_logs.find_one({"_id": rec["_id"]}))


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=DATE_PATTERN),
    date_to: Optional[str] = Query(None, pattern=DATE_PATTERN),
    status: Optional[StatusT] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query, sort, skip, limit = attendance_query(emp_code, date_from, date_to, status, page, page_size)
    total = db.attendance_logs.count_documents(query)
    cursor = (db.attendance_logs.find(query, {"_id": 0})
              .sort(list(sort.items()))
              .skip(skip)
              .limit(limit))
    return {
        "items": [serialize_record(d) for d in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def history_guard(rec: dict) -> dict:
    """Pins the history length we read, so a concurrent correction makes our update match nothing."""
    count = len(rec.get("history") or [])
    if count == 0:
        return {"$or": [{"history": {"$exists": False}}, {"history": {"$size": 0}}]}
    return {"history": {"$size": count}}


@app.patch("/attendance/{emp_code}/{day}")
def regularize_attendance(
    body: RegularizeRequest,
    emp_code: str = Path(...),
    day: str = Path(..., pattern=DATE_PATTERN),
):
    emp = get_employee_or_404(emp_code)
    record_day = day_or_422(day, "path")
    rec = db.attendance_logs.find_one({"emp_code": emp["emp_code"], "date": day})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")

    supplied = body.model_fields_set
    for name in ("status", "punch_in", "punch_out"):
        if name in supplied and getattr(body, name) is None:
            reject(f"{name} cannot be null")

    stored_in = aware(rec["punch_in"]) if rec.get("punch_in") else None
    stored_out = aware(rec["punch_out"]) if rec.get("punch_out") else None
    new_status = body.status or rec["status"]

    if new_status in NON_PRESENCE:
        if {"punch_in", "punch_out"} & supplied:
            reject(f"{new_status} records cannot carry punch times")
        new_in = new_out = None
    else:
        new_in = instant(body.punch_in) if "punch_in" in supplied else stored_in
        new_out = instant(body.punch_out) if "punch_out" in supplied else stored_out
        if new_in is None:
            reject("a presence status requires a punch_in")
        if "punch_in" in supplied and attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != record_day:
            reject("punch_in must fall on the record's attendance date (R1)")
        if new_out is not None:
            if new_out <= new_in:
                reject("punch_out must be after punch_in")
            if new_out - new_in > MAX_PUNCH_GAP:
                reject("punch_out must be within 24 hours of punch_in")

    derived = derive(record_day, emp, new_in, new_out)
    before = {
        "status": rec["status"],
        "punch_in": stored_in,
        "punch_out": stored_out,
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes", 0),
        "overtime_minutes": rec.get("overtime_minutes", 0),
        "half_day": rec.get("half_day", False),
    }
    after = {"status": new_status, "punch_in": new_in, "punch_out": new_out, **derived}
    changes = {f: {"from": before[f], "to": after[f]} for f in CHANGE_FIELDS if before[f] != after[f]}
    if not changes:
        reject("the request changes nothing")

    entry = {"at": now_millis(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    guard = {
        "emp_code": emp["emp_code"],
        "date": day,
        "status": before["status"],
        "punch_in": stored_in,
        "punch_out": stored_out,
        **history_guard(rec),
    }
    result = db.attendance_logs.update_one(
        guard,
        {"$set": after, "$push": {"history": entry}},
    )
    if result.matched_count == 0:
        raise HTTPException(409, "the record changed while this request was running; retry")
    return serialize_record(db.attendance_logs.find_one({"emp_code": emp["emp_code"], "date": day}))


# --------------------------------------------------------------------------- #
# 9. Endpoints: analytics
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_PATTERN)):
    emp = get_employee_or_404(emp_code)
    start, end = month_bounds(month)
    agg = next(db.attendance_logs.aggregate(employee_monthly_pipeline(emp["emp_code"], start, end)), {})
    workdays = working_days(max(emp["joined_on"], start), end)
    present = agg.get("present_days", 0)
    pct = None
    if workdays:
        pct = round_half_up(Decimal(str(present)) * 100 / Decimal(workdays), 2)
    return {
        "emp_code": emp["emp_code"],
        "month": month,
        "working_days": workdays,
        "present_days": round_half_up(present, 2),
        "leave_days": int(agg.get("leave_days", 0)),
        "late_count": int(agg.get("late_count", 0)),
        "total_late_minutes": int(agg.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(agg.get("total_overtime_minutes", 0)),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=MONTH_PATTERN), department: Optional[str] = None):
    start, end = month_bounds(month)
    items = []
    for row in db.employees.aggregate(department_summary_pipeline(start, end, department)):
        items.append({
            "department": row["department"],
            "headcount": row["headcount"],
            "present_days": round_half_up(row["present_days"], 2),
            "avg_work_hours": round_half_up(row["avg_work_hours"], 2),
            "late_count": int(row["late_count"]),
            "total_late_minutes": int(row["total_late_minutes"]),
            "leave_count": int(row["leave_count"]),
            "on_duty_count": int(row["on_duty_count"]),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_PATTERN),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    start, end = month_bounds(month)
    rows = list(db.attendance_logs.aggregate(leaderboard_pipeline(start, end, limit, department)))
    return {"month": month, "items": rows}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(..., alias="from", pattern=DATE_PATTERN),
    to: str = Query(..., pattern=DATE_PATTERN),
):
    start, end = check_range(from_, to)
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "unknown department")
    rows = list(db.aggregate(department_trend_pipeline(department, start, end)))
    items = [{
        "date": r["date"],
        "is_working_day": r["is_working_day"],
        "headcount": r["headcount"],
        "present_count": r["present_count"],
        "late_count": int(r["late_count"]),
        "attendance_rate": round_half_up(r.get("attendance_rate"), 4),
        "moving_avg_7d": round_half_up(r.get("moving_avg_7d"), 4),
    } for r in rows]
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# 10. Endpoint: explain
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainT = Path(...),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=DATE_PATTERN),
    date_to: Optional[str] = Query(None, pattern=DATE_PATTERN),
    status: Optional[StatusT] = None,
    from_: Optional[str] = Query(None, alias="from", pattern=DATE_PATTERN),
    to: Optional[str] = Query(None, pattern=DATE_PATTERN),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        query, sort, skip, limit_ = attendance_query(emp_code, date_from, date_to, status, page, page_size)
        collection = "attendance_logs"
        command = {"find": collection, "filter": query, "sort": sort, "skip": skip, "limit": limit_}
    elif endpoint == "employee_monthly":
        require(emp_code, "emp_code")
        require(month, "month")
        start, end = month_bounds(month)
        collection = "attendance_logs"
        command = {"aggregate": collection, "cursor": {},
                   "pipeline": employee_monthly_pipeline(emp_code, start, end)}
    elif endpoint == "department_summary":
        require(month, "month")
        start, end = month_bounds(month)
        collection = "employees"
        command = {"aggregate": collection, "cursor": {},
                   "pipeline": department_summary_pipeline(start, end, department)}
    elif endpoint == "late_leaderboard":
        require(month, "month")
        start, end = month_bounds(month)
        collection = "attendance_logs"
        command = {"aggregate": collection, "cursor": {},
                   "pipeline": leaderboard_pipeline(start, end, limit, department)}
    else:  # department_trend
        require(department, "department")
        require(from_, "from")
        require(to, "to")
        start, end = check_range(from_, to)
        collection = "attendance_logs"
        command = {"aggregate": 1, "cursor": {},
                   "pipeline": department_trend_pipeline(department, start, end)}

    result = db.command({"explain": command, "verbosity": "executionStats"})
    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": json.loads(json_util.dumps(result)),
    }
