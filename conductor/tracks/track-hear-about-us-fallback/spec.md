# Specification: Robust Fallback Handler for 'How Did You Hear About Us?'

## Overview
Handle Workday job portals where the "How did you hear about us?" dropdown field does not include `"LinkedIn"` as an option. The bot will automatically iterate through a ranked list of fallback options (e.g., `"Internet/Online Job Posting"`, `"Online Job Board"`, `"Social Media"`, `"Company Website"`, `"Careers Site"`, `"Search Engine"`, `"Advertisement"`, `"Other"`) and select the best available match without failing or getting stuck.

## Functional Requirements
1. **Multi-Term Priority Matching in `exec_button_dropdown`**:
   - Parse `value` (and `hear_about_us_fallback_order` from `library.json` / `JBM`) into candidate terms.
   - For each term, check exact match then fuzzy match against `opts`.
   - If no candidate term matches, fall back to `"Other"` if present, or the first non-disabled option.
2. **Prevent Stuck Validation Errors**:
   - Always pick a valid non-disabled option for "How did you hear about us?" dropdowns.

## Acceptance Criteria
- Unit test verifying option selection across 4 distinct Workday option formats (with LinkedIn, with Job Board, with Other, with Company Website).
- Successful option selection without getting stuck.
