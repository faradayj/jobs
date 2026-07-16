# Jobs Bot — Session Handoff
_Generated 2026-07-09_

---

## Repo layout

```
src/app_common.py       — rule engine (rule_based_answer, all label-matching rules)
src/app_workday.py      — Workday browser automation (main loop, handlers, executors)
src/app_greenhouse.py   — Greenhouse form filler
src/job_tracker.py      — SQLite tracker, CSV I/O, DeepSeek/Claude scoring pipeline
src/run_batch.py        — NEW: headed batch runner for P1 Workday jobs

data/library.json       — candidate profile, rules, Q&A examples, connections
data/resume_2026-06.pdf — active resume
data/jobs_tracker.csv   — source of truth for all job statuses (312 jobs)
data/jobs_details.json  — cached job descriptions
data/.env               — WORKDAY_PASSWORD + DEEPSEEK_API_KEY (never commit)

artifacts/              — run screenshots, run_report.json, eval_pending.json, eval_scores.json
```

---

## Run commands

```bash
# Single job (headless)
python3 src/app_workday.py "WORKDAY_URL"

# Single job (headed — visible browser)
python3 src/app_workday.py "WORKDAY_URL" --show

# Rule-based only (no DeepSeek key needed)
python3 src/app_workday.py "WORKDAY_URL" --sim-ds

# Batch all 21 P1 Workday jobs, headed, one-by-one with pause between
python3 src/run_batch.py
python3 src/run_batch.py --sim-ds          # rule-based
python3 src/run_batch.py --start-id 257    # resume from ID
python3 src/run_batch.py --ids 257 239     # specific IDs only
python3 src/run_batch.py --dry-run         # preview list only

# Job tracker
python3 src/job_tracker.py status
python3 src/job_tracker.py list --priority 1
python3 src/job_tracker.py ingest          # scrape new listings

# Claude scoring pipeline (no DeepSeek needed)
python3 src/job_tracker.py evaluate --export-prompts   # → artifacts/eval_pending.json
# [have Claude score it → write artifacts/eval_scores.json]
python3 src/job_tracker.py evaluate --import-scores    # load scores → CSV
```

---

## Job tracker state (as of 2026-07-09)

| Status | Count |
|--------|-------|
| Eligible (Priority 1) | 130 |
| Eligible (Priority 2) | 39 |
| Ineligible | 54 |
| Ineligible (Non-US/Canada) | 19 |
| Closed (Expired) | 10 |
| Closed (Removed) | 59 |
| Fetch Failed / Manual Review | 1 |
| **Total** | **312** |

55 jobs scored this session (Claude file-handoff, no DeepSeek).

---

## Priority 1 Workday jobs (21 total — ready for batch run)

| ID | Company | Role | Location | Tenant |
|----|---------|------|----------|--------|
| 45 | General Motors | Software Developer – Early Career | Markham, ON | generalmotors.wd5 |
| 48 | TSC | Software Engineer 1 | Silver Spring, MD | tsc.wd12 |
| 67 | Leidos | Junior Software Engineer | Columbia, MD | leidos.wd5 |
| 68 | KBR | Junior Comms Systems SWE | Beavercreek, OH | kbr.wd5 |
| 71 | Robert Half | Software Engineer 1 | San Ramon, CA | roberthalf.wd1 |
| 103 | Capital One | Associate SWE New Grad | Toronto, ON | capitalone.wd12 |
| 115 | Blue Origin | SDE 1 Early Career | Kent, WA | blueorigin.wd5 |
| 136 | UT Austin | Data Engineer 1 | Austin, TX | utaustin.wd1 |
| 155 | U of Arkansas | COSMOS Data Engineer 1 | Little Rock, AR | uasys.wd5 |
| 199 | Cox | Software Engineer 1 | Austin/Burlington/Atlanta | cox.wd1 |
| 206 | Cox | Entry Level SWE | Burlington, VT | cox.wd1 |
| 207 | Cox | Entry Level SWE | Irvine, CA | cox.wd1 |
| 214 | KBR | Junior Comms Systems SWE | Beavercreek, OH | kbr.wd5 |
| 238 | Alation | UX Software Engineer 1 | San Carlos, CA | alation.wd503 |
| 239 | Salesforce | AMTS College Grad | Palo Alto/SF/Seattle | salesforce.wd12 |
| 240 | Salesforce | AMTS/MTS College Grad | Palo Alto/SF/Bellevue | salesforce.wd12 |
| 241 | General Dynamics IT | Junior Software Developer | Annapolis Junction, MD | gdit.wd5 |
| 254 | GlobalFoundries | AI/ML Systems Engineer | Richardson, TX | globalfoundries.wd1 |
| 255 | RTX | Software Engineer 1 | Fort Wayne, IN | globalhr.wd5 |
| 257 | Walt Disney | Product Software Engineer 1 | Glendale, CA | disney.wd5 |
| 308 | Manulife | Data Analyst New Grad | Montreal, QC | manulife.wd3 |

