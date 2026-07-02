"""
app_greenhouse.py  —  Greenhouse Application Bot
==================================================
Fills Greenhouse job applications (job-boards.greenhouse.io / boards.greenhouse.io
and embedded ?gh_jid= company pages).

Architecture:
  1. Resolve the canonical Greenhouse apply URL (direct or via gh_jid embed detection)
  2. Navigate to the apply form
  3. Click Apply button if present; detect and enter Greenhouse iframe if present
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
from pathlib import Path
from urllib.parse import urlparse, parse_qsl

from playwright.async_api import async_playwright, Page

# Shared infrastructure
from app_common import (
    RESUME_PATH, DEEPSEEK_KEY,
    PROFILE_SUMMARY,
    deepseek_fill_page, rule_based_fill_fields, fuzzy_pick,
    ARTIFACTS_DIR,
    launch_browser,
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

    For embedded pages we try to construct the direct board URL so the apply
    form loads cleanly without the company site's nav chrome.
    """
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

        // ── Text / email / tel / number / url / textarea ──────────────────
        const textEls = document.querySelectorAll(
            'input[type="text"], input[type="email"], input[type="tel"],' +
            'input[type="number"], input[type="url"], input:not([type]), textarea'
        );
        for (const el of textEls) {
            if (!isVisible(el)) continue;
            // Skip hidden / submit / button inputs
            if (['hidden','submit','button','file','checkbox','radio'].includes(el.type)) continue;
            el.dataset.ghIdx = idx;
            fields.push({
                index:   idx++,
                tag:     el.tagName.toLowerCase(),
                type:    el.type || 'text',
                id:      el.id || '',
                name:    el.name || '',
                label:   getLabel(el),
                section: getSection(el),
                value:   el.value || '',
                options: [],
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
            fields.push({
                index:   idx++,
                tag:     'select',
                type:    'select-one',
                id:      el.id || '',
                name:    el.name || '',
                label:   getLabel(el),
                section: getSection(el),
                value:   el.options[el.selectedIndex]?.text.trim() || '',
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
                radioGroups[key] = { el, texts: [], values: [] };
            }
            const lbl = document.querySelector('label[for="' + el.id + '"]');
            radioGroups[key].texts.push(lbl ? lbl.innerText.trim() : el.value);
            radioGroups[key].values.push(el.value);
        }
        for (const [key, grp] of Object.entries(radioGroups)) {
            grp.el.dataset.ghIdx = idx;
            fields.push({
                index:       idx++,
                tag:         'input',
                type:        'radio',
                id:          grp.el.id || '',
                name:        grp.el.name || key,
                label:       getLabel(grp.el),
                section:     getSection(grp.el),
                value:       '',
                options:     grp.texts,
                radioValues: grp.values,
                role:        'radio',
            });
        }

        // ── Checkboxes (individual — not radio-style) ──────────────────
        const checkboxEls = document.querySelectorAll('input[type="checkbox"]');
        for (const el of checkboxEls) {
            if (!isVisible(el)) continue;
            el.dataset.ghIdx = idx;
            fields.push({
                index:   idx++,
                tag:     'input',
                type:    'checkbox',
                id:      el.id || '',
                name:    el.name || '',
                label:   getLabel(el),
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
        await el.click(click_count=3, timeout=5000)
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


async def gh_exec_select(page: Page, field: dict, value: str, target=None):
    target = target or _tgt(page)
    idx  = field["index"]
    opts = field.get("options", [])
    match = fuzzy_pick(opts, value) or value
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
    match = fuzzy_pick(opts, value) or (opts[0] if opts else value)
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
        file_input = target.locator('input[type="file"]').first
        if not await file_input.is_visible(timeout=3000):
            print("  [file] no file input visible — form may not be open")
            # Continue anyway — do not abort the run
        else:
            await file_input.set_input_files(resume_path)
            print(f"    ✓ file  Résumé uploaded: {Path(resume_path).name}")
    except Exception as e:
        print(f"    ~ file  Upload failed: {e}")


async def execute_answer(page: Page, field: dict, value: str, target=None):
    if not value:
        return
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
        else:
            await gh_exec_text(page, field, value, target=target)
    except Exception as e:
        print(f"    ~ err   [{field['index']}] {field['label']!r}: {e}")


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


# ── Main applicator ───────────────────────────────────────────────────────────

async def main(job_url: str, headed: bool = False):
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
                "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer":         "https://www.google.com/",
            },
        )

        try:
            print("[GH] Navigating to apply form …")
            await page.goto(apply_url, timeout=45000, wait_until="networkidle")
            await page.wait_for_timeout(2000)

            # ── Step 1: Click Apply button if present ─────────────────────────
            for selector in [
                "button:has-text(\"Apply\")",
                "#apply_button",
                "a:has-text(\"Apply for this job\")",
                "a:has-text(\"Apply Now\")",
            ]:
                try:
                    btn = page.locator(selector).first
                    if await btn.is_visible(timeout=2000):
                        await btn.click()
                        await page.wait_for_timeout(1500)
                        break
                except Exception:
                    pass

            # ── Step 2: Detect Greenhouse embed iframe ────────────────────────
            _frame = None
            for iframe_sel in [
                "iframe[src*='greenhouse']",
                "#grnhse_iframe",
                "iframe[src*='boards.greenhouse']",
            ]:
                try:
                    el = page.locator(iframe_sel).first
                    if await el.is_visible(timeout=2000):
                        _frame = await el.content_frame()
                        print(f"[GH] Detected Greenhouse iframe ({iframe_sel})")
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

            # Scan fields
            fields = await scan_fields(page)
            print(f"[GH] Scanned {len(fields)} fields")

            # Resolve answers
            if DEEPSEEK_KEY:
                print("[GH] Sending fields to DeepSeek …")
                answers = await deepseek_fill_page(fields, profile_override=runtime_profile)
                print(f"[GH] DeepSeek returned {len(answers)} answers")
                # Merge: rule-based fills gaps DeepSeek left
                ds_indices = {a["index"] for a in answers}
                rule_answers = rule_based_fill_fields(fields)
                for ra in rule_answers:
                    if ra["index"] not in ds_indices:
                        answers.append(ra)
            else:
                print("[GH] Rule-based fallback (no DeepSeek key) …")
                answers = rule_based_fill_fields(fields)

            # Build index → value map
            answer_map = {a["index"]: a["value"] for a in answers}
            print(f"[GH] {len(answer_map)} fields to fill")

            # Execute answers
            filled = 0
            for field in fields:
                val = answer_map.get(field["index"])
                if val:
                    await execute_answer(page, field, val, target=target)
                    filled += 1

            print(f"\n[GH] Filled {filled}/{len(fields)} fields.")

            # Screenshot before pause
            ss_path = ARTIFACTS / "gh_before_submit.png"
            await page.screenshot(path=str(ss_path), full_page=True)
            print(f"[GH] Screenshot saved → {ss_path.name}")

            _write_report(job_url, "ready_to_submit", filled, len(fields))

            # ── PAUSE — do NOT auto-submit ────────────────────────────────────
            print("\n" + "="*60)
            print("  REVIEW COMPLETE — BOT HAS STOPPED")
            print("  Open the browser window to inspect / correct any fields.")
            print("  Press [Enter] here when ready to SUBMIT the application.")
            print("  Press Ctrl+C to CANCEL without submitting.")
            print("="*60)

            if headed:
                try:
                    await asyncio.to_thread(input, "")
                except (KeyboardInterrupt, EOFError):
                    print("[GH] Cancelled — application NOT submitted.")
                    _write_report(job_url, "cancelled", filled, len(fields))
                    return

                # Click submit
                submitted = await page.evaluate("""() => {
                    const btn = Array.from(document.querySelectorAll('button[type="submit"], input[type="submit"]'))
                        .find(b => /submit|apply/i.test(b.innerText || b.value || ''));
                    if (btn) { btn.click(); return true; }
                    return false;
                }""")
                if submitted:
                    await page.wait_for_timeout(3000)
                    ss2 = ARTIFACTS / "gh_after_submit.png"
                    await page.screenshot(path=str(ss2), full_page=True)
                    print(f"[GH] ✓ Submitted! Screenshot → {ss2.name}")
                    _write_report(job_url, "submitted", filled, len(fields))
                else:
                    print("[GH] Could not find submit button — submit manually in the browser.")
                    _write_report(job_url, "submit_button_not_found", filled, len(fields))
            else:
                # Headless: just report ready; user can re-run with --show to submit
                print("[GH] Headless mode — run with --show to review and submit.")
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
    args = parser.parse_args()
    asyncio.run(main(args.job_url, headed=args.show))
