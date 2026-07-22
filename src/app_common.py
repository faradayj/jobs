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
SKILLS  = LIBRARY.get("skills", [])
ROLE_PREFS = LIBRARY.get("role_preferences", {})
PREPARED_ANSWERS = LIBRARY.get("prepared_answers", {})

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

def generic_fit_blurb(max_len: int = 600) -> str:
    """Generic 'why are you a good fit' short-answer built from the candidate's own
    profile — used as a rules-only fallback for required open-ended prose questions
    when no DeepSeek key is available. Not tailored to the specific listing, but avoids
    leaving a required field blank."""
    titles = ROLE_PREFS.get("target_titles", []) or ["Software Engineer"]
    title = titles[0]
    degree = EDU[0].get("degree_type", "") if EDU else ""
    major = (EDU[0].get("major_search_term") or "") if EDU else ""
    school = (EDU[0]["institution_variants"][0] if EDU and EDU[0].get("institution_variants") else "")
    top_skills = ", ".join(SKILLS[:6]) if SKILLS else ""
    recent_work = WE[0].get("description", "") if WE else ""
    recent_work_snippet = recent_work[:200].rsplit(" ", 1)[0] if recent_work else ""

    parts = [f"I'm a {title.lower()} candidate"]
    if degree and school:
        parts.append(f"with a {degree} in {major or 'Computer Science'} from {school}")
    if top_skills:
        parts.append(f"and hands-on experience with {top_skills}")
    blurb = " ".join(parts) + "."
    if recent_work_snippet:
        blurb += f" Recently, {recent_work_snippet}."
    return blurb[:max_len].rstrip()


def generic_experience_blurb(max_len: int = 600) -> str:
    """Generic technical-experience short-answer for open-ended questions this rules engine
    cannot meaningfully tailor (e.g. "describe a system you've built", "what startup/founder
    experience do you have", "what AI-tool experience do you have"). Grounded in the
    candidate's actual work description — honest, not tailored to the specific question, but
    avoids leaving a required field blank. A DeepSeek-backed run would answer these questions
    with real judgment; this is a rules-only placeholder."""
    recent_work = WE[0].get("description", "") if WE else ""
    if not recent_work:
        return "I don't have directly applicable experience with this yet, but I'm eager to learn."
    return recent_work[:max_len].rstrip()

DECLINE_KEYWORDS = ["not wish", "don't wish", "prefer not", "decline", "choose not",
                    "not wish to self", "no wish", "i do not wish"]

def pick_decline(opts: list[str]) -> str | None:
    for o in opts:
        if any(k in o.lower() for k in DECLINE_KEYWORDS):
            return o
    return None

def fuzzy_pick(opts: list[str], value: str) -> str | None:
    """Fuzzy match value against options: exact → starts → contains (normalises apostrophes,
    and treats hyphens/commas as equivalent word separators, e.g. "X - Y" == "X, Y")."""
    def _norm(s: str) -> str:
        s = re.sub(r"['‘’‚‛]", "", s.lower().strip())
        s = re.sub(r"[,\-]", " ", s)
        return re.sub(r"\s+", " ", s).strip()
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


def pick_gpa_bucket(opts: list[str], gpa: float) -> str | None:
    """Match a numeric GPA against range-bucket options like '3.75+', '3.4 - 3.70',
    'Below 3.4' — fuzzy_pick can't handle these since the GPA value never appears
    verbatim in the option text. Picks the bucket whose numeric range contains gpa,
    preferring the highest-matching bucket when ranges overlap ('+' buckets first)."""
    best = None
    for o in opts:
        ol = o.lower().strip()
        m_plus = re.match(r"^([\d.]+)\s*\+", ol)
        if m_plus and gpa >= float(m_plus.group(1)):
            return o  # '+' bucket is unambiguous — take it immediately
        m_below = re.match(r"^below\s+([\d.]+)", ol)
        if m_below and gpa < float(m_below.group(1)):
            best = best or o
            continue
        m_range = re.match(r"^([\d.]+)\s*-\s*([\d.]+)", ol)
        if m_range and float(m_range.group(1)) <= gpa <= float(m_range.group(2)):
            best = best or o
    return best


# Preference order for "which engineering/role track?" ranked-choice questions (1st/2nd/3rd
# engineering preference, etc.) when no rule-based value is knowable ahead of time — the
# candidate's target roles skew backend/systems, so prefer those tracks over frontend/mobile
# when picking among a board's real (live-discovered) options.
ROLE_TRACK_PRIORITY = ["backend", "full-stack", "fullstack", "full stack", "infrastructure",
                       "systems", "platform", "distributed systems", "data engineering",
                       "machine learning", "ml", "api", "server", "frontend", "front-end",
                       "front end", "mobile", "ios", "android", "web"]


