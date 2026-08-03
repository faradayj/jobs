# Track Specification: Fix IXL Learning Greenhouse Embedded Link & Form Discovery

## Goal
Fix Greenhouse job application navigation for IXL Learning (and other embedded company career pages with `?gh_jid=`) in `src/app_greenhouse.py` so that listing URLs automatically navigate from job description landing pages to the fillable application form.

## Technical Requirements
1. Enhance Apply button/link selectors in `app_greenhouse.py` to match `<a href=".../apply...">Apply now</a>` and `a[href*='apply']`.
2. Add automatic navigation detection: when landing on an embedded `gh_jid` page without input fields, click any `Apply` link or navigate to `/careers/apply?gh_jid=...`.
3. Verify via direct Playwright browser testing on live IXL listings (`https://www.ixl.com/company/jobs?gh_jid=8615710002`).
