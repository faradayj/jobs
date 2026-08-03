# Track Specification: RTX Workday LinkedIn OAuth Authentication Handler

## Goal
Support RTX Workday job application portals (`rec_rtx_ext_gateway` / `globalhr.wd5.myworkdayjobs.com`) which require OAuth authentication via LinkedIn ("Sign in with LinkedIn") rather than standard Workday email/password inputs.

## Technical Requirements
1. Strictly scope LinkedIn OAuth sign-in path to RTX portals (`"rtx" in current_url.lower() or tenant in ("rec_rtx_ext_gateway", "globalhr")`) in `workday_auth()` inside `src/app_workday.py`.
2. Locate and click `[data-automation-id='LinkedInSignInButton']` or `button:has-text('Sign in with LinkedIn')`.
3. Fill candidate email (`PI["email"]`) and password (`PASSWORD`) on `linkedin.com` OAuth page (`input#username` and `input#password`).
4. Wait for authentication callback redirect back to Workday (`myworkday.com` / `myworkdayjobs.com`).
5. Ensure non-RTX Workday portals remain 100% unaffected and continue using direct email/password login.
