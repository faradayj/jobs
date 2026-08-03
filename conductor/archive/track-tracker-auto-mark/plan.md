# Track Implementation Plan: Fix Automatic 'Applied' Status Marking in Tracker CSV & DB

- [x] **Task 1**: Update `clean_url()` and `set_status_by_url()` in `src/job_tracker.py` to handle Workday/Greenhouse locale & tail path normalization and dual CSV/DB updates.
- [x] **Task 2**: Perform verification test using redirected Workday URLs to confirm `Applied` status is successfully recorded in `data/jobs_tracker.csv` and `data/jobs.db`.
