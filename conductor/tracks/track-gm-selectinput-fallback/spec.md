# Specification: Workday SelectInput 'How Did You Hear About Us?' Fallback Handler

## Overview
Fix form navigation blocking on General Motors (`https://generalmotors.wd5.myworkdayjobs.com/...`) and similar Workday portals where `How Did You Hear About Us?*` is rendered as an `input` (`selectInput` / combobox) rather than a `button` dropdown, and does not return a direct match for `"LinkedIn"`.

## Functional Requirements
1. **`exec_selectinput` Fallback Term Sequence**:
   - For fields matching `"how did you hear"`, `"source"`, or `"referral"`, expand `terms` to include full fallback hierarchy (`"LinkedIn"`, `"Internet/Online Job Posting"`, `"Online Job Board"`, `"Job Board"`, `"Social Media"`, `"Company Website"`, `"Careers Website"`, `"GM Careers Website"`, `"GM.com Website"`, `"Recruiter"`, `"Event"`, `"Other"`).
2. **Unfiltered Options Fallback Selection**:
   - If Workday returns option results without a direct token match for `"LinkedIn"`, evaluate fallback keywords (`"careers"`, `"website"`, `"social"`, `"recruiter"`, `"event"`, `"other"`).
   - Select the highest-ranked matching option (e.g. `'GM Careers Website'`) and confirm via Enter / Tab press.

## Acceptance Criteria
- Unit test verifying GM Workday results `['Career Fair / Professional Organization Event', 'Contacted by Company Recruiter', 'Email / Newsletter', 'GM.com Website', 'GM Careers Website']` resolve to `'GM Careers Website'`.
- Successful application form fill without getting blocked on GM Workday listing.
