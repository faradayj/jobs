"""
app_ashby.py  —  Ashby Application Bot
==================================================
Fills Ashby job applications (jobs.ashbyhq.com/{company}/{job-id}/application).

Architecture:
  1. Navigate directly to the apply URL (embed or direct).
  2. Scrape listing metadata (salary midpoint and locations) from page text or tracker DB.
  3. Scan ALL visible form fields:
     - Text / Email / Tel / Number / URL inputs
     - Textareas (preserves multi-line project descriptions)
     - Radio groups (scoped by unique option indices and labels)
     - Yes/No button-pair widgets ([class*="_yesno_"])
     - Single checkboxes and grouped multi-select checkboxes
     - File upload disambiguation (Resume vs Transcript vs Cover Letter)
     - Repeatable Education history widgets (autocomplete school, degree, major, dates)
  4. Send batch fields to DeepSeek for pure semantic evaluation against candidate profile.
  5. Execute answers deterministically:
     - Dispatches proper DOM input/change/blur events for React state sync.
  6. Audit form validation before submission (checks native HTML5 constraint validation).
  7. Support dry-run verification mode (--no-submit): captures full-page screenshot
     and report without submitting.
  8. For live submissions: pauses for user review in headed mode, verifies actual
     confirmation / URL change (handling invisible reCAPTCHA v3), and marks job
     Applied in data/jobs_tracker.csv.

Usage:
  # Dry-run test (no submission):
  python src/app_ashby.py "JOB_URL" --no-submit

  # Review in headed Chrome before submitting:
  python src/app_ashby.py "JOB_URL" --show
"""

import argparse
import asyncio
import datetime
import json
import random
import re
import sys
from pathlib import Path

from playwright.async_api import async_playwright, Page

from app_common import (
    RESUME_PATH, TRANSCRIPT_PATH, DEEPSEEK_KEY,
    PROFILE_SUMMARY, EDU,
    deepseek_fill_page, deepseek_pick_option,
    ARTIFACTS_DIR,
    launch_browser,
    scrape_salary,
    snooze_job_by_url,
)

ARTIFACTS = ARTIFACTS_DIR
ARTIFACTS.mkdir(exist_ok=True)

FIELD_ENTRY_SEL = '.ashby-application-form-field-entry, [class*="_fieldEntry_"]'
QUESTION_TITLE_SEL = ".ashby-application-form-question-title"


def is_ashby_url(url: str) -> bool:
    return "ashbyhq.com" in url.lower()


# ── Field scanner ─────────────────────────────────────────────────────────────

