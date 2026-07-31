"""
app_ashby.py  —  Ashby Application Bot
==================================================
Fills Ashby job applications (jobs.ashbyhq.com/{company}/{job-id}/application).

Architecture:
  1. Navigate directly to the apply URL (Ashby doesn't redirect to a branded page or gate
     behind a Quick-Apply login wall the way some Greenhouse boards do).
  2. Scan ALL visible fields. Ashby forms are uniform: every question lives in a
     `.ashby-application-form-field-entry` (div or fieldset) with a
     `.ashby-application-form-question-title` label — text/email/tel/url/number inputs,
     textareas, single yes/no checkboxes, radio groups, file upload, and (defensively
     handled, not yet observed in practice) native <select> / custom comboboxes.
  3. Primary:  send batch to DeepSeek → [{index, value}]   (requires DEEPSEEK_API_KEY)
     Fallback: label-matching rules from app_common.py     (no API needed)
  4. Execute answers field-by-field — pauses at the Submit button for user review.
  5. Click Submit, then verify ACTUAL success (confirmation text / URL change) before
     declaring victory — Ashby forms commonly run invisible reCAPTCHA v3, which can
     silently reject a bot-driven click while leaving the form visually unchanged.
  6. NEVER auto-submits in headless mode — pauses for [Enter] in headed mode; if the
     click doesn't yield confirmed success, pauses again for manual completion.
  7. On confirmed success, marks the job Applied in data/jobs_tracker.csv (same as
     app_workday.py / app_greenhouse.py).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 USAGE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  # Headless (default):
  python3 src/app_ashby.py "JOB_URL"

  # Visible Chrome window for review/submit:
  python3 src/app_ashby.py "JOB_URL" --show

  # Log to file (Mac/Linux):
  python3 -u src/app_ashby.py "JOB_URL" > run_ashby.txt 2>&1 &
  tail -f run_ashby.txt

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 FLAGS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  JOB_URL   Ashby application URL (jobs.ashbyhq.com/{company}/{uuid}/application)
  --show    Launch a visible Chrome window (required to submit)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 CONFIG
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  data/.env            DEEPSEEK_API_KEY=sk-...   (optional)
  data/library.json    Candidate profile, resume path, preferences
"""

import asyncio
import datetime
import json
import re
from pathlib import Path

from playwright.async_api import async_playwright, Page

# Shared infrastructure
from app_common import (
    RESUME_PATH, DEEPSEEK_KEY,
    PROFILE_SUMMARY,
    deepseek_fill_page, rule_based_fill_fields, fuzzy_pick, label_match,
    ARTIFACTS_DIR,
    launch_browser,
)

ARTIFACTS = ARTIFACTS_DIR
ARTIFACTS.mkdir(exist_ok=True)

# Ashby applies the semantic ".ashby-application-form-field-entry" class to most question
# wrappers, but NOT to <fieldset> radio-group wrappers in some sections (confirmed:
# a "Work Authorization Questions" custom section renders
# `<fieldset class="_container_1258i_28 _fieldEntry_1e3gg_28">` with the CSS-module class
# but no semantic one). The `_fieldEntry_` CSS-module class prefix is present in BOTH
# cases, so match on either to avoid silently missing whole sections of radio questions.
FIELD_ENTRY_SEL = '.ashby-application-form-field-entry, [class*="_fieldEntry_"]'
QUESTION_TITLE_SEL = ".ashby-application-form-question-title"


def is_ashby_url(url: str) -> bool:
    return "ashbyhq.com" in url.lower()


# ── Field scanner ─────────────────────────────────────────────────────────────

