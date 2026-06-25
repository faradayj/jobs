"""
Workday Application Bot v2
Clean rewrite incorporating all lessons learned.
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from dotenv import load_dotenv
from playwright.async_api import async_playwright
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

class NextAction(BaseModel):
    element_index: int = Field(description="Index of the element to act on.")
    action_type: str = Field(description="One of: type | select | radio | checkbox | click | skip")
    value: str = Field(description="Value to enter/select. Empty string for click/skip.")


# ---------------------------------------------------------------------------
# JS: scan all visible form elements on the page
# ---------------------------------------------------------------------------

SCAN_JS = r"""
() => {
    const results = [];
    let idx = 0;

    const isVisible = el => {
        const s = window.getComputedStyle(el);
        if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') return false;
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
    };

    const getLabel = el => {
        let t = (el.getAttribute('aria-label') || '').trim();
        if (t) return t;
        if (el.id) {
            const lbl = document.querySelector(`label[for="${el.id}"]`);
            if (lbl) return lbl.innerText.trim();
        }
        const fw = el.closest('[data-uxi-widget-type="formField"]') ||
                   el.closest('[data-automation-id="formField"]');
        if (fw) {
            const lbl = fw.querySelector('label');
            if (lbl) return lbl.innerText.trim();
        }
        return (el.placeholder || el.name || el.getAttribute('data-automation-id') || '').trim();
    };

    const getPillValue = el => {
        // Workday pill: look up from element to selectinput container
        const sc = el.closest('[data-uxi-widget-type="selectinput"]') ||
                   el.parentElement?.closest('[data-uxi-widget-type="selectinput"]');
        if (sc) {
            const pill = sc.querySelector('[data-automation-id="promptAriaInstruction"]');
            if (pill && pill.innerText) {
                const t = pill.innerText.trim();
                if (t.includes('item selected, '))  return t.split('item selected, ')[1].trim();
                if (t.includes('items selected, ')) return t.split('items selected, ')[1].trim();
                return t;
            }
            const sel = sc.querySelector('[data-automation-id="promptSelectionLabel"]');
            if (sel && sel.innerText) return sel.innerText.trim();
        }
        return '';
    };

    const getValue = (el, tag, type) => {
        if (tag === 'select') {
            return el.options[el.selectedIndex] ? el.options[el.selectedIndex].text.trim() : '';
        }
        if (type === 'radio') {
            const checked = document.querySelector(`input[type="radio"][name="${el.name}"]:checked`);
            return checked ? (checked.getAttribute('aria-label') || checked.value || '') : '';
        }
        if (type === 'checkbox') return el.checked ? 'on' : '';
        const pill = getPillValue(el);
        if (pill) return pill;
        return el.value || '';
    };

    // Labels of buttons we want the LLM to be able to click (navigation)
    const NAV_INCLUDES = ['save and continue', 'next', 'continue', 'submit', 'review'];
    // Labels to always exclude (UI chrome, not form actions)
    const ALWAYS_SKIP = [
        'apply', 'apply manually', 'sign in', 'create account',
        'introduce yourself', 'candidate home', 'all jobs', 'english',
        'view all', 'privacy', 'cookie'
    ];

    document.querySelectorAll('*').forEach(el => {
        if (!isVisible(el)) return;
        const tag = el.tagName.toLowerCase();
        const type = (el.getAttribute('type') || tag).toLowerCase();
        const role = (el.getAttribute('role') || '').toLowerCase();

        // ---- TEXT INPUT / TEXTAREA ----
        if (tag === 'textarea' ||
            (tag === 'input' && ['text','email','tel','number','search','url'].includes(type))) {
            el.setAttribute('data-wda-idx', idx);
            results.push({
                index: idx++, type: 'text_input',
                labelText: getLabel(el), id: el.id || '',
                currentValue: el.value || ''
            });
            return;
        }

        // ---- NATIVE SELECT ----
        if (tag === 'select') {
            el.setAttribute('data-wda-idx', idx);
            results.push({
                index: idx++, type: 'select',
                labelText: getLabel(el), id: el.id || '',
                currentValue: getValue(el, tag, type)
            });
            return;
        }

        // ---- WORKDAY SEARCH DROPDOWN (selectinput / combobox) ----
        // Exclude value-pill buttons that live INSIDE a selectinput
        if (el.closest('[data-automation-id="selectedItem"]') ||
            el.closest('[data-automation-id="promptSelectionContainer"]')) return;

        if (role === 'combobox' ||
            el.getAttribute('data-uxi-widget-type') === 'selectinput') {
            el.setAttribute('data-wda-idx', idx);
            results.push({
                index: idx++, type: 'search_dropdown',
                labelText: getLabel(el), id: el.id || '',
                currentValue: getPillValue(el)
            });
            return;
        }

        // ---- RADIO ----
        if (type === 'radio') {
            el.setAttribute('data-wda-idx', idx);
            const radioLabel = getLabel(el) || el.value || '';
            // Try to get the question label from the enclosing group
            const groupEl = el.closest('[data-uxi-widget-type="formField"]') ||
                            el.closest('[role="group"]');
            const groupLabel = groupEl
                ? (groupEl.querySelector('legend, [data-automation-id="formLabel"]')?.innerText?.trim() || '')
                : '';
            results.push({
                index: idx++, type: 'radio',
                labelText: radioLabel, groupLabel: groupLabel,
                id: el.id || '', currentValue: getValue(el, tag, type)
            });
            return;
        }

        // ---- CHECKBOX ----
        if (type === 'checkbox') {
            el.setAttribute('data-wda-idx', idx);
            results.push({
                index: idx++, type: 'checkbox',
                labelText: getLabel(el), id: el.id || '',
                currentValue: el.checked ? 'on' : 'off'
            });
            return;
        }

        // ---- BUTTONS (NAV OR DROPDOWN) ----
        if (tag === 'button' || role === 'button') {
            const txt = (el.innerText || '').trim();
            if (!txt || txt.length > 80) return;
            const tl = txt.toLowerCase();
            if (ALWAYS_SKIP.some(s => tl.includes(s))) return;
            
            // Check if it is a dropdown trigger button
            const isDropdown = el.closest('[data-uxi-widget-type="formField"]') ||
                               el.closest('[data-automation-id="formField"]') ||
                               el.id.includes('--') ||
                               el.getAttribute('aria-haspopup') === 'listbox' ||
                               el.getAttribute('aria-haspopup') === 'true';
            
            if (isDropdown && !NAV_INCLUDES.some(n => tl.includes(n))) {
                // Skip value-pill buttons inside selectinput
                if (el.closest('[data-uxi-widget-type="selectinput"]')) return;
                el.setAttribute('data-wda-idx', idx);
                results.push({
                    index: idx++, type: 'search_dropdown',
                    labelText: getLabel(el), id: el.id || '',
                    currentValue: txt === 'Select One' ? '' : txt
                });
                return;
            }

            // Only include if it's a recognised nav button
            if (!NAV_INCLUDES.some(n => tl.includes(n))) return;
            // Skip pills inside selectinput
            if (el.closest('[data-uxi-widget-type="selectinput"]')) return;
            el.setAttribute('data-wda-idx', idx);
            results.push({ index: idx++, type: 'button', text: txt });
        }
    });

    return results;
}
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def find_locator(page, element):
    """Locate an element by its injected data-wda-idx attribute, with id fallback."""
    sel = f"[data-wda-idx='{element['index']}']"
    loc = page.locator(sel).first
    if await loc.is_visible():
        return loc
    if element.get('id'):
        loc2 = page.locator(f"#{element['id']}").first
        if await loc2.is_visible():
            return loc2
    return loc  # Return anyway; caller handles timeout


