"""
workday_executors.py — Workday low-level form widget executors
=============================================================
Widget-level automation functions for typing text, clicking dropdowns,
combobox search, radio buttons, checkboxes, and navigation save/continue.
"""

import re
from playwright.async_api import Page
from app_common import label_match, pick_decline, ARTIFACTS_DIR
from workday_scan import get_heading, read_validation_errors

ARTIFACTS = ARTIFACTS_DIR
ARTIFACTS.mkdir(exist_ok=True)

# ── Timing constants (milliseconds) ──────────────────────────────────────────
SETTLE_MS  = 300   # after click, before reading DOM
SEARCH_MS  = 600   # after typing into search box, before reading results
SAVE_MS    = 3000  # after save/continue, before polling heading
MEDIUM_MS  = 1500  # medium settle (e.g. after dialog save)

MONTH_NUM = {
    "january": "01", "february": "02", "march": "03", "april": "04",
    "may": "05", "june": "06", "july": "07", "august": "08",
    "september": "09", "october": "10", "november": "11", "december": "12"
}


async def locate_field(page: Page, field: dict):
    """Locate a Workday form element by data-fill-idx, id, or automation-id."""
    idx = field["index"]
    fid = field.get("id", "")
    loc = page.locator(f'[data-fill-idx="{idx}"]').first
    if not await loc.is_visible(timeout=800):
        if fid:
            loc = page.locator(f'#{fid}').first
        if not await loc.is_visible(timeout=800):
            loc = page.locator(f'[data-automation-id="{fid}"]').first
    return loc


async def exec_text(page: Page, field: dict, value: str):
    # Convert month name to number if it looks like a date section input
    label_l = field.get("label", "").lower()
    if ("month" in label_l) and str(value).lower() in MONTH_NUM:
        value = MONTH_NUM[str(value).lower()]

    idx = field["index"]
    fid = field.get("id", "")

    # Date spinbutton fields (Month/Year/Day) live inside dialog containers; using
    # scroll_into_view causes repeated re-scrolling. Fill directly via JS instead.
    is_date_part = any(k in label_l for k in ("month", "year", "day")) and len(str(value).strip()) <= 4 and str(value).strip().isdigit()
    if is_date_part:
        val_str = str(value).strip()
        if any(k in label_l for k in ("month", "day")) and len(val_str) == 1:
            val_str = val_str.zfill(2)
        await page.evaluate("(args) => { "
            "const el = document.querySelector('[data-fill-idx=\"' + args.idx + '\"]') "
            "           || document.getElementById(args.fid); "
            "if (!el) return; "
            "el.focus(); "
            "try { "
            "    const proto = el.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype; "
            "    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set; "
            "    if (setter) setter.call(el, args.value); "
            "    else el.value = args.value; "
            "} catch(e) { el.value = args.value; } "
            "el.dispatchEvent(new Event('input', {bubbles: true})); "
            "el.dispatchEvent(new Event('change', {bubbles: true})); "
            "el.dispatchEvent(new FocusEvent('blur', {bubbles: true, cancelable: true})); "
            "}", {"idx": idx, "fid": fid, "value": val_str})
        print(f"    ✓ text  [{idx}] {field['label']!r} = {val_str!r} (JS date fill)")
        return

    sel = f"[data-fill-idx='{idx}']"
    el = page.locator(sel).first
    if not await el.is_visible() and fid:
        el = page.locator(f"#{fid}").first
    if not await el.is_visible():
        print(f"    ~ text  [{idx}] {field['label']!r} is not visible (conditionally hidden) — skipping")
        return

    text_val = str(value)
    if field.get("tag") != "textarea":
        text_val = text_val.split("\n")[0].strip()

    # Smart check: if the input already contains the correct text, DO NOT retype!
    try:
        current_val = await el.input_value(timeout=1000)
    except Exception:
        current_val = await page.evaluate("(args) => { "
            "const el = document.querySelector('[data-fill-idx=\"' + args.idx + '\"]') || document.getElementById(args.fid); "
            "return el ? (el.value || '') : ''; "
            "}", {"idx": idx, "fid": fid})

    if current_val and current_val.strip().lower() == text_val.strip().lower():
        print(f"    ✓ text  [{idx}] {field['label']!r} = {current_val!r} (already correct)")
        return

    try:
        await el.scroll_into_view_if_needed(timeout=3000)
        await page.keyboard.press("Escape")   # close any open dropdown first
        await page.wait_for_timeout(SETTLE_MS)
        await el.click(click_count=3, timeout=3000)
    except Exception:
        # JS fallback: scroll + dispatch click
        await page.evaluate("(args) => { "
            "const el = document.querySelector('[data-fill-idx=\"' + args.idx + '\"]') "
            "           || document.getElementById(args.fid); "
            "if (el) { el.scrollIntoView({block:'center'}); el.click(); } "
            "}", {"idx": idx, "fid": fid})
        await page.wait_for_timeout(SETTLE_MS)

    try:
        await el.click(click_count=3, force=True, timeout=2000)
    except Exception:
        pass
    await el.fill(text_val, timeout=3000)
    try:
        await el.press("Control+A")
        await page.keyboard.type(text_val, delay=15)
    except Exception:
        pass
    # Trigger React synthetic events so React's internal state stays in sync with the DOM value
    await page.evaluate("(args) => { "
        "const el = document.querySelector('[data-fill-idx=\"' + args.idx + '\"]') "
        "           || document.getElementById(args.fid); "
        "if (!el) return; "
        "try { "
        "    const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype; "
        "    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set; "
        "    if (setter) setter.call(el, el.value); "
        "} catch(e) {} "
        "el.dispatchEvent(new Event('input', {bubbles: true})); "
        "el.dispatchEvent(new Event('change', {bubbles: true})); "
        "el.dispatchEvent(new Event('blur', {bubbles: true})); "
        "}", {"idx": idx, "fid": fid})
    try:
        await page.keyboard.press("Tab")
    except Exception:
        pass

    print(f"    ✓ text  [{idx}] {field['label']!r} = {value!r}")


