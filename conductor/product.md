# Product Definition

## Vision & Overview
The **Job Application Automator** is an intelligent, Playwright-driven Python automation system designed to streamline online job applications across major Applicant Tracking Systems (ATS) including Workday, Greenhouse, and Ashby.

Driven by structured candidate data (`data/library.json`) and DeepSeek LLM reasoning with rule-based fallback mechanisms, the system automates field discovery, complex form filling, date spinbuttons, dynamic lists, and combobox selection while adhering to candidate preferences.

## Key Features
- **ATS Form Filling**: End-to-end form navigation and filling for Workday, Greenhouse, and Ashby portals.
- **Candidate Data Integration**: Dynamic profile matching from `data/library.json` and PDF resume parsing.
- **DeepSeek & Rule-Based Hybrid Intelligence**: Combines LLM inference with fast local deterministic rule engines to select dropdown options, checkboxes, radio groups, and combobox search pills.
- **Review Page Safeguard**: Automates filling up to the final Review page, pausing for manual inspection before submission.
- **Job Tracker Pipeline**: CLI pipeline (`src/job_tracker.py`) for ingesting, evaluating, and applying to job batches.
