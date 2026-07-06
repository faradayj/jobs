# fill-and-heal

Self-healing loop for the Workday application bot. Run the bot on a listing URL, read the
structured report + page screenshots, diagnose failures, patch the code, and rerun until
the bot reaches Review with no errors. Stop before submitting — user reviews and submits.

---

## Trigger

User says something like:
- "run the bot on [URL]"
- "test the bot on [URL] and fix any issues"
- "fill-and-heal [URL]"
- "/fill-and-heal [URL]"

---

## Prerequisites

1. `data/.env` must have `WORKDAY_PASSWORD=<your password>` set.
2. `data/library.json` must have `resume_path` pointing to a valid PDF (default: `data/resume.pdf`).
3. Bot can run headless or headed (`--show`). Use `--show` for debugging so the browser is visible.
4. `--sim-ds` flag runs the rule-based path without a DeepSeek API key (good for first-pass testing).

---

## The Loop

### Step 1 — Run the bot

```bash
python3 -u src/app_workday.py "JOB_URL" > artifacts/run.txt 2>&1
```

- Runs **headless** by default (no dock-icon spam on macOS).
- Output goes to `run.txt`; also tails in the terminal when run interactively.
- Bot auto-saves `artifacts/run_report.json` on finish (even if it crashed).
- Add `--show` only for final visual inspection or when a screenshot alone isn't enough to diagnose.

For rule-based-only testing (no DeepSeek key needed):
```bash
python3 -u src/app_workday.py "JOB_URL" --sim-ds > artifacts/run.txt 2>&1
```

### Step 2 — Read the report

Read `artifacts/run_report.json`. Find pages where `status` is `"stuck"` or `"error"`.

Key fields per page entry:
- `name` — page heading (e.g. "My Experience", "Application Questions 2 of 2")
- `status` — `"advanced"` | `"stuck"` | `"error"` | `"review"` | `"complete"`
- `errors` — validation error strings from Workday's DOM
- `required_invalid` — field labels that Workday marked required but invalid
- `screenshot` — filename in `artifacts/` to Read

### Step 3 — Read the screenshot(s)

For each stuck page, Read its screenshot from `artifacts/<filename>`.

**JSON alone is not trusted.** The screenshot reveals what the bot *sees* vs what was intended:
- A field that *looks* filled in the screenshot but has a Workday error = value didn't commit
  to the server (AJAX race, wrong field selector, or Tab/blur didn't fire).
- A required dropdown still on "Select One" = the rule didn't fire or the label didn't match.
- An "Errors Found" banner listing field names = cross-reference with `required_invalid`.

Also read `artifacts/run.txt` (console log) for the `[SCAN]`, `[LLM]`, `[DIALOG]`,
and `[NAV]` lines around the stuck page for execution detail.

### Step 4 — Diagnose and patch

Map the failure to the right function in `src/app_workday.py`:

| Page type | Handler | Key functions |
|---|---|---|
| My Information, Application Questions (generic) | `smart_fill_page` | `rule_based_answer`, `deepseek_fill_page`, `execute_answer` |
| My Experience (add-dialogs) | `handle_my_experience` | `fill_add_dialog`, `exec_selectinput`, `entry_answer` |
| Voluntary Disclosures | `handle_voluntary_disclosures` | T&C checkbox logic |
| Self Identify | `handle_self_identify` | disability radio, date fields |
| Any page — dropdown | `exec_button_dropdown` | `fuzzy_pick`, `li[role=option]` click |
| Any page — selectinput/combobox | `exec_selectinput` | type→Enter→pick from results |
| Any page — radio | `exec_radio` | name/value strategy, aria-label fallback |

Common failure patterns and fixes:

**School/University not persisting (My Experience)**
- `exec_selectinput` typed the search term and got results but the Tab/blur AJAX save raced.
- Fix: increase post-click wait (`await page.wait_for_timeout(1200)` → `2000`) in `exec_selectinput`
  at the `await page.keyboard.press("Tab")` line, or re-check the search term
  in `entry_answer` for "school" labels returns the right institution name.

**Required field stuck on "Select One" (Application Questions)**
- The rule didn't fire: the label in `rule_based_answer` didn't match.
- Fix: add the label substring to the relevant `label_match(label, ...)` call, or add a new
  case in the `button`/`select` block.
- The postgraduate-enroll dropdown: verify `currently_in_grad` is True for ASU.

**Field filled but not committed (any page)**
- React state didn't sync. After `exec_text`, the `input`/`change` events may not have fired.
- Fix: in `exec_text`, ensure the React synthetic event dispatch fires.
- For selectinput: after clicking an option, increase the Tab wait.

**Validation error on a field that LOOKS correct**
- Workday sometimes re-validates with server-side data that differs from client state.
- Fix: scroll the page back to the top after `smart_fill_page` and re-scan (`save_and_continue`
  already does a re-fill retry — check if the field shows up in `required_invalid`).

**Checkbox answer rules (library.json)**
- Change answers in `workday_qa_examples` or `job_board_mappings` — they now reach the LLM.
- Rule-based answers live in `rule_based_answer()` for fallback.

### Step 5 — Rerun and iterate

Rerun Step 1 with the same URL. The bot will restart from the beginning (it re-logs in).
Repeat Steps 2–5 until `run_report.json` shows `"final": "review"` with no stuck pages.

### Step 6 — Hand off to user

Surface the review screenshot: Read `artifacts/review_page.png`.
Tell the user the application is ready at the Review page and they should open the browser
(or rerun with `--show`) to inspect and submit.

---

## Architecture quick-reference

```
main()                          — page loop, calls handlers per page type
  ├── handle_my_experience()    — WE/EDU/LANG add-dialogs + skills
  │     └── fill_add_dialog()   — scans new dialog fields, calls entry_answer + executors
  ├── handle_voluntary_disclosures()
  ├── handle_self_identify()
  └── smart_fill_page()         — generic pages: scan → LLM/rules → execute
        ├── deepseek_fill_page()  — LLM batch fill (requires DEEPSEEK_API_KEY)
        ├── rule_based_fill_page() — label-matching fallback
        └── execute_answer()    — dispatches to exec_text/exec_button_dropdown/exec_selectinput/exec_radio/exec_checkbox

PROFILE_SUMMARY                 — JSON blob sent to DeepSeek; includes routing_priorities,
                                   workday_qa_examples, today (all now wired)
RUN_REPORT                      — populated per-page; written to artifacts/run_report.json
```

---

## Notes

- Never submit: the loop always stops at Review. The user manually submits.
- `--sim-ds` overrides `deepseek_fill_page` with rule-based answers — useful for testing
  the rule path without an API key or to isolate rule vs LLM failures.
- Screenshots are named `run_<N>_<HeadingSlug>.png`; stuck pages get `stuck_<Heading>.png`.
- `BOT_DEBUG_DOM=1` (env flag — add to `data/.env`) enables DOM dump for selectinput/dropdown
  fields — useful for diagnosing pill-commit failures in `--show` runs.
