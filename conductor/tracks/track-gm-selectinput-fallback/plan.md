# Implementation Plan: Workday SelectInput 'How Did You Hear About Us?' Fallback Handler

- [ ] **Task 1: Update `exec_selectinput()` in `src/app_workday.py`**
  - Add fallback term expansion for `"how did you hear"` / `"referral"` fields in `exec_selectinput()`.
  - Add keyword recovery fallback for unfiltered option results to select valid career/website options.

- [ ] **Task 2: Verification Unit Test Suite**
  - Run diagnostic test `scratch/test_selectinput_hear_about_us.py` to verify resolution on GM Workday options.