async def scan_fields(page: Page) -> list[dict]:
    """Scan the Ashby apply form and return structured field descriptors.

    Each question lives in a .ashby-application-form-field-entry (div or fieldset)
    with a .ashby-application-form-question-title label.
    We tag each interactable element with data-ab-idx (and data-ab-opt-idx for radios).
    Education history entries are handled by a dedicated routine and excluded here.
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
            if (lbl && (/_required_/.test(lbl.className) || lbl.innerText.includes('*'))) return true;
            const reqInput = entry.querySelector('input[required], textarea[required], select[required]');
            return !!reqInput;
        }

        const entries = Array.from(document.querySelectorAll(FIELD_ENTRY_SEL));
        for (const entry of entries) {
            // Strictly ignore the top autofill-from-resume pane to prevent triggering background resume parser
            if (entry.closest('.ashby-application-form-autofill-pane, [class*="autofillPane"]')) {
                continue;
            }

            // Repeatable education history is handled separately by fill_ashby_education
            if (entry.getAttribute('data-field-path') === '_systemfield_education_history' ||
                entry.querySelector('[class*="repeatableEducationEntry"]')) {
                continue;
            }

            const label = getLabel(entry);
            const required = isRequired(entry);

            // ── Radio group ──────────────────────────────────────────────────
            const radios = Array.from(entry.querySelectorAll('input[type="radio"]')).filter(isVisible);
            if (radios.length) {
                const texts = radios.map((r, rIdx) => {
                    r.dataset.abIdx = idx;
                    r.dataset.abOptIdx = rIdx;
                    const lbl = document.querySelector('label[for="' + r.id + '"]') || r.closest('label');
                    return lbl ? lbl.innerText.trim() : (r.value || '');
                });
                const values = radios.map(r => r.value || '');
                fields.push({
                    index: idx++, tag: 'input', type: 'radio', role: 'radio',
                    id: radios[0].id || '', name: radios[0].name || '',
                    label, required, value: '', options: texts, radioValues: values,
                });
                continue;
            }

            // ── Yes/No button-pair widget ────────────────────────────────────
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

            // ── Checkbox GROUP (multiple checkboxes sharing one question) ────
            const checkboxes = Array.from(entry.querySelectorAll('input[type="checkbox"]')).filter(isVisible);
            if (checkboxes.length > 1) {
                for (const cb of checkboxes) {
                    const lbl = cb.closest('label') || document.querySelector('label[for="' + cb.id + '"]');
                    const optLabel = lbl ? lbl.innerText.trim() : (cb.name || '');
                    cb.dataset.abIdx = idx;
                    fields.push({
                        index: idx++, tag: 'input', type: 'checkbox', role: 'checkbox',
                        id: cb.id || '', name: cb.name || '',
                        label: label ? `${label} — ${optLabel}` : optLabel,
                        required, value: cb.checked ? 'true' : 'false',
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
            // Do not require isVisible: modern file inputs are intentionally hidden (0x0 or clip)
            const fileInput = entry.querySelector('input[type="file"]');
            if (fileInput) {
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

            // ── Autocomplete / Combobox (e.g. Location search) ────────────────
            const autocompleteInput = entry.querySelector(
                'input[role="combobox"], input.ashby-application-form-input-autocomplete, [class*="input-autocomplete"]'
            );
            if (autocompleteInput && isVisible(autocompleteInput)) {
                autocompleteInput.dataset.abIdx = idx;
                fields.push({
                    index: idx++, tag: 'input', type: 'autocomplete', role: 'combobox',
                    id: autocompleteInput.id || '', name: autocompleteInput.name || '',
                    label, required, value: autocompleteInput.value || '', options: [],
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

async def ab_exec_autocomplete(page: Page, field: dict, value: str):
    """Handle Ashby autocomplete / combobox dropdowns (e.g. location, city search) with human pacing."""
    idx = field["index"]
    clean_val = value.strip()
    city_keyword = clean_val.split(",")[0].strip() or clean_val
    clicked = False
    selected_text = ""
    try:
        auto_input = page.locator(f"[data-ab-idx='{idx}']").first
        await auto_input.scroll_into_view_if_needed(timeout=3000)
        try:
            await auto_input.hover(timeout=2000)
            await page.wait_for_timeout(random.randint(120, 250))
        except Exception:
            pass
        await auto_input.click(timeout=3000)
        await auto_input.fill("")
        await auto_input.press_sequentially(city_keyword, delay=random.randint(35, 70))

        # Wait for options to appear in dropdown
        try:
            await page.wait_for_selector(
                "[role='option'], .ashby-application-form-input-autocomplete-popup-result",
                state="visible",
                timeout=4000,
            )
            await page.wait_for_timeout(random.randint(300, 500))
            options = await page.locator("[role='option'], .ashby-application-form-input-autocomplete-popup-result").all()
            opt_texts = [await opt.inner_text() for opt in options]
            target_opt = None
            for idx_opt, txt in enumerate(opt_texts):
                if city_keyword.lower() in txt.lower():
                    target_opt = options[idx_opt]
                    break
            if not target_opt and opt_texts and DEEPSEEK_KEY:
                chosen = await deepseek_pick_option(field.get("label", ""), opt_texts, context=f"Target candidate value: {clean_val}")
                if chosen:
                    for idx_opt, txt in enumerate(opt_texts):
                        if txt.strip().lower() == chosen.strip().lower():
                            target_opt = options[idx_opt]
                            break
            if not target_opt and options:
                target_opt = options[0]

            if target_opt:
                selected_text = await target_opt.inner_text()
                try:
                    await target_opt.hover(timeout=2000)
                    await page.wait_for_timeout(random.randint(150, 280))
                except Exception:
                    pass
                await target_opt.click(timeout=3000)
                clicked = True
            else:
                await auto_input.press("Enter")
                clicked = True
        except Exception:
            await auto_input.press("Enter")
            clicked = True

        await page.wait_for_timeout(random.randint(300, 600))
    except Exception as e:
        print(f"    ~ auto  [{idx}] error: {e}")

    final_val = selected_text or clean_val
    print(f"    {'✓' if clicked else '~'} auto  [{idx}] {field['label']!r} = {final_val!r}")


async def ab_exec_text(page: Page, field: dict, value: str):
    idx = field["index"]
    tag = field.get("tag", "").lower()
    if tag == "input":
        clean_val = value.split("\n")[0].strip()
    else:
        clean_val = value.strip()

    try:
        el = page.locator(f"[data-ab-idx='{idx}']").first
        is_combo = await el.evaluate(
            "e => e.getAttribute('role') === 'combobox' || e.classList.contains('ashby-application-form-input-autocomplete')"
        )
        if is_combo:
            await ab_exec_autocomplete(page, field, clean_val)
            return

        await el.scroll_into_view_if_needed(timeout=5000)
        try:
            await el.hover(timeout=2000)
            await page.wait_for_timeout(random.randint(100, 220))
        except Exception:
            pass

        await el.click(click_count=3, timeout=5000)
        await page.wait_for_timeout(random.randint(80, 160))

        # Human-like typing for standard inputs vs textareas
        if tag == "input" and len(clean_val) < 120:
            await el.fill("")
            await el.press_sequentially(clean_val, delay=random.randint(20, 45))
        else:
            # Long essay / textarea response: simulate typing initial segment then realistic paste
            await el.fill("")
            if len(clean_val) > 20:
                head = clean_val[:15]
                tail = clean_val[15:]
                await el.press_sequentially(head, delay=random.randint(25, 45))
                await page.keyboard.insert_text(tail)
            else:
                await el.press_sequentially(clean_val, delay=random.randint(20, 40))
            await page.wait_for_timeout(random.randint(400, 800))

        # Natively blur the input so React 18 bubbles focusout and triggers Ashby's ApiSetFormValue mutation
        await el.evaluate("e => e.blur()")
        await page.wait_for_timeout(random.randint(250, 450))
        print(f"    ✓ text  [{idx}] {field['label']!r} = {clean_val!r}")
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
                el.blur();
            }""",
            [idx, clean_val],
        )
        await page.wait_for_timeout(250)
        print(f"    ✓ text  [{idx}] {field['label']!r} = {clean_val!r} (JS fallback: {e})")


