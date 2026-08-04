# Track Specification: Fix Workday Application Questions LLM JSON Extraction & Button Dropdown Rules

## Goal
Fix application form failure on Workday listing `https://globalhr.wd5.myworkdayjobs.com/en-US/Private_Posting_No_TMP/job/US-CA-FULLERTON-675--1801-Hughes-Dr--BLDG-675/Software-Engineer-I--Onsite-_01863008` caused by LLM JSON extraction error (`JSONDecodeError: Extra data`) and missing Workday button dropdown question rules.

## Technical Requirements
1. Update `deepseek_fill_page()` in `src/app_common.py` using `json.JSONDecoder().raw_decode()` to extract the first valid JSON object starting at `{`, ignoring trailing commentary or extra text from DeepSeek.
2. Expand `rule_based_answer()` in `src/app_common.py` so that Workday button dropdown questions (`tag == "button"`) for US citizenship, prior employment, federal/military status, auditor/PwC status, and politically exposed person status are answered deterministically by rules.
3. Verify via Playwright browser execution on the target RTX Workday listing (`Software-Engineer-I--Onsite-_01863008`).
