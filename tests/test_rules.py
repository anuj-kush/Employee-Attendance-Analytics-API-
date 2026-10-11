"""Pure-rule tests. Expected values come from DATA_MODEL.md and openapi.yaml R1-R7."""
from datetime import date, datetime, timedelta, timezone

from app.main import (
    IST,
    attendance_date,
    derive,
    is_half_day,
    late_minutes,
    overtime_minutes,
    shift_end_at,
    shift_start_at,
    work_hours,
    working_days,
)


def ist(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=IST)


DAY_SHIFT = {"shift_start": "09:30", "shift_end": "18:30"}
NIGHT_SHIFT = {"shift_start": "22:00", "shift_end": "06:00"}


# R2: late. Examples come straight from the contract.
def test_late_grace_boundary_is_strict():
    start = shift_start_at(date(2026, 7, 6), "09:30")
    assert late_minutes(ist(2026, 7, 6, 9, 40, 0), start) == 0
    assert late_minutes(ist(2026, 7, 6, 9, 40, 1), start) == 10
    assert late_minutes(ist(2026, 7, 6, 10, 15, 59), start) == 45


def test_sample_late_with_overtime_record():
    # DATA_MODEL sample 2: punch-in 10:05 IST on a 09:30 shift -> 35 minutes late
    emp = DAY_SHIFT
    d = derive(date(2026, 7, 6), emp, ist(2026, 7, 6, 10, 5), ist(2026, 7, 6, 19, 10))
    assert d["late_minutes"] == 35
    assert d["overtime_minutes"] == 40
    assert d["work_hours"] == 9.08
    assert d["half_day"] is False


# R3: overtime, counted only when at least 30 whole minutes.
def test_overtime_threshold():
    end = shift_end_at(date(2026, 7, 6), "09:30", "18:30")
    assert overtime_minutes(ist(2026, 7, 6, 18, 35), end) == 0      # sample 1: 5 min -> 0
    assert overtime_minutes(ist(2026, 7, 6, 18, 59, 59), end) == 0  # 29 min 59 s -> 0
    assert overtime_minutes(ist(2026, 7, 6, 19, 0, 0), end) == 30   # exactly 30 -> counted
    assert overtime_minutes(ist(2026, 7, 6, 17, 0, 0), end) == 0    # early leave -> 0


# R4: work hours, half-up to two decimals (not Python's banker's round).
def test_work_hours_half_up():
    punch_in = ist(2026, 7, 6, 9, 0)
    assert work_hours(punch_in, ist(2026, 7, 6, 13, 7, 30)) == 4.13   # 4.125 h
    assert work_hours(punch_in, ist(2026, 7, 6, 18, 35)) == 9.58


def test_sample_full_day_work_hours():
    # DATA_MODEL sample 1: 09:28 -> 18:35 IST
    assert work_hours(ist(2026, 7, 6, 9, 28), ist(2026, 7, 6, 18, 35)) == 9.12


# R5: half day is tested against the rounded value.
def test_half_day_uses_rounded_value():
    assert is_half_day(4.49) is True
    assert is_half_day(4.50) is False
    # 4h29m40s = 4.4944 h -> rounds to 4.49 -> half day
    assert is_half_day(work_hours(ist(2026, 7, 6, 9, 0), ist(2026, 7, 6, 13, 29, 40))) is True


def test_sample_half_day_wfh():
    # DATA_MODEL sample 4: 09:30 -> 13:00 IST = 3.5 h
    d = derive(date(2026, 7, 7), DAY_SHIFT, ist(2026, 7, 7, 9, 30), ist(2026, 7, 7, 13, 0))
    assert d["work_hours"] == 3.5
    assert d["half_day"] is True


# R1: attendance date, including the overnight exception.
def test_day_shift_uses_ist_calendar_date():
    # 00:30 IST on 7 July is 19:00 UTC on 6 July. The record belongs to 7 July.
    punch = datetime(2026, 7, 6, 19, 0, tzinfo=timezone.utc)
    assert attendance_date(punch, "09:30", "18:30") == date(2026, 7, 7)


def test_overnight_punch_before_shift_end_belongs_to_previous_day():
    assert attendance_date(ist(2026, 7, 7, 5, 50), "22:00", "06:00") == date(2026, 7, 6)
    assert attendance_date(ist(2026, 7, 6, 21, 55), "22:00", "06:00") == date(2026, 7, 6)
    # 06:00 is not earlier than shift_end, so it starts a new day.
    assert attendance_date(ist(2026, 7, 7, 6, 0), "22:00", "06:00") == date(2026, 7, 7)


def test_sample_overnight_shift():
    # DATA_MODEL sample 7: in 21:55 IST on 6 July, out 06:40 IST on 7 July
    d = derive(date(2026, 7, 6), NIGHT_SHIFT, ist(2026, 7, 6, 21, 55), ist(2026, 7, 7, 6, 40))
    assert d["overtime_minutes"] == 40
    assert d["work_hours"] == 8.75
    assert d["late_minutes"] == 0


def test_absent_derives_nothing():
    d = derive(date(2026, 7, 8), DAY_SHIFT, None, None)
    assert d == {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}


# R7: Monday to Friday only.
def test_working_days_skips_weekends():
    # July 2026: 1 July is a Wednesday. The month has 23 weekdays.
    assert working_days("2026-07-01", "2026-07-31") == 23
    # 2026-07-13 is a Monday; joining there gives 15 working days through 31 July.
    assert working_days("2026-07-13", "2026-07-31") == 15
    assert working_days("2026-08-01", "2026-07-31") == 0