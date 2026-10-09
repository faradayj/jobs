# Architecture

The project uses directly executable Python scripts. Run commands from the repository
root; shared modules live beside the scripts in `src/`.

```text
job_tracker.py
  ├── scraper.py       listing parsing, ATS extraction, visible-browser fallback
  ├── evaluator.py     DeepSeek rubric, output validation, evidence, checkpoints
  └── tracker_db.py    SQLite, CSV/details synchronization, applied/filtered history

app_workday.py / app_greenhouse.py / app_ashby.py / app_tiktok.py
  ├── app_common.py    local profile, prompts, model calls, browser launch helpers
  ├── browser_utils.py system Chrome discovery, independent of profile loading
  └── tracker_db.py    confirmed application history and snoozing

app_workday.py
  └── workday_auth.py / workday_scan.py / workday_executors.py / workday_sections.py
```

## State and protection

SQLite stores active jobs and cached descriptions. The CSV is the local human-readable
tracker and is imported when the tracker CLI starts. Export synchronizes the CSV and
details JSON. Applied and filtered manifests preserve history across ingestion;
Applied records take precedence over upstream removal and evaluation changes.

Evaluation caches a usable description before calling DeepSeek. The output validator
checks eligibility, priority, field types, and rejection excerpts against the posting.
Failed extraction or validation remains reviewable without becoming a rejection.
Timestamped reports retain rubric version, input hashes, evidence, and token usage.

## Adapter boundaries

Each application adapter owns its ATS-specific navigation and selectors. Workday
separates authentication, scanning, field execution, and page sections into helpers.
`app_common.py` intentionally loads the local candidate profile when application
adapters import it. Scraping and tracker imports do not depend on that profile.

`run_batch.py` runs supported priority-one jobs sequentially in visible browsers.
`scrape_amazon.py` is a standalone local enrichment utility. Resume compilation is
handled separately by `generate_resumes.py`.

Keep ATS extraction fixes in the scraper and candidate policies in the profile/rubric.
Use mocked HTML and model responses for offline tests; live browser testing always
uses `--show` and requires user review before application submission.
