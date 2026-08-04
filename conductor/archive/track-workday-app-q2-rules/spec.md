# Specification: Workday Application Questions 2 of 2 (Credit Hours & Student Status Rules)

## Functional Requirements
1. **Candidate Profile Library Update (`data/library.json`)**:
   - Add `"anticipated_completed_credit_hours": "30"` under the candidate's active Master's degree entry at Arizona State University.
   - Add standard Q&A reference entries for credit hours (`30`), current degree seeking enrollment (`Yes`), degree program (`Masters`), GPA (`3.5 or higher` / `4.0`), conditional major (`Computer Science`), and conditional permanent address (`800 S Abel St, Unit 506, Milpitas, CA 95035, United States`).

2. **Rules Engine & Prompt Updates (`src/app_common.py`)**:
   - Update `SYSTEM_PROMPT` to instruct DeepSeek on credit hours (`30`), student enrollment (`Yes`), degree program (`Masters`), cumulative GPA (`3.5 or higher`), conditional major (`Computer Science`), and permanent address.
   - Expand `rule_based_answer()` to match label patterns for credit hours, degree program enrollment, GPA dropdown/text, conditional major, and conditional address.

## Acceptance Criteria
- Automated diagnostic test suite passes for all 6 target question patterns.
- Target Workday listings fill `30` credit hours on `Application Questions 2 of 2` without stalling.
