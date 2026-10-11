"""
Error sweep: sends malformed and edge-case requests to every endpoint and
flags any 500 response. The assignment treats a 500 as a failed test, so every
line should show 404, 409 or 422, and the summary should say "No 500 errors."

Start the API first (uvicorn app.main:app --port 8000), then run:
    python scripts/error_sweep.py

Environment: BASE_URL (default http://localhost:8000).
"""
import os
import sys

import httpx

BASE = os.getenv("BASE_URL", "http://localhost:8000")
JSON_HEADERS = {"Content-Type": "application/json"}
VALID_REASON = '{"status": "WFH", "reason": "valid reason", "regularized_by": "hr"}'

CASES = [
    # listing and paging
    ("GET", "/employees?page=abc", None),
    ("GET", "/employees?page_size=abc", None),
    ("GET", "/attendance?status=BOGUS", None),
    ("GET", "/attendance?date_from=notadate", None),
    ("GET", "/attendance?emp_code=", None),
    # analytics
    ("GET", "/analytics/employees/EMP0001/monthly?month=2026-13", None),
    ("GET", "/analytics/employees/NOPE/monthly?month=2026-07", None),
    ("GET", "/analytics/employees/EMP0001/monthly", None),
    ("GET", "/analytics/departments/summary?month=bad", None),
    ("GET", "/analytics/leaderboard/late?month=2026-07&limit=999", None),
    ("GET", "/analytics/departments/Sales/trend?from=2026-07-10&to=2026-07-01", None),
    ("GET", "/analytics/departments/Sales/trend", None),
    ("GET", "/analytics/departments/Nowhere/trend?from=2026-07-06&to=2026-07-12", None),
    # explain
    ("GET", "/admin/explain/nope", None),
    ("GET", "/admin/explain/employee_monthly", None),
    ("GET", "/admin/explain/department_trend", None),
    # employees
    ("POST", "/employees", "{}"),
    ("POST", "/employees", "not json"),
    ("POST", "/employees", '{"emp_code": 123}'),
    # punch-in
    ("POST", "/attendance/punch-in", '{"emp_code": 123}'),
    ("POST", "/attendance/punch-in", '{"emp_code": "NOPE", "punched_at": 1e20}'),
    ("POST", "/attendance/punch-in", '{"emp_code": "EMP0001", "punched_at": "abc"}'),
    ("POST", "/attendance/punch-in", '{"emp_code": "EMP0001", "status": null}'),
    # punch-out
    ("POST", "/attendance/punch-out", '{"emp_code": "NOPE"}'),
    ("POST", "/attendance/punch-out", '{"emp_code": "EMP0001", "punched_at": "abc"}'),
    ("POST", "/attendance/punch-out", '{"emp_code": "EMP0001", "punched_at": -5}'),
    # regularize
    ("PATCH", "/attendance/EMP0001/2026-07-07", "{}"),
    ("PATCH", "/attendance/EMP0001/notadate", VALID_REASON),
    ("PATCH", "/attendance/NOPE/2026-07-07", VALID_REASON),
    ("PATCH", "/attendance/EMP0001/2026-07-07", '{"status": null, "reason": "valid reason", "regularized_by": "hr"}'),
    ("PATCH", "/attendance/EMP0001/2026-07-07", '{"punch_in": null, "reason": "valid reason", "regularized_by": "hr"}'),
    ("PATCH", "/attendance/EMP0001/2026-07-07", '{"punch_in": "abc", "reason": "valid reason", "regularized_by": "hr"}'),
    ("PATCH", "/attendance/EMP0001/2026-07-07", '{"status": "ABSENT", "punch_in": 1783312500000, "reason": "valid reason", "regularized_by": "hr"}'),
    ("PATCH", "/attendance/EMP0001/2026-07-07", '{"status": "WFH", "reason": "x", "regularized_by": "hr"}'),
    ("PATCH", "/attendance/EMP0001/2026-07-07", '{"status": "WFH", "reason": "valid reason", "regularized_by": ""}'),
]


def main() -> int:
    server_errors = 0
    for method, path, body in CASES:
        res = httpx.request(
            method,
            BASE + path,
            content=body,
            headers=JSON_HEADERS if body is not None else None,
            timeout=20,
        )
        is_500 = res.status_code >= 500
        server_errors += is_500
        flag = "FAIL (500)" if is_500 else "ok"
        print(f"{res.status_code}  {method:<5} {path[:70]:<70} {flag}")

    print()
    if server_errors == 0:
        print("No 500 errors.")
        return 0
    print(f"{server_errors} request(s) returned 500. Paste the FAIL lines above.")
    return 1


if __name__ == "__main__":
    sys.exit(main())