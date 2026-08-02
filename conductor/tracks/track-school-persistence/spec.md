# Track Specification: Fix Workday School or University Combobox Pill Selection & Persistence

## Goal
Fix `School or University` field persistence errors on the `My Experience` page across Workday tenants so that school names 1 and 2 lock in option pills and persist on Save without triggering validation errors.

## Technical Requirements
1. Route `School or University` fields to `exec_selectinput` (combobox search mode) in `execute_answer()`.
2. Provide fallback search terms (`"Arizona State"` for ASU, `"UC San Diego"` / `"San Diego"` for UCSD) so Workday's search listbox returns option pills.
3. Click option pill to lock in `selectedItem` React state.
4. Verify using direct Playwright step-by-step browser interaction (no CLI scripts).
