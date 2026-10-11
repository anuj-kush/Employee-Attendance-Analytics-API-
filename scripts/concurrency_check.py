"""
Fires parallel requests at a RUNNING API and checks the outcome.

Start the API first (uvicorn app.main:app --port 8000), then run:
    python scripts/concurrency_check.py

Environment: BASE_URL (default http://localhost:8000), MONGO_URI, MONGO_DB.
The script only touches the test employee CONC001 and its records.

Expected results:
  parallel punch-in   -> exactly one 201, the rest 409
  parallel punch-out  -> exactly one 200, the rest 409
  parallel PATCH      -> no 500s, and the history length equals the number of 200s
"""
import os
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import httpx
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()
BASE = os.getenv("BASE_URL", "http://localhost:8000")
EMP = "EMP9001"
DAY = "2026-08-03"
WORKERS = 10

db = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))[os.getenv("MONGO_DB", "attendance_db")]
failures: list[str] = []


def epoch_ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


def fire(task, count: int = WORKERS) -> Counter:
    with ThreadPoolExecutor(max_workers=count) as pool:
        futures = [pool.submit(task, i) for i in range(count)]
        return Counter(f.result().status_code for f in futures)


def check(label: str, got: Counter, expected: Counter) -> None:
    ok = got == expected
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got {dict(got)}, expected {dict(expected)}")
    if not ok:
        failures.append(label)


def setup_employee() -> None:
    body = {"emp_code": EMP, "name": "Concurrency Test", "email": "conc@example.com",
            "department": "Engineering", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01"}
    res = httpx.post(f"{BASE}/employees", json=body, timeout=20)
    print(f"employee {EMP}: {res.status_code} (409 is fine if it already exists)")
    db.attendance_logs.delete_many({"emp_code": EMP})


def punch_in_task(i: int) -> httpx.Response:
    return httpx.post(f"{BASE}/attendance/punch-in", timeout=20,
                      json={"emp_code": EMP, "punched_at": epoch_ms(f"{DAY}T04:05:00+00:00")})


def punch_out_task(i: int) -> httpx.Response:
    return httpx.post(f"{BASE}/attendance/punch-out", timeout=20,
                      json={"emp_code": EMP, "punched_at": epoch_ms(f"{DAY}T13:{10 + i:02d}:00+00:00")})


def patch_task(i: int) -> httpx.Response:
    return httpx.patch(f"{BASE}/attendance/{EMP}/{DAY}", timeout=20, json={
        "punch_out": epoch_ms(f"{DAY}T13:{30 + i:02d}:00+00:00"),
        "reason": f"concurrent correction {i}",
        "regularized_by": "load-test",
    })


def main() -> int:
    setup_employee()

    print("\n-- parallel punch-in (same employee, same day)")
    check("punch-in", fire(punch_in_task), Counter({201: 1, 409: WORKERS - 1}))

    print("\n-- parallel punch-out (same open record)")
    check("punch-out", fire(punch_out_task), Counter({200: 1, 409: WORKERS - 1}))

    print("\n-- parallel regularizations (must not lose history entries)")
    outcome = fire(patch_task)
    ok_count = outcome.get(200, 0)
    no_500 = outcome.get(500, 0) == 0
    print(f"       status counts: {dict(outcome)}")
    res = httpx.get(f"{BASE}/attendance", params={"emp_code": EMP, "date_from": DAY, "date_to": DAY}, timeout=20)
    history_len = len(res.json()["items"][0]["history"])
    print(f"       history entries on the record: {history_len}, successful PATCH responses: {ok_count}")
    passed = no_500 and history_len == ok_count and ok_count >= 1
    print(f"{'PASS' if passed else 'FAIL'}  regularize: no 500s and history matches successes")
    if not passed:
        failures.append("regularize")

    print("\n" + ("ALL CHECKS PASSED" if not failures else f"FAILED: {', '.join(failures)}"))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
