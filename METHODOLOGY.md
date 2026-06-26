# Workday Automation Bot — Development Methodology

How this bot was built, debugged, and iterated to full completion across 42 test runs.

---

## Overview

The goal was a headless Playwright bot that fills a full Workday application (6 pages) using
candidate data from `library.json`, with DeepSeek LLM as a future backend when the API is
accessible. The core challenge: Workday is a highly dynamic SPA with virtual DOM, custom widget
types, no stable element IDs on many fields, and server-side search dropdowns that respond
differently to timing.

---

## Phase 1 — Manual Recon First, Always

**Rule: Before writing any automation code, manually walk through the target form and document
every field type.**

For each page:
1. Open the form in a real browser
2. Note every field: label, input type, whether it's a native `<input>` or Workday custom widget
3. Identify the "weird" ones (search-based dropdowns, virtual scroll lists, pill inputs, spinbuttons)
4. Test edge cases manually — what happens if you type in a selectinput? Does pressing Enter
   trigger a server search? Does selecting a pill lock the underlying input?

This produced the initial field taxonomy:
- **Standard inputs** — `<input type="text">`, `<textarea>` — trivial to fill
- **Button dropdowns** — Workday's `<button>` that opens a `<li role="option">` list — needs
  click → wait → select
- **Selectinputs** — `<input data-uxi-widget-type="selectinput">` — needs type → Enter → wait
  for server results → pick from filtered list
- **Date spinbuttons** — `<input role="spinbutton">` for Month/Year/Day — native value setter
  via JS, not keyboard input
- **Checkboxes** — often CSS-hidden, need parent visibility check not element visibility

---

## Phase 2 — SCAN_JS: The Universal Field Scanner

**Rule: Build one canonical scanner that runs in the browser's JavaScript context and returns
a normalized field list. Never hard-code element selectors by page.**

SCAN_JS is a `page.evaluate()` call that:
1. Queries all interactive elements via a broad CSS selector
2. Filters to visible, non-hidden, non-nav elements
3. Extracts a normalized label via a 5-strategy fallback chain:
   - `aria-labelledby` → `aria-label` → `label[for=id]` → `fieldset` first line →
     `formField` container's `formLabel`/`label`
4. Detects Workday-specific widget types (`isSelectInput`, `role="combobox"`)
5. Tags each field with `data-fill-idx` for stable re-location between scans
6. Returns `{index, tag, type, id, auto, label, options, value, isSelectInput}`

**Key lesson — `button[id]` vs `button`:** The initial SCAN_JS selector used `button[id]`
which only matched buttons WITH an `id`. Many Workday dropdowns (notably the Language NAME
button) have no `id`. The fix was changing to `button` and tightening the JS filter instead:
allow buttons that have `id`, `data-automation-id`, OR are inside a `formField` wrapper with a
label. This is a common trap — CSS attribute selectors are exclusive, not permissive.

---

## Phase 3 — Run → Read Output → Hypothesize → Fix

Every run wrote to `/tmp/rh_outNN.txt`. The debug loop was:

```
Launch run (background process) →
Tail output live while doing other investigation →
grep for specific signals (field fills, navigation, errors) →
Form hypothesis about what's wrong →
Make surgical code edit →
Syntax check →
Re-run
```

**42 runs total.** Each run advanced understanding. Runs were never wasted — even a run that
crashed immediately revealed a new code path.

### Hypothesis → Test → Fix examples:

| Observation | Hypothesis | Fix |
|---|---|---|
| "Language Select One Required" validation error | Language NAME field not filled | Added diagnostic logging before each Add click |
| `~ skill 'Python' — no results`, then `✓ skill 'SQL' → 'Python'` | Stale dropdown results from previous search arriving late | Switched from fixed 1200ms wait to polling loop (4 × 800ms) |
| Field of Study returns 24 A's (unfiltered) | Server search didn't engage; Workday shows full unfiltered list | Detect unfiltered: if `results[0]` contains no word from search term → try fallback term |
| Language dialog shows only Reading/Speaking/Writing, not Language NAME | Language NAME is inserted at a DOM position BELOW `count_before` index | Switch from index-slice approach to field-identity diff (before vs after) |
| `button[id]` filter in querySelectorAll | Language NAME button has no `id` → never reached by scan | Change to `button`, add formField-wrapper check in JS filter |

