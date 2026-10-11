# REVIEW.md

The starter was reviewed against `openapi.yaml` and `DATA_MODEL.md`. The final implementation is in `app/main.py`.

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | The 10-minute grace is strict: exactly 10:00 late is not late, but 10:00.001 is; the stored number is whole minutes since shift start, not after grace. | Shift 09:30: compare 09:40:00 with 09:40:01 and 10:15:59. | Truncate instants to whole seconds, then apply `elapsed > 600`; return floored whole minutes from shift start. |
| 2 | `compute_work_hours` | Binary floating-point/banker's rounding can give the wrong 2-decimal result. | Use a duration whose third decimal is exactly 5. | Use `Decimal` and `ROUND_HALF_UP`. |
| 3 | `compute_overtime` / punch-out calculations | Overnight shifts must use the following calendar day's shift-end boundary; overtime under 30 minutes is zero. | Shift 22:00–06:00, punch out next day at 06:20 and 06:30. | The shared derived-field calculation advances the shift-end date for overnight shifts and applies the 30-minute threshold. |
| 4 | `POST /employees` | Duplicate checks alone are not safe for simultaneous requests; both requests could pass a check before either insert. | Send two creates with the same `emp_code` concurrently. | Create a unique `emp_code` index at startup and map `DuplicateKeyError` to 409. |
| 5 | `GET /employees` | Pagination must be 1-based and `total` must count the filtered department, not all employees. | Request page 1 with a department filter and compare `total` with that department's count. | Use `(page - 1) * page_size`, count the same filter, and sort by `emp_code`. |
| 6 | `POST /attendance/punch-in` | A check-then-insert is race-prone and timestamps must be epoch milliseconds, not seconds or local naive datetimes. | Concurrent same-day punch-ins; send a seconds timestamp or string timestamp. | Use a unique `(emp_code, date)` index, validate epoch milliseconds, normalize to UTC, and return 409 on duplicate key. |
| 7 | `GET /attendance` | Loading every matching record into Python is slow at 100,000 records; invalid date ranges/statuses must be rejected. | Use a large collection, `date_from > date_to`, or an unsupported status. | Apply filters, count the filtered set, sort and paginate in MongoDB, and validate dates/status. |
| 8 | `POST /attendance/punch-out` | Closing a record must be atomic so concurrent punch-outs cannot both succeed; overnight duration and derived fields must be correct. | Send two punch-outs at the same time; test an overnight shift. | Use `find_one_and_update` with `punch_out: None` and calculate work hours, overtime and half-day consistently. |
| 9 | `PATCH /attendance/{emp_code}/{date}` | Corrections must recompute stored derived fields, record only actual changes, and append history without silently overwriting concurrent corrections. | Correct a record twice concurrently, including a seeded record with no `history` field. | Recompute derived values, append one audit entry, and use an optimistic concurrency filter that treats missing history as empty. |
| 10 | Analytics endpoints | Analytics must use stored derived values, count weekdays from `joined_on`, include employees without logs in headcount, preserve leaderboard ties, and fill missing trend dates. | Test no-log employees, a mid-month joiner, weekend records, a missing weekday and ties at the limit. | Use MongoDB aggregation pipelines and window functions, plus date densification for the trend. |
| 11 | `GET /admin/explain/{endpoint}` | An explain plan must correspond to the endpoint's query shape and use execution statistics; a generic partial `$match` does not prove the real query is indexed. | Compare the explain filters/pipeline to the real endpoint and inspect for IXSCAN/COLLSCAN. | Added an explain endpoint for the five supported endpoint names. Check each plan on the full seeded dataset before submission. |

## Reviewed and not considered defects

- `present_days` excludes weekend attendance, but weekend records still contribute to late/overtime totals; this is explicitly required by R7.
- Department `avg_work_hours` includes completed presence records on weekends. R7 excludes weekends only from `present_days`, not from stored-work-hours averages.
- A missing weekday record is not equivalent to an `ABSENT` record; the analytics must not manufacture absence records.
