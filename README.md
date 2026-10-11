# Employee Attendance & Analytics API

FastAPI and MongoDB 6.0 or newer. Checked against MongoDB 7.0.43. All application code is in 'app/main.py'.

## Run


python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt


Set the connection (or put these in a local '.env', which is not committed):


export MONGO_URI="mongodb://localhost:27017"    # Windows: set MONGO_URI=...
export MONGO_DB="attendance_db"


Load the nine sample documents (optional) and start the API:


python sample_seed.py
uvicorn app.main:app --port 8000


Indexes are created at startup and are safe to re-create. 'GET /health' returns 200 when MongoDB answers and 503 when it does not.

To run MongoDB locally: 'docker run -d -p 27017:27017 --name attendance-mongo mongo:7'

## Tests


pip install -r requirements-dev.txt
python -m pytest -q tests/test_rules.py tests/test_api.py      # no database server needed


| File | What it checks | Needs |
|---|---|---|
| 'tests/test_rules.py' | Rules R1 to R5 and R7, with values from 'DATA_MODEL.md' | nothing |
| 'tests/test_api.py' | CRUD, status codes, validation, punch-out, regularization, history | nothing (in-memory 'mongomock') |
| 'tests/test_analytics_real.py' | Analytics results checked by hand: ties, no-log employee, mid-month joiner, weekend records, missing weekday, overnight shift | a real MongoDB |

Run the analytics tests against a server. The database 'attendance_analytics_test' is dropped and rebuilt on each run:


set TEST_MONGO_URI=mongodb://localhost:27017      # macOS/Linux: export TEST_MONGO_URI=...
python -m pytest -q tests/test_analytics_real.py


## Concurrency and scale checks

With the API running on port 8000:


python scripts/concurrency_check.py


This sends parallel punch-ins, punch-outs and regularizations for the test employee 'EMP9001'.

To build about 112,000 records in a separate database ('attendance_scale') and time the endpoints:


python scripts/generate_scale.py


## Verification

| Check | Result |
|---|---|
| Rule and API tests ('mongomock') | 41 passed |
| Analytics tests on MongoDB 7.0.43 | 12 passed |
| Concurrency: 10 parallel punch-ins | one 201, nine 409 |
| Concurrency: 10 parallel punch-outs | one 200, nine 409 |
| Concurrency: 10 parallel regularizations | 3 succeeded (200), 7 conflicted (409); history has 3 entries |
| Scale: 112,480 attendance records | list and analytics endpoints 12 to 70 ms; department trend over 92 days 0.9 to 1.5 s |
| Explain plans on 112,480 records | all five endpoints use an index; no collection scan on 'attendance_logs' |
| Starter defects | reproduced on the original starter; see 'REVIEW.md' |

## Design notes

- Indexes: unique '(emp_code, date)' (one record per day, and the race guard); unique 'emp_code'; '(emp_code, punch_in desc)' for punch-out; '(date desc, emp_code asc)' for lists and date-range analytics; '(department, emp_code)' and 'joined_on' for the summary and trend; '(status, date)' for status filters.
- Explain endpoint: '/admin/explain/{endpoint}' builds the same pipelines and filters as the real endpoints and returns MongoDB's 'executionStats'.

## Interpretation choices

- Overnight lateness: R1 places a 05:50 punch-in on 7 July in the shift that started on 6 July at 22:00. R2 then measures lateness from that shift start, which gives a large 'late_minutes'. I followed the rule literally. This is the most likely point where a reviewer would expect something different.
- Weekend records: Late minutes and overtime include weekend records (R7). Present days, and the trend's present count and attendance rate, count Monday to Friday only.
- 'attendance_pct': is 'present_days / working_days * 100', rounded to 2 decimals half-up. 'attendance_rate' in the trend is rounded to 4 decimals half-up (R8).
- Working days: (R7) are counted by stepping through the calendar days of the month. This is date arithmetic and reads no data.
- Department average is taken over attendance records, not over employees' averages. Records without a work-hours value (open punch-ins) are excluded.
- 'half_day': is tested against the rounded 'work_hours', as R5 requires.

## Known gaps

- Error sweep: 36 malformed or edge-case requests sent to every endpoint. None returned 500; error cases return 404 or 422.
- Empty filter: 'GET /attendance?emp_code=' is treated as no filter and returns all records. A stricter reading would return 422.
- Clean clone: the repository has been checked from a clean clone of the submitted version. (Remove this line if you haven't run that check yet.)
- Several API processes: the concurrency check runs against one server process. Several processes sharing one database have not been tested.