async def do_search_select(page, trigger, value: str):
    """
    Workday search dropdown interaction:
      1. Click the trigger to open the dropdown popover.
      2. Find the active search input box inside the dropdown popover.
      3. If a search input exists, type the value and press Enter.
      4. Pick the best matching option from the visible options list and click it.
    """
    print(f"      [DEBUG] Starting do_search_select for '{value}'")
    
    # 1. Click trigger to open dropdown
    await trigger.scroll_into_view_if_needed()
    await trigger.click(force=True)
    await page.wait_for_timeout(800)  # Wait for popover to render
    
    # 2. Look for search input inside the opened popover
    active_input = None
    popover_id = await trigger.get_attribute("aria-controls")
    if popover_id:
        inp = page.locator(f'[id="{popover_id}"] input[data-automation-id="searchBox"], [id="{popover_id}"] input[type="text"], [id="{popover_id}"] input:not([type="radio"]):not([type="checkbox"])').first
        if await inp.is_visible():
            active_input = inp
            print(f"      [DEBUG] Found active input via aria-controls='{popover_id}'")
    
    if not active_input:
        for selector in [
            "[data-automation-id='promptDialog'] input[data-automation-id='searchBox']",
            "[data-automation-id='promptDialog'] input[type='text']",
            "[data-automation-id='promptDialog'] input:not([type='radio']):not([type='checkbox'])",
            "[role='dialog'] input[data-automation-id='searchBox']",
            "[role='dialog'] input[type='text']",
            "[role='dialog'] input:not([type='radio']):not([type='checkbox'])",
            "[role='listbox'] input[type='text']",
            "[role='listbox'] input:not([type='radio']):not([type='checkbox'])",
            "input[data-automation-id='searchBox']"
        ]:
            inp = page.locator(selector).first
            if await inp.is_visible():
                active_input = inp
                print(f"      [DEBUG] Found active input via selector '{selector}'")
                break

    if not active_input:
        # Check if the trigger itself is an input
        trigger_tag = await trigger.evaluate("el => el.tagName.toLowerCase()")
        if trigger_tag == "input":
            active_input = trigger
            print("      [DEBUG] Using trigger input itself as active search input")

    # 3. Fill search input if it exists
    if active_input:
        try:
            await active_input.fill("")
            await active_input.press_sequentially(value, delay=50)
        except Exception as e:
            print(f"      [DEBUG] fill failed: {e}. Trying keyboard type.")
            await active_input.click(force=True)
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Backspace")
            await page.keyboard.type(value, delay=40)
            
        await page.wait_for_timeout(500)
        await active_input.press("Enter")
        await page.wait_for_timeout(1500)
    else:
        print("      [DEBUG] No active search input found. Searching visible options directly...")

    # 4. Find the visible open dialog/listbox/popover to pick option
    visible_opts = await page.evaluate(r"""() => {
        const dialogs = Array.from(document.querySelectorAll(
            "[data-automation-id='promptDialog'], [role='dialog'], [role='listbox'], [data-behavior-click-outside-close='topmost']"
        ));
        const open = dialogs.reverse().find(d => {
            if (d.getAttribute('aria-hidden') === 'true') return false;
            const style = window.getComputedStyle(d);
            if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
            const r = d.getBoundingClientRect();
            return r.height > 20 && r.width > 20;
        });
        if (!open) return [];
        return Array.from(open.querySelectorAll(
            "[data-automation-id='promptOption'], [data-automation-id='menuItem'], [role='option'], li"
        )).filter(o => o.getBoundingClientRect().height > 0)
          .map(o => o.innerText.trim());
    }""")

    print(f"      [DEBUG] Results: {visible_opts}")

    if visible_opts:
        clicked = await page.evaluate(r"""(val) => {
            const strip = s => s.replace(/\s*\([^)]*\)/g, '').trim().toLowerCase();
            const dialogs = Array.from(document.querySelectorAll(
                "[data-automation-id='promptDialog'], [role='dialog'], [role='listbox'], [data-behavior-click-outside-close='topmost']"
            ));
            const open = dialogs.reverse().find(d => {
                if (d.getAttribute('aria-hidden') === 'true') return false;
                const style = window.getComputedStyle(d);
                if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
                const r = d.getBoundingClientRect();
                return r.height > 20 && r.width > 20;
            });
            if (!open) return null;
            const opts = Array.from(open.querySelectorAll(
                "[data-automation-id='promptOption'], [data-automation-id='menuItem'], [role='option'], li"
            )).filter(o => o.getBoundingClientRect().height > 0);

            const qFull  = val.toLowerCase().trim();
            const qStrip = strip(val);
            let best = null, bestScore = -1;

            opts.forEach(el => {
                const tFull  = el.innerText.toLowerCase().trim();
                const tStrip = strip(el.innerText);
                if (tFull === qFull)              { best = el; bestScore = 1000; return; }
                if (bestScore < 999 && tStrip === qStrip) { best = el; bestScore = 999; return; }
                if (bestScore < 900 && (tFull.includes(qFull) || qFull.includes(tFull))) {
                    const s = 100 - Math.abs(tFull.length - qFull.length);
                    if (s > bestScore) { bestScore = s; best = el; }
                }
                if (bestScore < 90 && (tStrip.includes(qStrip) || qStrip.includes(tStrip))) {
                    const s = 50 - Math.abs(tStrip.length - qStrip.length);
                    if (s > bestScore) { bestScore = s; best = el; }
                }
            });

            if (best && bestScore > 0) { 
                const clickTarget = best.closest("[role='option']") || best.closest("[data-automation-id='menuItem']") || best;
                clickTarget.click(); 
                return best.innerText.trim(); 
            }
            if (opts.length > 0) { 
                const clickTarget = opts[0].closest("[role='option']") || opts[0].closest("[data-automation-id='menuItem']") || opts[0];
                clickTarget.click(); 
                return opts[0].innerText.trim() + ' [first]'; 
            }
            return null;
        }""", value)
        print(f"      [DEBUG] Clicked option: '{clicked}'")
        await page.wait_for_timeout(500)
    else:
        print(f"      [DEBUG] No options visible. Pressing Escape.")
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(300)


