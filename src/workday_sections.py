"""
workday_sections.py — Workday page section handlers and add-dialogs
==================================================================
Handlers for My Experience (work history, education, languages, skills),
Voluntary Disclosures, Self Identify, document uploads, and smart_fill_page.
"""

import asyncio
import datetime
import json
import re
import sys
from playwright.async_api import Page

from app_common import (
    LIBRARY, PI, WE, EDU, LANG, RESUME_PATH, TRANSCRIPT_PATH,
    DEEPSEEK_KEY, PHONE_DIGITS, PROFILE_SUMMARY, SYSTEM_PROMPT,
    deepseek_fill_page, deepseek_pick_option, label_match, pick_decline,
)
from workday_scan import SCAN_JS, get_heading, read_validation_errors
from workday_executors import (
    SETTLE_MS, SEARCH_MS, MEDIUM_MS, SAVE_MS, MONTH_NUM,
    exec_text, exec_button_dropdown, exec_selectinput, exec_radio, exec_checkbox,
    execute_answer, prefetch_options, save_and_continue, save_and_continue_with_report,
)


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


async def handle_document_uploads(page: Page):
    """
    Intelligently handles document uploads (Resume and Transcript).
    Routes transcript-specific dropzones to TRANSCRIPT_PATH and resume dropzones to RESUME_PATH.
    Traverses parent containers to accurately classify dropzones based on section headers and labels.
    """
    dropzone_info = await page.evaluate("""() => {
        const results = [];
        const inputs = Array.from(document.querySelectorAll('input[type="file"]'));
        const dzs = Array.from(document.querySelectorAll('[data-automation-id="file-upload-drop-zone"]'));
        const allElements = Array.from(new Set([...inputs, ...dzs]));
        for (const el of allElements) {
            let text = '';
            let curr = el;
            for (let i = 0; i < 8; i++) {
                if (!curr) break;
                if (curr.innerText && curr.innerText.length > text.length) {
                    text = curr.innerText;
                }
                curr = curr.parentElement;
            }
            results.push({
                tag: el.tagName.toLowerCase(),
                contextText: text.toLowerCase()
            });
        }
        return results;
    }""")

    if not dropzone_info:
        return

    # Check existing uploaded files
    existing_uploads = await page.evaluate("""() =>
        Array.from(document.querySelectorAll('[data-automation-id="file-upload-item-name"]'))
            .map(el => el.innerText.trim()).filter(Boolean)""")
    existing_lower = [f.lower() for f in existing_uploads]

    file_inputs = page.locator("input[type='file']")
    if not await file_inputs.count():
        try:
            dz = page.locator("[data-automation-id='file-upload-drop-zone']").first
            if await dz.count():
                await dz.click()
                await page.wait_for_timeout(800)
        except Exception:
            pass
        file_inputs = page.locator("input[type='file']")

    input_count = await file_inputs.count()
    if not input_count:
        return

    transcript_keywords = ("transcript", "academic record", "school record", "grades", "mark sheet", "education document", "diploma")
    resume_keywords = ("resume", "cv", "curriculum vitae")

    for i in range(input_count):
        inp = file_inputs.nth(i)
        ctx_text = dropzone_info[i]["contextText"] if i < len(dropzone_info) else ""
        
        is_transcript_zone = any(k in ctx_text for k in transcript_keywords)
        is_resume_zone = any(k in ctx_text for k in resume_keywords) and not is_transcript_zone

        if is_transcript_zone:
            if TRANSCRIPT_PATH and not any("transcript" in f for f in existing_lower):
                await inp.set_input_files(TRANSCRIPT_PATH)
                await page.wait_for_timeout(2000)
                print(f"    ✓ transcript uploaded ({TRANSCRIPT_PATH})")
        elif is_resume_zone or (not is_transcript_zone and not existing_uploads and input_count == 1):
            if RESUME_PATH and not any("resume" in f for f in existing_lower):
                await inp.set_input_files(RESUME_PATH)
                await page.wait_for_timeout(2000)
                print(f"    ✓ resume uploaded ({RESUME_PATH})")


async def smart_fill_page(page: Page, heading: str, context_hint: str = "",
                          exclude_ids: set = None):
    print(f"\n  [SCAN] '{heading}'...")

    # Scroll to bottom then top to trigger lazy-rendering of all form sections
    await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
    await page.wait_for_timeout(SEARCH_MS)
    await page.evaluate("() => window.scrollTo(0, 0)")
    await page.wait_for_timeout(400)

    # Handle document uploads (Resume and Transcript)
    await handle_document_uploads(page)

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

    # DeepSeek evaluates all fields (including qualified date spinbuttons)
    if not DEEPSEEK_KEY:
        raise RuntimeError("DEEPSEEK_API_KEY is required in data/.env.")
    print(f"  [LLM] Sending {len(fillable)} fields to DeepSeek for semantic evaluation...")
    answers = await deepseek_fill_page(fillable)
    print(f"  [LLM] Got {len(answers)} answers")

    field_map = {f["index"]: f for f in fillable}

    # Execute DeepSeek/rule answers first (checkboxes before other fields)
    for ans in sorted(answers,
                      key=lambda a: 0 if field_map.get(a.get("index",0),{}).get("type") in ("checkbox","radio") else 1):
        idx = ans.get("index"); val = ans.get("value","")
        if idx is None or not val: continue
        field = field_map.get(idx)
        if field:
            lbl = field.get("label", "").strip("* ").lower()
            if label_match(lbl, "country") and "phone" not in lbl:
                curr_val = field.get("value", "").lower()
                if "united states of america" in curr_val or "united states" in curr_val:
                    print(f"    ✓ drop  [{field['index']}] 'Country' = 'United States of America' (already pre-filled)")
                    continue
                val = PI.get("country", "United States of America")
            if label_match(lbl, "major", "field of study", "discipline", "area of study"):
                if EDU and "\n" not in str(val):
                    primary_term = str(val).strip()
                    all_variants = []
                    for e in EDU:
                        all_variants.extend(e.get("major_variants", []))
                    fallbacks = list(dict.fromkeys([primary_term] + all_variants))
                    val = "\n".join(t for t in fallbacks if t)
            await execute_answer(page, field, val)
            await page.wait_for_timeout(200)

    # Post-fill phone number
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
                await phone_inp.scroll_into_view_if_needed(timeout=3000)
                await phone_inp.click(timeout=3000)
                print(f"    ✓ phone post-fill: already {PHONE_DIGITS}")
            await page.keyboard.press("Tab")
            await page.wait_for_timeout(300)


# ── Add-dialog filler (My Experience modals) ─────────────────────────────────

