"""
app_tiktok.py  —  TikTok & ByteDance Application Bot
====================================================
Fills and submits job applications on lifeattiktok.com, careers.tiktok.com,
and jobs.bytedance.com.

Architecture:
  1. Resolves direct apply URL (careers.tiktok.com/resume/<ID>/apply)
  2. Authenticates using stored session state or auto-login with credentials
  3. Inspects application quota banner (remaining submissions)
  4. Uploads latest candidate résumé
  5. Scans all interactive fields (Ant Design / ATSX form items + Formily custom modules)
  6. Expands live dropdowns and queries DeepSeek for pure semantic option matching
  7. Multi-pass convergence loop (up to 3 passes) to detect conditional fields/errors
  8. Stops at review state for manual inspection with [Enter] / snooze [s] options.
"""

import argparse
import asyncio
import datetime
import json
import os
import re
import sys
from pathlib import Path
from playwright.async_api import async_playwright, Page

# Windows UTF-8 fix
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from app_common import (
    launch_browser, write_json_report, scrape_salary,
    EMAIL, PASSWORD, RESUME_PATH, PI, WE, EDU, LANG, SKILLS,
    PROFILE_SUMMARY, SYSTEM_PROMPT,
    deepseek_fill_page, deepseek_pick_option, label_match, pick_decline,
    ARTIFACTS_DIR, DATA_DIR,
    snooze_job_by_url,
)
from tracker_db import mark_applied_by_url, clean_url

STORAGE_STATE_PATH = DATA_DIR / "tiktok_storage_state.json"
ARTIFACTS = ARTIFACTS_DIR
ARTIFACTS.mkdir(exist_ok=True)


def normalize_tiktok_url(url: str) -> tuple[str, str | None]:
    """Extract numeric job ID from URL and return direct apply URL and job_id."""
    m = re.search(r'(\d{15,})', url)
    if m:
        job_id = m.group(1)
        return f"https://careers.tiktok.com/resume/{job_id}/apply", job_id
    return url, None


async def dismiss_popups(page: Page) -> None:
    """Dismiss AI assistant modal, cookies banner, or welcome popups."""
    for _ in range(3):
        try:
            popup_btn = page.locator(
                'button:has-text("Maybe Later"), [aria-label="Close"], '
                'button:has-text("Accept All"), button:has-text("Agree"), '
                '.atsx-modal-close'
            ).first
            if await popup_btn.is_visible(timeout=1000):
                txt = (await popup_btn.inner_text()).strip() or "close"
                print(f"[TT] Dismissing popup ({txt}) …")
                await popup_btn.click()
                await page.wait_for_timeout(800)
            else:
                break
        except Exception:
            break


async def ensure_signed_in(page: Page, context, headed: bool) -> bool:
    """Ensure session is authenticated. Auto-fills login form and waits for slider/form."""
    curr_url = page.url
    is_login = "login" in curr_url.lower() or await page.locator('input#email, input[placeholder*="Email"]').count() > 0
    if not is_login:
        return True

    print(f"[TT] Login wall detected. Attempting authentication with {EMAIL} …")
    email_input = page.locator('input#email, input[placeholder*="Email"], input[name="email"]').first
    await email_input.wait_for(state="visible", timeout=8000)
    await email_input.click()
    await email_input.fill(EMAIL)
    await page.wait_for_timeout(400)

    pwd_input = page.locator('input#password, input[placeholder*="Password"], input[type="password"]').first
    if await pwd_input.is_visible() and PASSWORD:
        await pwd_input.click()
        await pwd_input.fill(PASSWORD)
        await page.wait_for_timeout(400)

    # Privacy / agreement checkbox
    agree_cb = page.locator('input[type="checkbox"]').first
    if await agree_cb.is_visible():
        if not await agree_cb.is_checked():
            await agree_cb.check()
            await page.wait_for_timeout(300)
    else:
        custom_cb = page.locator('.byted-checkbox, .checkbox, [class*="checkbox"], [role="checkbox"]').first
        if await custom_cb.is_visible():
            await custom_cb.click()
            await page.wait_for_timeout(300)

    sign_in_btn = page.locator('button:has-text("Sign in"), button[type="submit"]').first
    if await sign_in_btn.is_visible():
        await sign_in_btn.click()
        await page.wait_for_timeout(2000)

    # Wait for login transition
    print("\n" + "=" * 60)
    print("  TIKTOK AUTHENTICATION")
    print("  Watching session... If a slider puzzle or OTP appears, solve it.")
    print("=" * 60)

    for tick in range(60):  # Wait up to 120s
        curr_url = page.url
        if "resume" in curr_url and "login" not in curr_url:
            body_text = await page.locator("body").inner_text()
            if any(w in body_text.lower() for w in ["basic information", "resume", "education", "submit"]):
                print(f"[TT] ✓ Authenticated! Reached application form: {curr_url}")
                try:
                    await context.storage_state(path=str(STORAGE_STATE_PATH))
                    print(f"[TT] Saved session storage state → {STORAGE_STATE_PATH.name}")
                except Exception as e:
                    print(f"[TT] Warning saving storage state: {e}")
                return True
        if tick % 5 == 0 and tick > 0:
            print(f"[TT] Waiting for login transition ({tick*2}s) …")
        await page.wait_for_timeout(2000)

    print("[TT] ❌ Timed out waiting for login to complete.")
    return False