async def verify_value(page, element, expected_value: str) -> bool:
    """Verify if the element's value in the DOM matches the expected value (or is non-empty)."""
    # Wait a brief moment for DOM updates
    await page.wait_for_timeout(500)
    locator = await find_locator(page, element)
    try:
        if not await locator.is_visible():
            return False
            
        actual_val = await locator.evaluate(r"""(el) => {
            const getPillValue = el => {
                const sc = el.closest('[data-uxi-widget-type="selectinput"]') ||
                           el.parentElement?.closest('[data-uxi-widget-type="selectinput"]');
                if (sc) {
                    const pill = sc.querySelector('[data-automation-id="promptAriaInstruction"]');
                    if (pill && pill.innerText) {
                        const t = pill.innerText.trim();
                        if (t.includes('item selected, '))  return t.split('item selected, ')[1].trim();
                        if (t.includes('items selected, ')) return t.split('items selected, ')[1].trim();
                        return t;
                    }
                    const sel = sc.querySelector('[data-automation-id="promptSelectionLabel"]');
                    if (sel && sel.innerText) return sel.innerText.trim();
                }
                return '';
            };
            const tag = el.tagName.toLowerCase();
            const type = (el.getAttribute('type') || tag).toLowerCase();
            if (tag === 'select') {
                return el.options[el.selectedIndex] ? el.options[el.selectedIndex].text.trim() : '';
            }
            if (tag === 'button') {
                const txt = el.innerText.trim();
                return txt === 'Select One' ? '' : txt;
            }
            if (type === 'radio') {
                const name = el.getAttribute('name');
                if (name) {
                    const checked = document.querySelector(`input[type="radio"][name="${name}"]:checked`);
                    return checked ? (checked.getAttribute('aria-label') || checked.value || '') : '';
                }
                return el.checked ? (el.getAttribute('aria-label') || el.value || 'on') : '';
            }
            if (type === 'checkbox') return el.checked ? 'on' : 'off';
            const pill = getPillValue(el);
            if (pill) return pill;
            return el.value || '';
        }""")
        
        print(f"      [VERIFY] Expected: '{expected_value}' | Actual in DOM: '{actual_val}'")
        
        if expected_value.lower() in ('on', 'off') and actual_val.lower() in ('on', 'off'):
            return expected_value.lower() == actual_val.lower()
            
        if not actual_val:
            return False
            
        # Check if they are similar/matching (sub-string, case-insensitive)
        return expected_value.lower() in actual_val.lower() or actual_val.lower() in expected_value.lower()
    except Exception as e:
        print(f"      [VERIFY_ERROR] {e}")
        return False


