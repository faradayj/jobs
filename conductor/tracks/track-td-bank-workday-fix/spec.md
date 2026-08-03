# Track Specification: Fix TD Bank Workday Form Navigation & Candidate Residence Country Override

## Goal
Fix application form navigation for TD Bank Workday listing (`https://td.wd3.myworkdayjobs.com/en-US/TD_Bank_Careers/job/Toronto-Ontario/Data-Engineer-I_R_1498701`) so international/Canadian listings advance past Page 1 (`My Information`) to Page 2 (`My Experience`) and complete successfully.

## Technical Requirements
1. Enforce candidate profile country (`PI["country"]` = `"United States of America"`) for contact address `Country` fields in `src/app_workday.py`.
2. Prioritize exact string equality (`o.strip().lower() == value.strip().lower()`) in `exec_button_dropdown()` before calling `fuzzy_pick()`.
3. Clarify `SYSTEM_PROMPT` in `src/app_common.py` that personal contact address `Country` uses candidate profile residence.
4. Verify via Playwright browser execution on TD Bank listing (`https://td.wd3.myworkdayjobs.com/en-US/TD_Bank_Careers/job/Toronto-Ontario/Data-Engineer-I_R_1498701`).
