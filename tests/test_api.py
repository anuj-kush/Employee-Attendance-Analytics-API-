"""
HTTP-level tests run against mongomock, an in-memory stand-in for MongoDB.

mongomock does not implement every aggregation stage (for example $densify,
$setWindowFields, $documents). The analytics pipelines are therefore NOT covered
here; they must be run against a real MongoDB 6.0+ server. See README.md.
"""
from datetime import datetime, timezone

import mongomock
import pytest
from fastapi.testclient import TestClient

from app import main


def ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


@pytest.fixture
def api(monkeypatch):
    database = mongomock.MongoClient()["attendance_test"]
    monkeypatch.setattr(main, "db", database)
    with TestClient(main.app) as client:  # runs the lifespan: creates indexes
        yield client


def make_employee(api, emp_code="EMP0001", department="Engineering", shift=("09:30", "18:30"), joined="2026-01-05"):
    body = {
        "emp_code": emp_code, "name": "Test Person", "email": "t@example.com",
        "department": department, "shift_start": shift[0], "shift_end": shift[1], "joined_on": joined,
    }
    res = api.post("/employees", json=body)
    assert res.status_code == 201, res.text
    return res.json()


# ---------------------------------------------------------------- employees
def test_create_employee_returns_epoch_millis(api):
    emp = make_employee(api)
    assert isinstance(emp["created_at"], int)
    assert "_id" not in emp and "id" not in emp


def test_duplicate_emp_code_is_409(api):
    make_employee(api)
    body = {"emp_code": "EMP0001", "name": "X", "email": "x@example.com",
            "department": "Sales", "joined_on": "2026-01-05"}
    assert api.post("/employees", json=body).status_code == 409


def test_invalid_employee_is_422(api):
    bad = {"emp_code": "E1", "name": "X", "email": "x@example.com",
           "department": "Sales", "joined_on": "2026-01-05"}
    assert api.post("/employees", json=bad).status_code == 422
    same_shift = {"emp_code": "EMP0002", "name": "X", "email": "x@example.com", "department": "Sales",
                  "joined_on": "2026-01-05", "shift_start": "09:00", "shift_end": "09:00"}
    assert api.post("/employees", json=same_shift).status_code == 422


def test_list_employees_paginates_from_page_one(api):
    for i in range(1, 6):
        make_employee(api, emp_code=f"EMP{i:04d}")
    page1 = api.get("/employees", params={"page": 1, "page_size": 2}).json()
    assert [e["emp_code"] for e in page1["items"]] == ["EMP0001", "EMP0002"]
    assert page1["total"] == 5


def test_list_employees_total_respects_department(api):
    make_employee(api, "EMP0001", department="Sales")
    make_employee(api, "EMP0002", department="Engineering")
    body = api.get("/employees", params={"department": "Sales"}).json()
    assert body["total"] == 1 and len(body["items"]) == 1


def test_list_employees_rejects_page_size_over_100(api):
    assert api.get("/employees", params={"page_size": 500}).status_code == 422


# ---------------------------------------------------------------- punch-in
def test_punch_in_late_and_shape(api):
    make_employee(api)
    res = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-06T04:35:00Z")})
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["date"] == "2026-07-06"
    assert body["late_minutes"] == 35
    assert body["punch_in"] == ms("2026-07-06T04:35:00Z")
    assert body["punch_out"] is None and body["work_hours"] is None
    assert body["history"] == []
    assert "id" not in body


def test_punch_in_duplicate_day_is_409(api):
    make_employee(api)
    payload = {"emp_code": "EMP0001", "punched_at": ms("2026-07-06T04:00:00Z")}
    assert api.post("/attendance/punch-in", json=payload).status_code == 201
    assert api.post("/attendance/punch-in", json=payload).status_code == 409


def test_punch_in_unknown_employee_is_404(api):
    res = api.post("/attendance/punch-in", json={"emp_code": "NOPE", "punched_at": ms("2026-07-06T04:00:00Z")})
    assert res.status_code == 404


def test_punch_in_rejects_absent_status(api):
    make_employee(api)
    res = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "status": "ABSENT"})
    assert res.status_code == 422


def test_punch_in_rejects_seconds_and_floats(api):
    make_employee(api)
    seconds = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 1783312500})
    assert seconds.status_code == 422
    floaty = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 1783312500000.5})
    assert floaty.status_code == 422


def test_punch_in_truncates_to_whole_seconds(api):
    make_employee(api)
    res = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-06T04:35:00Z") + 900})
    assert res.json()["punch_in"] == ms("2026-07-06T04:35:00Z")


# ---------------------------------------------------------------- punch-out
def test_punch_out_then_second_punch_out_is_409(api):
    make_employee(api)
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-06T03:58:00Z")})
    first = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-06T13:05:00Z")})
    assert first.status_code == 200, first.text
    assert first.json()["work_hours"] == 9.12
    assert first.json()["overtime_minutes"] == 0
    second = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-06T13:10:00Z")})
    assert second.status_code == 409


def test_punch_out_without_open_punch_in_is_404(api):
    make_employee(api)
    res = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-06T13:05:00Z")})
    assert res.status_code == 404


