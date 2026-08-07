# Implementation Plan: Protect LLM Answers & Refine Education Block Parsing

- [ ] **Task 1: Add Personal Projects Data & Update System Prompt (`data/library.json` & `src/app_common.py`)**
  - Add `personal_projects` entry to `data/library.json`.
  - Include `personal_projects` in `PROFILE_SUMMARY` and update `SYSTEM_PROMPT` in `src/app_common.py`.

- [ ] **Task 2: Enforce Immutability & Defensive Education Matching (`src/app_greenhouse.py`)**
  - Protect `ds_indices` and `<textarea>` fields from post-processing mutation in `src/app_greenhouse.py`.
  - Exclude question keywords and enforce label length limit (< 60 chars) on education block fields.

- [ ] **Task 3: Verification Unit & Integration Test Suite**
  - Run `scratch/test_samsara_filled_screenshot.py` verifying Field 19 retains DeepSeek's response and is never overwritten.
