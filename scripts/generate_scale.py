"""
Scale check: generates about 100,000 attendance records, times the endpoints and
checks each explain plan for collection scans.

Run from the project root (the venv must be active):
    python scripts/generate_scale.py

It writes to its own database, SCALE_MONGO_DB (default attendance_scale), so your
main attendance_db is never touched. Rebuilding takes a few minutes.
"""
import json
import os
import random
import re
import sys
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pymongo import MongoClient  # noqa: E402

from app import main  # noqa: E402
from app.main import IST, attendance_date, derive  # noqa: E402

load_dotenv(ROOT / ".env")
SCALE_DB = os.getenv("SCALE_MONGO_DB", "attendance_scale")
URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
DEPARTMENTS = ["Engineering", "Sales", "Support", "Finance", "Operations"]
N_EMPLOYEES = 520
START, END = date(2026, 1, 5), date(2026, 12, 31)
rng = random.Random(42)


def weekdays(first: date, last: date):
    day = first
    while day <= last:
        if day.weekday() < 5:
            yield day
        day += timedelta(days=1)


def build_employees() -> list[dict]:
    employees = []
    for n in range(1, N_EMPLOYEES + 1):
        joined = START + timedelta(days=rng.randint(0, 120))
        employees.append({
            "emp_code": f"SCL{n:05d}",
            "name": f"Scale Employee {n}",
            "email": f"scl{n}@example.com",
            "department": DEPARTMENTS[n % len(DEPARTMENTS)],
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": joined.isoformat(),
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        })
    return employees


def ist_on(day: date, hhmm: str, offset_minutes: int) -> datetime:
    h, m = map(int, hhmm.split(":"))
    base = datetime.combine(day, dtime(h, m), tzinfo=IST)
    return (base + timedelta(minutes=offset_minutes)).astimezone(timezone.utc)


def build_logs(employees: list[dict]):
    for emp in employees:
        joined = date.fromisoformat(emp["joined_on"])
        for day in weekdays(max(joined, START), END):
            roll = rng.random()
            if roll < 0.03:
                yield {"emp_code": emp["emp_code"], "date": day.isoformat(), "status": "ABSENT",
                       "punch_in": None, "punch_out": None, "work_hours": None,
                       "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []}
                continue
            if roll < 0.05:
                yield {"emp_code": emp["emp_code"], "date": day.isoformat(), "status": "LEAVE",
                       "punch_in": None, "punch_out": None, "work_hours": None,
                       "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []}
                continue
            status = rng.choice(["PRESENT", "PRESENT", "PRESENT", "WFH", "ON_DUTY"])
            punch_in = ist_on(day, emp["shift_start"], rng.randint(-25, 70))
            punch_out = None if rng.random() < 0.01 else ist_on(day, emp["shift_end"], rng.randint(-200, 120))
            derived = derive(day, emp, punch_in, punch_out)
            yield {"emp_code": emp["emp_code"], "date": attendance_date(
                       punch_in, emp["shift_start"], emp["shift_end"]).isoformat(),
                   "status": status, "punch_in": punch_in, "punch_out": punch_out,
                   "history": [], **derived}


def load(db) -> int:
    db.drop_collection("employees")
    db.drop_collection("attendance_logs")
    employees = build_employees()
    db["employees"].insert_many([dict(e) for e in employees])
    batch, total = [], 0
    for doc in build_logs(employees):
        batch.append(doc)
        if len(batch) == 5000:
            db["attendance_logs"].insert_many(batch)
            total += len(batch)
            batch = []
            print(f"  loaded {total:,} records", flush=True)
    if batch:
        db["attendance_logs"].insert_many(batch)
        total += len(batch)
    return total


def timed(client: TestClient, label: str, path: str, params: dict | None = None) -> float:
    start = time.perf_counter()
    res = client.get(path, params=params)
    elapsed = (time.perf_counter() - start) * 1000
    status = "ok" if res.status_code == 200 else f"HTTP {res.status_code}"
    print(f"  {label:<34} {elapsed:8.1f} ms   {status}")
    return elapsed


def main_run() -> int:
    raw = MongoClient(URI, tz_aware=True)
    raw.drop_database(SCALE_DB)
    db = raw[SCALE_DB]
    print(f"Loading into {SCALE_DB} ...")
    total = load(db)
    print(f"Loaded {total:,} attendance records.\n")

    main.db = db
    with TestClient(main.app) as client:  # creates the indexes
        print("Timing (in-process, includes MongoDB time):")
        timed(client, "list attendance, page 1", "/attendance", {"page": 1, "page_size": 50})
        timed(client, "list attendance, one employee", "/attendance", {"emp_code": "SCL00007"})
        timed(client, "list attendance, date range", "/attendance",
              {"date_from": "2026-06-01", "date_to": "2026-06-30", "page": 1, "page_size": 100})
        timed(client, "employee monthly", "/analytics/employees/SCL00007/monthly", {"month": "2026-06"})
        timed(client, "department summary", "/analytics/departments/summary", {"month": "2026-06"})
        timed(client, "late leaderboard", "/analytics/leaderboard/late", {"month": "2026-06", "limit": 10})
        timed(client, "department trend (92 days)", "/analytics/departments/Sales/trend",
              {"from": "2026-04-01", "to": "2026-06-30"})

        print("\nExplain plans (COLLSCAN on attendance_logs is a failure at this size):")
        plans = {
            "attendance_list": {},
            "employee_monthly": {"emp_code": "SCL00007", "month": "2026-06"},
            "department_summary": {"month": "2026-06"},
            "late_leaderboard": {"month": "2026-06"},
            "department_trend": {"department": "Sales", "from": "2026-04-01", "to": "2026-06-30"},
        }
        problems = []
        for name, params in plans.items():
            res = client.get(f"/admin/explain/{name}", params=params)
            text = json.dumps(res.json()) if res.status_code == 200 else res.text
            uses_index = "IXSCAN" in text or '"indexesUsed"' in text
            has_collscan = ('"stage": "COLLSCAN"' in text or
                            any(int(n) > 0 for n in re.findall(r'"collectionScans": (\d+)', text)))
            verdict = "ok" if uses_index and not has_collscan else "CHECK"
            print(f"  {name:<20} IXSCAN={uses_index!s:<5} COLLSCAN={has_collscan!s:<5} {verdict}")
            if verdict != "ok":
                problems.append(name)

    print("\n" + ("All plans use indexes." if not problems
                  else f"Check these plans in the explain output: {', '.join(problems)}"))
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main_run())