def test_punch_out_before_punch_in_is_422(api):
    make_employee(api)
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-06T04:00:00Z")})
    # The only open punch-in is at 04:00Z, which is after 03:00Z, so no record qualifies: 404.
    res = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-06T03:00:00Z")})
    assert res.status_code == 404


def test_overnight_punch_out_across_midnight(api):
    make_employee(api, emp_code="EMP0005", shift=("22:00", "06:00"))
    api.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ms("2026-07-06T16:25:00Z")})
    res = api.post("/attendance/punch-out", json={"emp_code": "EMP0005", "punched_at": ms("2026-07-07T01:10:00Z")})
    body = res.json()
    assert res.status_code == 200
    assert body["date"] == "2026-07-06"
    assert body["overtime_minutes"] == 40
    assert body["work_hours"] == 8.75


# ---------------------------------------------------------------- regularize
def seed_present(api):
    make_employee(api)
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-07T04:35:00Z")})
    api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-07T13:00:00Z")})


def test_regularize_recomputes_and_appends_history(api):
    seed_present(api)
    res = api.patch("/attendance/EMP0001/2026-07-07", json={
        "punch_in": ms("2026-07-07T03:58:00Z"), "reason": "biometric glitch", "regularized_by": "hr.admin"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["late_minutes"] == 0
    assert body["work_hours"] == 9.03
    assert len(body["history"]) == 1
    entry = body["history"][0]
    assert set(entry["changes"]) == {"punch_in", "late_minutes", "work_hours"}
    assert entry["changes"]["punch_in"] == {"from": ms("2026-07-07T04:35:00Z"), "to": ms("2026-07-07T03:58:00Z")}


def test_regularize_second_correction_appends_not_overwrites(api):
    seed_present(api)
    api.patch("/attendance/EMP0001/2026-07-07", json={
        "punch_in": ms("2026-07-07T03:58:00Z"), "reason": "first fix", "regularized_by": "hr.admin"})
    res = api.patch("/attendance/EMP0001/2026-07-07", json={
        "punch_out": ms("2026-07-07T13:30:00Z"), "reason": "second fix", "regularized_by": "hr.admin"})
    assert len(res.json()["history"]) == 2


def test_regularize_no_change_is_422(api):
    seed_present(api)
    res = api.patch("/attendance/EMP0001/2026-07-07", json={"status": "PRESENT", "reason": "nothing changes", "regularized_by": "hr"})
    assert res.status_code == 422


def test_regularize_absent_clears_punches(api):
    seed_present(api)
    res = api.patch("/attendance/EMP0001/2026-07-07", json={"status": "ABSENT", "reason": "did not come in", "regularized_by": "hr"})
    body = res.json()
    assert res.status_code == 200
    assert body["punch_in"] is None and body["punch_out"] is None and body["work_hours"] is None


def test_regularize_absent_with_punch_is_422(api):
    seed_present(api)
    res = api.patch("/attendance/EMP0001/2026-07-07", json={
        "status": "ABSENT", "punch_in": ms("2026-07-07T04:00:00Z"), "reason": "conflicting input", "regularized_by": "hr"})
    assert res.status_code == 422


def test_regularize_punch_in_on_other_day_is_422(api):
    seed_present(api)
    res = api.patch("/attendance/EMP0001/2026-07-07", json={
        "punch_in": ms("2026-07-09T04:00:00Z"), "reason": "wrong day", "regularized_by": "hr"})
    assert res.status_code == 422


def test_regularize_unknown_record_is_404(api):
    make_employee(api)
    res = api.patch("/attendance/EMP0001/2026-07-07", json={"status": "LEAVE", "reason": "approved leave", "regularized_by": "hr"})
    assert res.status_code == 404


def test_regularize_ignores_client_derived_fields(api):
    seed_present(api)
    res = api.patch("/attendance/EMP0001/2026-07-07", json={
        "work_hours": 99, "late_minutes": 99, "status": "WFH", "reason": "switched to WFH", "regularized_by": "hr"})
    body = res.json()
    assert res.status_code == 200
    # Stored times are 10:05 -> 18:30 IST: late 35, 8.42 h. The client values are ignored.
    assert body["late_minutes"] == 35
    assert body["work_hours"] == 8.42


def test_regularize_reason_too_short_is_422(api):
    seed_present(api)
    res = api.patch("/attendance/EMP0001/2026-07-07", json={"status": "WFH", "reason": "no", "regularized_by": "hr"})
    assert res.status_code == 422


# ---------------------------------------------------------------- list attendance
def test_list_attendance_rejects_inverted_range(api):
    res = api.get("/attendance", params={"date_from": "2026-07-10", "date_to": "2026-07-01"})
    assert res.status_code == 422


def test_list_attendance_rejects_page_zero(api):
    assert api.get("/attendance", params={"page": 0}).status_code == 422


def test_list_attendance_has_no_id_field_and_epoch_times(api):
    seed_present(api)
    items = api.get("/attendance").json()["items"]
    assert items and "id" not in items[0]
    assert isinstance(items[0]["punch_in"], int)


# ---------------------------------------------------------------- health
def test_health_ok(api):
    assert api.get("/health").status_code == 200