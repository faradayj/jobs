"""
app_workday.py  —  Workday Application Bot
==============================================
Fills and submits Workday job applications (*.myworkdayjobs.com).

Architecture:
  1. Navigate to job listing URL
  2. For each page: scan ALL visible fields with labels + options (workday_scan)
  3. Send batch to DeepSeek → [{index, value}] (pure LLM evaluation)
  4. Execute answers field-by-field — no hardcoded field IDs (workday_executors)
  5. Handle multi-step sections & dialogs (workday_sections)
  6. Authenticate & bypass login walls (workday_auth)
  7. Stop at Review page; waits for [Enter] before submitting

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 USAGE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  # Headless:
  python3 src/app_workday.py "JOB_URL"

  # Headed / displayed mode (shows Chrome window for manual review/edits):
  python3 src/app_workday.py "JOB_URL" --show
"""

import asyncio
import datetime
import json
import re
import sys
from playwright.async_api import async_playwright, Page

# Windows: prevent UnicodeEncodeError on emoji/special chars in log output
# and enable line buffering so logs appear in real-time instead of at exit
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from app_common import (
    LIBRARY, PI, WE, EDU, LANG, RESUME_PATH, TRANSCRIPT_PATH,
    CHROME_PATH, EMAIL, PASSWORD, DEEPSEEK_KEY,
    PHONE_DIGITS, PROFILE_SUMMARY, SYSTEM_PROMPT,
    deepseek_fill_page, deepseek_pick_option, label_match, pick_decline,
    launch_browser, write_json_report, scrape_salary,
    ARTIFACTS_DIR,
)

from workday_scan import (
    normalize_workday_url,
    SCAN_JS,
    get_heading,
    read_validation_errors,
    _scrape_listing_locations,
    _scrape_listing_salary,
)

from workday_executors import (
    SETTLE_MS,
    SEARCH_MS,
    SAVE_MS,
    MEDIUM_MS,
    MONTH_NUM,
    locate_field,
    exec_text,
    exec_button_dropdown,
    exec_selectinput,
    exec_radio,
    exec_checkbox,
    execute_answer,
    prefetch_options,
    save_and_continue,
    save_and_continue_with_report,
)

from workday_auth import (
    ensure_signed_in,
)

from workday_sections import (
    SECTION_SPEC,
    PILLS_JS,
    _READ_VISIBLE_OPTIONS_JS,
    deepseek_pick_skill,
    handle_document_uploads,
    fill_add_dialog,
    _type_and_pick_option,
    exec_skills_field,
    smart_fill_page,
    handle_my_experience,
    handle_voluntary_disclosures,
    handle_self_identify,
)

ARTIFACTS = ARTIFACTS_DIR
ARTIFACTS.mkdir(exist_ok=True)

# ── Run report (written to artifacts/run_report.json after each run) ──────────
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

