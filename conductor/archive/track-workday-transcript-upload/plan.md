# Implementation Plan: Workday Transcript Upload & Q&A Handler (`data/transcript.pdf`)

- [x] **Task 1: Add `TRANSCRIPT_PATH` & Transcript Rules to `src/app_common.py`**
  - Define `TRANSCRIPT_PATH` resolving to `data/transcript.pdf`.
  - Add transcript question guidelines to `SYSTEM_PROMPT` and `rule_based_answer()`.

- [x] **Task 2: Implement Smart File Upload Router in `src/app_workday.py`**
  - In `handle_my_experience()` and `smart_fill_page()`, inspect dropzone label context.
  - Route transcript dropzones to `TRANSCRIPT_PATH` and resume dropzones to `RESUME_PATH`.

- [x] **Task 3: Run Verification Unit Test Suite**
  - Write and run diagnostic test `scratch/test_transcript_rules.py` to verify `TRANSCRIPT_PATH` resolution and `rule_based_answer()` behavior.