async def handle_linkedin_if_needed(page, email_val, pass_val) -> bool:
    """Detect and handle LinkedIn login or OAuth consent screens if visible."""
    if "linkedin.com" in page.url:
        # 1. Check for LinkedIn Login page
        linkedin_user = page.locator("input#username, input[type='email']").first
        linkedin_pass = page.locator("input#password, input[type='password']").first
        linkedin_submit = page.locator("button[type='submit']:has-text('Sign in'), button:has-text('Sign In'), button:has-text('Sign in')").first
        
        if await linkedin_user.is_visible():
            print("[*] LinkedIn login page detected. Logging in...")
            await linkedin_user.fill(email_val)
            await linkedin_pass.fill(pass_val)
            await page.wait_for_timeout(400)
            await linkedin_submit.click(force=True)
            await page.wait_for_timeout(3000)
            return True
            
        # 2. Check for LinkedIn OAuth consent screen (Allow button)
        allow_btn = page.locator("button:has-text('Allow'), button[type='submit']:has-text('Allow'), #oauth__auth-form__submit-btn").first
        if await allow_btn.is_visible():
            print("[*] LinkedIn OAuth consent page detected. Clicking 'Allow'...")
            await allow_btn.click(force=True)
            await page.wait_for_timeout(4000)
            return True
    return False