---

## Phase 4 — DOM Archaeology for Stubborn Bugs

When a field kept disappearing from scans despite code fixes, the approach was:

1. **Add targeted `print()` logging inline** — e.g., `[LANG-PRE]` output dumping all
   language-related fields captured just before clicking Add
2. **Read the field index numbers** — Workday's virtual DOM reindexes when sections expand.
   Watching which indices appear/disappear between scans revealed DOM insertion order
3. **Write a standalone diagnostic script** — `diag2.py` that navigated to the exact page,
   clicked Add Languages, and printed before/after field lists with full attributes
4. **Cross-reference multiple run outputs** — run 40 vs run 41 vs the LANG-PRE dump showed
   that after clicking Add Languages for English, 6 new fields appeared but 3 of them
   (Language NAME, I am fluent, Overall) were inserted at positions 33-35 in the DOM, while
   `count_before` was 36 (because Skills had been at position 33 before the click and was now
   pushed down to 39)

**The DOM insertion order insight:** Workday inserts the new language form fields *above* the
Skills selectinput in the DOM (because the Language section comes before Skills in the page
layout). So after clicking Add, the new language fields appear at lower indices than
`count_before`. The fix — proper field-identity diff using `(id|auto|label+tag)` sets — is
now the gold standard for any section where fields are inserted mid-page.

---

## Phase 5 — Handling Workday's "Weird Dropdowns"

Three distinct dropdown patterns required three distinct strategies:

### 5a — Button dropdowns (Month, Degree, Veteran status)
```
click button → wait for <li role="option"> to appear → fuzzy_pick best match → click <li>
```
Options are in a normal visible list. `fuzzy_pick` uses `difflib.SequenceMatcher`.

### 5b — Selectinput (Field of Study, Language NAME, Skills)
```
click input → type search term → press Enter → POLL for visible results (height > 0) →
pick best match by score function → click option element
```
Critical details:
- Must press **Enter** to trigger server-side search (typing alone shows unfiltered local list)
- Must filter by `getBoundingClientRect().height > 0` — Workday keeps non-matching items in
  DOM with `height = 0`; without this filter you get all 24 alphabetical results regardless
- Auto-fill detection: if `results = []` after search, Workday auto-selected a single match
  and closed the dropdown; read the pill element instead
- Unfiltered detection: if `results[0]` doesn't contain any word from search term, the search
  didn't engage; try next fallback term

### 5c — Skills (multi-select pills)
Same as selectinput but with additional complexity:
- Already-selected pills must be checked before clicking to avoid accidental deselection
- Score function: exact(0) > starts-with-space(1) > whole-word-boundary(2) > starts-with(3)
  > no-match(99) — reject score-99 matches entirely (wrong match is worse than no match)
- Search terms in `library.json` matter: `"Python Programming"` finds
  `"Python (Programming Language)"` while bare `"Python"` returned no results due to timing

---

## Phase 6 — The `count_before` / Field Diff Pattern

This pattern is used anywhere the bot adds a repeating entry (Work Experience, Education,
Languages). It ensures only the newly added fields are filled, not fields from previous entries.

**Original (fragile) approach:**
```python
count_before = len(await page.evaluate(SCAN_JS))
await add_button.click()
# In fill_add_dialog:
new_fields = all_fields[count_before:]  # wrong when new fields inserted above old ones
```

**Fixed (robust) approach for lang sections:**
```python
fields_before = await page.evaluate(SCAN_JS)
before_ids = {field_identity(f) for f in fields_before}
await add_button.click()
all_fields_after = await page.evaluate(SCAN_JS)
new_fields = [f for f in all_fields_after if field_identity(f) not in before_ids]
# Stop at section boundaries (Skills, LinkedIn, etc.)
```

Where `field_identity(f)` is `"id:X"` or `"auto:X"` or `"lt:label|tag"` — stable across scans.

The `count_before` index-slice still works for Work Experience and Education because those
sections insert new entries at the END of the form (below all existing entries). It fails only
for Languages because Workday inserts the language form ABOVE the Skills section.

---

## Phase 7 — Timing Is Everything

Workday is a React SPA with server-side search. Many bugs were pure timing issues:

