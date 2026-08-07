# Implementation Plan: Fix DeepSeek Dynamic Prompting & Race/EEO Matching

- [ ] **Task 1: Update Profile Context, System Prompt & Greenhouse Option Executor (`src/app_common.py` & `src/app_greenhouse.py`)**
  - Add independent project context and updated EEO / bullet / product guidelines to `SYSTEM_PROMPT` in `src/app_common.py`.
  - Update `gh_exec_react_select()` in `src/app_greenhouse.py` to prevent random fallback clicks on demographic fields.

- [ ] **Task 2: Verification Unit & Integration Test Suite**
  - Run diagnostic test `scratch/test_samsara_deepseek_prompt.py` verifying dynamic response quality and clean field selection.