async def sign_in(page, library):
    """Detect and handle Workday sign-in modal/page and automatic registration fallback."""
    # Check if we are already on the form (e.g. if cookies worked)
    is_form_visible = await page.locator(
        "input:not([type='hidden']):not([type='submit']):not([type='button']):not([type='file']), "
        "select, textarea, [role='combobox'], [data-uxi-widget-type='selectinput']"
    ).count() > 0
    is_login_page_init = await page.locator(
        "[data-automation-id='SignInWithEmailButton'], "
        "button:has-text('Sign in with email'), "
        "[data-automation-id='LinkedInSignInButton'], "
        "input[type='email'], input[data-automation-id='email']"
    ).count() > 0
    if is_form_visible and not is_login_page_init:
        print("[*] Already on the application form. Skipping sign-in.")
        return

    sign_in_btn = page.locator(
        "[data-automation-id='signIn'], button:has-text('Sign In'), a:has-text('Sign In')"
    ).first
    
    # 1. Click initial header Sign In if visible (only if not already on the login gate page)
    is_login_page = await page.locator(
        "[data-automation-id='SignInWithEmailButton'], "
        "button:has-text('Sign in with email'), "
        "[data-automation-id='LinkedInSignInButton'], "
        "input[type='email'], input[data-automation-id='email']"
    ).count() > 0
    if not is_login_page and await sign_in_btn.is_visible():
        print("[*] Sign-in button detected. Clicking...")
        await sign_in_btn.click(force=True)
        await page.wait_for_timeout(2000)

    # 2. Handle intermediate sign-in options page: prefer LinkedIn!
    linkedin_option = page.locator(
        "[data-automation-id='LinkedInSignInButton'], "
        "button:has-text('Sign in with LinkedIn')"
    ).first
    email_option = page.locator(
        "[data-automation-id='SignInWithEmailButton'], "
        "button:has-text('Sign in with email')"
    ).first
    
    used_linkedin = False
    if await linkedin_option.is_visible():
        print("[*] Intermediate sign-in screen detected. Clicking 'Sign in with LinkedIn'...")
        await linkedin_option.click(force=True)
        await page.wait_for_timeout(2000)
        used_linkedin = True
    elif await email_option.is_visible():
        print("[*] Intermediate sign-in screen detected. Clicking 'Sign in with email'...")
        await email_option.click(force=True)
        await page.wait_for_timeout(2000)

    email_val = library.get("personal_info", {}).get("email", "")
    pass_val = library.get("personal_info", {}).get("password", "")

    if not used_linkedin:
        # 3. Fill in email/password
        email_inp = page.locator("input[data-automation-id='email'], input[type='email'], input[name='username']").first
        pass_inp  = page.locator("input[data-automation-id='password'], input[type='password']").first
        try:
            await email_inp.wait_for(state="visible", timeout=10000)
        except Exception:
            print("[!] Email input never appeared — skipping login.")
            return

        await email_inp.fill(email_val)
        await pass_inp.fill(pass_val)
        await page.wait_for_timeout(400)

        submit = page.locator(
            "[data-automation-id='signInSubmitButton'], "
            "form button[type='submit'], "
            "form button:has-text('Sign In'), "
            "button:has-text('Sign In')"
        ).first
        await submit.click(force=True)

    print("[*] Waiting for login to complete...")
    login_succeeded = False
    for _ in range(25):
        await page.wait_for_timeout(1000)
        
        # Check and handle LinkedIn (login form or OAuth consent dialog)
        await handle_linkedin_if_needed(page, email_val, pass_val)

        # Check for success
        is_form_visible = await page.locator(
            "input:not([type='hidden']):not([type='submit']):not([type='button']):not([type='file']), "
            "select, textarea, [role='combobox'], [data-uxi-widget-type='selectinput']"
        ).count() > 0
        is_still_gate = await page.locator(
            "[data-automation-id='SignInWithEmailButton'], "
            "button:has-text('Sign in with email'), "
            "[data-automation-id='LinkedInSignInButton']"
        ).count() > 0
        
        # Check for incorrect login error message
        error_msg = page.locator("[role='alert'], .css-18t3jhe, :has-text('incorrect'), :has-text('invalid')").first
        if await error_msg.is_visible():
            print(f"[!] Login error detected: {await error_msg.inner_text()}")
            break
            
        if not is_still_gate and (is_form_visible or "candidateHome" in page.url):
            login_succeeded = True
            break

    if login_succeeded:
        print("[+] Login complete.")
        return

    # Fallback to registration if login failed or timed out
    print("[!] Login failed or timed out. Attempting account creation fallback...")
    create_acct_btn = page.locator(
        "[data-automation-id='createAccountLink'], "
        "button:has-text('Create Account'), "
        "a:has-text('Create Account')"
    ).first
    
    if await create_acct_btn.is_visible():
        print("[*] Clicking 'Create Account' link...")
        await create_acct_btn.click(force=True)
        await page.wait_for_timeout(2000)
        
        reg_email = page.locator("input[data-automation-id='email'], input[type='email']").first
        reg_pass = page.locator("input[data-automation-id='password']").first
        reg_confirm = page.locator(
            "input[data-automation-id='verifyPassword'], "
            "input[data-automation-id='confirmPassword']"
        ).first
        reg_checkbox = page.locator(
            "input[type='checkbox'], "
            "[data-automation-id='agreeToTermsCheckbox'], "
            "[data-automation-id='createAccountCheckbox']"
        ).first
        
        if await reg_email.is_visible():
            print("[*] Filling registration form...")
            await reg_email.fill(email_val)
            await reg_pass.fill(pass_val)
            if await reg_confirm.is_visible():
                await reg_confirm.fill(pass_val)
            if await reg_checkbox.is_visible() and not await reg_checkbox.is_checked():
                await reg_checkbox.click(force=True)
                
            await page.wait_for_timeout(500)
            
            reg_submit = page.locator(
                "[data-automation-id='createAccountSubmitButton'], "
                "button:has-text('Create Account'), "
                "button[type='submit']"
            ).first
            await reg_submit.click(force=True)
            print("[*] Registration form submitted. Waiting for form page to load...")
            
            # Wait for form page
            reg_success = False
            for _ in range(25):
                await page.wait_for_timeout(1000)
                
                # Check and handle LinkedIn during registration OIDC redirect
                await handle_linkedin_if_needed(page, email_val, pass_val)

                is_form = await page.locator(
                    "input:not([type='hidden']):not([type='submit']):not([type='button']):not([type='file']), "
                    "select, textarea, [role='combobox'], [data-uxi-widget-type='selectinput']"
                ).count() > 0
                is_still_gate = await page.locator(
                    "[data-automation-id='SignInWithEmailButton'], "
                    "button:has-text('Sign in with email'), "
                    "[data-automation-id='createAccountSubmitButton']"
                ).count() > 0
                
                if is_form and not is_still_gate:
                    reg_success = True
                    break
                    
            if reg_success:
                print("[+] Registration successful and form page loaded!")
                return
            else:
                print("[!] Registration page timed out or failed.")
    
    # If all automated attempts failed, pause and prompt the user
    print("\n[!] Auto-login and registration fallback did not navigate to the application form.")
    print("[!] If you need to solve a captcha, please do so manually in the browser window.")
    print("[!] Once you are logged in and see the application form, press [Enter] here to resume...")
    await asyncio.to_thread(input, "Press [Enter] to resume...")