def _match_opt(opts: list[str], val: str) -> str | None:
    if not val or not opts:
        return None
    vl = val.strip().lower()
    for o in opts:
        if o.strip().lower() == vl:
            return o
    vn = re.sub(r'[^a-z0-9]', '', vl)
    if vn:
        for o in opts:
            if re.sub(r'[^a-z0-9]', '', o.lower()) == vn:
                return o
    return None


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
            if label_match(label, "language","lang name","name") or (not label and field.get("isSelectInput")):
                return entry.get("language","")
            if label_match(label, "proficiency","fluency","level","ability",
                           "reading","writing","speaking","conversation","comprehension"):
                return entry.get("proficiency","")
        if is_work:
            if label_match(label, "description","responsibilities","summary","duties","role description"):
                desc = entry.get("description","")
                ml = field.get("maxlength") or 0
                cap = ml if ml > 0 else 4000
                return desc[:cap]
            if label_match(label, "job title","title","position") or \
               (label_match(label, "role") and not label_match(label, "description")):
                return entry.get("role","")
            if label_match(label, "company", "employer", "organization"):
                return entry.get("company","")
            if label_match(label, "city", "location") and not label_match(label, "country","state"):
                return entry.get("city","")
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
                terms = [entry.get("major_search_term", "")] + entry.get("major_variants", [])
                terms = list(dict.fromkeys(t for t in terms if t))
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
                return str(entry.get("end_year",""))

    # ── Button dropdowns (month/degree pickers) ───────────────────────────────
    if tag == "button" or ftype == "select-one":
        if not opts:
            if is_edu and label_match(label, "degree", "degree type", "level of education"):
                dtype = entry.get("degree_type", "")
                abbrev = entry.get("degree_abbreviation", "")
                kw = "Masters" if "master" in dtype.lower() else ("Bachelors" if "bachelor" in dtype.lower() else "")
                terms = [dtype, kw, abbrev] + entry.get("degree_search_variants", [])
                return "\n".join(dict.fromkeys(t for t in terms if t))
            return None
        if is_lang:
            if label_match(label, "language","lang name","name"):
                return _match_opt(opts, entry.get("language",""))
            if label_match(label, "proficiency","fluency","level","ability",
                           "reading","writing","speaking","overall","conversation","comprehension"):
                return _match_opt(opts, entry.get("proficiency",""))
        if is_work:
            if label_match(label, "start month") or (label_match(label,"month") and date_seq=="start"):
                return _match_opt(opts, entry.get("start_month",""))
            if label_match(label, "end month") or (label_match(label,"month") and date_seq=="end"):
                cur = entry.get("current_status","").lower()
                return "" if "current" in cur else _match_opt(opts, entry.get("end_month",""))
            if label_match(label, "currently work","still work","current job","present"):
                cur = entry.get("current_status","").lower()
                want = "Yes" if "current" in cur else "No"
                return _match_opt(opts, want)
        if is_edu:
            if label_match(label, "degree","degree type","level of education"):
                dtype = entry.get("degree_type","")
                kw = "Masters" if "master" in dtype.lower() else ("Bachelors" if "bachelor" in dtype.lower() else "")
                candidates = [dtype, kw] + entry.get("degree_search_variants", []) + [entry.get("degree_abbreviation", "")]
                for cand in candidates:
                    if cand:
                        hit = _match_opt(opts, cand)
                        if hit: return hit
                terms = [dtype, kw, entry.get("degree_abbreviation", "")] + entry.get("degree_search_variants", [])
                return "\n".join(dict.fromkeys(t for t in terms if t))
            if label_match(label, "start month") or (label_match(label,"month") and date_seq=="start"):
                return _match_opt(opts, entry.get("start_month",""))
            if label_match(label, "end month","graduation month") or (label_match(label,"month") and date_seq=="end"):
                return _match_opt(opts, entry.get("end_month",""))
            if label_match(label, "currently attend","still attend","current student","present"):
                cur = entry.get("current_status","").lower()
                want = "Yes" if "attending" in cur else "No"
                return _match_opt(opts, want)

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
            return _match_opt(radio_opts, want)
        if is_edu and label_match(label, "currently attend","still attend","present"):
            cur = entry.get("current_status","").lower()
            want = "Yes" if "attending" in cur else "No"
            return _match_opt(radio_opts, want)

    return None


def is_section_anchor(field: dict, section_type: str) -> bool:
    """Identify if a field represents the primary anchor of a card entry in My Experience."""
    lbl = field.get("label", "").lower().strip()
    if section_type == "edu":
        return any(kw in lbl for kw in ("school or university", "school", "university", "institution", "college"))
    elif section_type == "work":
        return any(kw in lbl for kw in ("job title", "title"))
    elif section_type == "lang":
        return ("language" in lbl and not any(kw in lbl for kw in ("fluent", "proficiency", "overall", "reading", "speaking", "writing", "comprehension")))
    return False


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
        "anchor_kws": ("language",),
        "stop_kws": ("type to add skills", "website", "job title", "school",
                     "upload a file", "linkedin", "provide your linkedin"),
    },
}