async def ab_exec_select(page: Page, field: dict, value: str):
    idx = field["index"]
    opts = field.get("options", [])
    # 1. Exact match
    match = next((o for o in opts if o.strip().lower() == value.strip().lower()), None)
    # 2. DeepSeek evaluation on live options
    if not match and opts and DEEPSEEK_KEY:
        match = await deepseek_pick_option(field.get("label", ""), opts, context=f"Target candidate value: {value}")
    # 3. Substring match fallback
    if not match:
        match = next((o for o in opts if value.strip().lower() in o.strip().lower()), None)
    match = match or value
    try:
        el = page.locator(f"select[data-ab-idx='{idx}']").first
        await el.scroll_into_view_if_needed(timeout=3000)
        try:
            await el.hover(timeout=2000)
            await page.wait_for_timeout(random.randint(120, 250))
        except Exception:
            pass
        await el.select_option(label=match, timeout=5000)
        await el.evaluate("e => e.blur()")
        await page.wait_for_timeout(random.randint(250, 450))
        print(f"    ✓ sel   [{idx}] {field['label']!r} = {match!r}")
    except Exception as e:
        print(f"    ~ sel   [{idx}] {field['label']!r}: {e}")


async def ab_exec_radio(page: Page, field: dict, value: str):
    idx = field["index"]
    opts = field.get("options", [])
    val_norm = value.strip().lower()

    # 1. Exact match
    match = next((o for o in opts if o.strip().lower() == val_norm), None)
    # 2. DeepSeek evaluation on live options
    if not match and opts and DEEPSEEK_KEY:
        match = await deepseek_pick_option(field.get("label", ""), opts, context=f"Target candidate value: {value}")
    # 3. Substring match
    if not match:
        match = next((o for o in opts if val_norm in o.strip().lower() or o.strip().lower() in val_norm), None)
    if not match and opts:
        match = opts[0]

    match_idx = opts.index(match) if match in opts else 0
    clicked = False

    try:
        radio = page.locator(f"input[type='radio'][data-ab-idx='{idx}'][data-ab-opt-idx='{match_idx}']").first
        if await radio.count():
            await radio.scroll_into_view_if_needed(timeout=3000)
            lbl = page.locator(f"label[for='{await radio.get_attribute('id')}']").first
            target_el = lbl if await lbl.count() else radio
            try:
                await target_el.hover(timeout=2000)
                await page.wait_for_timeout(random.randint(120, 250))
            except Exception:
                pass
            await target_el.click(timeout=3000)
            clicked = True
    except Exception:
        pass

    if not clicked:
        clicked = await page.evaluate(
            """([idx, matchIdx, matchText]) => {
                const radio = document.querySelector(`input[type="radio"][data-ab-idx="${idx}"][data-ab-opt-idx="${matchIdx}"]`);
                if (radio) {
                    const lbl = document.querySelector('label[for="' + radio.id + '"]') || radio.closest('label');
                    if (lbl) { lbl.click(); return true; }
                    radio.click();
                    radio.dispatchEvent(new Event('change', {bubbles: true}));
                    return true;
                }
                const first = document.querySelector(`input[type="radio"][data-ab-idx="${idx}"]`);
                if (first && first.name) {
                    const group = Array.from(document.querySelectorAll(`input[type="radio"][name="${first.name}"]`));
                    const target = group.find(r => {
                        const l = document.querySelector('label[for="' + r.id + '"]') || r.closest('label');
                        return l && l.innerText.trim().toLowerCase() === matchText.toLowerCase();
                    });
                    if (target) {
                        const l = document.querySelector('label[for="' + target.id + '"]') || target.closest('label');
                        if (l) { l.click(); return true; }
                        target.click();
                        target.dispatchEvent(new Event('change', {bubbles: true}));
                        return true;
                    }
                }
                return false;
            }""",
            [idx, match_idx, match],
        )

    await page.wait_for_timeout(random.randint(300, 550))
    print(f"    {'✓' if clicked else '~'} radio [{idx}] {field['label']!r} = {match!r}")


