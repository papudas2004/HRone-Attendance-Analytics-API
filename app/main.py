
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, PyMongoError
from pymongo.read_concern import ReadConcern
from pymongo.write_concern import WriteConcern

load_dotenv()

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    serverSelectionTimeoutMS=3000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
STATUSES = PRESENCE | {"ABSENT", "LEAVE"}
MAX_EPOCH_MS = 4102444800000
MIN_EPOCH_MS = 100000000000

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# ---------------------------------------------------------------------
# Startup and indexes
# ---------------------------------------------------------------------
@app.on_event("startup")
def create_indexes():
    db.employees.create_index(
        [("emp_code", ASCENDING)], unique=True, name="uq_employee_code"
    )
    db.employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)],
        name="department_employee",
    )
    db.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)],
        name="department_joined",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uq_attendance_employee_date",
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="attendance_date_employee",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)],
        name="attendance_employee_punchin",
    )
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("status", ASCENDING)],
        name="attendance_date_status",
    )


# ---------------------------------------------------------------------
# Common helpers
# ---------------------------------------------------------------------
def now_utc():
    return datetime.now(UTC).replace(microsecond=0)


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def epoch_ms(value: Optional[datetime]):
    if value is None:
        return None
    value = as_utc(value)
    return int(value.timestamp() * 1000)


def parse_epoch(value: int) -> datetime:
    if type(value) is not int or not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
        raise HTTPException(422, "Timestamp must be valid epoch milliseconds")
    return datetime.fromtimestamp(value / 1000, UTC).replace(microsecond=0)


def clean_instant(value: datetime) -> datetime:
    return as_utc(value).replace(microsecond=0)


def local_datetime(value: datetime) -> datetime:
    return as_utc(value).astimezone(IST)


def attendance_date(punch_in: datetime, employee: dict) -> str:
    local = local_datetime(punch_in)
    start = employee["shift_start"]
    end = employee["shift_end"]

    if end <= start and local.strftime("%H:%M") < end:
        return (local.date() - timedelta(days=1)).isoformat()
    return local.date().isoformat()


def shift_boundary(day: str, shift_time: str) -> datetime:
    d = date.fromisoformat(day)
    h, m = map(int, shift_time.split(":"))
    return datetime.combine(d, time(h, m), tzinfo=IST).astimezone(UTC)


