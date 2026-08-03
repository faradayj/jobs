# Track Specification: Fix Automatic 'Applied' Status Marking in Tracker CSV & DB

## Goal
Ensure `jobs_tracker.csv` and `jobs.db` automatically record `Status = 'Applied'` and `Date Applied` when a candidate completes an application via `app_workday.py`, `app_greenhouse.py`, or `app_ashby.py`, even when URLs redirect (e.g., Workday locale `/en-US/` and tail `/apply/applyManually` paths).

## Technical Requirements
1. Update `clean_url(url)` in `src/job_tracker.py` to strip locale prefixes (`/en-US/`, `/en-CA/`, `/fr/`) and apply tail paths (`/apply`, `/applyManually`) so redirected URLs match CSV/DB records.
2. Update `set_status_by_url()` in `src/job_tracker.py` to update both `data/jobs_tracker.csv` and `data/jobs.db` (if SQLite DB exists).
3. Ensure `app_workday.py` passes the initial un-redirected `job_url` parameter to `mark_applied_by_url()`.
4. Verify via isolated python unit test (`scratch/test_mark_applied_fix.py`).