**Note:** 241 (GDIT) requires active TS/SCI — was scored P1 before this was caught; skip it.
**Note:** 255 (RTX) and 308 (Manulife) closing imminently — apply first.

**Tenants with stored credentials:** `roberthalf`, `cox`, `default` (in `workday_accounts` in library.json).
All others → bot pauses, shows email/password, waits up to 3 min for manual account creation.

---

## BCBSAZ applications (two live, reached Review)

| ID | Role | URL |
|----|------|-----|
| R6054 | Analyst, Analytics and Data Science II/III (Remote) | `https://bcbsaz.wd1.myworkdayjobs.com/BCBSAZCareers/job/AZ-Blue-Phoenix-AZ-85021/Analyst--Analytics-and-Data-Science-II-III--Remote-_R6054` |
| R6112 | Data Engineer, Business Intelligence (Hybrid) | `https://bcbsaz.wd1.myworkdayjobs.com/BCBSAZCareers/job/AZ-Blue-Phoenix-AZ-85021/Data-Engineer--Business-Intelligence----Hybrid_R6112` |

Both reached Review cleanly last run. **User must manually submit.**

**BCBSAZ special rules (in library.json + app_common.py):**
- "How Did You Hear About Us?" → picks from `referral_source_terms` (e.g. "I know someone at the company")
- Follow-up "name or email" field → "Brandon Tran" (from `personal_connections.bcbsaz.referral_contact`)
- Both fields are in `_force_override_labels` so pre-filled wrong answers get overwritten

---

## Key fixes shipped this session

### 1. Hear-about-us + referral follow-up (per-company)
- `library.json` → `personal_connections.bcbsaz.referral_source_terms` + `referral_contact`
- `app_common.py` isSelectInput branch (~386) + options branch (~640): checks `_current_connection()` first
- Email rule (~460): exclusion guard prevents "email" matching "name or email" label
- Referral follow-up rule (~493): fires before generic name rule; returns `referral_contact`
- `_force_override_labels` includes `"name or email"`, `"their name or email"`

### 2. Degree picking GED instead of MS/BS
- BCBSAZ dropdown uses `"M.S."` / `"B.S."` with periods
- Fix: prepended `"M.S."` and `"B.S."` to `degree_search_variants` in library.json for both edu entries

### 3. Desired compensation scaling
- `library.json`: `baseline_target_pay = "120000"`, `desired_comp_scale_factor = 0.95`
- Both free-text (~471) and dropdown (~769) comp branches now read `job_listing_salary` from `PROFILE_SUMMARY` and multiply by factor
- Falls back to `baseline_target_pay` when no listing salary

### 4. Finder spam on headed mode (file-upload buttons)
- 4-layer defense: skip in `prefetch_options()`, skip in `execute_answer()`, added `"upload a file"` to `stop_kws` in `SECTION_SPEC` work+edu, registered global `filechooser` handler in `main()`

### 5. Self Identify CC-305 date not committing
- `handle_self_identify()`: uses `InputEvent` (not plain `Event`), dispatches `blur`, then `Tab` keypress
- React spinbutton requires all three to commit state to server

