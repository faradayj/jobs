# Implementation Plan: Fix Transcript Dropzone Misidentification & Resume Overwrite

- [ ] **Task 1: Update `handle_document_uploads()` in `src/app_workday.py`**
  - Ascend parent DOM containers to extract full context text (section header, legend, label).
  - Strictly route transcript dropzones to `TRANSCRIPT_PATH` and resume dropzones to `RESUME_PATH`.
  - Remove generic `or not existing_uploads` fallback to prevent misidentifying transcript fields as resume fields.

- [ ] **Task 2: Verification Unit Test Suite**
  - Write and run diagnostic test `scratch/test_dropzone_context.py` verifying accurate classification and routing.
