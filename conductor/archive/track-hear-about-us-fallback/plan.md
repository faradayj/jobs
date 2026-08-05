# Implementation Plan: Robust Fallback Handler for 'How Did You Hear About Us?'

- [x] **Task 1: Update `rule_based_answer()` & `exec_button_dropdown()` in `src/app_common.py` and `src/app_workday.py`**
  - Update `hear_about_us` fallback list in `src/app_common.py`.
  - Update `exec_button_dropdown()` in `src/app_workday.py` to evaluate multiline/priority candidate terms against `opts` and fall back to `"Other"` or first non-disabled option.

- [x] **Task 2: Verification Unit Test Suite**
  - Write and run diagnostic test `scratch/test_hear_about_us.py` to verify option selection across all Workday portal variations.