async def ab_exec_checkbox(page: Page, field: dict, value: str):
    want = str(value).lower() in ("true", "yes", "on", "checked", "1")
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
                await lbl.scroll_into_view_if_needed(timeout=3000)
                try:
                    await lbl.hover(timeout=2000)
                    await page.wait_for_timeout(random.randint(100, 200))
                except Exception:
                    pass
                await lbl.click(timeout=3000)
                clicked = True
        except Exception:
            pass
    if not clicked:
        try:
            el = page.locator(f"[data-ab-idx='{idx}']").first
            await el.scroll_into_view_if_needed(timeout=3000)
            await el.click(force=True, timeout=3000)
            clicked = True
        except Exception:
            pass
    await page.wait_for_timeout(random.randint(250, 450))
    print(f"    {'✓' if clicked else '~'} check [{idx}] {field['label']!r} = {value!r}")


async def ab_exec_yesno(page: Page, field: dict, value: str):
    """Click the correct button in an Ashby Yes/No button-pair widget with human pacing."""
    idx = field["index"]
    opts = field.get("options") or ["Yes", "No"]
    want_yes = str(value).lower() in ("true", "yes", "on", "checked", "1")
    target_val = "Yes" if want_yes else "No"
    match = next((o for o in opts if o.strip().lower() == target_val.lower()), None) or (opts[0] if opts else target_val)
    clicked = False

    try:
        hidden_cb = page.locator(f"[data-ab-idx='{idx}']").first
        container = hidden_cb.locator("xpath=..")
        btn = container.locator("button").filter(has_text=re.compile(r'^\s*' + re.escape(match) + r'\s*$', re.I)).first
        if not await btn.count():
            btn = container.locator(f"button[data-option='{target_val.lower()}']").first

        if await btn.count():
            await btn.scroll_into_view_if_needed(timeout=3000)
            try:
                await btn.hover(timeout=2000)
                await page.wait_for_timeout(random.randint(120, 250))
            except Exception:
                pass

            # Check if button is ALREADY pressed with the target answer
            is_already_pressed = await btn.get_attribute("aria-pressed") == "true"
            if is_already_pressed:
                clicked = True
            else:
                # Click once. Ashby buttons toggle off if clicked when already selected!
                await btn.click(timeout=3000)
                # Wait for aria-pressed to reflect selection
                for _ in range(5):
                    if await btn.get_attribute("aria-pressed") == "true":
                        clicked = True
                        break
                    await page.wait_for_timeout(100)
    except Exception as e:
        print(f"    ~ y/n   [{idx}] click error: {e}")

    await page.wait_for_timeout(random.randint(300, 550))
    print(f"    {'✓' if clicked else '~'} y/n   [{idx}] {field['label']!r} = {match!r}")


async def ab_exec_file(page: Page, field: dict) -> bool:
    idx = field["index"]
    label = field.get("label", "").lower()
    fid = field.get("id", "")

    if "transcript" in label:
        if TRANSCRIPT_PATH and Path(TRANSCRIPT_PATH).exists():
            target_path = TRANSCRIPT_PATH
            file_desc = "Transcript"
        else:
            print(f"    ~ file  [{idx}] Optional transcript not found, skipping.")
            return False
    elif "cover" in label or "letter" in label:
        print(f"    ~ file  [{idx}] Cover letter is optional, skipping.")
        return False
    else:
        target_path = RESUME_PATH
        file_desc = "Résumé"

    if not target_path or not Path(target_path).exists():
        print(f"    ~ file  [{idx}] {file_desc} file not found: {target_path!r}")
        return False

    # Try locator by data-ab-idx, then id, then dropzone input[type=file]
    locators = [
        page.locator(f"[data-ab-idx='{idx}']").first,
    ]
    if fid:
        locators.append(page.locator(f"#{fid}").first)
    if file_desc == "Résumé":
        locators.extend([
            page.locator("input[type='file']#_systemfield_resume").first,
            page.locator(".ashby-application-form-input-file input[type='file']").first,
        ])
    elif file_desc == "Transcript":
        locators.append(page.locator("input[type='file'][name*='transcript'], input[type='file'][id*='transcript']").first)

    uploaded = False
    for loc in locators:
        try:
            if await loc.count():
                # Strictly ensure we do not touch the autofill pane
                is_autofill = await loc.evaluate(
                    "el => !!el.closest('.ashby-application-form-autofill-pane, [class*=\"autofillPane\"]')"
                )
                if is_autofill:
                    continue
                await loc.set_input_files(target_path)
                uploaded = True
                break
        except Exception:
            continue

    if uploaded:
        # Wait up to 10s for Ashby to complete S3 upload & render the attached file pill
        try:
            await page.wait_for_selector(
                ".ashby-application-form-input-file-item, [class*='inputFileItem'], [class*='_file_']",
                state="visible",
                timeout=10000,
            )
            # Give Ashby's ApiSetFormValueToFile GraphQL mutation a moment to settle
            await page.wait_for_timeout(1000)
        except Exception:
            pass
        print(f"    ✓ file  [{idx}] {file_desc} uploaded & attached: {Path(target_path).name}")
        return True
    else:
        print(f"    ~ file  [{idx}] {file_desc} upload could not find input element.")
        return False


