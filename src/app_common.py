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

US_STATE_ABBR = {
    "california":"CA","washington":"WA","texas":"TX","new york":"NY","massachusetts":"MA",
    "indiana":"IN","arizona":"AZ","georgia":"GA","illinois":"IL","colorado":"CO",
    "virginia":"VA","michigan":"MI","florida":"FL","oregon":"OR","north carolina":"NC",
    "pennsylvania":"PA","new jersey":"NJ","ohio":"OH","nevada":"NV","utah":"UT",
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


def _current_connection() -> dict | None:
    """Return the personal_connections entry for the current job's tenant, or None."""
    try:
        _p = json.loads(PROFILE_SUMMARY)
    except Exception:
        _p = {}
    tenant  = (_p.get("job_tenant") or "").lower()
    company = (_p.get("job_company") or "").lower()
    locs    = " ".join(str(l) for l in _p.get("job_listing_locations", [])).lower()
    conns   = LIBRARY.get("personal_connections", {})
    # Direct tenant-key match first (fastest, most reliable)
    if tenant and tenant in conns:
        return conns[tenant]
    # Alias match against company/tenant/locations text
    for c in conns.values():
        if any(a in company or a in tenant or a in locs
               for a in c.get("company_aliases", [])):
            return c
    return None


def rule_based_answer(field: dict, context_hint: str = "", exclude: set = None) -> str | None:
    """Return the best library.json-grounded answer for a field, or None if no match.

    exclude: optional set of option strings to skip (used for per-slot location dedup).
    """
    label   = field.get("label", "")
    opts    = field.get("options", [])
    section = (field.get("section") or field.get("page_heading") or context_hint).lower()
    # Also track context_hint separately so graduation signals passed from smart_fill_page
    # aren't overridden by a generic section heading like "Application Questions 2 of 2".
    _context_hint_lower = context_hint.lower() if context_hint else ""
    tag     = field.get("tag", "")
    ftype   = field.get("type", "")
    current = field.get("value", "")

    # Fields that must always be re-answered (bot may have wrong values from a prior session).
    _force_override_labels = (
        "preferred location", "location 1", "location 2", "location 3",
        "futureforce", "office location", "work location",  # location dropdowns
        "preferred geographic", "geographic preference", "location preference",  # App-Q1 text box
        "postgraduate", "graduate degree", "intend to enroll",
        "pursuing a degree", "graduate school",             # postgrad intent
        "personal relationship",                            # per-company referral answer may change
        "name or email", "their name or email",             # referral follow-up — pre-filled wrong
    )
    _grad_ctx_override = any(k in section or k in _context_hint_lower for k in
                             ("graduation", "anticipated graduation", "expected graduation", "anticipated"))
    # Also detect location dropdowns by option content: if options look like "City, State" pairs
    # (Workday pre-fills aria-label with selected value, so the field label may not say "location").
    _state_abbrevs = (" washington", " california", " texas", " georgia", " massachusetts",
                      " colorado", " virginia", " indiana", " arizona", " michigan", ", wa",
                      ", ca", ", tx", ", ga", ", ma", ", co", ", va", ", in", ", az", ", mi")
    _opts_look_like_locations = (
        tag == "button" and len(opts) >= 4 and
        sum(1 for o in opts if any(s in o.lower() for s in _state_abbrevs)) >= 3
    )
    _force = (any(kw in label.lower() for kw in _force_override_labels)
              or _opts_look_like_locations
              or (_grad_ctx_override and label_match(label, "month", "day", "year")))

    # Skip already-filled fields (except unchecked checkboxes and forced overrides)
    if not _force and current and current.lower() not in ("select one", "", "false", "unchecked") \
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
            # ── React-Select / Greenhouse combobox fields ──────────────────
            if label_match(label, "how did you hear", "source", "referral", "learn about"):
                _hconn = _current_connection()
                if _hconn and _hconn.get("works_at_company"):
                    _ref_terms = _hconn.get("referral_source_terms",
                        ["I know someone at the company", "Employee Referral", "Referral"])
                    return "\n".join(_ref_terms)
                hear_terms = JBM.get("hear_about_us_fallback_order",
                    ["LinkedIn", "Internet/Online Job Posting", "Job Board", "Other"])
                return "\n".join(hear_terms)
            if label_match(label, "country") and not label_match(label, "phone"):
                return "United States"
            if label_match(label, "state", "province"):
                if "united" not in label.lower() and len(label) < 50:
                    return PI["state"]
            if label_match(label, "city", "location (city)", "candidate-location"):
                return PI.get("city", "San Jose")
            # Education dropdowns
            if label_match(label, "school", "institution", "university", "college") \
                    and not label_match(label, "high school"):
                school = EDU[0]["institution_variants"][0] if EDU else ""
                return school
            if label_match(label, "degree", "education level", "highest education"):
                deg = (EDU[0].get("degree_type") or EDU[0].get("degree","Bachelor's Degree") if EDU else "Bachelor's Degree")
                return deg
            # Custom GH questions
            if label_match(label, "open to working", "onsite", "on site", "4 days"):
                # Sigma: options are NYC / SF / London / Remote only
                # "No" → "Remote only"; prefer SF if local, else Remote only
                city = PI.get("city", "").lower()
                if "san francisco" in city or "sf" in city or "san jose" in city \
                        or "milpitas" in city or "sunnyvale" in city or "bay" in city:
                    return "SF"
                return "Remote only"
            if label_match(label, "relocat"):
                return "Yes"
            if label_match(label, "acknowledge", "confirm", "agree to the following",
                           "certif", "consent"):
                return "Yes"
            if label_match(label, "sponsor", "h-1b", "h1b", "visa sponsorship"):
                return "No" if not NEEDS_SPONSOR else "Yes"
            if label_match(label, "visa status", "current visa", "work authorization status"):
                return PI.get("visa_status", "U.S. Citizen")
            if label_match(label, "personal pronoun", "pronoun"):
                return PI.get("pronouns", "He/ Him")
            # EEO / self-identification dropdowns
            # When configured to decline, return a decline sentinel for ALL identity fields
            # (gender identity, sexual orientation, transgender, gender, sex, race, ethnicity,
            # hispanic/latino) — the executor fuzzy-matches this against the real options.
            _decline_mode = "decline" in str(REG.get("primary_demographic_action", "")).lower()
            # Decline ONLY the expanded LGBTQ+ identity fields (not standard EEO gender/race/hispanic)
            if _decline_mode and label_match(label, "gender identity", "sexual orientation",
                                             "transgender"):
                return "I do not wish to answer"
            if label_match(label, "gender", "sex"):
                fb = REG.get("fallback_gender", "Male")
                return fb
            if label_match(label, "hispanic", "latino"):
                val = str(REG.get("fallback_hispanic_ethnicity", "No")).lower()
                return "No" if val in ("no", "false", "0", "") else "Yes"
            if label_match(label, "race", "ethnicity"):
                return REG.get("fallback_race", "Asian")
            if label_match(label, "veteran"):
                return "I am not a protected veteran"
            if label_match(label, "disability"):
                if DISABILITY_ANSWER == "yes":
                    return "Yes, I have a disability, or have had one in the past"
                return "I do not want to answer"
            return None

        if label_match(label, "preferred first name", "preferred name", "nickname", "goes by"):
            return PI.get("preferred_name", "Josh")
        if label_match(label, "first name", "given name"):       return PI["first_name"]
        if label_match(label, "last name", "surname", "family name"): return PI["last_name"]
        if label_match(label, "middle name", "middle initial"):  return ""
        if label_match(label, "email") \
                and not label_match(label, "name or email", "their name or email",
                                    "name of the employee", "who referred",
                                    "person you know", "someone you know", "referrer"):
            return PI["email"]
        if label_match(label, "phone number", "telephone", "cell number", "mobile number", "phone"):
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
        if label_match(label, "salary", "compensation", "pay", "wage", "expected") \
                and not label_match(label, "open to working", "onsite", "4 days"):
            try: _ls_ft = int(json.loads(PROFILE_SUMMARY).get("job_listing_salary") or 0)
            except Exception: _ls_ft = 0
            _factor_ft = float(COMP.get("desired_comp_scale_factor", 0.95))
            if _ls_ft:
                return str(round(_ls_ft * _factor_ft))
            return str(COMP.get("baseline_target_pay", COMP.get("expected_base_salary", "120000")))

        if label_match(label, "current company", "current employer", "employer name") \
                and "work" not in section and "experience" not in section:
            return WE[0]["company"] if WE else ""

        if label_match(label, "current visa status", "visa status", "work authorization status") \
                and not field.get("isSelectInput"):
            return PI.get("visa_status", "U.S. Citizen")

        # Referral follow-up: "who referred you" / "name or email of the employee" etc.
        if label_match(label, "who referred", "person you know", "someone you know", "referrer",
                       "name or email", "their name or email", "name of the employee", "employee name") \
                or (label_match(label, "referral") and label_match(label, "name", "email", "contact")):
            _rconn = _current_connection()
            if _rconn:
                return _rconn.get("referral_contact") or _rconn.get("name", "")
            return ""

        if label_match(label, "name") and "employee" not in label.lower() \
                and not label_match(label, "provide the name", "name of the",
                                    "name and relationship", "organization", "relationship",
                                    "who referred", "person you know", "referrer",
                                    "name or email", "employee name"):
            return f"{PI['first_name']} {PI['last_name']}"

        # Follow-up text field after a "personal relationship" Yes answer: provide name/relationship.
        if label_match(label, "provide the name", "name of the person", "name and relationship",
                       "their name", "list the name", "provide their name"):
            conn = _current_connection()
            if conn:
                if label_match(label, "relationship"):
                    return f"{conn['name']} ({conn['relationship']})"
                return conn["name"]
            return ""  # no connection for this company — field shouldn't appear (we answered No)

        # STEP 4: Preferred geographic/location TEXT field (App-Q1 textarea; NOT the App-Q2 dropdowns).
        # Use the LISTING's primary location, not the candidate's home city.
        if label_match(label, "preferred geographic", "preferred location", "geographic preference",
                       "location preference"):
            try:
                _ll = json.loads(PROFILE_SUMMARY).get("job_listing_locations", [])
            except Exception:
                _ll = []
            if _ll:
                _primary = str(_ll[0]).strip()          # e.g. "California - San Francisco"
                if " - " in _primary:                    # normalize "State - City" → "City, ST"
                    _state, _city = [p.strip() for p in _primary.split(" - ", 1)]
                    _st = US_STATE_ABBR.get(_state.lower(), _state)
                    return f"{_city}, {_st}"
                return _primary
            return PI.get("city", "San Diego") + ", CA"  # fallback: home city

        # Graduation context: check the field label OR the page/section context (e.g. question heading
        # "Please input your anticipated graduation date" passed via context_hint/heading as `section`).
        _grad_ctx = any(k in section or k in _context_hint_lower for k in
                        ("graduation", "anticipated graduation", "expected graduation", "anticipated"))
        # Graduation date FIRST — keyed off page context so bare Month/Day/Year fields still fire.
        if _grad_ctx and label_match(label, "month", "day", "year"):
            school = next((e for e in EDU if "attending" in e.get("current_status","").lower()), None)
            if school:
                if label_match(label, "month"): return MONTH_NUM.get(school.get("end_month","").lower(), "")
                if label_match(label, "day"):   return "01"
                if label_match(label, "year"):  return str(school.get("end_year",""))
        # Keep old label-based graduation branch for edge cases where label includes "graduation".
        if label_match(label, "graduation", "anticipated graduation", "expected graduation") and \
                label_match(label, "month", "day", "year"):
            school = next((e for e in EDU if "attending" in e.get("current_status","").lower()), None)
            if school:
                if label_match(label, "month"): return MONTH_NUM.get(school.get("end_month","").lower(), "")
                if label_match(label, "day"):   return "01"
                if label_match(label, "year"):  return str(school.get("end_year",""))
        # Generic availability date — only when NOT a graduation page.
        if not _grad_ctx and label_match(label, "month", "day", "year") and (
            "application" in section or
            not any(k in section for k in ("work","experience","education","school","self identify","signature"))
        ):
            import datetime
            avail = datetime.date.today() + datetime.timedelta(weeks=2)
            if label_match(label, "month"): return str(avail.month).zfill(2)
            if label_match(label, "day"):   return str(avail.day).zfill(2)
            if label_match(label, "year"):  return str(avail.year)

        if "work" in section or "experience" in section or "employment" in section:
            if label_match(label, "company", "employer", "organization"):
                return WE[0]["company"] if WE else ""
            if label_match(label, "title", "position", "role"):
                return WE[0]["role"] if WE else ""
            if label_match(label, "city", "location"):
                return WE[0].get("city", "") if WE else ""
            if label_match(label, "description", "responsibilities", "summary"):
                if not WE: return ""
                desc = WE[0].get("description","")
                ml = field.get("maxlength") or 0
                cap = ml if ml > 0 else 4000  # respect DOM limit; 4000 safe ceiling if absent
                return desc[:cap]
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

        _opts_l_all = [o.lower() for o in opts]
        _is_yes_no = set(o for o in _opts_l_all if o not in ("select one", "")) <= {"yes", "no"}
        if not _is_yes_no and label_match(label, "export control", "arms export", "itar",
                                          "u.s. persons", "citizenship status",
                                          "controlled information"):
            return fuzzy_pick(opts, "U.S. Citizen") or next(
                (o for o in opts if "citizen" in o.lower() and "select" not in o.lower()), None)

        if label_match(label, "18 years of age", "18 years old", "at least 18", "18 years or older",
                       "years of age or older", "at least 18 years"):
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

        if label_match(label, "gender identity", "sexual orientation", "transgender",
                       "gender", "sex", "race", "ethnicity", "hispanic",
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
            _hconn = _current_connection()
            if _hconn and _hconn.get("works_at_company"):
                for _rterm in _hconn.get("referral_source_terms",
                        ["I know someone at the company", "Employee Referral", "Referral"]):
                    _rpick = fuzzy_pick(opts, _rterm)
                    if _rpick: return _rpick
                # no referral-style option present — fall through to LinkedIn ladder
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
            return (fuzzy_pick(opts, "Mobile") or fuzzy_pick(opts, "Personal Cell")
                    or fuzzy_pick(opts, "Cell") or fuzzy_pick(opts, "Mobile Phone")
                    or fuzzy_pick(opts, "Work Cell") or fuzzy_pick(opts, "Home")
                    or next((o for o in opts if o.lower() != "select one"), None))

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

        # STEP 7: Non-compete / previous employment restrictions (hyphenated AND un-hyphenated forms)
        if label_match(label, "non-disclosure", "nondisclosure", "non-compete", "noncompete",
                       "non compete", "previously worked", "prior employ", "work for us before",
                       "worked here", "worked for", "restrictive covenant",
                       "non-solicitation", "nonsolicitation"):
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

        # Felony / criminal history
        if label_match(label, "felony", "convicted", "criminal", "misdemeanor", "crime",
                       "charged", "indicted", "injunction", "judgment", "decree"):
            return fuzzy_pick(opts, "No") or opts[0]

        # Officer / board-member / incorporator status — Josh is not an officer of any org
        if label_match(label, "officer or board", "board member", "incorporator",
                       "serve as an officer", "serve as a board"):
            return fuzzy_pick(opts, "No") or opts[0]

        # Personal-relationship — split by question type:
        # "works FOR <this company>" (no vendor/conflict signal) → Yes if connection here; else No.
        # "vendor/competitor/conflict" or generic personal-relationship → always No.
        _has_vendor_signal = label_match(label, "vendor", "competitor", "subcontractor",
                                         "doing business with", "conflict of interest",
                                         "contractor", "broker", "agent")
        if label_match(label, "personal relationship") and not _has_vendor_signal \
                and label_match(label, "work for", "works for", "employed by",
                                "employee of", "work at", "work for us"):
            conn = _current_connection()
            return fuzzy_pick(opts, "Yes") if (conn and conn.get("works_at_company")) \
                   else (fuzzy_pick(opts, "No") or opts[0])

        if label_match(label, "personal relationship", "conflict of interest",
                       "vendor", "competitor", "subcontractor", "doing business with"):
            return fuzzy_pick(opts, "No") or opts[0]

        # Debarment / exclusion from federal health-care programs
        if label_match(label, "excluded", "ineligible to perform", "federal health care",
                       "federal health-care", "excluded or otherwise ineligible"):
            return fuzzy_pick(opts, "No") or opts[0]

        # Second job / outside employment
        if label_match(label, "second job", "outside employment", "other employment",
                       "employment other than"):
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
            try: _ls_d = int(json.loads(PROFILE_SUMMARY).get("job_listing_salary") or 0)
            except Exception: _ls_d = 0
            _factor_d = float(COMP.get("desired_comp_scale_factor", 0.95))
            comp = COMP.get("baseline_target_pay", COMP.get("expected_base_salary", 0))
            comp_n = 0
            try:
                comp_n = round(_ls_d * _factor_d) if _ls_d else int(str(comp).replace(",","").replace("$","").strip())
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
            _excl = exclude or set()
            opts_l = [o.lower() for o in opts]
            # Step 1: Remote always wins (but skip if excluded by prior slot).
            remote_kws = ("remote", "virtual", "work from home", "work-from-home", "wfh")
            for i, ol in enumerate(opts_l):
                if any(kw in ol for kw in remote_kws) and ol not in ("select one",) \
                        and opts[i] not in _excl:
                    return opts[i]
            # Step 2 removed: listing-location fuzzy match caused "california" token collision
            # (e.g. "California - San Francisco" → "Irvine/Santa Monica, California").
            # Drive entirely from the priority ladder (Step 3) which uses specific city/region tokens.
            # Step 3: walk the correct priority ladder.
            # Determine country (US vs Canada) from listing locations; default US.
            _rp = LIBRARY.get("routing_priorities", {})
            try:
                _locs2 = json.loads(PROFILE_SUMMARY).get("job_listing_locations", [])
            except Exception:
                _locs2 = []
            _canada_tokens = ("canada", "ontario", "british columbia", "alberta", "quebec",
                              "toronto", "vancouver", "calgary", "montreal", " bc ", " on ", " ab ")
            _us_tokens = ("united states", "california", "new york", "texas", "washington",
                          "georgia", "illinois", "massachusetts", "colorado", "virginia",
                          "indiana", "arizona", "michigan", " ca ", " ny ", " tx ", " wa ")
            _locs2_lower = [str(l).lower() for l in _locs2]
            _is_canada = (any(tok in l for tok in _canada_tokens for l in _locs2_lower)
                          and not any(tok in l for tok in _us_tokens for l in _locs2_lower))
            _country = "canada" if _is_canada else "us"
            _ladder = (_rp.get(f"onsite_{_country}_location_priority_ladder", [])
                       or _rp.get(f"remote_{_country}_location_priority_ladder", []))
            for ladder_entry in _ladder:
                kws = re.split(r'[/,()\s]+', ladder_entry)
                # Minimum 6 chars to avoid short tokens ("san", "bay", "los", "new", "santa",
                # "menlo", "palo", "clara") causing cross-city collisions — e.g. "santa" from
                # "Santa Clara" (Bay Area ladder entry) matching "Santa Monica" in an option.
                kws = [k.strip().lower() for k in kws if len(k.strip()) >= 6]
                for i, ol in enumerate(opts_l):
                    if ol in ("select one",):
                        continue
                    if opts[i] in _excl:
                        continue
                    if any(kw in ol for kw in kws):
                        return opts[i]
            return None

        # Postgraduate intent — "Do you intend to ENROLL in a postgraduate degree?"
        # Josh is already in/completing his terminal Master's program, so he will NOT newly
        # enroll in another postgraduate degree → always "No".
        if label_match(label, "postgraduate", "graduate degree", "intend to enroll",
                       "pursuing a degree", "graduate school"):
            return fuzzy_pick(opts, "No") or (opts[-1] if opts else None)

        # STEP 9: Communications/future-openings — always opt OUT ("No / please do not contact").
        if label_match(label, "future positions", "future openings",
                       "receive communications", "contact me about"):
            non_select = [o for o in opts if o.lower() not in ("select one", "")]
            no_opt = next((o for o in non_select
                           if re.search(r'do not|don.t|please do not', o, re.IGNORECASE)
                           or (re.search(r'\bno\b', o, re.IGNORECASE)
                               and not re.search(r'\byes\b', o, re.IGNORECASE))), None)
            return no_opt or (non_select[-1] if non_select else None)

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

        # "current"/"present"/"still work" — ONLY fire in a work/edu/experience section context
        # to avoid matching unrelated labels that happen to contain "currently" (e.g. "Do you
        # currently serve as an officer or board member…")
        if label_match(label, "current", "present", "still work", "still attend") and \
                any(k in section for k in ("work", "experience", "education", "school",
                                           "employment", "position", "job")):
            data = WE[0] if ("work" in section or "experience" in section) else (EDU[0] if EDU else None)
            if data:
                is_current = data.get("current_status","").lower() in ("currently work here","still attending")
                return fuzzy_pick(opts, "Yes" if is_current else "No") or opts[0]

        return None

    return None


def rule_based_fill_fields(fields: list[dict], context_hint: str = "") -> list[dict]:
    """Apply rule_based_answer to all fields. Returns [{index, value}]."""
    answers = []
    _used_locations: set = set()  # per-slot dedup across preferred-location dropdowns
    _location_labels = ("preferred location", "location 1", "location 2", "location 3",
                        "futureforce", "office location", "work location")
    for f in fields:
        is_loc = any(kw in f.get("label","").lower() for kw in _location_labels)
        if is_loc:
            val = rule_based_answer(f, context_hint, exclude=_used_locations)
        else:
            val = rule_based_answer(f, context_hint)
        if val is not None and val != "":
            if is_loc:
                _used_locations.add(val)
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