async def scan_fields(page: Page) -> list[dict]:
    """Scan the Ashby apply form and return structured field descriptors.

    Ashby forms are uniform: each question is a `.ashby-application-form-field-entry`
    (div or fieldset) with a `.ashby-application-form-question-title` label — a stable,
    semantic class Ashby keeps even though the surrounding CSS module classes are hashed.
    We tag each element with a data-ab-idx attribute for stable addressing.
    """
    fields = await page.evaluate(r"""([FIELD_ENTRY_SEL, QUESTION_TITLE_SEL]) => {
        let idx = 0;
        const fields = [];

        function isVisible(el) {
            const rect = el.getBoundingClientRect();
            return rect.width > 0 && rect.height > 0 &&
                   getComputedStyle(el).display !== 'none' &&
                   getComputedStyle(el).visibility !== 'hidden';
        }

        function getLabel(entry) {
            const lbl = entry.querySelector(QUESTION_TITLE_SEL);
            if (lbl && lbl.innerText.trim()) return lbl.innerText.trim().replace(/\*$/, '').trim();
            return '';
        }

        function isRequired(entry) {
            const lbl = entry.querySelector(QUESTION_TITLE_SEL);
            return !!(lbl && /_required_/.test(lbl.className));
        }

        const entries = Array.from(document.querySelectorAll(FIELD_ENTRY_SEL));
        for (const entry of entries) {
            const label = getLabel(entry);
            const required = isRequired(entry);

            // ── Radio group (one entry per group of input[type=radio]) ──────
            const radios = Array.from(entry.querySelectorAll('input[type="radio"]')).filter(isVisible);
            if (radios.length) {
                const texts = radios.map(r => {
                    const lbl = r.closest('label') || document.querySelector('label[for="' + r.id + '"]');
                    return lbl ? lbl.innerText.trim() : (r.value || '');
                });
                const values = radios.map(r => r.value || '');
                radios[0].dataset.abIdx = idx;
                fields.push({
                    index: idx++, tag: 'input', type: 'radio', role: 'radio',
                    id: radios[0].id || '', name: radios[0].name || '',
                    label, required, value: '', options: texts, radioValues: values,
                });
                continue;
            }

            // ── Yes/No button-pair widget ────────────────────────────────────
            // Ashby renders "confirm this / are you X?" questions as two visible
            // <button>Yes</button><button>No</button> elements with an underlying
            // input[type=checkbox] that's genuinely hidden (display:none) and only
            // holds form state — NOT a real checkbox to toggle. Detected by the
            // `_yesno_`-prefixed wrapper class Ashby applies to this widget.
            const yesNoContainer = entry.querySelector('[class*="_yesno_"]');
            if (yesNoContainer) {
                const buttons = Array.from(yesNoContainer.querySelectorAll('button'));
                const hiddenCb = yesNoContainer.querySelector('input[type="checkbox"]');
                if (buttons.length && hiddenCb) {
                    hiddenCb.dataset.abIdx = idx;
                    fields.push({
                        index: idx++, tag: 'button', type: 'yesno', role: 'yesno',
                        id: hiddenCb.id || '', name: hiddenCb.name || '',
                        label, required, value: hiddenCb.checked ? 'Yes' : '',
                        options: buttons.map(b => b.innerText.trim()),
                    });
                    continue;
                }
            }

            // ── Checkbox GROUP (multiple checkboxes sharing one question — e.g.
            //    pronouns "select all that apply"). Distinguish from a single
            //    yes/no confirmation checkbox by count > 1.
            const checkboxes = Array.from(entry.querySelectorAll('input[type="checkbox"]')).filter(isVisible);
            if (checkboxes.length > 1) {
                for (const cb of checkboxes) {
                    const lbl = cb.closest('label') || document.querySelector('label[for="' + cb.id + '"]');
                    const optLabel = lbl ? lbl.innerText.trim() : (cb.name || '');
                    cb.dataset.abIdx = idx;
                    fields.push({
                        index: idx++, tag: 'input', type: 'checkbox', role: 'checkbox',
                        id: cb.id || '', name: cb.name || '',
                        label: label + ' — ' + optLabel, required, value: cb.checked ? 'true' : 'false',
                        options: [], isGroupOption: true,
                    });
                }
                continue;
            }
            if (checkboxes.length === 1) {
                const cb = checkboxes[0];
                cb.dataset.abIdx = idx;
                fields.push({
                    index: idx++, tag: 'input', type: 'checkbox', role: 'checkbox',
                    id: cb.id || '', name: cb.name || '',
                    label, required, value: cb.checked ? 'true' : 'false', options: [],
                });
                continue;
            }

            // ── Native <select> ──────────────────────────────────────────────
            const select = entry.querySelector('select');
            if (select && isVisible(select)) {
                select.dataset.abIdx = idx;
                const opts = Array.from(select.options).map(o => o.text.trim())
                    .filter(t => t && t.toLowerCase() !== 'select...');
                fields.push({
                    index: idx++, tag: 'select', type: 'select-one',
                    id: select.id || '', name: select.name || '',
                    label, required, value: select.options[select.selectedIndex]?.text.trim() || '',
                    options: opts,
                });
                continue;
            }

            // ── File upload ──────────────────────────────────────────────────
            const fileInput = entry.querySelector('input[type="file"]');
            if (fileInput && isVisible(fileInput)) {
                fileInput.dataset.abIdx = idx;
                fields.push({
                    index: idx++, tag: 'input', type: 'file',
                    id: fileInput.id || '', name: fileInput.name || '',
                    label, required, value: '', options: [],
                });
                continue;
            }

            // ── Textarea ─────────────────────────────────────────────────────
            const textarea = entry.querySelector('textarea:not(.g-recaptcha-response)');
            if (textarea && isVisible(textarea)) {
                textarea.dataset.abIdx = idx;
                fields.push({
                    index: idx++, tag: 'textarea', type: 'textarea',
                    id: textarea.id || '', name: textarea.name || '',
                    label, required, value: textarea.value || '', options: [],
                });
                continue;
            }

            // ── Plain text / email / tel / url / number input ───────────────
            const input = entry.querySelector(
                'input[type="text"], input[type="email"], input[type="tel"],' +
                'input[type="number"], input[type="url"], input:not([type])'
            );
            if (input && isVisible(input)) {
                input.dataset.abIdx = idx;
                fields.push({
                    index: idx++, tag: 'input', type: input.type || 'text',
                    id: input.id || '', name: input.name || '',
                    label, required, value: input.value || '', options: [],
                });
                continue;
            }
        }

        return fields;
    }""", [FIELD_ENTRY_SEL, QUESTION_TITLE_SEL])
    return fields