def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    local = local_datetime(punch_in)
    h, m = map(int, shift_start.split(":"))
    start = local.replace(hour=h, minute=m, second=0, microsecond=0)
    minutes = int((local - start).total_seconds() // 60)
    elapsed = (local - start).total_seconds()
    return max(0, minutes) if elapsed > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = Decimal(str((punch_out - punch_in).total_seconds()))
    hours = (seconds / Decimal(3600)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return float(hours)


def compute_overtime(punch_out: datetime, shift_end: str, date_str: str) -> int:
    end = shift_boundary(date_str, shift_end)
    employee_day = date.fromisoformat(date_str)
    # An overnight shift ends on the calendar day after it starts.
    punch_local = local_datetime(punch_out)
    if punch_local.date() > employee_day and shift_end <= "23:59":
        # The caller's employee shift determines whether this is overnight;
        # the actual end boundary is adjusted below when required.
        pass
    minutes = int((as_utc(punch_out) - end).total_seconds() // 60)
    return minutes if minutes >= 30 else 0


def round_half_up(value, places=2):
    if value is None:
        return None
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def output_attendance(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": epoch_ms(doc.get("punch_in")),
        "punch_out": epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": int(doc.get("late_minutes", 0)),
        "overtime_minutes": int(doc.get("overtime_minutes", 0)),
        "half_day": bool(doc.get("half_day", False)),
        "history": [
            {
                **entry,
                "at": epoch_ms(entry.get("at")),
                "changes": {
                    key: {
                        "from": epoch_ms(change["from"])
                        if key in {"punch_in", "punch_out"} and change.get("from") is not None
                        else change.get("from"),
                        "to": epoch_ms(change["to"])
                        if key in {"punch_in", "punch_out"} and change.get("to") is not None
                        else change.get("to"),
                    }
                    for key, change in entry.get("changes", {}).items()
                },
            }
            for entry in doc.get("history", [])
        ],
    }


def month_bounds(month: str):
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(422, "month must use YYYY-MM")
    start = date.fromisoformat(month + "-01")
    if start.month == 12:
        end = date(start.year + 1, 1, 1)
    else:
        end = date(start.year, start.month + 1, 1)
    return start.isoformat(), end.isoformat(), (end - timedelta(days=1)).isoformat()


def validate_date_string(value: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError):
        raise HTTPException(422, "Date must use YYYY-MM-DD")


def working_days(start: date, end: date) -> int:
    count = 0
    current = start
    while current <= end:
        if current.weekday() < 5:
            count += 1
        current += timedelta(days=1)
    return count


def calculate_derived(record: dict, employee: dict) -> dict:
    pin = record.get("punch_in")
    pout = record.get("punch_out")
    if record["status"] in {"ABSENT", "LEAVE"}:
        record["punch_in"] = None
        record["punch_out"] = None
        record["late_minutes"] = 0
        record["work_hours"] = None
        record["overtime_minutes"] = 0
        record["half_day"] = False
        return record

    if pin is None:
        raise HTTPException(422, "Presence statuses require punch_in")

    pin = clean_instant(pin)
    record["punch_in"] = pin
    record["late_minutes"] = compute_late_minutes(pin, employee["shift_start"])

    if pout is None:
        record["work_hours"] = None
        record["overtime_minutes"] = 0
        record["half_day"] = False
        return record

    pout = clean_instant(pout)
    if pout <= pin or (pout - pin).total_seconds() > 86400:
        raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    record["punch_out"] = pout
    record["work_hours"] = compute_work_hours(pin, pout)

    shift_end_day = date.fromisoformat(record["date"])
    start_hm = employee["shift_start"]
    end_hm = employee["shift_end"]
    if end_hm <= start_hm:
        shift_end_day += timedelta(days=1)
    record["overtime_minutes"] = max(
        0,
        int((pout - shift_boundary(shift_end_day.isoformat(), end_hm)).total_seconds() // 60),
    )
    if record["overtime_minutes"] < 30:
        record["overtime_minutes"] = 0

    record["half_day"] = record["work_hours"] < 4.5
    return record


def serialize_employee(doc: dict):
    result = {k: v for k, v in doc.items() if k != "_id"}
    if "created_at" in result:
        result["created_at"] = epoch_ms(result["created_at"])
    return result


def validate_page(page: int, page_size: int):
    if page < 1 or page_size < 1 or page_size > 100:
        raise HTTPException(422, "page must be >= 1 and page_size must be between 1 and 100")


def month_match(month: str):
    start, end, _ = month_bounds(month)
    return {"date": {"$gte": start, "$lt": end}}


# ---------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------
class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def check_joined_on(cls, value):
        return validate_date_string(value)

    @field_validator("shift_end")
    @classmethod
    def validate_shift(cls, value, info):
        if info.data.get("shift_start") == value:
            raise ValueError("shift_start must differ from shift_end")
        return value


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, value):
        if value is not None:
            parse_epoch(value)
        return value

    @field_validator("status")
    @classmethod
    def validate_presence_status(cls, value):
        if value not in PRESENCE:
            raise ValueError("status must be PRESENT, WFH, or ON_DUTY")
        return value


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, value):
        if value is not None:
            parse_epoch(value)
        return value


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("status")
    @classmethod
    def validate_status(cls, value):
        if value is not None and value not in STATUSES:
            raise ValueError("Invalid status")
        return value

    @field_validator("punch_in", "punch_out")
    @classmethod
    def validate_times(cls, value):
        if value is not None:
            parse_epoch(value)
        return value


# ---------------------------------------------------------------------
# Health and employees
# ---------------------------------------------------------------------
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
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
    docs = db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING)
    docs = list(docs.skip((page - 1) * page_size).limit(page_size))
    for doc in docs:
        if doc.get("created_at"):
            doc["created_at"] = epoch_ms(doc["created_at"])
    return {"items": docs, "total": total, "page": page, "page_size": page_size}


# ---------------------------------------------------------------------
# Punch in/out
# ---------------------------------------------------------------------
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if not employee:
        raise HTTPException(404, "Employee not found")

    ts = clean_instant(parse_epoch(body.punched_at)) if body.punched_at is not None else now_utc()
    day = attendance_date(ts, employee)

    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, employee["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "Already punched in for this date")

    return output_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if not employee:
        raise HTTPException(404, "Employee not found")

    ts = clean_instant(parse_epoch(body.punched_at)) if body.punched_at is not None else now_utc()
    existing = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", DESCENDING)],
    )
    if not existing:
        raise HTTPException(404, "No punch-in found")

    pin = as_utc(existing["punch_in"])
    if ts <= pin or (ts - pin).total_seconds() > 86400:
        raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    updated = db.attendance_logs.find_one_and_update(
        {
            "emp_code": body.emp_code,
            "date": existing["date"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": ts,
                "work_hours": compute_work_hours(pin, ts),
                "overtime_minutes": calculate_derived(
                    {
                        **existing,
                        "punch_in": pin,
                        "punch_out": ts,
                    },
                    employee,
                )["overtime_minutes"],
                "half_day": compute_work_hours(pin, ts) < 4.5,
            }
        },
        return_document=True,
    )
    if not updated:
        raise HTTPException(409, "Record already punched out")
    return output_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {}
    if emp_code:
        query["emp_code"] = emp_code
    if date_from:
        query.setdefault("date", {})["$gte"] = validate_date_string(date_from)
    if date_to:
        query.setdefault("date", {})["$lte"] = validate_date_string(date_to)
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")
    if status:
        if status not in STATUSES:
            raise HTTPException(422, "Invalid status")
        query["status"] = status

    total = db.attendance_logs.count_documents(query)
    docs = db.attendance_logs.find(query).sort(
        [("date", DESCENDING), ("emp_code", ASCENDING)]
    )
    docs = list(docs.skip((page - 1) * page_size).limit(page_size))
    return {
        "items": [output_attendance(doc) for doc in docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ---------------------------------------------------------------------
# Manual corrections and audit history
# ---------------------------------------------------------------------
@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    date = validate_date_string(date)
    employee = db.employees.find_one({"emp_code": emp_code})
    if not employee:
        raise HTTPException(404, "Employee not found")

    old = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not old:
        raise HTTPException(404, "Attendance record not found")

    old = dict(old)
    new = dict(old)
    new["history"] = list(old.get("history", []))
    requested = body.model_dump(exclude_unset=True)
    requested.pop("reason", None)
    requested.pop("regularized_by", None)

    if body.status in {"ABSENT", "LEAVE"} and (
        body.punch_in is not None or body.punch_out is not None
    ):
        raise HTTPException(422, "ABSENT and LEAVE cannot have punch times")

    for field in ("status", "punch_in", "punch_out"):
        if field in requested:
            value = requested[field]
            if field in {"punch_in", "punch_out"}:
                value = parse_epoch(value) if value is not None else None
            new[field] = value

    if new["status"] in {"ABSENT", "LEAVE"}:
        new["punch_in"] = None
        new["punch_out"] = None
    elif new.get("punch_in") is None:
        raise HTTPException(422, "Presence statuses require punch_in")

    if new.get("punch_in") is not None:
        if attendance_date(new["punch_in"], employee) != date:
            raise HTTPException(422, "punch_in must belong to the record's attendance date")

    new = calculate_derived(new, employee)

    changed_fields = {}
    fields = [
        "status", "punch_in", "punch_out", "work_hours",
        "late_minutes", "overtime_minutes", "half_day",
    ]
    for field in fields:
        before = old.get(field, False if field == "half_day" else None)
        after = new.get(field, False if field == "half_day" else None)
        if isinstance(before, datetime):
            before = as_utc(before)
        if isinstance(after, datetime):
            after = as_utc(after)
        if before != after:
            changed_fields[field] = {"from": before, "to": after}

    if not changed_fields:
        raise HTTPException(422, "The request does not change the record")

    history_entry = {
        "at": now_utc(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changed_fields,
    }

    # Optimistic concurrency check: compare the fields used to build this
    # correction. Seeded documents may omit history, which is equivalent to [].
    concurrency_filter = {
        "emp_code": emp_code,
        "date": date,
        "punch_in": old.get("punch_in"),
        "punch_out": old.get("punch_out"),
        "status": old.get("status"),
    }
    if "history" in old:
        concurrency_filter["history"] = old["history"]
    else:
        concurrency_filter["$or"] = [{"history": {"$exists": False}}, {"history": []}]

    result = db.attendance_logs.update_one(
        concurrency_filter,
        {
            "$set": {
                "status": new["status"],
                "punch_in": new.get("punch_in"),
                "punch_out": new.get("punch_out"),
                "work_hours": new.get("work_hours"),
                "late_minutes": new.get("late_minutes", 0),
                "overtime_minutes": new.get("overtime_minutes", 0),
                "half_day": new.get("half_day", False),
            },
            "$push": {"history": history_entry},
        },
    )
    if result.modified_count != 1:
        raise HTTPException(409, "Record changed concurrently; retry the correction")

    updated = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    return output_attendance(updated)


# ---------------------------------------------------------------------
# Employee monthly analytics - aggregation over stored fields
# ---------------------------------------------------------------------
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    start_s, end_s, last_s = month_bounds(month)
    employee = db.employees.find_one({"emp_code": emp_code})
    if not employee:
        raise HTTPException(404, "Employee not found")

    first = max(date.fromisoformat(start_s), date.fromisoformat(employee["joined_on"]))
    last = date.fromisoformat(last_s)
    wd = working_days(first, last) if first <= last else 0

    pipeline = [
        {"$match": {"emp_code": emp_code, **month_match(month)}},
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", list(PRESENCE)]},
                                    {"$lt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 7]},
                                    {"$gt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 1]},
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]
    result = list(db.attendance_logs.aggregate(pipeline))
    data = result[0] if result else {}
    present_days = data.get("present_days", 0)
    pct = round_half_up(present_days / wd * 100, 2) if wd else None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": wd,
        "present_days": present_days,
        "leave_days": data.get("leave_days", 0),
        "late_count": data.get("late_count", 0),
        "total_late_minutes": data.get("total_late_minutes", 0),
        "total_overtime_minutes": data.get("total_overtime_minutes", 0),
        "attendance_pct": pct,
    }


# ---------------------------------------------------------------------
# Department summary - cross-collection aggregation
# ---------------------------------------------------------------------
@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    start_s, end_s, last_s = month_bounds(month)
    employee_match = {"joined_on": {"$lte": last_s}}
    if department is not None:
        employee_match["department"] = department

    pipeline = [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", start_s]},
                                    {"$lt": ["$date", end_s]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "headcount_codes": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", list(PRESENCE)]},
                                    {"$lt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, 7]},
                                    {"$gt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, 1]},
                                ]
                            },
                            {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "hours": {
                    "$push": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", list(PRESENCE)]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            "$logs.work_hours",
                            None,
                        ]
                    }
                },
                "late_count": {"$sum": {"$cond": [{"$gt": ["$logs.late_minutes", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$headcount_codes"},
                "present_days": 1,
                               "avg_work_hours": {
                    "$cond": [
                        {
                            "$gt": [
                                {
                                    "$size": {
                                        "$filter": {
                                            "input": "$hours",
                                            "as": "h",
                                            "cond": {"$ne": ["$$h", None]},
                                        }
                                    }
                                },
                                0,
                            ]
                        },
                        {
                            "$avg": {
                                "$filter": {
                                    "input": "$hours",
                                    "as": "h",
                                    "cond": {"$ne": ["$$h", None]},
                                }
                            }
                        },
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]
    items = list(db.employees.aggregate(pipeline))
    for item in items:
        item["present_days"] = round_half_up(item["present_days"], 2)
        item["avg_work_hours"] = round_half_up(item["avg_work_hours"], 2)
    return {"month": month, "items": items}


# ---------------------------------------------------------------------
# Late leaderboard - competition ranking with ties
# ---------------------------------------------------------------------
@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str,
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    start_s, end_s, _ = month_bounds(month)
    pipeline = [
        {"$match": {"date": {"$gte": start_s, "$lt": end_s}, "late_minutes": {"$gt": 0}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ]
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


# ---------------------------------------------------------------------
# Department daily trend - gap filling and moving window in MongoDB
# ---------------------------------------------------------------------
@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(alias="from"), to: str = Query(...)):
    start = date.fromisoformat(validate_date_string(from_))
    end = date.fromisoformat(validate_date_string(to))
    if end < start:
        raise HTTPException(422, "to must not be before from")
    if (end - start).days > 91:
        raise HTTPException(422, "Range cannot exceed 92 days")
    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, "Unknown department")

    start_s, end_s = start.isoformat(), end.isoformat()
    pipeline = [
        {"$match": {"date": {"$gte": start_s, "$lte": end_s}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
        {"$match": {"employee.department": department}},
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", list(PRESENCE)]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            }
        },
        {"$project": {"date": "$_id", "_id": 0, "present_count": 1, "late_count": 1}},
        {"$set": {"date": {"$dateFromString": {"dateString": "$date"}}}},
        {
            "$densify": {
                "field": "date",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [
                        datetime.combine(start, time.min),
                        datetime.combine(end, time.min) + timedelta(days=1),
                    ],
                },
            }
        },
        {
            "$set": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date"}},
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"day": "$date"},
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$match": {"$expr": {"$lte": ["$joined_on", "$$day"]}}},
                    {"$count": "count"},
                ],
                "as": "hc",
            }
        },
        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.count", 0]}, 0]}}},
        {
            "$set": {
                "is_working_day": {
                    "$not": [
                        {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [1, 7]]}
                    ]
                },
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$not": [{"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [1, 7]]}]},
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                },
            }
        },
        {"$sort": {"date": 1}},
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
    ]
    items = list(db.attendance_logs.aggregate(pipeline))
    for item in items:
        item["attendance_rate"] = round_half_up(item["attendance_rate"], 4)
        item["moving_avg_7d"] = round_half_up(item["moving_avg_7d"], 4)
        item["present_count"] = round_half_up(item["present_count"], 2)
    return {"department": department, "items": items}


# ---------------------------------------------------------------------
# Explain plans for supported endpoint queries
# ---------------------------------------------------------------------
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    allowed = {
        "attendance_list", "employee_monthly", "department_summary",
        "late_leaderboard", "department_trend",
    }
    if endpoint not in allowed:
        raise HTTPException(422, "Unsupported endpoint")
    if endpoint != "attendance_list" and not month and endpoint != "department_trend":
        raise HTTPException(422, "month is required for this endpoint")
    if endpoint == "attendance_list":
        query = {}
        if emp_code:
            query["emp_code"] = emp_code
        if date_from:
            query.setdefault("date", {})["$gte"] = validate_date_string(date_from)
        if date_to:
            query.setdefault("date", {})["$lte"] = validate_date_string(date_to)
        if status:
            query["status"] = status
        explain = db.command({
            "explain": {
                "find": "attendance_logs",
                "filter": query,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size,
                "limit": page_size,
            },
            "verbosity": "executionStats",
        })
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    

    if endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(422, "emp_code is required")

        start_s, end_s, _ = month_bounds(month)

        pipeline = [
            {"$match": {"emp_code": emp_code, **month_match(month)}},
            {
                "$group": {
                    "_id": None,
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$status", list(PRESENCE)]},
                                        {"$lt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 7]},
                                        {"$gt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 1]},
                                    ]
                                },
                                {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]},
                                0,
                            ]
                        }
                    },
                    "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "total_overtime_minutes": {"$sum": "$overtime_minutes"},
                }
            },
        ]

        explain = db.command({
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })

        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain,
        }


    
    if endpoint == "department_summary":
        start_s, end_s, last_s = month_bounds(month)

        employee_match = {"joined_on": {"$lte": last_s}}
        if department is not None:
            employee_match["department"] = department

        pipeline = [
            {"$match": employee_match},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"code": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$emp_code", "$$code"]},
                                        {"$gte": ["$date", start_s]},
                                        {"$lt": ["$date", end_s]},
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "logs",
                }
            },
            {
                "$unwind": {
                    "path": "$logs",
                    "preserveNullAndEmptyArrays": True,
                }
            },
            {
                "$group": {
                    "_id": "$department",
                    "headcount_codes": {"$addToSet": "$emp_code"},
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$logs.status", list(PRESENCE)]},
                                        {
                                            "$lt": [
                                                {
                                                    "$dayOfWeek": {
                                                        "$dateFromString": {
                                                            "dateString": "$logs.date"
                                                        }
                                                    }
                                                },
                                                7,
                                            ]
                                        },
                                        {
                                            "$gt": [
                                                {
                                                    "$dayOfWeek": {
                                                        "$dateFromString": {
                                                            "dateString": "$logs.date"
                                                        }
                                                    }
                                                },
                                                1,
                                            ]
                                        },
                                    ]
                                },
                                {
                                    "$cond": [
                                        {"$eq": ["$logs.half_day", True]},
                                        0.5,
                                        1,
                                    ]
                                },
                                0,
                            ]
                        }
                    },
                    "hours": {
                        "$push": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$logs.status", list(PRESENCE)]},
                                        {"$ne": ["$logs.work_hours", None]},
                                    ]
                                },
                                "$logs.work_hours",
                                None,
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [{"$gt": ["$logs.late_minutes", 0]}, 1, 0]
                        }
                    },
                    "total_late_minutes": {
                        "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                    },
                    "leave_count": {
                        "$sum": {
                            "$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]
                        }
                    },
                    "on_duty_count": {
                        "$sum": {
                            "$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]
                        }
                    },
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "department": "$_id",
                    "headcount": {"$size": "$headcount_codes"},
                    "present_days": 1,
                    "avg_work_hours": {
                        "$cond": [
                            {
                                "$gt": [
                                    {
                                        "$size": {
                                            "$filter": {
                                                "input": "$hours",
                                                "as": "h",
                                                "cond": {"$ne": ["$$h", None]},
                                            }
                                        }
                                    },
                                    0,
                                ]
                            },
                            {
                                "$avg": {
                                    "$filter": {
                                        "input": "$hours",
                                        "as": "h",
                                        "cond": {"$ne": ["$$h", None]},
                                    }
                                }
                            },
                            None,
                        ]
                    },
                    "late_count": 1,
                    "total_late_minutes": 1,
                    "leave_count": 1,
                    "on_duty_count": 1,
                }
            },
            {"$sort": {"department": 1}},
        ]

        explain = db.command({
            "explain": {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })

        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": explain,
        }

    if endpoint == "late_leaderboard":
        start_s, end_s, _ = month_bounds(month)
        pipeline = [{"$match": {"date": {"$gte": start_s, "$lt": end_s}, "late_minutes": {"$gt": 0}}}]
        explain = db.command({
            "explain": {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            "verbosity": "executionStats",
        })
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    if not department or not from_ or not to:
        raise HTTPException(422, "department, from and to are required")
    start_s = validate_date_string(from_)
    end_s = validate_date_string(to)
    pipeline = [{"$match": {"date": {"$gte": start_s, "$lte": end_s}}}]
    explain = db.command({
        "explain": {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
        "verbosity": "executionStats",
    })
    return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}