# Job Application Bots

A local Python toolkit for finding roles, evaluating candidate fit with DeepSeek,
and filling applications in a visible browser. Supports Workday, Greenhouse, Ashby,
and TikTok application flows. Listings come from the SimplifyJobs new-grad tracker
on GitHub; unsupported application sites can still be tracked and evaluated.

## Quick start

Use Python 3.11 or later. From the repository root:

```sh
python -m venv .venv
```

Activate the environment in PowerShell with `.venv\Scripts\Activate.ps1`, or on
macOS/Linux with `source .venv/bin/activate`, then run:

```sh
python -m pip install -r requirements.txt
python -m playwright install chromium
python scripts/init_local.py
```

The initializer creates `data/library.json` and `data/.env` from fictional examples.
It preserves files that already exist. Edit the profile with your own details,
put a resume PDF at the configured `resume_path`, and set `DEEPSEEK_API_KEY` in
`data/.env`. Set `WORKDAY_PASSWORD` if using Workday account automation.
System Chrome is used when available; otherwise the bots use Playwright Chromium.
Linux installations may need `python -m playwright install --with-deps chromium`.

See [configuration](docs/configuration.md) for profile fields and privacy details.

## Find and evaluate roles

```sh
python src/job_tracker.py ingest
python src/job_tracker.py evaluate --calibrate
python src/job_tracker.py evaluate --limit -1
python src/job_tracker.py status
python src/job_tracker.py list --priority 1
python src/job_tracker.py apply-loop
```

Calibration evaluates a diverse sample of 15 pending roles and saves a report in
`artifacts/evaluation_*.json`. Review the report before evaluating the remainder.
Evaluation uses paid DeepSeek API calls; `--dry-run` scrapes without calling the model.
Run `python src/job_tracker.py --help` for individual commands and options.

Eligibility and priority are separate. Eligible roles get priority 1 (high), 2
(medium), or 3 (low); low priority remains in the tracker. Rejections require a
posting excerpt and a conflicting candidate fact. Ambiguity becomes `Needs Review`.
Unusable scrapes become `Fetch Failed / Manual Review`; invalid model output retries
and then becomes `Evaluation Error`. Neither failure is treated as ineligibility.

Descriptions and evaluation evidence are cached. Candidate degree equivalence,
clearance interpretation, and earliest availability can be configured in
`role_evaluation_preferences`; application answers must still report actual facts.

## Fill an application

```sh
python src/app_workday.py "JOB_URL" --show
python src/app_greenhouse.py "JOB_URL" --show
python src/app_ashby.py "JOB_URL" --show
python src/app_tiktok.py "JOB_URL" --show
```

Browsers are visible by default. Use `--show` explicitly for testing; application
bots accept `--headless` when requested. Review every application before submitting.
Greenhouse and Ashby wait for manual submission and confirmation. Workday and TikTok
ask for confirmation before attempting submission. The tracker also supports explicit
manual marking with `python src/job_tracker.py apply --id JOB_ID`.

Applied history takes precedence over upstream listings and evaluation. Imports
preserve Applied statuses and dates, and ingestion keeps applied jobs even when a
listing closes. Back up the entire local `data/` directory to preserve your history.

Reports and screenshots are written under `artifacts/`. Workday writes
`run_report.json`; the other adapters write `run_report_gh.json`,
`run_report_ashby.json`, and `run_report_tiktok.json`. Console output stays in your
terminal unless you redirect it.

## Repository layout

| Path | Purpose |
| --- | --- |
| `src/job_tracker.py` | CLI for ingestion, evaluation, and tracking |
| `src/tracker_db.py` | SQLite, CSV synchronization, and history protection |
| `src/scraper.py`, `src/evaluator.py` | Posting extraction and DeepSeek evaluation |
| `src/app_*.py`, `src/workday_*.py` | Application adapters and shared helpers |
| `examples/` | Fictional configuration templates |
| `scripts/init_local.py` | Non-destructive local setup |
| `tests/` | Offline regression tests |
| `docs/` | Configuration and architecture notes |
| `data/`, `artifacts/`, `scratch/` | Ignored local state and output |

[Architecture](docs/architecture.md) explains module boundaries. See
[CONTRIBUTING.md](CONTRIBUTING.md) for checks and development conventions.
The optional [fill-and-heal skill](.claude/skills/fill-and-heal/SKILL.md)
provides a visible-browser debugging procedure.

## Sharing the repository

Candidate profiles, credentials, resumes, application history, browser sessions,
and private working notes are ignored. Commit fictional fixtures and examples only.
Ignoring or untracking files does **not** remove them from older commits. This
repository previously tracked personal data; review or sanitize its Git history
before publishing it publicly. See [configuration](docs/configuration.md#sharing-and-backups).