# ── Field executors ───────────────────────────────────────────────────────────

async def ab_exec_text(page: Page, field: dict, value: str):
    idx = field["index"]
    try:
        el = page.locator(f"[data-ab-idx='{idx}']").first
        await el.scroll_into_view_if_needed(timeout=5000)
        await el.click(click_count=3, timeout=5000)
        await el.fill(value)
        print(f"    ✓ text  [{idx}] {field['label']!r} = {value!r}")
    except Exception as e:
        await page.evaluate(
            """([idx, value]) => {
                const el = document.querySelector('[data-ab-idx="' + idx + '"]');
                if (!el) return;
                el.focus();
                const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                if (setter) setter.call(el, value);
                else el.value = value;
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
            }""",
            [idx, value],
        )
        print(f"    ✓ text  [{idx}] {field['label']!r} = {value!r} (JS fallback: {e})")


async def ab_exec_select(page: Page, field: dict, value: str):
    idx = field["index"]
    opts = field.get("options", [])
    match = fuzzy_pick(opts, value) or value
    try:
        el = page.locator(f"select[data-ab-idx='{idx}']").first
        await el.select_option(label=match, timeout=5000)
        print(f"    ✓ sel   [{idx}] {field['label']!r} = {match!r}")
    except Exception as e:
        print(f"    ~ sel   [{idx}] {field['label']!r}: {e}")


