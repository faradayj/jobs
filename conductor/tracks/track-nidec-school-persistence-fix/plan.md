# Implementation Plan: Fix Education School / University Name Persistence Glitch

- [x] **Task 1: Update School/University Fallbacks & Remove Destructive `Escape` in `src/app_workday.py`**
  - Build multi-term institution fallbacks in `fill_add_dialog()` for school/university fields.
  - Remove `await page.keyboard.press("Escape")` after `exec_selectinput()` in `handle_my_experience()`.
  - Add explicit `Tab` blur on text-input school re-fills.

- [x] **Task 2: Verification Unit Test Suite**
  - Write and run diagnostic test `scratch/test_school_persistence.py` verifying multi-term fallbacks and pill retention.