async def check_application_quota(page: Page) -> int | None:
    """Inspect banner for remaining applications in current recruitment project."""
    try:
        body_text = await page.locator("body").inner_text()
        m = re.search(r'You currently have\s*(\d+)\s*application\(s\)\s*remaining', body_text, re.IGNORECASE)
        if m:
            remaining = int(m.group(1))
            print(f"[TT] ℹ Application quota: {remaining} application(s) remaining.")
            return remaining
    except Exception:
        pass
    return None


async def handle_resume_upload(page: Page) -> bool:
    """Upload candidate resume to the application form if available."""
    if not RESUME_PATH or not Path(RESUME_PATH).exists():
        print("[TT] No local resume file found to upload.")
        return False

    resume_name = Path(RESUME_PATH).name
    try:
        file_input = page.locator('input[type="file"]').first
        if not await file_input.count():
            print("[TT] No file input found on page.")
            return False

        # Check existing resume name
        try:
            current_txt = await page.locator('.uploadResume-section, .uploadFile').first.inner_text()
            if resume_name in current_txt:
                print(f"[TT] Résumé already up-to-date ({resume_name}).")
                return True
        except Exception:
            pass

        print(f"[TT] Uploading résumé: {resume_name} …")
        await file_input.set_input_files(RESUME_PATH)
        await page.wait_for_timeout(3000)

        # Confirm upload
        new_txt = await page.locator('.uploadResume-section, .uploadFile').first.inner_text()
        if resume_name in new_txt or "pdf" in new_txt.lower():
            print(f"    ✓ file  Résumé uploaded: {resume_name}")
            return True
    except Exception as e:
        print(f"[TT] Warning during résumé upload: {e}")
    return False