| Problem | Cause | Fix |
|---|---|---|
| Skill search returns previous skill's results | 1200ms not enough; stale XHR response arrives late | Poll up to 3s: `for wait in [800, 800, 800, 600]: check if results; if found break` |
| Date spinbutton scroll spam | `scroll_into_view_if_needed()` triggered repeatedly on dialog fields | Use JS `nativeInputValueSetter` directly; no scroll needed |
| Field of Study returns unfiltered A-Z list | Search didn't engage; checked too early | Fixed wait + unfiltered detection |
| Language dialog scan returns 0 new fields | SCAN_JS ran before DOM settled | Increased dialog open wait from 1000ms to 1500ms |

**General rule:** Always prefer polling-with-timeout over fixed sleeps for server-response-dependent actions.

---

## Phase 8 — Rule-Based Answer Engine

The `entry_answer()` function maps field labels to library values without LLM:

```
label → category (work/edu/lang) → specific field mapping → fuzzy_pick(options, value)
```

Key ordering rules learned the hard way:
- **Description check must precede title/role check** — "Role Description" contains "role",
  so title matching would wrongly win if checked first
- **Checkbox processing before dropdowns** — "I currently work here" must be set first
  because checking it removes the end-date fields from DOM
- **`date_seq` annotation** — Workday uses generic "Month"/"Year" labels twice (start + end).
  Annotate first occurrence as "start", second as "end" during field scanning

---

## Phase 9 — Library JSON as Ground Truth

`library.json` is the single source of truth for candidate data. Design decisions:

- **Multi-term fallback for Field of Study:** `"major_search_term": "Computer Science"` +
  `"major_variants": ["Computer Science Engineering", "CSE"]` — if first term returns
  unfiltered results, try variants
- **Skills as search terms, not display names:** `"Apache Spark"` not `"Spark"`;
  `"Python Programming"` not `"Python"`. The search term must match Workday's skills database
- **Proficiency must fuzzy-match dropdown options:** `"Fluent"` → fuzzy-picks
  `"Advanced / Fluent"` via `difflib`
- **Degree variants for fuzzy matching:** `degree_abbreviation: "MS"` + `degree_search_variants`
  to handle different Workday tenants having different degree option labels

---

## What Made This Work vs. Alternatives

### Why not CSS selectors hardcoded per field?
Workday field IDs are UUIDs that change per application and per tenant. Any hardcoded selector
becomes stale immediately.

### Why a generic SCAN_JS instead of `page.locator()`?
`page.locator()` requires knowing the selector in advance. SCAN_JS builds the complete field
map in one JS evaluation, which is then available as a Python list for scoring, sorting, and
dispatching. One round-trip to the browser, full picture of the page state.

### Why field-identity diff instead of counting?
DOM insertion order in Workday is not always append-to-end. Sections insert inline in their
visual position. Any approach that assumes new fields are always at the tail will break.

### Why polling instead of `waitForSelector`?
Workday's search results appear in existing DOM elements that go from `height:0` to
`height:>0`. `waitForSelector` would always return immediately (element exists); you need to
check the rendered height instead, which requires JS polling.

### Why reject score-99 skill matches?
A wrong skill pill is worse than no skill pill. It can't be easily undone (clicking deselects),
and it actively misleads the reader. Fail fast and skip rather than guess badly.

---

## Running the Bot

```bash
# Headless (default)
python3 -u agent_test/app_workday_v3.py "WORKDAY_JOB_URL"

# Visible window (for manual intervention / debugging)
python3 -u agent_test/app_workday_v3.py "WORKDAY_JOB_URL" --show

# With live output log
python3 -u agent_test/app_workday_v3.py "WORKDAY_JOB_URL" > /tmp/run.txt 2>&1 &
tail -f /tmp/run.txt
```

The bot stops at the Review page and prints the screenshot path. It does NOT auto-submit.

---

## Files

| File | Purpose |
|---|---|
| `agent_test/app_workday_v3.py` | Main bot — all automation logic |
| `agent_test/library.json` | Candidate profile, education, work history, skills, languages |
| `agent_test/jobs_details.json` | Job URLs to apply for |
| `agent_test/job_tracker.py` | Scrapes and filters job listings |