async def click_apply_flow(page, target_url, library=None):
    """Click Apply -> Apply Manually -> (sign in if prompted) -> skip intro pages."""
    apply_btn = page.locator(
        "[data-automation-id='adventureButton']:visible, "
        "[data-automation-id='applyButton']:visible, "
        "button:has-text('Apply'):visible, "
        "a:has-text('Apply Now'):visible"
    ).first
    try:
        await apply_btn.wait_for(state="visible", timeout=8000)
    except Exception:
        pass

    if not await apply_btn.is_visible():
        print("[!] Apply button not found — may already be on form.")
        return

    print("[*] Clicking Apply...")
    await apply_btn.scroll_into_view_if_needed()
    await apply_btn.click(force=True)
    await page.wait_for_timeout(2000)

    # Apply Manually (shown on some Workday boards as a dropdown/menu item after clicking Apply)
    apply_manually = page.locator(
        "[data-automation-id='applyManually'], "
        "a:has-text('Apply Manually'), button:has-text('Apply Manually')"
    ).first
    if await apply_manually.is_visible():
        print("[*] Clicking Apply Manually...")
        await apply_manually.scroll_into_view_if_needed()
        await apply_manually.click(force=True)
        await page.wait_for_timeout(3000)

    # Some Workday boards redirect to sign-in after clicking Apply or Apply Manually
    if library:
        await sign_in(page, library)
        
        # Check for LinkedIn redirect
        if "linkedin.com" in page.url:
            print("[*] LinkedIn redirect detected. Navigating back to target job URL...")
            await page.goto(target_url, timeout=30000, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)
            
            # Dismiss cookie banner if it appears again
            try:
                cookie_btn = page.locator(
                    "button:has-text('Accept All'), button:has-text('Accept Cookies'), "
                    "#onetrust-accept-btn-handler"
                ).first
                if await cookie_btn.is_visible():
                    await cookie_btn.click(force=True)
                    await page.wait_for_timeout(800)
            except Exception:
                pass

        # If redirected back to job page after login/redirect bypass, re-trigger Apply & Apply Manually
        apply_btn2 = page.locator(
            "[data-automation-id='adventureButton']:visible, "
            "[data-automation-id='applyButton']:visible, "
            "button:has-text('Apply'):visible"
        ).first
        if await apply_btn2.is_visible():
            print("[*] Re-clicking Apply after login/redirect bypass...")
            await apply_btn2.scroll_into_view_if_needed()
            await apply_btn2.click(force=True)
            await page.wait_for_timeout(2000)
            
            apply_manually2 = page.locator(
                "[data-automation-id='applyManually'], "
                "a:has-text('Apply Manually'), button:has-text('Apply Manually')"
            ).first
            if await apply_manually2.is_visible():
                print("[*] Re-clicking Apply Manually after login/redirect bypass...")
                await apply_manually2.scroll_into_view_if_needed()
                await apply_manually2.click(force=True)
                await page.wait_for_timeout(3000)

    # Skip intro/splash pages (pages with no real inputs — just job info + Next button)
    for _ in range(5):
        await page.wait_for_timeout(2000)
        # Check if we are on a login/auth page or options page
        is_login = await page.locator(
            "[data-automation-id='SignInWithEmailButton'], "
            "button:has-text('Sign in with email'), "
            "input[type='email'], input[data-automation-id='email']"
        ).count() > 0
        if is_login:
            print("[!] Still on login page — attempting login again.")
            await sign_in(page, library)
            continue
            
        real_count = await page.locator(
            "input:not([type='hidden']):not([type='submit']):not([type='button']):not([type='file']), "
            "select, textarea, [role='combobox'], [data-uxi-widget-type='selectinput']"
        ).count()
        if real_count > 0:
            break  # Real form visible
        nxt = page.locator(
            "button:has-text('Next'), button:has-text('Continue'), "
            "button:has-text('Get Started'), button:has-text('Start')"
        ).first
        if await nxt.is_visible():
            txt = await nxt.inner_text()
            print(f"[*] Intro page — clicking '{txt}'...")
            await nxt.click(force=True)
        else:
            break

    print("[+] Form ready.")


# ---------------------------------------------------------------------------
# LLM resolver
# ---------------------------------------------------------------------------

LLM_SYSTEM = """You are filling out a Workday job application form one field at a time.

Rules:
1. Return EXACTLY ONE action.
2. If a field already has a non-empty 'currentValue' it is SATISFIED — do NOT target it, use action_type='skip'.
3. Fill all empty inputs BEFORE clicking navigation buttons (Save and Continue / Next).
4. action_type must be one of: type | select | radio | checkbox | click | skip
5. For 'search_dropdown' and 'select' element types use action_type='select'.
6. For 'text_input' use action_type='type'.
7. For 'radio' elements: action_type='radio', value = exact label of the radio button to select.
8. For 'checkbox': value = 'on' to check, 'off' to leave unchecked.
9. Demographic / disability / veteran questions: always select 'Decline to Identify' or 'I do not wish to answer'.
10. 'How Did You Hear About Us?': search for 'LinkedIn' or 'Job Board'.
11. NEVER check 'I have a preferred name' unless the candidate profile explicitly sets one.
12. For the Country Phone Code field, search 'United States'.
"""

