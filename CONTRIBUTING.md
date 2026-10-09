# Contributing

Follow the setup in [README.md](README.md). Python 3.11+ is supported; dependencies
are pinned in `requirements.txt`. No formatter or additional test framework is required.

Run the offline checks from the repository root:

```sh
python -m unittest discover -s tests -v
python -m compileall -q src scripts tests
python src/job_tracker.py --help
python src/run_batch.py --help
python scripts/init_local.py --help
git diff --check
```

Tests use temporary databases and mocked network responses. They must work without
candidate data, API keys, installed browsers, or live application submissions.
GitHub Actions runs the suite on Windows and Linux with Python 3.11 and 3.14.

Preserve Applied records, dates, and history when changing ingestion or evaluation.
Keep eligibility separate from priority and require posting evidence for rejection.
Add regression coverage for behavior changes; use fictional profiles and HTML fixtures.
Avoid company-specific candidate answers or personal information in source code.

Use `--show` for authorized live testing. Review structured reports and screenshots
before changing selectors. Stop for user review before submitting an application.
Describe the concrete behavior change, validation performed, and relevant limitations
in pull requests. Include sanitized errors, never candidate data or credentials.

See [configuration](docs/configuration.md#sharing-and-backups) before publishing.
