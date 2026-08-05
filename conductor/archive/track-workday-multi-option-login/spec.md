# Specification: Workday Multi-Option Sign-In Landing Page Handler (NVIDIA & RTX)

## Overview
Support Workday portals (such as NVIDIA, RTX, and others) that present a multi-option sign-in landing page (e.g. "Sign in with Google", "Sign in with LinkedIn", "Sign in with email"). For non-RTX portals, the bot will detect the multi-option view, click "Sign in with email" (`SignInWithEmailButton`), reveal the standard Workday credentials form (`email`, `password`, `signInSubmitButton`, `createAccountLink`), and proceed with standard account authentication.

## Functional Requirements
1. **Multi-Option Recognition**:
   - Expand `LOGIN_SELECTORS` to include `[data-automation-id='SignInWithEmailButton']`, `[data-automation-id='signInWithEmailButton']`, `button:has-text('Sign in with email')`, and `button:has-text('Sign in with Email')`.
2. **Email Expand Action**:
   - In `ensure_signed_in()`, before evaluating State A/B, check if a multi-option "Sign in with email" button is visible for non-RTX portals.
   - If present, click "Sign in with email" and wait 1500ms to reveal standard `email` and `password` inputs.
3. **Preserve RTX LinkedIn Routing**:
   - RTX portals (`is_rtx == True`) continue to use LinkedIn OAuth sign-in (`_do_linkedin_sign_in()`).

## Non-Functional Requirements
- Zero regression for standard Workday portals (Danaher, Capital One, TD Bank, etc.).
