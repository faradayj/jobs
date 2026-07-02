"""
app_common.py  —  Shared Application Bot Infrastructure
========================================================
Pure-Python helpers shared between app_workday_v3.py and app_greenhouse.py.

Contains:
  - Library loading + profile constants
  - PROFILE_SUMMARY / SYSTEM_PROMPT for DeepSeek
  - deepseek_fill_page()
  - label_match() / fuzzy_pick() / pick_decline()
  - rule_based_answer()
  - MONTH_NUM
  - launch_browser() / write_json_report() / scrape_salary()

No Playwright imports here — executors live in each applicator script
because DOM strategies differ between ATSes.
"""

import json, os, re, sys
from pathlib import Path
from dotenv import load_dotenv

# ── Platform UTF-8 fix ────────────────────────────────────────────────────────
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True)
else:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)

REPO_ROOT     = Path(__file__).resolve().parent.parent   # src/ → repo root
DATA_DIR      = REPO_ROOT / "data"
ARTIFACTS_DIR = REPO_ROOT / "artifacts"
load_dotenv(DATA_DIR / ".env")

# ── Chrome path ───────────────────────────────────────────────────────────────
def _find_chrome() -> str:
    system = _plat.system()
    candidates = []
    if system == "Darwin":
        candidates = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    elif system == "Windows":
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        ]
    else:
        candidates = ["/usr/bin/google-chrome", "/usr/bin/chromium-browser",
                      "/usr/bin/chromium", "/snap/bin/chromium"]
    for p in candidates:
        if os.path.exists(p):
            return p
    return ""

# ── Library loading ───────────────────────────────────────────────────────────
LIBRARY = json.loads((DATA_DIR / "library.json").read_text(encoding="utf-8"))
PI      = LIBRARY["personal_info"]
WE      = LIBRARY.get("work_experience", [])
EDU     = LIBRARY.get("education_history", [])
LANG    = LIBRARY.get("languages", [])

# Résumé: resolve to absolute path for set_input_files
_resume_rel = LIBRARY.get("resume_path", "")
if _resume_rel:
    _rp = Path(_resume_rel) if Path(_resume_rel).is_absolute() else REPO_ROOT / _resume_rel
    RESUME_PATH = str(_rp.resolve())
else:
    _pdfs = list(DATA_DIR.glob("*.pdf"))
    RESUME_PATH = str(_pdfs[0].resolve()) if _pdfs else ""