async def ab_exec_radio(page: Page, field: dict, value: str):
    idx = field["index"]
    opts = field.get("options", [])
    rvals = field.get("radioValues", [])
    name = field.get("name", "")
    match = fuzzy_pick(opts, value) or (opts[0] if opts else value)
    match_idx = opts.index(match) if match in opts else 0
    rval = rvals[match_idx] if match_idx < len(rvals) else match

    # Ashby's Yes/No radio inputs commonly all share the SAME value attribute (e.g.
    # value="on" for every option in the group) — a name+value CSS lookup can't
    # disambiguate them and always resolves to whichever option is first in DOM order,
    # silently clicking the wrong one. Only trust the name+value lookup when the
    # group's values are actually distinct; otherwise skip straight to matching by the
    # rendered label text, which IS reliably distinct.
    _rvals_distinct = len(set(rvals)) == len(rvals) and len(rvals) > 0

    clicked = False
    if name and rval and _rvals_distinct:
        clicked = await page.evaluate(
            "([name, rval]) => { const r = document.querySelector('input[type=\"radio\"][name=\"' + name + '\"][value=\"' + rval + '\"]'); if (r) { r.click(); return true; } return false; }",
            [name, rval],
        )
    if not clicked and name:
        # Scope strictly to radios sharing this exact group `name` — a page-wide label-text
        # search (e.g. "label with text 'Yes'") is NOT safe when multiple Yes/No radio
        # groups exist on the same page (common: "unrestricted right to work" AND "require
        # sponsorship" both have Yes/No options) — it silently clicks the first "Yes" label
        # found anywhere on the page, which may belong to a completely different question.
        clicked = await page.evaluate(
            """([name, matchText]) => {
                const radios = Array.from(document.querySelectorAll('input[type="radio"][name="' + name + '"]'));
                const target = radios.find(r => {
                    const lbl = document.querySelector('label[for="' + r.id + '"]');
                    return lbl && lbl.innerText.trim().toLowerCase() === matchText.toLowerCase();
                }) || radios[0];
                if (target) { target.click(); return true; }
                return false;
            }""",
            [name, match],
        )
    await page.wait_for_timeout(300)
    print(f"    {'✓' if clicked else '~'} radio [{idx}] {field['label']!r} = {match!r}")


async def ab_exec_checkbox(page: Page, field: dict, value: str):
    want = value.lower() in ("true", "yes", "on", "checked", "1")
    idx = field["index"]
    fid = field.get("id", "")

    current_checked = await page.evaluate(
        "(idx) => { const el = document.querySelector('[data-ab-idx=\"' + idx + '\"]'); return el ? el.checked : null; }",
        idx,
    )
    if (want and current_checked) or (not want and not current_checked):
        print(f"    ✓ check [{idx}] {field['label']!r} = {value!r} (already)")
        return

    clicked = False
    if fid:
        try:
            lbl = page.locator(f"label[for='{fid}']").first
            if await lbl.count():
                await lbl.click(timeout=3000)
                clicked = True
        except Exception:
            pass
    if not clicked:
        try:
            el = page.locator(f"[data-ab-idx='{idx}']").first
            await el.click(force=True, timeout=3000)
            clicked = True
        except Exception:
            pass
    await page.wait_for_timeout(200)
    print(f"    {'✓' if clicked else '~'} check [{idx}] {field['label']!r} = {value!r}")


async def ab_exec_yesno(page: Page, field: dict, value: str):
    """Click the correct button in an Ashby Yes/No button-pair widget (see scan_fields'
    `_yesno_` detection — the underlying checkbox is display:none and must not be clicked
    directly; only the visible <button>Yes</button>/<button>No</button> pair responds)."""
    idx = field["index"]
    opts = field.get("options") or ["Yes", "No"]
    want_yes = value.lower() in ("true", "yes", "on", "checked", "1")
    match = fuzzy_pick(opts, "Yes" if want_yes else "No") or opts[0]
    clicked = False
    try:
        hidden_cb = page.locator(f"[data-ab-idx='{idx}']").first
        container = hidden_cb.locator("xpath=..")
        btn = container.locator("button").filter(has_text=match).first
        if await btn.count():
            await btn.click(timeout=3000)
            clicked = True
    except Exception:
        pass
    await page.wait_for_timeout(200)
    print(f"    {'✓' if clicked else '~'} y/n   [{idx}] {field['label']!r} = {match!r}")