async def execute_answer(page: Page, field: dict, value: str):
    if value is None or value == "":
        return
    ftype = field.get("type", "")
    role = field.get("role", "")
    tag = field.get("tag", "")
    try:
        if ftype == "autocomplete" or role == "combobox":
            await ab_exec_autocomplete(page, field, str(value))
        elif ftype == "yesno" or role == "yesno":
            await ab_exec_yesno(page, field, value)
        elif ftype == "checkbox" or role == "checkbox":
            await ab_exec_checkbox(page, field, value)
        elif ftype == "radio" or role == "radio":
            await ab_exec_radio(page, field, value)
        elif tag == "select" or ftype == "select-one":
            await ab_exec_select(page, field, value)
        elif ftype == "file":
            await ab_exec_file(page, field)
        else:
            await ab_exec_text(page, field, str(value))
    except Exception as e:
        print(f"    ~ err   [{field['index']}] {field['label']!r}: {e}")


# ── Repeatable Education Handler ──────────────────────────────────────────────

async def fill_ashby_education(page: Page):
    """Handle Ashby's repeatable education entry card if present."""
    entry = page.locator('[data-field-path="_systemfield_education_history"], [class*="repeatableEducationEntry"]').first
    if not await entry.count():
        return

    print("  [EDU] Detected Ashby Education History block...")
    if not EDU:
        # If empty and not required, delete empty card to avoid validation error
        del_btn = page.locator('button', has_text="Delete").first
        if await del_btn.count():
            await del_btn.click()
            print("  [EDU] Deleted empty education card.")
        return

    active_edu = EDU[0]

    # 1. School autocomplete
    school_input = page.locator("input[placeholder*='Search schools']").first
    if await school_input.count():
        school_name = active_edu["institution_variants"][0]
        try:
            await school_input.click()
            await school_input.fill("")
            await school_input.type(school_name, delay=25)
            await page.wait_for_timeout(800)
            opt = page.locator('[role="option"]').filter(has_text=school_name).first
            if await opt.count():
                await opt.click()
                print(f"    ✓ edu school = {school_name!r}")
            else:
                await school_input.press("Enter")
                print(f"    ✓ edu school = {school_name!r} (enter)")
        except Exception as e:
            print(f"    ~ edu school: {e}")

    # 2. Degree
    degree_input = page.locator("input[id*='education_history-degree'], input[placeholder*='Bachelor of Science']").first
    if await degree_input.count():
        deg = active_edu.get("degree_type", "Master of Science")
        await degree_input.fill(deg)
        print(f"    ✓ edu degree = {deg!r}")

    # 3. Major / Field of Study
    major_input = page.locator("input[id*='education_history-major'], input[placeholder*='Computer Science']").first
    if await major_input.count():
        maj = active_edu.get("major_search_term", "Computer Science")
        await major_input.fill(maj)
        print(f"    ✓ edu major = {maj!r}")

    # 4. Dates
    selects = await page.locator('[class*="repeatableEducationEntry"] select').all()
    if len(selects) >= 4:
        try:
            await selects[0].select_option(label=active_edu.get("start_month", "August"))
            await selects[1].select_option(label=str(active_edu.get("start_year", "2025")))
            await selects[2].select_option(label=active_edu.get("end_month", "December"))
            await selects[3].select_option(label=str(active_edu.get("end_year", "2026")))
            print(f"    ✓ edu dates = {active_edu.get('start_month')} {active_edu.get('start_year')} to {active_edu.get('end_month')} {active_edu.get('end_year')}")
        except Exception as e:
            print(f"    ~ edu dates: {e}")


# ── Form Validation Audit ─────────────────────────────────────────────────────

