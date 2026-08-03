# Track Specification: Fix IXL Resume Upload & Education Combobox Selection

## Goal
Fix candidate resume file uploading and education combobox field selection (school name & field of study) in `src/app_greenhouse.py` for IXL Learning and custom Greenhouse embedded application forms.

## Technical Requirements
1. Update `gh_exec_file()` in `src/app_greenhouse.py` to check `file_input.count() == 0` instead of `is_visible()` so hidden `<input type="file">` elements are populated with `set_input_files()`.
2. Expand `isSelectInput` check in `scan_fields()` to classify `sf__input`, `df__input`, `role="combobox"`, and `[class*='__input']` as comboboxes (`isSelectInput: true`).
3. Expand menu container and option locators in `gh_exec_react_select()` to match `[class*='__option']`, `.select__option`, `[id*='option']`.
4. Verify via direct Playwright browser testing on live IXL listing (`https://www.ixl.com/company/jobs?gh_jid=8615710002`).