async def fill_add_dialog(page: Page, dialog_label: str, entry: dict = None, section_type: str = "",
                          count_before: int = 0, fields_before: list = None,
                          entry_index: int = None):
    """Scan an open add-dialog, fill only the specified (or LAST) entry group."""
    await page.wait_for_timeout(MEDIUM_MS)
    all_fields = await page.evaluate(SCAN_JS, None)

    spec = SECTION_SPEC.get(section_type, {})
    stop_kws   = spec.get("stop_kws", ())

    anchor_positions = [
        i for i, f in enumerate(all_fields)
        if is_section_anchor(f, section_type)
    ]
    if anchor_positions:
        if entry_index is not None and entry_index < len(anchor_positions):
            target_start = anchor_positions[entry_index]
            next_anchor = anchor_positions[entry_index + 1] if entry_index + 1 < len(anchor_positions) else len(all_fields)
        else:
            target_start = anchor_positions[-1]
            next_anchor = len(all_fields)

        stop_pos = next_anchor
        for i in range(target_start + 1, min(next_anchor, len(all_fields))):
            lbl = all_fields[i].get("label","").lower()
            if any(kw in lbl for kw in stop_kws):
                stop_pos = i
                break
        fields = all_fields[target_start: stop_pos]

        if section_type == "edu" and fields:
            anchor_field = fields[0]
            anchor_idx = anchor_field["index"]
            scope_attr = f"data-entry-scope"
            scope_val  = f"edu-{anchor_idx}"
            scope_sel  = f"[{scope_attr}='{scope_val}']"
            stamped = await page.evaluate("""(args) => {
                const el = document.querySelector('[data-fill-idx="' + args.anchorIdx + '"]');
                if (!el) return false;
                let node = el.closest('[data-automation-id^="formField"]') || el;
                for (let i = 0; i < 6; i++) {
                    node = node.parentElement;
                    if (!node) break;
                    const ffs = node.querySelectorAll('[data-automation-id^="formField"]');
                    if (ffs.length >= 2) {
                        node.setAttribute(args.scopeAttr, args.scopeVal);
                        return true;
                    }
                }
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
    elif section_type == "lang" and fields_before is not None:
        def field_id(f):
            if f.get("id"): return "id:" + f["id"]
            if f.get("auto"): return "auto:" + f["auto"]
            return "lt:" + f.get("label","") + "|" + f.get("tag","")
        before_ids = set(field_id(f) for f in fields_before)
        fields = [f for f in all_fields if field_id(f) not in before_ids]
        trimmed = []
        for f in fields:
            if any(kw in f.get("label","").lower() for kw in stop_kws):
                break
            trimmed.append(f)
        fields = trimmed
    else:
        fields = all_fields[count_before:] if count_before > 0 else all_fields

    for f in fields:
        f["page_heading"] = dialog_label
        f["section"] = dialog_label

    if section_type == "work" and fields:
        first = fields[0]
        if not first.get("label") and first.get("tag") in ("input","textarea") \
                and first.get("type","text") not in ("checkbox","radio","button","select-one"):
            first["label"] = "Job Title"

    month_seq = 0; year_seq = 0
    for f in fields:
        lbl = f.get("label","").lower().strip("* ")
        if lbl == "month" or lbl.endswith("- month") or lbl.endswith(" month"):
            f["date_seq"] = "start" if month_seq == 0 else "end"
            month_seq += 1
        elif lbl == "year" or lbl.endswith("- year") or lbl.endswith(" year"):
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

    if not DEEPSEEK_KEY:
        raise RuntimeError("DEEPSEEK_API_KEY is required in data/.env.")
    answers = await deepseek_fill_page(fillable, entry=entry, section_type=section_type)
    answered_indices = {a.get("index") for a in answers if a.get("value")}
    for f in fillable:
        if f["index"] not in answered_indices and entry and section_type:
            val = entry_answer(f, entry, section_type)
            if val is not None and val != "":
                answers.append({"index": f["index"], "value": val})

    field_map = {f["index"]: f for f in fillable}
    for ans in answers:
        idx = ans.get("index"); val = ans.get("value","")
        if idx is None or not val: continue
        field = field_map.get(idx)
        if field and (field.get("type") in ("checkbox","radio") or field.get("role") in ("checkbox","radio")):
            await execute_answer(page, field, val)
            await page.wait_for_timeout(400)
    for ans in answers:
        idx = ans.get("index"); val = ans.get("value","")
        if idx is None or not val: continue
        field = field_map.get(idx)
        if field and field.get("type") not in ("checkbox","radio") and field.get("role") not in ("checkbox","radio"):
            lbl = field.get("label", "").strip("* ").lower()
            is_select_input = field.get("isSelectInput", False)
            if label_match(lbl, "major", "field of study", "discipline", "area of study"):
                if is_select_input:
                    if entry and "\n" not in str(val):
                        primary_term = str(val).strip()
                        variants = entry.get("major_variants", [])
                        fallbacks = list(dict.fromkeys([primary_term] + variants))
                        val = "\n".join(t for t in fallbacks if t)
                else:
                    val = str(val).split("\n")[0].strip()
            elif label_match(lbl, "school", "university", "institution", "college"):
                if is_select_input:
                    if entry and "\n" not in str(val):
                        primary_term = str(val).strip()
                        variants = entry.get("institution_variants", [])
                        fallbacks = list(dict.fromkeys([primary_term] + variants))
                        val = "\n".join(t for t in fallbacks if t)
                else:
                    val = str(val).split("\n")[0].strip()
            elif label_match(lbl, "degree", "degree type", "level of education"):
                if entry and "\n" not in str(val):
                    dtype = entry.get("degree_type", "")
                    abbrev = entry.get("degree_abbreviation", "")
                    kw = "Masters" if "master" in dtype.lower() else ("Bachelors" if "bachelor" in dtype.lower() else "")
                    terms = [dtype, kw, abbrev] + entry.get("degree_search_variants", [])
                    val = "\n".join(dict.fromkeys(t for t in terms if t))
            elif not is_select_input and field.get("tag") == "input":
                val = str(val).split("\n")[0].strip()
            await execute_answer(page, field, val)
            await page.wait_for_timeout(200)

    await page.wait_for_timeout(500)
    has_combobox = any(f.get("isSelectInput") for f in fillable)
    if has_combobox and section_type == "edu":
        await page.wait_for_timeout(1500)
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
    return items.map(e => e.innerText.trim().toLowerCase()).filter(Boolean);
}"""

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
    """Type search_term into input_loc, wait for [role=option] results, click best_match."""
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
    match = next((r for r in results if r.strip().lower() == best_match.strip().lower()), None)
    if not match:
        t_norm = re.sub(r'[^a-z0-9]', '', best_match.lower())
        match = next((r for r in results if re.sub(r'[^a-z0-9]', '', r.lower()) == t_norm), None)
    if not match and results:
        from app_common import deepseek_pick_option
        match = await deepseek_pick_option(best_match, results, context="Option search picker")
    if not match:
        match = results[0]
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
    """Fill a skills selectinput: for each skill, type → Enter → pick best match."""
    fid = field.get('id', '')
    idx = field['index']

    current_pills: set[str] = set(await page.evaluate(PILLS_JS, {"fid": fid, "idx": idx}))
    if current_pills:
        print(f"    [SKILLS] {len(current_pills)} already-selected pills found: {sorted(current_pills)[:8]}{'...' if len(current_pills)>8 else ''}")

    def is_already_selected(name: str) -> bool:
        n = name.lower().strip()
        for p in current_pills:
            if n == p:
                return True
            if len(n) < 3:
                continue
            try:
                if re.search(r'(?<![a-zA-Z0-9])' + re.escape(n) + r'(?![a-zA-Z0-9])', p):
                    return True
                if len(p) >= 3 and re.search(r'(?<![a-zA-Z0-9])' + re.escape(p) + r'(?![a-zA-Z0-9])', n):
                    return True
            except re.error:
                pass
        return False

    # If all prefiltered target skills for this job are already selected, skip!
    unselected_skills = [s for s in skills if not is_already_selected(s)]
    if not unselected_skills:
        print(f"    ✓ [SKILLS] All target skills already selected ({len(current_pills)} pills present) — skipping skill fill!")
        return

    for skill in unselected_skills:
        if page.is_closed():
            print(f"    [SKILLS] page closed mid-loop — stopping skill fill")
            break

        if is_already_selected(skill):
            print(f"    ~ skill '{skill}' → already selected (pre-check), skipping")
            continue

        if fid:
            inp = page.locator(f"input#{fid}").first
        else:
            inp = page.locator(f"[data-fill-idx='{idx}']").first
        try:
            await inp.scroll_into_view_if_needed(timeout=5000)
            await inp.click(force=True, timeout=5000)
        except Exception:
            pass

        await inp.click(click_count=3, force=True)
        await inp.type(skill, delay=70)
        await page.wait_for_timeout(300)
        await inp.press("Enter")

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
        for _wait in [800, 800, 800, 600]:
            await page.wait_for_timeout(_wait)
            results = await page.evaluate(READ_JS)
            if results:
                break

        if not results:
            await inp.press("Escape")
            await page.wait_for_timeout(600)
            print(f"    ~ skill '{skill}' — no results")
            continue

        fresh_pills = set(await page.evaluate(PILLS_JS, {"fid": fid, "idx": idx}))
        current_pills.update(fresh_pills)

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
            if is_already_selected(best):
                print(f"    ~ skill '{skill}' → {best!r} already selected, skipping")
                await inp.press("Escape")
                await page.wait_for_timeout(300)
                continue
        else:
            best = min(results, key=rule_score)
            if rule_score(best) >= 99:
                await inp.press("Escape")
                await page.wait_for_timeout(400)
                print(f"    ~ skill '{skill}' → no relevant match (best: {best!r}), skipping")
                continue
            print(f"    → skill '{skill}' → rule picked {best!r}")

            if is_already_selected(best):
                print(f"    ~ skill '{skill}' → {best!r} already selected, skipping")
                await inp.press("Escape")
                await page.wait_for_timeout(300)
                continue

        best_lower = best[:60].lower()

        if DEEPSEEK_KEY:
            await inp.click(click_count=3, force=True)
            await inp.type(skill, delay=70)
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
            current_pills.add(best.lower())
        except Exception as e:
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
        await inp.press("Escape")
        await page.wait_for_timeout(300)


async def delete_extra_cards(page: Page, section_type: str, max_allowed: int) -> int:
    """Ensure the number of cards in section_type ('edu', 'work', 'lang') does not exceed max_allowed.
    Accurately counts cards by anchor fields, clicks delete button on excess cards,
    confirms modal, and verifies deletion. Returns count of deleted cards."""
    deleted = 0
    keyword = "education|school|formation|degree" if section_type == "edu" else ("work\\s+experience|employment|job\\s+history|work\\s+history|professional\\s+experience|expérience\\s+professionnelle|emplois" if section_type == "work" else "language|langue")

    for _ in range(8):
        info = await page.evaluate("""(args) => {
            const pageTitle = (document.querySelector('h1, h2, [data-automation-id="progressBarActiveStep"]')?.innerText || '').trim().toLowerCase();
            const headings = Array.from(document.querySelectorAll('h3, h4, [data-automation-id="sectionTitle"], [data-automation-id="groupTitle"]'));
            const h = headings.find(el => {
                const t = el.innerText.trim().toLowerCase();
                return t && t !== pageTitle && new RegExp(args.keyword, 'i').test(t);
            });
            if (!h) return { cardCount: 0, delBtnsCount: 0 };

            let sec = h.parentElement;
            while (sec && sec !== document.body) {
                if (sec.querySelectorAll('[data-automation-id="add-button"]').length) break;
                sec = sec.parentElement;
            }
            if (!sec) return { cardCount: 0, delBtnsCount: 0 };

            let anchors = [];
            if (args.sectionType === 'edu') {
                anchors = Array.from(sec.querySelectorAll('[data-automation-id="formField-school"], [data-automation-id*="school" i]'));
                if (anchors.length === 0) {
                    anchors = Array.from(sec.querySelectorAll('input, button')).filter(el => {
                        const lbl = (el.getAttribute('aria-label') || el.closest('[data-automation-id^="formField"]')?.querySelector('label')?.innerText || '').toLowerCase();
                        return /school|university|institution|college/i.test(lbl);
                    });
                }
            } else if (args.sectionType === 'work') {
                anchors = Array.from(sec.querySelectorAll('[data-automation-id="formField-jobTitle"]'));
                if (anchors.length === 0) {
                    anchors = Array.from(sec.querySelectorAll('input')).filter(el => {
                        const lbl = (el.getAttribute('aria-label') || el.closest('[data-automation-id^="formField"]')?.querySelector('label')?.innerText || '').toLowerCase();
                        return /job title|title/i.test(lbl);
                    });
                }
            } else if (args.sectionType === 'lang') {
                anchors = Array.from(sec.querySelectorAll('button[aria-haspopup="listbox"], button[data-automation-id*="select"]')).filter(b => {
                    const lbl = (b.getAttribute('aria-label') || b.closest('[data-automation-id^="formField"]')?.querySelector('label')?.innerText || '').toLowerCase();
                    return /language/i.test(lbl) && !/fluent|comprehension|overall|reading|speaking|writing|proficiency/i.test(lbl);
                });
            }

            const delBtns = Array.from(sec.querySelectorAll(
                '[data-automation-id="DELETE_charm"], [data-automation-id="delete-button"], [data-automation-id="panelActions"] button, button[aria-label*="Delete" i], button[aria-label*="Remove" i], button'
            )).filter(b => b.getAttribute('data-automation-id') === 'DELETE_charm' || b.getAttribute('data-automation-id') === 'delete-button' || b.closest('[data-automation-id="panelActions"]') || /delete|remove/i.test(b.getAttribute('aria-label') || '') || /delete/i.test(b.innerText.trim()));

            return { cardCount: anchors.length, delBtnsCount: delBtns.length };
        }""", {"keyword": keyword, "sectionType": section_type})

        card_count = info.get("cardCount", 0)
        del_count = info.get("delBtnsCount", 0)

        if card_count <= max_allowed:
            break

        if del_count == 0:
            print(f"  [CLEANUP] {section_type}: {card_count} cards present (> {max_allowed}), but 0 delete buttons found")
            break

        print(f"  [CLEANUP] {section_type}: {card_count} cards present (max {max_allowed}) — deleting extra card...")

        # Click the last delete button
        clicked = await page.evaluate("""(args) => {
            const pageTitle = (document.querySelector('h1, h2, [data-automation-id="progressBarActiveStep"]')?.innerText || '').trim().toLowerCase();
            const headings = Array.from(document.querySelectorAll('h3, h4, [data-automation-id="sectionTitle"], [data-automation-id="groupTitle"]'));
            const h = headings.find(el => {
                const t = el.innerText.trim().toLowerCase();
                return t && t !== pageTitle && new RegExp(args.keyword, 'i').test(t);
            });
            if (!h) return false;
            let sec = h.parentElement;
            while (sec && sec !== document.body) {
                if (sec.querySelectorAll('[data-automation-id="add-button"]').length) break;
                sec = sec.parentElement;
            }
            if (!sec) return false;

            const delBtns = Array.from(sec.querySelectorAll(
                '[data-automation-id="DELETE_charm"], [data-automation-id="delete-button"], [data-automation-id="panelActions"] button, button[aria-label*="Delete" i], button[aria-label*="Remove" i], button'
            )).filter(b => b.getAttribute('data-automation-id') === 'DELETE_charm' || b.getAttribute('data-automation-id') === 'delete-button' || b.closest('[data-automation-id="panelActions"]') || /delete|remove/i.test(b.getAttribute('aria-label') || '') || /delete/i.test(b.innerText.trim()));

            if (delBtns.length > 0) {
                const btn = delBtns[delBtns.length - 1];
                btn.scrollIntoView({ block: 'center' });
                btn.click();
                return true;
            }
            return false;
        }""", {"keyword": keyword})

        if not clicked:
            break

        await page.wait_for_timeout(1000)

        # Handle confirmation dialog
        confirm_clicked = await page.evaluate("""() => {
            const modal = document.querySelector('[role="dialog"], [data-automation-id="promptPopup"], [data-automation-id="deleteConfirmationModal"], [data-automation-id="confirmModal"]');
            if (modal) {
                const btns = Array.from(modal.querySelectorAll('button[data-automation-id="confirmButton"], button[data-automation-id="delete-confirmation-button"], button[data-automation-id="click_filter"], button'));
                const confirmBtn = btns.find(b => /delete|confirm/i.test(b.innerText.trim()) || b.getAttribute('data-automation-id') === 'confirmButton');
                if (confirmBtn) {
                    confirmBtn.click();
                    return true;
                }
            }
            return false;
        }""")

        if not confirm_clicked:
            dialog = page.locator("[role='dialog']").first
            if await dialog.count():
                c_btn = dialog.locator("button:has-text('Delete'), button:has-text('Confirm'), [data-automation-id='confirmButton'], [data-automation-id='delete-confirmation-button']").first
                if await c_btn.count():
                    try:
                        await c_btn.click(timeout=3000)
                    except Exception:
                        pass

        await page.wait_for_timeout(1500)
        deleted += 1
        print(f"  [CLEANUP] Deleted 1 extra card in '{section_type}'")

    return deleted


def pair_cards_with_entries(cards_or_anchors: list[dict], data_list: list[dict], section_type: str) -> list[dict]:
    """Pair each card on the page with the best matching entry from data_list,
    preserving existing valid assignments and avoiding unnecessary swaps."""
    n_cards = len(cards_or_anchors)
    paired = [None] * n_cards
    used_entries = set()

    def norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]", "", str(s).lower())

    # Pass 1: Match cards that already display an entry's identifier
    for card_idx, item in enumerate(cards_or_anchors):
        if section_type == "lang":
            val = norm(item.get("language") or item.get("value") or "")
        elif section_type == "edu":
            val = norm(item.get("school") or item.get("value") or "")
        elif section_type == "work":
            val = norm(item.get("company") or item.get("value") or "")
        else:
            val = norm(item.get("value") or "")

        if not val or val in ("selectone", "select"):
            continue

        for ent_idx, ent in enumerate(data_list):
            if ent_idx in used_entries:
                continue
            if section_type == "lang":
                exp = norm(ent.get("language", ""))
                if exp and (exp in val or val in exp):
                    paired[card_idx] = ent
                    used_entries.add(ent_idx)
                    break
            elif section_type == "edu":
                s_variants = [norm(v) for v in ent.get("institution_variants", []) if v] + [norm(ent.get("school", ""))]
                if any(v and (v in val or val in v) for v in s_variants if v):
                    paired[card_idx] = ent
                    used_entries.add(ent_idx)
                    break
            elif section_type == "work":
                c_variants = [norm(ent.get("company", ""))] + [norm(v) for v in ent.get("company_variants", []) if v]
                if any(v and (v in val or val in v) for v in c_variants if v):
                    paired[card_idx] = ent
                    used_entries.add(ent_idx)
                    break

    # Pass 2: Assign remaining unused entries to unpaired cards
    unused_ents = [ent for i, ent in enumerate(data_list) if i not in used_entries]
    for card_idx in range(n_cards):
        if paired[card_idx] is None and unused_ents:
            paired[card_idx] = unused_ents.pop(0)

    # Fallback for any still unpaired
    for card_idx in range(n_cards):
        if paired[card_idx] is None:
            paired[card_idx] = data_list[min(card_idx, len(data_list) - 1)]

    return paired