async def exec_button_dropdown(page: Page, field: dict, value: str):
    label = field.get("label", "")
    # Suffix safeguard: candidate has NO suffix. If field is Suffix and profile has no suffix, skip
    if label_match(label, "suffix") and not PI.get("suffix"):
        print(f"    ~ drop  [{field['index']}] {field['label']!r} — skipping suffix (candidate profile has no suffix)")
        return
    sel = f"[data-fill-idx='{field['index']}']"
    btn = page.locator(sel).first
    fid = field.get("id", "")
    faid = field.get("auto", "")  # data-automation-id fallback (for buttons without id)
    if not await btn.is_visible():
        if fid:
            btn = page.locator(f"button#{fid}").first
        elif faid:
            btn = page.locator(f"button[data-automation-id='{faid}']").first
    # Build candidate terms (supports multiline priority lists, e.g. "How did you hear about us?")
    terms = [t.strip() for t in value.split('\n') if t.strip()]
    if label_match(label, "how did you hear", "source", "referral", "learn about"):
        hear_fallbacks = ["LinkedIn", "Internet/Online Job Posting", "Online Job Board", "Job Board", "Indeed", "Social Media", "Company Website", "Careers Site", "Search Engine", "Internet Search", "Advertisement", "Other"]
        for fb in hear_fallbacks:
            if fb not in terms:
                terms.append(fb)

    # Smart check: if the dropdown button already displays an acceptable target option, DO NOT click or re-select!
    try:
        curr_btn_text = (await btn.inner_text(timeout=1000) or "").strip()
    except Exception:
        curr_btn_text = ""
    if not curr_btn_text:
        try:
            curr_btn_text = (await btn.get_attribute("aria-label", timeout=1000) or "").strip()
        except Exception:
            curr_btn_text = ""

    def _btn_matches_terms(btn_text: str, cand_terms: list[str]) -> bool:
        b_clean = re.sub(r'[^a-z0-9]', '', btn_text.lower())
        if not b_clean or b_clean in ("selectone", "selectoption", "select"):
            return False
        for ct in cand_terms:
            c_clean = re.sub(r'[^a-z0-9]', '', ct.lower())
            if not c_clean:
                continue
            if b_clean == c_clean:
                return True
            # Never do loose substring matching for short terms (<= 4 chars, e.g. "no", "yes", "sf")
            # to prevent "no" in "now" or "sf" in "transfer"
            if len(c_clean) > 4 and len(b_clean) > 4:
                if b_clean in c_clean or c_clean in b_clean:
                    return True
        return False

    if curr_btn_text and _btn_matches_terms(curr_btn_text, terms):
        has_error = await page.evaluate("""(idx) => {
            const el = document.querySelector('[data-fill-idx="' + idx + '"]');
            if (!el) return false;
            if (el.getAttribute('aria-invalid') === 'true') return true;
            const container = el.closest('[data-automation-id^="formField"], [class*="formField"], [class*="FormField"]') || el.parentElement;
            if (container) {
                if (container.getAttribute('aria-invalid') === 'true') return true;
                const err = container.querySelector('[data-automation-id="errorMessage"], [class*="error" i], [aria-invalid="true"]');
                if (err && err.getBoundingClientRect().height > 0) return true;
            }
            return false;
        }""", field.get("index"))
        if not has_error:
            print(f"    ✓ drop  [{field['index']}] {field['label']!r} = {curr_btn_text!r} (already correct)")
            return
        print(f"    ⚠ drop  [{field['index']}] {field['label']!r} = {curr_btn_text!r} (value matches but field has active error — re-selecting to clear error)")

    await btn.scroll_into_view_if_needed()
    # If button is already expanded (still open from prefetch), close it first
    try:
        fidx = field["index"]
        is_expanded = await page.evaluate("(idx) => { "
            "const el = document.querySelector('[data-fill-idx=\"' + idx + '\"]'); "
            "return el ? el.getAttribute('aria-expanded') === 'true' : false; "
            "}", fidx)
        if is_expanded:
            await btn.click()
            await page.wait_for_timeout(SEARCH_MS)
    except Exception:
        pass
    await btn.click()
    # Wait for options to fully load (retry up to 8x with re-click if no options appear)
    opts = []
    for attempt in range(8):
        await page.wait_for_timeout(800 if attempt > 0 else 1200)
        opts = await page.evaluate("()=>Array.from(document.querySelectorAll(\"li[role='option']\")).map(l=>l.innerText.trim()).filter(Boolean)")
        real_opts = [o for o in opts if o.lower() not in ('select one', '')]
        if real_opts:
            break
        # After 3 failed polls, try re-clicking the button to re-open the dropdown
        if attempt == 3:
            try:
                await btn.click()
                await page.wait_for_timeout(800)
            except Exception:
                pass

    non_disabled = [o for o in opts if o.lower() not in ('select one', '', 'select option')]

    match = None
    for term in terms:
        # 1. Exact match check first
        match = next((o for o in non_disabled if o.strip().lower() == term.lower()), None)
        if not match:
            # 2. Normalized match (punctuation and whitespace agnostic)
            t_norm = re.sub(r'[^a-z0-9]', '', term.lower())
            match = next((o for o in non_disabled if re.sub(r'[^a-z0-9]', '', o.lower()) == t_norm), None)
        if match:
            break

    # 3. If no exact match from terms, ask DeepSeek directly to evaluate the available options!
    if not match and non_disabled:
        from app_common import deepseek_pick_option
        match = await deepseek_pick_option(label, non_disabled, context=f"Candidate terms: {terms}")

    if not match:
        if non_disabled:
            # Check for 'Other' option
            other_opt = next((o for o in non_disabled if "other" in o.lower()), None)
            if label_match(label, "how did you hear", "source", "referral", "learn about") and other_opt:
                match = other_opt
            elif label_match(label, "gender", "sex", "race", "ethnicity", "hispanic", "veteran", "disability"):
                decline = pick_decline(non_disabled)
                if decline:
                    match = decline
                else:
                    print(f"  [skip] compliance field has no matching option: {label!r}")
                    await page.keyboard.press("Escape")
                    return
            elif label_match(label, "degree", "degree type", "level of education"):
                is_master = any("master" in t.lower() or "ms" == t.lower() for t in terms)
                is_bachelor = any("bachelor" in t.lower() or "bs" == t.lower() for t in terms)
                if is_master:
                    match = next((o for o in non_disabled if "master" in o.lower() or "graduate" in o.lower()), None)
                elif is_bachelor:
                    match = next((o for o in non_disabled if "bachelor" in o.lower() or "undergraduate" in o.lower()), None)
                if not match:
                    print(f"    ~ drop  [{field['index']}] {field['label']!r} — no matching degree option found in {opts} for {terms}")
                    await page.keyboard.press("Escape")
                    return
            else:
                match = non_disabled[0]
        elif opts:
            match = opts[0]
    if match and match.lower() != 'select one':
        try:
            # Use Playwright's filter (safe with apostrophes, quotes, etc.) — no CSS injection
            opt_loc = (page.locator("li[role='option']")
                       .filter(has_text=re.compile(r'^\s*' + re.escape(match[:50]) + r'\s*$'))
                       .first)
            if not await opt_loc.count():
                # Fallback: filter by has_text (partial, less precise but safe)
                opt_loc = page.locator("li[role='option']").filter(has_text=match[:50]).first
            await opt_loc.wait_for(state="visible", timeout=5000)
            await opt_loc.click()
            await page.wait_for_timeout(SEARCH_MS)  # wait for React to process the selection
            # Press Tab to trigger blur and commit React state (important for inline forms)
            await page.keyboard.press("Tab")
            await page.wait_for_timeout(400)  # let blur/onChange complete
            await page.keyboard.press("Tab")
            print(f"    ✓ drop  [{field['index']}] {field['label']!r} = {match!r}")
        except Exception as e:
            await page.keyboard.press("Escape")
            print(f"    ~ err   [{field['index']}] {field['label']!r}: {e}")
    else:
        await page.keyboard.press("Escape")
        print(f"    ~ drop  [{field['index']}] {field['label']!r} — no real options (got {opts})")


