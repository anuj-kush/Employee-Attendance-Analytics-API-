"""
Analytics correctness tests against a REAL MongoDB server.

mongomock cannot run the aggregation pipelines, so these tests need a server:

    set TEST_MONGO_URI=mongodb://localhost:27017      (Windows)
    python -m pytest -q tests/test_analytics_real.py

Without TEST_MONGO_URI the module is skipped. The database
"attendance_analytics_test" is dropped and rebuilt on every run; never point this
at data you care about.

The fixture covers the awkward cases from the assignment:
  EMP1001  mixed records: late, half day (weekday), weekend late, absent, leave,
           a missing weekday (13 July), an open record (14 July)
  EMP1002  no logs at all (headcount without logs)
  EMP1003  mid-month joiner (joined 20 July)
  EMP1004  overnight shift 22:00-06:00 (Sales)
  EMP1005  a late record tied with EMP1004 on total late minutes (Support)

Every expected value below was worked out by hand from the rules in openapi.yaml
and checked against the app's rule functions. See the comments for each one.
"""
import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient

from app import main

TEST_URI = os.getenv("TEST_MONGO_URI")
TEST_DB = "attendance_analytics_test"

pytestmark = pytest.mark.skipif(
    not TEST_URI, reason="set TEST_MONGO_URI to run analytics tests against a real MongoDB"
)

IST = timezone(timedelta(hours=5, minutes=30))


def ist(text: str) -> datetime:
    """'2026-07-06 09:40:01' read as IST, stored as UTC."""
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST).astimezone(timezone.utc)


def employee(code, name, dept, joined, shift=("09:30", "18:30")):
    return {
        "emp_code": code, "name": name, "email": f"{code.lower()}@example.com", "department": dept,
        "shift_start": shift[0], "shift_end": shift[1], "joined_on": joined,
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }


def log(emp, day, status, pin=None, pout=None, work=None, late=0, ot=0, half=False):
    return {
        "emp_code": emp, "date": day, "status": status,
        "punch_in": ist(pin) if pin else None,
        "punch_out": ist(pout) if pout else None,
        "work_hours": work, "late_minutes": late, "overtime_minutes": ot,
        "half_day": half, "history": [],
    }


EMPLOYEES = [
    employee("EMP1001", "Asha Test", "Engineering", "2026-01-05"),
    employee("EMP1002", "Bilal Test", "Engineering", "2026-01-05"),          # no logs at all
    employee("EMP1003", "Chitra Test", "Engineering", "2026-07-20"),         # mid-month joiner
    employee("EMP1004", "Dev Test", "Sales", "2026-01-05", ("22:00", "06:00")),  # overnight
    employee("EMP1005", "Esha Test", "Support", "2026-01-05"),
]

LOGS = [
    # EMP1001 ----------------------------------------------------------------
    # Mon 6 Jul: late 10 (09:40:01 is 601 s after 09:30), OT 30 (19:00 is 30 min after 18:30)
    log("EMP1001", "2026-07-06", "PRESENT", "2026-07-06 09:40:01", "2026-07-06 19:00:00", 9.33, late=10, ot=30),
    # Tue 7 Jul: WFH, 4.33 h < 4.50 -> half day, counts 0.5
    log("EMP1001", "2026-07-07", "WFH", "2026-07-07 09:00:00", "2026-07-07 13:20:00", 4.33, half=True),
    # Wed 8 Jul: absent (a real record, not a missing one)
    log("EMP1001", "2026-07-08", "ABSENT"),
    # Thu 9 Jul: approved leave
    log("EMP1001", "2026-07-09", "LEAVE"),
    # Sat 11 Jul: weekend presence, late 30. Late counts (R7); present does not.
    # 4.50 h is NOT a half day (the rule is strictly below 4.50).
    log("EMP1001", "2026-07-11", "PRESENT", "2026-07-11 10:00:00", "2026-07-11 14:30:00", 4.5, late=30),
    # Mon 13 Jul: no record at all (a missing weekday).
    # Tue 14 Jul: open record, punched in but not out yet. Counts as present.
    log("EMP1001", "2026-07-14", "PRESENT", "2026-07-14 09:30:00", None, None),
    # EMP1002: deliberately no logs.
    # EMP1003 -----------------------------------------------------------------
    log("EMP1003", "2026-07-20", "PRESENT", "2026-07-20 09:30:00", "2026-07-20 18:30:00", 9.0),
    # EMP1004 overnight (22:00-06:00) ------------------------------------------
    # 22:35 on 6 Jul: late 35. Out 06:40 on 7 Jul: OT 40. Attendance date is 6 Jul.
    log("EMP1004", "2026-07-06", "PRESENT", "2026-07-06 22:35:00", "2026-07-07 06:40:00", 8.08, late=35, ot=40),
    # 21:55 on 10 Jul (early, so late 0). Out 06:00 on 11 Jul: exactly shift end, OT 0.
    log("EMP1004", "2026-07-10", "PRESENT", "2026-07-10 21:55:00", "2026-07-11 06:00:00", 8.08),
    # EMP1005 -----------------------------------------------------------------
    # Wed 15 Jul: late 35. Ties with EMP1004 on total late minutes.
    log("EMP1005", "2026-07-15", "PRESENT", "2026-07-15 10:05:00", "2026-07-15 18:30:00", 8.42, late=35),
]


