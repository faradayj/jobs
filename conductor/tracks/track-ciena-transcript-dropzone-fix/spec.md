# Specification: Fix Transcript Dropzone Misidentification & Resume Overwrite

## Overview
Fix an issue on Ciena (`https://ciena.wd5.myworkdayjobs.com/...`) and other Workday job listings where document upload dropzones for transcripts misidentify as resume dropzones and automatically upload `RESUME_PATH` instead of `TRANSCRIPT_PATH` (`data/transcript.pdf`). Additionally, fix frame-1 auto-overwrites that prevent users from manually replacing wrong uploads.

## Functional Requirements
1. **Deep DOM Context Traversal**:
   - Ascend parent elements (up to 8 levels or fieldsets/sections) in `handle_document_uploads()` to capture section titles (`"Academic Transcript"`, `"School Records"`, `"Resume / CV"`).
2. **Strict Dropzone Routing**:
   - Route transcript dropzones strictly to `TRANSCRIPT_PATH` and resume dropzones to `RESUME_PATH`.
   - Remove generic fallback `or not existing_uploads` on resume dropzone matching so non-resume dropzones are never populated with `RESUME_PATH`.

## Acceptance Criteria
- Unit test verifying accurate dropzone classification and document routing on Workday forms.
