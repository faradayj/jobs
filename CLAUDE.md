# Working on Job Application Bots

Read [CONTRIBUTING.md](CONTRIBUTING.md) and [docs/architecture.md](docs/architecture.md).

- Preserve local `data/` and Applied history; never replace candidate configuration with examples.
- Keep private data, credentials, documents, reports, and session notes out of commits.
- Use `--show` for live browser testing. Do not submit applications without explicit user authorization.
- Keep candidate facts in the local profile; keep ATS mechanics in the adapters/scraper.
- Run offline regression tests before and after behavior changes.

The optional Workday debugging procedure is
[fill-and-heal](.claude/skills/fill-and-heal/SKILL.md).