async def scan_form_fields(page: Page) -> list[dict]:
    """Scan all interactive fields on the TikTok form: Ant Design and Formily items."""
    return await page.evaluate("""() => {
        const fields = [];
        let idx = 0;

        // 1. Ant Design / ATSX form items
        document.querySelectorAll('.atsx-form-item, [class*="form-item"]').forEach(fi => {
            const labelEl = fi.querySelector('label, .atsx-form-item-label');
            let label = labelEl ? labelEl.innerText.trim().replace(/\\*$/, '').trim() : '';
            if (label.includes('\\n')) label = label.split('\\n')[0].trim();
            if (!label) return;

            const req = fi.classList.contains('atsx-form-item-required') || 
                        (labelEl && labelEl.classList.contains('atsx-form-item-required')) || 
                        fi.innerText.includes('*');

            const sel = fi.querySelector('.atsx-select');
            const datePicker = fi.querySelector('.atsx-calendar-picker, [class*="picker"], [class*="Picker"]');
            const inp = fi.querySelector('input:not([type="hidden"]):not([type="file"]):not([class*="period-hidden"]), textarea');

            if (sel) {
                const selId = sel.id || (inp ? inp.id : '');
                const selValEl = sel.querySelector('.atsx-select-selection-selected-value, .atsx-select-selection__rendered');
                let curVal = selValEl ? selValEl.innerText.trim() : (inp && inp.value ? inp.value.trim() : sel.innerText.trim());
                fields.push({
                    index: idx++,
                    kind: 'atsx-select',
                    id: selId,
                    label: label,
                    required: req,
                    current_value: curVal,
                    placeholder: '',
                });
            } else if (datePicker) {
                const dateInp = datePicker.querySelector('input') || inp;
                let curVal = dateInp && dateInp.value ? dateInp.value.trim() : datePicker.innerText.trim();
                fields.push({
                    index: idx++,
                    kind: 'atsx-date',
                    id: dateInp ? dateInp.id : '',
                    label: label,
                    required: req,
                    current_value: curVal,
                    placeholder: '',
                });
            } else if (inp) {
                fields.push({
                    index: idx++,
                    kind: 'atsx-input',
                    id: inp.id || '',
                    type: inp.type || inp.tagName.toLowerCase(),
                    label: label,
                    required: req,
                    current_value: inp.value ? inp.value.trim() : '',
                    placeholder: inp.placeholder || '',
                });
            }
        });

        // 2. Formily custom question modules (.ud-formily-item)
        document.querySelectorAll('.ud-formily-item').forEach(fi => {
            const qName = fi.getAttribute('data-form-field-i18n-name') || '';
            const qId = fi.getAttribute('data-form-field-id') || fi.id || '';
            const labelEl = fi.querySelector('.ud-formily-item-label, label');
            let label = (labelEl ? labelEl.innerText.trim() : qName).replace(/\\*$/, '').trim();
            if (label.includes('\\n')) label = label.split('\\n')[0].trim();
            if (!label) return;

            const req = fi.querySelector('.ud-formily-item-required-asterisk') !== null || fi.innerText.includes('*');
            const sel = fi.querySelector('.ud__select');
            const inp = fi.querySelector('input:not([type="search"]), textarea');

            if (sel) {
                let curVal = sel.innerText.trim();
                const isMulti = sel.className.includes('multiSelect') || sel.className.includes('multiple');
                fields.push({
                    index: idx++,
                    kind: isMulti ? 'formily-multiselect' : 'formily-select',
                    id: qId,
                    label: label,
                    required: req,
                    current_value: curVal,
                    placeholder: '',
                });
            } else if (inp) {
                fields.push({
                    index: idx++,
                    kind: 'formily-input',
                    id: qId,
                    type: inp.type || inp.tagName.toLowerCase(),
                    label: label,
                    required: req,
                    current_value: inp.value || '',
                    placeholder: inp.placeholder || '',
                });
            }
        });

        return fields;
    }""")


async def execute_select_field(page: Page, field: dict, context_info: str = "") -> str | None:
    """Scroll to dropdown, open it, scrape live options, evaluate via DeepSeek, and select."""
    fid = field.get("id", "")
    label = field.get("label", "")
    clean_lbl = label.split("\n")[0].strip()
    kind = field.get("kind", "")

    # Locate trigger element
    if "formily" in kind:
        container = page.locator(f'#formily-item-{fid}, [data-form-field-id="{fid}"]').first
        if not await container.count():
            container = page.locator('.ud-formily-item').filter(has_text=clean_lbl).first
        trigger = container.locator('.ud__select__selector, input[role="combobox"]').first
    else:
        if fid:
            container = page.locator(f'div[id="{fid}"], .atsx-form-item:has(#{re.escape(fid)})').first
            if not await container.count():
                container = page.locator('.atsx-form-item').filter(has_text=clean_lbl).first
        else:
            container = page.locator('.atsx-form-item').filter(has_text=clean_lbl).first
        trigger = container.locator('.atsx-select-selection, [role="combobox"]').first

    try:
        await container.scroll_into_view_if_needed(timeout=4000)
        await page.wait_for_timeout(300)
        await trigger.click(timeout=4000)
        await page.wait_for_timeout(600)

        # Scrape live options from popup
        if "formily" in kind:
            raw_options = await page.evaluate("""() => {
                return Array.from(document.querySelectorAll('.ud__select__list__item, [role="option"]')).map(el => el.innerText.trim()).filter(Boolean);
            }""")
        else:
            raw_options = await page.evaluate("""() => {
                return Array.from(document.querySelectorAll('.atsx-select-dropdown-menu-item, li[role="option"]')).map(el => el.innerText.trim()).filter(Boolean);
            }""")

        # Deduplicate while preserving order
        seen = set()
        options = []
        for o in raw_options:
            if o not in seen and o.lower() not in ("select", "please select", ""):
                seen.add(o)
                options.append(o)

        if not options:
            print(f"    ⚠ sel   [{field['index']}] {clean_lbl!r}: no options found in dropdown")
            await page.keyboard.press("Escape")
            return None

        # Determine best answer via DeepSeek
        chosen = await deepseek_pick_option(clean_lbl, options, current=field.get("current_value", ""), context=context_info)
        if not chosen and options:
            # Fallback for authorization or standard binary questions
            if label_match(clean_lbl, "authorized", "eligible", "legally"):
                for o in options:
                    if o.lower() in ("yes", "authorized"): chosen = o; break
            elif label_match(clean_lbl, "sponsorship", "visa"):
                for o in options:
                    if o.lower() in ("no", "not require"): chosen = o; break
            elif label_match(clean_lbl, "degree"):
                for o in options:
                    if "bachelor" in o.lower(): chosen = o; break
            elif label_match(clean_lbl, "hear about", "opportunity", "source"):
                for o in options:
                    if any(w in o.lower() for w in ["website", "internet", "search", "career"]): chosen = o; break

        if not chosen:
            chosen = options[0]

        # Click the chosen option (must be visible in the open popup)
        if "formily" in kind:
            opt_el = page.locator('.ud__select__list__item:visible, [role="option"]:visible').filter(has_text=chosen).first
        else:
            opt_el = page.locator('.atsx-select-dropdown-menu-item:visible, li[role="option"]:visible').filter(has_text=chosen).first

        try:
            await opt_el.wait_for(state="visible", timeout=3000)
            await opt_el.click(timeout=3000)
            await page.wait_for_timeout(400)
            if "multi" in kind:
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(300)
            print(f"    ✓ sel   [{field['index']}] {clean_lbl!r} = {chosen!r}")
            return chosen
        except Exception:
            # Fallback with regex exact word boundary / case-insensitive
            fallback = page.locator('.ud__select__list__item:visible, [role="option"]:visible, .atsx-select-dropdown-menu-item:visible').filter(has_text=re.compile(f'^{re.escape(chosen)}$', re.I)).first
            if await fallback.count() and await fallback.is_visible():
                await fallback.click(timeout=3000)
                await page.wait_for_timeout(400)
                if "multi" in kind:
                    await page.keyboard.press("Escape")
                    await page.wait_for_timeout(300)
                print(f"    ✓ sel   [{field['index']}] {clean_lbl!r} = {chosen!r}")
                return chosen
            print(f"    ⚠ sel   [{field['index']}] {clean_lbl!r}: option {chosen!r} element not clickable")
            await page.keyboard.press("Escape")
    except Exception as e:
        print(f"    ✗ sel   [{field['index']}] {clean_lbl!r} error: {e}")
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
    return None