@pytest.fixture(scope="module")
def api():
    real = MongoClient(TEST_URI, tz_aware=True)
    real.drop_database(TEST_DB)
    database = real[TEST_DB]
    database["employees"].insert_many([dict(e) for e in EMPLOYEES])
    database["attendance_logs"].insert_many([dict(d) for d in LOGS])
    original = main.db
    main.db = database
    try:
        with TestClient(main.app) as client:   # runs the lifespan: creates the indexes
            yield client
    finally:
        main.db = original
        real.close()


def close(actual, expected):
    """Compare numbers with a tolerance; None must match None exactly."""
    if expected is None:
        assert actual is None, f"expected None, got {actual}"
    else:
        assert actual == pytest.approx(expected, abs=1e-9), f"expected {expected}, got {actual}"


# ---------------------------------------------------------------- monthly, one employee
def test_monthly_mixed_records(api):
    # Working days: July 2026 has 23 weekdays (EMP1001 joined before the month).
    # present_days: 6 Jul (1) + 7 Jul half day (0.5) + 14 Jul open record (1) = 2.5.
    #   11 Jul is a weekend, so it does not count as present. 13 Jul has no record.
    # late_count: 6 Jul (10) and 11 Jul (30, weekend lateness still counts) = 2.
    # total_late_minutes: 10 + 30 = 40. overtime: 30. leave_days: 1.
    # attendance_pct: 2.5 / 23 * 100 = 10.8695... -> 10.87
    body = api.get("/analytics/employees/EMP1001/monthly", params={"month": "2026-07"}).json()
    assert body["working_days"] == 23
    close(body["present_days"], 2.5)
    assert body["leave_days"] == 1
    assert body["late_count"] == 2
    assert body["total_late_minutes"] == 40
    assert body["total_overtime_minutes"] == 30
    close(body["attendance_pct"], 10.87)


def test_monthly_employee_with_no_logs(api):
    # EMP1002 has no records. The count of working days still applies.
    body = api.get("/analytics/employees/EMP1002/monthly", params={"month": "2026-07"}).json()
    assert body["working_days"] == 23
    close(body["present_days"], 0.0)
    close(body["attendance_pct"], 0.0)


def test_monthly_mid_month_joiner(api):
    # EMP1003 joined Monday 20 July, so the working days run 20 to 31 July: 10 days.
    # One present day -> 1 / 10 = 10.00%.
    body = api.get("/analytics/employees/EMP1003/monthly", params={"month": "2026-07"}).json()
    assert body["working_days"] == 10
    close(body["present_days"], 1.0)
    close(body["attendance_pct"], 10.0)


def test_monthly_overnight_shift(api):
    # EMP1004: two presence days, both weekdays (6 Jul and 10 Jul).
    # late 35 (one record), overtime 40 (across midnight), 2 / 23 = 8.70%.
    body = api.get("/analytics/employees/EMP1004/monthly", params={"month": "2026-07"}).json()
    close(body["present_days"], 2.0)
    assert body["late_count"] == 1
    assert body["total_late_minutes"] == 35
    assert body["total_overtime_minutes"] == 40
    close(body["attendance_pct"], 8.70)


# ---------------------------------------------------------------- department summary
def test_department_summary_july(api):
    # Engineering: headcount 3 (EMP1001, EMP1002 with no logs, EMP1003 who joined 20 Jul).
    #   present_days 2.5 + 1 = 3.5. Late records: 6 Jul (10) and 11 Jul (30) -> 2 late, 40 min.
    #   avg_work_hours over records with a value: 9.33, 4.33, 4.50, 9.00 -> 27.16 / 4 = 6.79.
    #   The open record (14 Jul) has no hours, so it is excluded from the average.
    #   Averaging per employee instead would give (6.0867 + 9.00) / 2 = 7.54: that is wrong.
    #   leave_count 1.
    # Sales: headcount 1. present 2.0 (both shifts). avg (8.08 + 8.08) / 2 = 8.08. late 1, 35 min.
    # Support: headcount 1. present 1.0. avg 8.42. late 1, 35 min.
    body = api.get("/analytics/departments/summary", params={"month": "2026-07"}).json()
    items = {i["department"]: i for i in body["items"]}
    assert list(items) == ["Engineering", "Sales", "Support"]

    eng = items["Engineering"]
    assert eng["headcount"] == 3
    close(eng["present_days"], 3.5)
    close(eng["avg_work_hours"], 6.79)
    assert eng["late_count"] == 2
    assert eng["total_late_minutes"] == 40
    assert eng["leave_count"] == 1

    sales = items["Sales"]
    assert sales["headcount"] == 1
    close(sales["present_days"], 2.0)
    close(sales["avg_work_hours"], 8.08)
    assert sales["late_count"] == 1
    assert sales["total_late_minutes"] == 35

    support = items["Support"]
    assert support["headcount"] == 1
    close(support["avg_work_hours"], 8.42)
    assert support["total_late_minutes"] == 35


