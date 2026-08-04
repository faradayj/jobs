# Implementation Plan: Workday Application Questions 2 of 2 (Credit Hours & Student Status Rules)

- [ ] **Task 1: Update Candidate Profile Library (`data/library.json`)**
  - Add `"anticipated_completed_credit_hours": "30"` to education history and add Q&A references to `standard_answers`.

- [ ] **Task 2: Expand Rules Engine & System Prompt (`src/app_common.py`)**
  - Add explicit student status and credit hours rules (`30`) to `SYSTEM_PROMPT`.
  - Add label pattern matchers in `rule_based_answer()` for credit hours (`30`), degree program enrollment (`Yes`), degree program (`Masters`), GPA (`3.5 or higher` / `4.0`), conditional major (`Computer Science`), and conditional address.

- [ ] **Task 3: Run Verification Test Suite**
  - Execute diagnostic test script `scratch/test_app_q2_rules.py` to verify logic against all 6 target question shapes.