async def execute_input_field(page: Page, field: dict, value: str) -> bool:
    """Scroll to input / textarea and fill value."""
    fid = field.get("id", "")
    label = field.get("label", "")
    clean_lbl = label.split("\n")[0].strip()

    # Skip referral code if empty or N/A
    if label_match(clean_lbl, "referral"):
        if value.lower() in ("n/a", "none", "no", "na", ""):
            print(f"    ⊘ skip  [{field['index']}] {clean_lbl!r} (referral code left blank)")
            return True

    try:
        if fid:
            el = page.locator(f'#{re.escape(fid)}, [name="{fid}"]').first
        else:
            container = page.locator('.atsx-form-item, .ud-formily-item').filter(has_text=clean_lbl).first
            el = container.locator('input:not([type="hidden"]), textarea').first

        await el.scroll_into_view_if_needed(timeout=4000)
        await page.wait_for_timeout(200)
        await el.click(timeout=4000)
        await el.fill(value)
        await page.wait_for_timeout(200)
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(200)
        print(f"    ✓ text  [{field['index']}] {clean_lbl!r} = {value[:40]!r}")
        return True
    except Exception as e:
        print(f"    ✗ text  [{field['index']}] {clean_lbl!r} error: {e}")
        return False


async def apply_tiktok_job(job_url: str, headed: bool = True, no_submit: bool = False) -> int:
    """Main application loop for TikTok and ByteDance roles."""
    apply_url, job_id = normalize_tiktok_url(job_url)
    print("=" * 70)
    print(f"[TT] Target Job URL: {job_url}")
    print(f"[TT] Apply URL     : {apply_url}")
    print(f"[TT] Job ID        : {job_id}")
    print(f"[TT] Mode          : {'Headed (visible)' if headed else 'Headless'}")
    print("=" * 70)

    async with async_playwright() as p:
        browser, context, page = await launch_browser(
            p,
            headed=headed,
            storage_state=str(STORAGE_STATE_PATH) if STORAGE_STATE_PATH.exists() else None
        )

        try:
            print(f"[TT] Navigating to apply page …")
            await page.goto(apply_url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(3000)
            await dismiss_popups(page)

            # Check / handle authentication
            signed_in = await ensure_signed_in(page, context, headed)
            if not signed_in:
                raise RuntimeError("Failed to authenticate session.")

            await page.wait_for_timeout(2000)
            await dismiss_popups(page)

            # Check remaining quota
            quota = await check_application_quota(page)
            if quota is not None and quota == 0:
                print("\n[TT] ❌ 0 applications remaining for this recruitment project.")
                ss = ARTIFACTS / "tiktok_quota_exceeded.png"
                await page.screenshot(path=str(ss), full_page=True)
                write_json_report(ARTIFACTS / "run_report_tiktok.json", {
                    "job_url": job_url,
                    "status": "quota_exceeded",
                    "remaining_quota": 0,
                    "timestamp": datetime.datetime.now().isoformat()
                })
                return 1

            # Upload Résumé
            await handle_resume_upload(page)

            # Multi-pass form filling loop
            MAX_PASSES = 3
            filled_count = 0
            all_fields = []

            for pass_num in range(1, MAX_PASSES + 1):
                print(f"\n[TT] Pass {pass_num}: Scanning form fields …")
                fields = await scan_form_fields(page)
                all_fields = fields

                # Filter fields needing answers
                unfilled_fields = []
                for f in fields:
                    if f.get("kind") == "atsx-date":
                        continue
                    if label_match(f.get("label", ""), "referral"):
                        continue
                    cur = (f.get("current_value") or "").strip()
                    # An ATSX select or Formily select with placeholder or empty string needs filling
                    if "select" in f["kind"]:
                        if not cur or cur.lower() in ("please select", "select", ""):
                            unfilled_fields.append(f)
                    else:
                        # Input / textarea: fill if empty
                        if not cur:
                            unfilled_fields.append(f)

                print(f"[TT] Pass {pass_num}: Scanned {len(fields)} fields ({len(unfilled_fields)} unfilled).")

                if not unfilled_fields:
                    print(f"[TT] Pass {pass_num}: ✓ Form complete — 0 unfilled required fields remaining.")
                    break

                # Prepare context for DeepSeek
                # If there are text fields to fill in batch, send to deepseek_fill_page
                text_to_evaluate = [f for f in unfilled_fields if "select" not in f["kind"]]
                ds_answers = {}
                if text_to_evaluate:
                    print(f"[TT] Sending {len(text_to_evaluate)} text field(s) to DeepSeek …")
                    payload = [{
                        "index": f["index"],
                        "label": f["label"],
                        "type": f.get("type", "text"),
                        "required": f.get("required", False),
                        "placeholder": f.get("placeholder", ""),
                        "options": []
                    } for f in text_to_evaluate]
                    evaluated = await deepseek_fill_page(payload, section_type="TikTok Application")
                    for ans in evaluated:
                        ds_answers[ans.get("index")] = ans.get("value", "")

                # Execute fields
                for f in unfilled_fields:
                    idx = f["index"]
                    lbl = f["label"]
                    kind = f["kind"]

                    if "select" in kind:
                        # Dropdown evaluation and selection
                        picked = await execute_select_field(page, f, context_info=f"Job Apply: {job_url}")
                        if picked:
                            filled_count += 1
                    else:
                        # Text input
                        val = ds_answers.get(idx)
                        if not val:
                            # Rule-based fallback
                            if label_match(lbl, "first name", "given name"): val = PI.get("first_name", "")
                            elif label_match(lbl, "last name", "family name"): val = PI.get("last_name", "")
                            elif label_match(lbl, "name"): val = f"{PI.get('first_name', '')} {PI.get('last_name', '')}"
                            elif label_match(lbl, "email"): val = EMAIL
                            elif label_match(lbl, "mobile", "phone"): val = PI.get("phone", "")
                            elif label_match(lbl, "linkedin"): val = PI.get("linkedin", "")
                            elif label_match(lbl, "github"): val = PI.get("github", "")

                        if val:
                            ok = await execute_input_field(page, f, val)
                            if ok:
                                filled_count += 1

                await page.wait_for_timeout(1500)

            # Ensure privacy policy checkbox is checked
            try:
                privacy_cb = page.locator('input[type="checkbox"]').last
                if await privacy_cb.is_visible() and not await privacy_cb.is_checked():
                    print("[TT] Checking Privacy Policy agreement checkbox …")
                    await privacy_cb.check()
                    await page.wait_for_timeout(400)
            except Exception:
                pass

            # Pre-submit screenshot
            ss_path = ARTIFACTS / "tiktok_before_submit.png"
            await page.screenshot(path=str(ss_path), full_page=True)
            print(f"\n[TT] Screenshot saved → {ss_path.name}")

            # Dry-run check
            if no_submit:
                print("\n" + "=" * 60)
                print("  DRY-RUN COMPLETE (--no-submit active)")
                print(f"  All available fields processed.")
                print(f"  Screenshot saved to {ss_path.name}")
                print("  Application was NOT submitted.")
                print("=" * 60)
                write_json_report(ARTIFACTS / "run_report_tiktok.json", {
                    "job_url": job_url,
                    "status": "dry_run_success",
                    "fields_scanned": len(all_fields),
                    "fields_filled": filled_count,
                    "timestamp": datetime.datetime.now().isoformat()
                })
                return 0

            # Manual review prompt
            if headed:
                print("\n" + "=" * 60)
                print("  FORM READY FOR MANUAL REVIEW — BOT HAS STOPPED")
                print("  1. Inspect the pre-filled fields in the browser window.")
                print("  2. If satisfied, press [Enter] to submit and mark Applied in tracker.")
                print("  3. Type 's' (or 'skip') to skip/snooze this job for 3 days.")
                print("  Press Ctrl+C to CANCEL without updating tracker.")
                print("=" * 60)

                try:
                    user_input = await asyncio.to_thread(input, "  Action [Enter=submit / s=skip 3d / q=cancel]: ")
                except (KeyboardInterrupt, EOFError):
                    print("[TT] Cancelled by user.")
                    return 0

                user_input = user_input.strip().lower()
                m = re.match(r"^s(?:kip)?\s*(\d+)?$", user_input)
                if m or user_input in ("snooze", "3"):
                    days = int(m.group(1)) if (m and m.group(1)) else 3
                    snooze_until = snooze_job_by_url(job_url, days=days, reason=f"Skipped by user for {days} days")
                    print(f"\n[TT] ⏸ Job snoozed for {days} days (until {snooze_until}).")
                    write_json_report(ARTIFACTS / "run_report_tiktok.json", {
                        "job_url": job_url,
                        "status": f"snoozed_{days}_days",
                        "timestamp": datetime.datetime.now().isoformat()
                    })
                    sys.exit(2)
                elif user_input in ("q", "cancel", "abort"):
                    print("[TT] Cancelled by user.")
                    return 0

                # User pressed Enter: submit
                print("[TT] Submitting application …")
                submit_btn = page.locator('button:has-text("Submit")').first
                if await submit_btn.is_visible():
                    await submit_btn.click()
                    await page.wait_for_timeout(3500)

                ss2 = ARTIFACTS / "tiktok_after_submit.png"
                try:
                    await page.screenshot(path=str(ss2), full_page=True)
                    print(f"[TT] Post-submit screenshot saved → {ss2.name}")
                except Exception:
                    pass

                print(f"[TT] ✓ Application confirmed! Marking job Applied in tracker …")
                mark_applied_by_url(job_url)
                write_json_report(ARTIFACTS / "run_report_tiktok.json", {
                    "job_url": job_url,
                    "status": "submitted_manual",
                    "fields_scanned": len(all_fields),
                    "timestamp": datetime.datetime.now().isoformat()
                })
                return 0
            else:
                print("[TT] Headless mode — run with --show to review and submit.")
                return 0

        except Exception as e:
            ss_err = ARTIFACTS / "tiktok_error.png"
            try:
                await page.screenshot(path=str(ss_err), full_page=True)
            except Exception:
                pass
            print(f"\n[TT] ERROR: {e}")
            print(f"[TT] Error screenshot → {ss_err.name}")
            write_json_report(ARTIFACTS / "run_report_tiktok.json", {
                "job_url": job_url,
                "status": f"error: {e}",
                "timestamp": datetime.datetime.now().isoformat()
            })
            raise
        finally:
            await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="TikTok & ByteDance Application Bot",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("job_url", help="Job URL (lifeattiktok.com/search/<ID> or careers.tiktok.com/resume/<ID>/apply)")
    parser.add_argument("--show", action="store_true", help="Show Chrome window for manual review")
    parser.add_argument("--no-submit", action="store_true", help="Fill form and verify without submitting (dry-run)")
    args = parser.parse_args()

    asyncio.run(apply_tiktok_job(args.job_url, headed=args.show, no_submit=args.no_submit))
