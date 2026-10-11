# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.** The unique '(emp_code, date)' index enforces one record per day and serves monthly lookups. The unique 'emp_code' index enforces identity. '(emp_code, punch_in desc)' finds the open record for a punch-out. '(date desc, emp_code asc)' serves the list sort and date-range analytics. '(department, emp_code)' serves trend lookups. I rejected a single 'date' index, because it cannot serve per-employee lookups.

2. **Punch-in race.** Both requests compute the same date and call 'insert_one'. The unique '(emp_code, date)' index lets exactly one insert succeed. The other raises 'DuplicateKeyError', which becomes 409. Nothing is checked before the insert, so both requests cannot pass a check.

3. **Ties.** '$rank' gives equal totals the same rank and skips the next (1, 2, 2, 4). 'limit' applies after ranking, so everyone at rank 'limit' or better is returned, even if that exceeds 'limit'. Rows sort by total descending, then 'emp_code'.

4. **Headcount.** The summary starts from employees who joined by month end, then joins their logs. Employees with no logs keep one row through 'preserveNullAndEmptyArrays' and headcount counts distinct 'emp_code' values.

5. **At 100x.** I would stop computing trends per request and keep a daily per-department rollup, updated on punch and regularize. I would also shard 'attendance_logs' on 'emp_code' and 'date'.

