# Specification: Persistent Applied Job Tracking & Manifest Enforcer

## Overview
Provide a robust, bulletproof way to mark job listings as `Applied` (by URL, ID, or search query) and ensure the `Applied` status is permanently persisted in `data/applied_history.json` and `jobs_tracker.csv`. This prevents GitHub ingestion (`ingest`), re-scoring (`evaluate`), or database re-initialization from ever overwriting previously applied jobs (such as the NVIDIA listing) back to pending/eligible status.

## Functional Requirements
1. **Persistent Manifest Manager (`data/applied_history.json`)**:
   - Save all applied job records to `data/applied_history.json`.
   - Implement `sync_applied_manifest()` to enforce `Status = 'Applied'` across `jobs_tracker.csv` and `jobs.db`.
2. **Hook Ingest & Import Pipeline**:
   - Automatically execute `sync_applied_manifest()` during `import_csv_to_db()`, `run_ingest()`, and `export_db_to_csv()`.
3. **CLI Commands**:
   - Support `python src/job_tracker.py apply` and `mark-applied` with `--url <URL>`, `--id <ID>`, or `--search <QUERY>`.

## Acceptance Criteria
- Unit test verifying persistent manifest creation, ingestion protection, and CLI options.
- Marking NVIDIA URL as applied persists across `run_ingest()`.
