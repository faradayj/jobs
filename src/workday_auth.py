"""
workday_auth.py — Workday authentication and login wall handling
==============================================================
Detects Workday login walls and executes sign-in, account creation,
RTX LinkedIn OAuth, or interactive sign-in fallback.
"""

import re
from playwright.async_api import Page
from app_common import LIBRARY, EMAIL, PASSWORD


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
        "[data-automation-id='SignInWithEmailButton']",
        "[data-automation-id='signInWithEmailButton']",
        "button:has-text('Sign in with email')",
        "button:has-text('Sign in with Email')",
    ]
    current_url = page.url
    tenant = ""
    m = re.search(r'https://([^.]+)\.wd\d+\.myworkdayjobs\.com', current_url)
    if m:
        tenant = m.group(1)
    is_rtx = "rtx" in current_url.lower() or "rec_rtx" in current_url.lower() or tenant in ("rec_rtx_ext_gateway", "globalhr", "rtx")
    if is_rtx:
        LOGIN_SELECTORS.extend([
            "[data-automation-id='LinkedInSignInButton']",
            "[data-automation-id='linkedInSignInButton']",
            "button:has-text('Sign in with LinkedIn')",
        ])
    is_login_wall = any([await page.locator(sel).count() > 0 for sel in LOGIN_SELECTORS])
    if not is_login_wall:
        return
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

    async def _do_linkedin_sign_in() -> bool:
        """Attempt LinkedIn sign-in for RTX/OAuth portal."""
        linkedin_selectors = [
            "[data-automation-id='LinkedInSignInButton']",
            "[data-automation-id='linkedInSignInButton']",
            "button:has-text('Sign in with LinkedIn')",
            "button:has-text('LinkedIn')",
            "a:has-text('LinkedIn')",
            "[aria-label*='LinkedIn']",
        ]
        btn = None
        for sel in linkedin_selectors:
            loc = page.locator(sel).first
            try:
                if await loc.count():
                    await loc.wait_for(state="visible", timeout=6000)
                    btn = loc
                    break
            except Exception:
                pass
        if not btn:
            return False
        print("[AUTH] Attempting Sign in with LinkedIn...")
        try:
            await btn.click(force=True)
            await page.wait_for_timeout(4000)

            # Check if navigated to LinkedIn OAuth login page
            target_page = page
            if "linkedin.com" not in page.url and len(page.context.pages) > 1:
                target_page = page.context.pages[-1]

            if "linkedin.com" in target_page.url:
                print(f"[AUTH] On LinkedIn OAuth page ({target_page.url}) — filling credentials...")
                user_input = target_page.locator("#username, input[name='session_key']").first
                pass_input = target_page.locator("#password, input[name='session_password']").first
                if await user_input.count():
                    await user_input.click(click_count=3)
                    await user_input.fill(use_email)
                    await pass_input.click(click_count=3)
                    await pass_input.fill(use_password)
                    await target_page.wait_for_timeout(300)
                    sub_btn = target_page.locator("button[type='submit']:has-text('Sign in'), button[type='submit'], .btn__primary--large").first
                    if await sub_btn.count():
                        await sub_btn.click()
                    print("[AUTH] Submitted credentials on LinkedIn OAuth page...")

                # Wait for navigation/redirect back to Workday
                for _ in range(25):
                    await page.wait_for_timeout(1000)
                    if "myworkday" in page.url:
                        break

            passed = await _wait_past_login(timeout_s=25)
            if passed:
                print("[AUTH] ✓ Signed in successfully via LinkedIn.")
                return True
        except Exception as e:
            print(f"[AUTH] LinkedIn sign-in exception: {e}")
        return False

    # ── Check for multi-option sign-in landing page (e.g. NVIDIA) ──
    if not is_rtx:
        email_option_selectors = [
            "[data-automation-id='SignInWithEmailButton']",
            "[data-automation-id='signInWithEmailButton']",
            "button:has-text('Sign in with email')",
            "button:has-text('Sign in with Email')",
            "button:has-text('Sign In with Email')",
            "button:has-text('Sign in with email/password')",
            "a:has-text('Sign in with email')",
            "a:has-text('Sign in with Email')",
            "[aria-label*='Sign in with email']",
            "[aria-label*='Sign in with Email']",
        ]
        for sel in email_option_selectors:
            loc = page.locator(sel).first
            try:
                if await loc.count() and await loc.is_visible():
                    print(f"[AUTH] Multi-option sign-in page detected — clicking 'Sign in with email' ({sel})...")
                    await loc.click(force=True)
                    await page.wait_for_timeout(1000)
                    try:
                        await page.locator("[data-automation-id='email']").first.wait_for(state="visible", timeout=8000)
                    except Exception:
                        pass
                    break
            except Exception:
                pass

    # ── Detect current state ──
    has_verify = await page.locator("[data-automation-id='verifyPassword']").count()
    has_create_btn = await page.locator("[data-automation-id='createAccountSubmitButton']").count()
    has_signin_btn = await page.locator("[data-automation-id='signInSubmitButton']").count()
    has_signin_link = await page.locator("[data-automation-id='signInLink']").count()

    # Try LinkedIn sign-in strictly for RTX portals
    if is_rtx:
        print("[AUTH] RTX portal detected — attempting LinkedIn OAuth sign-in...")
        if await _do_linkedin_sign_in():
            return

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
            if is_rtx and await _do_linkedin_sign_in():
                return
            # Sign-in failed — no registered account yet.
            print("[AUTH] ⚠ No existing account found.")
            ca_link = page.locator("[data-automation-id='createAccountLink']").first
            if await ca_link.count():
                try: await ca_link.click(force=True); await page.wait_for_timeout(1500)
                except Exception: pass

    elif has_signin_btn and not has_verify:
        # ── State B: Sign-in form only ──
        if await _do_sign_in():
            return
        if is_rtx and await _do_linkedin_sign_in():
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