async def audit_form_validation(page: Page) -> list[dict]:
    """Inspect all inputs for native HTML5 constraint validation and Ashby custom widget errors."""
    return await page.evaluate("""() => {
        const issues = [];

        // 1. Native HTML5 constraint validation
        const invalids = Array.from(document.querySelectorAll('input:invalid, select:invalid, textarea:invalid'));
        for (const i of invalids) {
            const entry = i.closest('.ashby-application-form-field-entry, [class*="_fieldEntry_"]');
            const label = entry ? (entry.querySelector('.ashby-application-form-question-title')?.innerText || '') : '';
            issues.push({
                id: i.id || '',
                name: i.name || '',
                label: label.trim().replace(/\\*$/, '').trim() || i.placeholder || i.name,
                valMsg: i.validationMessage || 'Value is invalid'
            });
        }

        // 2. Required File inputs (must have attached file pill)
        const fileEntries = Array.from(document.querySelectorAll('[data-field-path*="resume"], .ashby-application-form-input-file'));
        for (const fe of fileEntries) {
            const entry = fe.closest('.ashby-application-form-field-entry, [class*="_fieldEntry_"]');
            const isReq = entry ? (entry.querySelector('._required_') || entry.innerText.includes('*')) : false;
            if (isReq) {
                const hasPill = entry.querySelector('._file_10xk4_76, [class*="inputFileItem"], [class*="_file_"]');
                const fileInput = entry.querySelector('input[type="file"]');
                const hasFile = (fileInput && fileInput.files && fileInput.files.length > 0) || hasPill;
                if (!hasFile) {
                    const label = entry.querySelector('.ashby-application-form-question-title')?.innerText || 'Resume';
                    issues.push({
                        id: fileInput ? fileInput.id : '',
                        name: fileInput ? fileInput.name : '',
                        label: label.trim().replace(/\\*$/, '').trim(),
                        valMsg: 'File is required but not attached'
                    });
                }
            }
        }

        // 3. Required Yes/No button widgets
        const yesNoContainers = Array.from(document.querySelectorAll('[class*="_yesno_"]'));
        for (const yn of yesNoContainers) {
            const entry = yn.closest('.ashby-application-form-field-entry, [class*="_fieldEntry_"]');
            const isReq = entry ? (entry.querySelector('._required_') || entry.innerText.includes('*')) : false;
            if (isReq) {
                const pressed = yn.querySelector('button[aria-pressed="true"]');
                let savedVal = null;
                const key = Object.keys(yn).find(k => k.startsWith('__reactFiber$') || k.startsWith('__reactInternalInstance$'));
                if (key) {
                    let curr = yn[key];
                    while (curr) {
                        if (curr.memoizedProps && 'savedValue' in curr.memoizedProps) {
                            savedVal = curr.memoizedProps.savedValue;
                            break;
                        }
                        curr = curr.return;
                    }
                }
                const hasValidAnswer = pressed && (key ? (savedVal !== null && savedVal !== undefined) : true);
                if (!hasValidAnswer) {
                    const label = entry.querySelector('.ashby-application-form-question-title')?.innerText || 'Yes/No Question';
                    issues.push({
                        id: '',
                        name: '',
                        label: label.trim().replace(/\\*$/, '').trim(),
                        valMsg: pressed ? 'Selection was visually marked but dropped by server state (savedValue is null)' : 'Required option (Yes/No) was not selected'
                    });
                }
            }
        }

        // 4. Required Autocomplete / Combobox inputs
        const autocompletes = Array.from(document.querySelectorAll('input[role="combobox"], input.ashby-application-form-input-autocomplete'));
        for (const ac of autocompletes) {
            const entry = ac.closest('.ashby-application-form-field-entry, [class*="_fieldEntry_"]');
            const isReq = entry ? (entry.querySelector('._required_') || entry.innerText.includes('*')) : false;
            if (isReq && !ac.value.trim()) {
                const label = entry ? (entry.querySelector('.ashby-application-form-question-title')?.innerText || '') : 'Location';
                issues.push({
                    id: ac.id || '',
                    name: ac.name || '',
                    label: label.trim().replace(/\\*$/, '').trim(),
                    valMsg: 'Required location/autocomplete option was not selected'
                });
            }
        }

        return issues;
    }""")


# ── Listing scraper (salary / locations) ─────────────────────────────────────

async def scrape_listing_meta(page: Page) -> tuple[str | None, list[str]]:
    text = await page.locator("body").inner_text()
    salary = scrape_salary(text)

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


