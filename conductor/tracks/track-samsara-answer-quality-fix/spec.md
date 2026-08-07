# Specification: Fix DeepSeek Dynamic Prompting & Race/EEO Matching

## Overview
Fix answer quality issues on Samsara (`https://www.samsara.com/company/careers/roles/8097343?gh_jid=8097343`) and other job applications dynamically via prompt engineering, structured profile context, and option execution rules:
1. **Independent Project Distinction**: Provide DeepSeek with explicit structured context separating Independent/Capstone Projects (UCSD DE-HNN Graph-ML chip congestion prediction) from Internship/Employment (BILL).
2. **Dynamic Bullet Formatting**: Instruct DeepSeek in `SYSTEM_PROMPT` to automatically format responses as clean bullet points (`• `) whenever a question asks for "bullets" or "a few bullets".
3. **Company Product & Operating Principles**: Instruct DeepSeek to dynamically synthesize enthusiastic, product-specific responses connecting candidate background to company products (AI Dash Cams, Vehicle Gateways) and principles (*Focus on Customer Impact*, *Build for the Long Term*).
4. **Race / Gender Profile Alignment**: Update DeepSeek instructions and `app_greenhouse.py` option executors to cleanly select candidate profile race (`Asian`) and gender (`Male`/`Man`), eliminating fallback selection to `Black / of African descent`.

## Functional Requirements
- Update `PROFILE_SUMMARY` string builder in `src/app_common.py` to include independent projects (UCSD DE-HNN Graph ML capstone).
- Update `SYSTEM_PROMPT` in `src/app_common.py` with bullet formatting, independent project context, company product/principles guidelines, and EEO race (`Asian`) & gender (`Male`/`Man`) rules.
- Update `gh_exec_react_select()` in `src/app_greenhouse.py` to prevent random fallback clicks on demographic fields when option text matching fails.

## Acceptance Criteria
- Integration test on Samsara application fields confirming field 19 generates an independent capstone project response, field 20 generates clean bullet points, field 21 generates Samsara product/principles response, and race field matches `Asian`.
