"""
workday_scan.py — Workday DOM scanning and listing scrapers
============================================================
Functions to inspect the page DOM, extract headings, read validation errors,
normalize URLs, and scrape job listing locations/salaries.
"""

import re
from playwright.async_api import Page
from app_common import scrape_salary


def normalize_workday_url(url: str) -> str:
    """Normalize a Workday URL by converting locale prefixes (fr-CA, fr-FR, etc.) to en-US."""
    if not url:
        return url
    return re.sub(r'(\.myworkdayjobs\.com/)(?:[a-z]{2}-[A-Z]{2}/)?', r'\1en-US/', url)


# ── DOM scanner ───────────────────────────────────────────────────────────────

SCAN_JS = r"""(rootSel) => {
    const root = (rootSel && document.querySelector(rootSel)) || document;
    const isVis = el => {
        const s = window.getComputedStyle(el);
        if (s.display==='none'||s.visibility==='hidden') return false;
        const isCheckOrRadio = el.type === 'radio' || el.type === 'checkbox' || el.getAttribute('role') === 'radio' || el.getAttribute('role') === 'checkbox';
        if (s.opacity==='0' && !isCheckOrRadio) return false;
        const r = el.getBoundingClientRect();
        if (r.width>0 && r.height>0) return true;
        if (isCheckOrRadio && el.parentElement) {
            const pr = el.parentElement.getBoundingClientRect();
            return pr.width > 0 && pr.height > 0;
        }
        return false;
    };
    const getLabel = el => {
        // 1) <label for="id"> anywhere in document (direct 1:1 binding)
        if (el.id) {
            const forLbl = document.querySelector(`label[for="${el.id}"]`);
            if (forLbl && forLbl.innerText.trim()) return forLbl.innerText.trim();
        }

        // 2) aria-labelledby: extract meaningful question text, filtering out generic button values
        const lblBy = el.getAttribute('aria-labelledby');
        if (lblBy) {
            const parts = lblBy.split(' ').map(id => {
                const t = document.getElementById(id);
                if (t && !/^[0-9a-f]{8}-[0-9a-f]{4}/.test(t.innerText.trim()))
                    return t.innerText.trim();
                return '';
            }).filter(Boolean);
            const filteredParts = parts.filter(p => !/^(yes|no|select one|required|\*)$/i.test(p.trim()));
            if (filteredParts.length > 0) {
                return filteredParts.join(' ');
            }
        }

        // 3) Walk up ancestors: within the field's container, find formLabel, label, or legend
        let node = el.parentElement;
        for (let i = 0; i < 8 && node && node !== document.body; i++) {
            // Check formLabel first
            const fls = Array.from(node.querySelectorAll('[data-automation-id="formLabel"]')).filter(isVis);
            if (fls.length === 1 && fls[0].innerText.trim()) {
                return fls[0].innerText.trim();
            } else if (fls.length > 1) {
                // Multi-field outer container: stop walking up
                break;
            }

            const lbls = Array.from(node.querySelectorAll('label, legend')).filter(isVis);
            if (lbls.length === 1 && lbls[0].innerText.trim()) {
                const txt = lbls[0].innerText.trim();
                if (!/^(yes|no|select one)(\s+required)?$/i.test(txt)) return txt;
            } else if (lbls.length > 1) {
                break;
            }

            const sl = node.querySelector('[data-automation-id="formLabelSublabel"]');
            if (sl && sl.innerText.trim()) return sl.innerText.trim();

            node = node.parentElement;
        }

        // 4) explicit aria-label (reject generic button states)
        const al = el.getAttribute('aria-label');
        if (al && al.trim() && !/^(yes|no|select one)(\s+required)?$/i.test(al.trim())) {
            return al.trim();
        }

        // 5) Placeholder attribute
        return el.getAttribute('placeholder') || '';
    };

    const getSection = el => {
        let p = el.parentElement;
        while (p && p !== document.body) {
            const h = p.querySelector('h3,h4,[data-automation-id="groupTitle"]');
            if (h && isVis(h)) return h.innerText.trim();
            p = p.parentElement;
        }
        return '';
    };

    // Helper: extract pill/badge text already selected inside a formField container
    const getPillValue = el => {
        const ff = el.closest('[data-automation-id^="formField"]') || el.closest('[data-uxi-widget-type="selectinput"]');
        if (ff) {
            const pills = Array.from(ff.querySelectorAll('[data-automation-id="selectedItem"]'));
            const visPills = pills.filter(isVis).map(p => p.innerText.trim()).filter(Boolean);
            if (visPills.length) return visPills.join(', ');
            const tagBtns = Array.from(ff.querySelectorAll('[data-automation-id="deleteTag"], [aria-label^="Remove "]'));
            const tags = tagBtns.map(b => b.getAttribute('aria-label')?.replace(/^Remove\s+/i,'').trim()).filter(Boolean);
            if (tags.length) return tags.join(', ');
        }
        return '';
    };

    // Helper: identify date parts (Month, Day, Year) for multi-field date widgets
    const getDatePart = el => {
        // 1) Direct element attributes:
        const auto = (el.getAttribute('data-automation-id') || '').toLowerCase();
        const id = (el.id || '').toLowerCase();
        const name = (el.getAttribute('name') || '').toLowerCase();
        const al = (el.getAttribute('aria-label') || '').toLowerCase();
        const ph = (el.getAttribute('placeholder') || '').toLowerCase();
        const ml = parseInt(el.getAttribute('maxlength')) || 0;

        // Explicit Month indicators
        if (auto.includes('datesectionmonth') || auto.includes('datewidgetmonth') || auto.includes('month-input') || auto === 'month' ||
            id.includes('datesectionmonth') || id.includes('datewidgetmonth') || id.includes('month-input') || id.includes('--month') ||
            name.includes('month') ||
            al === 'month' || al === 'mm' || /^month(\s*\(mm\))?$/i.test(al) ||
            ph === 'mm' || ph === 'month') {
            return 'Month';
        }

        // Explicit Day indicators
        if (auto.includes('datesectionday') || auto.includes('datewidgetday') || auto.includes('day-input') || auto === 'day' ||
            id.includes('datesectionday') || id.includes('datewidgetday') || id.includes('day-input') || id.includes('--day') ||
            name.includes('day') ||
            al === 'day' || al === 'dd' || /^day(\s*\(dd\))?$/i.test(al) ||
            ph === 'dd' || ph === 'day') {
            return 'Day';
        }

        // Explicit Year indicators
        if (auto.includes('datesectionyear') || auto.includes('datewidgetyear') || auto.includes('year-input') || auto === 'year' ||
            id.includes('datesectionyear') || id.includes('datewidgetyear') || id.includes('year-input') || id.includes('--year') ||
            name.includes('year') ||
            al === 'year' || al === 'yyyy' || /^year(\s*\(yyyy\))?$/i.test(al) ||
            ph === 'yyyy' || ph === 'year') {
            return 'Year';
        }

        // 2) Immediate parent/wrapper indicators:
        let p = el.parentElement;
        for (let i = 0; i < 2 && p && p !== document.body; i++) {
            const pAuto = (p.getAttribute('data-automation-id') || '').toLowerCase();
            const pClass = (p.className || '').toLowerCase();
            if (pAuto.includes('month') || pClass.includes('month')) return 'Month';
            if (pAuto.includes('day') || pClass.includes('day')) return 'Day';
            if (pAuto.includes('year') || pClass.includes('year')) return 'Year';
            p = p.parentElement;
        }

        // 3) Sibling analysis within date widget or formField:
        const container = el.closest('[data-automation-id*="dateSection"], [data-automation-id*="dateWidget"], [data-automation-id*="date-"], [data-uxi-widget-type*="date"]') ||
                          el.closest('[data-automation-id^="formField"]');
        if (container) {
            const allInputs = Array.from(container.querySelectorAll('input:not([type="hidden"]):not([type="checkbox"]):not([type="radio"]):not([type="file"])')).filter(isVis);
            const hasDateSignal = container.querySelector('[data-automation-id*="datePicker"], [data-automation-id*="dateSection"], [data-automation-id*="dateWidget"], button[aria-label*="calendar" i], [class*="calendar" i], [class*="datePicker" i]') !== null ||
                                  /date|graduat|birth|dob|start|avail|attend|from|to\b/i.test(container.querySelector('[data-automation-id="formLabel"]')?.innerText || '') ||
                                  allInputs.some(inp => /^(mm|dd|yyyy)$/i.test(inp.getAttribute('placeholder') || '') || (inp.getAttribute('data-automation-id') || '').toLowerCase().includes('date'));
            if (hasDateSignal) {
                const idx = allInputs.indexOf(el);
                if (idx >= 0) {
                    if (allInputs.length === 3) {
                        if (ml === 4 || idx === 2) return 'Year';
                        if (idx === 0) return 'Month';
                        if (idx === 1) return 'Day';
                    } else if (allInputs.length === 2) {
                        if (ml === 4 || idx === 1) return 'Year';
                        if (idx === 0) return 'Month';
                    }
                }
            }
        }

        return '';
    };

    const items = [];

    // A) Native <select> elements
    root.querySelectorAll('select').forEach(el => {
        if (!isVis(el)) return;
        const options = Array.from(el.options).map(o => o.text.trim()).filter(Boolean);
        const sel = el.options[el.selectedIndex];
        items.push({
            el,
            data: {
                tag: 'select', type: 'select', id: el.id || '',
                auto: el.getAttribute('data-automation-id') || '',
                label: getLabel(el), section: getSection(el), options,
                value: sel ? sel.text.trim() : '',
                maxlength: 0, placeholder: '', isSelectInput: false
            }
        });
    });

    // B) Workday custom dropdown BUTTONS (single-select, open on click)
    // Matches standard button dropdowns: promptSearchButton, select-one, dropdown, etc.
    // Explicitly excludes promptIcon (the secondary chevron on selectinputs — those are NOT button-dropdowns)
    root.querySelectorAll([
        'button[data-automation-id="promptSearchButton"]',
        'button[aria-haspopup="listbox"]',
        'button[aria-haspopup="dialog"]:not([data-automation-id="promptIcon"])',
        'button[data-automation-id*="dropdown"]:not([data-automation-id="promptIcon"])',
        'button[data-automation-id*="select-one"]:not([data-automation-id="promptIcon"])',
        'button[data-automation-id*="select"]:not([data-automation-id="promptIcon"])',
        'div[data-automation-id="formField"] button[aria-expanded]:not([data-automation-id="promptIcon"])'
    ].join(',')).forEach(el => {
        if (!isVis(el)) return;
        if (el.closest('[data-automation-id="pageFooter"]')) return;
        // Skip buttons that belong to selectinput widgets (handled in section C via the input)
        if (el.closest('[data-uxi-widget-type="selectinput"]') ||
            el.closest('[data-automation-id*="selectinput"]') ||
            el.closest('[data-automation-id*="selectInput"]')) return;

        // Skip promptIcon buttons (they are helper chevron buttons, not main dropdown buttons)
        if (el.getAttribute('data-automation-id') === 'promptIcon') return;

        const label = getLabel(el);
        if (!label) return;

        // Read current selected value:
        // Priority 1: button's own text if not a generic placeholder
        let value = '';
        const raw = el.innerText.trim();
        const generic = ['select one', 'select', 'choose', 'search', ''];
        if (!generic.includes(raw.toLowerCase())) {
            value = raw;
        } else {
            value = getPillValue(el);
        }

        items.push({
            el,
            data: {
                tag: 'button', type: 'dropdown', id: el.id || '',
                auto: el.getAttribute('data-automation-id') || '',
                label, section: getSection(el), options: [], value,
                maxlength: 0, placeholder: '', isSelectInput: false
            }
        });
    });

    // C) Radio Groups (collect as one entry per radio group)
    const processedRadioGroups = new Set();
    const getRadioGroupLabel = el => {
        const fieldset = el.closest('fieldset');
        const leg = fieldset?.querySelector('legend');
        if (leg && leg.innerText.trim()) return leg.innerText.trim();

        const rg = el.closest('[role="radiogroup"], [aria-labelledby]');
        const lblBy = rg?.getAttribute('aria-labelledby');
        if (lblBy) {
            const t = document.getElementById(lblBy);
            if (t && t.innerText.trim()) return t.innerText.trim();
        }

        let node = el.parentElement;
        for (let i = 0; i < 8 && node && node !== document.body; i++) {
            const fl = node.querySelector('[data-automation-id="formLabel"]');
            if (fl && fl.innerText.trim()) return fl.innerText.trim();
            const l = node.querySelector('legend');
            if (l && l.innerText.trim()) return l.innerText.trim();
            node = node.parentElement;
        }
        return getLabel(el);
    };

    root.querySelectorAll('input[type="radio"], [role="radio"]').forEach(el => {
        if (!isVis(el)) return;
        const groupName = el.name || el.closest('fieldset')?.querySelector('legend')?.innerText || el.closest('[role="radiogroup"]')?.id || el.id;
        if (processedRadioGroups.has(groupName)) return;
        processedRadioGroups.add(groupName);

        const label = getRadioGroupLabel(el);
        const siblings = el.name
            ? Array.from(root.querySelectorAll(`input[name="${el.name}"]`))
            : Array.from((el.closest('fieldset') || el.parentElement).querySelectorAll('[role="radio"], input[type="radio"]'));

        const options = [];
        const radioValues = [];
        let value = '';

        siblings.forEach(sib => {
            let optLbl = '';
            if (sib.id) {
                const l = document.querySelector(`label[for="${sib.id}"]`);
                if (l) optLbl = l.innerText.trim();
            }
            if (!optLbl) {
                const p = sib.parentElement;
                optLbl = p ? p.innerText.trim() : '';
            }
            const optVal = sib.value || optLbl;
            options.push(optLbl);
            radioValues.push(optVal);

            const isChecked = sib.checked || sib.getAttribute('aria-checked') === 'true';
            if (isChecked) value = optLbl || optVal;
        });

        items.push({
            el,
            data: {
                tag: el.tagName.toLowerCase(),
                type: 'radio',
                role: 'radio',
                id: el.id || '',
                name: el.name || '',
                auto: el.getAttribute('data-automation-id') || '',
                label: label,
                section: getSection(el),
                options: options,
                radioValues: radioValues,
                value: value,
                maxlength: 0,
                placeholder: '',
                isSelectInput: false
            }
        });
    });

    // D) Other inputs (text, checkbox, combobox, textarea, selectinput, role=checkbox/switch)
    root.querySelectorAll('input:not([type="radio"]), textarea, [role="checkbox"], [role="switch"]').forEach(el => {
        if (!isVis(el)) return;
        if (el.type === 'hidden') return;
        // Skip native file chooser inputs — resume is uploaded separately via direct file attachment
        if (el.type === 'file') return;
        // Skip search inputs in page navigation/headers
        if (el.getAttribute('data-automation-id') === 'globalSearchInput') return;
        // Skip datepicker popup calendar inputs (internal Workday widgets)
        if (el.getAttribute('data-automation-id') === 'datePickerInput') return;

        // Skip the tiny inner text input of button-dropdowns (promptSearchButton).
        // That hidden/inner input is just an accessibility mirror for the button.
        // The button itself was already scanned in section B above.
        // Exception: DO NOT skip inputs inside selectinput widgets (those are real type-to-search fields).
        const inButtonDropdown = el.closest('[data-automation-id="formField"]')
            ?.querySelector('button[data-automation-id="promptSearchButton"], button[aria-haspopup="listbox"]');
        const inSelectInput = el.closest('[data-uxi-widget-type="selectinput"]') ||
                              el.closest('[data-automation-id*="selectinput"]') ||
                              el.closest('[data-automation-id*="selectInput"]') ||
                              el.getAttribute('role') === 'combobox';
        if (inButtonDropdown && !inSelectInput) return;

        const tag = el.tagName.toLowerCase();
        const role = el.getAttribute('role') || '';
        const rawType = el.type || 'text';
        const isCheck = rawType === 'checkbox' || role === 'checkbox' || role === 'switch';
        const type = isCheck ? 'checkbox' : rawType;
        let label = getLabel(el);
        if (/^(month|day|year|mm|dd|yyyy)\*?$/i.test(label.trim())) {
            let p = el.parentElement;
            let parentQ = '';
            for (let i = 0; i < 8 && p && p !== document.body; i++) {
                const legend = p.querySelector('legend');
                if (legend && isVis(legend) && legend.innerText.trim()) {
                    const lt = legend.innerText.trim();
                    if (!/^(month|day|year|mm|dd|yyyy)\*?$/i.test(lt)) { parentQ = lt; break; }
                }
                const fl = p.querySelector('[data-automation-id="formLabel"]');
                if (fl && isVis(fl) && fl.innerText.trim()) {
                    const flt = fl.innerText.trim();
                    if (!/^(month|day|year|mm|dd|yyyy)\*?$/i.test(flt)) { parentQ = flt; break; }
                }
                const gt = p.querySelector('[data-automation-id="groupTitle"]');
                if (gt && isVis(gt) && gt.innerText.trim()) {
                    const gtt = gt.innerText.trim();
                    if (!/^(month|day|year|mm|dd|yyyy)\*?$/i.test(gtt)) { parentQ = gtt; break; }
                }
                p = p.parentElement;
            }
            if (parentQ) {
                label = `${parentQ.replace(/\*+$/, '').trim()} - ${label.trim()}`;
            }
        }

        const datePart = getDatePart(el);
        if (datePart) {
            if (!label) {
                label = datePart;
            } else {
                const endsWithPart = new RegExp(`(-\\s*|\\b)${datePart}\\s*\\*?$`, 'i').test(label);
                if (!endsWithPart) {
                    label = `${label.replace(/\*+$/, '').trim()} - ${datePart}`;
                }
            }
        }
        const auto = el.getAttribute('data-automation-id') || '';

        let value = '';
        let options = [];
        if (isCheck) {
            const isChecked = el.checked || el.getAttribute('aria-checked') === 'true';
            value = isChecked ? 'true' : 'false';
        } else if (inSelectInput) {
            value = getPillValue(el);
            if (!value) {
                const widget = el.closest('[data-uxi-widget-type="selectinput"]') || el.parentElement;
                const chips = Array.from(widget?.querySelectorAll('[class*="chip"], [class*="tag"], [class*="pill"]') || []);
                const chipTexts = chips.filter(isVis).map(c => c.innerText.trim()).filter(Boolean);
                if (chipTexts.length) value = chipTexts.join(', ');
            }
            if (!value) value = (el.value || '').trim();
        }
        else value = (el.value||'').trim();

        const ml = parseInt(el.getAttribute('maxlength')) || 0;
        const ph = el.getAttribute('placeholder') || '';
        items.push({
            el,
            data: {
                tag, type, role: isCheck ? 'checkbox' : role, id: el.id || '', auto,
                label, section: getSection(el), options, value,
                maxlength: ml, placeholder: ph, isSelectInput: inSelectInput
            }
        });
    });

    // E) Sort all items by document position (top-to-bottom natural reading order)
    items.sort((a, b) => {
        if (a.el === b.el) return 0;
        const pos = a.el.compareDocumentPosition(b.el);
        if (pos & (Node.DOCUMENT_POSITION_PRECEDING | Node.DOCUMENT_POSITION_CONTAINS)) return 1;
        if (pos & (Node.DOCUMENT_POSITION_FOLLOWING | Node.DOCUMENT_POSITION_CONTAINED_BY)) return -1;
        return 0;
    });

    // F) Assign indices and stamp data-fill-idx in document order
    const results = [];
    items.forEach((item, index) => {
        item.el.setAttribute('data-fill-idx', index);
        item.data.index = index;
        results.push(item.data);
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


async def read_validation_errors(page: Page) -> list[str]:
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
                                 + '[data-automation-id*="error" i] li, [role="alert"] li, [aria-live="assertive"] li, '
                                 + '.css-1dbjc4n [role="list"] li').forEach(e => {
            if (e.getBoundingClientRect().height > 0) {
                const t = e.innerText?.trim();
                if (t && (t.startsWith('Error') || t.length > 5)) errs.push(t.slice(0, 120));
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