async def ab_exec_file(page: Page, field: dict, resume_path: str):
    idx = field["index"]
    if not resume_path or not Path(resume_path).exists():
        print(f"    ~ file  résumé not found: {resume_path!r}")
        return
    try:
        file_input = page.locator(f"[data-ab-idx='{idx}']").first
        await file_input.set_input_files(resume_path)
        print(f"    ✓ file  [{idx}] Résumé uploaded: {Path(resume_path).name}")
    except Exception as e:
        print(f"    ~ file  [{idx}] Upload failed: {e}")


async def execute_answer(page: Page, field: dict, value: str):
    if not value:
        return
    ftype = field.get("type", "")
    role = field.get("role", "")
    tag = field.get("tag", "")
    try:
        if ftype == "yesno" or role == "yesno":
            await ab_exec_yesno(page, field, value)
        elif ftype == "checkbox" or role == "checkbox":
            await ab_exec_checkbox(page, field, value)
        elif ftype == "radio" or role == "radio":
            await ab_exec_radio(page, field, value)
        elif tag == "select" or ftype == "select-one":
            await ab_exec_select(page, field, value)
        elif ftype == "file":
            await ab_exec_file(page, field, RESUME_PATH)
        else:
            await ab_exec_text(page, field, value)
    except Exception as e:
        print(f"    ~ err   [{field['index']}] {field['label']!r}: {e}")


# ── Listing scraper (salary / locations) ─────────────────────────────────────

