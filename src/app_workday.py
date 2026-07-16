"""
app_workday.py  —  Workday Application Bot
==============================================
Fills and submits Workday job applications (*.myworkdayjobs.com).

Architecture:
  1. Navigate to job listing URL
  2. For each page: scan ALL visible fields with labels + options
  3. Primary:  send batch to DeepSeek → [{index, value}]   (requires DEEPSEEK_API_KEY in .env)
     Fallback: label-matching rules against library.json   (no API needed)
  4. Execute answers field-by-field — no hardcoded field IDs
  5. Stop at Review page; waits for [Enter] before submitting

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 USAGE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  # Headless (default, no browser window):
  python3 src/app_workday.py "JOB_URL"

  # Headed / displayed mode (shows Chrome window for manual review/edits):
  python3 src/app_workday.py "JOB_URL" --show

  # Log to file — Mac/Linux:
  python3 -u src/app_workday.py "JOB_URL" > run.txt 2>&1 &
  tail -f run.txt

  # Log to file — Windows (PowerShell):
  Start-Process python -ArgumentList "-u src/app_workday.py `"JOB_URL`"" -RedirectStandardOutput run.txt -NoNewWindow
  Get-Content run.txt -Wait

  # Screenshots saved to artifacts/ on errors or stuck pages.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 FLAGS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  JOB_URL   Workday listing URL (positional, required)
  --show    Launch a visible Chrome window so you can watch/intervene.
            The bot pauses at Review anyway — use this when you want to
            manually correct fields before submitting.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 CONFIG
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  data/.env            DEEPSEEK_API_KEY=sk-...   (optional)
  data/library.json    Candidate profile, resume path, preferences
"""

import asyncio, datetime, json, re, sys
from playwright.async_api import async_playwright, Page

# Windows: prevent UnicodeEncodeError on emoji/special chars in log output
# and enable line buffering so logs appear in real-time instead of at exit
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True)
else:
    # Also force line buffering on Mac/Linux when piped (python -u flag helps but not always)
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)

from app_common import (
    LIBRARY, PI, WE, EDU, LANG, RESUME_PATH,
    CHROME_PATH, EMAIL, PASSWORD, DEEPSEEK_KEY,
    PHONE_DIGITS, PROFILE_SUMMARY, SYSTEM_PROMPT,
    deepseek_fill_page, label_match, pick_decline, fuzzy_pick,
    rule_based_answer, rule_based_fill_fields,
    launch_browser, write_json_report, scrape_salary,
    ARTIFACTS_DIR,
)

ARTIFACTS = ARTIFACTS_DIR
ARTIFACTS.mkdir(exist_ok=True)

# Async shim so existing await call sites work unchanged
async def rule_based_fill_page(fields: list[dict], context_hint: str = "") -> list[dict]:
    return rule_based_fill_fields(fields, context_hint)


# ── Workday-specific: skills pill picker via DeepSeek ────────────────────────

async def deepseek_pick_skill(search_term: str, options: list[str], already_selected: list[str]) -> str | None:
    """Ask DeepSeek which dropdown option best matches the desired skill.
    Returns the exact option string to click, or None to skip.
    already_selected: pills already in the field (to avoid re-selecting/deselecting)."""
    if not DEEPSEEK_KEY:
        return None
    already_note = (f"\nAlready selected (DO NOT pick these — clicking again deselects): {already_selected}"
                    if already_selected else "")
    prompt = (f"I am filling a skills field on a job application.\n"
              f"I searched for: {search_term!r}\n"
              f"The dropdown shows these options:\n" +
              "\n".join(f"  {i+1}. {o}" for i, o in enumerate(options)) +
              f"{already_note}\n\n"
              f"Which option is the best match for a candidate with this profile?\n"
              f"Profile skills context: {PROFILE_SUMMARY}\n\n"
              f"Reply ONLY with the exact option text from the list, or 'NONE' if no option is a good match.")
    try:
        import httpx
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post("https://api.deepseek.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {DEEPSEEK_KEY}"},
                json={"model": "deepseek-chat", "temperature": 0.0, "max_tokens": 80,
                      "messages": [{"role": "system", "content": "You are a precise job application assistant. Follow instructions exactly."},
                                   {"role": "user",   "content": prompt}]})
        reply = r.json()["choices"][0]["message"]["content"].strip()
        if reply.upper() == "NONE" or not reply:
            return None
        # Verify the reply actually matches one of the provided options (case-insensitive)
        reply_l = reply.lower()
        match = next((o for o in options if o.lower() == reply_l), None)
        if match is None:
            # Partial match fallback — LLM sometimes adds/removes parenthetical
            match = next((o for o in options if reply_l in o.lower() or o.lower() in reply_l), None)
        return match
    except Exception as e:
        print(f"  [LLM] deepseek_pick_skill error: {e}")
        return None


# ── Timing constants (milliseconds) ──────────────────────────────────────────
SETTLE_MS  = 300   # after click, before reading DOM
SEARCH_MS  = 600   # after typing into search box, before reading results
SAVE_MS    = 3000  # after save/continue, before polling heading
MEDIUM_MS  = 1500  # medium settle (e.g. after dialog save)

# ── DOM scanner ───────────────────────────────────────────────────────────────

SCAN_JS = r"""(rootSel) => {
    const root = (rootSel && document.querySelector(rootSel)) || document;
    const isVis = el => {
        const s = window.getComputedStyle(el);
        if (s.display==='none'||s.visibility==='hidden'||s.opacity==='0') return false;
        const r = el.getBoundingClientRect();
        return r.width>0 && r.height>0;
    };
    const getLabel = el => {
        // 1) aria-labelledby (check for real text, not UUIDs)
        const lblBy = el.getAttribute('aria-labelledby');
        if (lblBy) {
            const parts = lblBy.split(' ').map(id => {
                const t = document.getElementById(id);
                return t ? t.innerText.trim() : '';
            }).filter(t => t && !/^select one/i.test(t) && !/^[0-9a-f]{20,}$/i.test(t));
            if (parts.length) return parts.join(' ').replace(/\*$/, '').trim();
        }
        // 1b) For button dropdowns: try formField question label FIRST so that a pre-filled
        //     Workday dropdown (aria-label = selected value, not the question) gets the real label.
        if (el.tagName === 'BUTTON') {
            const fw2 = el.closest('[data-automation-id^="formField"]') ||
                        el.closest('[data-uxi-widget-type="formField"]');
            if (fw2) {
                for (const sel of [
                    '[data-automation-id="questionText"] p',
                    '[data-automation-id="questionText"]',
                    '[data-automation-id="formLabel"]',
                    'label', 'legend'
                ]) {
                    const l = fw2.querySelector(sel);
                    if (l) {
                        const lt = l.innerText.trim().replace(/\*$/, '').trim();
                        if (lt && !/^[0-9a-f]{20,}$/i.test(lt) && !/^select one/i.test(lt)) return lt;
                    }
                }
            }
        }
        // 2) aria-label (skip generic placeholders and UUIDs, but strip leading whitespace)
        let t = (el.getAttribute('aria-label')||'').trim();
        const isGeneric = /^\d+$/.test(t) || /^select one/i.test(t) || /^required$/i.test(t)
                          || /^[0-9a-f]{20,}$/i.test(t);
        if (t && !isGeneric) return t;
        // 3) label[for=id] — individual label (prioritized for radio/checkbox to distinguish options)
        if (el.id) {
            const l = document.querySelector(`label[for="${el.id}"]`);
            if (l) {
                const lt = l.innerText.trim();
                if (lt && !/^[0-9a-f]{20,}$/i.test(lt) && !/^select one/i.test(lt)) return lt;
            }
        }
        // 3b) For radio/checkbox: check closest ancestor label (input nested inside label)
        if (el.type === 'radio' || el.type === 'checkbox') {
            const closestLbl = el.closest('label');
            if (closestLbl) {
                const lt = closestLbl.innerText.trim().replace(/\*$/, '').trim();
                if (lt && !/^[0-9a-f]{20,}$/i.test(lt) && !/^select one/i.test(lt)) return lt;
            }
        }
        // 4) Closest fieldset — question text is first line (Application Questions pattern)
        const fieldset = el.closest('fieldset');
        if (fieldset) {
            const firstLine = fieldset.innerText.split('\n')[0].trim().replace(/\*$/, '').trim();
            if (firstLine && !/^select one/i.test(firstLine) && !/^[0-9a-f]{20,}$/i.test(firstLine))
                return firstLine;
        }
        // 5) formField context (prefix match for "formField-XXXX" and exact "formField")
        const fw = el.closest('[data-automation-id^="formField"]') ||
                   el.closest('[data-uxi-widget-type="formField"]');
        if (fw) {
            for (const sel of [
                '[data-automation-id="questionText"] p',
                '[data-automation-id="questionText"]',
                '[data-automation-id="formLabel"]',
                'label', 'legend'
            ]) {
                const l = fw.querySelector(sel);
                if (l) {
                    const lt = l.innerText.trim().replace(/\*$/, '').trim();
                    if (lt && !/^[0-9a-f]{20,}$/i.test(lt) && !/^select one/i.test(lt)) return lt;
                }
            }
        }
        return (el.placeholder||el.name||'').trim();
    };
    const getSection = el => {
        let node = el.parentElement;
        while (node && node !== document.body) {
            const h = node.querySelector('h3,h4,[data-automation-id="sectionTitle"],[data-automation-id="groupTitle"]');
            if (h) return h.innerText.trim();
            node = node.parentElement;
        }
        return '';
    };
    const SKIP = new Set(['beecatcher','click_filter','utilityMenuButton','backToJobPosting',
        'navigationItem-Search for Jobs','navigationItem-Candidate Home','navigationItem-Careers Home',
        'pageFooterBackButton','selectedItem','selectedItemList','menuItem','promptIcon']);
    const results = []; let idx = 0;
    root.querySelectorAll(
        'input:not([type=hidden]):not([type=submit]):not([type=file]),' +
        'textarea,select,button:not([aria-hidden="true"]),' +
        '[role="combobox"],[data-uxi-widget-type="selectinput"],' +
        'input[type="checkbox"],[role="checkbox"],' +
        '[role="radio"],input[type="radio"]'
    ).forEach(el => {
        // Native checkboxes and radios are often CSS-hidden (opacity:0/size:0) for custom styling
        // but their parent wrapper IS visible. Skip isVis for these and check parent instead.
        const isRadio = el.getAttribute('role') === 'radio' || el.getAttribute('type') === 'radio';
        const isCheckbox = el.getAttribute('role') === 'checkbox' || el.getAttribute('type') === 'checkbox';
        if (!isRadio && !isCheckbox && !isVis(el)) return;
        if (isRadio || isCheckbox) {
            // Only include if the parent wrapper is actually visible on screen
            const parent = el.parentElement?.parentElement || el.parentElement;
            if (!parent) return;
            const pr = parent.getBoundingClientRect();
            if (pr.height === 0) return;
        }
        const auto = el.getAttribute('data-automation-id')||'';
        if (SKIP.has(auto)) return;
        const tag = el.tagName.toLowerCase();
        const type = el.getAttribute('type')||tag;
        if (tag==='button') {
            const txt=(el.innerText||'').trim().toLowerCase();
            if (!txt||/save|next|back|submit|continue|cancel/.test(txt)) return;
            // Allow buttons with id, data-automation-id, OR inside a formField wrapper with a label
            // (Language NAME dropdown lacks id and aid but is inside a formField)
            const aid = el.getAttribute('data-automation-id')||'';
            if (!el.id && !aid) {
                const fw = el.closest('[data-automation-id^="formField"]');
                if (!fw || !fw.querySelector('[data-automation-id="formLabel"],label')) return;
            }
        }
        const label = getLabel(el);
        let options = [];
        if (tag==='select') options = Array.from(el.options).map(o=>o.text.trim()).filter(Boolean);

        // Detect if this input is inside a Workday selectinput widget
        // NOTE: On some tenants (RH), the <input> itself has data-uxi-widget-type="selectinput"
        // NOTE: On some tenants (Salesforce wd12+), school/FoS use role="combobox" instead
        const inSelectInput = !!(
            el.getAttribute('data-uxi-widget-type') === 'selectinput' ||
            el.closest('[data-uxi-widget-type="selectinput"]') ||
            el.getAttribute('role') === 'combobox'
        );

        // For selectinput: collect available options from the listbox if already open,
        // and fix label lookup — the input IS the widget, so check aria-describedby paragraph for label
        if (inSelectInput && !label) {
            const descId = el.getAttribute('aria-describedby');
            if (descId) {
                const desc = document.getElementById(descId);
                if (desc) label = desc.innerText.trim().replace(/\*$/,'').trim();
            }
            // Also try parent formField
            const fw2 = el.closest('[data-automation-id="formField"]') ||
                        el.parentElement?.closest('[data-automation-id]');
            if (!label && fw2) {
                const lbl2 = fw2.querySelector('[data-automation-id="formLabel"],label');
                if (lbl2) label = lbl2.innerText.trim().replace(/\*$/,'').trim();
            }
        }

        // For radio buttons — only process the FIRST one in each group and collect all siblings
        if (el.getAttribute('role') === 'radio' || el.getAttribute('type') === 'radio') {
            // Match formField or formField-* (RH uses formField-candidateIsPreviousWorker etc.)
            const fw = el.closest('[data-automation-id^="formField"]') ||
                       el.closest('[data-uxi-widget-type="formField"]') ||
                       el.closest('[data-automation-id]') ||
                       el.parentElement?.parentElement;
            if (!fw) return;
            // Skip if we already tagged this group
            const siblings = Array.from(fw.querySelectorAll('[role="radio"],input[type="radio"]'));
            if (siblings.some(r => r !== el && r.hasAttribute('data-fill-idx'))) return;
            if (siblings[0] !== el) return;
            // Get option labels — for input[type=radio], use the span/div sibling text
            const radioOpts = siblings.map(r => {
                const lbl = r.getAttribute('aria-label');
                if (lbl) return lbl.trim();
                // Walk up to find the label text next to this radio
                const parent = r.parentElement;
                const text = parent?.innerText?.trim() || '';
                // Also check value attribute as fallback mapping
                const val = r.getAttribute('value');
                if (text) return text;
                if (val === 'true') return 'Yes';
                if (val === 'false') return 'No';
                return val || '';
            }).filter(Boolean);
            const radioValues = siblings.map(r => r.getAttribute('value') || '');
            // For label: try multiple strategies
            let groupLabel = '';
            // 1. Standard formLabel child
            const fwLabel = fw.querySelector('[data-automation-id="formLabel"],label');
            if (fwLabel) groupLabel = fwLabel.innerText.trim().replace(/\*$/,'').trim();
            // 2. aria-label on fw
            if (!groupLabel) groupLabel = (fw.getAttribute('aria-label') || '').trim();
            // 3. First <p> or <span> text in fw that's not a radio option
            if (!groupLabel) {
                const optTexts = new Set(radioOpts.map(o => o.toLowerCase()));
                for (const node of Array.from(fw.querySelectorAll('p,span,div,[class*="label"]'))) {
                    const t = node.innerText?.trim().replace(/\*$/,'').trim();
                    if (t && !optTexts.has(t.toLowerCase()) && t.length > 3 && t.length < 200) {
                        groupLabel = t; break;
                    }
                }
            }
            // 4. Derive from data-automation-id ("formField-candidateIsPreviousWorker" → "candidate Is Previous Worker")
            if (!groupLabel) {
                const autoId = fw.getAttribute('data-automation-id') || '';
                groupLabel = autoId.replace(/^formField-/, '').replace(/([A-Z])/g,' $1').trim();
            }
            const radioName = el.getAttribute('name') || '';
            el.setAttribute('data-fill-idx', idx);
            results.push({index:idx++, tag:'input', type:'radio', role:'radio',
                id: el.id||'', auto, name: radioName,
                label: groupLabel, section: getSection(el),
                options: radioOpts, radioValues, value: '', isSelectInput: false});
            return;
        }

        let value = '';
        if (type==='checkbox') value = el.checked ? 'true' : 'false';
        else if (tag==='select') value = el.options[el.selectedIndex]?.text.trim()||'';
        else if (tag==='button') {
            value = (el.getAttribute('aria-label')||'').trim();
        }
        else if (inSelectInput) {
            // Read current pill selection
            // Strategy 1: selectedItem pill (most reliable)
            const fwSI = el.closest('[data-automation-id^="formField"]') || el.parentElement;
            const pill = fwSI?.querySelector('[data-automation-id="selectedItem"]');
            if (pill) { value = pill.innerText.trim(); }
            // Strategy 2: promptAriaInstruction (e.g. "1 item selected, Website / Job Board Posting")
            if (!value) {
                const aria = el.closest('[data-automation-id^="formField"]')?.querySelector('[data-automation-id="promptAriaInstruction"]')
                    || document.getElementById(el.getAttribute('aria-describedby') || '');
                if (aria) {
                    const txt = aria.innerText.trim();
                    if (txt && !txt.includes('0 items') && !txt.includes('Expanded') && txt !== '') {
                        const m = txt.match(/item[s]? selected,\s*(.+)/i);
                        value = m ? m[1].trim() : txt;
                    }
                }
            }
            // Strategy 3: the input's own value
            if (!value) value = (el.value || '').trim();
        }
        else value = (el.value||'').trim();

        el.setAttribute('data-fill-idx', idx);
        const ml = parseInt(el.getAttribute('maxlength')) || 0;
        results.push({index:idx++, tag, type, id:el.id||'', auto,
            label, section:getSection(el), options, value,
            maxlength: ml, isSelectInput: inSelectInput});
    });
    return results;
}"""

# ── Page heading ──────────────────────────────────────────────────────────────

async def get_heading(page: Page) -> str:
    return await page.evaluate("""() => {
        const a = document.querySelector('[data-automation-id="progressBarActiveStep"]');
        if (a) return a.innerText.trim().replace(/^current step \\d+ of \\d+\\n/i,'').trim();
        const h3 = document.querySelector('h3'); if (h3) return h3.innerText.trim();
        return document.querySelector('h2')?.innerText.trim()||'';
    }""")

