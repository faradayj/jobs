# Local configuration

Run `python scripts/init_local.py` from the repository root. It copies the checked-in
fictional examples into `data/` without overwriting existing configuration.
The application bots require a profile; tracker help and offline tests do not.

## Candidate profile

`data/library.json` is the profile read by both the evaluator and application bots.
`examples/library.example.json` shows the supported structure. Replace all fictional
values before running an application. JSON does not allow comments.

| Field | Purpose |
| --- | --- |
| `personal_info` | Legal name, contact information, address, and public links |
| `education_history` | Schools, degrees, majors, dates, GPA, and enrollment status |
| `work_experience`, `projects`, `personal_projects` | Actual experience and achievements |
| `skills`, `languages` | Skills inventory and language proficiency |
| `resume_path`, `transcript_path` | Paths relative to the repository root, or absolute paths |
| `role_preferences` | Target roles, locations, and earliest start date |
| `role_evaluation_preferences` | Candidate-specific interpretation of eligibility requirements |
| `job_board_mappings` | Factual work authorization, sponsorship, and application answers |
| `regulatory_self_identification` | Your disclosure preferences and factual fallback answers |
| `compensation_rules` | Compensation preferences |
| `prepared_answers`, `workday_qa_examples` | Reusable truthful application answers |
| `routing_priorities`, `workday_accounts` | Optional location rules and tenant-specific account emails |

An empty section means it is not configured. Fill required application facts and
self-identification preferences explicitly; some existing adapters have fallback
answers. Do not rely on fictional examples or defaults for your actual application.
Optional `data/resume_profile.json`, `data/skills_inventory.json`, and tailored
LaTeX/PDF resumes are local assets, not prerequisites for the tracker.
`src/generate_resumes.py` compiles local `data/resumes/tex/` files and requires
`pdflatex` separately. It does not generate resume content.

## Environment

| Variable | Purpose |
| --- | --- |
| `DEEPSEEK_API_KEY` | Role evaluation and model-assisted form filling |
| `WORKDAY_PASSWORD` | Workday account authentication |
| `BOT_DEBUG_DOM` | Optional DOM diagnostics; set to `1` to enable |

These are loaded from `data/.env`; existing process environment variables take
precedence. Keep passwords in the environment instead of your candidate JSON.
Legacy profile password fields remain supported for compatibility.

## Sharing and backups

The entire `data/` directory is ignored, including the CSV tracker, SQLite database,
applied/filtered manifests, profile, credentials, documents, and browser storage.
`artifacts/`, `scratch/`, `conductor/`, `HANDOFF.md`, and editor settings also stay local.
Back up local data privately; Git no longer provides backups for these files.

Before sharing, inspect `git ls-files` and the changes you plan to commit. Logs,
screenshots, exported evaluation prompts, and reports can contain candidate data.
Only fictional examples and sanitized fixtures belong in the shared repository.

Previously committed files remain in Git history after they are untracked. Public
publication requires reviewing that history and removing personal data where needed,
or creating a fresh repository from the sanitized working tree. History rewriting
is a separate operation and is not performed by the local initializer.
