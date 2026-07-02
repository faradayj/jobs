# Job Application Bots

Playwright-based bots that auto-fill Workday and Greenhouse ATS applications, driven by a
candidate profile in `data/library.json`. Bots never auto-submit — they pause at the Review
page for manual inspection and submission.

## Layout

| Directory | Contents |
|---|---|
| `src/` | Python scripts: `app_workday.py`, `app_greenhouse.py`, `app_common.py`, `job_tracker.py` |
| `data/` | Candidate data: `library.json`, `resume.pdf`, `jobs_tracker.csv`, `jobs_details.json`, `.env` |
| `artifacts/` | Run screenshots and reports (gitignored) |

## Setup

```bash
pip install -r requirements.txt
```

Populate `data/.env`:
```
WORKDAY_PASSWORD=your_workday_password
DEEPSEEK_API_KEY=your_key   # optional — rule-based fallback works without it
```

Ensure `data/library.json` has `resume_path` pointing to a valid PDF (default: `data/resume.pdf`).

## Typical workflow

Ingest job listings, evaluate fit, then run the apply loop:

```bash
python3 src/job_tracker.py ingest
python3 src/job_tracker.py evaluate --limit 10
python3 src/job_tracker.py list --priority 1
python3 src/job_tracker.py apply-loop
```

## Running a bot directly

**Workday:**
```bash
python3 -u src/app_workday.py "WORKDAY_JOB_URL" --show
```

**Greenhouse:**
```bash
python3 -u src/app_greenhouse.py "GH_JOB_URL" --show
```

Flags:
- `--show` — open a visible Chrome window (recommended for debugging)
- `--sim-ds` — rule-based answers only, no DeepSeek API key required

Output is written to `artifacts/run.txt` and `artifacts/run_report.json`.

## Self-healing loop

Use the `/fill-and-heal` Claude skill to run the bot, diagnose failures from the report and
screenshots, patch the code, and rerun automatically until the application reaches Review.

```
/fill-and-heal WORKDAY_JOB_URL
```

See `.claude/skills/fill-and-heal/SKILL.md` for the full procedure.

## Config

- `data/library.json` — candidate profile, Q&A examples, resume path, routing priorities
- `data/.env` — secrets (`WORKDAY_PASSWORD`, `DEEPSEEK_API_KEY`, `BOT_DEBUG_DOM`)
