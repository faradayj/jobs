# Specification: NVIDIA Multi-Option Sign-In Landing Page Handler

## Overview
Fix authentication failure on NVIDIA (`https://nvidia.wd5.myworkdayjobs.com/...`) and similar Workday portals where the initial login screen displays multiple auth options (*"Sign in with Google"*, *"Sign in with email"*).

## Functional Requirements
1. **Comprehensive Selectors**:
   - Expand `email_option_selectors` in `ensure_signed_in()` to cover all text/attribute variants (*"Sign in with email"*, *"Sign in with Email"*, *"Sign In with Email"*, *"Sign in with email/password"*).
2. **Explicit State Synchronization**:
   - After clicking *"Sign in with email"*, explicitly await `[data-automation-id='email']` visibility (`timeout=8000ms`) before reading `has_signin_btn` and executing `_do_sign_in()`.

## Acceptance Criteria
- Unit test verifying that navigating to NVIDIA applyManually clicks *"Sign in with email"*, waits for email input, fills credentials, and successfully authenticates.
