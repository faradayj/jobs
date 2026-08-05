# Implementation Plan: Restrict LinkedIn OAuth Sign-In Strictly to RTX Workday Portals

- [ ] **Task 1: Refactor `workday_login()` in `src/app_workday.py`**
  - Wrap all calls to `_do_linkedin_sign_in()` behind explicit `if is_rtx:` checks.
  - Ensure fallback sign-in/account creation in State A and State B never calls `_do_linkedin_sign_in()` when `is_rtx` is `False`.

- [ ] **Task 2: Refactor `LOGIN_SELECTORS` & `is_rtx` Detection in `src/app_workday.py`**
  - Ensure `LOGIN_SELECTORS` only uses LinkedIn button selectors when `is_rtx` is `True`.

- [ ] **Task 3: Run Verification Unit Test Suite**
  - Write and run diagnostic test `scratch/test_rtx_login_gate.py` to verify `is_rtx` gate and login routing for RTX vs non-RTX portals.
