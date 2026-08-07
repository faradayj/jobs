# Specification: Protect LLM Answers & Refine Education Block Parsing

## Overview
Eliminate fragile ad-hoc regex overrides in `src/app_greenhouse.py` and enforce strict immutability for LLM/textarea answers:
1. **LLM Answer Immutability**: Any field answered by DeepSeek (`ds_indices`) or rendered as a `<textarea>` / open-ended prose field must be protected from post-processing mutation.
2. **Defensive Education Field Filtering**: Education block override parsing must strictly exclude labels containing `"schoolwork"`, `"project"`, or `"initiative"`, as well as labels longer than 60 characters.
3. **Personal Projects Addition**: Add an explicit `personal_projects` entry to `data/library.json` and `PROFILE_SUMMARY` detailing the candidate's Open-Source LLM Browser Automation Agent (Playwright, Python, Ollama, LiteLLM).

## Functional Requirements
- In `data/library.json`, add `personal_projects` entry for the Open-Source LLM Browser Automation Agent.
- In `src/app_common.py`, include `personal_projects` in `PROFILE_SUMMARY` and update `SYSTEM_PROMPT` rules for non-internship/non-schoolwork project handling.
- In `src/app_greenhouse.py`, add protection checks preventing post-processing loops from mutating `ds_indices` or textareas, and restrict education field label matching.

## Acceptance Criteria
- Full `app_greenhouse.main()` run on Samsara listing confirms Field 19 retains DeepSeek's answer (Open-Source LLM Browser Automation Agent) and is **NEVER** overwritten with `"University of California, San Diego"`.