async def resolve_action(llm, elements, library):
    prompt = f"""{LLM_SYSTEM}

Candidate Profile:
{json.dumps(library, indent=2)}

Current Visible Form Elements (already-filled fields have been removed):
{json.dumps(elements, indent=2)}
"""
    structured_llm = llm.with_structured_output(NextAction, method="function_calling")
    return await structured_llm.ainvoke(prompt)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        print("[ERROR] No DEEPSEEK_API_KEY in .env")
        return

    library_path = Path(__file__).parent / "library.json"
    with open(library_path, "r", encoding="utf-8") as f:
        library = json.load(f)

    target_url = sys.argv[1] if len(sys.argv) > 1 else ""
    if not target_url:
        print("[ERROR] Provide a job URL as the first argument.")
        return

    print(f"\n[*] Workday Application Bot v2")
    print(f"[*] Target: {target_url}")

    llm = ChatOpenAI(
        model="deepseek-chat",
        api_key=api_key,
        base_url="https://api.deepseek.com/v1",
        temperature=0.0,
        extra_body={"thinking": {"type": "disabled"}}
    )

    async with async_playwright() as p:
        user_data_dir = Path(__file__).parent / "playwright_user_data"
        context = await p.chromium.launch_persistent_context(
            str(user_data_dir),
            headless=False,
            slow_mo=20,
            args=[
                "--disable-gpu", 
                "--no-sandbox", 
                "--disable-setuid-sandbox",
                "--force-device-scale-factor=1"
            ],
            viewport={"width": 1280, "height": 800}
        )
        page = context.pages[0] if context.pages else await context.new_page()

        # Navigate
        print("[*] Navigating...")
        await page.goto(target_url, timeout=30000, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)

        # Cookie banner
        try:
            cookie_btn = page.locator(
                "button:has-text('Accept All'), button:has-text('Accept Cookies'), "
                "#onetrust-accept-btn-handler"
            ).first
            if await cookie_btn.is_visible():
                await cookie_btn.click(force=True)
                await page.wait_for_timeout(800)
        except Exception:
            pass

        # Apply flow — this may trigger a sign-in redirect on some Workday boards
        await click_apply_flow(page, target_url, library)

        # --- DIAGNOSTIC: screenshot + raw element dump ---
        screenshot_path = str(Path(__file__).parent / "debug_screenshot.png")
        await page.screenshot(path=screenshot_path, full_page=True)
        print(f"[DEBUG] Screenshot saved: {screenshot_path}")

        raw_all = await page.evaluate("""() => {
            const isVis = el => {
                const s = window.getComputedStyle(el);
                if (s.display==='none'||s.visibility==='hidden') return false;
                const r = el.getBoundingClientRect();
                return r.width>0 && r.height>0;
            };
            return Array.from(document.querySelectorAll(
                'input, select, textarea, button, [role="combobox"], [role="button"], [data-uxi-widget-type="selectinput"]'
            )).filter(isVis).map(el => ({
                tag: el.tagName, type: el.type||'', role: el.getAttribute('role')||'',
                automation: el.getAttribute('data-automation-id')||'',
                uxi: el.getAttribute('data-uxi-widget-type')||'',
                text: (el.innerText||el.value||'').trim().slice(0,60),
                id: el.id || ''
            }));
        }""")
        print(f"[DEBUG] Raw visible interactive elements ({len(raw_all)}):")
        for el in raw_all:
            print(f"         {el}")

        # ----------------------------------------------------------------
        # Main LLM loop
        # ----------------------------------------------------------------
        filled_labels: set = set()   # labels of fields successfully filled this page
        loop_counts:   dict = {}     # consecutive LLM hits per label (anti-loop)
        max_iter = 100

        for iteration in range(max_iter):
            await page.wait_for_timeout(1000)

            # Scan
            raw_elements = await page.evaluate(SCAN_JS)

            # Filter already-filled
            elements = [
                e for e in raw_elements
                if e.get('labelText', e.get('text', '')) not in filled_labels
            ]

            print(f"\n[*] --- Cycle {iteration+1} | {len(elements)} unfilled element(s) ---")
            for e in elements:
                print(f"      [FIELD] idx={e['index']} type={e['type']} label='{e.get('labelText', '')}' id='{e.get('id', '')}' val='{e.get('currentValue', '')}'")

            if not elements:
                print("\n[*] No unfilled elements remain — trying navigation button...")
                nav = page.locator(
                    "button:has-text('Save and Continue'), button:has-text('Next'), "
                    "button:has-text('Continue')"
                ).first
                if await nav.is_visible():
                    nav_txt = await nav.inner_text()
                    print(f"[*] Clicking '{nav_txt}'...")
                    
                    url_before = page.url
                    labels_before = {e.get('labelText', e.get('text', '')) for e in raw_elements if e.get('labelText', e.get('text', ''))}
                    
                    await nav.click(force=True)
                    await page.wait_for_timeout(5000)
                    
                    url_after = page.url
                    raw_elements_after = await page.evaluate(SCAN_JS)
                    labels_after = {e.get('labelText', e.get('text', '')) for e in raw_elements_after if e.get('labelText', e.get('text', ''))}
                    
                    if url_after != url_before or labels_after != labels_before:
                        print("[+] Navigation successful! Clearing filled labels for new page.")
                        filled_labels.clear()
                        loop_counts.clear()
                    else:
                        print("[!] Navigation failed (URL and fields did not change). Validation error probably occurred. Keeping filled labels.")
                        screenshot_path = str(Path(__file__).parent / f"debug_nav_failed_iter_{iteration}.png")
                        await page.screenshot(path=screenshot_path)
                        print(f"[DEBUG] Validation failure screenshot saved: {screenshot_path}")
                    continue
                else:
                    print("[*] Nothing left to fill and no nav button. Done.")
                    break

            # Ask LLM
            try:
                result = await resolve_action(llm, elements, library)
            except Exception as e:
                print(f"[!] LLM error: {e}")
                await asyncio.sleep(2)
                continue

            element = next((e for e in elements if e['index'] == result.element_index), None)
            if not element:
                print(f"[!] LLM chose index {result.element_index} which doesn't exist in filtered list.")
                continue

            field_label = element.get('labelText', element.get('text', ''))
            print(f"    [{result.action_type.upper()}] '{field_label}' -> '{result.value}'")

            # Anti-loop guard
            loop_counts[field_label] = loop_counts.get(field_label, 0) + 1
            if loop_counts[field_label] >= 3:
                print(f"    [LOOP] Forcing skip on '{field_label}'")
                filled_labels.add(field_label)
                loop_counts[field_label] = 0
                await page.keyboard.press("Escape")
                continue

            if result.action_type == 'skip':
                filled_labels.add(field_label)
                continue

            # Execute
            try:
                locator = await find_locator(page, element)

                if result.action_type == 'type':
                    await locator.scroll_into_view_if_needed(timeout=5000)
                    await locator.fill(result.value)
                    await locator.evaluate("el => el.dispatchEvent(new Event('input', {bubbles:true}))")
                    await locator.evaluate("el => el.dispatchEvent(new Event('blur', {bubbles:true}))")
                    if await verify_value(page, element, result.value):
                        filled_labels.add(field_label)
                    else:
                        print(f"    [VERIFY_FAIL] Value for '{field_label}' did not register in DOM.")

                elif result.action_type == 'select':
                    await locator.scroll_into_view_if_needed(timeout=5000)
                    if element['type'] == 'search_dropdown':
                        await do_search_select(page, locator, result.value)
                    elif element['type'] == 'select':
                        try:
                            await locator.select_option(label=result.value, timeout=3000)
                        except Exception:
                            # Workday sometimes wraps <select> in a custom widget; fall back to search
                            await do_search_select(page, locator, result.value)
                    
                    if await verify_value(page, element, result.value):
                        filled_labels.add(field_label)
                    else:
                        print(f"    [VERIFY_FAIL] Value for '{field_label}' did not register in DOM.")

                elif result.action_type == 'radio':
                    # Find the label element with matching text and click it
                    radio_label = page.locator(
                        f"label:has-text('{result.value}'), "
                        f"[data-automation-id='radioBtn']:has-text('{result.value}')"
                    ).first
                    if await radio_label.is_visible():
                        await radio_label.scroll_into_view_if_needed(timeout=5000)
                        await radio_label.click(force=True)
                    else:
                        # Fallback: click the element the LLM pointed to
                        await locator.scroll_into_view_if_needed(timeout=5000)
                        await locator.click(force=True)
                    
                    if await verify_value(page, element, result.value):
                        filled_labels.add(field_label)
                    else:
                        print(f"    [VERIFY_FAIL] Value for '{field_label}' did not register in DOM.")

                elif result.action_type == 'checkbox':
                    is_checked = await locator.is_checked()
                    want_on = result.value.lower() in ('on', 'true', 'yes', 'checked')
                    if is_checked != want_on:
                        await locator.scroll_into_view_if_needed(timeout=5000)
                        await locator.click(force=True)
                    
                    if await verify_value(page, element, result.value):
                        filled_labels.add(field_label)
                    else:
                        print(f"    [VERIFY_FAIL] Value for '{field_label}' did not register in DOM.")

                elif result.action_type == 'click':
                    await locator.scroll_into_view_if_needed(timeout=5000)
                    url_before = page.url
                    labels_before = {e.get('labelText', e.get('text', '')) for e in raw_elements if e.get('labelText', e.get('text', ''))}
                    
                    await locator.click(force=True)
                    nav_keywords = ('next', 'continue', 'save', 'submit', 'review')
                    if any(w in field_label.lower() for w in nav_keywords):
                        print("[*] Navigation clicked — checking navigation status...")
                        await page.wait_for_timeout(5000)
                        
                        url_after = page.url
                        raw_elements_after = await page.evaluate(SCAN_JS)
                        labels_after = {e.get('labelText', e.get('text', '')) for e in raw_elements_after if e.get('labelText', e.get('text', ''))}
                        
                        if url_after != url_before or labels_after != labels_before:
                            print("[+] Navigation successful! Clearing filled labels for new page.")
                            filled_labels.clear()
                            loop_counts.clear()
                        else:
                            print("[!] Navigation failed (URL and fields did not change). Keeping filled labels.")

            except Exception as e:
                print(f"    [!] Action failed: {e}")

        # ----------------------------------------------------------------
        print("\n" + "=" * 60)
        print("APPLICATION LOOP CONCLUDED")
        print("=" * 60)
        await asyncio.to_thread(input, "\nPress [Enter] to close browser...")


if __name__ == "__main__":
    asyncio.run(main())
