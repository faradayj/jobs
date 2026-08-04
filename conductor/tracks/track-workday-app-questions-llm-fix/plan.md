# Track Implementation Plan: Fix Workday Application Questions LLM JSON Extraction & Button Dropdown Rules

- [x] **Task 1**: Update `deepseek_fill_page()` and `rule_based_answer()` in `src/app_common.py` to fix JSON extraction and handle Workday button dropdown questions.
- [x] **Task 2**: Perform end-to-end verification test on target Workday job listing (`https://globalhr.wd5.myworkdayjobs.com/en-US/Private_Posting_No_TMP/job/US-CA-FULLERTON-675--1801-Hughes-Dr--BLDG-675/Software-Engineer-I--Onsite-_01863008`) to confirm `Application Questions 1 of 2` populates cleanly and advances to Review.