### 6. Claude scoring pipeline (no DeepSeek)
- `job_tracker.py`: `--export-prompts` → `artifacts/eval_pending.json` (rubric + profile + job descriptions)
- Have Claude read file, write `artifacts/eval_scores.json`
- `--import-scores` → loads scores into DB → exports CSV
- `SCORING_RUBRIC` constant shared between DeepSeek path and export file

### 7. Batch runner (`src/run_batch.py`)
- Reads CSV, filters P1 + Workday, runs bot headed one-by-one
- Prompts between jobs: Enter/s/q
- Flags: `--sim-ds`, `--start-id`, `--ids`, `--dry-run`

---

## Candidate profile summary

**Joshua Li** — MS CS @ ASU (current, 4.0 GPA, Dec 2026), BS Data Science @ UCSD (3.91, Mar 2025)

**Work experience:**
- BILL — Associate Fraud Risk Strategy Data Scientist (Dec 2025–present, San Jose CA)
  - NSGA-II genetic algorithm, Random Forest KYC, SageMaker batch ML pipeline, NetworkX identity graph, LiteLLM agentic loop, Tableau dashboards
- Single Particle LLC — Software Intern (Jul–Oct 2023, San Diego CA) — fine-tuned ChatGPT for cryoEM
- Intel — Software Intern (Jun–Sep 2022, Folsom CA) — data science benchmarking, Scikit-learn

**Skills:** Python, SQL, R, C, C++, Java, JS/TS, HTML, Pandas, PyTorch, TensorFlow, Keras, Scikit-learn, NetworkX, Spark, Hadoop, Databricks, Optuna, AWS, GCP, Docker, Tableau, React, Flask, Django, MySQL, PostgreSQL, MongoDB, Machine Learning, Data Science

**Target roles:** Software Engineer, Data Scientist, Machine Learning Engineer

**Location:** Open to anywhere US/Canada; relocation yes; remote/hybrid/onsite all accepted

**Citizenship:** US + Canadian dual citizen; US Person for ITAR; no sponsorship needed; no active security clearance

**Personal connection:** Brandon Tran works at BCBSAZ (referral for both listings)

---

## Architecture quick-reference

```
main()
  ├── ensure_signed_in()          — login wall detection + auto sign-in + 3-min manual wait
  ├── handle_my_experience()      — WE/EDU/LANG add-dialogs
  │     └── fill_add_dialog()     — entry_answer() + executors
  ├── handle_voluntary_disclosures()
  ├── handle_self_identify()      — CC-305 disability + date spinbutton
  └── smart_fill_page()           — generic pages
        ├── deepseek_fill_page()  — LLM batch (requires DEEPSEEK_API_KEY)
        ├── rule_based_fill_page() — label-match fallback (--sim-ds or no key)
        └── execute_answer()      → exec_text / exec_button_dropdown / exec_selectinput / exec_radio / exec_checkbox

rule_based_answer(label, context_hint, exclude)
  — defined in app_common.py
  — reads PROFILE_SUMMARY (JSON blob with job_listing_salary, job_listing_locations, job_tenant, today)
  — reads JBM (job_board_mappings), COMP (compensation_rules), PI (personal_info), WE/EDU from library.json
  — _current_connection() → looks up job_tenant in personal_connections for referral logic
  — fuzzy_pick(opts, value) → exact → prefix → contains → normalized matching
```

---

## Known issues / next steps

- **241 GDIT**: requires active TS/SCI poly — should be marked Ineligible in tracker manually or re-scored
- **Disney 257**: closes July 11 — apply immediately (already at Review, just need to submit manually — check artifacts/review_page.png)
- **Manulife 308**: closes July 11 — same urgency
- **DAT Freight 265, 297**: no job description scraped (nav-only content) — may need manual apply or URL update
- **Batch runner**: no resume capability mid-run; if killed, use `--start-id` to pick up where left off
- **DeepSeek scoring**: still wired for future use when API key available (`evaluate` with no flags); Claude handoff (`--export-prompts` / `--import-scores`) works standalone
- **New jobs**: run `python3 src/job_tracker.py ingest` periodically then re-export for Claude scoring