async def audit_my_experience_section(page: Page, section_type: str, expected_list: list[dict]) -> tuple[bool, list[dict], list[str]]:
    """Inspect all cards in a section from live DOM using SCAN_JS and verify exact correctness against candidate library."""
    all_fields = await page.evaluate(SCAN_JS, None)

    spec = SECTION_SPEC.get(section_type, {})
    stop_kws = spec.get("stop_kws", ())

    anchor_positions = [
        i for i, f in enumerate(all_fields)
        if is_section_anchor(f, section_type)
    ]

    # If the section does not appear at all on the page, don't fail if no anchors are found
    if len(anchor_positions) == 0:
        has_section = await page.evaluate("""(kws) => {
            const pageTitle = (document.querySelector('h1, h2, [data-automation-id="progressBarActiveStep"]')?.innerText || '').trim().toLowerCase();
            const headings = Array.from(document.querySelectorAll('h3, h4, [data-automation-id="sectionTitle"], [data-automation-id="groupTitle"]'));
            const hasHeading = headings.some(h => {
                const t = h.innerText.trim().toLowerCase();
                if (!t || t === pageTitle) return false;
                return kws.some(kw => t.includes(kw));
            });
            if (hasHeading) return true;

            // Check if any add-button on the page targets this section
            const addBtns = Array.from(document.querySelectorAll('[data-automation-id="add-button"], button[aria-label*="Add" i]'));
            return addBtns.some(b => {
                const lbl = (b.getAttribute('aria-label') || b.innerText || '').toLowerCase();
                let p = b.parentElement;
                let parentH = '';
                while (p && p !== document.body) {
                    const h = p.querySelector('h3, h4, [data-automation-id="sectionTitle"], [data-automation-id="groupTitle"]');
                    if (h) { parentH = h.innerText.trim().toLowerCase(); break; }
                    p = p.parentElement;
                }
                return kws.some(kw => lbl.includes(kw) || (parentH && parentH !== pageTitle && parentH.includes(kw)));
            });
        }""", ["education", "school", "formation", "études", "degree"] if section_type == "edu" else (["work experience", "employment", "job history", "work history", "professional experience", "expérience professionnelle", "emplois"] if section_type == "work" else ["language", "langue", "langues"]))
        if not has_section:
            return True, [], []

    cards = []
    for i, start_pos in enumerate(anchor_positions):
        next_pos = anchor_positions[i + 1] if i + 1 < len(anchor_positions) else len(all_fields)
        card_fields = []
        for f in all_fields[start_pos:next_pos]:
            lbl = f.get("label", "").lower()
            if any(kw in lbl for kw in stop_kws):
                break
            card_fields.append(f)

        card_data = {"cardIndex": i}
        if section_type == "edu":
            for f in card_fields:
                lbl = f.get("label", "").lower()
                val = f.get("value", "").strip()
                if any(k in lbl for k in ("school", "university", "institution", "college")):
                    card_data["school"] = val
                elif "degree" in lbl:
                    card_data["degree"] = val
                elif any(k in lbl for k in ("field of study", "major", "discipline", "area of study")):
                    card_data["fieldOfStudy"] = val
        elif section_type == "work":
            for f in card_fields:
                lbl = f.get("label", "").lower()
                val = f.get("value", "").strip()
                if "job title" in lbl or lbl == "title":
                    card_data["title"] = val
                elif any(k in lbl for k in ("company", "employer")):
                    card_data["company"] = val
                elif "currently work here" in lbl or "current" in lbl:
                    card_data["currentlyWorkHere"] = (val.lower() == "true")
        elif section_type == "lang":
            for f in card_fields:
                lbl = f.get("label", "").lower()
                val = f.get("value", "").strip()
                if is_section_anchor(f, "lang"):
                    card_data["language"] = val
                elif any(k in lbl for k in ("proficiency", "comprehension", "overall")):
                    card_data["proficiency"] = val
        cards.append(card_data)

    errors = []
    if len(cards) != len(expected_list):
        errors.append(f"Card count mismatch: found {len(cards)}, expected {len(expected_list)}")
        return False, cards, errors

    def norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]", "", str(s).lower())

    paired_expected = pair_cards_with_entries(cards, expected_list, section_type)

    if section_type == "edu":
        for idx, (card, exp) in enumerate(zip(cards, paired_expected)):
            s_val = norm(card.get("school", ""))
            s_variants = [norm(v) for v in exp.get("institution_variants", []) if v] + [norm(exp.get("school", ""))]
            if not s_val:
                errors.append(f"Card {idx}: school is blank (expected {exp.get('institution_variants')})")
            elif not any((v and v in s_val) or (s_val in v) for v in s_variants if v):
                errors.append(f"Card {idx}: school {card.get('school')!r} does not match {exp.get('institution_variants')}")

            d_val = norm(card.get("degree", ""))
            d_expected = norm(exp.get("degree_type", ""))
            d_variants = [d_expected, norm(exp.get("degree_abbreviation", ""))] + [norm(v) for v in exp.get("degree_search_variants", []) if v]
            if not d_val or d_val in ("selectone", "select"):
                errors.append(f"Card {idx}: degree is unselected (expected {exp.get('degree_type')})")
            else:
                d_ok = any((v and v in d_val) or (d_val in v) for v in d_variants if v and v != "selectone")
                if "bachelor" in d_expected and ("master" in d_val or d_val == "ms"):
                    d_ok = False
                if "master" in d_expected and ("bachelor" in d_val or d_val in ("bs", "ba")):
                    d_ok = False
                if not d_ok:
                    errors.append(f"Card {idx}: degree {card.get('degree')!r} does not match {exp.get('degree_type')}")

            f_val = norm(card.get("fieldOfStudy", ""))
            f_variants = [norm(exp.get("major", "")), norm(exp.get("major_search_term", ""))] + [norm(v) for v in exp.get("major_variants", []) if v]
            if not f_val:
                errors.append(f"Card {idx}: fieldOfStudy is blank (expected {exp.get('major_search_term')})")
            elif not any((v and v in f_val) or (f_val in v) for v in f_variants if v):
                errors.append(f"Card {idx}: fieldOfStudy {card.get('fieldOfStudy')!r} does not match {exp.get('major_search_term')}")

    elif section_type == "work":
        for idx, (card, exp) in enumerate(zip(cards, paired_expected)):
            c_val = norm(card.get("company", ""))
            c_variants = [norm(exp.get("company", ""))] + [norm(v) for v in exp.get("company_variants", []) if v]
            c_ok = any(v in c_val or c_val in v for v in c_variants if v)
            if not c_ok:
                errors.append(f"Card {idx}: company {card.get('company')!r} does not match {exp.get('company')}")

            t_val = norm(card.get("title", ""))
            t_expected = norm(exp.get("role", ""))
            t_ok = t_expected in t_val or t_val in t_expected
            if not t_ok and "intern" in t_expected and "intern" in t_val:
                t_ok = True
            if not t_ok:
                errors.append(f"Card {idx}: title {card.get('title')!r} does not match {exp.get('role')}")

    elif section_type == "lang":
        for idx, (card, exp) in enumerate(zip(cards, paired_expected)):
            l_val = norm(card.get("language", ""))
            l_expected = norm(exp.get("language", ""))
            l_ok = l_expected in l_val or l_val in l_expected
            if not l_ok:
                errors.append(f"Card {idx}: language {card.get('language')!r} does not match {exp.get('language')}")

    return len(errors) == 0, cards, errors