def pick_role_track(opts: list[str], avoid: set | None = None) -> str | None:
    """Pick the best-fit option from a ranked engineering/role-track dropdown (e.g. 1st/2nd/
    3rd engineering preference), preferring backend-leaning tracks per ROLE_TRACK_PRIORITY.
    Skips anything already in `avoid` (so 1st/2nd/3rd picks differ). Falls back to the first
    remaining option if nothing in the priority list matches."""
    avoid = avoid or set()
    candidates = [o for o in opts if o not in avoid] or list(opts)
    for keyword in ROLE_TRACK_PRIORITY:
        match = next((o for o in candidates if keyword in o.lower()), None)
        if match:
            return match
    return candidates[0] if candidates else None


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
        "highest level of education", "education level", "education completed",  # may be wrong from prior run
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
        # "N/A" checkbox on event-attendance lists — check it when label is exactly N/A
        if label.strip().upper() == "N/A":
            return "true"
        if label_match(label, "procuring contracting officer", "source selection",
                       "program manager", "administrative contracting",
                       "award a contract", "establish overhead", "approve issuance",
                       "pay or settle a claim", "senior employee", "political appointee",
                       "public financial disclosure", "covered dod official",
                       "military officer", "official involved with contracts",
                       "otherwise involved with snc", "otherwise involved"):
            return "false"
        # "I am not a member of a technical club or project" — check it (opt-out option)
        if label_match(label, "not a member of a technical club", "i am not a member"):
            return "true"

        # Location checkbox group "Available for Any" — always opt in to all locations
        if label.strip().lower() == "available for any":
            return "true"

        # Location checkbox GROUPS where each option is its own checkbox labeled "City, ST"
        # (e.g. "San Francisco, CA", "Seattle, WA") — per
        # role_preferences.location_and_relocation_rule, the candidate is open to relocating
        # anywhere, so multi-select location lists should have ALL options checked.
        if re.match(r"^\s*[A-Za-z .'-]+,\s*[A-Z]{2}\s*$", label.strip()):
            return "true"

        # Disney "Why are you interested in working for us?" checkbox group — pick career advancement
        if label_match(label, "opportunity for career advancement"):
            return "true"

        # Start-date availability checkbox GROUPS (each option scanned as its own checkbox,
        # e.g. "June 2026", "July 2026", ... "After October 1, 2026"). Check the box for the
        # earliest month that is still >= ~1 month from today — matches the single-line
        # "when are you available to start" text answer used elsewhere ("within one month").
        _month_label = re.match(
            r"^\s*(january|february|march|april|may|june|july|august|september|october|"
            r"november|december)\s+(\d{4})\s*$", label.strip(), re.I)
        _after_label = re.match(
            r"^\s*after\s+(january|february|march|april|may|june|july|august|september|october|"
            r"november|december)\s+\d{1,2},?\s*(\d{4})\s*$", label.strip(), re.I)
        if _month_label or _after_label:
            import datetime as _dt
            _target = _dt.date.today() + _dt.timedelta(days=30)
            _target_month_start = _dt.date(_target.year, _target.month, 1)
            if _month_label:
                _opt_year = int(_month_label.group(2))
                _opt_month = int(MONTH_NUM[_month_label.group(1).lower()])
                _opt_date = _dt.date(_opt_year, _opt_month, 1)
                return "true" if _opt_date == _target_month_start else "false"
            # "After <Month> <Day>, <Year>" catch-all option — only check it if the target
            # month is strictly later than the named month (i.e. no earlier listed option fits).
            _after_year = int(_after_label.group(2))
            _after_month = int(MONTH_NUM[_after_label.group(1).lower()])
            _after_date = _dt.date(_after_year, _after_month, 1)
            return "true" if _target_month_start > _after_date else "false"

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
            # "Do you live within commutable distance of <City>?" / "are you local to <City>?"
            # — a Yes/No proximity question, not a request for the candidate's own city.
            # Must be checked BEFORE the generic "city" match below, since city names like
            # "New York City" or "Kansas City" contain the substring "city" and would
            # otherwise wrongly return the candidate's home city as the answer.
            if label_match(label, "commutable distance", "commuting distance", "live within",
                           "local to", "live near", "based near", "based in the area"):
                return "No"
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
            if label_match(label, "discipline", "major", "field of study"):
                return EDU[0].get("major_search_term") or (EDU[0]["major_variants"][0] if EDU and EDU[0].get("major_variants") else "")
            # Education Start/End Month react-select dropdowns (e.g. Greenhouse's paired
            # "Start date month" / "End date month" fields, distinct from the plain-text
            # Year counterparts handled elsewhere in the text branch).
            if re.match(r"^\s*start\s+(date\s+)?month\s*\*?\s*$", label.strip(), re.I) and EDU:
                return EDU[0].get("start_month", "")
            if re.match(r"^\s*end\s+(date\s+)?month\s*\*?\s*$", label.strip(), re.I) and EDU:
                return EDU[0].get("end_month", "")
            # "Have you ever been employed by / worked at <Company>...?" — previous-employment
            # screening question. Distinguish from "current or previous employer" (a free-text
            # field asking WHO the candidate worked for) by requiring "employed by"/"worked
            # for"/"worked at" phrasing, not just "employer".
            if label_match(label, "employed by", "have you worked for", "have you previously worked",
                           "have you ever worked", "have you worked at"):
                return "No"
            # Industry-experience / internship-count screening questions — candidate has
            # multiple internships and current full-time experience, so "Yes" is accurate.
            if label_match(label, "years of industry work experience", "completed at least",
                           "relevant full-time experience", "internship"):
                return "Yes"
            # Earliest available start date (react-select variant; free-text variant is
            # handled elsewhere via "when are you available to start").
            if label_match(label, "earliest available start date", "available to start"):
                return "Within one month of receiving an offer."
            # Role-track interest screening (e.g. "interest and experience in a Mobile role?")
            # — candidate's target titles are Software Engineer / Data Scientist / MLE with no
            # mobile/frontend specialization claimed; answer No unless it's a backend/full-stack
            # variant of the same question.
            if label_match(label, "interest and experience in a") and label_match(label, "role"):
                if label_match(label, "backend", "full-stack", "fullstack", "full stack"):
                    return "Yes"
                return "No"
            if label_match(label, "gpa"):
                return str(EDU[0].get("gpa", "")) if EDU else ""
            if label_match(label, "sms", "whatsapp", "text message"):
                return "No"
            # "Are you [currently] [authorized/eligible] to work [lawfully] in <Country>...?"
            # — distinct from visa-sponsorship questions (handled separately below); default
            # Yes for the candidate's home country. Match "authorized...to work...in" and
            # "eligible...to work...in" loosely since companies insert words like "lawfully"
            # or a company name in between (e.g. "authorized to work lawfully in the US for X").
            if label_match(label, "eligible to work in", "authorized to work in",
                           "currently eligible to work"):
                return "Yes"
            if (label_match(label, "authorized", "eligible") and label_match(label, "work")
                    and label_match(label, " in ")):
                return "Yes"
            # Bare "U.S. WORK AUTHORIZATION" style labels (no "work in"/"eligible to work"
            # phrasing at all — just a section-style label followed by a plain Yes/No
            # dropdown). Distinct from the phrasing-dependent matches above; some boards
            # (e.g. Anduril) use this terser style.
            if re.match(r"^\s*u\.?s\.?\s+work\s+authorization\s*\*?\s*$", label.strip(), re.I):
                return "Yes"
            # "If you have held a clearance in the past, what level?" follow-up — check
            # BEFORE the general clearance-eligibility rule below (both mention "clearance";
            # this one is specifically about a *past level*, not current eligibility).
            if label_match(label, "clearance") and label_match(label, "level", "held"):
                return "N/A"
            # Security clearance eligibility — three-option style ("Yes, I hold an active
            # clearance" / "Yes, I am eligible for a clearance" / "No"). Candidate is a U.S.
            # citizen with no active clearance, so "eligible" (not "hold") is accurate.
            # Requires "obtain and maintain" phrasing (the real clearance-eligibility
            # question's distinctive wording) to avoid over-matching compound labels that
            # merely mention "clearance eligibility" in passing while actually being a plain
            # Yes/No "are you eligible to meet this requirement" question (see below).
            if label_match(label, "obtain and maintain", "obtain or maintain") and \
                    label_match(label, "clearance"):
                return "eligible"
            # Export-control "U.S. Person status" question with a REAL multi-option answer
            # list (citizen/national vs green card vs refugee/asylee vs none). The label
            # itself doesn't literally say "green card" (that's only in the OPTIONS, which
            # aren't populated yet at rule-evaluation time for react-selects) — so distinguish
            # by structure instead: this variant is a declarative "EXPORT CONTROLS -" preamble
            # ending in a period, not a direct "are you a ...?" question ending in "?".
            if label.strip().upper().startswith("EXPORT CONTROLS") and "?" not in label:
                return "citizen"
            # Bare Yes/No variants of the U.S. Person / export-control / clearance-adjacent
            # access-requirement question (an explicit "...are you eligible/are you a...?"
            # question, distinct from the declarative multi-option preamble above).
            if label_match(label, "u.s. person", "export control", "clearance eligibility") and \
                    label_match(label, "eligible to meet", "are you eligible", "are you a"):
                return "Yes"
            # Conflict-of-interest Yes/No screening question.
            if label_match(label, "conflict of interest"):
                return "No"
            # "History with [Company]" bare Yes/No toggle — distinct from the more specific
            # "have you ever been employed by [Company]" question (handled elsewhere), which
            # some boards ask as a separate, more general "any history" checkbox.
            if label_match(label, "history with") and not label_match(label, "employed by"):
                return "No"
            # "Top location preference" — a real dropdown of the company's actual office
            # cities, no strong signal on which to prefer since the candidate is open to
            # relocating anywhere (role_preferences.location_and_relocation_rule). Sentinel
            # tells the executor to pick the first live-discovered option rather than leave
            # a required field blank, same pattern as the engineering-preference sentinel.
            if label_match(label, "location preference", "top location"):
                return "__PICK_FIRST_OPTION__"
            # Company-specific "engineering/team preference" ranked dropdowns (1st/2nd/3rd choice
            # among role tracks). scan_fields never opens the dropdown, so `options` is empty
            # at rule-evaluation time — the executor (gh_exec_react_select) discovers the real
            # options live when it clicks the field. Returning a sentinel here (rather than None)
            # ensures execute_answer still calls the executor; its own no-match fallback picks
            # the first real option instead of leaving a required field blank.
            if label_match(label, "engineering preference", "engineering profile", "team preference",
                           "role preference") and label_match(label, "first", "second", "third",
                           "1st", "2nd", "3rd"):
                return "__PICK_FIRST_OPTION__"
            # "Are you comfortable coming in N days a week to the office?" — plain Yes/No
            # variant, distinct from Sigma's NYC/SF/London/Remote-only multi-choice below.
            if re.search(r"\d+\s*days?\s+a\s+week", label, re.I) and label_match(label, "office", "comfortable"):
                return "Yes"
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
        if label_match(label, "address line 2", "apt number", "suite number",
                       "apt/suite", "unit number", "unit #") or label.strip() in ("apt", "suite", "unit"): return ""
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

        if label_match(label, "current company", "current employer", "employer name",
                       "current or previous employer") \
                and "work" not in section and "experience" not in section:
            return WE[0]["company"] if WE else ""

        if label_match(label, "current or previous job title", "current job title",
                       "current or previous role", "current position"):
            return WE[0]["role"] if WE else ""

        # "Are you comfortable coming in N days a week to the office?" — plain Yes/No
        # variant. Present here too (not just the isSelectInput branch above) since some
        # Greenhouse boards render this as a plain text/react-select toggle rather than a
        # dropdown — scan_fields' isSelectInput flag varies per board.
        if re.search(r"\d+\s*days?\s+a\s+week", label, re.I) and label_match(label, "office", "comfortable"):
            return "Yes"

        # Open-ended "tell us about yourself / why are you a good fit" prose question.
        # DeepSeek would normally tailor this to the listing; without it, a generic
        # profile-based blurb still beats leaving a required field blank.
        if label_match(label, "tell us a little bit about you", "why you would be a good fit",
                       "why do you want to work", "why are you interested", "tell us about yourself"):
            return generic_fit_blurb()

        # "Where did you attend undergrad?" / "What did you get your undergrad degree in?"
        # — answerable directly from EDU data (the Bachelor's entry, or the last EDU entry
        # if none is explicitly marked "Graduated"). Matches "undergrad(uate)" tolerantly
        # (e.g. via `re.search(r"under\s*gr?a[dt]")`) since real listings sometimes have
        # typos in these custom questions (seen: "undergad" missing the second "r").
        _undergrad_re = re.compile(r"under\s*gr?a[dt]", re.I)
        if _undergrad_re.search(label) and label_match(label, "where", "school", "institution", "attend"):
            _undergrad = next((e for e in EDU if "graduated" in e.get("current_status","").lower()),
                              EDU[-1] if EDU else None)
            return _undergrad["institution_variants"][0] if _undergrad else ""
        if _undergrad_re.search(label) and label_match(label, "degree", "major", "get your", "study"):
            _undergrad = next((e for e in EDU if "graduated" in e.get("current_status","").lower()),
                              EDU[-1] if EDU else None)
            return (_undergrad.get("major_search_term") or "") if _undergrad else ""

        # Open-ended technical/experience questions this rules engine can't meaningfully
        # tailor without DeepSeek. Several of these were manually answered by hand on a
        # real listing (Wonderschool, 2026-07-21) — those real, specific answers are stored
        # in library.json's prepared_answers and reused here for the same question shape.
        # Anything not matching one of these specific shapes falls through to the generic,
        # honest, profile-grounded blurb (still better than a blank required field, but
        # DeepSeek would do meaningfully better here if available).
        if label_match(label, "significant portion of your time", "significant amount of time",
                       "working directly with customers"):
            return PREPARED_ANSWERS.get("customer_facing_time_commitment") or generic_experience_blurb()
        if label_match(label, "experience do you have using tools like", "claude code",
                       "ai-assisted development"):
            return PREPARED_ANSWERS.get("ai_dev_tool_experience") or generic_experience_blurb()
        if label_match(label, "startup or founder experience"):
            return PREPARED_ANSWERS.get("startup_founder_experience") or generic_experience_blurb()
        if label_match(label, "describe a system you", "system you've built", "system you have built"):
            return PREPARED_ANSWERS.get("system_you_built") or generic_experience_blurb()

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

        # "If your school isn't listed above, let us know your school name here" — a fallback
        # free-text field for when the school-selector dropdown lacks the candidate's
        # institution. Leave blank: the real school was already matched and selected in the
        # dropdown field, so this fallback isn't needed and must NOT receive the candidate's
        # own name (its label contains the substring "name", which would otherwise fall
        # through to the generic person-name handler below).
        if label_match(label, "school name", "university name", "institution name",
                       "know your school", "let us know your school"):
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

        # Bare Month/Day/Year INPUT WIDGET detector — require the label (stripped of a
        # trailing "*"/whitespace, and an optional "start"/"end"/"graduation"/"anticipated"
        # prefix) to be ESSENTIALLY JUST the unit word itself ("Month", "Day", "Year",
        # "Start date year", "Anticipated graduation month", etc.), not a bare substring
        # match. A substring test alone false-positives on ordinary prose that happens to
        # contain "day"/"month"/"year" — e.g. "Are you comfortable coming in 5 DAYS a week
        # to the office?" — which would otherwise get a nonsensical day-of-month answer.
        # `section` is NOT used as a signal here: confirmed unreliable on Greenhouse forms
        # (scan_fields' getSection() often returns the PREVIOUS field's own label, not a
        # real heading), so gating on section content is no safer than not gating at all.
        # NOTE: previously gated behind `"education" in section or "school" in section or
        # "degree" in section`. Confirmed on Greenhouse forms that `section` is unreliable —
        # scan_fields' getSection() often returns the PREVIOUS field's own label (e.g.
        # section='Phone' for a School/Start-date-year field), not a real "Education"
        # heading, so that gate silently never passed and these handlers never fired. The
        # label keywords below are specific enough to be safe unconditionally.
        if label_match(label, "school", "institution", "university", "college"):
            return EDU[0]["institution_variants"][0] if EDU else ""
        if label_match(label, "major", "field of study", "discipline"):
            return EDU[0]["major_variants"][0] if EDU else ""
        if label_match(label, "gpa", "grade"):
            return str(EDU[0].get("gpa","")) if EDU else ""

        # Education Start/End Month+Year fields — MUST be checked before the generic bare
        # Month/Day/Year fallback below (which would otherwise answer with "today + 2 weeks"
        # instead of the candidate's actual degree dates). Match "start date month", "start
        # date year", etc. — broader than a bare "start year"/"end year" substring, since
        # Greenhouse commonly inserts "date" between the qualifier and the unit
        # ("Start date year" does NOT contain "start year" as a substring).
        _edu_start_field = re.match(r"^\s*start\s+(date\s+)?(month|year)\s*\*?\s*$", label.strip(), re.I)
        _edu_end_field = re.match(r"^\s*end\s+(date\s+)?(month|year)\s*\*?\s*$", label.strip(), re.I)
        if _edu_start_field and EDU:
            if "month" in _edu_start_field.group(2):
                return MONTH_NUM.get(EDU[0].get("start_month", "").lower(), "")
            return str(EDU[0].get("start_year", ""))
        if _edu_end_field and EDU:
            if "month" in _edu_end_field.group(2):
                return MONTH_NUM.get(EDU[0].get("end_month", "").lower(), "")
            return str(EDU[0].get("end_year", ""))

        # Bare Month/Day/Year INPUT WIDGET detector (graduation-date or generic availability-
        # date fields) — require the label (stripped of a trailing "*"/whitespace, and an
        # optional "start"/"end"/"graduation"/"anticipated" prefix) to be ESSENTIALLY JUST
        # the unit word itself ("Month", "Day", "Year", "Anticipated graduation month",
        # etc.), not a bare substring match. A substring test alone false-positives on
        # ordinary prose that happens to contain "day"/"month"/"year" — e.g. "Are you
        # comfortable coming in 5 DAYS a week to the office?" — which would otherwise get a
        # nonsensical day-of-month answer. `section` is NOT used as a signal here: confirmed
        # unreliable on Greenhouse forms (scan_fields' getSection() often returns the
        # PREVIOUS field's own label, not a real heading), so gating on section content is
        # no safer than not gating at all.
        _bare_date_word = re.match(
            r"^\s*(anticipated\s+)?(start\s+|end\s+|graduation\s+)?"
            r"(date\s+)?(month|day|year)s?\s*\*?\s*$",
            label.strip(), re.I)
        if _bare_date_word:
            _grad_ctx = any(k in label.lower() for k in ("graduation", "anticipated")) or \
                        any(k in _context_hint_lower for k in
                            ("graduation", "anticipated graduation", "expected graduation", "anticipated"))
            if _grad_ctx:
                school = next((e for e in EDU if "attending" in e.get("current_status","").lower()), None)
                if school:
                    if label_match(label, "month"): return MONTH_NUM.get(school.get("end_month","").lower(), "")
                    if label_match(label, "day"):   return "01"
                    if label_match(label, "year"):  return str(school.get("end_year",""))
            else:
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

        # Degree Major / Minor — fire even outside an "education" section context
        if label_match(label, "degree major", "degree minor"):
            if label_match(label, "minor"):
                return EDU[0].get("minor","") if EDU else ""
            return EDU[0]["major_variants"][0] if EDU else ""

        # Graduation Year — label-keyed (outside education section context)
        if label_match(label, "graduation year"):
            school = next((e for e in EDU if "attending" in e.get("current_status","").lower()), None)
            if school: return str(school.get("end_year",""))
            return str(EDU[0].get("end_year","")) if EDU else ""

        # Visa sponsorship — textarea variant ("Will you require sponsorship?")
        if label_match(label, "require employment visa sponsorship", "require visa sponsorship",
                       "will you now or in the future require"):
            return "No"

        # Visa type elaboration — "If Yes list type; if No write N/A"
        if label_match(label, "if you answered yes to the previous question",
                       "list the type of visa sponsorship", "if you answered no.*n/a",
                       "if you answered \"no,\" please write"):
            return "N/A"

        # Motorola/bilingual: Canada unrestricted employment auth
        if label_match(label, "unrestricted employment authorization",
                       "autorisation d'emploi sans restriction",
                       "authorization that will allow you to work with any employer in canada"):
            return "Yes"

        # Motorola/bilingual: non-compete or commitments with another employer
        if label_match(label, "commitments or agreements with another employer",
                       "non-compete agreement that might affect",
                       "engagements ou des ententes avec un autre employeur",
                       "restrict the type of work"):
            return "No"

        # Motorola bilingual open-ended textareas
        if label_match(label, "why are you looking for new opportunities",
                       "pour quelles raison"):
            return "Seeking a full-time role in software engineering and data science to apply my ML and data pipeline experience from BILL and my UCSD capstone research."

        if label_match(label, "when are you available to start",
                       "quand êtes-vous disponible"):
            return "Within one month of receiving an offer."

        if label_match(label, "what is the best way to contact you",
                       "quelle est la meilleure façon de vous contacter"):
            return f"Email: {PI.get('email','joshualingli@gmail.com')} | Phone: {PI.get('phone','(480) 616-8194')}"

        if label_match(label, "are you open to relocation",
                       "êtes-vous prêt à déménager"):
            return "Yes — open to relocating anywhere in the United States or Canada."

        # Blue Origin: "why interested in Blue Origin" short-answer
        if label_match(label, "interested in blue origin", "why you are interested in blue origin",
                       "why are you interested in blue origin"):
            return (
                "My current work at BILL involves building production ML pipelines that process "
                "hundreds of thousands of payment events with near-zero tolerance for silent "
                "errors — the kind of engineering discipline that spaceflight demands at a "
                "fundamentally higher level. My UCSD research on scalable graph processing for "
                "chip-design with Qualcomm reinforced that conviction: optimizing systems under "
                "hard resource constraints is the most interesting class of engineering problems. "
                "Blue Origin sits at that intersection of rigor and mission, and I want to apply "
                "the same standard of correctness I've built in production fraud systems to "
                "software that puts people in space."
            )

        # Blue Origin: leadership principle essay
        if label_match(label, "embrace team blue", "earn the trust of others",
                       "practice humility", "leadership principle"):
            return (
                "I resonate most with Earn the Trust of Others. At BILL, I discovered a "
                "portfolio-wide pipeline bug that had silently corrupted data for 411,000 "
                "payment events across a six-month window. The easy path would have been "
                "to quietly fix it and move on; instead I documented the full scope of "
                "impact, presented it to the team, and drove a coordinated remediation so "
                "that the affected fraud-detection rules could be cleanly re-tuned from "
                "accurate baselines. Surfacing an uncomfortable truth proactively — and "
                "owning the downstream work it created — earned trust with stakeholders "
                "and set a standard for how we handle data integrity issues."
            )

        # Blue Origin / technical project description
        if label_match(label, "hands-on technical project", "technical club",
                       "rocketry", "research in which you participated",
                       "your role, contributions and the skills"):
            return (
                "For my UCSD capstone in collaboration with Qualcomm, I built a graph-ML "
                "pipeline to predict chip-design congestion using the DE-HNN architecture. "
                "I converted six production netlists (460k–920k nodes each) into bipartite "
                "graphs and engineered spectral and structural node features in Python with "
                "NetworkX, then trained and optimized DE-HNN models in PyTorch with CUDA. "
                "My contributions spanned the full pipeline: graph construction, feature "
                "engineering, model training, and evaluation. The final system achieved an "
                "89.3% reduction in training runtime and a 38.8% reduction in GPU memory "
                "usage compared to the baseline, at a roughly 6% average performance trade-off "
                "— a result that made the approach viable for large-scale production chip design."
            )

        # Blue Origin: technical skills/experiences for the role
        if label_match(label, "technical skills/experiences", "skills/experiences do you hope",
                       "skills or experiences do you hope", "hope to utilize for this position"):
            return (
                "I want to bring my production ML and data-pipeline experience to high-reliability "
                "software. At BILL I've built end-to-end AWS SageMaker batch pipelines, tuned "
                "optimization algorithms with Optuna, and applied graph analytics with NetworkX at "
                "production scale. My capstone work added PyTorch and CUDA-accelerated graph "
                "processing to that toolkit. I'm proficient in Python, SQL, and Java, with working "
                "knowledge of C and C++. The skills I most want to deepen at Blue Origin are "
                "systems-level reliability engineering and applying the same rigorous correctness "
                "standards I've built in fraud-detection software to mission-critical embedded and "
                "ground-systems contexts."
            )

        # Blue Origin: primary role preference
        if label_match(label, "primary role preference", "role preference"):
            return "Software Development / Backend Systems"

        # "If you responded Yes above, please elaborate below." — honors elaboration
        if label_match(label, "if you responded yes above, please elaborate",
                       "responded yes above"):
            return (
                "I received the Shore Scholarship at UC San Diego, awarded for academic merit. "
                "In high school I earned National AP Scholar recognition, which is granted to "
                "students who score 4 or higher on eight or more AP exams."
            )

        # Tool/equipment experience (CNC, drill press, 3D printer etc.) — check BEFORE
        # the coding-languages rule because "tool experience" label also mentions expertise level
        if label_match(label, "relevant tool experience for this position",
                       "cnc, drill press", "drill press, mill", "3d printer") and not label_match(
                       label, "software", "scripting", "coding"):
            return "No hands-on manufacturing tool experience; primarily software-focused background."

        # Software/coding languages + level of expertise
        if label_match(label, "software and scripting", "coding languages you regularly use",
                       "scripting/coding languages"):
            return (
                "Python (Advanced), C++ (Intermediate), SQL (Intermediate), "
                "Java (Intermediate), Bash (Intermediate), JavaScript (Beginner)"
            )

        # Technical club involvement detail — required even when "not a member" is checked
        if label_match(label, "involved in a technical project or team listed above",
                       "information about your involvement", "dates of involvement"):
            return "N/A — not currently a member of a technical club or project team."

        # Conditional "Other" text box revealed after selecting "Other" in university dropdown.
        # The label is exactly "Other" with no other context — fill with the full institution name.
        if label.strip().lower() == "other" and ftype in ("text", "textarea", ""):
            school = next((e for e in EDU if "attending" in e.get("current_status","").lower()),
                          EDU[0] if EDU else None)
            if school:
                variants = school.get("institution_variants", [])
                return variants[0] if variants else school.get("institution", "")
            return ""

        return None

    # ── Button dropdown / native select ──────────────────────────────────────
    if tag in ("button", "select") or ftype in ("button", "select-one", "select"):
        # Fire deterministic rules even when opts weren't prefetched; exec_button_dropdown
        # will open the dropdown and fuzzy-match the returned string against live options.
        if label_match(label, "letter of interest", "references, and/or additional required"):
            return fuzzy_pick(opts, "No") if opts else "No"

        if not opts:
            return None

        _opts_l_all = [o.lower() for o in opts]
        _is_yes_no = set(o for o in _opts_l_all if o not in ("select one", "")) <= {"yes", "no"}
        if not _is_yes_no and label_match(label, "export control", "arms export", "itar",
                                          "u.s. persons", "citizenship status",
                                          "controlled information"):
            return fuzzy_pick(opts, "U.S. Citizen") or next(
                (o for o in opts if "citizen" in o.lower() and "select" not in o.lower()), None)

        # ── US Citizen / security-clearance questions ────────────────────────────
        # "Are you a U.S. Citizen?" (8 USC 1324b / government contractor access)
        # These labels are often very long; check for key phrase fragments.
        if label_match(label, "are you a u.s. citizen", "are you a us citizen",
                       "1324b", "u.s. citizen", "us citizen"):
            return fuzzy_pick(opts, "Yes") or opts[0]

        # "Would you have the ability to obtain and maintain a security clearance?"
        # Josh has no active clearance but is eligible (US citizen, no disqualifying factors).
        if label_match(label, "obtain and maintain a security clearance",
                       "ability to obtain", "obtain a security clearance"):
            return fuzzy_pick(opts, "Yes") or opts[0]

        # "Do you currently or in the past have held a security clearance?"
        # No active or past clearance.
        if label_match(label, "currently or in the past have held",
                       "have held a security clearance",
                       "currently hold a security clearance",
                       "held a security clearance",
                       "do you currently hold",
                       "do you have an active clearance",
                       "active clearance",
                       "active security clearance"):
            return fuzzy_pick(opts, "No") or opts[0]

        if label_match(label, "18 years of age", "18 years old", "at least 18", "18 years or older",
                       "years of age or older", "at least 18 years", "18 or over", "age 18"):
            return fuzzy_pick(opts, "Yes") or opts[0]

        # "Are you currently in school or recently graduated ... seeking full time employment?"
        if label_match(label, "currently in school or recently graduated",
                       "recently graduated", "seeking full time employment"):
            _is_student = any("attending" in e.get("current_status","").lower() for e in EDU)
            return fuzzy_pick(opts, "Yes" if _is_student else "No") or opts[0]

        # "Are you a former [company] intern?" — No
        if label_match(label, "former", "intern") and label_match(label, "intern"):
            return fuzzy_pick(opts, "No") or opts[0]

        # "university award", "honors upon graduation", "Summa / Magna Cum Laude" — check GPA
        if label_match(label, "university award", "honors upon graduation",
                       "summa", "magna cum laude", "departmental honors", "college honors"):
            school = next((e for e in EDU if "attending" in e.get("current_status","").lower()),
                          EDU[0] if EDU else None)
            if school:
                try:
                    _gpa = float(str(school.get("gpa","0")).replace(",","") or "0")
                except ValueError:
                    _gpa = 0.0
                return fuzzy_pick(opts, "Yes" if _gpa >= 3.5 else "No") or opts[0]
            return fuzzy_pick(opts, "No") or opts[0]

        if label_match(label, "currently employed", "present employer", "may we speak to"):
            for kw in ("do not contact", "don't contact", "cannot contact"):
                m = next((o for o in opts if kw in o.lower()), None)
                if m: return m
            return fuzzy_pick(opts, "Currently employed") or fuzzy_pick(opts, "Not currently employed") or opts[0]

        # "Do you have a Bachelor's degree in CS/STEM or equivalent?" — Yes
        if label_match(label, "bachelor's degree in computer science",
                       "bachelor.*stem", "degree in computer science",
                       "equivalent in education and/or work experience"):
            return fuzzy_pick(opts, "Yes") or opts[0]

        # "Do you have N additional years of software engineering experience?" — No (new grad)
        if label_match(label, "additional years of software engineering",
                       "additional years of experience"):
            return fuzzy_pick(opts, "No") or opts[0]

        # "Do you meet all of the Basic Qualifications for this role?"
        if label_match(label, "basic qualifications", "meet all of the", "minimum qualifications"):
            return fuzzy_pick(opts, "Yes") or opts[0]

        # "Do you meet the Preferred Qualifications?" — "Yes, Some" is safe/honest
        if label_match(label, "preferred qualifications"):
            return fuzzy_pick(opts, "Yes, Some") or fuzzy_pick(opts, "Yes") or opts[0]

        # ── UT Austin / Texas state-agency specific questions ────────────────────
        # "Are you at least 15 years of age?" / "Are you at least 18 years of age?"
        if label_match(label, "at least 15 years", "15 years of age"):
            return fuzzy_pick(opts, "Yes") or opts[0]

        # "Are you authorized to work in the country of hire?" — Yes
        if label_match(label, "authorized to work in the country of hire",
                       "authorized to work in the country"):
            return fuzzy_pick(opts, "Yes") or opts[0]

        # "Have you ever worked at [this university]?" — No
        if label_match(label, "ever worked at university of texas", "previously worked at ut",
                       "previously employed at the university"):
            return fuzzy_pick(opts, "No") or opts[0]

        # "Have you ever been barred from employment at [this university]?" — No
        if label_match(label, "barred from employment", "barred from working"):
            return fuzzy_pick(opts, "No") or opts[0]

        # "I have confirmed to the best of my knowledge that I am eligible for this job." — Yes
        if label_match(label, "confirmed to the best of my knowledge", "eligible for this job",
                       "i have confirmed"):
            return fuzzy_pick(opts, "Yes") or opts[0]

        # Texas foster-child preference (Gov Code 672) — No (not applicable)
        if label_match(label, "conservatorship", "foster", "texas department of family",
                       "dfps", "managing conservatorship"):
            return fuzzy_pick(opts, "No") or opts[0]

        # "Do you certify you are not employed by a foreign-adversary government?" (15 C.F.R. 791.4)
        # Correct answer is Yes (certifying NOT employed by listed country)
        if label_match(label, "791", "governmental entity or political apparatus",
                       "certify that you are not employed by"):
            return fuzzy_pick(opts, "Yes") or opts[0]

        # "Can you confirm you meet the minimum qualifications for critical infrastructure?" — Yes
        if label_match(label, "critical infrastructure", "security and integrity of this infrastructure"):
            return fuzzy_pick(opts, "Yes") or opts[0]

        # "Are you willing to comply with the university's ethics policy?" (gifts/foreign adversaries) — Yes
        if label_match(label, "ethics policy", "prohibits accepting gifts",
                       "gifts or travel from entities associated with foreign"):
            return fuzzy_pick(opts, "Yes") or opts[0]

        # "Do you agree to notify the university of any future personal travel to a foreign-adversary nation?" — Yes
        if label_match(label, "notify the university", "post-travel briefing",
                       "foreign-adversary nation", "future personal travel"):
            return fuzzy_pick(opts, "Yes") or opts[0]

        # "Are you a current student or recent graduate?"
        if label_match(label, "current student", "recent graduate"):
            _is_student = any("attending" in e.get("current_status","").lower() for e in EDU)
            return fuzzy_pick(opts, "Yes" if _is_student else "No") or opts[0]

        # GPA dropdown (range buckets like "3.5 - 4.0") — must be checked BEFORE the
        # university rule because Capital One's GPA label contains "University/College"
        if label_match(label, "cumulative gpa", "current gpa", "gpa scale", "cgpa"):
            school = next((e for e in EDU if "attending" in e.get("current_status","").lower()),
                          EDU[0] if EDU else None)
            if school:
                try:
                    _gpa = float(str(school.get("gpa","0")).replace(",","") or "0")
                except ValueError:
                    _gpa = 0.0
                if _gpa > 0 and opts:
                    def _gpa_score(opt):
                        nums = [float(n) for n in re.findall(r'[\d.]+', opt)]
                        if len(nums) >= 2:
                            lo, hi = nums[0], nums[1]
                            if lo <= _gpa <= hi: return 0
                            return abs(_gpa - (lo + hi) / 2)
                        return 9999
                    non_sel = [o for o in opts if o.lower() not in ("select one","")]
                    if non_sel: return min(non_sel, key=_gpa_score)
            return None

        # "What university/college are you currently enrolled in or recently graduated from?"
        if label_match(label, "university/college", "university or college",
                       "currently enrolled in", "recently graduated from"):
            for e in EDU:
                for variant in e.get("institution_variants", []):
                    hit = fuzzy_pick(opts, variant)
                    if hit: return hit
            return fuzzy_pick(opts, "Other") or None  # ASU/UCSD not in Canadian school lists

        # "Graduation Semester:" — map EDU end_month to Fall/Spring/Summer
        if label_match(label, "graduation semester"):
            school = next((e for e in EDU if "attending" in e.get("current_status","").lower()), None)
            if school:
                _em = (school.get("end_month","") or "").lower()
                _mn = int(MONTH_NUM.get(_em, "0"))
                if 9 <= _mn <= 12:  return fuzzy_pick(opts, "Fall") or opts[0]
                if 1 <= _mn <= 6:   return fuzzy_pick(opts, "Spring") or opts[0]
                if 7 <= _mn <= 8:   return fuzzy_pick(opts, "Summer") or opts[0]
            return None

        # Indigenous identity (Canada EEO) — decline
        if label_match(label, "indigenous person", "treaty indian", "métis", "first nation",
                       "inuit", "north american indian"):
            return fuzzy_pick(opts, "Prefer Not to Disclose") or fuzzy_pick(opts, "No") or opts[0]

        # "Do you currently/previously work(ed) at [this company]?" — pick the "No, never" option.
        # Capital One uses a long multi-part option with zero-width spaces; fuzzy_pick on "No" finds it.
        if label_match(label, "previously, worked at", "worked at capital one",
                       "company acquired by capital one", "previously worked for",
                       "prior employment at this company"):
            # Prefer an option that starts with "No" or contains "never"
            no_opt = next((o for o in opts
                           if re.search(r'\bno\b', o, re.IGNORECASE)
                           and not re.search(r'\byes\b', o, re.IGNORECASE)), None)
            return no_opt or fuzzy_pick(opts, "No") or opts[0]

        # Senior Government Official questions — always No
        if label_match(label, "senior government official", "government official"):
            return fuzzy_pick(opts, "No") or opts[0]

        # Ernst & Young / specific accounting firm employment
        if label_match(label, "ernst & young", "ernst and young", "accounting firm"):
            return fuzzy_pick(opts, "No") or opts[0]

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

        # ── Graduation-year / currently-graduating questions (GM-style) ──────────
        # "Are you graduating this year?" — Yes if any current edu ends this calendar year.
        if label_match(label, "graduating this year"):
            import datetime as _dt
            cur_year = str(_dt.date.today().year)
            for e in EDU:
                if str(e.get("end_year", "")) == cur_year:
                    return fuzzy_pick(opts, "Yes") or opts[0]
            return fuzzy_pick(opts, "No") or opts[0]

        # "Do you have a Bachelor's/Master's degree in CS/EE/related?" — Yes/No from EDU.
        if label_match(label, "bachelors degree", "bachelor's degree",
                       "baccalaureate") and label_match(label, "computer science",
                       "information system", "information technology"):
            _has_bs = any("bachelor" in e.get("degree_type","").lower() for e in EDU)
            return fuzzy_pick(opts, "Yes" if _has_bs else "No") or opts[0]

        if label_match(label, "master's degree", "masters degree", "master of science") \
                and label_match(label, "computer science", "computer engineering",
                                "electrical engineering", "related field"):
            _has_ms = any("master" in e.get("degree_type","").lower() for e in EDU)
            return fuzzy_pick(opts, "Yes" if _has_ms else "No") or opts[0]

        # Graduation-date dropdown (range buckets like "January-March 2025").
        # Fires when options contain year numbers — pick the bucket matching the relevant edu.
        _opts_have_years = any(re.search(r'\b20\d{2}\b', o) for o in opts)
        if _opts_have_years and label_match(label, "expected graduation date",
                                            "graduation date", "anticipated graduation"):
            # Determine which edu entry to use: bachelor's or graduate.
            _want_grad = label_match(label, "graduate degree", "master", "mba", "phd",
                                     "juris", "graduate school")
            if _want_grad:
                _edu_e = next((e for e in EDU if "master" in e.get("degree_type","").lower()
                               or "phd" in e.get("degree_type","").lower()), None)
            else:
                _edu_e = next((e for e in EDU if "bachelor" in e.get("degree_type","").lower()), None)
            if _edu_e:
                _ey = str(_edu_e.get("end_year",""))
                _em = (_edu_e.get("end_month","") or "").lower()
                _month_n = MONTH_NUM.get(_em, "00")
                _mn = int(_month_n) if _month_n.isdigit() else 0
                # Build quarter label to fuzzy-match: "January-March 2025", "April-June 2025", etc.
                if 1 <= _mn <= 3:   _qrange = f"January-March {_ey}"
                elif 4 <= _mn <= 6: _qrange = f"April-June {_ey}"
                elif 7 <= _mn <= 9: _qrange = f"July-September {_ey}"
                else:               _qrange = f"October-December {_ey}"
                hit = fuzzy_pick(opts, _qrange)
                if hit:
                    return hit
                # Fallback: any option containing the year
                hit = next((o for o in opts if _ey in o), None)
                if hit: return hit

        # Postgraduate intent — "Do you intend to ENROLL in a postgraduate degree?"
        # Josh is already in/completing his terminal Master's program, so he will NOT newly
        # enroll in another postgraduate degree → always "No".
        # Guard: only fire when options look like Yes/No (not graduation-date ranges).
        if label_match(label, "postgraduate", "intend to enroll",
                       "graduate school") and not _opts_have_years:
            return fuzzy_pick(opts, "No") or (opts[-1] if opts else None)
        # "pursuing a degree" / "graduate degree" — only fire for Yes/No options (not date ranges).
        if label_match(label, "pursuing a degree", "graduate degree") \
                and label_match(label, "intend", "plan to", "do you intend") \
                and not _opts_have_years:
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
            _non_sel = [o for o in opts if o.lower() not in ("select one", "")]
            # "highest level of education completed" → use highest *completed* degree (skip in-progress)
            _completed = [e for e in EDU if "attending" not in e.get("current_status","").lower()
                          and "pursuing" not in e.get("current_status","").lower()]
            _edu_list = _completed if _completed else EDU
            for edu in _edu_list:
                abbrev = edu.get("degree_abbreviation","")
                if abbrev:
                    hit = fuzzy_pick(opts, abbrev)
                    if hit: return hit
                for variant in edu.get("degree_search_variants",[]):
                    hit = fuzzy_pick(opts, variant)
                    if hit: return hit
                hit = fuzzy_pick(opts, edu.get("degree_type",""))
                if hit: return hit
            # Fallback: pick highest non-placeholder option (last in typical edu-level list)
            return _non_sel[-1] if _non_sel else None

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