def _write_report(job_url: str, status: str, fields_filled: int, fields_total: int, validation_errors: list = None):
    _report.update({
        "job_url": job_url,
        "started": _report.get("started", datetime.datetime.now().isoformat()),
        "final": status,
        "fields_filled": fields_filled,
        "fields_total": fields_total,
        "validation_errors": validation_errors or [],
    })
    report_path = ARTIFACTS / "run_report_ashby.json"
    report_path.write_text(json.dumps(_report, indent=2, ensure_ascii=False))
    print(f"  [report] Written → {report_path}")


def _mark_applied(job_url: str):
    """Mark this job Applied in data/jobs_tracker.csv after a confirmed successful submit."""
    try:
        from tracker_db import mark_applied_by_url
        mark_applied_by_url(job_url)
    except Exception as e:
        print(f"  [tracker] mark-applied failed (non-fatal): {e}")


# ── Main applicator ───────────────────────────────────────────────────────────

async def main(job_url: str, headed: bool = False, no_submit: bool = False):
    _report["started"] = datetime.datetime.now().isoformat()

    print(f"[AB] Job URL    : {job_url}")
    print(f"[AB] Résumé     : {RESUME_PATH or '(not found)'}")
    print(f"[AB] DeepSeek   : {'enabled' if DEEPSEEK_KEY else 'DISABLED'}")
    print(f"[AB] Mode       : {'DRY-RUN (--no-submit)' if no_submit else ('HEADED' if headed else 'HEADLESS')}")
    print()

    async with async_playwright() as p:
        browser, context, page = await launch_browser(
            p,
            headed,
            extra_headers={
                "Accept-Language": "en-US,en;q=0.9",
            },
        )

        try:
            print("[AB] Navigating to application form …")
            await page.goto(job_url, referer="https://www.google.com/", timeout=45000, wait_until="networkidle")
            # Initial human-like review pause before starting to fill
            await page.wait_for_timeout(random.randint(2000, 3500))

            # Grab listing salary / locations before the form takes over
            salary, locations = await scrape_listing_meta(page)
            print(f"[AB] Scraped salary: {salary}  |  locations: {locations}")
            runtime_profile = build_runtime_profile(salary, locations)

            # Prevent sticky navigation header from intercepting clicks or overlaying fields
            await page.evaluate("""() => {
                const header = document.querySelector('.ashby-job-posting-header, [class*="_navRoot_"]');
                if (header) {
                    header.style.position = 'static';
                }
            }""")

            # Fill repeatable education history if present
            await fill_ashby_education(page)

            MAX_PASSES = 3
            filled = 0
            file_uploaded_count = 0
            _seen_once: set = set()
            _DEDUP_KWS = ("linkedin", "website", "github")
            all_fields: list[dict] = []
            previously_seen_labels: set = set()

            for pass_num in range(1, MAX_PASSES + 1):
                fields = await scan_fields(page)
                all_fields = fields

                if pass_num == 1:
                    print(f"[AB] Pass 1: Scanned {len(fields)} fields")
                    to_fill = fields
                    previously_seen_labels = {f.get("label", "") for f in fields}

                    # File upload: route files deterministically on pass 1
                    file_fields = [f for f in fields if f.get("type") == "file"]
                    for ff in file_fields:
                        success = await ab_exec_file(page, ff)
                        if success:
                            file_uploaded_count += 1
                        await page.wait_for_timeout(random.randint(500, 1000))
                else:
                    # Run pre-submit validation audit to detect dropped, invalid, or unfilled required fields
                    invalids = await audit_form_validation(page)
                    current_labels = {f.get("label", "") for f in fields}
                    new_fields = [f for f in fields if f.get("label", "") not in previously_seen_labels]

                    invalid_labels = {inv.get("label", "").lower() for inv in invalids}
                    invalid_ids = {inv.get("id", "") for inv in invalids if inv.get("id")}
                    invalid_names = {inv.get("name", "") for inv in invalids if inv.get("name")}

                    to_fill = [
                        f for f in fields
                        if f.get("label", "").lower() in invalid_labels
                        or (f.get("id") and f.get("id") in invalid_ids)
                        or (f.get("name") and f.get("name") in invalid_names)
                        or f in new_fields
                        or (f.get("required") and not f.get("value"))
                    ]

                    if not to_fill and not invalids:
                        print(f"\n[AB] Pass {pass_num}: ✓ Form complete — 0 invalid required fields remaining.")
                        break

                    print(f"\n[AB] Pass {pass_num}: ⚠ Found {len(to_fill)} unfilled/invalid field(s) — re-evaluating with DeepSeek …")
                    previously_seen_labels.update(current_labels)

                fillable = [f for f in to_fill if f.get("type") != "file"]
                if not fillable:
                    break

                if not DEEPSEEK_KEY:
                    raise RuntimeError("DEEPSEEK_API_KEY is required in data/.env.")

                print(f"[AB] Sending {len(fillable)} field(s) to DeepSeek …")
                answers = await deepseek_fill_page(fillable, profile_override=runtime_profile)
                print(f"[AB] DeepSeek returned {len(answers)} answer(s)")
                answer_map = {a["index"]: a["value"] for a in answers}

                print(f"[AB] Filling {len(answer_map)} field(s) …")
                for field in fillable:
                    val = answer_map.get(field["index"])
                    if val is None or val == "":
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
                    await page.wait_for_timeout(random.randint(450, 900))

                # Allow Ashby's background GraphQL mutation worker to settle
                await page.wait_for_timeout(random.randint(2500, 3500))

            total_filled = filled + file_uploaded_count
            print(f"\n[AB] Filled {total_filled}/{len(all_fields)} fields (including {file_uploaded_count} attached file(s)).")

            # Final validation audit
            invalids = await audit_form_validation(page)
            if invalids:
                print(f"\n[AUDIT] ⚠ {len(invalids)} invalid required field(s) remaining after passes:")
                for inv in invalids:
                    print(f"    ✗ [{inv.get('name') or inv.get('id')}] {inv.get('label')!r}: {inv.get('valMsg')}")
            else:
                print("\n[AUDIT] ✓ All required fields passed validation (0 invalid inputs)!")

            ss_path = ARTIFACTS / "ab_before_submit.png"
            await page.screenshot(path=str(ss_path), full_page=True)
            print(f"[AB] Screenshot saved → {ss_path.name}")

            if no_submit:
                print("\n" + "=" * 60)
                print("  DRY-RUN COMPLETE (--no-submit active)")
                print(f"  All {total_filled} fields filled and validated.")
                print(f"  Screenshot saved to {ss_path.name}")
                print("  Application was NOT submitted.")
                print("=" * 60)
                _write_report(job_url, "dry_run_success", total_filled, len(fields), invalids)
                return

            _write_report(job_url, "ready_to_submit", total_filled, len(fields), invalids)

            if headed:
                print("\n" + "=" * 60)
                print("  FORM READY FOR MANUAL SUBMIT — BOT HAS STOPPED")
                print("  1. Inspect the pre-filled fields in the browser window.")
                print("  2. Click 'Submit Application' yourself in the browser.")
                print("  3. Solve any CAPTCHA if prompted.")
                print("  4. Once submitted, press [Enter] here to confirm and update tracker.")
                print("     Type 's' (or 'skip') to skip/snooze this job for 3 days.")
                print("  Press Ctrl+C to CANCEL without updating tracker.")
                print("=" * 60)
                try:
                    user_input = await asyncio.to_thread(input, "  Action [Enter=submit / s=skip 3d / q=cancel]: ")
                except (KeyboardInterrupt, EOFError):
                    print("[AB] Cancelled by user — application not marked as applied.")
                    _write_report(job_url, "cancelled_by_user", filled, len(fields), invalids)
                    return

                user_input = user_input.strip().lower()
                m = re.match(r"^s(?:kip)?\s*(\d+)?$", user_input)
                if m or user_input in ("snooze", "3"):
                    days = int(m.group(1)) if (m and m.group(1)) else 3
                    snooze_until = snooze_job_by_url(job_url, days=days, reason=f"Skipped by user for {days} days")
                    print(f"\n[AB] ⏸ Job snoozed for {days} days (until {snooze_until}).")
                    _write_report(job_url, f"snoozed_{days}_days", filled, len(fields), invalids)
                    sys.exit(2)
                elif user_input in ("q", "cancel", "abort"):
                    print("[AB] Cancelled by user — application not marked as applied.")
                    _write_report(job_url, "cancelled_by_user", filled, len(fields), invalids)
                    return

                await page.wait_for_timeout(1000)
                ss2 = ARTIFACTS / "ab_after_submit.png"
                try:
                    await page.screenshot(path=str(ss2), full_page=True)
                    print(f"[AB] Post-submit screenshot saved → {ss2.name}")
                except Exception:
                    pass

                print(f"[AB] ✓ Confirmed by user! Marking job Applied in tracker …")
                _write_report(job_url, "submitted_manual", filled, len(fields), invalids)
                _mark_applied(job_url)
            else:
                print("[AB] Headless mode — run with --show to review and manually submit.")
                _write_report(job_url, "ready_to_submit_headless", filled, len(fields), invalids)

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
    parser = argparse.ArgumentParser(
        description="Ashby Application Bot",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("job_url", help="Ashby application URL (jobs.ashbyhq.com/{company}/{uuid}/application)")
    parser.add_argument("--show", action="store_true", help="Show Chrome window (required to submit)")
    parser.add_argument("--no-submit", action="store_true", help="Fill form and verify without submitting (dry-run)")
    args = parser.parse_args()
    asyncio.run(main(args.job_url, headed=args.show, no_submit=args.no_submit))
