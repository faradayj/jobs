# Specification: Restrict LinkedIn OAuth Sign-In Strictly to RTX Workday Portals

## Overview
Ensure that LinkedIn OAuth sign-in (`_do_linkedin_sign_in()`) is executed **ONLY** for RTX Workday portals. All non-RTX Workday portals must bypass LinkedIn OAuth sign-in completely and adhere to standard email/password authentication and account creation workflows.

## Functional Requirements
1. **Strict RTX Gate**:
   - Evaluate `is_rtx` based strictly on URL patterns (`"rtx" in current_url.lower()`, `"rec_rtx" in current_url.lower()`) or tenant (`"rec_rtx_ext_gateway"`, `"globalhr"`, `"rtx"`).
   - Only attempt `_do_linkedin_sign_in()` if `is_rtx` is `True`.
2. **Non-RTX Workday Authentication**:
   - For all non-RTX Workday portals (`is_rtx == False`), standard sign-in (`_do_sign_in()`) and account creation (`_do_create_account()`) must be executed.
   - If direct sign-in or account creation requires manual input/verification, proceed via standard fallback without triggering LinkedIn OAuth popups.
3. **Login Wall Selection Detection**:
   - Ensure `LOGIN_SELECTORS` does not trigger login wall handling solely on LinkedIn button elements for non-RTX sites.

## Non-Functional Requirements
- Maintain fast execution time and zero regression for existing Workday portals.

## Acceptance Criteria
- Unit test verifying `is_rtx` gate returns `True` for RTX job URLs and `False` for non-RTX job URLs.
- Standard email/password flow attempted without calling `_do_linkedin_sign_in()` for non-RTX portals.