def test_department_summary_excludes_people_not_yet_joined(api):
    # June: EMP1003 joined 20 July, so headcount for Engineering is 2 (EMP1001, EMP1002).
    # No logs in June, so present is 0 and the average is null (not zero).
    body = api.get("/analytics/departments/summary", params={"month": "2026-06"}).json()
    eng = {i["department"]: i for i in body["items"]}["Engineering"]
    assert eng["headcount"] == 2
    close(eng["present_days"], 0.0)
    close(eng["avg_work_hours"], None)


# ---------------------------------------------------------------- leaderboard
def test_leaderboard_ties_share_rank_and_limit_applies_after_ranking(api):
    # Totals: EMP1001 = 40 (rank 1), EMP1004 = 35 and EMP1005 = 35 (tied, rank 2).
    # The next rank would be 4, not 3 (standard competition ranking).
    # limit=2 must still return all three rows, because the tie is at the cutoff.
    # EMP1003 (0 late minutes) and EMP1002 (no logs) are not listed at all.
    body = api.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 2}).json()
    rows = [(r["emp_code"], r["rank"], r["total_late_minutes"]) for r in body["items"]]
    assert rows == [("EMP1001", 1, 40), ("EMP1004", 2, 35), ("EMP1005", 2, 35)]


def test_leaderboard_department_filter(api):
    body = api.get("/analytics/leaderboard/late",
                   params={"month": "2026-07", "limit": 10, "department": "Sales"}).json()
    assert [(r["emp_code"], r["rank"]) for r in body["items"]] == [("EMP1004", 1)]


# ---------------------------------------------------------------- trend
TREND = [
    # date,        working, headcount, present, late, rate,  moving 7-day average
    ("2026-07-06", True,    2,         1.0,     1,    0.5,   0.5),
    ("2026-07-07", True,    2,         0.5,     0,    0.25,  0.375),
    ("2026-07-08", True,    2,         0.0,     0,    0.0,   0.25),
    ("2026-07-09", True,    2,         0.0,     0,    0.0,   0.1875),
    ("2026-07-10", True,    2,         0.0,     0,    0.0,   0.15),
    # Weekend: no rate. Present stays 0 (R7), but the 11 Jul late (30) still counts.
    ("2026-07-11", False,   2,         0.0,     1,    None,  0.15),
    ("2026-07-12", False,   2,         0.0,     0,    None,  0.15),
]


def test_trend_one_row_per_day_with_gaps_and_moving_average(api):
    # Engineering, 6 to 12 July: exactly 7 rows, including the weekend and the days with no records.
    # Rates are per working day: 6 Jul 1 of 2, 7 Jul 0.5 of 2, and so on.
    # The 7-day moving average uses the current day and up to six before it.
    # Nulls are ignored in the average, so the weekend keeps the Friday's window.
    body = api.get("/analytics/departments/Engineering/trend",
                   params={"from": "2026-07-06", "to": "2026-07-12"}).json()
    rows = body["items"]
    assert [r["date"] for r in rows] == [t[0] for t in TREND]
    for row, (day, working, headcount, present, late, rate, moving) in zip(rows, TREND):
        assert row["is_working_day"] is working, day
        assert row["headcount"] == headcount, day
        close(row["present_count"], present)
        assert row["late_count"] == late, day
        close(row["attendance_rate"], rate)
        close(row["moving_avg_7d"], moving)


def test_trend_unknown_department_is_404(api):
    res = api.get("/analytics/departments/Nowhere/trend", params={"from": "2026-07-06", "to": "2026-07-12"})
    assert res.status_code == 404


def test_trend_range_over_92_days_is_422(api):
    res = api.get("/analytics/departments/Engineering/trend", params={"from": "2026-01-01", "to": "2026-07-01"})
    assert res.status_code == 422


# ---------------------------------------------------------------- explain
def test_explain_leaderboard_uses_the_date_index(api):
    res = api.get("/admin/explain/late_leaderboard", params={"month": "2026-07"})
    assert res.status_code == 200
    text = res.text
    assert "IXSCAN" in text
    assert "date_emp_code" in text
