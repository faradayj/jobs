# Specification: Fix Education School / University Name Persistence Glitch

## Overview
Fix an issue on Nidec (`https://nidec.wd1.myworkdayjobs.com/...`) and other Workday portals where the selected School / University name is wiped out or fails to persist upon form completion or retry.

## Functional Requirements
1. **Remove Destructive `Escape` Keypress**:
   - Remove `await page.keyboard.press("Escape")` in `handle_my_experience()` right after `exec_selectinput()` calls (lines 2963 & 2976) so selected Workday combobox pills are not canceled/cleared.
2. **Multi-Term Fallbacks for School & University Fields**:
   - In `fill_add_dialog()`, build fallback strings (`"Arizona State University\nASU\nUC San Diego"`) using `institution_variants` for `school`, `university`, and `institution` fields.
3. **Commit React State on Text Re-fills**:
   - Ensure text-input school re-fills dispatch `change` and `blur` events and press `Tab` so React state commits cleanly.

## Acceptance Criteria
- Unit test verifying multi-term institution fallback assembly and combobox pill retention without `Escape` cancellation.
