# Implementation Plan: Fix Greenhouse Navigation Timeout & Samsara EEO/Project Question Filling

- [ ] **Task 1: Update Navigation Timeouts & Answer Rules (`src/app_greenhouse.py` & `src/app_common.py`)**
  - Replace `wait_until="networkidle"` with `wait_until="domcontentloaded"` in `src/app_greenhouse.py`.
  - Add open-ended project question rule and enhance EEO demographic decline option handling in `src/app_common.py`.

- [ ] **Task 2: Verification Unit Test Suite**
  - Run `scratch/test_samsara_full_run.py` verifying clean startup navigation, EEO decline matching, and project blurb population.