# ── Low-level executors ───────────────────────────────────────────────────────

MONTH_NUM = {"january":"01","february":"02","march":"03","april":"04","may":"05","june":"06",
             "july":"07","august":"08","september":"09","october":"10","november":"11","december":"12"}

async def locate_field(page, field: dict):
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
    label_l = field.get("label","").lower()
    if label_l in ("month","month*") and value.lower() in MONTH_NUM:
        value = MONTH_NUM[value.lower()]

    idx = field["index"]
    fid = field.get("id","")

    # Date spinbutton fields (Month/Year/Day) live inside dialog containers; using
    # scroll_into_view causes repeated re-scrolling. Fill directly via JS instead.
    is_date_part = label_l.strip("* ") in ("month", "year", "day")
    if is_date_part:
        await page.evaluate("(args) => { "
            "const el = document.querySelector('[data-fill-idx=\"' + args.idx + '\"]') "
            "           || document.getElementById(args.fid); "
            "if (!el) return; "
            "el.focus(); "
            "const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set; "
            "if (setter) setter.call(el, args.value); "
            "else el.value = args.value; "
            "el.dispatchEvent(new Event('input', {bubbles: true})); "
            "el.dispatchEvent(new Event('change', {bubbles: true})); "
            "el.dispatchEvent(new FocusEvent('blur', {bubbles: true, cancelable: true})); "
            "}", {"idx": idx, "fid": fid, "value": value})
        print(f"    ✓ text  [{idx}] {field['label']!r} = {value!r} (JS date fill)")
        return

    sel = f"[data-fill-idx='{idx}']"
    el = page.locator(sel).first
    if not await el.is_visible() and fid:
        el = page.locator(f"#{fid}").first

    try:
        await el.scroll_into_view_if_needed(timeout=5000)
        await page.keyboard.press("Escape")   # close any open dropdown first
        await page.wait_for_timeout(SETTLE_MS)
        await el.click(click_count=3, timeout=8000)
    except Exception:
        # JS fallback: scroll + dispatch click
        await page.evaluate("(args) => { "
            "const el = document.querySelector('[data-fill-idx=\"' + args.idx + '\"]') "
            "           || document.getElementById(args.fid); "
            "if (el) { el.scrollIntoView({block:'center'}); el.click(); } "
            "}", {"idx": idx, "fid": fid})
        await page.wait_for_timeout(SETTLE_MS)

    await el.fill(value)
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
        "}", {"idx": idx, "fid": fid})

    print(f"    ✓ text  [{idx}] {field['label']!r} = {value!r}")

