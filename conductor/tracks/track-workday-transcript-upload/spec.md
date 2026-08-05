# Specification: Workday Transcript Upload & Q&A Handler (`data/transcript.pdf`)

## Overview
Support Workday job applications (such as Ciena and similar tech/new-grad listings) that require or request an academic transcript (`data/transcript.pdf`). The bot will automatically:
1. Detect and upload `data/transcript.pdf` (`TRANSCRIPT_PATH`) to transcript-specific file upload dropzones or additional document dropzones across all application steps.
2. Update the rule-based engine and library in `src/app_common.py` to automatically answer `"Yes"` to questions regarding transcript uploads (e.g., *"In order to move forward, you must upload your most recent transcript. Have you done this?"*).

## Functional Requirements
1. **`TRANSCRIPT_PATH` Resolution**:
   - Resolve `TRANSCRIPT_PATH` in `src/app_common.py` from `library.json` key `"transcript_path"` or default relative path `data/transcript.pdf`.
2. **Rules Engine Transcript Question Matching**:
   - Update `rule_based_answer()` and `SYSTEM_PROMPT` in `src/app_common.py` to answer `"Yes"` to transcript upload verification questions.
3. **Smart File Upload Routing**:
   - In `src/app_workday.py` (`handle_my_experience` and `smart_fill_page`), inspect dropzone labels and fieldset titles.
   - If context contains `"transcript"`, `"academic record"`, `"grades"`, `"mark sheet"`, set input files to `TRANSCRIPT_PATH`.
   - If context contains `"resume"`, `"cv"`, set input files to `RESUME_PATH`.

## Acceptance Criteria
- Unit test verifying `TRANSCRIPT_PATH` resolution and `rule_based_answer()` returning `"Yes"` for transcript verification questions.
- Successful application execution on Ciena Workday job listing.
