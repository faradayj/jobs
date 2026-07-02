# Workday Application Bot

## Layout

```
src/        Python scripts (app_common, app_workday, app_greenhouse, job_tracker)
data/       Candidate data (library.json, resume.pdf, jobs_tracker.csv, jobs_details.json, .env)
artifacts/  Run screenshots + reports (gitignored)
```

## Skills

### /fill-and-heal
Self-healing loop for the Workday bot. Runs the bot on a listing URL, reads the structured
report + page screenshots, diagnoses failures, patches the code, and reruns until the
application reaches Review. Never auto-submits — user reviews and submits.

Full procedure: `.claude/skills/fill-and-heal/SKILL.md`
