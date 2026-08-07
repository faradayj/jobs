# Specification: Fix Greenhouse Navigation Timeout & Samsara EEO/Project Question Filling

## Overview
Fix an issue where Greenhouse applications on Samsara (`https://www.samsara.com/company/careers/roles/8097343?gh_jid=8097343`) and other company-hosted portals break at startup due to `wait_until="networkidle"` navigation timeouts. Additionally, improve rule-based answers for custom EEO demographic dropdowns and open-ended project questions.

## Functional Requirements
1. **Navigation Timeout Fix (`src/app_greenhouse.py`)**:
   - Replace `wait_until="networkidle"` with `wait_until="domcontentloaded"` across all `page.goto()` calls in `src/app_greenhouse.py`.
2. **EEO & Demographic Answer Matching (`src/app_common.py`)**:
   - Enhance demographic rules (`gender identity`, `race`, `ethnicity`, `disability`) to prefer `"Decline to self-identify"`, `"I do not wish to answer"`, or `"Prefer not to say"` decline options rather than falling back to the first dropdown option (`Agender`, `Black`, etc.).
3. **Open-Ended Project Question Rule (`src/app_common.py`)**:
   - Ensure project/initiative questions (e.g. *"Tell us about a project you built or led..."*) return a project experience blurb instead of mis-matching to school name (`Arizona State University`).

## Acceptance Criteria
- Full test run on Samsara listing navigating cleanly without `networkidle` timeouts and filling EEO decline options and project blurbs correctly.
