# DECISIONS.md

1. **Indexes.** I create a unique index on `employees.emp_code` so duplicate employee codes cannot be inserted, plus department/code and department/join-date indexes for filtered employee queries and headcount. Attendance has a unique `(emp_code, date)` index for one record per employee per attendance day, a `(date, emp_code)` index for the sorted attendance list, an `(emp_code, punch_in)` index for finding the latest punch, and a `(date, status)` index for analytics. I would avoid adding indexes that duplicate an existing index's useful prefix without measured benefit, because each index costs storage and slows writes.

2. **Punch-in race.** Both requests may try to insert, but MongoDB's unique `(emp_code, date)` index allows only one. The successful insert returns 201; the duplicate-key error is caught and returns 409.

3. **Ties.** The leaderboard ranks by total late minutes with a ranking window. Employees tied at the cutoff share a rank and are all included; the result can contain more than `limit` rows.

4. **Headcount.** The summary starts from eligible employees and left-joins attendance, so employees with no logs still contribute to headcount.

5. **100x data.** I would benchmark query plans and latency, then consider pre-aggregated daily summaries and a scheduled refresh for analytics, while keeping the source attendance records authoritative.