async def handle_my_experience(page: Page):
    print("\n[PAGE] My Experience")

    # Resume and Transcript upload
    await handle_document_uploads(page)

    # Clean up any leftover duplicate cards from prior failed attempts
    await delete_extra_cards(page, "lang", len(LANG))
    await delete_extra_cards(page, "edu", len(EDU))
    await delete_extra_cards(page, "work", len(WE))

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
        "expérience": ("work", WE), "emplois": ("work", WE),
        "education": ("edu", EDU), "school": ("edu", EDU), "degree": ("edu", EDU),
        "formation": ("edu", EDU), "études": ("edu", EDU),
        "language": ("lang", LANG), "langue": ("lang", LANG), "langues": ("lang", LANG),
    }

    for btn_info in add_btns_info:
        label_l = btn_info["label"].lower()

        if "website" in label_l:
            print(f"  [ADD] Skipping '{btn_info['label']}' (websites not required)")
            continue

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

        print(f"\n  [SECTION] Reconciling and filling '{btn_info['label']}' ({len(data_list)} expected entries)...")

        # 1. Clean up extra cards if any
        await delete_extra_cards(page, section_type, len(data_list))

        # 2. Check existing card count
        all_fields = await page.evaluate(SCAN_JS, None)
        anchors = [i for i, f in enumerate(all_fields) if is_section_anchor(f, section_type)]
        cur_card_count = len(anchors)

        # 3. Add missing cards until cur_card_count == len(data_list)
        add_attempts = 0
        while cur_card_count < len(data_list) and add_attempts < len(data_list) + 3:
            add_attempts += 1
            print(f"  [ADD] Adding card {cur_card_count + 1}/{len(data_list)} for '{btn_info['label']}'...")
            clicked_add = await page.evaluate("""(kws) => {
                const pageTitle = (document.querySelector('h1, h2, [data-automation-id="progressBarActiveStep"]')?.innerText || '').trim().toLowerCase();
                const headings = Array.from(document.querySelectorAll('h3, h4, [data-automation-id="sectionTitle"], [data-automation-id="groupTitle"]'));
                const h = headings.find(el => {
                    const t = el.innerText.trim().toLowerCase();
                    return t && t !== pageTitle && kws.some(k => t.includes(k));
                });
                if (!h) return false;
                let sec = h.parentElement;
                while (sec && sec !== document.body) {
                    const btn = sec.querySelector('[data-automation-id="add-button"]');
                    if (btn) {
                        btn.scrollIntoView({block: 'center'});
                        btn.click();
                        return true;
                    }
                    sec = sec.parentElement;
                }
                return false;
            }""", ["education", "school", "formation", "études", "degree"] if section_type == "edu" else (["work experience", "employment", "job history", "work history", "professional experience", "expérience professionnelle", "emplois"] if section_type == "work" else ["language", "langue", "langues"]))

            if not clicked_add:
                curr_btn = page.locator(f"[data-automation-id='add-button']").nth(btn_info["index"])
                try:
                    await curr_btn.scroll_into_view_if_needed(timeout=3000)
                    await curr_btn.click(timeout=3000)
                except Exception:
                    await page.evaluate("""(idx) => {
                        const btns = Array.from(document.querySelectorAll('[data-automation-id="add-button"]'));
                        if (btns[idx]) { btns[idx].scrollIntoView({block:'center'}); btns[idx].click(); }
                    }""", btn_info["index"])
            await page.wait_for_timeout(MEDIUM_MS)
            all_fields = await page.evaluate(SCAN_JS, None)
            anchors = [i for i, f in enumerate(all_fields) if is_section_anchor(f, section_type)]
            new_card_count = len(anchors)
            if new_card_count == cur_card_count and add_attempts > 2:
                print(f"  [ADD] Card count did not increase ({cur_card_count}) — stopping add attempts for '{btn_info['label']}'")
                break
            cur_card_count = new_card_count

        # 4. Pair existing cards with data_list entries to preserve valid assignments
        all_fields = await page.evaluate(SCAN_JS, None)
        anchor_fields = [f for f in all_fields if is_section_anchor(f, section_type)]
        paired_entries = pair_cards_with_entries(anchor_fields, data_list, section_type)

        # Fill and reconcile each card
        for ent_idx, entry in enumerate(paired_entries):
            if page.is_closed():
                print(f"  [ADD] Page closed — stopping")
                return False

            print(f"  [RECONCILE] Card {ent_idx+1}/{len(paired_entries)} of '{btn_info['label']}'...")
            await fill_add_dialog(page, btn_info["label"], entry=entry, section_type=section_type, entry_index=ent_idx)
            await page.wait_for_timeout(1000)

        # 5. Clean up any extra cards that might have been created
        await delete_extra_cards(page, section_type, len(data_list))

        # 6. Audit section correctness
        is_ok, cards_found, errs = await audit_my_experience_section(page, section_type, data_list)
        if not is_ok:
            print(f"  [AUDIT] Discrepancies in '{btn_info['label']}': {errs}")
            for err in errs:
                m = re.search(r'Card (\d+):', err)
                if m:
                    bad_card_idx = int(m.group(1))
                    if bad_card_idx < len(paired_entries):
                        print(f"  [RECONCILE] Retrying targeted fill for Card {bad_card_idx+1}...")
                        await fill_add_dialog(page, btn_info["label"], entry=paired_entries[bad_card_idx],
                                              section_type=section_type, entry_index=bad_card_idx)
                        await page.wait_for_timeout(1000)

    # ── Standalone Skills Field ──────────────────────────────────────────────
    skills_data = []
    try:
        _ps = json.loads(PROFILE_SUMMARY)
        skills_data = _ps.get("job_target_skills") or []
    except Exception:
        skills_data = []

    if not skills_data:
        # Fallback to top relevant skills from library rather than all 50
        skills_data = LIBRARY.get("skills", [])[:10]

    if skills_data:
        all_flds = await page.evaluate(SCAN_JS, None)
        skill_field = next((f for f in all_flds if label_match(f.get("label","").lower(),
                                                               "skills","competencies","qualifications","type to add skills")), None)
        if skill_field and skill_field.get("isSelectInput"):
            print(f"\n  [SKILLS] Found skills selectinput [{skill_field['index']}] {skill_field['label']!r} ({len(skills_data)} target skills)")
            await exec_skills_field(page, skill_field, skills_data)
            await page.wait_for_timeout(1000)

    # ── Full Audit Report before Save ────────────────────────────────────────
    print("\n" + "=" * 60)
    print("[AUDIT] Pre-Save My Experience Correctness Audit:")
    edu_ok, edu_cards, edu_errs = await audit_my_experience_section(page, "edu", EDU)
    edu_status = "✓ PASSED (not present on form)" if edu_ok and not edu_cards else ("✓ PASSED" if edu_ok else "✗ FAILED")
    print(f"  Education ({len(edu_cards)}/{len(EDU)} cards): {edu_status}")
    for c in edu_cards:
        print(f"    Card {c.get('cardIndex', 0)+1}: School={c.get('school')!r} | Degree={c.get('degree')!r} | FoS={c.get('fieldOfStudy')!r}")
    if edu_errs:
        for e in edu_errs: print(f"    ⚠ {e}")

    work_ok, work_cards, work_errs = await audit_my_experience_section(page, "work", WE)
    work_status = "✓ PASSED (not present on form)" if work_ok and not work_cards else ("✓ PASSED" if work_ok else "✗ FAILED")
    print(f"  Work Experience ({len(work_cards)}/{len(WE)} cards): {work_status}")
    for c in work_cards:
        print(f"    Card {c.get('cardIndex', 0)+1}: Company={c.get('company')!r} | Title={c.get('title')!r} | Current={c.get('currentlyWorkHere')}")
    if work_errs:
        for e in work_errs: print(f"    ⚠ {e}")

    lang_ok, lang_cards, lang_errs = await audit_my_experience_section(page, "lang", LANG)
    lang_status = "✓ PASSED (not present on form)" if lang_ok and not lang_cards else ("✓ PASSED" if lang_ok else "✗ FAILED")
    print(f"  Languages ({len(lang_cards)}/{len(LANG)} cards): {lang_status}")
    for c in lang_cards:
        print(f"    Card {c.get('cardIndex', 0)+1}: Language={c.get('language')!r} | Proficiency={c.get('proficiency')!r}")
    if lang_errs:
        for e in lang_errs: print(f"    ⚠ {e}")
    print("=" * 60 + "\n")

    # ── Audit Enforcement Gate: actively remediate any discrepancies before saving ──
    for audit_retry in range(3):
        if edu_ok and work_ok and lang_ok:
            break
        print(f"  [AUDIT-GATE] Correctness audit failed — actively remediating discrepancies (attempt {audit_retry+1}/3)...")
        if not edu_ok:
            for err in edu_errs:
                m = re.search(r'Card (\d+):', err)
                if m:
                    bad_idx = int(m.group(1))
                    if bad_idx < len(EDU):
                        print(f"  [REMEDIATE] Re-filling Education Card {bad_idx+1} ({EDU[bad_idx].get('institution_variants',[None])[0]})...")
                        await fill_add_dialog(page, "Highest Level of Education", entry=EDU[bad_idx],
                                              section_type="edu", entry_index=bad_idx)
                        await page.wait_for_timeout(1000)
        if not work_ok:
            for err in work_errs:
                m = re.search(r'Card (\d+):', err)
                if m:
                    bad_idx = int(m.group(1))
                    if bad_idx < len(WE):
                        print(f"  [REMEDIATE] Re-filling Work Experience Card {bad_idx+1} ({WE[bad_idx].get('company')})...")
                        await fill_add_dialog(page, "Work Experience", entry=WE[bad_idx],
                                              section_type="work", entry_index=bad_idx)
                        await page.wait_for_timeout(1000)
        if not lang_ok:
            for err in lang_errs:
                m = re.search(r'Card (\d+):', err)
                if m:
                    bad_idx = int(m.group(1))
                    if bad_idx < len(LANG):
                        print(f"  [REMEDIATE] Re-filling Language Card {bad_idx+1} ({LANG[bad_idx].get('language')})...")
                        await fill_add_dialog(page, "Languages", entry=LANG[bad_idx],
                                              section_type="lang", entry_index=bad_idx)
                        await page.wait_for_timeout(1000)

        # Re-audit after remediation pass
        edu_ok, edu_cards, edu_errs = await audit_my_experience_section(page, "edu", EDU)
        r_edu = "✓ PASSED (not present)" if edu_ok and not edu_cards else ("✓ PASSED" if edu_ok else "✗ FAILED")
        r_work = "✓ PASSED (not present)" if work_ok and not work_cards else ("✓ PASSED" if work_ok else "✗ FAILED")
        r_lang = "✓ PASSED (not present)" if lang_ok and not lang_cards else ("✓ PASSED" if lang_ok else "✗ FAILED")
        print(f"  [REMEDIATE] Re-audit: Edu={r_edu} | Work={r_work} | Lang={r_lang}")

    if not edu_ok or not work_ok or not lang_ok:
        print(f"  [FATAL] My Experience correctness audit FAILED — refusing to advance to prevent saving corrupted data!")
        return False

    # Save and continue with retry loop
    saved = False
    for attempt in range(3):
        saved = await save_and_continue(page)
        if saved:
            break
        print(f"  [NAV] My Experience save attempt {attempt+1}/3 failed — retrying validation fixes...")
        await page.wait_for_timeout(2000)

        # Clean up any rogue extra cards causing validation errors
        await delete_extra_cards(page, "edu", len(EDU))
        await delete_extra_cards(page, "work", len(WE))
        await delete_extra_cards(page, "lang", len(LANG))

        # Check validation errors on page
        val_errors = await read_validation_errors(page)
        print(f"  [RETRY] Active validation errors: {val_errors}")

        if any("language" in err.lower() or "duplicate" in err.lower() for err in val_errors):
            print(f"  [RETRY] Language validation error detected: {val_errors} — re-filling language cards...")
            all_fields = await page.evaluate(SCAN_JS, None)
            anchor_fields = [f for f in all_fields if is_section_anchor(f, "lang")]
            paired_langs = pair_cards_with_entries(anchor_fields, LANG, "lang")
            for ent_idx, entry in enumerate(paired_langs):
                await fill_add_dialog(page, "Languages", entry=entry, section_type="lang", entry_index=ent_idx)
                await page.wait_for_timeout(500)

        retry_fields = await page.evaluate(SCAN_JS, None)

        # Empty Field of Study / School comboboxes
        all_comboboxes = [rf for rf in retry_fields if rf.get("isSelectInput") and rf.get("label","")]
        edu_anchors = [rf for rf in retry_fields if any(k in rf.get("label","").lower() for k in ("school or university", "school", "university"))]
        for cb_f in all_comboboxes:
            lbl_l = cb_f.get("label","").lower()
            current_val = cb_f.get("value", "").strip()
            if current_val:
                continue

            # Determine card index by position relative to edu anchors
            cb_idx = cb_f.get("index", 0)
            card_idx = 0
            for i, a in enumerate(edu_anchors):
                if cb_idx >= a.get("index", 0):
                    card_idx = i

            if card_idx >= len(EDU):
                continue  # Ignore extra cards

            ent = EDU[card_idx]
            if label_match(lbl_l, "school","university","institution","college"):
                s_val = ent.get("institution_variants", [""])[0]
                if s_val:
                    print(f"  [RETRY] Card {card_idx+1}: Re-filling School combobox = {s_val!r}")
                    await exec_selectinput(page, cb_f, s_val)
                    await page.keyboard.press("Tab")
                    await page.wait_for_timeout(500)
            elif label_match(lbl_l, "major","field of study","discipline"):
                terms = [ent.get("major_search_term", "")] + ent.get("major_variants", [])
                terms = list(dict.fromkeys(t for t in terms if t))
                s_val = "\n".join(terms)
                if s_val:
                    print(f"  [RETRY] Card {card_idx+1}: Re-filling FoS combobox = {s_val!r}")
                    await exec_selectinput(page, cb_f, s_val)
                    await page.keyboard.press("Tab")
                    await page.mouse.click(50, 50)
                    await page.wait_for_timeout(500)

        # Empty text inputs (e.g. School as plain text input)
        all_text_inputs = [rf for rf in retry_fields if rf.get("tag") in ("input", "textarea") and not rf.get("isSelectInput") and rf.get("label","")]
        for ti_f in all_text_inputs:
            lbl_l = ti_f.get("label","").lower()
            current_val = ti_f.get("value", "").strip()
            if current_val:
                continue
            ti_idx = ti_f.get("index", 0)
            card_idx = 0
            for i, a in enumerate(edu_anchors):
                if ti_idx >= a.get("index", 0):
                    card_idx = i
            if card_idx < len(EDU) and label_match(lbl_l, "school","university","institution","college"):
                ent = EDU[card_idx]
                s_val = ent.get("institution_variants", [""])[0] or ent.get("school", "")
                if s_val:
                    print(f"  [RETRY] Card {card_idx+1}: Re-filling School text input = {s_val!r}")
                    await exec_text(page, ti_f, s_val)
                    await page.wait_for_timeout(500)

        # Empty degree dropdowns
        empty_drops = [rf for rf in retry_fields
                       if rf.get("tag") == "button"
                       and ("select one" in rf.get("value","").lower() or not rf.get("value","").strip())
                       and rf.get("label","")]
        degree_drops = [d for d in empty_drops
                        if label_match(d.get("label","").lower(), "degree","degree type","level of education")]
        for drop_i, drop_f in enumerate(degree_drops):
            if drop_i >= len(EDU):
                continue
            ent = EDU[drop_i]
            terms = [ent.get("degree_type", ""), ent.get("degree_abbreviation", "")] + ent.get("degree_search_variants", [])
            terms = list(dict.fromkeys(t for t in terms if t))
            search_val = "\n".join(terms)
            if search_val:
                print(f"  [RETRY] Card {drop_i+1}: Re-filling Degree dropdown = {search_val!r}")
                await exec_button_dropdown(page, drop_f, search_val)
                await page.wait_for_timeout(500)

        await page.wait_for_timeout(1000)

    if not saved:
        print(f"  [ERR] handle_my_experience: save failed after 3 attempts — stopping")
    return saved