async def main(job_url: str, headed: bool = False, no_submit: bool = False):
    job_url = normalize_workday_url(job_url)
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

        # Safety net: intercept any stray native file chooser
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

        # ── Already-applied early detection ──────────────────────────────────
        async def _check_already_applied() -> str | None:
            try:
                body = (await page.evaluate("() => document.body.innerText")).lower()
                body = body.replace("’", "'").replace("`", "'")
            except Exception:
                return None
            from job_tracker import ALREADY_APPLIED_INDICATORS
            for phrase in ALREADY_APPLIED_INDICATORS:
                if phrase in body:
                    return phrase
            return None

        async def _handle_already_applied(signal: str, page_num: int = 0):
            print(f"\n[BOT] ✓ Already applied ({signal!r}) — marking as Applied in tracker and exiting early.")
            ss_name = "already_applied.png"
            try:
                await page.screenshot(path=str(ARTIFACTS / ss_name))
            except Exception:
                pass
            RUN_REPORT["final"] = "complete"
            _report_page(page_num, "Already Applied", "complete", screenshot=ss_name)
            try:
                from job_tracker import mark_applied_by_url
                mark_applied_by_url(job_url)
            except Exception as _e:
                print(f"  [tracker] auto-mark-applied failed (non-fatal): {_e}")
            _write_report()
            await browser.close()

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

        _applied_hint = await _check_already_applied()
        if _applied_hint:
            await _handle_already_applied(_applied_hint, 0)
            return

        # Check if precomputed evaluation metadata exists from job_tracker.py evaluate
        from tracker_db import get_job_eval_metadata
        eval_meta = get_job_eval_metadata(job_url)

        if eval_meta and eval_meta.get("salary_midpoint"):
            listing_salary = str(eval_meta["salary_midpoint"])
            print(f"[NAV] Using precomputed evaluated salary midpoint: ${int(listing_salary):,}")
        else:
            listing_salary = await _scrape_listing_salary(page)
            if listing_salary:
                print(f"[NAV] Using listing salary midpoint: ${int(listing_salary):,}")
            else:
                listing_salary = str(LIBRARY.get("compensation_rules", {}).get("baseline_target_pay", "120000"))
                print(f"[NAV] No salary range found in listing — using baseline: {listing_salary}")

        if eval_meta and eval_meta.get("ordered_locations"):
            listing_locations = eval_meta["ordered_locations"]
            print(f"[NAV] Using precomputed evaluated locations: {listing_locations}")
        else:
            listing_locations = await _scrape_listing_locations(page, job_url)

        target_skills = (eval_meta.get("target_skills") if eval_meta else None) or []
        if target_skills:
            print(f"[NAV] Using precomputed evaluated target skills: {target_skills}")

        _tenant_m = re.match(r'https://([^.]+)\.wd\d+\.myworkdayjobs\.com', job_url)
        listing_tenant = _tenant_m.group(1).lower() if _tenant_m else ""

        # Update PROFILE_SUMMARY
        _mod = sys.modules[__name__]
        _profile_data = json.loads(_mod.PROFILE_SUMMARY)
        _profile_data["job_listing_salary"] = int(listing_salary)
        _profile_data["job_listing_locations"] = listing_locations
        _profile_data["job_target_skills"] = target_skills
        _profile_data["job_tenant"] = listing_tenant
        _profile_data["today"] = datetime.date.today().isoformat()
        _mod.PROFILE_SUMMARY = json.dumps(_profile_data, indent=2)

        import app_common as _ac
        _ac.PROFILE_SUMMARY = _mod.PROFILE_SUMMARY
        import workday_sections as _ws
        _ws.PROFILE_SUMMARY = _mod.PROFILE_SUMMARY

        await ensure_signed_in(page)

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
        _applied_hint = await _check_already_applied()
        if _applied_hint:
            await _handle_already_applied(_applied_hint, 0)
            return

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

        # Navigate via "Apply Manually" link
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

        await ensure_signed_in(page)
        _applied_hint = await _check_already_applied()
        if _applied_hint:
            await _handle_already_applied(_applied_hint, 0)
            return

        try:
            await page.wait_for_selector('[data-automation-id="progressBarActiveStep"]', timeout=25000)
        except Exception:
            pass
        await page.wait_for_timeout(1000)

        for page_num in range(1, 12):
            await page.wait_for_timeout(1500)
            _applied_hint = await _check_already_applied()
            if _applied_hint:
                await _handle_already_applied(_applied_hint, page_num)
                return

            heading = await get_heading(page)
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
                blank_ss = str(ARTIFACTS / f"blank_heading_p{page_num}.png")
                await page.screenshot(path=blank_ss)
                print(f"  [NAV] Blank heading after 8s — screenshot saved, stopping")
                RUN_REPORT["final"] = "blocked"
                _report_page(page_num, "(blank)", "stuck", errors=["blank heading after 8s"], screenshot=blank_ss)
                break

            if any(kw in heading.lower() for kw in ("sign in", "create account", "log in", "signin")):
                print(f"  → Sign-in page inside application — attempting sign-in")
                await ensure_signed_in(page)
                continue

            body_text = await page.evaluate("() => document.body.innerText")
            if "something went wrong" in body_text.lower() and "please refresh" in body_text.lower():
                print(f"  → Workday error page detected — reloading...")
                await page.reload(wait_until="domcontentloaded")
                await page.wait_for_timeout(4000)
                continue

            _applied_hint = await _check_already_applied()
            if _applied_hint:
                await _handle_already_applied(_applied_hint, page_num)
                break

            ss_name = f"run_{page_num:02d}_{re.sub(r'[^a-zA-Z0-9]','_',heading)[:30]}.png"
            ss = ARTIFACTS / ss_name
            await page.screenshot(path=str(ss))

            has_add_buttons  = await page.locator("[data-automation-id='add-button']").count() > 0
            has_tc_checkbox  = await page.locator("[id*='termsAndConditions'],[data-automation-id='termsAndConditions']").count() > 0
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
                    orig_vp = page.viewport_size or {"width": 1280, "height": 900}
                    await page.set_viewport_size({"width": orig_vp["width"], "height": 3000})
                    await page.wait_for_timeout(400)
                    await page.screenshot(path=review_shot, full_page=True)
                    await page.set_viewport_size(orig_vp)
                    print(f"  Screenshot saved: {review_shot}")
                    RUN_REPORT["final"] = "review"
                    _report_page(page_num, heading, "review", screenshot="review_page.png")
                    try:
                        if no_submit or not sys.stdin.isatty():
                            raise EOFError("Review only / non-interactive mode")
                        user_input = await asyncio.to_thread(input, "  → Press [Enter] to submit, 's' to skip 3 days, Ctrl+C to abort: ")
                        user_input = user_input.strip().lower()
                        m = re.match(r"^s(?:kip)?\s*(\d+)?$", user_input)
                        if m or user_input in ("snooze", "3"):
                            days = int(m.group(1)) if (m and m.group(1)) else 3
                            from job_tracker import snooze_job_by_url
                            snooze_until = snooze_job_by_url(job_url, days=days, reason=f"Skipped by user for {days} days")
                            print(f"\n  [WD] ⏸ Job snoozed for {days} days (until {snooze_until}).")
                            RUN_REPORT["final"] = "snoozed"
                            _write_report()
                            sys.exit(2)
                        elif user_input in ("q", "cancel", "abort"):
                            print("  [WD] Cancelled by user.")
                            RUN_REPORT["final"] = "cancelled_by_user"
                            _write_report()
                            return
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
                            print("  → Browser staying open for 10 minutes. Kill this process when done.")
                            await page.wait_for_timeout(600_000)
                    break

                elif has_add_buttons:
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
                    print(f"  → Detected as voluntary disclosures page (T&C checkbox present)")
                    ok = await handle_voluntary_disclosures(page)
                    if not ok:
                        current = await get_heading(page)
                        if current and current == "Voluntary Disclosures":
                            print(f"  [NAV] Voluntary Disclosures save may have failed — continuing anyway")
                    _report_page(page_num, heading, "advanced" if ok else "filled", screenshot=ss_name)

                elif has_self_id:
                    print(f"  → Detected as self-identify page")
                    ok = await handle_self_identify(page)
                    if not ok:
                        current = await get_heading(page)
                        if current and current == "Self Identify":
                            print(f"  [NAV] Self Identify save may have failed — continuing anyway")
                    _report_page(page_num, heading, "advanced" if ok else "filled", screenshot=ss_name)

                else:
                    print(f"  → Generic form page — smart fill")
                    for _wait_attempt in range(5):
                        _applied_hint = await _check_already_applied()
                        if _applied_hint:
                            await _handle_already_applied(_applied_hint, page_num)
                            return
                        await smart_fill_page(page, heading)
                        nxt = await page.locator("[data-automation-id='pageFooterNextButton']").count()
                        flds = await page.evaluate(SCAN_JS, None)
                        fillable = [f for f in flds if f.get("tag") in ("input","textarea","button","select")
                                    and not f.get("label","").lower().startswith("search")]
                        if fillable or nxt:
                            break
                        _applied_hint = await _check_already_applied()
                        if _applied_hint:
                            await _handle_already_applied(_applied_hint, page_num)
                            return
                        print(f"  [NAV] 0 fillable fields found — waiting for page to load (attempt {_wait_attempt+1}/5)...")
                        await page.wait_for_timeout(3000)
                    ok, _errs, _reqs = await save_and_continue_with_report(page)
                    if not ok:
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
    if sys.platform == "win32" and sys.version_info < (3, 8):
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    args = sys.argv[1:]
    headed = "--show" in args
    no_submit = "--no-submit" in args
    url_args = [a for a in args if not a.startswith("--")]
    if not url_args:
        print("Usage: python3 -u src/app_workday.py <WORKDAY_JOB_URL> [--show] [--no-submit]")
        sys.exit(1)
    if not DEEPSEEK_KEY:
        print("Error: DEEPSEEK_API_KEY is required in data/.env.")
        sys.exit(1)
    job_url = url_args[0]

    import traceback
    try:
        asyncio.run(main(job_url, headed=headed, no_submit=no_submit))
    except Exception:
        traceback.print_exc()
        sys.exit(1)