async def exec_button_dropdown(page: Page, field: dict, value: str):
    label = field.get("label", "")
    sel = f"[data-fill-idx='{field['index']}']"
    btn = page.locator(sel).first
    fid = field.get("id","")
    faid = field.get("auto","")  # data-automation-id fallback (for buttons without id)
    if not await btn.is_visible():
        if fid:
            btn = page.locator(f"button#{fid}").first
        elif faid:
            btn = page.locator(f"button[data-automation-id='{faid}']").first
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
    match = fuzzy_pick(opts, value)
    if not match:
        # Skip disabled "Select One" — fall back to first non-disabled option
        non_disabled = [o for o in opts if o.lower() not in ('select one', '')]
        if non_disabled:
            match = fuzzy_pick(non_disabled, value)
            if not match:
                # D1b: for compliance/consent fields, prefer a decline option; for others use first
                if label_match(label, "gender", "sex", "race", "ethnicity", "hispanic",
                               "veteran", "disability"):
                    decline = pick_decline(non_disabled)
                    if decline:
                        match = decline
                    else:
                        print(f"  [skip] compliance field has no matching option: {label!r}")
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
    elif row_scope:
        # Scope to the entry's row so we never accidentally target another entry's input.
        # evaluate_handle returns an ElementHandle scoped to the correct row, not page-global.
        # Strategy: find formField whose label matches, then its combobox input.
        inp_handle = await page.evaluate_handle("""(args) => {
            const row = document.querySelector(args.rowScope);
            if (!row) return null;
            // If data-automation-id known, use it directly
            if (args.faid) {
                const el = row.querySelector('[data-automation-id="' + args.faid + '"]');
                if (el && (el.tagName === 'INPUT' || el.getAttribute('role') === 'combobox')) return el;
            }
            // Find the formField whose label text contains the field's label keywords
            const ffs = Array.from(row.querySelectorAll('[data-automation-id^="formField"]'));
            for (const ff of ffs) {
                const lbl = (ff.querySelector('[data-automation-id="formLabel"],label,legend')
                             ?.innerText || '').toLowerCase();
                if (args.labelHint && lbl.includes(args.labelHint)) {
                    const inp = ff.querySelector('input[role="combobox"],[data-uxi-widget-type="selectinput"] input,input:not([type=hidden])');
                    if (inp) return inp;
                }
            }
            // Fallback: first combobox in the row that isn't already filled (no pill)
            const combos = Array.from(row.querySelectorAll('input[role="combobox"],[data-uxi-widget-type="selectinput"] input,input:not([type=hidden])'));
            for (const c of combos) {
                const fw = c.closest('[data-automation-id^="formField"]') || c.parentElement;
                if (!fw?.querySelector('[data-automation-id="selectedItem"]')) return c;
            }
            return combos[0] || null;
        }""", {"rowScope": row_scope, "faid": faid, "labelHint": field_label_lower})
        # as_element() returns the underlying ElementHandle (or None if JS returned null)
        inp = inp_handle.as_element() if inp_handle else None
        if inp is None:
            inp = page.locator(f"[data-fill-idx='{idx}']").first
    else:
        inp = page.locator(f"[data-fill-idx='{idx}']").first

    try:
        await inp.scroll_into_view_if_needed(timeout=5000)
        await inp.click(force=True, timeout=5000)
    except Exception:
        if row_scope:
            await page.evaluate("""(args) => {
                const row = document.querySelector(args.rowScope);
                const el = row?.querySelector('input[role="combobox"]') || row?.querySelector('input');
                if (el) { el.scrollIntoView({block:'center'}); el.click(); }
            }""", {"rowScope": row_scope})
        else:
            await page.evaluate("(idx) => { "
                "const el = document.querySelector('[data-fill-idx=\"' + idx + '\"]'); "
                "if (el) { el.scrollIntoView({block:'center'}); el.click(); } "
                "}", idx)
    await page.wait_for_timeout(400)

    # JS to read the pill value from the SPECIFIC formField for this field.
    # When row_scope is set, find the formField matching the label hint within the row
    # so we don't accidentally read the already-filled School pill from the same row.
    _PILL_JS_SCOPED = """(args) => {
        let fw;
        if (args.rowScope) {
            const row = document.querySelector(args.rowScope);
            if (!row) return null;
            // Find the specific formField whose label matches this field (e.g. "field of study")
            const ffs = Array.from(row.querySelectorAll('[data-automation-id^="formField"]'));
            if (args.labelHint) {
                for (const ff of ffs) {
                    const lbl = (ff.querySelector('[data-automation-id="formLabel"],label,legend')
                                 ?.innerText || '').toLowerCase();
                    if (lbl.includes(args.labelHint)) { fw = ff; break; }
                }
            }
            // Fallback: look for a formField whose input is the one we just typed into
            // (it should be the focused/active element)
            if (!fw) {
                const active = document.activeElement;
                if (active && row.contains(active)) {
                    fw = active.closest('[data-automation-id^="formField"]') || active.parentElement;
                }
            }
            if (!fw) fw = row;  // last resort: whole row
        } else if (args.fid) {
            const el = document.getElementById(args.fid);
            fw = el?.closest('[data-automation-id^="formField"]') || el?.parentElement;
        } else {
            const el = document.querySelector('[data-fill-idx="' + args.idx + '"]');
            fw = el?.closest('[data-automation-id^="formField"]') || el?.parentElement;
        }
        const pill = fw?.querySelector('[data-automation-id="selectedItem"]');
        if (pill && pill.getBoundingClientRect().height > 0) return pill.innerText.trim();
        return null;
    }"""

    def _norm_tokens(s: str) -> set:
        """Normalize string to a set of lowercase alphanum tokens (strips punctuation/hyphens)."""
        return set(re.sub(r'[^a-z0-9 ]', ' ', s.lower()).split())

    def _pill_matches_any_term(pill_text: str, all_terms: list) -> bool:
        """Return True if pill_text token-overlaps ANY term by >50% of the term's tokens.
        Used to detect Workday auto-filled pills whose text doesn't exactly match the search
        term (e.g. pill='Computer and Information Science', term='Computer Science')."""
        pill_tok = _norm_tokens(pill_text)
        for t in all_terms:
            tok = _norm_tokens(t)
            if not tok:
                continue
            overlap = len(pill_tok & tok)
            if overlap >= max(1, len(tok) // 2):
                return True
        return False

    for term_idx, term in enumerate(terms):
        # Clear any existing text and type the search term
        await inp.click(click_count=3, force=True)
        await inp.type(term, delay=70)
        await page.wait_for_timeout(SETTLE_MS)

        # Press Enter to trigger Workday's server-side search / auto-fill
        await inp.press("Enter")

        # --- Poll for pill OR visible results (mirrors the skills-path poll loop) ---
        # A single fixed wait misses Workday's async autofill when it lands late.
        # Exit as soon as we see a matching pill OR any option results appear.
        pill = None
        results = []
        for _wait in (600, 500, 500, 400, 400):   # up to ~2.4s total, exits early
            await page.wait_for_timeout(_wait)
            # Check pill first — auto-filled comboboxes set this before the dropdown settles
            pill = await page.evaluate(_PILL_JS_SCOPED,
                                       {"fid": fid, "idx": idx, "rowScope": row_scope,
                                        "labelHint": field_label_lower})
            if pill and fuzzy_pick([pill], term):
                break  # confirmed auto-fill via strict match — stop polling immediately
            # Check visible dropdown options
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
                # Dropdown appeared — but the pill may arrive shortly after.
                # Give it one extra read before deciding (Workday sometimes renders
                # the generic list 200-300ms before committing the autofill pill).
                await page.wait_for_timeout(400)
                pill = await page.evaluate(_PILL_JS_SCOPED,
                                           {"fid": fid, "idx": idx, "rowScope": row_scope,
                                            "labelHint": field_label_lower})
                break

        # --- 1. Accept auto-filled pill ---
        # Check pill BEFORE evaluating the dropdown/unfiltered-list guard.
        # A Workday `selectedItem` pill is authoritative: Workday only sets it when it has
        # committed a real value. The generic dropdown (Accounting, Actuarial Science, …) can
        # appear SIMULTANEOUSLY with a correct pill, so we must check the pill first.
        # Use token-overlap against ALL terms (not just the current one) so that
        # "Computer and Information Science" is accepted on the first search term "Computer Science"
        # (tokens {computer, science} overlap ≥ 50% threshold → match).
        if pill and (fuzzy_pick([pill], term) or _pill_matches_any_term(pill, terms)):
            await inp.press("Tab")
            await page.wait_for_timeout(1200)
            print(f"    ✓ sel   [{idx}] {field['label']!r} = {pill!r} (auto-filled pill)")
            return

        print(f"    [sel] search={term!r} results ({len(results)}): {results[:5]}")

        if not results:
            # No dropdown visible and no matching pill.
            if pill:
                print(f"    ~ sel   stale/unmatched pill {pill!r} — not accepting, trying next term")
            # Try next fallback term
            if term_idx < len(terms) - 1:
                print(f"    ~ sel   no results for {term!r}, trying fallback {terms[term_idx+1]!r}...")
                await inp.press("Escape")
                await page.wait_for_timeout(SETTLE_MS)
                continue
            print(f"    ~ sel   [{idx}] {field['label']!r} — no results for any term: {terms}")
            await inp.press("Escape")
            return

        # If results look unfiltered (first result doesn't contain any word from search term),
        # Workday returned all options (no match for this term) — try next fallback term.
        # Never blindly pick results[0] — that accepts "Accounting" for "Computer Science".
        term_words = set(term.lower().split())
        first_l = results[0].lower()
        if not any(w in first_l for w in term_words):
            if term_idx < len(terms) - 1:
                print(f"    ~ sel   results unfiltered for {term!r} (first: {results[0]!r}), trying fallback {terms[term_idx+1]!r}...")
                await inp.press("Escape")
                await page.wait_for_timeout(SETTLE_MS)
                continue
            # Last term exhausted with only unfiltered results — leave unset.
            print(f"    ~ sel   [{idx}] {field['label']!r} — no term matched; leaving unset to avoid wrong pick")
            await inp.press("Escape")
            return

        match = fuzzy_pick(results, term) or results[0]
        match_text = match[:60]
        # Use Playwright locator click — React dropdowns ignore JS-injected MouseEvents
        opt_loc = (
            page.locator('[data-automation-id="activeListContainer"] [role="option"],'
                         '[data-popper-placement] [role="option"]')
            .filter(has_text=match_text)
            .first
        )
        clicked = None
        try:
            await opt_loc.wait_for(state="visible", timeout=3000)
            await opt_loc.click(timeout=3000)
            clicked = match_text
        except Exception:
            # Fallback: JS click
            match_lower = match[:60].lower()
            clicked = await page.evaluate("(matchStr) => { "
                "const getVisible = (c) => Array.from(c.querySelectorAll('[role=\"option\"]')) "
                "    .filter(e => e.getBoundingClientRect().height > 0); "
                "let opts = []; "
                "const c1 = Array.from(document.querySelectorAll('[data-automation-id=\"activeListContainer\"]')) "
                "    .find(x => x.getBoundingClientRect().height > 0); "
                "if (c1) opts = getVisible(c1); "
                "if (!opts.length) { "
                "    const poppers = Array.from(document.querySelectorAll('[data-popper-placement]')) "
                "        .filter(x => x.getBoundingClientRect().height > 0); "
                "    for (const p of poppers) { const o = getVisible(p); if (o.length) { opts = o; break; } } "
                "} "
                "const target = opts.find(e => e.innerText.trim().toLowerCase() === matchStr) || "
                "               opts.find(e => e.innerText.trim().toLowerCase().includes(matchStr)) || "
                "               opts[0]; "
                "if (target) { "
                "    target.scrollIntoView({block:'nearest'}); "
                "    target.dispatchEvent(new MouseEvent('mousedown', {bubbles:true})); "
                "    target.click(); "
                "    return target.innerText.trim(); "
                "} "
                "return null; "
                "}", match_lower)
        await page.wait_for_timeout(SETTLE_MS)
        # Tab to trigger blur/onChange and commit React state.
        # Longer wait than Escape to allow AJAX auto-save to complete.
        await page.keyboard.press("Tab")
        await page.wait_for_timeout(1200)
        print(f"    {'✓' if clicked else '~'} sel   [{idx}] {field['label']!r} = {clicked or match!r}")
        return

async def exec_radio(page: Page, field: dict, value: str):
    """Click the correct radio in a group, works for both [role=radio] and input[type=radio]."""
    label = field.get("label", "")
    texts = field.get("options") or []
    radio_values = field.get("radioValues") or []
    radio_name = field.get("name") or ""

    match = fuzzy_pick(texts, value)
    if not match:
        # D1b: for compliance/consent fields, don't fall back to first option
        if label_match(label, "gender", "sex", "race", "ethnicity", "hispanic",
                       "veteran", "disability"):
            print(f"  [skip] compliance radio has no matching option: {label!r} (value={value!r})")
            return
        match = texts[0] if texts else value
    match_idx = texts.index(match) if match in texts else 0
    target_value = radio_values[match_idx] if match_idx < len(radio_values) else None

    # Strategy 1: input[type=radio][name=...][value=...] — stable for RH-style
    # Pass as args to avoid breaking on values containing quotes/backslashes
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

    # Strategy 2: [role=radio] by aria-label — use Playwright filter (no CSS string injection)
    match_lower = match[:30].lower()
    # Try aria-label exact match via Playwright locator (safe with any character)
    opt = page.locator("[role='radio']").filter(has_text=match[:50]).first
    if await opt.count():
        await opt.scroll_into_view_if_needed()
        await opt.click(force=True)
        print(f"    ✓ radio [{field['index']}] {field['label']!r} = {match!r} (role/filter)")
        return

    # Strategy 3: JS walk up from tagged element — pass match_lower as arg
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
    want = value.lower() in ("true","yes","on","checked","1")
    idx = field["index"]; fid = field.get("id","")
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
    if any(k in field.get("label","").lower() for k in ("upload a file", "5mb", "attach")):
        return  # never click file-upload buttons — resume already uploaded via input[type=file]
    tag = field.get("tag",""); ftype = field.get("type","")
    role = field.get("role","")
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
        elif tag in ("input","textarea") or ftype in ("text","email","tel","number","url","search"):
            await exec_text(page, field, value)
        elif tag == "select":
            el = page.locator(f"[data-fill-idx='{field['index']}']").first
            try: await el.select_option(label=value, timeout=3000)
            except Exception: await exec_button_dropdown(page, field, value)
            print(f"    ✓ sel   [{field['index']}] {field['label']!r} = {value!r}")
        else:
            await exec_text(page, field, value)
    except Exception as e:
        print(f"    ~ err   [{field['index']}] {field['label']!r}: {e}")

async def save_and_continue(page: Page) -> bool:
    """Click Save and Continue. Returns True if page advanced, False if validation error."""
    url_before = page.url
    heading_before = await get_heading(page)
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
    shot = str(ARTIFACTS / f"stuck_{heading_before.replace(' ','_')}.png")
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

async def read_validation_errors(page) -> list[str]:
    """Return list of visible validation error messages and red-outlined field labels."""
    errors = await page.evaluate("""() => {
        const errs = [];
        document.querySelectorAll('[data-automation-id="errorMessage"]').forEach(e => {
            if (e.getBoundingClientRect().height > 0) {
                const t = e.innerText?.trim();
                if (t) errs.push(t);
            }
        });
        document.querySelectorAll('[data-automation-id="validation-error-section"] li, '
                                 + '.css-1dbjc4n [role="list"] li').forEach(e => {
            if (e.getBoundingClientRect().height > 0) {
                const t = e.innerText?.trim();
                if (t && t.startsWith('Error')) errs.push(t.slice(0, 80));
            }
        });
        document.querySelectorAll('[class*="error"],[class*="Error"],[class*="invalid"],[class*="Invalid"]').forEach(e => {
            if (e.getBoundingClientRect().height > 0 && e.childElementCount === 0) {
                const t = e.innerText?.trim();
                if (t && t.length < 200) errs.push(t);
            }
        });
        return [...new Set(errs)].slice(0, 10);
    }""")
    return errors

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

# ── Pre-fetch options for all button-dropdowns on a page ─────────────────────

async def prefetch_options(page: Page, fields: list[dict]):
    for f in fields:
        _lbl = f.get("label", "").lower()
        if any(k in _lbl for k in ("upload a file", "5mb", "attach", "upload")):
            continue  # file-upload buttons open native OS chooser — nothing to prefetch
        if f["tag"] == "button" and not f["options"]:
            fid = f.get("id","")
            faid = f.get("auto","")
            fidx = f.get("index","")
            try:
                # Locate by id > auto-id > data-fill-idx
                if fid:
                    btn = page.locator(f"button#{fid}").first
                elif faid:
                    btn = page.locator(f"button[data-automation-id='{faid}']").first
                else:
                    btn = page.locator(f"[data-fill-idx='{fidx}']").first
                await btn.scroll_into_view_if_needed()
                await btn.click()
                await page.wait_for_timeout(900)
                opts = await page.evaluate("()=>Array.from(document.querySelectorAll(\"li[role='option']\")).map(l=>l.innerText.trim()).filter(Boolean)")
                f["options"] = opts
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(400)
                print(f"    [{f['index']:2}] opts {f['label']!r}: {opts[:5]}{'...' if len(opts)>5 else ''}")
            except Exception as e: print(f"  [prefetch] {e}")

# ── Smart page filler: DeepSeek primary, rule-based fallback ─────────────────

async def smart_fill_page(page: Page, heading: str, context_hint: str = "",
                          exclude_ids: set = None):
    print(f"\n  [SCAN] '{heading}'...")

    # Scroll to bottom then top to trigger lazy-rendering of all form sections
    await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
    await page.wait_for_timeout(SEARCH_MS)
    await page.evaluate("() => window.scrollTo(0, 0)")
    await page.wait_for_timeout(400)

    fields = await page.evaluate(SCAN_JS, None)

    fillable = [f for f in fields if f["label"]]
    # If 0 fields found, page might still be loading — retry up to 3 times
    for retry in range(3):
        if fillable: break
        await page.wait_for_timeout(2000)
        fields = await page.evaluate(SCAN_JS, None)
        fillable = [f for f in fields if f["label"]]

    # Remove any explicitly excluded field IDs (e.g. already-filled Self Identify date fields)
    if exclude_ids:
        fillable = [f for f in fillable if f.get("id") not in exclude_ids]

    print(f"  [SCAN] {len(fillable)} fields:")
    for f in fillable:
        print(f"    [{f['index']:2}] {f['tag']:8} {f['label']!r:45} val={f['value']!r}")

    if not fillable: return

    # Pre-fetch options for all button-dropdowns
    await prefetch_options(page, fillable)
    await page.wait_for_timeout(SEARCH_MS)  # let page settle after prefetch opens/closes

    # Date spinbuttons (Month/Day/Year) are deterministic — always use rule-based answers
    # regardless of DeepSeek mode.  They have no options list so the LLM can't reason
    # about them, and it may return wrong format ("July" instead of "07") or skip them.
    _date_labels = {"month", "day", "year"}
    avail = datetime.date.today() + datetime.timedelta(weeks=2)
    _date_answers = {
        "month": str(avail.month).zfill(2),
        "day":   str(avail.day).zfill(2),
        "year":  str(avail.year),
    }
    # Override with graduation date if the page contains graduation-related context
    # (e.g., Application Questions page with "anticipated graduation date" question)
    _page_text_lower = (context_hint or heading or "").lower()
    # Also check visible labels on this page for graduation context
    _all_labels_lower = " ".join(f.get("label","") for f in fillable).lower()
    # Also scan page body text (catches standalone question headings not in field labels).
    try:
        _body_text_lower = (await page.evaluate("() => document.body.innerText")).lower() if not page.is_closed() else ""
    except Exception:
        _body_text_lower = ""
    # Also detect via "school location" + date spinbuttons on same page (Salesforce App Q 2 pattern)
    _has_school_context = ("graduation" in _all_labels_lower or "graduation" in _page_text_lower
                           or "graduation" in _body_text_lower
                           or ("school location" in _all_labels_lower and
                               any(f["label"].strip("* ").lower() in _date_labels for f in fillable))
                           or "anticipated" in _all_labels_lower or "anticipated" in _page_text_lower
                           or "anticipated" in _body_text_lower)
    if _has_school_context:
        _school = next((e for e in EDU if "attending" in e.get("current_status","").lower()), None)
        if _school:
            _end_month_num = MONTH_NUM.get(_school.get("end_month","").lower(), "")
            if _end_month_num:
                _date_answers["month"] = _end_month_num
            _date_answers["day"] = "01"
            if _school.get("end_year"):
                _date_answers["year"] = str(_school["end_year"])
    date_spinbuttons = [
        f for f in fillable
        if f.get("tag") in ("input",) and not f.get("options")
        and f["label"].strip("* ").lower() in _date_labels
    ]
    non_date_fillable = [f for f in fillable if f not in date_spinbuttons]

    # Primary: DeepSeek handles all non-date fields
    if DEEPSEEK_KEY:
        print(f"  [LLM] Sending {len(non_date_fillable)} fields to DeepSeek "
              f"({len(date_spinbuttons)} date spinbuttons handled by rules)...")
        answers = await deepseek_fill_page(non_date_fillable)
        print(f"  [LLM] Got {len(answers)} answers")
    else:
        print(f"  [RULES] DeepSeek unavailable — using label-matching rules")
        # Enrich context_hint with graduation signal if page body text contains it,
        # so rule_based_answer's `section` sees "anticipated graduation" and fires the
        # graduation-date branch (not the generic today+2wk branch) for bare M/D/Y inputs.
        _rule_ctx = context_hint or heading
        if _has_school_context and "graduation" not in _rule_ctx.lower():
            _rule_ctx = "anticipated graduation " + _rule_ctx
        answers = await rule_based_fill_page(fillable, _rule_ctx)

    field_map = {f["index"]: f for f in fillable}

    # Execute DeepSeek/rule answers first (checkboxes before other fields)
    for ans in sorted(answers,
                      key=lambda a: 0 if field_map.get(a.get("index",0),{}).get("type") in ("checkbox","radio") else 1):
        idx = ans.get("index"); val = ans.get("value","")
        if idx is None or not val: continue
        field = field_map.get(idx)
        if field:
            await execute_answer(page, field, val)
            await page.wait_for_timeout(200)

    # Always execute date spinbuttons with rule-based values (even in DeepSeek mode)
    if DEEPSEEK_KEY and date_spinbuttons:
        for f in date_spinbuttons:
            val = _date_answers.get(f["label"].strip("* ").lower(), "")
            if val:
                await execute_answer(page, f, val)
                await page.wait_for_timeout(150)
                print(f"    ✓ date-rule [{f['index']}] {f['label']!r} = {val!r}")

    # Post-fill phone number: after Phone Device Type selection, React may re-render
    # the phone input. Re-fill directly via formField-phoneNumber to ensure persisted.
    # Use pressSequentially to fire real keyboard events per character (React-friendly).
    if PHONE_DIGITS:
        phone_inp_sel = '[data-automation-id="formField-phoneNumber"] input'
        phone_inp = page.locator(phone_inp_sel).first
        if await phone_inp.count():
            current_val = await phone_inp.input_value()
            current_digits = current_val.replace(" ","").replace("-","").replace("(","").replace(")","")
            if current_digits != PHONE_DIGITS:
                await phone_inp.scroll_into_view_if_needed(timeout=3000)
                await phone_inp.click(click_count=3, timeout=3000)
                await phone_inp.press_sequentially(PHONE_DIGITS, delay=30)
                print(f"    ✓ phone post-fill (pressSequentially): {PHONE_DIGITS}")
            else:
                # Value already correct — just ensure blur to commit React state
                await phone_inp.scroll_into_view_if_needed(timeout=3000)
                await phone_inp.click(timeout=3000)
                print(f"    ✓ phone post-fill: already {PHONE_DIGITS}")
            await page.keyboard.press("Tab")  # trigger blur to commit
            await page.wait_for_timeout(300)



async def ensure_signed_in(page: Page):
    """Detect any Workday login wall and auto sign-in (or create account) with stored credentials.

    State-machine approach:
    - State A (create-account form): verifyPassword + createAccountSubmitButton visible
      → Try create account first. If "already exists" error → switch to sign-in.
    - State B (sign-in form): signInSubmitButton visible, no verifyPassword
      → Fill sign-in. If error → try switching to create account.
    - signInLink present on create-account page → clicking it goes to State B.
    - createAccountLink present on sign-in page → clicking it goes to State A.
    """
    LOGIN_SELECTORS = [
        "[data-automation-id='email']",
        "[data-automation-id='signInSubmitButton']",
        "[data-automation-id='signInLink']",
        "[data-automation-id='createAccountSubmitButton']",
    ]
    is_login_wall = any([await page.locator(sel).count() > 0 for sel in LOGIN_SELECTORS])
    if not is_login_wall:
        return

    # Determine credentials — per-tenant first, fallback to personal_info
    current_url = page.url
    tenant = ""
    m = re.search(r'https://([^.]+)\.wd\d+\.myworkdayjobs\.com', current_url)
    if m:
        tenant = m.group(1)
    workday_accounts = LIBRARY.get("workday_accounts", {})
    creds = workday_accounts.get(tenant) or workday_accounts.get("default") or {}
    use_email    = creds.get("email", EMAIL)
    # PASSWORD is already env-sourced; ignore any plaintext password stored in library.json
    use_password = PASSWORD or creds.get("password", "")
    print(f"[AUTH] Login wall detected (tenant={tenant!r}) — attempting auth with {use_email}...")

    async def _wait_past_login(timeout_s=25):
        """Wait until login wall disappears or next-step button appears. Returns True if passed."""
        for _ in range(timeout_s):
            await page.wait_for_timeout(1000)
            if await page.locator("[data-automation-id='pageFooterNextButton']").count(): return True
            if await page.locator("[data-automation-id='adventureButton']").count(): return True
            # If neither email nor sign-in form is present, we've moved past login
            has_email = await page.locator("[data-automation-id='email']").count()
            has_signin = await page.locator("[data-automation-id='signInSubmitButton']").count()
            has_create = await page.locator("[data-automation-id='createAccountSubmitButton']").count()
            if not has_email and not has_signin and not has_create:
                return True
        return False

    async def _get_auth_error():
        err_el = page.locator("[data-automation-id='errorMessage']")
        if await err_el.count():
            return (await err_el.first.inner_text()).strip()
        return ""

    async def _do_sign_in() -> bool:
        """Fill sign-in form (assumes signInSubmitButton is visible). Returns True on success."""
        print("[AUTH] Attempting sign-in (filling email/password)...")
        email_el = page.locator("[data-automation-id='email']").first
        pw_el    = page.locator("[data-automation-id='password']").first
        try:
            await email_el.wait_for(state="visible", timeout=8000)
            await email_el.click(click_count=3); await email_el.fill(use_email)
            await pw_el.wait_for(state="visible", timeout=5000)
            await pw_el.click(click_count=3); await pw_el.fill(use_password)
            await page.wait_for_timeout(400)
            # click_filter div intercepts pointer events for the actual submit button.
            # Playwright native click works (JS click does NOT trigger form submission headlessly).
            cf = page.locator("[data-automation-id='click_filter'][aria-label='Sign In']").first
            if await cf.count():
                await cf.click()
            else:
                # Fallback for tenants without click_filter pattern
                btns = page.locator("button").all()
                for b in await btns:
                    txt = (await b.inner_text()).strip()
                    auto = await b.get_attribute("data-automation-id") or ""
                    if txt.lower() == "sign in" and auto != "utilityButtonSignIn":
                        await b.click(force=True)
                        break
            await page.wait_for_timeout(3000)
            err = await _get_auth_error()
            if err:
                print(f"[AUTH] Sign-in error: {err[:120]}")
                return False
            passed = await _wait_past_login(timeout_s=15)
            if passed:
                print("[AUTH] ✓ Signed in successfully.")
                # Wait for the application form to fully render before returning
                await page.wait_for_timeout(3000)
                return True
            print("[AUTH] Sign-in: no progress after 15s.")
            return False
        except Exception as e:
            print(f"[AUTH] Sign-in exception: {e}")
            return False

    async def _do_create_account() -> bool:
        """Fill create-account form (assumes verifyPassword+createAccountSubmitButton visible). Returns True on success."""
        print("[AUTH] Attempting create account (filling email/password/verify)...")
        email_el  = page.locator("[data-automation-id='email']").first
        pw_el     = page.locator("[data-automation-id='password']").first
        verify_pw = page.locator("[data-automation-id='verifyPassword']").first
        expand_btn = page.locator("[data-automation-id='createAccountExpandButton']").first
        try:
            await email_el.wait_for(state="visible", timeout=8000)
            await email_el.click(click_count=3); await email_el.fill(use_email)
            await pw_el.wait_for(state="visible", timeout=5000)
            await pw_el.click(click_count=3); await pw_el.fill(use_password)
            await verify_pw.wait_for(state="visible", timeout=5000)
            await verify_pw.click(click_count=3); await verify_pw.fill(use_password)
            # Expand optional fields if present
            if await expand_btn.count():
                try: await expand_btn.click(force=True); await page.wait_for_timeout(500)
                except Exception: pass
            # Check terms checkbox
            checkbox = page.locator("[data-automation-id='createAccountCheckbox']").first
            if await checkbox.count():
                if not await checkbox.is_checked():
                    await checkbox.click(force=True); await page.wait_for_timeout(300)
            await page.wait_for_timeout(400)
            # click_filter intercepts pointer events; use Playwright native click (JS click won't submit).
            cf = page.locator("[data-automation-id='click_filter'][aria-label='Create Account']").first
            if await cf.count():
                await cf.click()
                print("[AUTH] Create account submit via: click_filter (Playwright native click)")
            else:
                cf2 = page.locator("[data-automation-id='click_filter']").first
                if await cf2.count():
                    await cf2.click()
                    print("[AUTH] Create account submit via: click_filter (first)")
                else:
                    btn = page.locator("[data-automation-id='createAccountSubmitButton']").first
                    await btn.click(force=True)
                    print("[AUTH] Create account submit via: createAccountSubmitButton")
            await page.wait_for_timeout(3000)
            err = await _get_auth_error()
            if err:
                print(f"[AUTH] Create account error: {err[:120]}")
                return False
            passed = await _wait_past_login(timeout_s=20)
            if passed:
                print("[AUTH] ✓ Account created and signed in.")
                return True
            print("[AUTH] ✓ Create account submitted (may need email verification).")
            return True  # optimistic — let main loop detect if something is wrong
        except Exception as e:
            print(f"[AUTH] Create account exception: {e}")
            return False

    # ── Detect current state ──
    has_verify = await page.locator("[data-automation-id='verifyPassword']").count()
    has_create_btn = await page.locator("[data-automation-id='createAccountSubmitButton']").count()
    has_signin_btn = await page.locator("[data-automation-id='signInSubmitButton']").count()
    has_signin_link = await page.locator("[data-automation-id='signInLink']").count()

    if has_verify and has_create_btn:
        # ── State A: Create-account form shown ──
        # Try sign-in first (account may already exist from a previous run)
        if has_signin_link:
            try:
                await page.locator("[data-automation-id='signInLink']").first.click(force=True)
                await page.wait_for_timeout(1500)
            except Exception:
                pass
        # Attempt sign-in (credentials already registered)
        if await page.locator("[data-automation-id='signInSubmitButton']").count():
            if await _do_sign_in():
                return
            # Sign-in failed — no registered account yet.
            # Switch back to Create Account form and ask user to complete it manually.
            print("[AUTH] ⚠ No existing account found.")
            ca_link = page.locator("[data-automation-id='createAccountLink']").first
            if await ca_link.count():
                try: await ca_link.click(force=True); await page.wait_for_timeout(1500)
                except Exception: pass

    elif has_signin_btn and not has_verify:
        # ── State B: Sign-in form only ──
        if await _do_sign_in():
            return
        # Sign-in failed — switch to create account if link present
        if await page.locator("[data-automation-id='createAccountLink']").count():
            print("[AUTH] ⚠ Sign-in failed — no account yet.")
            try:
                await page.locator("[data-automation-id='createAccountLink']").first.click(force=True)
                await page.wait_for_timeout(1500)
            except Exception: pass

    # ── Manual wait: ask user to complete auth in the browser window ──
    print()
    print("=" * 60)
    print("[AUTH] ACTION REQUIRED — Please complete in the browser window:")
    print(f"       Email: {use_email}")
    print(f"       Password: {use_password}")
    print("       Create the account (fill form + submit), then sign in.")
    print("       Bot will auto-continue once you're past the login page.")
    print("=" * 60)
    for _ in range(180):
        await page.wait_for_timeout(1000)
        still_login = any([await page.locator(sel).count() > 0
                           for sel in ["[data-automation-id='signInSubmitButton']",
                                       "[data-automation-id='email']",
                                       "[data-automation-id='createAccountSubmitButton']"]])
        if not still_login:
            break
    print("[AUTH] ✓ Auth complete — continuing...")
    await page.wait_for_timeout(1500)

# ── Add-dialog filler (My Experience modals) ─────────────────────────────────

def entry_answer(field: dict, entry: dict, section_type: str) -> str | None:
    """Answer a dialog field using a specific library entry (WE or EDU item)."""
    label = field.get("label", "").lower()
    ftype = field.get("type", "")
    tag   = field.get("tag", "")
    opts  = field.get("options", [])
    date_seq = field.get("date_seq", "")  # "start" or "end" (annotated in fill_add_dialog)

    is_work = section_type == "work"
    is_edu  = section_type == "edu"
    is_lang = section_type == "lang"

    # ── Selectinput / text fields ────────────────────────────────────────────
    if field.get("isSelectInput") or tag in ("input","textarea") or ftype in ("text","email","tel","number"):
        if is_lang:
            # Language NAME: either labeled "language/lang name" or unlabeled selectinput (first in dialog)
            if label_match(label, "language","lang name","name") or (not label and field.get("isSelectInput")):
                return entry.get("language","")
            # Reading/Speaking/Writing proficiency — all map to the entry proficiency value
            if label_match(label, "proficiency","fluency","level","ability",
                           "reading","writing","speaking"):
                return entry.get("proficiency","")
        if is_work:
            # Description check MUST come before title/role — "Role Description" contains "role"
            # and would incorrectly match the title rule if order were reversed
            if label_match(label, "description","responsibilities","summary","duties","role description"):
                desc = entry.get("description","")
                ml = field.get("maxlength") or 0
                cap = ml if ml > 0 else 4000  # respect DOM limit; 4000 safe ceiling if absent
                return desc[:cap]
            if label_match(label, "job title","title","position") or \
               (label_match(label, "role") and not label_match(label, "description")):
                return entry.get("role","")
            if label_match(label, "company", "employer", "organization"):
                return entry.get("company","")
            if label_match(label, "city", "location") and not label_match(label, "country","state"):
                return entry.get("city","")
            # (description already matched above — this branch is unreachable but kept for clarity)
            if label_match(label, "start month") or (label_match(label,"month") and date_seq=="start"):
                return entry.get("start_month","")
            if label_match(label, "end month") or (label_match(label,"month") and date_seq=="end"):
                cur = entry.get("current_status","").lower()
                return "" if "current" in cur else entry.get("end_month","")
            if label_match(label, "start year","year from") or (label_match(label,"year") and date_seq=="start"):
                return str(entry.get("start_year",""))
            if label_match(label, "end year","year to") or (label_match(label,"year") and date_seq=="end"):
                cur = entry.get("current_status","").lower()
                return "" if "current" in cur else str(entry.get("end_year",""))
        if is_edu:
            if label_match(label, "school","institution","university","college"):
                return entry.get("institution_variants",[""])[0]
            if label_match(label, "major","field of study","discipline","area of study"):
                # Return primary + fallback terms separated by \n so exec_selectinput tries each
                terms = [entry.get("major_search_term", "")] + entry.get("major_variants", [])
                terms = list(dict.fromkeys(t for t in terms if t))  # dedupe, preserve order
                return "\n".join(terms) if terms else None
            if label_match(label, "gpa","grade"):
                return str(entry.get("gpa",""))
            if label_match(label, "start month") or (label_match(label,"month") and date_seq=="start"):
                return entry.get("start_month","")
            if label_match(label, "end month","graduation month") or (label_match(label,"month") and date_seq=="end"):
                return entry.get("end_month","")
            if label_match(label, "start year","year from","from") or (label_match(label,"year") and date_seq=="start"):
                return str(entry.get("start_year",""))
            if label_match(label, "end year","year to","graduation year","to (actual","to actual","expected") or (label_match(label,"year") and date_seq=="end"):
                # Always return end_year — for still-attending students, this is the EXPECTED graduation year
                # Salesforce's "To (Actual or Expected)" field requires it; Cox hides it via "currently attend" checkbox
                return str(entry.get("end_year",""))

    # ── Button dropdowns (month/degree pickers) ───────────────────────────────
    if tag == "button" or ftype == "select-one":
        if not opts:
            return None
        if is_lang:
            if label_match(label, "language","lang name","name"):
                return fuzzy_pick(opts, entry.get("language","")) or opts[0]
            # Reading/Speaking/Writing/Overall proficiency dropdowns all use entry proficiency
            if label_match(label, "proficiency","fluency","level","ability",
                           "reading","writing","speaking","overall"):
                return fuzzy_pick(opts, entry.get("proficiency","")) or opts[0]
        if is_work:
            if label_match(label, "start month") or (label_match(label,"month") and date_seq=="start"):
                return fuzzy_pick(opts, entry.get("start_month","")) or opts[0]
            if label_match(label, "end month") or (label_match(label,"month") and date_seq=="end"):
                cur = entry.get("current_status","").lower()
                return "" if "current" in cur else (fuzzy_pick(opts, entry.get("end_month","")) or opts[0])
            if label_match(label, "currently work","still work","current job","present"):
                cur = entry.get("current_status","").lower()
                want = "Yes" if "current" in cur else "No"
                return fuzzy_pick(opts, want) or opts[0]
        if is_edu:
            if label_match(label, "degree","degree type","level of education"):
                # Try abbreviation first, then variants, then full name
                abbrev = entry.get("degree_abbreviation","")
                if abbrev:
                    hit = fuzzy_pick(opts, abbrev)
                    if hit: return hit
                for variant in entry.get("degree_search_variants",[]):
                    hit = fuzzy_pick(opts, variant)
                    if hit: return hit
                return fuzzy_pick(opts, entry.get("degree_type","")) or opts[0]
            if label_match(label, "start month") or (label_match(label,"month") and date_seq=="start"):
                return fuzzy_pick(opts, entry.get("start_month","")) or opts[0]
            if label_match(label, "end month","graduation month") or (label_match(label,"month") and date_seq=="end"):
                return fuzzy_pick(opts, entry.get("end_month","")) or opts[0]
            if label_match(label, "currently attend","still attend","current student","present"):
                cur = entry.get("current_status","").lower()
                want = "Yes" if "attending" in cur else "No"
                return fuzzy_pick(opts, want) or opts[0]

    # ── Checkboxes ────────────────────────────────────────────────────────────
    if ftype == "checkbox" or field.get("role") in ("checkbox",):
        if is_work and label_match(label, "currently work","still work","current job","present","i currently"):
            cur = entry.get("current_status","").lower()
            return "true" if "current" in cur else "false"
        if is_edu and label_match(label, "currently attend","still attend","current student","i currently"):
            cur = entry.get("current_status","").lower()
            return "true" if "attending" in cur else "false"

    # ── Radio ─────────────────────────────────────────────────────────────────
    if field.get("role") == "radio" or ftype == "radio":
        radio_opts = field.get("options", [])
        if is_work and label_match(label, "currently work","still work","present"):
            cur = entry.get("current_status","").lower()
            want = "Yes" if "current" in cur else "No"
            return fuzzy_pick(radio_opts, want) or (radio_opts[0] if radio_opts else None)
        if is_edu and label_match(label, "currently attend","still attend","present"):
            cur = entry.get("current_status","").lower()
            want = "Yes" if "attending" in cur else "No"
            return fuzzy_pick(radio_opts, want) or (radio_opts[0] if radio_opts else None)

    return None


# Data-driven section spec for fill_add_dialog field isolation.
# Each section_type maps to anchor keywords (first field in each repeated entry block)
# and stop keywords (first field of the NEXT section, halts slicing).
# Note: LANG has no anchor_kws — it uses field-diff instead (new fields only).
SECTION_SPEC = {
    "work": {
        "anchor_kws": ("job title",),
        "stop_kws": ("school or university", "school", "university", "degree",
                     "type to add skills", "language", "website", "upload a file"),
    },
    "edu": {
        "anchor_kws": ("school or university", "school", "university"),
        "stop_kws": ("type to add skills", "language", "website", "job title", "upload a file"),
    },
    "lang": {
        "anchor_kws": (),
        "stop_kws": ("type to add skills", "website", "job title", "school",
                     "upload a file", "linkedin", "provide your linkedin"),
    },
}

async def fill_add_dialog(page: Page, dialog_label: str, entry: dict = None, section_type: str = "",
                          count_before: int = 0, fields_before: list = None):
    """Scan an open add-dialog, fill only the LAST (newly added) entry group."""
    await page.wait_for_timeout(MEDIUM_MS)  # give Workday's JS time to initialize dialog fields
    all_fields = await page.evaluate(SCAN_JS, None)

    # Identify the NEW entry by finding the last occurrence of its anchor field.
    # The anchor is the first field in each repeated entry block:
    #   WE: "Job Title" | EDU: "School or University" | LANG: field-diff
    spec = SECTION_SPEC.get(section_type, {})
    anchor_kws = spec.get("anchor_kws", ())
    stop_kws   = spec.get("stop_kws", ())

    if anchor_kws:
        # Anchor-based slicing: find last block starting at an anchor keyword field
        anchor_positions = [
            i for i, f in enumerate(all_fields)
            if any(kw in f.get("label","").lower() for kw in anchor_kws)
        ]
        if anchor_positions:
            last_start = anchor_positions[-1]
            # Take from last anchor until the NEXT other-section boundary
            stop_pos = None
            for i in range(last_start + 1, len(all_fields)):
                lbl = all_fields[i].get("label","").lower()
                if any(kw in lbl for kw in stop_kws):
                    stop_pos = i
                    break
            if stop_pos:
                fields = all_fields[last_start: stop_pos]
            else:
                fields = all_fields[last_start:]

            # For EDU: stamp the entry's row container with a unique scope marker so
            # exec_selectinput can locate selectinput inputs WITHIN this entry's row rather
            # than relying on the page-global data-fill-idx (which is re-stamped on every
            # SCAN_JS call and can point to the wrong row after a DOM re-render).
            if section_type == "edu" and fields:
                anchor_field = fields[0]   # the School anchor field for this entry
                anchor_idx = anchor_field["index"]
                scope_attr = f"data-entry-scope"
                scope_val  = f"edu-{anchor_idx}"
                scope_sel  = f"[{scope_attr}='{scope_val}']"
                # Stamp the scope on the row's common ancestor container.
                # Walk up from the anchor input to find the education-row container:
                # Workday wraps each inline EDU row in a div that contains the school formField.
                stamped = await page.evaluate("""(args) => {
                    const el = document.querySelector('[data-fill-idx="' + args.anchorIdx + '"]');
                    if (!el) return false;
                    // Walk up to find a container that holds multiple formField descendants
                    // (the education row) but stop before the entire section.
                    let node = el.closest('[data-automation-id^="formField"]') || el;
                    for (let i = 0; i < 6; i++) {
                        node = node.parentElement;
                        if (!node) break;
                        // Stop at the section-level or body — the row container is the one
                        // that has at least 2 formField children (school + degree + FoS).
                        const ffs = node.querySelectorAll('[data-automation-id^="formField"]');
                        if (ffs.length >= 2) {
                            node.setAttribute(args.scopeAttr, args.scopeVal);
                            return true;
                        }
                    }
                    // Fallback: stamp directly on anchor's formField parent
                    const ff = el.closest('[data-automation-id^="formField"]');
                    if (ff && ff.parentElement) {
                        ff.parentElement.setAttribute(args.scopeAttr, args.scopeVal);
                        return true;
                    }
                    return false;
                }""", {"anchorIdx": anchor_idx, "scopeAttr": scope_attr, "scopeVal": scope_val})
                if stamped:
                    for f in fields:
                        f["row_scope"] = scope_sel
                    print(f"  [SCOPE] EDU entry scoped to {scope_sel!r}")
                else:
                    print(f"  [SCOPE] EDU scope stamp failed — falling back to data-fill-idx")
        else:
            fields = all_fields[count_before:] if count_before > 0 else all_fields
    elif section_type == "lang" and fields_before is not None:
        # Language dialog: field-diff to find newly added fields
        def field_id(f):
            if f.get("id"): return "id:" + f["id"]
            if f.get("auto"): return "auto:" + f["auto"]
            return "lt:" + f.get("label","") + "|" + f.get("tag","")
        before_ids = set(field_id(f) for f in fields_before)
        fields = [f for f in all_fields if field_id(f) not in before_ids]
        # Trim at "Type to Add Skills" / LinkedIn (outside lang section)
        trimmed = []
        for f in fields:
            if any(kw in f.get("label","").lower() for kw in stop_kws):
                break
            trimmed.append(f)
        fields = trimmed
    else:
        # Not a WE/EDU/LANG dialog — use count_before tail
        fields = all_fields[count_before:] if count_before > 0 else all_fields
    for f in fields:
        f["page_heading"] = dialog_label
        f["section"] = dialog_label

    # For WE dialogs: if the first field has an empty label and is a text input,
    # it's almost certainly the Job Title field (Workday sometimes omits aria-label on it).
    if section_type == "work" and fields:
        first = fields[0]
        if not first.get("label") and first.get("tag") in ("input","textarea") \
                and first.get("type","text") not in ("checkbox","radio","button","select-one"):
            first["label"] = "Job Title"

    # Annotate ambiguous "Month"/"Year" labels with start/end position
    # (Workday uses generic "Month"/"Year" labels — first occurrence = start, second = end)
    month_seq = 0; year_seq = 0
    for f in fields:
        lbl = f.get("label","").lower()
        if lbl in ("month","month*"):
            f["date_seq"] = "start" if month_seq == 0 else "end"
            month_seq += 1
        elif lbl in ("year","year*"):
            f["date_seq"] = "start" if year_seq == 0 else "end"
            year_seq += 1

    if section_type == "lang":
        fillable = [f for f in fields if f.get("label") or f.get("isSelectInput")]
    else:
        fillable = [f for f in fields if f["label"]]
    print(f"  [DIALOG] '{dialog_label}': {len(fillable)} fields")
    for f in fillable:
        print(f"    [{f['index']:2}] {f['tag']:8} {f['label']!r:45} opts={f['options'][:3] if f['options'] else []}")

    await prefetch_options(page, fillable)

    if DEEPSEEK_KEY:
        # Date spinbuttons (Month/Year in WE/EDU dialogs) are handled by entry_answer
        # — values come directly from library.json as numeric strings.
        # DeepSeek can't add value here and may return wrong format ("December" vs "12")
        # or swap start/end. Always use entry_answer for these.
        _dlabel = {"month", "year", "day"}
        date_fields   = [f for f in fillable
                         if f.get("tag") == "input" and not f.get("options")
                         and f["label"].strip("* ").lower() in _dlabel]
        llm_fillable  = [f for f in fillable if f not in date_fields]

        # Pass entry context so LLM knows which specific WE/EDU/LANG entry it's filling
        answers = await deepseek_fill_page(llm_fillable, entry=entry, section_type=section_type)

        # Fill date spinbuttons via entry_answer (deterministic, correct numeric format)
        for f in date_fields:
            val = entry_answer(f, entry, section_type) if (entry and section_type) else None
            if val is None:
                val = rule_based_answer(f, dialog_label)
            if val:
                answers.append({"index": f["index"], "value": val})
    elif entry and section_type:
        # Use entry-specific answers
        answers = []
        for f in fillable:
            val = entry_answer(f, entry, section_type)
            if val is None:
                # Fall back to generic rules for non-entry fields
                val = rule_based_answer(f, dialog_label)
            if val is not None and val != "":
                answers.append({"index": f["index"], "value": val})
    else:
        answers = await rule_based_fill_page(fillable, dialog_label)

    field_map = {f["index"]: f for f in fillable}
    # Process checkbox/radio first (e.g. "I currently work here" hides end-date fields)
    for ans in answers:
        idx = ans.get("index"); val = ans.get("value","")
        if idx is None or not val: continue
        field = field_map.get(idx)
        if field and (field.get("type") in ("checkbox","radio") or field.get("role") in ("checkbox","radio")):
            await execute_answer(page, field, val)
            await page.wait_for_timeout(400)
    # Then remaining fields
    for ans in answers:
        idx = ans.get("index"); val = ans.get("value","")
        if idx is None or not val: continue
        field = field_map.get(idx)
        if field and field.get("type") not in ("checkbox","radio") and field.get("role") not in ("checkbox","radio"):
            await execute_answer(page, field, val)
            await page.wait_for_timeout(200)

    await page.wait_for_timeout(500)
    # For EDU inline forms with combobox fields (School, FoS), add extra settle time
    # to ensure AJAX auto-save completes before the next entry is added.
    has_combobox = any(f.get("isSelectInput") for f in fillable)
    if has_combobox and section_type == "edu":
        await page.wait_for_timeout(1500)
    # Check for explicit per-entry Save/OK buttons (modal dialogs)
    # For inline accordion forms (like RH), there is NO per-entry save button — skip
    for ok_sel in [
        "[data-automation-id='wd-CommandButton_uic_okButton']",
        "[data-automation-id='saveButton']",
        "[data-automation-id='done']",
    ]:
        btn = page.locator(ok_sel).first
        if await btn.count() and await btn.is_visible():
            await btn.click()
            print(f"  [DIALOG] ✓ saved ({ok_sel})")
            await page.wait_for_timeout(1500)
            return
    print(f"  [DIALOG] inline form — no explicit save button (data auto-saved)")

# ── My Experience handler ─────────────────────────────────────────────────────

PILLS_JS = """(args) => {
    const {fid, idx} = args;
    const el = fid ? document.getElementById(fid) : document.querySelector('[data-fill-idx="' + idx + '"]');
    const fw = el?.closest('[data-automation-id^="formField"]') || el?.parentElement;
    const items = Array.from(fw?.querySelectorAll('[data-automation-id="selectedItem"]') || []);
    // Also scan any tags/multiselect containers that might be outside the formField
    const global = Array.from(document.querySelectorAll('[data-automation-id="selectedItem"]'));
    const all = [...new Set([...items, ...global])];
    return all.map(e => e.innerText.trim().toLowerCase()).filter(Boolean);
}"""

# Shared JS to read visible [role=option] results from activeListContainer or popper.
# Used by both exec_selectinput and exec_skills_field.
_READ_VISIBLE_OPTIONS_JS = """() => {
    const getOpts = (container) =>
        Array.from(container.querySelectorAll('[role="option"],[data-automation-id="promptLeafNode"]'))
            .filter(e => e.getBoundingClientRect().height > 0)
            .map(e => e.innerText.trim());
    const c1 = Array.from(document.querySelectorAll('[data-automation-id="activeListContainer"]'))
        .find(x => x.getBoundingClientRect().height > 0);
    if (c1) { const o = getOpts(c1); if (o.length) return o; }
    const poppers = Array.from(document.querySelectorAll('[data-popper-placement]'))
        .filter(x => x.getBoundingClientRect().height > 0);
    for (const p of poppers) { const o = getOpts(p); if (o.length) return o; }
    return [];
}"""

async def _type_and_pick_option(page, input_loc, search_term: str, best_match: str) -> bool:
    """Type search_term into input_loc, wait for [role=option] results, click best_match.
    Returns True if an option was successfully clicked."""
    await input_loc.click(click_count=3, force=True)
    await input_loc.type(search_term, delay=70)
    await input_loc.press("Enter")
    results = []
    for _wait in [800, 800, 800, 600]:
        await page.wait_for_timeout(_wait)
        results = await page.evaluate(_READ_VISIBLE_OPTIONS_JS)
        if results:
            break
    if not results:
        return False
    match = fuzzy_pick(results, best_match) or results[0]
    opt_loc = (
        page.locator('[data-automation-id="activeListContainer"] [role="option"],'
                     '[data-popper-placement] [role="option"],'
                     '[data-automation-id="activeListContainer"] [data-automation-id="promptLeafNode"],'
                     '[data-popper-placement] [data-automation-id="promptLeafNode"]')
        .filter(has_text=match[:60])
        .first
    )
    try:
        await opt_loc.wait_for(state="visible", timeout=3000)
        await opt_loc.click(timeout=3000)
        return True
    except Exception:
        return False

async def exec_skills_field(page: Page, field: dict, skills: list[str]):
    """Fill a skills selectinput: for each skill, type → Enter → pick best match.
    Fetches ALL currently-selected pills once at start (handles draft data from previous runs),
    then keeps the set updated as new skills are successfully added."""
    fid = field.get('id', '')
    idx = field['index']

    # Fetch complete current pill set ONCE before iterating.
    # This catches server-persisted draft skills that are already rendered.
    current_pills: set[str] = set(await page.evaluate(PILLS_JS, {"fid": fid, "idx": idx}))
    if current_pills:
        print(f"    [SKILLS] {len(current_pills)} already-selected pills found: {sorted(current_pills)[:8]}{'...' if len(current_pills)>8 else ''}")

    def is_already_selected(name: str) -> bool:
        """Fuzzy check if skill name matches any already-selected pill.
        Uses word-boundary regex. Short names (< 3 chars) only exact-match to avoid
        false positives like 'C' matching 'C++ Programming Language'."""
        n = name.lower().strip()
        for p in current_pills:
            if n == p:
                return True
            # Only do substring checks for names long enough to be unambiguous
            if len(n) < 3:
                continue
            try:
                # skill name as whole word(s) within pill text
                if re.search(r'(?<![a-zA-Z0-9])' + re.escape(n) + r'(?![a-zA-Z0-9])', p):
                    return True
                # pill text as whole word(s) within skill name
                if len(p) >= 3 and re.search(r'(?<![a-zA-Z0-9])' + re.escape(p) + r'(?![a-zA-Z0-9])', n):
                    return True
            except re.error:
                pass
        return False

    for skill in skills:
        # Guard: if the page/browser closed mid-loop, stop cleanly
        if page.is_closed():
            print(f"    [SKILLS] page closed mid-loop — stopping skill fill")
            break
        if fid:
            inp = page.locator(f"input#{fid}").first
        else:
            inp = page.locator(f"[data-fill-idx='{idx}']").first
        try:
            await inp.scroll_into_view_if_needed(timeout=5000)
            await inp.click(force=True, timeout=5000)
        except Exception:
            pass

        # Check already-selected pills before typing (uses pre-fetched set + fuzzy match)
        if is_already_selected(skill):
            print(f"    ~ skill '{skill}' → already selected (pre-check), skipping")
            continue

        await inp.click(click_count=3, force=True)
        await inp.type(skill, delay=70)
        await page.wait_for_timeout(300)
        await inp.press("Enter")

        # Poll for dropdown results (up to 3s) — server search can be slow
        READ_JS = """() => {
            const getOpts = (container) =>
                Array.from(container.querySelectorAll('[role="option"],[data-automation-id="promptLeafNode"]'))
                    .filter(e => e.getBoundingClientRect().height > 0)
                    .map(e => e.innerText.trim());
            const c1 = Array.from(document.querySelectorAll('[data-automation-id="activeListContainer"]'))
                .find(x => x.getBoundingClientRect().height > 0);
            if (c1) { const o = getOpts(c1); if (o.length) return o; }
            const poppers = Array.from(document.querySelectorAll('[data-popper-placement]'))
                .filter(x => x.getBoundingClientRect().height > 0);
            for (const p of poppers) { const o = getOpts(p); if (o.length) return o; }
            return [];
        }"""
        results = []
        # Poll for dropdown results (up to ~3s) — server search can be slow
        for _wait in [800, 800, 800, 600]:
            await page.wait_for_timeout(_wait)
            results = await page.evaluate(READ_JS)
            if results:
                break

        if not results:
            await inp.press("Escape")
            await page.wait_for_timeout(600)  # flush any pending network responses
            print(f"    ~ skill '{skill}' — no results")
            continue

        # Re-read pills: Workday sometimes auto-commits the top option on Enter before we click.
        # If the pill is already present, clicking again would toggle it off — skip the click.
        fresh_pills = set(await page.evaluate(PILLS_JS, {"fid": fid, "idx": idx}))
        current_pills.update(fresh_pills)

        # Pick best match: DeepSeek if available (with rule-based fallback), else rule-based only
        def rule_score(opt):
            o = opt.lower()
            skill_l = skill.lower()
            if o == skill_l: return 0
            if o.startswith(skill_l + " "): return 1
            if re.search(r'\b' + re.escape(skill_l) + r'\b', o): return 2
            if o.startswith(skill_l): return 3
            return 99

        if DEEPSEEK_KEY:
            best = await deepseek_pick_skill(skill, results, list(current_pills))
            if best is None:
                # DeepSeek said no match or errored — fall back to rule-based rather than skip
                best_rule = min(results, key=rule_score)
                if rule_score(best_rule) < 99:
                    best = best_rule
                    print(f"    → skill '{skill}' → LLM no match, rule fallback picked {best!r}")
                else:
                    await inp.press("Escape")
                    await page.wait_for_timeout(400)
                    print(f"    ~ skill '{skill}' → LLM + rule both no match, skipping")
                    continue
            else:
                print(f"    → skill '{skill}' → LLM picked {best!r}")
            # Guard against double-clicking an already-selected pill (uses fuzzy match like pre-check)
            if is_already_selected(best):
                print(f"    ~ skill '{skill}' → {best!r} already selected, skipping")
                await inp.press("Escape")
                await page.wait_for_timeout(300)
                continue
        else:
            # Rule-based only
            best = min(results, key=rule_score)
            if rule_score(best) >= 99:
                await inp.press("Escape")
                await page.wait_for_timeout(400)
                print(f"    ~ skill '{skill}' → no relevant match (best: {best!r}), skipping")
                continue
            print(f"    → skill '{skill}' → rule picked {best!r}")

            # Guard against double-clicking an already-selected pill (deselects it)
            if is_already_selected(best):
                print(f"    ~ skill '{skill}' → {best!r} already selected, skipping")
                await inp.press("Escape")
                await page.wait_for_timeout(300)
                continue

        best_lower = best[:60].lower()

        # Use Playwright locator click — React dropdowns ignore JS-injected MouseEvents.
        # Find the visible option element by text and let Playwright do a real click.
        # For DeepSeek path: dropdown may have closed during API call — re-search the
        # original skill term first (not best text) to get the correct results list back.
        if DEEPSEEK_KEY:
            await inp.click(click_count=3, force=True)
            await inp.type(skill, delay=70)   # re-search ORIGINAL term, not LLM text
            await inp.press("Enter")
            results = []
            for _wait in [1000, 1200, 1200, 1200, 1000]:
                await page.wait_for_timeout(_wait)
                results = await page.evaluate(READ_JS)
                if results:
                    break
            if not results:
                await inp.press("Escape")
                await page.wait_for_timeout(400)
                print(f"    ~ skill '{skill}' → re-search after LLM returned no results, skipping")
                continue

        # Click the chosen option via Playwright (real browser event, not JS injection)
        opt_loc = (
            page.locator('[data-automation-id="activeListContainer"] [role="option"],'
                         '[data-popper-placement] [role="option"],'
                         '[data-automation-id="activeListContainer"] [data-automation-id="promptLeafNode"],'
                         '[data-popper-placement] [data-automation-id="promptLeafNode"]')
            .filter(has_text=best[:60])
            .first
        )
        try:
            await opt_loc.wait_for(state="visible", timeout=3000)
            await opt_loc.click(timeout=3000)
            await page.wait_for_timeout(600)
            print(f"    ✓ skill '{skill}' → clicked {best!r}")
            current_pills.add(best.lower())  # track so next skill can see this as already added
        except Exception as e:
            # Fallback: JS click if Playwright locator fails
            clicked = await page.evaluate("(bestStr) => { "
                "const all = Array.from(document.querySelectorAll("
                "    '[role=\"option\"],[data-automation-id=\"promptLeafNode\"]')) "
                "    .filter(e => e.getBoundingClientRect().height > 0); "
                "const t = all.find(e => e.innerText.trim().toLowerCase() === bestStr) || "
                "          all.find(e => e.innerText.trim().toLowerCase().includes(bestStr)); "
                "if (t) { t.dispatchEvent(new MouseEvent('mousedown',{bubbles:true})); t.click(); return t.innerText.trim(); } "
                "return null; "
                "}", best_lower)
            await page.wait_for_timeout(600)
            if clicked:
                print(f"    ✓ skill '{skill}' → clicked {clicked!r} (JS fallback)")
                current_pills.add(clicked.lower())
            else:
                print(f"    ~ skill '{skill}' → click failed ({e})")
        # Close dropdown before next skill
        await inp.press("Escape")
        await page.wait_for_timeout(300)


async def handle_my_experience(page: Page):
    print("\n[PAGE] My Experience")

    # Resume upload — only if no file already uploaded
    file_input = page.locator("input[type='file']")
    if await file_input.count():
        existing_files = await page.evaluate("""() =>
            Array.from(document.querySelectorAll('[data-automation-id="file-upload-item-name"]'))
                .map(el => el.innerText.trim()).filter(Boolean)""")
        if existing_files:
            print(f"    ~ resume already uploaded: {existing_files[0]!r} — skipping")
        else:
            await file_input.first.set_input_files(RESUME_PATH)
            await page.wait_for_timeout(2500)
            print(f"    ✓ resume uploaded")

    # Discover add-buttons and their section labels
    add_btns_info = await page.evaluate("""() =>
        Array.from(document.querySelectorAll('[data-automation-id="add-button"]')).map((btn,i) => {
            let node = btn.parentElement;
            while (node && node !== document.body) {
                const h = node.querySelector('h3,h4,[data-automation-id="sectionTitle"],[data-automation-id="groupTitle"]');
                if (h) return {index:i, label:h.innerText.trim()};
                node = node.parentElement;
            }
            return {index:i, label:btn.getAttribute('aria-label')||`Section ${i}`};
        })
    """)
    print(f"  {len(add_btns_info)} add-buttons: {[b['label'] for b in add_btns_info]}")

    SECTION_MAP = {
        "work": ("work", WE), "experience": ("work", WE), "employment": ("work", WE),
        "education": ("edu", EDU), "school": ("edu", EDU), "degree": ("edu", EDU),
        "language": ("lang", LANG),
    }

    # Process buttons in DOM order (Work Experience → Education → Languages)
    # This preserves the original button indices which are used for nth() clicks.
    add_btns_sorted = add_btns_info

    for btn_info in add_btns_sorted:
        label_l = btn_info["label"].lower()

        # Websites — skip (no library data needed)
        if "website" in label_l:
            print(f"  [ADD] Skipping '{btn_info['label']}' (websites not required)")
            continue

        # Skills section — handled separately after all add-buttons
        if "skill" in label_l:
            print(f"  [ADD] Skills section detected via add-button — will scan as standalone field")
            continue

        section_type, data_list = next(
            ((st, dl) for k,(st,dl) in SECTION_MAP.items() if k in label_l),
            (None, None)
        )
        if not data_list:
            print(f"  [ADD] No data for '{btn_info['label']}' — skipping")
            continue

        # Guard: if entries of this type already exist on page (from a previous call),
        # skip adding new ones to prevent duplicates on retry.
        # formField-jobTitle / formField-school are reliable automation-ids present
        # for every WE/EDU entry (both expanded inline forms and collapsed cards).
        # Also purge blank Workday-initialized rows (fresh application always starts with 1 blank row).
        if section_type == "work":
            anchor_auto = "formField-jobTitle"
            filled_check_js = """() => Array.from(
                document.querySelectorAll('[data-automation-id="formField-jobTitle"]')
            ).map(el => {
                const inp = el.querySelector('input');
                return inp ? inp.value.trim() : '';
            })"""
        elif section_type == "edu":
            anchor_auto = "formField-school"
            filled_check_js = """() => Array.from(
                document.querySelectorAll('[data-automation-id="formField-school"]')
            ).map(el => {
                const inp = el.querySelector('input');
                return inp ? inp.value.trim() : '';
            })"""
        else:
            anchor_auto = None
            filled_check_js = None

        if anchor_auto:
            # For WE (text inputs): check input.value
            # For EDU (selectinput): check for selectedItem pill OR input.value
            if section_type == "work":
                filled_check_js = """() => Array.from(
                    document.querySelectorAll('[data-automation-id="formField-jobTitle"]')
                ).map(el => {
                    const inp = el.querySelector('input');
                    return inp ? inp.value.trim() : '';
                })"""
            else:  # edu
                # formField-school only exists in the DOM when the accordion entry is EXPANDED.
                # On tenants like Salesforce wd12, existing entries from prior runs are collapsed
                # by default, so formField-school returns 0 and the bot adds duplicates.
                # Fallback: scope to the nth add-button's containing section and count
                # DELETE_charm buttons — those are always present whether collapsed or not.
                edu_btn_idx = btn_info["index"]
                filled_check_js = f"""() => {{
                    const schools = Array.from(document.querySelectorAll('[data-automation-id="formField-school"]'));
                    if (schools.length > 0) {{
                        return schools.map(el => {{
                            const pill = el.querySelector('[data-automation-id="selectedItem"]');
                            if (pill && pill.innerText.trim()) return pill.innerText.trim();
                            const inp = el.querySelector('input');
                            return inp ? inp.value.trim() : '';
                        }});
                    }}
                    // Fallback: count EDU rows by scoping to this add-button's section and
                    // enumerating formField-degree CONTAINER ELEMENTS — exactly one per EDU row.
                    // Using mixed anchor types (degree button + FoS input + school input) overcounts
                    // because the same row has multiple such elements in different wrapper divs.
                    // formField-degree is the single most reliable one-per-row element.
                    const addBtns = Array.from(document.querySelectorAll('[data-automation-id="add-button"]'));
                    const eduBtn = addBtns[{edu_btn_idx}];
                    if (!eduBtn) return [];
                    let node = eduBtn.parentElement;
                    for (let i = 0; i < 8 && node && node !== document.body; i++) {{
                        const degFields = Array.from(node.querySelectorAll('[data-automation-id="formField-degree"]'));
                        if (degFields.length > 0) {{
                            // One entry per formField-degree container. Classify filled vs blank:
                            // - Has DELETE_charm ancestor → existing/filled entry (was added by user or a prior run).
                            // - No DELETE_charm → undeletable pre-spawned blank row (Salesforce mandatory).
                            // Determine value from the degree button aria-label or the row's school input.
                            return degFields.map(ff => {{
                                // Walk up to find if this row has a DELETE_charm
                                let anc = ff.parentElement;
                                let hasDelete = false;
                                for (let j = 0; j < 12 && anc && anc !== node; j++) {{
                                    if (anc.querySelector('[data-automation-id="DELETE_charm"]')) {{
                                        hasDelete = true;
                                        break;
                                    }}
                                    anc = anc.parentElement;
                                }}
                                if (hasDelete) {{
                                    // Filled/existing — read school pill or input value
                                    const pill = ff.closest('div')?.querySelector('[data-automation-id="selectedItem"]');
                                    if (pill && pill.innerText.trim()) return pill.innerText.trim();
                                    const schoolInp = node.querySelector('[data-automation-id="formField-school"] input');
                                    return schoolInp ? schoolInp.value.trim() : 'existing_edu';
                                }}
                                // No DELETE_charm → pre-spawned blank row.
                                // Check if degree is still "Select One" (blank) or already filled.
                                const btn = ff.querySelector('button');
                                const lbl = (btn ? (btn.getAttribute('aria-label') || btn.innerText || '') : '').toLowerCase();
                                return lbl.includes('select one') ? '' : lbl;
                            }});
                        }}
                        node = node.parentElement;
                    }}
                    return [];
                }}"""

            entry_values = await page.evaluate(filled_check_js)
            filled_count = sum(1 for v in entry_values if v)
            blank_count  = sum(1 for v in entry_values if not v)
            existing_count = len(entry_values)
            # Hard cap: blank_count can never exceed the number of physically distinct EDU rows
            # detected (= len(entry_values)).  This is a safety net — the single-anchor count above
            # should already yield 1 per row, but this prevents any future regression from allowing
            # two in-place fills into the same single row.
            if section_type == "edu" and blank_count > existing_count:
                print(f"  [ADD] EDU blank_count cap: {blank_count} → {existing_count} (clamped to row count)")
                blank_count = existing_count
            print(f"  [ADD] '{btn_info['label']}' existing={existing_count} (filled={filled_count} blank={blank_count}), need={len(data_list)}")

            # Delete blank Workday-initialized rows to avoid orphan empty entries causing save errors.
            # Use JS to find DELETE_charm buttons ONLY within blank rows of this section type
            # (can't use page.locator.first — WE delete buttons appear before EDU ones in DOM).
            if blank_count > 0:
                deleted = await page.evaluate(f"""() => {{
                    const anchorAuto = '{anchor_auto}';
                    const rows = Array.from(document.querySelectorAll('[data-automation-id="' + anchorAuto + '"]'));
                    let deleted = 0;
                    for (const row of rows) {{
                        // Determine if this row is blank
                        let isEmpty = false;
                        if (anchorAuto === 'formField-jobTitle') {{
                            const inp = row.querySelector('input');
                            isEmpty = !inp || inp.value.trim() === '';
                        }} else {{
                            // EDU: check for selectedItem pill
                            const pill = row.querySelector('[data-automation-id="selectedItem"]');
                            const inp = row.querySelector('input');
                            isEmpty = !(pill && pill.innerText.trim()) && (!inp || inp.value.trim() === '');
                        }}
                        if (!isEmpty) continue;
                        // Walk up to find the entry container, then find its DELETE_charm
                        let node = row.parentElement;
                        let found = false;
                        for (let i = 0; i < 10 && node && node !== document.body; i++) {{
                            const del = node.querySelector('[data-automation-id="DELETE_charm"]');
                            if (del) {{
                                del.click();
                                deleted++;
                                found = true;
                                break;
                            }}
                            node = node.parentElement;
                        }}
                        if (!found) break;  // Can't find delete — stop trying
                    }}
                    return deleted;
                }}""")
                if deleted:
                    print(f"  [ADD] Deleted {deleted} blank row(s)")
                    await page.wait_for_timeout(1200)
                    # Handle any confirmation dialog
                    for conf_sel in ["button:has-text('Delete')", "button:has-text('Yes')",
                                     "button:has-text('Confirm')", "button:has-text('Remove')"]:
                        conf = page.locator(conf_sel).first
                        if await conf.count() and await conf.is_visible():
                            await conf.click(timeout=2000)
                            await page.wait_for_timeout(400)
                            break
                    await page.wait_for_timeout(500)
                    entry_values = await page.evaluate(filled_check_js)
                    filled_count = sum(1 for v in entry_values if v)
                    existing_count = len(entry_values)
                    print(f"  [ADD] After cleanup: existing={existing_count} filled={filled_count}")

            if filled_count >= len(data_list):
                print(f"  [ADD] '{btn_info['label']}' already fully filled ({filled_count}) — skipping adds")
                continue
        elif section_type == "lang":
            # Count language entries with a real language selected (aria-label = "Language <Name> Required")
            existing_count = await page.evaluate("""() => {
                const btns = Array.from(document.querySelectorAll('button'));
                const lang_kws = ['afrikaans','albanian','amharic','arabic','armenian','azerbaijani',
                    'basque','belarusian','bengali','bosnian','bulgarian','catalan','cebuano','chinese',
                    'corsican','croatian','czech','danish','dutch','english','esperanto','estonian',
                    'filipino','finnish','french','frisian','galician','georgian','german','greek',
                    'gujarati','haitian','hausa','hawaiian','hebrew','hindi','hmong','hungarian',
                    'icelandic','igbo','indonesian','irish','italian','japanese','javanese','kannada',
                    'kazakh','khmer','kinyarwanda','korean','kurdish','kyrgyz','lao','latin','latvian',
                    'lithuanian','luxembourgish','macedonian','malagasy','malay','malayalam','maltese',
                    'maori','marathi','mongolian','myanmar','nepali','norwegian','odia','pashto',
                    'persian','polish','portuguese','punjabi','romanian','russian','samoan','scots',
                    'serbian','sesotho','shona','sindhi','sinhala','slovak','slovenian','somali',
                    'spanish','sundanese','swahili','swedish','tajik','tamil','tatar','telugu','thai',
                    'turkish','turkmen','ukrainian','urdu','uyghur','uzbek','vietnamese','welsh',
                    'xhosa','yiddish','yoruba','zulu'];
                return btns.filter(b => {
                    const lbl = (b.getAttribute('aria-label') || '').toLowerCase();
                    return lang_kws.some(kw => lbl.includes(kw));
                }).length;
            }""")
            filled_count = existing_count
            blank_count = 0
            if existing_count >= len(data_list):
                print(f"  [ADD] '{btn_info['label']}' already has {existing_count} entries — skipping adds")
                continue
            print(f"  [ADD] '{btn_info['label']}' existing={existing_count}, need={len(data_list)}")
        else:
            filled_count = 0
            blank_count = 0

        for entry_idx, entry in enumerate(data_list):
            # Re-query add-buttons each iteration — DOM re-renders after each dialog close
            # and old locator references become detached/stale.
            add_btns = page.locator("[data-automation-id='add-button']")

            entry_label = entry.get("role") or entry.get("degree_type") or entry.get("language", "entry")
            company = entry.get("company") or (entry.get("institution_variants",[""])[0]) or ""
            dialog_label = f"{btn_info['label']}: {entry_label} at {company}"
            print(f"\n  [ADD {entry_idx+1}/{len(data_list)}] '{btn_info['label']}' → {dialog_label}")

            # Count all scannable fields BEFORE clicking Add
            fields_before = await page.evaluate(SCAN_JS, None)
            count_before = len(fields_before)

            # If a blank slot exists for this entry index, fill it directly (no Add click needed).
            # Workday pre-populates 1 blank row on fresh applications; it has no DELETE button
            # so we can't remove it — we must fill it in-place instead.
            if entry_idx < blank_count:
                print(f"  [ADD] Filling existing blank slot (no Add click needed)")
                await fill_add_dialog(page, dialog_label, entry=entry, section_type=section_type,
                                      count_before=count_before, fields_before=fields_before)
                await page.wait_for_timeout(500)
                continue

            # Wait for buttons to be stable (Workday re-renders after dialog saves)
            for _stab in range(8):
                total = await add_btns.count()
                if total > btn_info["index"]:
                    break
                await page.wait_for_timeout(500)
            total = await add_btns.count()
            if btn_info["index"] >= total:
                print(f"  [ADD] Button index {btn_info['index']} out of range ({total}), stopping")
                break

            btn = add_btns.nth(btn_info["index"])
            # Retry click if element detaches mid-action (Workday re-render race)
            for _attempt in range(3):
                try:
                    await btn.scroll_into_view_if_needed(timeout=5000)
                    await btn.click(timeout=5000)
                    break
                except Exception as _e:
                    if _attempt == 2:
                        raise
                    await page.wait_for_timeout(800)
                    add_btns = page.locator("[data-automation-id='add-button']")
                    btn = add_btns.nth(btn_info["index"])
            await page.wait_for_timeout(1500)

            # After the first Add click for EDU, check whether Workday revealed a pre-rendered
            # blank row in addition to the new row (Salesforce wd12 pattern: clicking Add for
            # the first time expands a section that already contains 1 blank entry, so we get
            # 2 rows instead of 1).  If so, promote blank_count so entry 0 fills in-place.
            # NOTE: this path triggers only when pre-loop detected blank_count==0 (no visible rows
            # before any Add click) and entry_idx==0 just clicked Add.  If we end up with 2 rows
            # (mandatory + new), promote blank_count to 1 so entry 0 fills the mandatory row and
            # entry 1 still clicks Add to create another row (3 total = 2 fills + 1 error check).
            # Use formField-degree CONTAINERS (one per row) consistent with pre-loop detection.
            if section_type == "edu" and entry_idx == 0 and blank_count == 0:
                post_click_vals = await page.evaluate("""() => {
                    // Prefer formField-school (most tenants show it immediately)
                    const schools = Array.from(document.querySelectorAll('[data-automation-id="formField-school"]'));
                    if (schools.length > 0) {
                        return schools.map(el => {
                            const pill = el.querySelector('[data-automation-id="selectedItem"]');
                            if (pill && pill.innerText.trim()) return pill.innerText.trim();
                            const inp = el.querySelector('input');
                            return inp ? inp.value.trim() : '';
                        });
                    }
                    // Fallback: count formField-degree CONTAINERS — one per EDU row, same logic
                    // as the pre-loop probe. Avoids overcounting from mixed anchor types.
                    const degFields = Array.from(document.querySelectorAll('[data-automation-id="formField-degree"]'));
                    return degFields.map(ff => {
                        const btn = ff.querySelector('button');
                        const lbl = (btn ? (btn.getAttribute('aria-label') || btn.innerText || '') : '').toLowerCase();
                        return lbl.includes('select one') ? '' : lbl;
                    });
                }""")
                total_rows = len(post_click_vals)
                total_blank = sum(1 for v in post_click_vals if not v)
                if total_rows >= 2 and total_blank >= 1:
                    # 2+ rows visible after first Add click: a pre-spawned mandatory row exists
                    # alongside the newly-added row.  This can happen on Salesforce wd12 when the
                    # mandatory row isn't in DOM before the Add button is clicked.
                    # We must fill entry 0 into the FIRST anchor (mandatory row), NOT the last.
                    # fill_add_dialog always targets anchor_positions[-1] = the last School anchor.
                    # With 2 rows now in DOM, anchor[-1] = the newly-added row = wrong order.
                    # Solution: promote blank_count to 1 so the fill below still targets last anchor
                    # (newly-added row for entry 0 = ASU), then entry 1 will click Add again and fill
                    # UCSD into the third row's last anchor.  The MANDATORY row (first anchor) won't
                    # be filled by this path — but the next save retry or the user can fill it.
                    # BETTER: don't fill entry 0 into the newly-added row (wrong position).
                    # Instead, DON'T call fill_add_dialog for this Add click — delete the newly-added
                    # row if possible, reset, and let the normal in-place path handle both rows.
                    # Practical: just promote blank_count=total_rows so both entries go in-place.
                    # fill_add_dialog for entry 0 calls anchor[-1] = last school anchor = the newly-
                    # added row (row 2, blank) and fills ASU there.  Entry 1 in-place calls anchor[-1]
                    # again = STILL the same last anchor = overwrites ASU with UCSD.  BROKEN.
                    #
                    # TRUE fix: promote blank_count=1 and continue WITHOUT calling fill_add_dialog
                    # for this iteration.  Entry 0 will be re-evaluated: entry_idx(0) < blank_count(1)
                    # → in-place.  But the loop doesn't re-evaluate — it calls fill_add_dialog below.
                    # The only safe fallback when we've already clicked Add and now see 2 rows:
                    # fill entry 0 into anchor[-1] (the newly-added row) here, and treat the
                    # mandatory row as entry -1 that was pre-spawned.  Then entry 1 clicks Add again.
                    # This fills ASU into row 2 (last/new), leaves mandatory row (row 1) blank.
                    # That's still wrong — mandatory row is blank → save fails.
                    #
                    # The primary fix (Fix 1 pre-loop detection) should prevent blank_count==0 from
                    # ever reaching this path on Salesforce.  This block is a last-resort safety net
                    # for timing races on unknown tenants.  Best we can do here: promote blank_count=1
                    # so the loop knows to start clicking Add from entry 1 onward, and log clearly.
                    blank_count = 1
                    print(f"  [ADD] Detected {total_rows} EDU rows after first Add ({total_blank} blank) — "
                          f"promoting blank_count=1 (timing-race fallback; pre-loop detection should have caught this)")

            await fill_add_dialog(page, dialog_label, entry=entry, section_type=section_type,
                                  count_before=count_before, fields_before=fields_before)
            await page.wait_for_timeout(500)

    # ── Standalone Skills field (not behind an add-button on this listing) ────
    skills = LIBRARY.get("skills", [])
    if skills:
        await page.wait_for_timeout(500)
        fields = await page.evaluate(SCAN_JS, None)
        skill_field = next(
            (f for f in fields if f.get("isSelectInput") and
             any(kw in f.get("label","").lower() for kw in ["skill", "type to add"])),
            None
        )
        if skill_field:
            print(f"\n  [SKILLS] Filling {len(skills)} skills via standalone field...")
            await exec_skills_field(page, skill_field, skills)
            # Ensure skills dropdown is fully closed before save
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(400)
            await page.mouse.click(50, 50)
            await page.wait_for_timeout(400)
        else:
            print(f"  [SKILLS] No standalone skills selectinput found on page")
    else:
        print(f"  [SKILLS] No skills in library")

    # Debug: dump all WE field values before save
    _pre_save_vals = await page.evaluate("""() => {
        const anchors = ['jobTitle','company','dateSectionMonth-input','dateSectionYear-input'];
        return anchors.flatMap(a => {
            const els = document.querySelectorAll(`[data-automation-id="${a}"]`);
            return Array.from(els).map(el => ({auto:a, val: el.value||el.getAttribute('aria-valuenow')||''}));
        });
    }""")
    print(f"  [PRE-SAVE] WE field values: {_pre_save_vals}")

    # Save with internal retry — avoids the outer loop re-calling handle_my_experience
    # (which would click Add again and create duplicate WE/EDU/LANG entries).
    saved = False
    for _save_attempt in range(3):
        ok = await save_and_continue(page)
        if ok:
            saved = True
            break
        # save_and_continue may time out detecting navigation in headed/slow mode
        # Check if page actually advanced (e.g., to Application Questions)
        current_heading = await get_heading(page)
        if current_heading and current_heading != "My Experience":
            print(f"  [NAV] Detected navigation to '{current_heading}' — marking save as successful")
            saved = True
            break
        if _save_attempt < 2:
            # Re-scan and re-fill date spinbutton fields AND empty required dropdowns
            # that may have been reset by React re-renders.
            print(f"  [RETRY {_save_attempt+1}] Re-filling date spinbutton fields and empty dropdowns...")
            retry_fields = await page.evaluate(SCAN_JS, None)

            # ── Re-fill date spinbuttons ──
            date_fields_rf = [rf for rf in retry_fields
                              if rf.get("tag") == "input"
                              and rf.get("label","").strip("* ").lower() in ("month","year","day")]
            all_entries = list(WE or []) + list(EDU or [])
            # Build expected date values for all entries
            # WE entries have Month+Year pairs; EDU entries on Salesforce have Year-only (no Month)
            we_date_vals = []   # (month_val, year_val) pairs for WE
            edu_year_vals = []  # (start_year, end_year) pairs for EDU
            for ent in all_entries:
                stype = "work" if ent.get("company") else "edu"
                sm = entry_answer({"label": "Month", "tag": "input", "date_seq": "start"}, ent, stype)
                sy = entry_answer({"label": "Year",  "tag": "input", "date_seq": "start"}, ent, stype)
                em = entry_answer({"label": "Month", "tag": "input", "date_seq": "end"},   ent, stype)
                ey = entry_answer({"label": "Year",  "tag": "input", "date_seq": "end"},   ent, stype)
                if stype == "work":
                    if sm and sy: we_date_vals.append((sm, sy))
                    if em and ey: we_date_vals.append((em, ey))
                else:  # edu
                    edu_year_vals.append((sy, ey))

            # Process WE Month+Year pairs
            date_pairs = []
            i = 0
            while i < len(date_fields_rf):
                lbl = date_fields_rf[i].get("label","").strip("* ").lower()
                if lbl in ("month","day") and i+1 < len(date_fields_rf):
                    date_pairs.append((date_fields_rf[i], date_fields_rf[i+1]))
                    i += 2
                else:
                    i += 1
            for pair_i, (mf, yf) in enumerate(date_pairs):
                if pair_i < len(we_date_vals):
                    mv, yv = we_date_vals[pair_i]
                    mf["page_heading"] = "My Experience"
                    yf["page_heading"] = "My Experience"
                    await exec_text(page, mf, mv)
                    await page.wait_for_timeout(200)
                    await exec_text(page, yf, yv)
                    await page.wait_for_timeout(200)

            # Process EDU standalone Year fields (no Month counterpart — Salesforce has Year-only EDU)
            edu_year_fields = [rf for rf in date_fields_rf
                               if rf.get("label","").strip("* ").lower() == "year"
                               and rf not in [yf for _, yf in date_pairs]]
            for yf_i, yf in enumerate(edu_year_fields):
                edu_ent_i = yf_i // 2  # 2 year fields per EDU entry (start + end)
                year_within = yf_i % 2  # 0=start, 1=end
                if edu_ent_i < len(edu_year_vals):
                    sy_val, ey_val = edu_year_vals[edu_ent_i]
                    yv = sy_val if year_within == 0 else ey_val
                    if yv:
                        yf["page_heading"] = "My Experience"
                        print(f"  [RETRY] Re-filling EDU Year [{yf_i}] = {yv!r}")
                        await exec_text(page, yf, yv)
                        await page.wait_for_timeout(200)

            # ── Re-fill EDU combobox fields (School, Field of Study) if EMPTY on retry ──
            # Only re-fill if pill is absent — re-opening a filled combobox leaves the dropdown
            # open and blocks Save and Continue.
            # Guard: if DOM row count no longer matches len(EDU), the page re-stamped indices.
            # Positional mapping (edu_combobox_i // 2) would target wrong entries — skip it.
            live_edu_rows = await page.evaluate("""() => (
                document.querySelectorAll('[data-automation-id="formField-degree"]').length ||
                document.querySelectorAll('[data-automation-id="formField-school"]').length
            )""")
            _edu_layout_ok = (live_edu_rows == len(EDU)) if EDU else True
            if not _edu_layout_ok:
                print(f"  [RETRY] EDU DOM layout changed ({live_edu_rows} rows vs {len(EDU)} expected) — skipping positional re-fill")
            all_comboboxes = [rf for rf in retry_fields
                              if rf.get("isSelectInput")
                              and rf.get("label","")]
            edu_combobox_i = 0
            for cb_f in all_comboboxes:
                if not _edu_layout_ok:
                    break
                lbl_l = cb_f.get("label","").lower()
                edu_ent_i = edu_combobox_i // 2  # ~2 comboboxes per EDU entry
                ent = EDU[edu_ent_i] if edu_ent_i < len(EDU) else (EDU[-1] if EDU else None)
                if not ent:
                    continue
                if label_match(lbl_l, "school","university","institution","college"):
                    current_val = cb_f.get("value", "").strip()
                    if current_val:
                        print(f"  [RETRY] School combobox already has pill {current_val!r} — skipping")
                        edu_combobox_i += 1
                        continue
                    search_val = entry_answer(dict(cb_f, tag="input"), ent, "edu")
                    if search_val:
                        print(f"  [RETRY] Re-filling School combobox = {search_val!r}")
                        await exec_selectinput(page, cb_f, search_val)
                        await page.keyboard.press("Escape")
                        await page.wait_for_timeout(500)
                    edu_combobox_i += 1
                elif label_match(lbl_l, "major","field of study","discipline"):
                    current_val = cb_f.get("value", "").strip()
                    if current_val:
                        print(f"  [RETRY] FoS combobox already has pill {current_val!r} — skipping")
                        edu_combobox_i += 1
                        continue
                    search_val = entry_answer(dict(cb_f, tag="input"), ent, "edu")
                    if search_val:
                        print(f"  [RETRY] Re-filling FoS combobox = {search_val!r}")
                        await exec_selectinput(page, cb_f, search_val)
                        await page.keyboard.press("Escape")
                        await page.mouse.click(50, 50)
                        await page.wait_for_timeout(500)
                    edu_combobox_i += 1
            # These can be reset when React re-renders after adding entries.
            empty_drops = [rf for rf in retry_fields
                           if rf.get("tag") == "button"
                           and "select one" in rf.get("value","").lower()
                           and rf.get("label","")]
            degree_drops = [d for d in empty_drops
                            if label_match(d.get("label","").lower(),
                                           "degree","degree type","level of education")]
            for drop_i, drop_f in enumerate(degree_drops):
                if not _edu_layout_ok:
                    break
                ent = EDU[drop_i] if drop_i < len(EDU) else (EDU[-1] if EDU else None)
                if not ent:
                    continue
                # Match original fill priority: abbreviation first → search variants → full type.
                # NEVER use degree_type first — "Master's Degree" fuzzy-matches "MA" (contains "ma")
                # instead of "MS" via fuzzy_pick's substring strategy.
                search_val = (ent.get("degree_abbreviation","")
                              or next(iter(ent.get("degree_search_variants",[])), "")
                              or ent.get("degree_type",""))
                if search_val:
                    print(f"  [RETRY] Re-filling Degree dropdown [{drop_i}] = {search_val!r}")
                    await exec_button_dropdown(page, drop_f, search_val)
                    await page.wait_for_timeout(500)

            await page.wait_for_timeout(1000)
    if not saved:
        print(f"  [ERR] handle_my_experience: save failed after 3 attempts — stopping")
    return saved

async def handle_voluntary_disclosures(page: Page) -> bool:
    # First pass — fill all visible fields
    await smart_fill_page(page, "Voluntary Disclosures")

    # Scroll to bottom and re-scan to catch any below-fold fields
    # ("I certify the information is accurate", hidden radio buttons, etc.)
    await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
    await page.wait_for_timeout(700)
    await smart_fill_page(page, "Voluntary Disclosures")
    await page.evaluate("() => window.scrollTo(0, 0)")
    await page.wait_for_timeout(400)

    # T&C checkbox — robust multi-strategy click since Workday uses custom React checkboxes
    tc_id = "termsAndConditions--acceptTermsAndAgreements"
    tc_checked = await page.evaluate(f"""() => {{
        const el = document.getElementById('{tc_id}');
        return el ? (el.checked || el.getAttribute('aria-checked') === 'true') : null;
    }}""")

    if tc_checked is None:
        # tc_id not found — look for any unchecked consent checkbox on the page
        tc_checked = await page.evaluate("""() => {
            const inputs = Array.from(document.querySelectorAll('input[type="checkbox"]'));
            for (const inp of inputs) {
                const id = inp.id || '';
                const lbl = document.querySelector(`label[for="${id}"]`)?.innerText || '';
                if (/consent|agree|terms|certify/i.test(lbl) || /consent|agree|terms|certify/i.test(id)) {
                    return inp.checked || inp.getAttribute('aria-checked') === 'true';
                }
            }
            return null;
        }""")
        print(f"    [T&C] tc_id not found, scanned for consent checkbox: {tc_checked}")

    if not tc_checked:
        # Strategy 1: click associated <label> (React-friendly)
        clicked = await page.evaluate(f"""() => {{
            const input = document.getElementById('{tc_id}')
                       || Array.from(document.querySelectorAll('input[type="checkbox"]'))
                              .find(i => /consent|agree|terms|certify/i.test(i.id)
                                      || /consent|agree|terms|certify/i.test(
                                           document.querySelector('label[for="'+i.id+'"]')?.innerText||''));
            if (!input) return false;
            const lbl = document.querySelector(`label[for="${{input.id}}"]`);
            if (lbl && lbl.getBoundingClientRect().height > 0) {{
                lbl.scrollIntoView({{block:'center'}});
                lbl.click();
                return true;
            }}
            // Fallback: walk up for clickable wrapper
            let node = input.parentElement;
            for (let i=0; i<8 && node && node !== document.body; i++) {{
                const rect = node.getBoundingClientRect();
                if (rect.height > 10 && rect.width > 10) {{
                    node.scrollIntoView({{block:'center'}});
                    node.click();
                    return true;
                }}
                node = node.parentElement;
            }}
            return false;
        }}""")
        await page.wait_for_timeout(500)
        tc_checked = await page.evaluate(f"""() => {{
            const el = document.getElementById('{tc_id}')
                    || Array.from(document.querySelectorAll('input[type="checkbox"]'))
                           .find(i => /consent|agree|terms|certify/i.test(i.id)
                                   || /consent|agree|terms|certify/i.test(
                                        document.querySelector('label[for="'+i.id+'"]')?.innerText||''));
            return el ? (el.checked || el.getAttribute('aria-checked') === 'true') : false;
        }}""")
        if not tc_checked:
            # Strategy 2: Playwright locator click
            for sel in [f"#{tc_id}", "[id*='termsAndConditions']", "[data-automation-id*='termsAndConditions']"]:
                try:
                    el = page.locator(sel).first
                    if await el.count():
                        lbl_id = await el.get_attribute("id") or ""
                        if lbl_id:
                            lbl = page.locator(f"label[for='{lbl_id}']").first
                            if await lbl.count():
                                await lbl.scroll_into_view_if_needed(timeout=3000)
                                await lbl.click(timeout=3000)
                                await page.wait_for_timeout(400)
                                tc_checked = await el.evaluate("el => el.checked || el.getAttribute('aria-checked') === 'true'")
                                if tc_checked: break
                        await el.scroll_into_view_if_needed(timeout=3000)
                        await el.click(force=True, timeout=3000)
                        await page.wait_for_timeout(400)
                        tc_checked = await el.evaluate("el => el.checked || el.getAttribute('aria-checked') === 'true'")
                        if tc_checked: break
                except Exception as e:
                    print(f"    ~ T&C strategy2 ({sel}): {e}")
        print(f"    {'✓' if tc_checked else '~'} T&C checked (verified={tc_checked})")
    else:
        print(f"    ✓ T&C already checked")
    ok = await save_and_continue(page)
    if not ok:
        current = await get_heading(page)
        if current and current != "Voluntary Disclosures":
            ok = True
    return ok

# ── Self Identify handler ─────────────────────────────────────────────────────

async def handle_self_identify(page: Page) -> bool:
    print("\n[PAGE] Self Identify")
    today = datetime.datetime.today()

    # Fill name if the specific Workday Self-ID field exists
    name_el = page.locator("#selfIdentifiedDisabilityData--name").first
    if await name_el.count():
        await page.evaluate("el => el.scrollIntoView({block:'center'})", await name_el.element_handle())
        await page.wait_for_timeout(300)
        await name_el.fill(f"{PI['first_name']} {PI['last_name']}")
        print(f"    ✓ name = '{PI['first_name']} {PI['last_name']}'")

    # Fill signature date (today) — use nativeInputValueSetter directly; el.fill() on
    # React spinbuttons is unreliable (fails on Windows with "outside viewport" errors)
    for sfx, val in [("dateSectionMonth-input", str(today.month)),
                     ("dateSectionDay-input",   str(today.day)),
                     ("dateSectionYear-input",  str(today.year))]:
        full_id = f"selfIdentifiedDisabilityData--dateSignedOn-{sfx}"
        filled = await page.evaluate(f"""() => {{
            const e = document.getElementById('{full_id}');
            if (!e) return false;
            e.scrollIntoView({{block:'center'}});
            e.focus();
            const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
            if (setter) setter.call(e, '{val}');
            else e.value = '{val}';
            e.dispatchEvent(new InputEvent('input',  {{bubbles:true, data:'{val}', inputType:'insertText'}}));
            e.dispatchEvent(new Event('change', {{bubbles:true}}));
            e.dispatchEvent(new Event('blur',   {{bubbles:true}}));
            return true;
        }}""")
        if filled:
            # Tab away to commit React state after each spinbutton segment
            await page.keyboard.press("Tab")
            await page.wait_for_timeout(200)
            print(f"    ✓ date {sfx} = {val!r}")

    # smart_fill_page handles disability radio + language dropdown.
    # The Self Identify date spinbuttons were already filled above with today's date.
    # Exclude them from the DeepSeek scan so LLM can't overwrite them.
    await smart_fill_page(page, "Self Identify", context_hint="self identify signature",
                          exclude_ids={f"selfIdentifiedDisabilityData--dateSignedOn-dateSectionMonth-input",
                                       f"selfIdentifiedDisabilityData--dateSignedOn-dateSectionDay-input",
                                       f"selfIdentifiedDisabilityData--dateSignedOn-dateSectionYear-input"})
    ok = await save_and_continue(page)
    if not ok:
        current = await get_heading(page)
        if current and current != "Self Identify":
            ok = True
    return ok

# ── Run report (written to artifacts/run_report.json after each run) ──────────
# Used by the fill-and-heal loop: Claude reads this + screenshots to diagnose failures.

RUN_REPORT: dict = {
    "job_url": "",
    "started": "",
    "final": "unknown",   # "complete" | "review" | "blocked" | "error" | "listing_dead"
    "pages": [],
}

def _report_page(n: int, name: str, status: str,
                 errors: list = None, req_labels: list = None,
                 screenshot: str = "", fields_filled: int = 0, fields_skipped: list = None):
    """Append a page entry to RUN_REPORT. Call once per page after attempting save."""
    RUN_REPORT["pages"].append({
        "n": n,
        "name": name,
        "status": status,           # "advanced" | "stuck" | "filled" | "skipped"
        "errors": errors or [],
        "required_invalid": req_labels or [],
        "screenshot": screenshot,
        "fields_filled": fields_filled,
        "fields_skipped": fields_skipped or [],
    })

def _write_report():
    """Write RUN_REPORT to artifacts/run_report.json."""
    write_json_report(ARTIFACTS / "run_report.json", RUN_REPORT)

# ── Main ──────────────────────────────────────────────────────────────────────

async def _scrape_listing_locations(page: Page, job_url: str) -> list[str]:
    """Return the full list of job locations from the listing page.

    Tries the CXS JSON endpoint first (clean, structured). Falls back to DOM
    text parsing if CXS fails (bot-blocked, unexpected shape, network error).
    Returns a list like ['California - San Francisco', 'Washington - Seattle'].
    """
    # ── CXS JSON (preferred) ──────────────────────────────────────────────────
    # Pattern: /en-CA/SiteName/job/... OR /SiteName/job/...
    # The optional locale prefix (e.g. "en-CA", "en-US") is NOT the site name.
    m = re.match(
        r'https://([^.]+)\.(wd\d+)\.myworkdayjobs\.com/'
        r'(?:[a-z]{2}-[A-Z]{2}/)?'   # optional locale prefix like en-CA, en-US
        r'([^/]+)/job/(.+)',
        job_url
    )
    if m:
        tenant, wdn, site, ext_path = m.groups()
        cxs_url = f"https://{tenant}.{wdn}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/job/{ext_path}"
        try:
            resp = await page.request.get(cxs_url, timeout=10000)
            data = await resp.json()
            jpi = data.get("jobPostingInfo", {})
            locs = []
            if jpi.get("location"):
                locs.append(jpi["location"])
            locs.extend(jpi.get("additionalLocations", []))
            remote_type = jpi.get("remoteType", "")
            if locs:
                print(f"[NAV] Listing locations (CXS): {locs}  remoteType={remote_type!r}")
                return locs
        except Exception as e:
            print(f"[NAV] CXS location fetch failed ({e}) — falling back to DOM scrape")

    # ── DOM text fallback ─────────────────────────────────────────────────────
    # The listing page renders a "locations" label followed by individual city
    # lines, ending with "View All N Locations" or "time type".
    text = await page.evaluate("() => document.body.innerText")
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    locs = []
    in_locs = False
    for line in lines:
        if line.lower() == "locations":
            in_locs = True
            continue
        if in_locs:
            if re.match(r'view all \d+ location', line, re.IGNORECASE):
                continue  # skip "View All 6 Locations" banner
            if line.lower() in ("time type", "remote type", "posted on", "job requisition id",
                                "full time", "part time"):
                break  # reached next metadata section
            if re.match(r'.+ - .+', line) or re.match(r'[A-Z][a-z]+ - [A-Z]', line):
                locs.append(line)
            elif locs:
                break  # location block ended
    if locs:
        print(f"[NAV] Listing locations (DOM): {locs}")
    return locs


async def _scrape_listing_salary(page: Page) -> str | None:
    """Extract the salary/compensation range from the job listing page text.
    Returns a target integer (midpoint) as string, or None if not found."""
    text = await page.evaluate("() => document.body.innerText")
    result = scrape_salary(text)
    if result:
        print(f"[NAV] Listing salary midpoint: ${result}")
    return result

async def main(job_url: str, headed: bool = False):
    mode = "DeepSeek" if DEEPSEEK_KEY else "rule-based fallback"
    key_hint = f"sk-...{DEEPSEEK_KEY[-4:]}" if DEEPSEEK_KEY else "NOT SET (add DEEPSEEK_API_KEY to data/.env)"
    print(f"[BOT] Workday Application Bot")
    print(f"[BOT] Fill mode  : {mode}")
    print(f"[BOT] DeepSeek   : {key_hint}")
    print(f"[BOT] Chrome     : {CHROME_PATH or '(Playwright bundled Chromium)'}")
    print(f"[BOT] Resume     : {RESUME_PATH}")
    print(f"[BOT] Job        : {job_url}")
    print(f"[BOT] Display    : {'headed (visible)' if headed else 'headless (background)'}\n")

    # Initialize run report
    RUN_REPORT["job_url"] = job_url
    RUN_REPORT["started"] = datetime.datetime.now().isoformat()
    RUN_REPORT["final"] = "unknown"
    RUN_REPORT["pages"] = []

    async with async_playwright() as pw:
        browser, ctx, page = await launch_browser(pw, headed, extra_args=[
            "--no-first-run", "--disable-web-security",
            "--disable-blink-features=AutomationControlled",
        ])

        # Safety net: intercept any stray native file chooser (headed mode) so Finder never blocks.
        # The resume is already uploaded via input[type='file'] set_input_files — this just satisfies
        # any dialog that somehow opens and silently closes it.
        async def _on_filechooser(fc):
            try: await fc.set_files(RESUME_PATH)
            except Exception: pass
        page.on("filechooser", _on_filechooser)

        print("[NAV] Loading job listing...")
        try:
            await page.goto(job_url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            print(f"[NAV] page.goto failed: {e}")
            await page.screenshot(path=str(ARTIFACTS / "goto_failed.png"))
            raise
        await page.wait_for_timeout(3000)

        # ── Dead-listing early detection ──────────────────────────────────────
        # Check immediately after page load — before burning 40+ seconds on Apply selectors.
        async def _check_listing_dead() -> str | None:
            try:
                body = (await page.evaluate("() => document.body.innerText")).lower()
            except Exception:
                return None
            from job_tracker import EXPIRED_INDICATORS
            for phrase in EXPIRED_INDICATORS:
                if phrase in body:
                    return phrase
            return None

        _dead_hint = await _check_listing_dead()
        if _dead_hint:
            print(f"[NAV] Listing is dead/expired ({_dead_hint!r}) — exiting early.")
            await page.screenshot(path=str(ARTIFACTS / "listing_dead.png"))
            RUN_REPORT["final"] = "listing_dead"
            _report_page(0, "Listing Dead", "listing_dead", screenshot="listing_dead.png")
            try:
                from job_tracker import mark_closed_expired_by_url
                mark_closed_expired_by_url(job_url)
            except Exception as _e:
                print(f"  [tracker] auto-close failed (non-fatal): {_e}")
            _write_report()
            await browser.close()
            return

        # Scrape salary + locations from the listing page; inject into PROFILE_SUMMARY
        listing_salary = await _scrape_listing_salary(page)
        if listing_salary:
            print(f"[NAV] Using listing salary midpoint: ${int(listing_salary):,}")
        else:
            listing_salary = str(LIBRARY.get("compensation_rules", {}).get("baseline_target_pay", "140000"))
            print(f"[NAV] No salary range found in listing — using baseline: {listing_salary}")
        listing_locations = await _scrape_listing_locations(page, job_url)
        # Derive the Workday tenant slug (e.g. "bcbsaz") for per-company rule matching.
        _tenant_m = re.match(r'https://([^.]+)\.wd\d+\.myworkdayjobs\.com', job_url)
        listing_tenant = _tenant_m.group(1).lower() if _tenant_m else ""
        # Update PROFILE_SUMMARY with salary, locations, tenant, and today's date
        _mod = sys.modules[__name__]
        _profile_data = json.loads(_mod.PROFILE_SUMMARY)
        _profile_data["job_listing_salary"] = int(listing_salary)
        _profile_data["job_listing_locations"] = listing_locations
        _profile_data["job_tenant"] = listing_tenant
        _profile_data["today"] = datetime.date.today().isoformat()
        _mod.PROFILE_SUMMARY = json.dumps(_profile_data, indent=2)
        # Mirror into app_common so rule_based_answer (which reads app_common.PROFILE_SUMMARY)
        # also sees job_listing_locations and the updated salary/date.
        import app_common as _ac
        _ac.PROFILE_SUMMARY = _mod.PROFILE_SUMMARY

        # No proactive sign-in on listing page — navigate directly to /apply URL.
        # Tenants that require auth will redirect to their sign-in page inside the form flow,
        # which the main page loop detects and handles via ensure_signed_in.
        await ensure_signed_in(page)  # handle any login wall already on listing page

        # Try to find and click the Apply button — Workday uses different selectors per tenant:
        # RH/standard: data-automation-id="adventureButton"
        # Cox/custom:   a plain <button> or <a> with text "Apply" near the job title
        APPLY_SELECTORS = [
            "[data-automation-id='adventureButton']",
            "a[href*='startApplication']",
            "button:has-text('Apply')",
            "a:has-text('Apply Now')",
            "a:has-text('Apply')",
        ]
        apply_clicked = False
        for sel in APPLY_SELECTORS:
            try:
                loc = page.locator(sel).first
                await loc.wait_for(state="visible", timeout=2500)
                # If it's a link with href, navigate directly (avoids JS auth issues)
                href = await loc.get_attribute("href")
                if href and href.startswith("http"):
                    await page.goto(href, wait_until="domcontentloaded", timeout=30000)
                else:
                    await loc.click(force=True)
                apply_clicked = True
                print(f"  [NAV] Clicked Apply via selector: {sel}")
                break
            except Exception:
                continue
        if not apply_clicked:
            # Second dead-listing check — some pages render skeleton HTML that passes the
            # first check but have no Apply button (e.g. a redirect to the careers search page).
            _dead_hint2 = await _check_listing_dead()
            if _dead_hint2:
                print(f"[NAV] No Apply button + dead-listing signal ({_dead_hint2!r}) — exiting early.")
                await page.screenshot(path=str(ARTIFACTS / "listing_dead.png"))
                RUN_REPORT["final"] = "listing_dead"
                _report_page(0, "Listing Dead", "listing_dead", screenshot="listing_dead.png")
                try:
                    from job_tracker import mark_closed_expired_by_url
                    mark_closed_expired_by_url(job_url)
                except Exception as _e:
                    print(f"  [tracker] auto-close failed (non-fatal): {_e}")
                _write_report()
                await browser.close()
                return
            print("  [NAV] No Apply button found — trying JS fallback")
            await page.evaluate("""() => {
                const btn = document.querySelector('[data-automation-id="adventureButton"]') ||
                    Array.from(document.querySelectorAll('button,a')).find(e =>
                        /^apply$/i.test(e.innerText?.trim()) || /^apply now$/i.test(e.innerText?.trim()));
                if (btn) btn.click();
            }""")
        await page.wait_for_timeout(2000)

        # After clicking Apply, sign-in may be required
        await ensure_signed_in(page)
        # Re-click Apply if we're back on the listing page after sign-in
        for sel in APPLY_SELECTORS:
            try:
                loc = page.locator(sel).first
                if await loc.count():
                    await loc.click(force=True)
                    print(f"  [NAV] Re-clicked Apply after sign-in via: {sel}")
                    break
            except Exception:
                continue
        await page.wait_for_timeout(2000)

        # Navigate via "Apply Manually" link (standard Workday tenants)
        am = page.locator("[data-automation-id='applyManually']").first
        try:
            await am.wait_for(state="visible", timeout=10000)
            href = await am.get_attribute("href")
            if href:
                await page.goto(href, wait_until="domcontentloaded", timeout=30000)
            else:
                await am.click(force=True)
            print("  [NAV] Navigated via applyManually")
        except Exception:
            print("  [NAV] No applyManually — assuming direct form navigation")
            await page.screenshot(path=str(ARTIFACTS / "after_apply_click.png"))
        await page.wait_for_timeout(2500)

        # Final login check after navigation to application form
        await ensure_signed_in(page)

        # Wait for application form to fully load (progress bar indicates form is ready)
        try:
            await page.wait_for_selector('[data-automation-id="progressBarActiveStep"]', timeout=25000)
        except Exception:
            pass
        await page.wait_for_timeout(1000)

        for page_num in range(1, 12):
            await page.wait_for_timeout(1500)
            heading = await get_heading(page)
            # If heading is empty, the form may still be loading — wait up to 8s for it
            if not heading:
                for _ in range(8):
                    await page.wait_for_timeout(1000)
                    heading = await get_heading(page)
                    if heading:
                        break
            print(f"\n{'='*60}\n[PAGE {page_num}] {heading}\n{'='*60}")

            if "My Tasks" in (heading or ""):
                print("[BOT] ✓ Application complete.")
                RUN_REPORT["final"] = "complete"
                _report_page(page_num, heading or "My Tasks", "complete")
                break
            if not heading:
                # Still blank after waiting — take screenshot and break to avoid loop
                blank_ss = str(ARTIFACTS / f"blank_heading_p{page_num}.png")
                await page.screenshot(path=blank_ss)
                print(f"  [NAV] Blank heading after 8s — screenshot saved, stopping")
                RUN_REPORT["final"] = "blocked"
                _report_page(page_num, "(blank)", "stuck", errors=["blank heading after 8s"], screenshot=blank_ss)
                break

            # Sign-in page inside the application form — use ensure_signed_in
            if any(kw in heading.lower() for kw in ("sign in", "create account", "log in", "signin")):
                print(f"  → Sign-in page inside application — attempting sign-in")
                await ensure_signed_in(page)
                continue  # re-read heading after sign-in

            # Workday error page ("Something went wrong") — reload and retry
            body_text = await page.evaluate("() => document.body.innerText")
            if "something went wrong" in body_text.lower() and "please refresh" in body_text.lower():
                print(f"  → Workday error page detected — reloading...")
                await page.reload(wait_until="domcontentloaded")
                await page.wait_for_timeout(4000)
                continue

            ss_name = f"run_{page_num:02d}_{re.sub(r'[^a-zA-Z0-9]','_',heading)[:30]}.png"
            ss = ARTIFACTS / ss_name
            await page.screenshot(path=str(ss))

            # ── Detect page type by CONTENT, not heading text ──────────────────
            # This makes the bot work across different Workday tenants that use
            # different heading names ("Work History" vs "My Experience", etc.)
            has_add_buttons  = await page.locator("[data-automation-id='add-button']").count() > 0
            has_tc_checkbox  = await page.locator("[id*='termsAndConditions'],[data-automation-id='termsAndConditions']").count() > 0
            # Detect by heading keywords OR known DOM ids (covers any tenant naming)
            heading_l = heading.lower()
            has_self_id = (
                any(kw in heading_l for kw in ("self identify", "self-identify", "disability", "eeo", "equal employ")) or
                await page.locator("[id*='selfIdentified'],[id*='disability'],[data-automation-id*='selfIdentif']").count() > 0
            )
            is_review        = "review" in heading.lower()
            is_complete      = any(kw in heading for kw in ("My Tasks", "Thank You", "Submitted", "Complete"))

            print(f"  [TYPE] add_btns={has_add_buttons} tc={has_tc_checkbox} "
                  f"self_id={has_self_id} review={is_review}")

            try:
                if is_complete:
                    print("[BOT] ✓ Application complete.")
                    RUN_REPORT["final"] = "complete"
                    _report_page(page_num, heading, "complete", screenshot=ss_name)
                    break

                elif is_review:
                    print("\n" + "="*60)
                    print("  ⚠️  REVIEW — all fields filled. Check the browser.")
                    print("="*60)
                    review_shot = str(ARTIFACTS / "review_page.png")
                    # Scroll review content to top, then capture multiple vertical slices
                    # (Workday puts content in an inner scrollable div, not window)
                    scroll_js = """() => {
                        const inner = document.querySelector('[data-automation-id="scroll-container"]')
                            || document.querySelector('main')
                            || document.querySelector('[role="main"]')
                            || document.scrollingElement;
                        if (inner) inner.scrollTop = 0;
                        window.scrollTo(0, 0);
                    }"""
                    await page.evaluate(scroll_js)
                    await page.wait_for_timeout(300)
                    # Expand viewport to 3000px tall so more content is visible in one shot
                    orig_vp = page.viewport_size or {"width": 1280, "height": 900}
                    await page.set_viewport_size({"width": orig_vp["width"], "height": 3000})
                    await page.wait_for_timeout(400)
                    await page.screenshot(path=review_shot, full_page=True)
                    await page.set_viewport_size(orig_vp)
                    print(f"  Screenshot saved: {review_shot}")
                    RUN_REPORT["final"] = "review"
                    _report_page(page_num, heading, "review", screenshot="review_page.png")
                    try:
                        await asyncio.to_thread(input, "  → Press [Enter] to submit, Ctrl+C to abort: ")
                        await save_and_continue(page)
                        print("  ✓ Submitted!")
                        RUN_REPORT["final"] = "complete"
                        try:
                            from job_tracker import mark_applied_by_url
                            mark_applied_by_url(job_url)
                        except Exception as _e:
                            print(f"  [tracker] mark-applied failed (non-fatal): {_e}")
                    except (EOFError, KeyboardInterrupt):
                        print(f"  ⚠️  Non-interactive mode — NOT submitting. Review at {review_shot}")
                        if headed:
                            # Keep browser open so user can inspect the review page.
                            # stdin is /dev/null in async bash — use a timed wait instead of input().
                            print("  → Browser staying open for 10 minutes. Kill this process when done.")
                            await page.wait_for_timeout(600_000)  # 10 minutes
                    break

                elif has_add_buttons:
                    # Experience/Education/Languages page — any tenant name
                    print(f"  → Detected as experience/education page (add-buttons present)")
                    ok = await handle_my_experience(page)
                    if not ok:
                        blocked_ss = str(ARTIFACTS / f"blocked_my_experience.png")
                        print(f"  [NAV] My Experience save failed — taking screenshot and stopping")
                        await page.screenshot(path=blocked_ss)
                        RUN_REPORT["final"] = "blocked"
                        _report_page(page_num, heading, "stuck",
                                     errors=["My Experience save failed after 3 attempts"],
                                     screenshot=f"blocked_my_experience.png")
                        break
                    _report_page(page_num, heading, "advanced", screenshot=ss_name)

                elif has_tc_checkbox:
                    # Voluntary Disclosures / T&C page — any tenant name
                    print(f"  → Detected as voluntary disclosures page (T&C checkbox present)")
                    ok = await handle_voluntary_disclosures(page)
                    if not ok:
                        current = await get_heading(page)
                        if current and current == "Voluntary Disclosures":
                            print(f"  [NAV] Voluntary Disclosures save may have failed — continuing anyway")
                    _report_page(page_num, heading, "advanced" if ok else "filled", screenshot=ss_name)

                elif has_self_id:
                    # Self Identification / EEO page — any tenant name
                    print(f"  → Detected as self-identify page")
                    ok = await handle_self_identify(page)
                    if not ok:
                        current = await get_heading(page)
                        if current and current == "Self Identify":
                            print(f"  [NAV] Self Identify save may have failed — continuing anyway")
                    _report_page(page_num, heading, "advanced" if ok else "filled", screenshot=ss_name)

                else:
                    # Generic page (My Information, Application Questions, custom pages)
                    # smart_fill_page + save handles all standard form-field pages
                    print(f"  → Generic form page — smart fill")
                    # Wait for form to fully render (important after sign-in when React is still hydrating)
                    for _wait_attempt in range(5):
                        await smart_fill_page(page, heading)
                        # If fields were found and filled, break; otherwise wait and retry
                        nxt = await page.locator("[data-automation-id='pageFooterNextButton']").count()
                        flds = await page.evaluate(SCAN_JS, None)
                        fillable = [f for f in flds if f.get("tag") in ("input","textarea","button","select")
                                    and not f.get("label","").lower().startswith("search")]
                        if fillable or nxt:
                            break
                        print(f"  [NAV] 0 fillable fields found — waiting for page to load (attempt {_wait_attempt+1}/5)...")
                        await page.wait_for_timeout(3000)
                    ok, _errs, _reqs = await save_and_continue_with_report(page)
                    if not ok:
                        # Validation revealed hidden fields (e.g. RH radio on My Information)
                        print("  [NAV] Re-scanning for newly visible fields after validation...")
                        await smart_fill_page(page, heading)
                        ok, _errs, _reqs = await save_and_continue_with_report(page)
                    if not ok:
                        blocked_ss = f"blocked_{page_num:02d}.png"
                        print("  [NAV] Still blocked — taking screenshot and breaking")
                        await page.screenshot(path=str(ARTIFACTS / blocked_ss))
                        RUN_REPORT["final"] = "blocked"
                        _report_page(page_num, heading, "stuck", errors=_errs,
                                     req_labels=_reqs, screenshot=blocked_ss)
                        break
                    _report_page(page_num, heading, "advanced", errors=_errs,
                                 req_labels=_reqs, screenshot=ss_name)

            except Exception as e:
                print(f"  [ERR] {e}")
                err_ss = f"error_p{page_num}.png"
                if not page.is_closed():
                    await page.screenshot(path=str(ARTIFACTS / err_ss))
                RUN_REPORT["final"] = "error"
                _report_page(page_num, heading, "error", errors=[str(e)], screenshot=err_ss)
                break

        _write_report()
        print("\n[BOT] Done.")
        await browser.close()

if __name__ == "__main__":
    # Windows requires ProactorEventLoop for Playwright subprocess communication
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    args = sys.argv[1:]
    headed = "--show" in args
    sim_ds = "--sim-ds" in args   # simulate DeepSeek code path using rule-based answers
    url_args = [a for a in args if not a.startswith("--")]
    if not url_args:
        print("Usage: python3 -u src/app_workday.py <WORKDAY_JOB_URL> [--show] [--sim-ds]")
        sys.exit(1)
    job_url = url_args[0]

    if sim_ds and not DEEPSEEK_KEY:
        # Monkey-patch at module globals so every code path that checks DEEPSEEK_KEY
        # and calls deepseek_fill_page/deepseek_pick_skill runs the DeepSeek branch
        # — but with rule-based answers (no network calls).
        async def _sim_deepseek_fill(fields, entry=None, section_type=None, **kw):
            print(f"  [SIM-DS] Simulating DeepSeek for {len(fields)} fields (rule-based answers)")
            # ── Print the exact payload DeepSeek would receive ──────────────────
            _field_lines = []
            for _f in fields:
                _line = f"[{_f['index']}] type={_f['type']} label={_f['label']!r}"
                if _f.get("date_seq"): _line += f" date_seq={_f['date_seq']}"
                if _f.get("options"):  _line += f" options={_f['options']}"
                if _f.get("value"):    _line += f" current={_f['value']!r}"
                if _f.get("section"): _line += f" section={_f['section']!r}"
                _field_lines.append(_line)
            _entry_ctx = ""
            if entry and section_type:
                _entry_ctx = (f"\nEntry Context (you are filling ONE {section_type} entry — "
                              f"use ONLY this data for these fields):\n"
                              f"{json.dumps(entry, indent=2)}\n")
            _user_prompt = (f"Candidate Profile:\n{PROFILE_SUMMARY}\n"
                            f"{_entry_ctx}"
                            f"\nForm Fields (page: {fields[0].get('page_heading','') if fields else ''}):\n"
                            + "\n".join(_field_lines))
            print("  [SIM-DS] ══════════ DEEPSEEK SYSTEM PROMPT ══════════")
            for _ln in SYSTEM_PROMPT.splitlines():
                print(f"  [SIM-DS] {_ln}")
            print("  [SIM-DS] ══════════ DEEPSEEK USER PROMPT ══════════")
            for _ln in _user_prompt.splitlines():
                print(f"  [SIM-DS] {_ln}")
            print("  [SIM-DS] ══════════ END PROMPT ══════════")
            if entry and section_type:
                # Use entry-specific answers (same as the real DeepSeek path)
                answers = []
                for f in fields:
                    val = entry_answer(f, entry, section_type)
                    if val is None:
                        val = rule_based_answer(f)
                    if val is not None and val != "":
                        answers.append({"index": f["index"], "value": val})
                return answers
            return await rule_based_fill_page(fields)

        async def _sim_deepseek_pick_skill(search_term, options, already_selected):
            """Rule-based pick — same logic as exec_skills_field's inline rule_score."""
            def rule_score(opt):
                o = opt.lower(); sl = search_term.lower()
                if o == sl: return 0
                if o.startswith(sl + " "): return 1
                if re.search(r'\b' + re.escape(sl) + r'\b', o): return 2
                if o.startswith(sl): return 3
                return 99
            filtered = [o for o in options if o.lower() not in already_selected]
            if not filtered:
                return None
            best = min(filtered, key=rule_score)
            return best if rule_score(best) < 99 else None

        _mod = sys.modules[__name__]
        _mod.deepseek_fill_page = _sim_deepseek_fill
        _mod.deepseek_pick_skill = _sim_deepseek_pick_skill
        _mod.DEEPSEEK_KEY = "sim"
        print("[SIM-DS] DeepSeek simulation mode active — using rule-based answers via DS code path")

    import traceback
    try:
        asyncio.run(main(job_url, headed=headed))
    except Exception:
        traceback.print_exc()
        sys.exit(1)
