"""
app_greenhouse.py  —  Greenhouse Application Bot
==================================================
Fills Greenhouse job applications (job-boards.greenhouse.io / boards.greenhouse.io
and embedded ?gh_jid= company pages).

Architecture:
  1. Resolve the canonical Greenhouse apply URL (direct or via gh_jid embed detection)
  2. Navigate to the apply form. Some "Job Board with company branding" setups (e.g.
     Stripe) 302 the canonical URL back to a company careers page whose only CTA is
     "Quick Apply with MyGreenhouse" (a candidate-login wall, not a real form). If no
     form is found: retry the original un-canonicalized URL, then fall back to the
     legacy unauthenticated embed endpoint (boards.greenhouse.io/embed/job_app?for=
     {token}&token={job_id}), which serves the raw form directly.
  3. If the real form still isn't visible, click a plain Apply button — NEVER a Quick
     Apply / MyGreenhouse / Autofill CTA. Then detect and enter a Greenhouse iframe if present.
  4. Scan ALL visible fields: text inputs, selects, radios, checkboxes, file upload
  5. Primary:  send batch to DeepSeek → [{index, value}]   (requires DEEPSEEK_API_KEY)
     Fallback: label-matching rules from app_common.py     (no API needed)
  6. Execute answers field-by-field — pauses at the Submit button for user review
  7. NEVER auto-submits — press [Enter] in terminal to submit after reviewing

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 USAGE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  # Headless (default):
  python3 src/app_greenhouse.py "JOB_URL"

  # Visible Chrome window for review:
  python3 src/app_greenhouse.py "JOB_URL" --show

  # Log to file (Mac/Linux):
  python3 -u src/app_greenhouse.py "JOB_URL" > run_gh.txt 2>&1 &
  tail -f run_gh.txt

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 FLAGS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  JOB_URL   Greenhouse listing or company page URL (positional, required)
  --show    Launch a visible Chrome window (recommended for first-time testing)

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
import sys
from pathlib import Path
from urllib.parse import urlparse, parse_qsl

from playwright.async_api import async_playwright, Page

# Shared infrastructure
from app_common import (
    RESUME_PATH, DEEPSEEK_KEY,
    PROFILE_SUMMARY, EDU,
    deepseek_fill_page, deepseek_pick_option, label_match, pick_decline,
    ARTIFACTS_DIR,
    launch_browser,
    snooze_job_by_url,
)

ARTIFACTS = ARTIFACTS_DIR
ARTIFACTS.mkdir(exist_ok=True)

# Module-level frame reference — set in main() before scan_fields/executors run
_frame = None


def _tgt(page):
    """Return _frame if an iframe was detected, otherwise fall back to page."""
    return _frame if _frame is not None else page


# ── Greenhouse URL helpers ────────────────────────────────────────────────────

def parse_greenhouse_token_and_job(url: str) -> tuple[str | None, str | None]:
    """Extract (board_token, job_id) from a Greenhouse URL (direct or embedded)."""
    parsed = urlparse(url)
    netloc = parsed.netloc.lower()
    path   = parsed.path
    qs     = dict(parse_qsl(parsed.query))

    # Direct: job-boards.greenhouse.io/{token}/jobs/{id}
    #         boards.greenhouse.io/{token}/jobs/{id}
    if "greenhouse.io" in netloc:
        parts = [p for p in path.split("/") if p]
        # Expect: token / "jobs" / id  (possibly with locale prefix like "en")
        for i, part in enumerate(parts):
            if part == "jobs" and i + 1 < len(parts):
                board_token = parts[i - 1] if i > 0 else None
                job_id = parts[i + 1]
                return board_token, job_id
        # Fallback: last numeric segment
        numeric = next((p for p in reversed(parts) if p.isdigit()), None)
        token   = parts[0] if parts else None
        return token, numeric

    # Embedded: ?gh_jid=123 (any company domain)
    if "gh_jid" in qs:
        job_id = qs["gh_jid"]
        # Derive board token from domain (works for most; custom mappings for known overrides)
        KNOWN = {
            "seatgeek.com": "seatgeek",
            "www.seatgeek.com": "seatgeek",
            "braincorp.com": "braincorporation",
            "study.com": "studycareers",
            "www.study.com": "studycareers",
        }
        if netloc in KNOWN:
            board_token = KNOWN[netloc]
        else:
            domain_parts = netloc.split(".")
            board_token = domain_parts[-2] if len(domain_parts) >= 2 else netloc
        return board_token, job_id

    return None, None


def canonical_apply_url(url: str) -> str | None:
    """Given any Greenhouse-related URL, return the canonical apply form URL.

    If the URL is already hosted on greenhouse.io, canonicalize it to
    https://job-boards.greenhouse.io/{token}/jobs/{job_id}.
    For embedded company pages (?gh_jid=...), return the original page URL directly
    so the company's backend can initialize the embedded iframe with its signed
    validity tokens without guessing tenant board names.
    """
    parsed = urlparse(url)
    if "greenhouse.io" in parsed.netloc.lower():
        token, job_id = parse_greenhouse_token_and_job(url)
        if token and job_id:
            return f"https://job-boards.greenhouse.io/{token}/jobs/{job_id}"
    return url  # fall back to original


def is_greenhouse_url(url: str) -> bool:
    netloc = urlparse(url).netloc.lower()
    qs     = dict(parse_qsl(urlparse(url).query))
    return "greenhouse.io" in netloc or "gh_jid" in qs

# ── Field scanner ─────────────────────────────────────────────────────────────

async def scan_fields(page: Page) -> list[dict]:
    """Scan the Greenhouse apply form and return structured field descriptors.

    Greenhouse forms use standard HTML — no custom ARIA widgets.
    We tag each element with a data-gh-idx attribute for stable addressing.
    Uses _frame (iframe) if one was detected, otherwise falls back to page.
    """
    target = _tgt(page)
    fields = await target.evaluate(r"""() => {
        // Inject stable index attribute
        let idx = 0;
        const fields = [];

        // Helper: get visible label text for an input
        function getLabel(el) {
            // 1. <label for="id">
            if (el.id) {
                const lbl = document.querySelector('label[for="' + el.id + '"]');
                if (lbl) return lbl.innerText.trim();
            }
            // 2. aria-label
            if (el.getAttribute('aria-label')) return el.getAttribute('aria-label').trim();
            // 3. placeholder
            if (el.placeholder) return el.placeholder.trim();
            // 4. walk up to find a label sibling or ancestor text
            let node = el.parentElement;
            for (let i = 0; i < 5 && node; i++) {
                const lbl = node.querySelector('label');
                if (lbl && lbl.innerText.trim()) return lbl.innerText.trim();
                node = node.parentElement;
            }
            return el.name || el.id || '';
        }

        // Helper: get section/group heading
        function getSection(el) {
            let node = el.parentElement;
            for (let i = 0; i < 10 && node; i++) {
                const h = node.querySelector('h1,h2,h3,h4,fieldset legend');
                if (h && h.innerText.trim()) return h.innerText.trim();
                node = node.parentElement;
            }
            return '';
        }

        // Helper: is element visible?
        function isVisible(el) {
            const rect = el.getBoundingClientRect();
            return rect.width > 0 && rect.height > 0 &&
                   getComputedStyle(el).display !== 'none' &&
                   getComputedStyle(el).visibility !== 'hidden';
        }

        function isRequired(el, label) {
            if (el.required || el.getAttribute('aria-required') === 'true') return true;
            if (label && (label.includes('*') || /required/i.test(label))) return true;
            const p = el.closest('.field, .form-field, [class*="field"]');
            if (p && (p.querySelector('.required, [class*="asterisk"]') || p.innerText.includes('*'))) return true;
            return false;
        }

        // ── Text / email / tel / number / url / textarea ──────────────────
        const textEls = document.querySelectorAll(
            'input[type="text"], input[type="email"], input[type="tel"],' +
            'input[type="number"], input[type="url"], input:not([type]), textarea'
        );
        for (const el of textEls) {
            if (!isVisible(el)) continue;
            // Skip hidden / submit / button inputs
            if (['hidden','submit','button','file','checkbox','radio'].includes(el.type)) continue;
            // Skip the hidden validation sentinel injected by GH's React-Select wrapper
            // (class contains "requiredInput" and tabindex="-1" with aria-hidden="true")
            if (el.getAttribute('aria-hidden') === 'true' && el.getAttribute('tabindex') === '-1') continue;
            const isSelectInput = el.classList.contains('select__input') ||
                                  el.classList.contains('sf__input') ||
                                  el.classList.contains('df__input') ||
                                  el.getAttribute('role') === 'combobox' ||
                                  (el.id && (el.id.includes('school') || el.id.includes('discipline')));
            let val = el.value || '';
            if (isSelectInput) {
                const ctl = el.closest('[class*="select__control"], [class*="control"]');
                if (ctl) {
                    const hasVal = ctl.querySelector('[class*="has-value"], [class*="single-value"], [class*="singleValue"]') !== null;
                    const txt = ctl.innerText.trim();
                    if (hasVal || (txt && !txt.toLowerCase().startsWith('select') && txt !== '-- select --')) {
                        val = txt;
                    }
                }
            }
            const lbl = getLabel(el);
            el.dataset.ghIdx = idx;
            fields.push({
                index:        idx++,
                tag:          el.tagName.toLowerCase(),
                type:         el.type || 'text',
                id:           el.id || '',
                name:         el.name || '',
                label:        lbl,
                required:     isRequired(el, lbl),
                section:      getSection(el),
                value:        val,
                options:      [],
                isSelectInput: isSelectInput,
            });
        }

        // ── Native <select> ───────────────────────────────────────────────
        const selects = document.querySelectorAll('select');
        for (const el of selects) {
            if (!isVisible(el)) continue;
            el.dataset.ghIdx = idx;
            const opts = Array.from(el.options)
                .map(o => o.text.trim())
                .filter(t => t && t.toLowerCase() !== 'select...' && t.toLowerCase() !== '-- select --');
            const lbl = getLabel(el);
            let val = el.options[el.selectedIndex]?.text.trim() || '';
            if (val.toLowerCase().startsWith('select') || val === '-- select --') val = '';
            fields.push({
                index:   idx++,
                tag:     'select',
                type:    'select-one',
                id:      el.id || '',
                name:    el.name || '',
                label:   lbl,
                required: isRequired(el, lbl),
                section: getSection(el),
                value:   val,
                options: opts,
            });
        }

        // ── Radio groups ──────────────────────────────────────────────────
        const radioGroups = {};
        const radios = document.querySelectorAll('input[type="radio"]');
        for (const el of radios) {
            if (!isVisible(el)) continue;
            const key = el.name || el.id || String(idx);
            if (!radioGroups[key]) {
                radioGroups[key] = { el, texts: [], values: [], checkedVal: '' };
            }
            const lbl = document.querySelector('label[for="' + el.id + '"]');
            const text = lbl ? lbl.innerText.trim() : el.value;
            radioGroups[key].texts.push(text);
            radioGroups[key].values.push(el.value);
            if (el.checked) {
                radioGroups[key].checkedVal = text || el.value;
            }
        }
        for (const [key, grp] of Object.entries(radioGroups)) {
            grp.el.dataset.ghIdx = idx;
            const lbl = getLabel(grp.el);
            fields.push({
                index:       idx++,
                tag:         'input',
                type:        'radio',
                id:          grp.el.id || '',
                name:        grp.el.name || key,
                label:       lbl,
                required:    isRequired(grp.el, lbl),
                section:     getSection(grp.el),
                value:       grp.checkedVal || '',
                options:     grp.texts,
                radioValues: grp.values,
                role:        'radio',
            });
        }

        // ── Checkboxes (individual — not radio-style) ──────────────────
        const checkboxEls = document.querySelectorAll('input[type="checkbox"]');
        for (const el of checkboxEls) {
            if (!isVisible(el)) continue;
            if (el.getAttribute('aria-hidden') === 'true' && el.getAttribute('tabindex') === '-1') continue;
            el.dataset.ghIdx = idx;
            const lbl = getLabel(el);
            fields.push({
                index:   idx++,
                tag:     'input',
                type:    'checkbox',
                id:      el.id || '',
                name:    el.name || '',
                label:   lbl,
                required: isRequired(el, lbl),
                section: getSection(el),
                value:   el.checked ? 'true' : 'false',
                options: [],
                role:    'checkbox',
            });
        }

        return fields;
    }""")
    return fields


# ── Field executors (Greenhouse — standard HTML, no Workday custom widgets) ──

async def gh_exec_text(page: Page, field: dict, value: str, target=None):
    target = target or _tgt(page)
    idx = field["index"]
    try:
        el = target.locator(f"[data-gh-idx='{idx}']").first
        await el.scroll_into_view_if_needed(timeout=5000)
        # Dismiss any open react-select popup overlay from previous field
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        try:
            await el.click(click_count=3, timeout=3000)
        except Exception:
            await el.click(click_count=3, timeout=3000, force=True)
        await el.fill(value)
        print(f"    ✓ text  [{idx}] {field['label']!r} = {value!r}")
    except Exception as e:
        # JS fallback — pass value as argument to avoid f-string injection
        await target.evaluate(
            """([idx, value]) => {
                const el = document.querySelector('[data-gh-idx="' + idx + '"]');
                if (!el) return;
                el.focus();
                const setter = Object.getOwnPropertyDescriptor(
                    el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype,
                    'value')?.set;
                if (setter) setter.call(el, value);
                else el.value = value;
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
            }""",
            [idx, value],
        )
        print(f"    ✓ text  [{idx}] {field['label']!r} = {value!r} (JS fallback: {e})")


def match_numeric_range(val_str: str, options: list[str]) -> str | None:
    """Match a numeric or float value (e.g. '4.0', '3.65', '3') to options containing
    brackets, intervals, or threshold descriptors (e.g. '3.75 - 4', '4+', 'Below 2', '3.5 and above').
    """
    if not val_str or not options:
        return None
    m = re.search(r'(\d+(?:\.\d+)?)', str(val_str))
    if not m:
        return None
    val = float(m.group(1))

    matches = []
    for o in options:
        o_clean = o.strip()
        # 1. Closed Range: 'low - high' or 'low to high'
        m_range = re.search(r'(\d+(?:\.\d+)?)\s*(?:-|to)\s*(\d+(?:\.\d+)?)', o_clean, re.I)
        if m_range:
            low, high = float(m_range.group(1)), float(m_range.group(2))
            if low <= val <= high:
                matches.append((o, high - low, 1))
                continue

        # 2. Upper threshold: '4+' or '3.5+' or '3.5 and above' or 'above 3.5' or '>= 3.5'
        m_plus = re.search(r'(?:above|over|greater than|>=?)\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*(?:\+|and above|and higher|or above|or more)', o_clean, re.I)
        if m_plus:
            thresh = float(m_plus.group(1) or m_plus.group(2))
            if val >= thresh:
                matches.append((o, abs(val - thresh), 2))
                continue

        # 3. Lower threshold: 'Below 2', '< 3.0', 'under 2.5'
        m_below = re.search(r'(?:below|under|less than|<=?)\s*(\d+(?:\.\d+)?)', o_clean, re.I)
        if m_below:
            thresh = float(m_below.group(1))
            if val < thresh:
                matches.append((o, abs(thresh - val) + 10.0, 3))
                continue

        # 4. Exact single number: '4' or '4.0'
        m_single = re.match(r'^\s*(\d+(?:\.\d+)?)\s*$', o_clean)
        if m_single:
            if float(m_single.group(1)) == val:
                matches.append((o, 0.0, 0))
                continue

    if matches:
        matches.sort(key=lambda x: (x[2], x[1]))
        return matches[0][0]
    return None


async def gh_exec_select(page: Page, field: dict, value: str, target=None):
    target = target or _tgt(page)
    idx  = field["index"]
    opts = field.get("options", [])
    label = field.get("label", "")
    match = next((o for o in opts if o.strip().lower() == value.strip().lower()), None)
    if not match and opts and DEEPSEEK_KEY:
        match = await deepseek_pick_option(label, opts, context=f"Initial candidate target value: {value}")
    if not match and opts:
        match = match_numeric_range(value, opts)
    if not match:
        match = value
    try:
        el = target.locator(f"select[data-gh-idx='{idx}']").first
        await el.select_option(label=match, timeout=5000)
        print(f"    ✓ sel   [{idx}] {field['label']!r} = {match!r}")
    except Exception as e:
        print(f"    ~ sel   [{idx}] {field['label']!r}: {e}")


async def gh_exec_radio(page: Page, field: dict, value: str, target=None):
    target = target or _tgt(page)
    idx   = field["index"]
    opts  = field.get("options", [])
    rvals = field.get("radioValues", [])
    match = next((o for o in opts if o.strip().lower() == value.strip().lower()), None) or (opts[0] if opts else value)
    match_idx = opts.index(match) if match in opts else 0
    rval  = rvals[match_idx] if match_idx < len(rvals) else match

    name  = field.get("name", "")
    clicked = False
    if name and rval:
        # Pass name and rval as arguments to avoid CSS/JS injection
        clicked = await target.evaluate(
            "([name, rval]) => { const r = document.querySelector('input[type=\"radio\"][name=\"' + name + '\"][value=\"' + rval + '\"]'); if (r) { r.click(); return true; } return false; }",
            [name, rval],
        )
    if not clicked:
        # Try by label text using Playwright's filter (no string injection)
        try:
            lbl_loc = target.locator("label").filter(has_text=match[:50])
            count = await lbl_loc.count()
            for i in range(count):
                lbl = lbl_loc.nth(i)
                html_for = await lbl.get_attribute("for")
                if html_for:
                    radio = target.locator(f"input[type='radio'][id='{html_for}']")
                    if await radio.count():
                        await radio.click()
                        clicked = True
                        break
        except Exception:
            pass
    await target.wait_for_timeout(300)
    print(f"    {'✓' if clicked else '~'} radio [{idx}] {field['label']!r} = {match!r}")


async def gh_exec_checkbox(page: Page, field: dict, value: str, target=None):
    target = target or _tgt(page)
    want = value.lower() in ("true","yes","on","checked","1")
    idx  = field["index"]
    fid  = field.get("id","")

    current_checked = await target.evaluate(
        "(idx) => { const el = document.querySelector('[data-gh-idx=\"' + idx + '\"]'); return el ? el.checked : null; }",
        idx,
    )
    if (want and current_checked) or (not want and not current_checked):
        print(f"    ✓ check [{idx}] {field['label']!r} = {value!r} (already)")
        return

    clicked = False
    if fid:
        try:
            lbl = target.locator(f"label[for='{fid}']").first
            if await lbl.count():
                await lbl.click(timeout=3000)
                clicked = True
        except Exception:
            pass
    if not clicked:
        try:
            el = target.locator(f"[data-gh-idx='{idx}']").first
            await el.click(force=True, timeout=3000)
            clicked = True
        except Exception:
            pass
    await target.wait_for_timeout(200)
    print(f"    {'✓' if clicked else '~'} check [{idx}] {field['label']!r} = {value!r}")


async def gh_exec_file(page: Page, resume_path: str, target=None):
    """Upload résumé via the file input."""
    target = target or _tgt(page)
    if not resume_path or not Path(resume_path).exists():
        print(f"    ~ file  résumé not found: {resume_path!r}")
        return
    try:
        file_input = target.locator('input[type="file"][name="resume"], input[type="file"]').first
        if await file_input.count() == 0:
            print("  [file] no file input found on page")
        else:
            await file_input.set_input_files(resume_path)
            print(f"    ✓ file  Résumé uploaded: {Path(resume_path).name}")
    except Exception as e:
        print(f"    ~ file  Upload failed: {e}")


async def gh_exec_react_select(page: Page, field: dict, value: str, target=None, avoid: set | None = None) -> str | None:
    """Fill a Greenhouse React-Select (select__input) combobox.

    Strategy: click the input to open the dropdown, type the search value,
    wait for the menu-list to appear, then click the best-matching option.
    Falls back to plain text fill if no dropdown appears.

    avoid: option texts to skip when falling back to "first real option" (used for
    ranked-preference field groups like 1st/2nd/3rd choice, so each pick differs).
    Returns the option text actually picked, or None if nothing was selected.
    """
    target = target or _tgt(page)
    idx   = field["index"]
    fid   = field.get("id", "")
    label = field.get("label", "")
    try:
        # Locate by id if available, otherwise by data-gh-idx
        if fid:
            # CSS ID selectors can't start with a digit — use attribute selector instead
            el = target.locator(f"input[id='{fid}']").first
        else:
            el = target.locator(f"[data-gh-idx='{idx}']").first
        await el.scroll_into_view_if_needed(timeout=5000)
        await el.click(timeout=5000)
        await target.wait_for_timeout(400)
        # For decline/prefer-not values and the "pick any real option" sentinel, don't type —
        # clicking alone opens the full unfiltered list. Typing an unmatchable string (a
        # decline phrase, or the sentinel for fields where no rule-based value is knowable
        # ahead of time — see app_common.py's engineering-preference handler) would filter
        # react-select's live list down to zero results and the menu never opens.
        _DECLINE_VALS = ("do not wish", "prefer not", "decline", "choose not")
        _is_decline = any(k in value.lower() for k in _DECLINE_VALS)
        _is_pick_any = value == "__PICK_FIRST_OPTION__"
        if not _is_decline and not _is_pick_any:
            await el.fill(value)
            await target.wait_for_timeout(700)
        # Wait for the dropdown menu
        menu = target.locator("div.select__menu:visible, div.select__menu-list:visible").first
        try:
            await menu.wait_for(state="visible", timeout=3000)
            # Get all visible option texts
            # Scope to the VISIBLE menu only — when "Add another" duplicates a field (e.g. a
            # second School/Degree/Discipline block), multiple identical react-select menu
            # containers exist in the DOM simultaneously (one open, others hidden/stale).
            # An unscoped querySelector can grab the wrong one, silently reading a different
            # field's options. Always pick the menu whose bounding box has non-zero height.
            _READ_VISIBLE_MENU_JS = """() => {
                const menus = Array.from(document.querySelectorAll('div.select__menu, div.select__menu-list, [class*="select__menu-list"]'));
                const visible = menus.find(m => m.getBoundingClientRect().height > 0);
                if (!visible) return [];
                return Array.from(visible.querySelectorAll('[class*=option]')).map(o => o.innerText.trim());
            }"""
            opts_text = await target.evaluate(_READ_VISIBLE_MENU_JS)
            # School-name fields are server-searched against a huge list; a full institution
            # name (e.g. "University of California, San Diego") often doesn't match the
            # board's exact stored phrasing (e.g. "University of California - San Diego" —
            # different punctuation/word order). Retry with progressively shorter suffixes
            # of the value (last 2 words, then last word — typically the distinguishing
            # campus/city name) before giving up on a targeted search entirely.
            if (not opts_text or opts_text == ["No options"]) and not _is_decline and not _is_pick_any \
                    and label_match(label, "school", "institution", "university", "college"):
                words = [w for w in re.split(r'[, -]+', value) if w]
                for n in (2, 1):
                    if len(words) <= n:
                        continue
                    suffix = " ".join(words[-n:])
                    await el.fill(suffix)
                    await target.wait_for_timeout(700)
                    opts_text = await target.evaluate(_READ_VISIBLE_MENU_JS)
                    if opts_text and opts_text != ["No options"]:
                        break
            # Discipline / major fields: if exact phrase had no options, try standard keywords
            if (not opts_text or opts_text == ["No options"]) and not _is_decline and not _is_pick_any \
                    and label_match(label, "discipline", "major", "field of study"):
                if any(w in value.lower() for w in ["computer", "software", "cs", "cse"]):
                    for term in ["Computer Science", "Computer Engineering"]:
                        await el.fill(term)
                        await target.wait_for_timeout(700)
                        opts_text = await target.evaluate(_READ_VISIBLE_MENU_JS)
                        if opts_text and opts_text != ["No options"]:
                            break
                elif any(w in value.lower() for w in ["data", "stat"]):
                    for term in ["Data Science", "Statistics"]:
                        await el.fill(term)
                        await target.wait_for_timeout(700)
                        opts_text = await target.evaluate(_READ_VISIBLE_MENU_JS)
                        if opts_text and opts_text != ["No options"]:
                            break
            # Some Greenhouse react-selects hold a static, pre-loaded option list (e.g. GPA
            # buckets like "3.75+", degree names, date ranges) rather than a server-side
            # search. Typing an exact value against these can filter to "No options" even
            # though a matching-ish option exists untyped. If typing produced nothing (or
            # the "No options" placeholder), clear the input and re-open to read the full
            # untyped list before giving up.
            if (not opts_text or opts_text == ["No options"]) and not _is_decline and not _is_pick_any:
                await el.fill("")
                await target.wait_for_timeout(500)
                opts_text = await target.evaluate(_READ_VISIBLE_MENU_JS)
            # Pick best match from visible options (excluding already chosen options in avoid set)
            available_opts = [o for o in opts_text if o not in (avoid or set())] or opts_text
            best = None
            if _is_decline:
                best = pick_decline(available_opts) or next((o for o in available_opts if o.strip().lower() == value.strip().lower()), None)

            # 1. Exact string match (fast path)
            if not best:
                best = next((o for o in available_opts if o.strip().lower() == value.strip().lower()), None)

            # 2. Invoke DeepSeek on the live menu options
            if not best and available_opts and DEEPSEEK_KEY:
                avoid_ctx = f" (Do not choose any of these already picked options: {list(avoid)})" if avoid else ""
                context = f"Candidate target value: {value}.{avoid_ctx}"
                best = await deepseek_pick_option(label, available_opts, context=context)

            # 3. Deterministic fallbacks (if DeepSeek is offline, unkeyed, or returns None)
            val_lower = value.strip().lower()
            if not best and val_lower in ("yes", "no"):
                for o in available_opts:
                    o_l = o.strip().lower()
                    if re.match(r"^" + val_lower + r"(?:[,\s\.\-].*)?$", o_l):
                        best = o
                        break

            # Country alias match (e.g. 'United States' matching 'US', 'USA')
            if not best and any(k in label.lower() for k in ("country", "reside", "residence", "citizenship")):
                if any(us_term in val_lower for us_term in ("united states", "usa", "us")):
                    for o in available_opts:
                        if o.strip().lower() in ("us", "usa", "u.s.", "u.s.a.", "united states", "united states of america"):
                            best = o
                            break

            # Numeric range match (e.g. GPA '4.0' matching '3.75 - 4' or '4+', experience '0' matching '0 - 1')
            if not best and available_opts:
                best = match_numeric_range(value, available_opts)

            if not best and available_opts:
                val_norm = re.sub(r'[^a-z0-9]', '', value.lower())
                best = next((o for o in available_opts if re.sub(r'[^a-z0-9]', '', o.lower()) == val_norm), None)
            if best is None and available_opts and len(val_norm) > 2:
                # Substring containment match (only for non-trivial strings to prevent 'no' matching 'now')
                for o in available_opts:
                    o_norm = re.sub(r'[^a-z0-9]', '', o.lower())
                    if val_norm and (val_norm in o_norm or (len(o_norm) > 3 and o_norm in val_norm)):
                        best = o
                        break

            # If still no match and it's a GPA field, try candidate GPA from profile or pick highest tier
            if not best and available_opts and any(k in label.lower() for k in ("gpa", "grade point")):
                from app_common import EDU
                cand_gpa = next((e.get("gpa") for e in EDU if e.get("gpa")), "4.0")
                best = match_numeric_range(cand_gpa, available_opts)
                if not best:
                    best = next((o for o in reversed(available_opts) if not re.search(r'below|under|<', o, re.I)), available_opts[-1])

            # If still no match and it's a location preference, walk candidate's location priority ladder
            if not best and available_opts and any(k in label.lower() for k in ("location", "office", "city")):
                from app_common import LIBRARY
                ladder = LIBRARY.get("routing_priorities", {}).get("onsite_us_location_priority_ladder", [])
                for ladder_loc in ladder:
                    loc_terms = [t.strip().lower() for t in re.split(r'[,/()]+', ladder_loc) if len(t.strip()) > 2]
                    for o in available_opts:
                        if any(lt in o.lower() for lt in loc_terms):
                            best = o
                            break
                    if best:
                        break
            if not best and available_opts:
                if any(k in label.lower() for k in ("gpa", "grade point")):
                    best = next((o for o in reversed(available_opts) if not re.search(r'below|under|<', o, re.I)), available_opts[-1])
                else:
                    best = available_opts[0]
            if best:
                # Pure exact match on option text — no fuzzy regex or substring containment
                best_clean = best.strip()
                target_opt = None
                for opt in await menu.locator("[class*='option'], [id*='-option-']").all():
                    txt = (await opt.inner_text()).strip()
                    if txt == best_clean:
                        target_opt = opt
                        break
                if not target_opt:
                    for opt in await target.locator("div.select__option:visible, [class*='select__option']:visible").all():
                        txt = (await opt.inner_text()).strip()
                        if txt == best_clean:
                            target_opt = opt
                            break

                if target_opt:
                    await target_opt.click(timeout=3000)
                    await target.wait_for_timeout(300)
                    print(f"    ✓ rsel  [{idx}] {label!r} = {best!r}")
                    return best
            # Fallback: click first option (scoped to visible options)
            first_opt = target.locator("div.select__option:visible, [class*='select__option']:visible").first
            if await first_opt.count():
                first_text = await first_opt.inner_text()
                await first_opt.click(timeout=3000)
                print(f"    ~ rsel  [{idx}] {label!r} → first option {first_text!r}")
                return first_text
        except Exception:
            pass
        # Last resort: press Enter to accept whatever is highlighted
        await el.press("Enter")
        await target.wait_for_timeout(300)
        print(f"    ~ rsel  [{idx}] {label!r} = {value!r} (enter-confirm fallback)")
        return None
    except Exception as e:
        print(f"    ~ rsel  [{idx}] {label!r}: {e}")
        return None


async def execute_answer(page: Page, field: dict, value: str, target=None, avoid: set | None = None) -> str | None:
    """Returns the option text actually picked for react-select fields (used to build an
    `avoid` set across ranked-preference field groups); None for other field types."""
    if not value:
        return None
    target = target or _tgt(page)
    ftype = field.get("type","")
    role  = field.get("role","")
    tag   = field.get("tag","")
    try:
        if ftype == "checkbox" or role == "checkbox":
            await gh_exec_checkbox(page, field, value, target=target)
        elif ftype == "radio" or role == "radio":
            await gh_exec_radio(page, field, value, target=target)
        elif tag == "select" or ftype == "select-one":
            await gh_exec_select(page, field, value, target=target)
        elif field.get("isSelectInput"):
            return await gh_exec_react_select(page, field, value, target=target, avoid=avoid)
        else:
            await gh_exec_text(page, field, value, target=target)
    except Exception as e:
        print(f"    ~ err   [{field['index']}] {field['label']!r}: {e}")
    return None


# ── Listing scraper (salary / locations) ────────────────────────────────────

async def scrape_listing_meta(page: Page) -> tuple[str | None, list[str]]:
    """Scrape salary and locations from the listing page (if visible before apply form)."""
    text = await page.locator("body").inner_text()
    salary = None
    sal_m = re.search(
        r'\$\s*([\d,]+)\s*(?:–|-|to)\s*\$\s*([\d,]+)\s*(?:K|k|,000)?', text)
    if sal_m:
        lo = int(sal_m.group(1).replace(",",""))
        hi = int(sal_m.group(2).replace(",",""))
        if lo < 1000: lo *= 1000
        if hi < 1000: hi *= 1000
        salary = str((lo + hi) // 2)

    loc_m = re.findall(
        r'(?:Location|Office|Based in|Where)[\s:]+([A-Za-z ,/]+(?:CA|NY|TX|WA|CO|MA|IL|VA|GA|OR|FL|BC|ON))', text)
    locations = list({m.strip() for m in loc_m if m.strip()}) if loc_m else []
    return salary, locations


# ── Runtime PROFILE_SUMMARY injection ────────────────────────────────────────

def build_runtime_profile(salary: str | None, locations: list[str]) -> str:
    p = json.loads(PROFILE_SUMMARY)
    p["job_listing_salary"]    = salary
    p["job_listing_locations"] = locations
    p["today"]                 = datetime.date.today().isoformat()
    return json.dumps(p, indent=2)


# ── Artifacts report ──────────────────────────────────────────────────────────

_report: dict = {}

def _write_report(job_url: str, status: str, fields_filled: int, fields_total: int):
    _report.update({
        "job_url":       job_url,
        "started":       _report.get("started", datetime.datetime.now().isoformat()),
        "final":         status,
        "fields_filled": fields_filled,
        "fields_total":  fields_total,
    })
    report_path = ARTIFACTS / "run_report_gh.json"
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
    global _frame
    _report["started"] = datetime.datetime.now().isoformat()

    # Resolve canonical apply URL
    apply_url = canonical_apply_url(job_url)
    print(f"[GH] Job URL : {job_url}")
    print(f"[GH] Apply  : {apply_url}")
    print(f"[GH] Résumé : {RESUME_PATH or '(not found)'}")
    print(f"[GH] DeepSeek: {'enabled' if DEEPSEEK_KEY else 'DISABLED (rule-path fallback)'}")
    print()

    async with async_playwright() as p:
        browser, context, page = await launch_browser(
            p,
            headed,
            extra_headers={
                "Accept-Language": "en-US,en;q=0.9",
            },
        )

        async def _has_real_form() -> bool:
            try:
                for ctx in [page] + page.frames:
                    try:
                        if await ctx.locator('input[type="text"], input[type="email"], input[name="first_name"], input[name="email"], input[name="last_name"]').first.is_visible(timeout=1000):
                            return True
                        if (await ctx.locator('input[type="file"]').count()) > 0:
                            return True
                    except Exception:
                        pass
                return False
            except Exception:
                return False

        try:
            print("[GH] Navigating to apply form …")
            try:
                await page.goto(apply_url, timeout=25000, wait_until="domcontentloaded")
            except Exception as e:
                print(f"[GH] Initial goto notice: {e}")
            await page.wait_for_timeout(2000)

            # Some Greenhouse "Job Board with company branding" setups (e.g. Stripe) 302 the
            # canonical job-boards.greenhouse.io URL straight back to the company's own
            # careers page, whose ONLY apply CTA is "Quick Apply with MyGreenhouse" / "Autofill
            # with Greenhouse" — a candidate-login/OTP wall, not a gate in front of a real form.
            # Clicking it leads nowhere for a bot with no MyGreenhouse account, and no plain
            # form is rendered inline on these pages. If the canonical URL didn't land on a
            # real form, try (in order): the original un-canonicalized URL, then the legacy
            # unauthenticated embed endpoint, which serves the raw form directly.
            if not await _has_real_form():
                if apply_url != job_url:
                    print(f"[GH] No form at canonical URL (landed on {page.url}) "
                          f"— retrying with original URL …")
                    try:
                        await page.goto(job_url, timeout=25000, wait_until="domcontentloaded")
                    except Exception:
                        pass
                    await page.wait_for_timeout(2000)

            # Many company career pages embed the real Greenhouse form via an iframe whose
            # src carries the ACTUAL board token (and sometimes extra params like
            # validityToken) — our own domain-derived token guess can be wrong (e.g.
            # hioscar.com's real token is "oscar", not "hioscar"; careers.dat.com's is
            # "datsolutions", not "dat"). If such an iframe exists, navigate directly to its
            # src rather than guessing — this is more reliable than constructing the URL.
            if not await _has_real_form():
                try:
                    iframe_el = page.locator("iframe[src*='greenhouse']").first
                    iframe_src = await iframe_el.get_attribute("src", timeout=2000)
                except Exception:
                    iframe_src = None
                if iframe_src:
                    print(f"[GH] Found embedded Greenhouse iframe — navigating to its src: {iframe_src}")
                    try:
                        await page.goto(iframe_src, timeout=25000, wait_until="domcontentloaded")
                    except Exception:
                        pass
                    await page.wait_for_timeout(2000)

            if not await _has_real_form():
                # Check for a plain "Apply" button or link that gates the form on company pages (e.g. IXL).
                # MUST run before legacy embed endpoint guessing to avoid navigating away from valid company pages.
                # NEVER click Quick Apply / MyGreenhouse / Autofill CTAs — those lead to a login wall.
                for selector in [
                    "a[href*='/apply']",
                    "a[href*='apply']",
                    "button:has-text(\"Apply\")",
                    "a:has-text(\"Apply\")",
                    "#apply_button",
                    "a:has-text(\"Apply for this job\")",
                    "a:has-text(\"Apply Now\")",
                    "a:has-text(\"Apply now\")",
                    "button:has-text(\"Apply now\")",
                ]:
                    try:
                        btn = page.locator(selector).first
                        if await btn.is_visible(timeout=1500):
                            btn_text = ((await btn.inner_text(timeout=500)) or "").lower()
                            if any(k in btn_text for k in ("quick apply", "mygreenhouse", "autofill")):
                                continue
                            print(f"[GH] Found apply element ({selector}) — clicking …")
                            try:
                                await asyncio.gather(
                                    page.wait_for_navigation(timeout=15000, wait_until="domcontentloaded"),
                                    btn.click(),
                                    return_exceptions=True,
                                )
                            except Exception:
                                await btn.click()
                            await page.wait_for_timeout(2000)
                            if await _has_real_form():
                                break
                    except Exception:
                        pass

            if not await _has_real_form():
                token, gh_job_id = parse_greenhouse_token_and_job(job_url)
                if token and gh_job_id:
                    embed_url = f"https://boards.greenhouse.io/embed/job_app?for={token}&token={gh_job_id}"
                    print(f"[GH] No form found — trying legacy embed endpoint (guessed token): {embed_url}")
                    try:
                        await page.goto(embed_url, timeout=25000, wait_until="domcontentloaded")
                    except Exception:
                        pass
                    await page.wait_for_timeout(2000)

            # ── Step 2: Detect Greenhouse embed iframe ────────────────────────
            _frame = None
            for f in page.frames:
                if "greenhouse" in f.url and ("job_app" in f.url or "jobs" in f.url or "embed" in f.url):
                    _frame = f
                    print(f"[GH] Detected Greenhouse iframe via frames list: {f.url[:80]}…")
                    break
            if _frame is None:
                for iframe_sel in [
                    "iframe[src*='greenhouse']",
                    "#grnhse_iframe",
                    "iframe[src*='boards.greenhouse']",
                ]:
                    try:
                        el = page.locator(iframe_sel).first
                        if await el.count():
                            handle = await el.element_handle()
                            if handle:
                                _frame = await handle.content_frame()
                                if _frame:
                                    print(f"[GH] Detected Greenhouse iframe via handle ({iframe_sel})")
                                    break
                    except Exception:
                        pass

            target = _tgt(page)

            # Grab listing salary / locations before the form takes over
            salary, locations = await scrape_listing_meta(page)
            print(f"[GH] Listing salary: {salary}  |  locations: {locations}")
            runtime_profile = build_runtime_profile(salary, locations)

            # Upload résumé first (Greenhouse often puts the file input at the top)
            print("[GH] Uploading résumé …")
            await gh_exec_file(page, RESUME_PATH, target=target)
            await page.wait_for_timeout(1500)

            # Expand the education section to one block per EDU entry. Greenhouse's default
            # form shows a single School/Degree/Discipline/Start-date-year block; each click
            # of "Add another" appends an identical block. Without this, only EDU[0] (e.g. the
            # candidate's most recent/in-progress degree) ever gets a form section — earlier
            # degrees are silently dropped.
            if len(EDU) > 1:
                add_btn = target.locator(".add-another-button, button:has-text('Add another'), "
                                          "a:has-text('Add another')").first
                for _ in range(len(EDU) - 1):
                    try:
                        if not await add_btn.is_visible(timeout=1500):
                            break
                        await add_btn.click()
                        await target.wait_for_timeout(600)
                    except Exception:
                        break

            if not DEEPSEEK_KEY:
                raise RuntimeError("DEEPSEEK_API_KEY is required in data/.env.")

            MAX_PASSES = 3
            filled = 0
            _seen_once: set = set()
            _DEDUP_KWS = ("linkedin", "website")
            _group_picked: dict[str, set] = {}

            def _get_pref_group(label: str) -> str | None:
                lbl = label.lower()
                is_ranked = any(k in lbl for k in ("first", "second", "third", "1st", "2nd", "3rd", "primary", "secondary", "tertiary"))
                if not is_ranked:
                    return None
                if any(k in lbl for k in ("location", "office", "city")):
                    return "pref_group_location"
                if any(k in lbl for k in ("engineer", "profile", "discipline", "team", "track", "domain", "role")):
                    return "pref_group_engineering"
                return "pref_group_generic"

            all_scanned_fields: list[dict] = []
            previously_seen_labels: set = set()

            for pass_num in range(1, MAX_PASSES + 1):
                fields = await scan_fields(page)
                all_scanned_fields = fields

                if pass_num == 1:
                    print(f"[GH] Pass 1: Scanned {len(fields)} fields")
                    to_fill = fields
                    previously_seen_labels = {f.get("label", "") for f in fields}
                else:
                    # In subsequent passes, collect:
                    # 1. Any required field that is still empty
                    # 2. Any newly rendered field that wasn't present in the earlier scan
                    to_fill = []
                    current_labels = {f.get("label", "") for f in fields}
                    for f in fields:
                        v = f.get("value", "").strip()
                        is_empty = not v or v.lower() in ("select...", "-- select --", "select one")
                        if is_empty and f.get("required"):
                            to_fill.append(f)
                        elif f.get("label", "") not in previously_seen_labels and is_empty:
                            to_fill.append(f)

                    if not to_fill:
                        print(f"[GH] Pass {pass_num}: ✓ Form complete — 0 unfilled required fields remaining.")
                        break

                    print(f"[GH] Pass {pass_num}: Found {len(to_fill)} unfilled or newly revealed field(s) — re-evaluating with DeepSeek …")
                    previously_seen_labels.update(current_labels)

                print(f"[GH] Sending {len(to_fill)} field(s) to DeepSeek …")
                answers = await deepseek_fill_page(to_fill, profile_override=runtime_profile)
                print(f"[GH] DeepSeek returned {len(answers)} answer(s)")
                answer_map = {a["index"]: a["value"] for a in answers}

                # Override repeated education blocks on pass 1
                if pass_num == 1 and len(EDU) > 1:
                    _edu_field_labels = ("school", "institution", "university", "college",
                                         "degree", "discipline", "major", "field of study",
                                         "start date", "start year", "end date", "end year")
                    edu_group_fields = [f for f in to_fill
                                        if label_match(f.get("label", ""), *_edu_field_labels)
                                        and not label_match(f.get("label", ""), "schoolwork", "project", "initiative")
                                        and len(f.get("label", "")) < 60
                                        and f.get("tag") != "textarea"
                                        and (f.get("isSelectInput") or f.get("type") in ("number", "select", "text")
                                             or "start" in f.get("label", "").lower()
                                             or "end" in f.get("label", "").lower())]

                    def _is_school_field(f):
                        lbl = f.get("label", "")
                        if label_match(lbl, "schoolwork", "project", "initiative") or len(lbl) >= 60 or f.get("tag") == "textarea":
                            return False
                        return label_match(lbl, "school", "institution", "university", "college")

                    blocks = []
                    for f in edu_group_fields:
                        if _is_school_field(f) or not blocks:
                            blocks.append([f])
                        else:
                            blocks[-1].append(f)

                    for entry_idx, block in enumerate(blocks):
                        if entry_idx >= len(EDU):
                            break
                        entry = EDU[entry_idx]
                        for f in block:
                            if DEEPSEEK_KEY and answer_map.get(f["index"]):
                                continue
                            if f.get("tag") == "textarea":
                                continue

                            lbl = f.get("label", "")
                            if _is_school_field(f):
                                answer_map[f["index"]] = entry["institution_variants"][0]
                            elif label_match(lbl, "degree"):
                                answer_map[f["index"]] = entry.get("degree_type", "Bachelor's Degree")
                            elif label_match(lbl, "discipline", "major", "field of study"):
                                answer_map[f["index"]] = entry.get("major_search_term") or entry["major_variants"][0]
                            elif re.match(r"^\s*start\s+(date\s+)?month\s*\*?\s*$", lbl.strip(), re.I):
                                answer_map[f["index"]] = entry.get("start_month", "")
                            elif re.match(r"^\s*end\s+(date\s+)?month\s*\*?\s*$", lbl.strip(), re.I):
                                answer_map[f["index"]] = entry.get("end_month", "")
                            elif label_match(lbl, "start date", "start year"):
                                answer_map[f["index"]] = str(entry.get("start_year", ""))
                            elif label_match(lbl, "end date", "end year"):
                                answer_map[f["index"]] = str(entry.get("end_year", ""))

                print(f"[GH] Executing {len(answer_map)} field answers …")
                for field in to_fill:
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

                    group_key = _get_pref_group(field.get("label", ""))
                    avoid_set = _group_picked.setdefault(group_key, set()) if group_key else set()

                    picked = await execute_answer(page, field, val, target=target, avoid=avoid_set)
                    if group_key and picked:
                        avoid_set.add(picked)
                    filled += 1

                await page.wait_for_timeout(1000)

            print(f"\n[GH] Filled {filled}/{len(all_scanned_fields)} fields.")

            # Screenshot before pause
            ss_path = ARTIFACTS / "gh_before_submit.png"
            await page.screenshot(path=str(ss_path), full_page=True)
            print(f"[GH] Screenshot saved → {ss_path.name}")

            if no_submit:
                print("\n" + "=" * 60)
                print("  DRY-RUN COMPLETE (--no-submit active)")
                print(f"  All {filled} fields filled and validated.")
                print(f"  Screenshot saved to {ss_path.name}")
                print("  Application was NOT submitted.")
                print("=" * 60)
                _write_report(job_url, "dry_run_success", filled, len(fields))
                return

            _write_report(job_url, "ready_to_submit", filled, len(fields))

            if headed:
                print("\n" + "=" * 60)
                print("  FORM READY FOR MANUAL SUBMIT — BOT HAS STOPPED")
                print("  1. Inspect the pre-filled fields in the browser window.")
                print("  2. Click 'Submit Application' yourself in the browser.")
                print("  3. Solve any CAPTCHA / verification code if prompted.")
                print("  4. Once submitted, press [Enter] here to confirm and update tracker.")
                print("     Type 's' (or 'skip') to skip/snooze this job for 3 days.")
                print("  Press Ctrl+C to CANCEL without updating tracker.")
                print("=" * 60)
                try:
                    user_input = await asyncio.to_thread(input, "  Action [Enter=submit / s=skip 3d / q=cancel]: ")
                except (KeyboardInterrupt, EOFError):
                    print("[GH] Cancelled by user — application not marked as applied.")
                    _write_report(job_url, "cancelled_by_user", filled, len(fields))
                    return

                user_input = user_input.strip().lower()
                m = re.match(r"^s(?:kip)?\s*(\d+)?$", user_input)
                if m or user_input in ("snooze", "3"):
                    days = int(m.group(1)) if (m and m.group(1)) else 3
                    snooze_until = snooze_job_by_url(job_url, days=days, reason=f"Skipped by user for {days} days")
                    print(f"\n[GH] ⏸ Job snoozed for {days} days (until {snooze_until}).")
                    _write_report(job_url, f"snoozed_{days}_days", filled, len(fields))
                    sys.exit(2)
                elif user_input in ("q", "cancel", "abort"):
                    print("[GH] Cancelled by user — application not marked as applied.")
                    _write_report(job_url, "cancelled_by_user", filled, len(fields))
                    return

                await page.wait_for_timeout(1000)
                ss2 = ARTIFACTS / "gh_after_submit.png"
                try:
                    await page.screenshot(path=str(ss2), full_page=True)
                    print(f"[GH] Post-submit screenshot saved → {ss2.name}")
                except Exception:
                    pass

                print(f"[GH] ✓ Confirmed by user! Marking job Applied in tracker …")
                _write_report(job_url, "submitted_manual", filled, len(fields))
                _mark_applied(job_url)
            else:
                print("[GH] Headless mode — run with --show to review and manually submit.")
                _write_report(job_url, "ready_to_submit_headless", filled, len(fields))

        except Exception as e:
            ss = ARTIFACTS / "gh_error.png"
            try:
                await page.screenshot(path=str(ss), full_page=True)
            except Exception:
                pass
            print(f"\n[GH] ERROR: {e}")
            print(f"[GH] Screenshot → {ss.name}")
            _write_report(job_url, f"error: {e}", 0, 0)
            raise
        finally:
            await browser.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(
        description="Greenhouse Application Bot",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("job_url", help="Greenhouse listing URL (job-boards.greenhouse.io or company page with ?gh_jid=)")
    parser.add_argument("--show", action="store_true", help="Show Chrome window (required to submit)")
    parser.add_argument("--no-submit", action="store_true", help="Fill form and verify without submitting (dry-run)")
    args = parser.parse_args()
    asyncio.run(main(args.job_url, headed=args.show, no_submit=args.no_submit))
