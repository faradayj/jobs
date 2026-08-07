# Implementation Plan: Persistent Applied Job Tracking & Manifest Enforcer

- [x] **Task 1: Add Persistent Applied Manifest & Ingest Hooks in `src/job_tracker.py`**
  - Implement `APPLIED_HISTORY_PATH`, `save_to_applied_manifest()`, and `sync_applied_manifest()`.
  - Hook `sync_applied_manifest()` into `import_csv_to_db()`, `run_ingest()`, and `export_db_to_csv()`.

- [x] **Task 2: CLI Options (`--url`, `--id`, `--search`) in `src/job_tracker.py`**
  - Update `main()` argument parser to accept `--url` (string or list), `--id` (int or list), and `--search` (query string).
  - Update `mark_applied()` and `mark_applied_by_url()` to save to `applied_history.json`.

- [x] **Task 3: Verification Unit Test Suite**
  - Write and run diagnostic test `scratch/test_applied_manifest.py`.
  - Verify that marking NVIDIA URL as applied persists across `run_ingest()`.