# User-agent matching platform so pages render correctly
import platform as _plat
_ua_os = {
    "Darwin":  "Macintosh; Intel Mac OS X 10_15_7",
    "Windows": "Windows NT 10.0; Win64; x64",
    "Linux":   "X11; Linux x86_64",
}.get(_plat.system(), "Windows NT 10.0; Win64; x64")
USER_AGENT = (f"Mozilla/5.0 ({_ua_os}) AppleWebKit/537.36 "
              f"(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

CHROME_PATH  = _find_chrome()
EMAIL        = PI["email"]
PASSWORD     = os.environ.get("WORKDAY_PASSWORD", PI.get("password", ""))
DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY", "")

COMP = LIBRARY.get("compensation_rules", {})
REG  = LIBRARY.get("regulatory_self_identification", {})
JBM  = LIBRARY.get("job_board_mappings", {})
PHONE_DIGITS = "".join(c for c in PI.get("phone", "") if c.isdigit())
_sponsor_raw  = JBM.get("requires_visa_sponsorship", "No")
NEEDS_SPONSOR = str(_sponsor_raw).lower() not in ("no", "false", "0", "")
DISABILITY_ANSWER = str(REG.get("disability_answer", "yes")).lower()

MONTH_NUM = {
    "january":"01","february":"02","march":"03","april":"04","may":"05","june":"06",
    "july":"07","august":"08","september":"09","october":"10","november":"11","december":"12",
}

# ── Profile summary (runtime fields filled by each applicator) ────────────────
PROFILE_SUMMARY = json.dumps({
    "personal_info":            {k: v for k, v in PI.items() if k not in ("password",)},
    "work_experience":          WE,
    "education_history":        EDU,
    "skills":                   LIBRARY.get("skills", []),
    "languages":                LANG,
    "role_preferences":         LIBRARY.get("role_preferences", {}),
    "compensation_rules":       COMP,
    "job_listing_salary":       None,   # filled at runtime
    "job_listing_locations":    [],     # filled at runtime
    "regulatory_self_identification": REG,
    "job_board_mappings":       JBM,
    "routing_priorities":       LIBRARY.get("routing_priorities", {}),
    "workday_qa_examples":      LIBRARY.get("workday_qa_examples", []),
    "today":                    None,   # filled at runtime
}, indent=2)

SYSTEM_PROMPT = """You are an AI filling out a job application form for a candidate.
Given a list of form fields (with labels, types, and available options), return the best answer for EVERY fillable field.

Rules:
- Use candidate profile to answer accurately and honestly.
- "First Name" / "Last Name" / "Address" / "City" / "State" / "Zip" / "Phone" → use personal_info.
- "How did you hear" → "LinkedIn" or closest available option.
- Visa/sponsorship → use job_board_mappings.requires_visa_sponsorship.
- Salary/compensation → use job_listing_salary if set (pick the range option closest to it); otherwise use compensation_rules.baseline_target_pay.
- EEO fields (gender, sex, race, ethnicity, hispanic/latino, veteran) → check regulatory_self_identification.primary_demographic_action:
    * If "Decline To Self Identify" → FIRST look for an explicit "prefer not to answer"/"decline"/"I do not want to answer"/"I do not wish to disclose" option and pick it.
      If no such option exists AND the field label says "leave blank" → pick "Select One" (leave blank).
      If no such option exists AND the field appears required (no blank/decline available) → use fallback_gender / fallback_race / fallback_hispanic_ethnicity from the profile.
    * If a specific identity is given → use fallback_gender / fallback_race / fallback_hispanic_ethnicity from the profile.
  The label will often say "Leave blank if you do not wish to declare" — this means the field is optional; picking "Select One" is valid.
- Disability → use regulatory_self_identification.disability_answer.
- Veteran status → use regulatory_self_identification.veteran_status_selection.
- Age 18+ / authorized to work → "Yes".
- Non-compete / prior employment at this company → "No" unless profile says otherwise.
- Security clearance: holds_security_clearance="No". NEVER select any option indicating a current clearance.
- Open-ended text → concise honest answer from profile.
- Skills fields → use the skills array; pick the closest matching option from available choices.
- Language fields → use the languages array for language name and proficiency level.
- If filling a specific Work Experience / Education / Language entry, an "Entry Context" block
  will be provided — use ONLY that entry's data for fields in that dialog, not other entries.
- For selectinput/button-dropdown fields, your value must EXACTLY match one of the provided options.
- Skip nav buttons, already-filled fields, fields with no relevant data.
- Citizenship / work-authorization / export-control fields: apply routing_priorities.contextual_citizenship_and_export_rules.
  Determine if the job is US-based or Canada-based from job_listing_locations; default US.
  Select the citizenship/export country accordingly (USA for US roles, Canada for Canadian roles).
- Location / office / preferred-location dropdowns (single-select):
  Step 1 — if the dropdown contains "Remote", "Remote - US", "Virtual", or "Work from Home",
    pick the remote option UNLESS the job is Canada-based (then prefer Vancouver/Toronto remote).
  Step 2 — intersect dropdown options with job_listing_locations. If any option fuzzy-matches
    a listing location, pick the highest-priority one per the country ladder:
    US jobs → us_location_priority_ladder; Canada jobs → canada_location_priority_ladder.
  Step 3 — if no listing location matched, apply the country ladder directly.
    Pick the highest-ranked entry whose keywords appear in any dropdown option.
- Location checkboxes (multi-select, "select all that apply"): select ALL available options.
- Start/available date fields: use today's date from the "today" field plus the offset in
  role_preferences.anticipated_start_date_preference to compute the target date.
- Use workday_qa_examples as few-shot guidance for common application questions (authorization,
  sponsorship, termination history, background check consent, etc.).

Respond ONLY with valid JSON: {"answers": [{"index": <int>, "value": "<string>"}]}
Only include fields you have an answer for."""


async def deepseek_fill_page(fields: list[dict],
                              entry: dict = None,
                              section_type: str = "",
                              profile_override: str = None) -> list[dict]:
    """Send page/dialog fields to DeepSeek. Returns [{index, value}] or [] on error/no key.

    profile_override: pass a runtime-updated PROFILE_SUMMARY string with job_listing_salary,
    job_listing_locations, and today already injected.
    """
    if not DEEPSEEK_KEY:
        return []
    profile = profile_override or PROFILE_SUMMARY
    field_lines = []
    for f in fields:
        line = f"[{f['index']}] type={f['type']} label={f['label']!r}"
        if f.get("date_seq"):  line += f" date_seq={f['date_seq']}"
        if f.get("options"):   line += f" options={f['options']}"
        if f.get("value"):     line += f" current={f['value']!r}"
        if f.get("section"):   line += f" section={f['section']!r}"
        field_lines.append(line)

    entry_context = ""
    if entry and section_type:
        entry_context = (f"\nEntry Context (you are filling ONE {section_type} entry — "
                         f"use ONLY this data for these fields):\n"
                         f"{json.dumps(entry, indent=2)}\n")

    prompt = (f"Candidate Profile:\n{profile}\n"
              f"{entry_context}"
              f"\nForm Fields (section: {fields[0].get('section', '') if fields else ''}):\n"
              + "\n".join(field_lines))
    try:
        import httpx
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post("https://api.deepseek.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {DEEPSEEK_KEY}"},
                json={"model": "deepseek-chat", "temperature": 0.1, "max_tokens": 1000,
                      "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                                   {"role": "user",   "content": prompt}]})
        raw = r.json()["choices"][0]["message"]["content"].strip()
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if m:
            return json.loads(m.group()).get("answers", [])
    except Exception as e:
        print(f"  [LLM] DeepSeek error: {e}")
    return []


# ── Label / option helpers ────────────────────────────────────────────────────

def label_match(label: str, *keywords) -> bool:
    l = label.lower()
    return any(k in l for k in keywords)

DECLINE_KEYWORDS = ["not wish", "don't wish", "prefer not", "decline", "choose not",
                    "not wish to self", "no wish", "i do not wish"]

def pick_decline(opts: list[str]) -> str | None:
    for o in opts:
        if any(k in o.lower() for k in DECLINE_KEYWORDS):
            return o
    return None

def fuzzy_pick(opts: list[str], value: str) -> str | None:
    """Fuzzy match value against options: exact → starts → contains (normalises apostrophes)."""
    def _norm(s: str) -> str:
        return re.sub(r"[''\-]", "", s.lower().strip())
    vl = value.lower().strip()
    vn = _norm(value)
    for strategy in [
        lambda o: o.lower() == vl,
        lambda o: o.lower().startswith(vl) or vl.startswith(o.lower()),
        lambda o: vl in o.lower() or o.lower() in vl,
        lambda o: _norm(o) == vn,
        lambda o: _norm(o).startswith(vn) or vn.startswith(_norm(o)),
        lambda o: vn in _norm(o) or _norm(o) in vn,
    ]:
        m = next((o for o in opts if strategy(o)), None)
        if m: return m
    return None


def rule_based_answer(field: dict, context_hint: str = "") -> str | None:
    """Return the best library.json-grounded answer for a field, or None if no match."""
    label   = field.get("label", "")
    opts    = field.get("options", [])
    section = (field.get("section") or field.get("page_heading") or context_hint).lower()
    tag     = field.get("tag", "")
    ftype   = field.get("type", "")
    current = field.get("value", "")

    # Skip already-filled fields (except unchecked checkboxes)
    if current and current.lower() not in ("select one", "", "false", "unchecked") \
            and "select one" not in current.lower():
        return None

    # ── Radio ────────────────────────────────────────────────────────────────
    if field.get("role") == "radio" or ftype == "radio":
        if not opts:
            opts = ["Yes", "No"]
        if label_match(label, "previously worked", "prior employ", "work for us before",
                       "worked here", "worked for", "former employee", "previous worker",
                       "robert half", "protiviti"):
            return fuzzy_pick(opts, "No") or opts[-1]
        if label_match(label, "sponsor", "visa", "work authoriz"):
            want = "No" if not NEEDS_SPONSOR else "Yes"
            return fuzzy_pick(opts, want) or opts[0]
        if label_match(label, "18", "legal age", "authorized", "eligible"):
            return fuzzy_pick(opts, "Yes") or opts[0]
        if label_match(label, "non-compete", "non compete", "agreement", "restrictive"):
            return fuzzy_pick(opts, "No") or opts[0]
        if label_match(label, "relocat"):
            return fuzzy_pick(opts, "Yes") or opts[0]
        if label_match(label, "certify", "verify", "accurate", "acknowledge", "confirm",
                       "agree", "consent", "terms", "true and correct"):
            return fuzzy_pick(opts, "Yes") or fuzzy_pick(opts, "I agree") or opts[0]
        return fuzzy_pick(opts, "No") or opts[0]

    # ── Checkbox ─────────────────────────────────────────────────────────────
    if ftype == "checkbox" or field.get("role") == "checkbox":
        if label_match(label, "consent", "agree", "terms", "condition", "certify",
                       "verify", "accurate", "correct", "true and correct", "acknowledge"):
            return "true"
        if label_match(label, "preferred name"):
            return "false"
        if label_match(label, "none of the above"):
            return "true"
        if label_match(label, "procuring contracting officer", "source selection",
                       "program manager", "administrative contracting",
                       "award a contract", "establish overhead", "approve issuance",
                       "pay or settle a claim", "senior employee", "political appointee",
                       "public financial disclosure", "covered dod official",
                       "military officer", "official involved with contracts",
                       "otherwise involved with snc", "otherwise involved"):
            return "false"
        _PREFER_NOT_KWS = ("don't wish", "don't want to answer", "prefer not",
                           "decline to", "do not want to answer", "i do not want to answer")
        prefer_not = DISABILITY_ANSWER in ("prefer_not", "no_answer", "decline")
        # Standalone prefer-not checkbox label (no "disability" word required)
        if prefer_not and label_match(label, *_PREFER_NOT_KWS):
            return "true"
        if label_match(label, "disability"):
            ll = label.lower()
            if DISABILITY_ANSWER == "yes":
                if "yes" in ll and "disability" in ll:
                    return "true"
                return "false"
            else:
                # STEP 1: extended prefer-not keywords (merged from Workday)
                if prefer_not and any(k in ll for k in _PREFER_NOT_KWS):
                    return "true"
                if ll.startswith("no") and "disability" in ll:
                    return "true"
                return "false"
        return None

    # ── Text / textarea / selectinput ────────────────────────────────────────
    if tag in ("input", "textarea") or ftype in ("text", "email", "tel", "number"):

        if field.get("isSelectInput"):
            if label_match(label, "how did you hear", "source", "referral", "learn about"):
                hear_terms = JBM.get("hear_about_us_fallback_order",
                    ["LinkedIn", "Internet/Online Job Posting", "Job Board", "Other"])
                return "\n".join(hear_terms)
            if label_match(label, "country") and not label_match(label, "phone"):
                return "United States of America"
            if label_match(label, "state", "province"):
                if "united" not in label.lower() and len(label) < 50:
                    return PI["state"]
            return None

        if label_match(label, "first name", "given name"):       return PI["first_name"]
        if label_match(label, "last name", "surname", "family name"): return PI["last_name"]
        if label_match(label, "middle name", "middle initial"):  return ""
        if label_match(label, "email"):                          return PI["email"]
        if label_match(label, "phone number", "telephone", "cell number", "mobile number"):
            if label_match(label, "extension", "ext"):           return ""
            return PHONE_DIGITS
        if label_match(label, "address line 1", "street address", "address 1"):
            return PI["address"]
        if label_match(label, "address line 2", "apt", "suite", "unit"): return ""
        if label_match(label, "school location", "institution location", "school city"):
            school = next((e for e in EDU if "attending" in e.get("current_status","").lower()),
                          EDU[0] if EDU else None)
            return school.get("institution_city", "") if school else ""
        if label_match(label, "city", "town"):   return PI["city"]
        if label_match(label, "zip", "postal"):  return PI["zip_code"]
        if label_match(label, "linkedin"):       return PI.get("linkedin", "")
        if label_match(label, "github", "portfolio", "website", "url"):
            return PI.get("github", "")
        if label_match(label, "salary", "compensation", "pay", "wage", "expected"):
            return str(COMP.get("baseline_target_pay", COMP.get("expected_base_salary", "120000")))

        if label_match(label, "name") and "employee" not in label.lower():
            return f"{PI['first_name']} {PI['last_name']}"

        # STEP 4: Preferred geographic/location text field (from Workday line 489-491)
        if label_match(label, "preferred geographic", "preferred location", "geographic preference",
                       "location preference"):
            return PI.get("city", "San Diego") + ", CA"

        if label_match(label, "month", "day", "year") and (
            "application" in section or
            not any(k in section for k in ("work","experience","education","school","self identify","signature"))
        ):
            import datetime
            avail = datetime.date.today() + datetime.timedelta(weeks=2)
            if label_match(label, "month"): return str(avail.month).zfill(2)
            if label_match(label, "day"):   return str(avail.day).zfill(2)
            if label_match(label, "year"):  return str(avail.year)

        if label_match(label, "graduation", "anticipated graduation", "expected graduation") and \
                label_match(label, "month", "day", "year"):
            school = next((e for e in EDU if "attending" in e.get("current_status","").lower()), None)
            if school:
                if label_match(label, "month"): return MONTH_NUM.get(school.get("end_month","").lower(), "")
                if label_match(label, "day"):   return "01"
                if label_match(label, "year"):  return str(school.get("end_year",""))

        if "work" in section or "experience" in section or "employment" in section:
            if label_match(label, "company", "employer", "organization"):
                return WE[0]["company"] if WE else ""
            if label_match(label, "title", "position", "role"):
                return WE[0]["role"] if WE else ""
            if label_match(label, "city", "location"):
                return WE[0].get("city", "") if WE else ""
            if label_match(label, "description", "responsibilities", "summary"):
                return (WE[0].get("description","")[:500]) if WE else ""
            # STEP 2: WE start/end year text fields (from Workday lines 517-520)
            if label_match(label, "start year"):
                return WE[0].get("start_year","") if WE else ""
            if label_match(label, "end year"):
                return WE[0].get("end_year","") if WE else ""

        if "education" in section or "school" in section or "degree" in section:
            if label_match(label, "school", "institution", "university", "college"):
                return EDU[0]["institution_variants"][0] if EDU else ""
            if label_match(label, "major", "field of study", "discipline"):
                return EDU[0]["major_variants"][0] if EDU else ""
            if label_match(label, "gpa", "grade"):
                return str(EDU[0].get("gpa","")) if EDU else ""
            # STEP 3: EDU start/end/graduation year text fields (from Workday lines 530-533)
            if label_match(label, "start year"):
                return EDU[0].get("start_year","") if EDU else ""
            if label_match(label, "end year", "graduation year"):
                return EDU[0].get("end_year","") if EDU else ""

        return None

    # ── Button dropdown / native select ──────────────────────────────────────
    if tag in ("button", "select") or ftype in ("select-one", "select"):
        if not opts:
            return None

        if label_match(label, "export control", "arms export", "itar", "u.s. persons",
                       "citizenship status", "controlled information"):
            return fuzzy_pick(opts, "U.S. Citizen") or next(
                (o for o in opts if "citizen" in o.lower() and "select" not in o.lower()), None)

        if label_match(label, "18 years of age", "18 years old", "at least 18"):
            return fuzzy_pick(opts, "Yes") or opts[0]

        if label_match(label, "currently employed", "present employer", "may we speak to"):
            for kw in ("do not contact", "don't contact", "cannot contact"):
                m = next((o for o in opts if kw in o.lower()), None)
                if m: return m
            return fuzzy_pick(opts, "Currently employed") or fuzzy_pick(opts, "Not currently employed") or opts[0]

        if label_match(label, "friends", "professional colleagues", "acquaintances"):
            return fuzzy_pick(opts, "No") or opts[0]

        _opt_vals = [o.lower() for o in opts if o.lower() not in ("select one", "")]
        if label_match(label, "federal", "government employee", "conflict of interest",
                       "mandatory disqualification", "sierra nevada corporation requires") or \
                (opts and any("not a current" in o for o in _opt_vals) and
                 any("a current" in o or "a former" in o for o in _opt_vals)):
            return fuzzy_pick(opts, "Not a current or former") or \
                   next((o for o in opts if "not a current" in o.lower()), None) or opts[0]

        if (label_match(label, "following statements", "conflict of interest",
                        "government service", "procurement", "select all that apply") and
                label_match(label, "snc", "sierra nevada", "sierra space", "government")) or \
                (opts and any("none of the above" in o.lower() for o in opts) and
                 any("snc" in o.lower() or "sierra" in o.lower() for o in opts)):
            return fuzzy_pick(opts, "None of the above") or \
                   next((o for o in opts if "none" in o.lower()), None) or opts[0]

        if label_match(label, "gender", "sex", "race", "ethnicity", "hispanic",
                       "latino", "veteran", "disability"):
            decline = pick_decline(opts)
            if decline: return decline
            if label_match(label, "veteran"):
                for kw in ["not a protected veteran", "not a veteran", "i am not", "i do not wish"]:
                    m = next((o for o in opts if kw in o.lower()), None)
                    if m: return m
            if label_match(label, "gender", "sex"):
                fb = REG.get("fallback_gender", "Male")
                return fuzzy_pick(opts, fb) or next((o for o in opts if o.lower() not in ("select one","none")), None)
            if label_match(label, "hispanic", "latino"):
                fb = "No" if REG.get("fallback_hispanic_ethnicity","No") in ("No", False, "false") else "Yes"
                return fuzzy_pick(opts, fb)
            if label_match(label, "race", "ethnicity"):
                return fuzzy_pick(opts, REG.get("fallback_race","Asian"))
            return None

        if label_match(label, "how did you hear", "learn about") \
                or (label_match(label, "source", "referral") and len(label) < 80):
            hear_order = JBM.get("hear_about_us_fallback_order",
                ["LinkedIn", "Internet/Online Job Posting", "Job Board", "Other"])
            for term in hear_order:
                pick = fuzzy_pick(opts, term)
                if pick: return pick
            return opts[0]

        if label_match(label, "talent community", "future opportunities", "receive information",
                       "stay connected", "join our talent", "keep me informed"):
            consent = JBM.get("future_opportunities_consent", "Yes")
            return fuzzy_pick(opts, consent) or fuzzy_pick(opts, "Yes") or opts[0]

        if label_match(label, "unrestricted right to work",
                       "right to work in the country", "right to work in the u"):
            return fuzzy_pick(opts, "Yes") or opts[0]

        if label_match(label, "country") and not label_match(label, "phone") and len(label) < 60:
            return fuzzy_pick(opts, "United States") or fuzzy_pick(opts, "USA") or opts[0]

        if label_match(label, "state", "province", "region") and not label_match(label, "country") \
                and "united" not in label.lower() and len(label) < 50:
            return fuzzy_pick(opts, PI["state"]) or opts[0]

        if label_match(label, "phone", "device type", "phone type"):
            return fuzzy_pick(opts, "Mobile") or opts[0]

        if label_match(label, "suffix", "salutation", "prefix"):
            return ""

        if label_match(label, "sponsor", "visa", "work authorization"):
            want = "No" if not NEEDS_SPONSOR else "Yes"
            return fuzzy_pick(opts, want) or opts[0]

        if label_match(label, "legally authorized", "authorized to work", "legal age",
                       "eligible to work"):
            return fuzzy_pick(opts, "Yes") or opts[0]

        # STEP 6: Extended government/debarment keywords (from Workday lines 657-662)
        if label_match(label, "been an employee of a u.s. federal", "employee of a u.s. federal",
                       "employee of a u.s. government", "employee of a government",
                       "member of the u.s. armed", "debarred", "suspended",
                       "proposed for debarment", "ineligible for award",
                       "iran, cuba", "north korea or syria", "iran,cuba"):
            return fuzzy_pick(opts, "No") or opts[0]

        # STEP 5: Government "responsibility for matters involving" dropdown (from Workday lines 665-667)
        if label_match(label, "responsibility for matters involving",
                       "responsibilities: are you now", "government responsibilities"):
            return fuzzy_pick(opts, "No") or opts[0]

        # STEP 7: Non-compete / previous employment restrictions (added "non-solicitation")
        if label_match(label, "non-disclosure", "non-compete", "non compete",
                       "previously worked", "prior employ", "work for us before",
                       "worked here", "worked for", "restrictive covenant",
                       "non-solicitation"):
            return fuzzy_pick(opts, "No") or opts[0]

        if label_match(label, "applied", "previously applied", "applied to", "applied before",
                       "ever applied", "interviewed"):
            return fuzzy_pick(opts, "No") or opts[0]

        if label_match(label, "intellectual property", "inventions", "prior inventions",
                       "ownership of inventions"):
            return fuzzy_pick(opts, "No") or opts[0]

        if label_match(label, "relative", "family member", "familial"):
            return fuzzy_pick(opts, "No") or opts[0]

        if label_match(label, "terminat", "asked to resign", "discharged", "dismissed"):
            return fuzzy_pick(opts, "No") or opts[0]

        if label_match(label, "background check", "drug test", "drug screen", "acknowledgment",
                       "acknowledge", "consent to", "i understand and consent",
                       "i understand", "agree to", "attest", "certif"):
            for candidate in opts:
                if re.search(r'\b(understand|consent|agree)\b', candidate, re.IGNORECASE) \
                        and not re.search(r'\b(do not|don.t|not consent|not agree)\b', candidate, re.IGNORECASE):
                    return candidate
            return fuzzy_pick(opts, "Yes") or opts[0]

        if label_match(label, "located in", "applying for employment within", "maryland",
                       "massachusetts", "california", "colorado", "new york"):
            return fuzzy_pick(opts, "No") or opts[0]

        if label_match(label, "eligibil", "start date", "available", "when can you start"):
            return fuzzy_pick(opts, "Immediately") or fuzzy_pick(opts, "2 weeks") or opts[0]

        # STEP 10: Compensation — updated comp read + range_score guard + fallback
        if label_match(label, "compensation", "salary", "pay", "desired"):
            comp = COMP.get("baseline_target_pay", COMP.get("expected_base_salary", 0))
            comp_n = 0
            try:
                comp_n = int(str(comp).replace(",","").replace("$","").strip())
            except (ValueError, TypeError):
                pass
            if comp_n and opts:
                def range_score(opt):
                    # STEP 4 bug fix: guard non-numeric matches
                    nums = [int(n.replace(",","")) for n in re.findall(r'[\d,]+', opt)
                            if n.replace(",","").isdigit() and n.replace(",","")]
                    if len(nums) >= 2:
                        lo, hi = nums[0], nums[1]
                        if lo <= comp_n <= hi: return 0
                        return comp_n - hi if comp_n > hi else lo - comp_n
                    return 9999
                return min(opts, key=range_score)
            return fuzzy_pick(opts, "$75,000") or fuzzy_pick(opts, "$50,000") or opts[0]

        if label_match(label, "relocat"):
            return fuzzy_pick(opts, "Yes") or opts[0]

        # STEP 8: Preferred/office location dropdown (from Workday lines 738-767)
        if label_match(label, "preferred location", "location 1", "location 2", "location 3",
                       "futureforce", "office location", "work location"):
            opts_l = [o.lower() for o in opts]
            # Step 1: Remote always wins
            remote_kws = ("remote", "virtual", "work from home", "work-from-home", "wfh")
            for i, ol in enumerate(opts_l):
                if any(kw in ol for kw in remote_kws) and ol not in ("select one",):
                    return opts[i]
            # Step 2: listing locations (runtime-injected into PROFILE_SUMMARY)
            try:
                _locs = json.loads(PROFILE_SUMMARY).get("job_listing_locations", [])
            except Exception:
                _locs = []
            for loc in _locs:
                hit = fuzzy_pick(opts, loc)
                if hit and hit.lower() != "select one":
                    return hit
            # Step 3: walk the priority ladder
            _rp = LIBRARY.get("routing_priorities", {})
            for ladder_entry in _rp.get("us_location_priority_ladder", []):
                kws = re.split(r'[/,()\s]+', ladder_entry)
                kws = [k.strip().lower() for k in kws if len(k.strip()) > 2]
                for i, ol in enumerate(opts_l):
                    if ol in ("select one",):
                        continue
                    if any(kw in ol for kw in kws):
                        return opts[i]
            return None

        # Postgraduate intent
        if label_match(label, "postgraduate", "graduate degree", "intend to enroll",
                       "pursuing a degree", "graduate school"):
            currently_in_grad = any(
                "attending" in e.get("current_status","").lower() and
                any(kw in e.get("degree_type","").lower() for kw in ("master","phd","doctoral","graduate"))
                for e in EDU
            )
            return fuzzy_pick(opts, "Yes" if currently_in_grad else "No") or opts[0]

        # STEP 9: Communications/future-openings opt-in (from Workday lines 782-789)
        if label_match(label, "future positions", "future openings",
                       "receive communications", "contact me about"):
            non_select = [o for o in opts if o.lower() not in ("select one", "")]
            yes_opt = next((o for o in non_select if re.search(r'\byes\b', o, re.IGNORECASE) or
                           re.search(r'\bwould like\b', o, re.IGNORECASE)), None)
            return yes_opt or (non_select[0] if non_select else None)

        if label_match(label, "language"):
            return fuzzy_pick(opts, "English") or opts[0]

        if label_match(label, "degree", "education level", "highest"):
            if EDU:
                edu = EDU[0]
                abbrev = edu.get("degree_abbreviation","")
                if abbrev:
                    hit = fuzzy_pick(opts, abbrev)
                    if hit: return hit
                for variant in edu.get("degree_search_variants",[]):
                    hit = fuzzy_pick(opts, variant)
                    if hit: return hit
                return fuzzy_pick(opts, edu.get("degree_type","")) or opts[0]

        if label_match(label, "start month"):
            data = WE[0] if ("work" in section or "experience" in section) else (EDU[0] if EDU else None)
            if data: return fuzzy_pick(opts, data.get("start_month","")) or opts[0]

        if label_match(label, "end month"):
            data = WE[0] if ("work" in section or "experience" in section) else (EDU[0] if EDU else None)
            if data: return fuzzy_pick(opts, data.get("end_month","")) or opts[0]

        if label_match(label, "current", "present", "still work", "still attend"):
            data = WE[0] if ("work" in section or "experience" in section) else (EDU[0] if EDU else None)
            if data:
                is_current = data.get("current_status","").lower() in ("currently work here","still attending")
                return fuzzy_pick(opts, "Yes" if is_current else "No") or opts[0]

        return None

    return None


def rule_based_fill_fields(fields: list[dict], context_hint: str = "") -> list[dict]:
    """Apply rule_based_answer to all fields. Returns [{index, value}]."""
    answers = []
    for f in fields:
        val = rule_based_answer(f, context_hint)
        if val is not None and val != "":
            answers.append({"index": f["index"], "value": val})
    return answers


# ── Shared browser/IO helpers ─────────────────────────────────────────────────

async def launch_browser(p, headed: bool, extra_args: list = None, extra_headers: dict = None):
    """Launch a stealth Chrome browser context. Returns (browser, context, page)."""
    base_args = [
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
        "--disable-dev-shm-usage",
    ]
    args = base_args + (extra_args or [])
    launch_kwargs = dict(headless=not headed, args=args)
    if CHROME_PATH:
        launch_kwargs["executable_path"] = CHROME_PATH
    browser = await p.chromium.launch(**launch_kwargs)
    ctx_kwargs = dict(viewport={"width": 1280, "height": 900}, user_agent=USER_AGENT)
    if extra_headers:
        ctx_kwargs["extra_http_headers"] = extra_headers
    context = await browser.new_context(**ctx_kwargs)
    await context.add_init_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
    )
    page = await context.new_page()
    return browser, context, page


def write_json_report(path, obj: dict) -> None:
    """Write obj as indented JSON to path, logging success/failure."""
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(obj, indent=2), encoding="utf-8")
        print(f"  [report] wrote {path}")
    except Exception as e:
        print(f"  [report] write failed: {e}")


def scrape_salary(text: str) -> str | None:
    """Extract salary midpoint from job-description text. Returns str like '$145000' or None."""
    m = re.search(r'\$([\d,]+)\s*[-–—]\s*\$([\d,]+)', text or "")
    if m:
        lo = int(m.group(1).replace(",",""))
        hi = int(m.group(2).replace(",",""))
        return str((lo + hi) // 2)
    m2 = re.search(r'\$([\d,]+)', text or "")
    if m2:
        return m2.group(1).replace(",","")
    return None