async def exec_selectinput(page: Page, field: dict, value: str):
    """Workday selectinput: click → type search → Enter → wait for results → click match.
    value may contain newline-separated fallback terms (tried in order until results found)."""
    idx = field['index']
    fid = field.get('id', '')

    # Support multi-term fallback: "Primary Term\nFallback1\nFallback2"
    terms = [t.strip() for t in value.split("\n") if t.strip()]
    if label_match(field.get("label", ""), "how did you hear", "source", "referral", "learn about"):
        hear_fallbacks = [
            "LinkedIn", "Internet/Online Job Posting", "Online Job Board", "Job Board",
            "Indeed", "Social Media", "Company Website", "Careers Website", "GM Careers",
            "GM.com", "Search Engine", "Internet Search", "Advertisement", "Recruiter",
            "Event", "Other"
        ]
        for fb in hear_fallbacks:
            if fb not in terms:
                terms.append(fb)
    if not terms:
        print(f"    ~ sel   [{idx}] {field['label']!r} — empty value")
        return

    # row_scope: a CSS selector stamped on this entry's EDU row container by fill_add_dialog.
    # When present, we locate the selectinput within that row so an intervening SCAN_JS re-stamp
    # can never redirect a [data-fill-idx=N] lookup onto a different entry's field.
    row_scope = field.get("row_scope", "")

    # Locate input — prefer stable id, then row-scoped label query, then page-global data-fill-idx.
    faid = field.get("auto", "")   # data-automation-id of the input (may be empty)
    field_label_lower = field.get("label", "").lower().rstrip("* ")

    if fid:
        inp = page.locator(f"input#{fid}").first
    else:
        inp = page.locator(f"[data-fill-idx='{idx}']").first

    # JS to read the pill value from the SPECIFIC formField for this field.
    # CRITICAL: Always use directEl (data-fill-idx) first so we inspect this exact field's
    # enclosing formField and NEVER mistake a sibling card's pill for this card's pill.
    _PILL_JS_SCOPED = """(args) => {
        const directEl = (args.fid ? document.getElementById(args.fid) : null) ||
                         (args.idx !== null && args.idx !== undefined ? document.querySelector('[data-fill-idx="' + args.idx + '"]') : null);
        if (directEl) {
            const fw = directEl.closest('[data-automation-id^="formField"]') || directEl.parentElement;
            const pill = fw?.querySelector('[data-automation-id="selectedItem"]');
            if (pill && pill.getBoundingClientRect().height > 0) return pill.innerText.trim();
            return null;
        }
        if (args.rowScope) {
            const row = document.querySelector(args.rowScope);
            if (!row) return null;
            const ffs = Array.from(row.querySelectorAll('[data-automation-id^="formField"]'));
            if (args.labelHint) {
                for (const ff of ffs) {
                    const lbl = (ff.querySelector('[data-automation-id="formLabel"],label,legend')?.innerText || '').toLowerCase();
                    if (lbl.includes(args.labelHint)) {
                        const pill = ff.querySelector('[data-automation-id="selectedItem"]');
                        if (pill && pill.getBoundingClientRect().height > 0) return pill.innerText.trim();
                    }
                }
            }
        }
        return null;
    }"""

    def _norm_tokens(s: str) -> set:
        """Normalize string to a set of lowercase alphanum tokens (strips punctuation/hyphens)."""
        return set(re.sub(r'[^a-z0-9 ]', ' ', s.lower()).split())

    def _pill_matches_any_term(pill_text: str, all_terms: list) -> bool:
        """Return True if pill_text token-overlaps ANY term by >50% of the term's tokens."""
        pill_tok = _norm_tokens(pill_text)
        for t in all_terms:
            tok = _norm_tokens(t)
            if not tok:
                continue
            overlap = len(pill_tok & tok)
            if overlap >= max(1, len(tok) // 2):
                return True
        return False

    # Check if a pill is already present BEFORE touching or clicking the input!
    # Clicking into a combobox opens Workday's active dropdown list (defaulting alphabetically to 'A'),
    # which will then be inadvertently committed upon blur or clicking away.
    initial_pill = await page.evaluate(_PILL_JS_SCOPED, {
        "fid": fid, "idx": idx, "rowScope": row_scope, "labelHint": field_label_lower
    })
    if initial_pill:
        if _pill_matches_any_term(initial_pill, terms):
            print(f"    ✓ sel   [{idx}] {field['label']!r} already matches target pill: {initial_pill!r}")
            return
        else:
            print(f"    ~ sel   [{idx}] {field['label']!r} has stale pill {initial_pill!r} — removing...")
            await page.evaluate("""(args) => {
                const directEl = (args.fid ? document.getElementById(args.fid) : null) ||
                                 (args.idx !== null && args.idx !== undefined ? document.querySelector('[data-fill-idx="' + args.idx + '"]') : null);
                let fw = directEl?.closest('[data-automation-id^="formField"]') || directEl?.parentElement;
                if (!fw && args.rowScope) {
                    const row = document.querySelector(args.rowScope);
                    if (row && args.labelHint) {
                        const ffs = Array.from(row.querySelectorAll('[data-automation-id^="formField"]'));
                        for (const ff of ffs) {
                            const lbl = (ff.querySelector('[data-automation-id="formLabel"],label,legend')?.innerText || '').toLowerCase();
                            if (lbl.includes(args.labelHint)) { fw = ff; break; }
                        }
                    }
                    if (!fw) fw = row;
                }
                const delTag = fw?.querySelector('[data-automation-id="deleteTag"], [aria-label^="Remove "]');
                if (delTag) delTag.click();
            }""", {"rowScope": row_scope, "fid": fid, "idx": idx, "labelHint": field_label_lower})
            await page.wait_for_timeout(500)

    # Only focus and click into the input if we actually need to search/type
    try:
        await inp.scroll_into_view_if_needed(timeout=5000)
        await inp.click(force=True, timeout=5000)
    except Exception:
        await page.evaluate("(idx) => { "
            "const el = document.querySelector('[data-fill-idx=\"' + idx + '\"]'); "
            "if (el) { el.scrollIntoView({block:'center'}); el.click(); } "
            "}", idx)
    await page.wait_for_timeout(400)

    for term_idx, term in enumerate(terms):
        # Clear any existing text in input via JS & keyboard before typing (target directEl first)
        await page.evaluate("""(args) => {
            const directEl = (args.fid ? document.getElementById(args.fid) : null) ||
                             (args.idx !== null && args.idx !== undefined ? document.querySelector('[data-fill-idx="' + args.idx + '"]') : null);
            let el = directEl;
            if (!el && args.rowScope) {
                const row = document.querySelector(args.rowScope);
                el = row?.querySelector('input[role="combobox"]') || row?.querySelector('input');
            }
            if (el) {
                el.value = '';
                el.dispatchEvent(new Event('input', { bubbles: true }));
            }
        }""", {"rowScope": row_scope, "fid": fid, "idx": idx})
        await page.wait_for_timeout(100)

        # Focus, clear remaining text, and type search term
        try:
            await inp.click(click_count=3, force=True)
            await inp.press("Control+a")
            await inp.press("Backspace")
        except Exception:
            pass

        await inp.type(term, delay=70)
        await page.wait_for_timeout(SETTLE_MS)

        # Press Enter to trigger Workday's server-side search / auto-fill
        await inp.press("Enter")

        # --- Poll for pill OR visible results (mirrors the skills-path poll loop) ---
        pill = None
        results = []
        for _wait in (600, 500, 500, 400, 400):   # up to ~2.4s total, exits early
            await page.wait_for_timeout(_wait)
            pill = await page.evaluate(_PILL_JS_SCOPED,
                                       {"fid": fid, "idx": idx, "rowScope": row_scope,
                                        "labelHint": field_label_lower})
            if pill and (pill.strip().lower() == term.strip().lower() or _pill_matches_any_term(pill, terms)):
                break  # confirmed auto-fill via strict match — stop polling immediately
            results = await page.evaluate("""() => {
                const getVisible = (c) => Array.from(c.querySelectorAll('[role="option"]'))
                    .filter(e => e.getBoundingClientRect().height > 0)
                    .map(e => e.innerText.trim()).filter(Boolean);
                const c1 = Array.from(document.querySelectorAll('[data-automation-id="activeListContainer"]'))
                    .find(x => x.getBoundingClientRect().height > 0);
                if (c1) { const o = getVisible(c1); if (o.length) return o; }
                const poppers = Array.from(document.querySelectorAll('[data-popper-placement]'))
                    .filter(x => x.getBoundingClientRect().height > 0);
                for (const p of poppers) { const o = getVisible(p); if (o.length) return o; }
                return [];
            }""")
            if results:
                await page.wait_for_timeout(400)
                pill = await page.evaluate(_PILL_JS_SCOPED,
                                           {"fid": fid, "idx": idx, "rowScope": row_scope,
                                            "labelHint": field_label_lower})
                break

        # --- 1. Accept auto-filled pill ---
        if pill and (pill.strip().lower() == term.strip().lower() or _pill_matches_any_term(pill, terms)):
            await inp.press("Tab")
            await page.wait_for_timeout(1200)
            print(f"    ✓ sel   [{idx}] {field['label']!r} = {pill!r} (auto-filled pill)")
            return

        print(f"    [sel] search={term!r} results ({len(results)}): {results[:5]}")

        if not results:
            if pill:
                print(f"    ~ sel   stale/unmatched pill {pill!r} — not accepting, trying next term")
            if term_idx < len(terms) - 1:
                print(f"    ~ sel   no results for {term!r}, trying fallback {terms[term_idx+1]!r}...")
                await inp.press("Escape")
                await page.wait_for_timeout(SETTLE_MS)
                continue
            print(f"    ~ sel   [{idx}] {field['label']!r} — no results for any term: {terms}")
            await inp.press("Escape")
            return

        # If results look unfiltered, try next fallback term
        term_words = set(term.lower().split())
        first_l = results[0].lower()
        if not any(w in first_l for w in term_words):
            if term_idx < len(terms) - 1:
                print(f"    ~ sel   results appear unfiltered for {term!r} (first={results[0]!r}), trying fallback {terms[term_idx+1]!r}...")
                await inp.press("Escape")
                await page.wait_for_timeout(SETTLE_MS)
                continue

        # --- 2. Click matching option in dropdown ---
        match = next((r for r in results if r.strip().lower() == term.strip().lower()), None)
        if not match:
            t_norm = re.sub(r'[^a-z0-9]', '', term.lower())
            match = next((r for r in results if re.sub(r'[^a-z0-9]', '', r.lower()) == t_norm), None)
        if not match and results:
            from app_common import deepseek_pick_option
            match = await deepseek_pick_option(field.get("label", ""), results, context=f"Search term: {term}")

        if not match:
            if term_idx < len(terms) - 1:
                print(f"    ~ sel   no matching option in results for {term!r}, trying fallback {terms[term_idx+1]!r}...")
                await inp.press("Escape")
                await page.wait_for_timeout(SETTLE_MS)
                continue
            else:
                print(f"    ~ sel   [{idx}] {field['label']!r} — no matching option found in {results[:5]} for any term in {terms}")
                await inp.press("Escape")
                return

        opt_loc = (
            page.locator('[data-automation-id="activeListContainer"] [role="option"], [data-popper-placement] [role="option"]')
            .filter(has_text=match[:50])
            .first
        )
        try:
            await opt_loc.wait_for(state="visible", timeout=3000)
            await opt_loc.click(timeout=3000)
            await page.wait_for_timeout(600)
            await inp.press("Tab")
            await page.wait_for_timeout(600)
            print(f"    ✓ sel   [{idx}] {field['label']!r} = {match!r} (clicked dropdown option)")
            return
        except Exception:
            pass

        try:
            await page.evaluate("""(matchText) => {
                const getOpts = (c) => Array.from(c.querySelectorAll('[role="option"]'));
                const c1 = Array.from(document.querySelectorAll('[data-automation-id="activeListContainer"]'))
                    .find(x => x.getBoundingClientRect().height > 0);
                const poppers = Array.from(document.querySelectorAll('[data-popper-placement]'))
                    .filter(x => x.getBoundingClientRect().height > 0);
                const all = (c1 ? getOpts(c1) : []).concat(...poppers.map(getOpts));
                const target = all.find(e => e.innerText.trim().toLowerCase().includes(matchText.toLowerCase()));
                if (target) { target.click(); return true; }
                return false;
            }""", match[:40])
            await page.wait_for_timeout(600)
            await inp.press("Tab")
            await page.wait_for_timeout(600)
            print(f"    ✓ sel   [{idx}] {field['label']!r} = {match!r} (JS click)")
            return
        except Exception as e:
            print(f"    ~ sel   [{idx}] {field['label']!r}: click failed ({e})")

        if term_idx < len(terms) - 1:
            print(f"    ~ sel   trying fallback {terms[term_idx+1]!r}...")
            await inp.press("Escape")
            await page.wait_for_timeout(SETTLE_MS)
            continue
        break

    await inp.press("Escape")
    print(f"    ~ sel   [{idx}] {field['label']!r} — could not select any match from {terms}")


async def exec_radio(page: Page, field: dict, value: str):
    """Click the correct radio in a group, works for both [role=radio] and input[type=radio]."""
    label = field.get("label", "")
    texts = field.get("options") or []
    radio_values = field.get("radioValues") or []
    radio_name = field.get("name") or ""

    val_str = str(value).strip()
    # 1. Exact match
    match = next((o for o in texts if o.strip().lower() == val_str.lower()), None)
    if not match:
        # 2. Normalized match (punctuation and whitespace agnostic)
        v_norm = re.sub(r'[^a-z0-9]', '', val_str.lower())
        match = next((o for o in texts if re.sub(r'[^a-z0-9]', '', o.lower()) == v_norm), None)
    # 3. Ask DeepSeek directly to evaluate the available radio options
    if not match and texts:
        from app_common import deepseek_pick_option
        match = await deepseek_pick_option(label, texts, context=f"Candidate target value: {value}")
    if not match:
        if label_match(label, "gender", "sex", "race", "ethnicity", "hispanic",
                       "veteran", "disability"):
            print(f"  [skip] compliance radio has no matching option: {label!r} (value={value!r})")
            return
        match = texts[0] if texts else value
    match_idx = texts.index(match) if match in texts else 0
    target_value = radio_values[match_idx] if match_idx < len(radio_values) else None

    # Smart check: if target radio is already checked, skip!
    already_checked = await page.evaluate("""(args) => {
        if (args.name && args.val !== null && args.val !== undefined) {
            const r = document.querySelector('input[type="radio"][name="' + args.name + '"][value="' + args.val + '"]');
            if (r && (r.checked || r.getAttribute('aria-checked') === 'true')) return true;
        }
        const tagged = document.querySelector('[data-fill-idx="' + args.idx + '"]');
        if (tagged) {
            let parent = tagged.parentElement;
            for (let i = 0; i < 8 && parent; i++) {
                const radios = Array.from(parent.querySelectorAll('[role="radio"],input[type="radio"]'));
                for (const r of radios) {
                    const lbl = (r.getAttribute('aria-label') || r.parentElement?.innerText || '').toLowerCase();
                    if (lbl.includes(args.match)) {
                        if (r.checked || r.getAttribute('aria-checked') === 'true') return true;
                    }
                }
                parent = parent.parentElement;
            }
        }
        return false;
    }""", {"name": radio_name, "val": target_value, "idx": field["index"], "match": match.lower()[:30]})

    if already_checked:
        print(f"    ✓ radio [{field['index']}] {field['label']!r} = {match!r} (already checked)")
        return

    # Strategy 1: input[type=radio][name=...][value=...]
    if radio_name and target_value is not None:
        clicked = await page.evaluate("(args) => { "
            "const r = document.querySelector("
            "  'input[type=\"radio\"][name=\"' + args.name + '\"][value=\"' + args.val + '\"]'"
            "); "
            "if (r) { r.click(); return true; } "
            "return false; "
            "}", {"name": radio_name, "val": target_value})
        if clicked:
            print(f"    ✓ radio [{field['index']}] {field['label']!r} = {match!r} (name/value)")
            return

    # Strategy 2: [role=radio] by aria-label
    match_lower = match[:30].lower()
    opt = page.locator("[role='radio']").filter(has_text=match[:50]).first
    if await opt.count():
        await opt.scroll_into_view_if_needed()
        await opt.click(force=True)
        print(f"    ✓ radio [{field['index']}] {field['label']!r} = {match!r} (role/filter)")
        return

    # Strategy 3: JS walk up from tagged element
    clicked = await page.evaluate("(args) => { "
        "const tagged = document.querySelector('[data-fill-idx=\"' + args.idx + '\"]'); "
        "if (!tagged) return false; "
        "let parent = tagged.parentElement; "
        "for (let i = 0; i < 8 && parent; i++) { "
        "    const radios = Array.from(parent.querySelectorAll('[role=\"radio\"],input[type=\"radio\"]')); "
        "    if (radios.length) { "
        "        const r = radios.find(r => { "
        "            const lbl = (r.getAttribute('aria-label') || r.parentElement?.innerText || '').toLowerCase(); "
        "            return lbl.includes(args.match); "
        "        }) || radios[args.matchIdx]; "
        "        if (r) { r.click(); return true; } "
        "    } "
        "    parent = parent.parentElement; "
        "} "
        "return false; "
        "}", {"idx": field["index"], "match": match_lower, "matchIdx": match_idx})
    print(f"    {'✓' if clicked else '~'} radio [{field['index']}] {field['label']!r} = {match!r} (options: {texts})")
    await page.wait_for_timeout(SETTLE_MS)


async def exec_checkbox(page: Page, field: dict, value: str):
    want = value.lower() in ("true", "yes", "on", "checked", "1")
    idx = field["index"]
    fid = field.get("id", "")
    sel = f"[data-fill-idx='{idx}']"
    el = page.locator(sel).first
    if not await el.count() and fid:
        el = page.locator(f"#{fid}").first
    # Check current state
    current_checked = await page.evaluate("(args) => { "
        "const el = document.querySelector('[data-fill-idx=\"' + args.idx + '\"]') "
        "           || document.getElementById(args.fid); "
        "if (!el) return null; "
        "return el.checked || el.getAttribute('aria-checked') === 'true'; "
        "}", {"idx": idx, "fid": fid})
    if (want and current_checked) or (not want and not current_checked):
        print(f"    ✓ check [{idx}] {field['label']!r} = {value!r} (already set)")
        return
    # Try multiple click strategies for Workday custom checkboxes
    clicked = False
    # Strategy 1: click the associated <label> — React responds to label clicks, not input
    if fid:
        try:
            lbl = page.locator(f"label[for='{fid}']").first
            if await lbl.count():
                await lbl.scroll_into_view_if_needed(timeout=3000)
                await lbl.click(timeout=3000)
                clicked = True
        except Exception:
            pass
    # Strategy 2: Playwright click on the element itself
    if not clicked:
        try:
            await el.scroll_into_view_if_needed(timeout=3000)
            await el.click(force=True, timeout=3000)
            clicked = True
        except Exception:
            pass
    if not clicked:
        # Strategy 3: Find and click the visible wrapper/label via JS
        clicked = await page.evaluate("(args) => { "
            "const el = document.querySelector('[data-fill-idx=\"' + args.idx + '\"]') "
            "           || document.getElementById(args.fid); "
            "if (!el) return false; "
            "const lbl = args.fid ? document.querySelector('label[for=\"' + args.fid + '\"]') : null; "
            "if (lbl && lbl.getBoundingClientRect().height > 0) { lbl.click(); return true; } "
            "let node = el.parentElement; "
            "for (let i=0; i<6 && node; i++) { "
            "    const rect = node.getBoundingClientRect(); "
            "    if (rect.height > 5 && rect.width > 5) { "
            "        const cbChild = node.querySelector('[data-automation-id*=\"checkbox\"],[class*=\"checkbox\"],[role=\"checkbox\"]'); "
            "        if (cbChild) { cbChild.click(); return true; } "
            "        node.click(); "
            "        return true; "
            "    } "
            "    node = node.parentElement; "
            "} "
            "el.click(); "
            "el.dispatchEvent(new Event('change', {bubbles: true})); "
            "return true; "
            "}", {"idx": idx, "fid": fid})
    await page.wait_for_timeout(SETTLE_MS)
    # Verify the state changed
    after = await page.evaluate("(args) => { "
        "const el = document.querySelector('[data-fill-idx=\"' + args.idx + '\"]') "
        "           || document.getElementById(args.fid); "
        "return el ? (el.checked || el.getAttribute('aria-checked') === 'true') : null; "
        "}", {"idx": idx, "fid": fid})
    print(f"    ✓ check [{idx}] {field['label']!r} = {value!r} (verified={after})")


async def execute_answer(page: Page, field: dict, value: str):
    if not value: return
    if any(k in field.get("label", "").lower() for k in ("upload a file", "5mb", "attach")):
        return  # never click file-upload buttons — resume already uploaded via input[type=file]
    tag = field.get("tag", "")
    ftype = field.get("type", "")
    role = field.get("role", "")
    try:
        if ftype == "checkbox" or role == "checkbox":
            await exec_checkbox(page, field, value)
        elif role == "radio":
            await exec_radio(page, field, value)
        elif field.get("isSelectInput"):
            # Workday selectinput widget — type-to-search dropdown
            await exec_selectinput(page, field, value)
        elif tag == "button":
            await exec_button_dropdown(page, field, value)
        elif tag in ("input", "textarea") or ftype in ("text", "email", "tel", "number", "url", "search"):
            await exec_text(page, field, value)
        elif tag == "select":
            el = page.locator(f"[data-fill-idx='{field['index']}']").first
            try:
                await el.select_option(label=value, timeout=3000)
            except Exception:
                await exec_button_dropdown(page, field, value)
            print(f"    ✓ sel   [{field['index']}] {field['label']!r} = {value!r}")
        else:
            await exec_text(page, field, value)
    except Exception as e:
        print(f"    ~ err   [{field['index']}] {field['label']!r}: {e}")


async def prefetch_options(page: Page, fields: list[dict]):
    """Pre-fetch dropdown options by clicking each button-dropdown and reading options."""
    for f in fields:
        _lbl = f.get("label", "").lower()
        if any(k in _lbl for k in ("upload a file", "5mb", "attach", "upload")):
            continue  # file-upload buttons open native OS chooser — nothing to prefetch
        if f["tag"] == "button" and not f["options"]:
            fid = f.get("id", "")
            faid = f.get("auto", "")
            fidx = f.get("index", "")
            try:
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(200)
                # Locate by id > auto-id > data-fill-idx
                if fid:
                    btn = page.locator(f"button#{fid}").first
                elif faid:
                    btn = page.locator(f"button[data-automation-id='{faid}']").first
                else:
                    btn = page.locator(f"[data-fill-idx='{fidx}']").first
                await btn.scroll_into_view_if_needed(timeout=2000)
                try:
                    await btn.click(timeout=2000)
                except Exception:
                    await btn.click(force=True, timeout=1000)
                await page.wait_for_timeout(800)
                opts = await page.evaluate("()=>Array.from(document.querySelectorAll(\"li[role='option']\")).map(l=>l.innerText.trim()).filter(Boolean)")
                f["options"] = opts
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(300)
                print(f"    [{f['index']:2}] opts {f['label']!r}: {opts[:5]}{'...' if len(opts)>5 else ''}")
            except Exception as e:
                await page.keyboard.press("Escape")
                print(f"  [prefetch] {e}")


async def save_and_continue(page: Page) -> bool:
    """Click Save and Continue. Returns True if page advanced, False if validation error."""
    url_before = page.url
    heading_before = await get_heading(page)

    # 1. Blur active element to commit any in-progress inputs / tags
    await page.evaluate("() => { if (document.activeElement && document.activeElement.blur) document.activeElement.blur(); }")
    await page.wait_for_timeout(800)

    # 2. Wait up to 10s for pageFooterNextButton to be enabled (no disabled / aria-disabled=true)
    next_btn = page.locator("[data-automation-id='pageFooterNextButton']").first
    for _w in range(10):
        is_disabled = await page.evaluate("""() => {
            const btn = document.querySelector('[data-automation-id="pageFooterNextButton"]');
            if (!btn) return true;
            return btn.disabled || btn.getAttribute('aria-disabled') === 'true';
        }""")
        if not is_disabled:
            break
        await page.wait_for_timeout(1000)

    if is_disabled:
        blocked_reasons = await page.evaluate("""() => {
            const reasons = [];
            // A. Check required formFields with empty or generic placeholder values
            document.querySelectorAll('[data-automation-id^="formField"], [class*="formField"]').forEach(ff => {
                const text = ff.innerText || '';
                const hasAsterisk = text.includes('*') || ff.querySelector('[data-automation-id="requiredAsterisk"]') !== null || ff.querySelector('[aria-required="true"]') !== null;
                if (!hasAsterisk) return;
                const inp = ff.querySelector('input, textarea, select, button[aria-haspopup="listbox"], button[data-automation-id="promptSearchButton"]');
                if (!inp) return;
                const val = (inp.value || inp.innerText || '').trim();
                const isGeneric = ['select one', 'select', 'choose', 'search', ''].includes(val.toLowerCase());
                if (isGeneric) {
                    const lbl = ff.querySelector('[data-automation-id="formLabel"], label')?.innerText?.trim() || inp.getAttribute('aria-label') || 'unknown';
                    reasons.push(`Empty required field: ${lbl}`);
                }
            });
            // B. Check required radio groups with no option selected
            document.querySelectorAll('fieldset, [role="radiogroup"]').forEach(rg => {
                const text = rg.innerText || '';
                if (!text.includes('*') && !rg.querySelector('[aria-required="true"]')) return;
                const radios = Array.from(rg.querySelectorAll('input[type="radio"], [role="radio"]'));
                const anyChecked = radios.some(r => r.checked || r.getAttribute('aria-checked') === 'true');
                if (!anyChecked && radios.length > 0) {
                    const leg = rg.querySelector('legend, label, [data-automation-id="formLabel"]')?.innerText?.trim() || 'unknown radio group';
                    reasons.push(`Unchecked required radio: ${leg}`);
                }
            });
            // C. Check elements with aria-invalid="true"
            document.querySelectorAll('[aria-invalid="true"]').forEach(el => {
                const lbl = el.getAttribute('aria-label') || el.closest('[data-automation-id^="formField"]')?.querySelector('label')?.innerText || el.id || 'unknown';
                reasons.push(`Invalid field: ${lbl}`);
            });
            return [...new Set(reasons)].slice(0, 10);
        }""")
        if blocked_reasons:
            print(f"  [NAV] ⚠ Save and Continue is disabled! Blocker reasons: {blocked_reasons}")

    # 3. Use Playwright native click first so React synthetic click listeners fire reliably
    clicked = False
    if await next_btn.count():
        try:
            await next_btn.scroll_into_view_if_needed(timeout=3000)
            await next_btn.click(force=True, timeout=3000)
            clicked = True
        except Exception:
            pass

    if not clicked:
        clicked = await page.evaluate("""() => {
            const btn = document.querySelector('[data-automation-id="pageFooterNextButton"]');
            if (btn) { btn.click(); return true; }
            return false;
        }""")

    if not clicked:
        print(f"  [NAV] No Next button found — page may not be ready")
        await page.wait_for_timeout(2000)
        return False

    await page.wait_for_timeout(SAVE_MS)
    # Check for validation errors
    errors = await read_validation_errors(page)
    if errors:
        print(f"  [NAV] Validation errors: {errors}")
        # Also print any visible "required" field labels
        req_labels = await page.evaluate("""() => {
            return Array.from(document.querySelectorAll('[aria-required="true"],[aria-invalid="true"]'))
                .filter(e => e.getBoundingClientRect().height > 0)
                .map(e => {
                    const fw = e.closest('[data-automation-id="formField"]');
                    const lbl = fw?.querySelector('[data-automation-id="formLabel"],label')?.innerText || e.getAttribute('aria-label') || '';
                    return lbl.trim();
                }).filter(Boolean).slice(0, 10);
        }""")
        if req_labels:
            print(f"  [NAV] Invalid/required fields: {req_labels}")
        return False
    heading_after = await get_heading(page)
    url_after = page.url
    print(f"  [NAV] heading: {heading_before!r} → {heading_after!r} | url changed: {url_before != url_after}")
    if heading_after != heading_before or url_after != url_before:
        return True
    # Same heading/url — Windows/slow network: wait longer and retry twice more
    for _wait in (3000, 4000):
        await page.wait_for_timeout(_wait)
        heading_after2 = await get_heading(page)
        url_after2 = page.url
        print(f"  [NAV] (retry) heading: {heading_after2!r} | url changed: {url_before != url_after2}")
        if heading_after2 != heading_before or url_after2 != url_before:
            return True
    # Still stuck — screenshot for diagnostics
    shot = str(ARTIFACTS / f"stuck_{heading_before.replace(' ', '_')}.png")
    await page.screenshot(path=shot, full_page=False)
    print(f"  [NAV] screenshot: {shot}")
    # Print all visible text that looks like errors or required hints
    page_hints = await page.evaluate("""() => {
        const sel = ['[data-automation-id="errorMessage"]','[aria-invalid="true"]',
                     '[data-automation-id="validationError"]'];
        const found = [];
        sel.forEach(s => document.querySelectorAll(s).forEach(e => {
            const r = e.getBoundingClientRect();
            if (r.height > 0) {
                const fw = e.closest('[data-automation-id="formField"]');
                const lbl = fw?.querySelector('[data-automation-id="formLabel"],label')?.innerText
                          || e.getAttribute('aria-label') || e.innerText || '';
                if (lbl.trim()) found.push(lbl.trim().slice(0,100));
            }
        }));
        return [...new Set(found)].slice(0,15);
    }""")
    if page_hints:
        print(f"  [NAV] page error hints: {page_hints}")
    return False


async def save_and_continue_with_report(page: Page):
    """save_and_continue wrapper that returns (ok, errors, req_labels) for the run report.
    Captures the validation errors and required-field labels that save_and_continue prints,
    so the main loop can include them in run_report.json without re-querying the DOM."""
    ok = await save_and_continue(page)
    # Re-read validation errors after the attempt (may have been cleared on success)
    if not ok:
        errors = await read_validation_errors(page)
        req_labels = await page.evaluate("""() => {
            return Array.from(document.querySelectorAll('[aria-required="true"],[aria-invalid="true"]'))
                .filter(e => e.getBoundingClientRect().height > 0)
                .map(e => {
                    const fw = e.closest('[data-automation-id="formField"]');
                    const lbl = fw?.querySelector('[data-automation-id="formLabel"],label')?.innerText
                              || e.getAttribute('aria-label') || '';
                    return lbl.trim();
                }).filter(Boolean).slice(0, 10);
        }""")
    else:
        errors, req_labels = [], []
    return ok, errors, req_labels