async def scrape_listing_meta(page: Page) -> tuple[str | None, list[str]]:
    text = await page.locator("body").inner_text()
    salary = None
    sal_m = re.search(r'\$\s*([\d,]+)\s*(?:–|-|to)\s*\$\s*([\d,]+)\s*(?:K|k|,000)?', text)
    if sal_m:
        lo = int(sal_m.group(1).replace(",", ""))
        hi = int(sal_m.group(2).replace(",", ""))
        if lo < 1000: lo *= 1000
        if hi < 1000: hi *= 1000
        salary = str((lo + hi) // 2)

    loc_m = re.findall(
        r'(?:Location|Office|Based in|Where)[\s:]+([A-Za-z ,/]+(?:CA|NY|TX|WA|CO|MA|IL|VA|GA|OR|FL|BC|ON))', text)
    locations = list({m.strip() for m in loc_m if m.strip()}) if loc_m else []
    return salary, locations


def build_runtime_profile(salary: str | None, locations: list[str]) -> str:
    p = json.loads(PROFILE_SUMMARY)
    p["job_listing_salary"] = salary
    p["job_listing_locations"] = locations
    p["today"] = datetime.date.today().isoformat()
    return json.dumps(p, indent=2)


# ── Artifacts report ──────────────────────────────────────────────────────────

_report: dict = {}


def _write_report(job_url: str, status: str, fields_filled: int, fields_total: int):
    _report.update({
        "job_url": job_url,
        "started": _report.get("started", datetime.datetime.now().isoformat()),
        "final": status,
        "fields_filled": fields_filled,
        "fields_total": fields_total,
    })
    report_path = ARTIFACTS / "run_report_ashby.json"
    report_path.write_text(json.dumps(_report, indent=2, ensure_ascii=False))
    print(f"  [report] Written → {report_path}")


def _mark_applied(job_url: str):
    """Mark this job Applied in data/jobs_tracker.csv after a confirmed successful submit.
    Mirrors app_workday.py / app_greenhouse.py's post-submit tracker update — lazy import,
    non-fatal on failure so a tracker hiccup never masks a real, successful submission."""
    try:
        from job_tracker import mark_applied_by_url
        mark_applied_by_url(job_url)
    except Exception as e:
        print(f"  [tracker] mark-applied failed (non-fatal): {e}")


# ── Main applicator ───────────────────────────────────────────────────────────

async def main(job_url: str, headed: bool = False):
    _report["started"] = datetime.datetime.now().isoformat()

    print(f"[AB] Job URL : {job_url}")
    print(f"[AB] Résumé : {RESUME_PATH or '(not found)'}")
    print(f"[AB] DeepSeek: {'enabled' if DEEPSEEK_KEY else 'DISABLED (rule-path fallback)'}")
    print()

    async with async_playwright() as p:
        browser, context, page = await launch_browser(
            p,
            headed,
            extra_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": "https://www.google.com/",
            },
        )

        try:
            print("[AB] Navigating to apply form …")
            await page.goto(job_url, timeout=45000, wait_until="networkidle")
            await page.wait_for_timeout(2000)

            # Grab listing salary / locations before the form takes over
            salary, locations = await scrape_listing_meta(page)
            print(f"[AB] Listing salary: {salary}  |  locations: {locations}")
            runtime_profile = build_runtime_profile(salary, locations)

            # Scan fields
            fields = await scan_fields(page)
            print(f"[AB] Scanned {len(fields)} fields")

            # Resolve answers
            if DEEPSEEK_KEY:
                print("[AB] Sending fields to DeepSeek …")
                answers = await deepseek_fill_page(fields, profile_override=runtime_profile)
                print(f"[AB] DeepSeek returned {len(answers)} answers")
                ds_indices = {a["index"] for a in answers}
                rule_answers = rule_based_fill_fields(fields)
                for ra in rule_answers:
                    if ra["index"] not in ds_indices:
                        answers.append(ra)
            else:
                print("[AB] Rule-based fallback (no DeepSeek key) …")
                answers = rule_based_fill_fields(fields)

            answer_map = {a["index"]: a["value"] for a in answers}

            # Résumé upload — the file field is answered like any other via rule_based_answer's
            # generic handling is skipped (no text value applies); upload directly using the
            # scanned file field's index if present, exactly once.
            file_fields = [f for f in fields if f.get("type") == "file"]
            for ff in file_fields:
                await ab_exec_file(page, ff, RESUME_PATH)

            print(f"[AB] {len(answer_map)} fields to fill")

            filled = 0
            _seen_once: set = set()
            _DEDUP_KWS = ("linkedin", "website", "github")
            for field in fields:
                if field.get("type") == "file":
                    continue  # already handled above
                val = answer_map.get(field["index"])
                if not val:
                    continue
                lbl_low = field.get("label", "").lower()
                dk = next((k for k in _DEDUP_KWS if k in lbl_low), None)
                if dk:
                    if dk in _seen_once:
                        print(f"    ⊘ skip  [{field['index']}] {field.get('label')!r} (duplicate {dk})")
                        continue
                    _seen_once.add(dk)
                await execute_answer(page, field, val)
                filled += 1

            print(f"\n[AB] Filled {filled}/{len(fields)} fields.")

            ss_path = ARTIFACTS / "ab_before_submit.png"
            await page.screenshot(path=str(ss_path), full_page=True)
            print(f"[AB] Screenshot saved → {ss_path.name}")

            _write_report(job_url, "ready_to_submit", filled, len(fields))

            print("\n" + "=" * 60)
            print("  REVIEW COMPLETE — BOT HAS STOPPED")
            print("  Open the browser window to inspect / correct any fields.")
            print("  Press [Enter] here when ready to SUBMIT the application.")
            print("  Press Ctrl+C to CANCEL without submitting.")
            print("=" * 60)

            if headed:
                try:
                    await asyncio.to_thread(input, "")
                except (KeyboardInterrupt, EOFError):
                    print("[AB] Cancelled — application NOT submitted.")
                    _write_report(job_url, "cancelled", filled, len(fields))
                    return

                url_before_submit = page.url
                submitted = await page.evaluate("""() => {
                    const btn = Array.from(document.querySelectorAll('button[type="submit"], input[type="submit"]'))
                        .find(b => /submit|apply/i.test(b.innerText || b.value || ''));
                    if (btn) { btn.click(); return true; }
                    return false;
                }""")
                if submitted:
                    await page.wait_for_timeout(3000)

                    # Ashby forms commonly run invisible reCAPTCHA v3 — a bot-driven click
                    # can be silently rejected server-side while the form stays visually
                    # unchanged. Require unambiguous evidence of success (confirmation text
                    # or a URL change) before declaring victory — see app_greenhouse.py's
                    # identical fix for the mechanism and why weaker heuristics (form/button
                    # "gone") produce false positives.
                    recaptcha_present = await page.evaluate("""() => {
                        return !!document.querySelector('.grecaptcha-badge, [class*="recaptcha"], iframe[src*="recaptcha"]');
                    }""")
                    actually_submitted = await page.evaluate("""() => {
                        const text = document.body.innerText.toLowerCase();
                        return /thank you for applying|application (has been |was )?(received|submitted)|we('ve| have) received your application|your application (has been|was) submitted/.test(text);
                    }""") or page.url != url_before_submit

                    ss2 = ARTIFACTS / "ab_after_submit.png"
                    await page.screenshot(path=str(ss2), full_page=True)

                    if not actually_submitted:
                        _reason = ("this page has an active reCAPTCHA badge — invisible reCAPTCHA "
                                   "v3 likely scored the bot-driven click as suspicious and Ashby "
                                   "silently rejected the submission server-side") if recaptcha_present \
                            else "a validation error or disabled button may have silently blocked it"
                        print(f"[AB] ⚠ Clicked Submit, but the page shows NO confirmation and the "
                              f"form is still present — the application likely did NOT go through "
                              f"({_reason}). Screenshot → {ss2.name}")
                        print("\n" + "=" * 60)
                        print("  CHECK THE BROWSER WINDOW — look for a validation error or")
                        print("  CAPTCHA, fix it, and click Submit yourself if needed.")
                        print("  The browser stays open and waits here — take as long as you need.")
                        print("  Press [Enter] once the application is fully submitted.")
                        print("  Press Ctrl+C to abandon (this closes the browser without submitting).")
                        print("=" * 60)
                        try:
                            await asyncio.to_thread(input, "")
                        except (KeyboardInterrupt, EOFError):
                            print("[AB] Cancelled — closing browser. Application may not be submitted.")
                            _write_report(job_url, "cancelled_unconfirmed_submit", filled, len(fields))
                            return
                        ss3 = ARTIFACTS / "ab_after_verification.png"
                        await page.screenshot(path=str(ss3), full_page=True)
                        print(f"[AB] ✓ Confirmed by user. Screenshot → {ss3.name}")
                        _write_report(job_url, "submitted_after_manual_confirmation", filled, len(fields))
                        _mark_applied(job_url)
                    else:
                        print(f"[AB] ✓ Submitted! Screenshot → {ss2.name}")
                        _write_report(job_url, "submitted", filled, len(fields))
                        _mark_applied(job_url)
                else:
                    print("[AB] Could not find submit button — submit manually in the browser.")
                    _write_report(job_url, "submit_button_not_found", filled, len(fields))
            else:
                print("[AB] Headless mode — run with --show to review and submit.")
                _write_report(job_url, "ready_to_submit_headless", filled, len(fields))

        except Exception as e:
            ss = ARTIFACTS / "ab_error.png"
            try:
                await page.screenshot(path=str(ss), full_page=True)
            except Exception:
                pass
            print(f"\n[AB] ERROR: {e}")
            print(f"[AB] Screenshot → {ss.name}")
            _write_report(job_url, f"error: {e}", 0, 0)
            raise
        finally:
            await browser.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(
        description="Ashby Application Bot",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("job_url", help="Ashby application URL (jobs.ashbyhq.com/{company}/{uuid}/application)")
    parser.add_argument("--show", action="store_true", help="Show Chrome window (required to submit)")
    args = parser.parse_args()
    asyncio.run(main(args.job_url, headed=args.show))
