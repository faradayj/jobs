# Implementation Plan: Workday Multi-Option Sign-In Landing Page Handler (NVIDIA & RTX)

- [x] **Task 1: Update `LOGIN_SELECTORS` & Multi-Option Click in `src/app_workday.py`**
  - Add `SignInWithEmailButton` variants to `LOGIN_SELECTORS`.
  - Add pre-flight click check in `ensure_signed_in()` to expand "Sign in with email" when present.

- [x] **Task 2: Verification Unit Test Suite**
  - Run Playwright test script `scratch/test_nvidia_multi_option_login.py` to verify that `ensure_signed_in()` detects `SignInWithEmailButton`, clicks it, and reveals standard `email` & `password` inputs on NVIDIA Workday portal.