async def handle_voluntary_disclosures(page: Page) -> bool:
    await smart_fill_page(page, "Voluntary Disclosures")

    await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
    await page.wait_for_timeout(700)
    await smart_fill_page(page, "Voluntary Disclosures")
    await page.evaluate("() => window.scrollTo(0, 0)")
    await page.wait_for_timeout(400)

    tc_id = "termsAndConditions--acceptTermsAndAgreements"
    tc_checked = await page.evaluate(f"""() => {{
        const el = document.getElementById('{tc_id}');
        return el ? (el.checked || el.getAttribute('aria-checked') === 'true') : null;
    }}""")

    if tc_checked is None:
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


async def handle_self_identify(page: Page) -> bool:
    print("\n[PAGE] Self Identify")
    today = datetime.datetime.today()

    name_el = page.locator("#selfIdentifiedDisabilityData--name").first
    if await name_el.count():
        await page.evaluate("el => el.scrollIntoView({block:'center'})", await name_el.element_handle())
        await page.wait_for_timeout(300)
        await name_el.fill(f"{PI['first_name']} {PI['last_name']}")
        print(f"    ✓ name = '{PI['first_name']} {PI['last_name']}'")

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
            await page.keyboard.press("Tab")
            await page.wait_for_timeout(200)
            print(f"    ✓ date {sfx} = {val!r}")

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
