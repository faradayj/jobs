# Implementation Plan: NVIDIA Multi-Option Sign-In Landing Page Handler

- [x] **Task 1: Update `ensure_signed_in()` in `src/app_workday.py`**
  - Add comprehensive selectors for *"Sign in with email"* variants.
  - Add explicit state wait `[data-automation-id='email']` after clicking *"Sign in with email"*.

- [x] **Task 2: Verification Unit Test Suite**
  - Run Playwright diagnostic test `scratch/test_nvidia_full_run.py` to verify authentication flow on NVIDIA Workday listing.
