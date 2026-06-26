"""
app_workday_v3.py  —  Workday Application Bot
==============================================
Fills and submits Workday job applications (*.myworkdayjobs.com).

Architecture:
  1. Navigate to job listing URL
  2. For each page: scan ALL visible fields with labels + options
  3. Primary:  send batch to DeepSeek → [{index, value}]   (requires DEEPSEEK_API_KEY in .env)
     Fallback: label-matching rules against library.json   (no API needed)
  4. Execute answers field-by-field — no hardcoded field IDs
  5. Stop at Review page; waits for [Enter] before submitting

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 USAGE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  # Headless (default, no browser window):
  python3 agent_test/app_workday_v3.py "JOB_URL"

  # Headed / displayed mode (shows Chrome window for manual review/edits):
  python3 agent_test/app_workday_v3.py "JOB_URL" --show

  # Log to file — Mac/Linux:
  python3 -u agent_test/app_workday_v3.py "JOB_URL" > run.txt 2>&1 &
  tail -f run.txt

  # Log to file — Windows (PowerShell):
  Start-Process python -ArgumentList "-u agent_test/app_workday_v3.py `"JOB_URL`"" -RedirectStandardOutput run.txt -NoNewWindow
  Get-Content run.txt -Wait

  # Screenshots saved to agent_test/artifacts/ on errors or stuck pages.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 FLAGS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  JOB_URL   Workday listing URL (positional, required)
  --show    Launch a visible Chrome window so you can watch/intervene.
            The bot pauses at Review anyway — use this when you want to
            manually correct fields before submitting.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 CONFIG
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  agent_test/.env            DEEPSEEK_API_KEY=sk-...   (optional)
  agent_test/library.json    Candidate profile, resume path, preferences
"""

import asyncio, json, os, re, sys
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from playwright.async_api import async_playwright, Page

SCRIPT_DIR  = Path(__file__).parent
load_dotenv(SCRIPT_DIR / ".env")

# ── Chrome path: auto-detect by platform ─────────────────────────────────────
def _find_chrome() -> str:
    import platform
    system = platform.system()
    candidates = []
    if system == "Darwin":
        candidates = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    elif system == "Windows":
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        ]
    else:  # Linux
        candidates = ["/usr/bin/google-chrome", "/usr/bin/chromium-browser",
                      "/usr/bin/chromium", "/snap/bin/chromium"]
    for p in candidates:
        if os.path.exists(p):
            return p
    # Last resort: let Playwright use its own bundled Chromium
    return ""

LIBRARY     = json.loads((SCRIPT_DIR / "library.json").read_text())
PI          = LIBRARY["personal_info"]
WE          = LIBRARY.get("work_experience", [])
EDU         = LIBRARY.get("education_history", [])
LANG        = LIBRARY.get("languages", [])

# Resume path: from library.json (relative to repo root) or auto-discover
# Always resolve to absolute path — set_input_files requires absolute paths
_resume_rel = LIBRARY.get("resume_path", "")
if _resume_rel:
    _rp = Path(_resume_rel) if Path(_resume_rel).is_absolute() else SCRIPT_DIR.parent / _resume_rel
    RESUME_PATH = str(_rp.resolve())
else:
    _pdfs = list(SCRIPT_DIR.glob("*.pdf"))
    RESUME_PATH = str(_pdfs[0].resolve()) if _pdfs else ""

# User agent: match the actual platform so Workday renders the platform-correct version
import platform as _plat
_ua_os = {
    "Darwin":  "Macintosh; Intel Mac OS X 10_15_7",
    "Windows": "Windows NT 10.0; Win64; x64",
    "Linux":   "X11; Linux x86_64",
}.get(_plat.system(), "Windows NT 10.0; Win64; x64")
USER_AGENT = (f"Mozilla/5.0 ({_ua_os}) AppleWebKit/537.36 "
              f"(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

CHROME_PATH = _find_chrome()
EMAIL       = PI["email"]
PASSWORD    = PI["password"]
ARTIFACTS   = SCRIPT_DIR / "artifacts"
ARTIFACTS.mkdir(exist_ok=True)

DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY", "")

DEFAULT_JOB_URL = (
    "https://truist.wd1.myworkdayjobs.com/en-US/Careers/job/Raleigh-NC/"
    "Java-Software-Engineer-I---Full-Stack----Financial-Crimes_R0115229"
)

# ── Profile summary for DeepSeek ─────────────────────────────────────────────
PROFILE_SUMMARY = json.dumps({
    "personal_info": {k: v for k, v in PI.items() if k != "password"},
    "work_experience": WE,
    "education_history": EDU,
    "skills": LIBRARY.get("skills", []),
    "languages": LIBRARY.get("languages", []),
    "role_preferences":   LIBRARY.get("role_preferences", {}),
    "compensation_rules": LIBRARY.get("compensation_rules", {}),
    "regulatory_self_identification": LIBRARY.get("regulatory_self_identification", {}),
    "job_board_mappings": LIBRARY.get("job_board_mappings", {}),
}, indent=2)

# ── DeepSeek batch resolver ───────────────────────────────────────────────────

SYSTEM_PROMPT = """You are an AI filling out a job application form for a candidate.
Given a list of form fields (with labels, types, and available options), return the best answer for EVERY fillable field.

Rules:
- Use candidate profile to answer accurately and honestly.
- "First Name" / "Last Name" / "Address" / "City" / "State" / "Zip" / "Phone" → use personal_info.
- "How did you hear" → "LinkedIn" or closest available option.
- Visa/sponsorship → use job_board_mappings.requires_visa_sponsorship.
- Salary/compensation → use compensation_rules.expected_base_salary.
- EEO fields (gender, race, ethnicity, veteran, disability) → always pick "prefer not to answer" / "decline" option.
- Age 18+ / authorized to work → "Yes".
- Non-compete / prior employment at this company → "No" unless profile says otherwise.
- Open-ended text → concise honest answer from profile.
- Skills fields → use the skills array; pick the closest matching option from available choices.
- Language fields → use the languages array for language name and proficiency level.
- If filling a specific Work Experience / Education / Language entry, an "Entry Context" block
  will be provided — use ONLY that entry's data for fields in that dialog, not other entries.
- For selectinput/button-dropdown fields, your value must EXACTLY match one of the provided options.
- Skip nav buttons, already-filled fields, fields with no relevant data.

Respond ONLY with valid JSON: {"answers": [{"index": <int>, "value": "<string>"}]}
Only include fields you have an answer for."""

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


async def deepseek_fill_page(fields: list[dict],
                             entry: dict = None,
                             section_type: str = "") -> list[dict]:
    """Send page/dialog fields to DeepSeek. Returns [{index, value}] or [] on error/no key.

    entry + section_type are passed for add-dialog calls so the LLM knows exactly which
    Work Experience / Education / Language entry it is currently filling.
    """
    if not DEEPSEEK_KEY:
        return []
    field_lines = []
    for f in fields:
        line = f"[{f['index']}] type={f['type']} label={f['label']!r}"
        if f.get("options"): line += f" options={f['options']}"
        if f.get("value"):   line += f" current={f['value']!r}"
        if f.get("section"): line += f" section={f['section']!r}"
        field_lines.append(line)

    # Build entry context block so LLM knows which specific entry it's filling
    entry_context = ""
    if entry and section_type:
        entry_context = (f"\nEntry Context (you are filling ONE {section_type} entry — "
                         f"use ONLY this data for these fields):\n"
                         f"{json.dumps(entry, indent=2)}\n")

    prompt = (f"Candidate Profile:\n{PROFILE_SUMMARY}\n"
              f"{entry_context}"
              f"\nForm Fields (page: {fields[0].get('page_heading','') if fields else ''}):\n"
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


# ── Label-matching fallback fill engine ───────────────────────────────────────
# Used when DeepSeek is unavailable. Matches field labels → profile values.

COMP = LIBRARY.get("compensation_rules", {})
REG  = LIBRARY.get("regulatory_self_identification", {})
JBM  = LIBRARY.get("job_board_mappings", {})
PHONE_DIGITS = "".join(c for c in PI.get("phone","") if c.isdigit())
# Normalize sponsorship flag: "No"/"false"/False/0 → False; anything else → True
_sponsor_raw = JBM.get("requires_visa_sponsorship", "No")
NEEDS_SPONSOR = str(_sponsor_raw).lower() not in ("no", "false", "0", "")
# Disability answer from library (default "yes" per CC-305 self-ID guidance)
DISABILITY_ANSWER = str(REG.get("disability_answer", "yes")).lower()

def label_match(label: str, *keywords) -> bool:
    l = label.lower()
    return any(k in l for k in keywords)

DECLINE_KEYWORDS = ["not wish","don't wish","prefer not","decline","choose not",
                    "not wish to self","no wish","i do not wish"]

def pick_decline(opts: list[str]) -> str | None:
    for o in opts:
        if any(k in o.lower() for k in DECLINE_KEYWORDS):
            return o
    return None

def fuzzy_pick(opts: list[str], value: str) -> str | None:
    """Fuzzy match value against options list: exact → starts → contains."""
    vl = value.lower().strip()
    for strategy in [
        lambda o: o.lower() == vl,
        lambda o: o.lower().startswith(vl) or vl.startswith(o.lower()),
        lambda o: vl in o.lower() or o.lower() in vl,
    ]:
        m = next((o for o in opts if strategy(o)), None)
        if m: return m
    return None

def rule_based_answer(field: dict, context_hint: str = "") -> str | None:
    """
    Given a scanned field, return the best value from library.json
    purely by label inspection. Returns None if no match.
    """
    label   = field.get("label", "")
    opts    = field.get("options", [])
    section = (field.get("section") or field.get("page_heading") or context_hint).lower()
    tag     = field.get("tag", "")
    ftype   = field.get("type", "")
    current = field.get("value", "")

    # Skip if already filled meaningfully (but 'false' = unchecked checkbox, must not skip)
    if current and current.lower() not in ("select one", "", "false", "unchecked"):
        return None

    # ── Radio buttons — check BEFORE generic input handling ──────────────────
    if field.get("role") == "radio" or ftype == "radio":
        opts = field.get("options", [])
        if not opts:
            # No scanned options — infer Yes/No from name/label
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
        # Default generic Yes/No → No (safer)
        return fuzzy_pick(opts, "No") or opts[0]

    # ── Checkbox — check BEFORE generic text/input block ────────────────────
    if ftype == "checkbox" or field.get("role") == "checkbox":
        if label_match(label, "consent", "agree", "terms", "condition", "certify"):
            return "true"
        if label_match(label, "preferred name"):
            return "false"
        # Disability status radio-group (Self Identify page) — driven by library.json disability_answer
        if label_match(label, "disability"):
            ll = label.lower()
            if DISABILITY_ANSWER == "yes":
                # "Yes, I have a disability" — must contain "yes" (not just "have a disability"
                # which is also a substring of "No, I do NOT have a disability")
                if "yes" in ll and "disability" in ll:
                    return "true"
                return "false"
            else:
                # Select "No, I do not have a disability" or "prefer not to answer"
                if "no" in ll and "disability" in ll:
                    return "true"
                if any(k in ll for k in ("don't wish", "don't want to answer", "prefer not", "decline to", "do not want", "i do not want")):
                    return "true"
                return "false"
        return None

    # ── Text / textarea / selectinput fields ─────────────────────────────────
    if tag in ("input", "textarea") or ftype in ("text","email","tel","number"):

        # selectinput (search-dropdown) — treat like a dropdown value
        if field.get("isSelectInput"):
            if label_match(label, "how did you hear", "source", "referral", "learn about"):
                return "LinkedIn"  # type → Enter → auto-fills as single result pill
            if label_match(label, "country") and not label_match(label, "phone"):
                return "United States of America"
            if label_match(label, "state", "province"):
                if "united" not in label.lower() and len(label) < 50:
                    return PI["state"]
            return None

        if label_match(label, "first name", "given name"):
            return PI["first_name"]
        if label_match(label, "last name", "surname", "family name"):
            return PI["last_name"]
        if label_match(label, "middle name", "middle initial"):
            return ""  # skip
        if label_match(label, "email"):
            return PI["email"]
        if label_match(label, "phone number", "telephone", "cell number", "mobile number"):
            if label_match(label, "extension", "ext"):
                return ""
            return PHONE_DIGITS
        if label_match(label, "address line 1", "street address", "address 1"):
            return PI["address"]
        if label_match(label, "address line 2", "apt", "suite", "unit"):
            return ""
        if label_match(label, "city", "town"):
            return PI["city"]
        if label_match(label, "zip", "postal"):
            return PI["zip_code"]
        if label_match(label, "linkedin"):
            return PI.get("linkedin", "")
        if label_match(label, "github", "portfolio", "website", "url"):
            return PI.get("github", "")
        if label_match(label, "salary", "compensation", "pay", "wage", "expected"):
            return str(COMP.get("expected_base_salary", "120000"))

        # Availability / eligibility date fields (Month/Day/Year in Application Questions)
        # Exclude Self Identify page — those month/day/year fields are signature dates
        # already filled by handle_self_identify; returning None skips re-filling them.
        if label_match(label, "month", "day", "year") and (
            "application" in section or not any(k in section for k in ("work","experience","education","school","self identify","signature"))
        ):
            import datetime
            avail = datetime.date.today() + datetime.timedelta(weeks=2)
            if label_match(label, "month"):
                return str(avail.month).zfill(2)
            if label_match(label, "day"):
                return str(avail.day).zfill(2)
            if label_match(label, "year"):
                return str(avail.year)

        if label_match(label, "name") and "employee" not in label.lower():
            # Generic name field — use full name
            return f"{PI['first_name']} {PI['last_name']}"

        # Work experience dialog fields
        if "work" in section or "experience" in section or "employment" in section:
            if label_match(label, "company", "employer", "organization"):
                return WE[0]["company"] if WE else ""
            if label_match(label, "title", "position", "role"):
                return WE[0]["role"] if WE else ""
            if label_match(label, "city", "location"):
                return WE[0].get("city","") if WE else ""
            if label_match(label, "description", "responsibilities", "summary"):
                return (WE[0].get("description","")[:500]) if WE else ""
            if label_match(label, "start year"):
                return WE[0].get("start_year","") if WE else ""
            if label_match(label, "end year"):
                return WE[0].get("end_year","") if WE else ""

        # Education dialog fields
        if "education" in section or "school" in section or "degree" in section:
            if label_match(label, "school", "institution", "university", "college"):
                return EDU[0]["institution_variants"][0] if EDU else ""
            if label_match(label, "major", "field of study", "discipline"):
                return EDU[0]["major_variants"][0] if EDU else ""
            if label_match(label, "gpa", "grade"):
                return str(EDU[0].get("gpa","")) if EDU else ""
            if label_match(label, "start year"):
                return EDU[0].get("start_year","") if EDU else ""
            if label_match(label, "end year","graduation year"):
                return EDU[0].get("end_year","") if EDU else ""

        return None

    # ── Button dropdowns / select ─────────────────────────────────────────────
    if tag in ("button", "select") or ftype == "select-one":
        if not opts:
            return None

        # EEO / demographic fields → try decline option; if none, leave blank
        if label_match(label, "gender", "sex", "race", "ethnicity", "hispanic",
                       "latino", "veteran", "disability"):
            decline = pick_decline(opts)
            if decline:
                return decline
            # For veteran status, prefer "not a protected veteran" option
            if label_match(label, "veteran"):
                for kw in ["not a protected veteran", "not a veteran", "i am not"]:
                    m = next((o for o in opts if kw in o.lower()), None)
                    if m: return m
            return None  # leave blank — no decline option available

        if label_match(label, "how did you hear", "source", "referral", "learn about"):
            return fuzzy_pick(opts, "Website") or fuzzy_pick(opts, "Job Board") or fuzzy_pick(opts, "LinkedIn") or opts[0]

        if label_match(label, "country") and not label_match(label, "phone"):
            return fuzzy_pick(opts, "United States") or fuzzy_pick(opts, "USA") or opts[0]

        if label_match(label, "state", "province", "region") and not label_match(label, "country") \
                and "united" not in label.lower() and len(label) < 50:
            return fuzzy_pick(opts, PI["state"]) or opts[0]

        if label_match(label, "phone", "device type", "phone type"):
            return fuzzy_pick(opts, "Mobile") or opts[0]

        if label_match(label, "suffix", "salutation", "prefix"):
            return ""  # skip

        # Sponsorship / visa
        if label_match(label, "sponsor", "visa", "work authorization"):
            want = "No" if not NEEDS_SPONSOR else "Yes"
            return fuzzy_pick(opts, want) or opts[0]

        # Yes/No compliance questions
        if label_match(label, "legally authorized", "authorized to work", "18", "legal age", "eligible to work"):
            return fuzzy_pick(opts, "Yes") or opts[0]

        if label_match(label, "non-disclosure", "non-compete", "non compete", "agreement",
                       "previously worked", "prior employ", "work for us before",
                       "worked here", "worked for", "restrict"):
            return fuzzy_pick(opts, "No") or opts[0]

        # Location-specific compliance (Maryland/Massachusetts type questions)
        if label_match(label, "located in", "applying for employment within", "maryland", "massachusetts",
                       "california", "colorado", "new york"):
            return fuzzy_pick(opts, "No") or opts[0]

        # Eligibility / start date
        if label_match(label, "eligibil", "start date", "available", "when can you start"):
            return fuzzy_pick(opts, "Immediately") or fuzzy_pick(opts, "2 weeks") or opts[0]

        # Desired compensation
        if label_match(label, "compensation", "salary", "pay", "desired"):
            comp = COMP.get("expected_base_salary", "")
            if comp and opts:
                best = fuzzy_pick(opts, str(comp))
                if not best:
                    best = opts[len(opts)//2] if len(opts) > 1 else opts[0]
                return best
            return fuzzy_pick(opts, "$75,000") or fuzzy_pick(opts, "$50,000") or opts[0]

        if label_match(label, "relocat"):
            return fuzzy_pick(opts, "Yes") or opts[0]

        # Language
        if label_match(label, "language"):
            return fuzzy_pick(opts, "English") or opts[0]

        # Degree type — try abbreviation first (MS/BS), then full name
        if label_match(label, "degree", "education level", "highest"):
            if EDU:
                edu = EDU[0]
                # Try abbreviation (MS, BS) first for better dropdown matching
                abbrev = edu.get("degree_abbreviation", "")
                if abbrev:
                    hit = fuzzy_pick(opts, abbrev)
                    if hit: return hit
                # Try search variants
                for variant in edu.get("degree_search_variants", []):
                    hit = fuzzy_pick(opts, variant)
                    if hit: return hit
                return fuzzy_pick(opts, edu.get("degree_type","")) or opts[0]

        # Start/end month for work/education
        if label_match(label, "start month"):
            data = WE[0] if ("work" in section or "experience" in section) else (EDU[0] if EDU else None)
            if data: return fuzzy_pick(opts, data.get("start_month","")) or opts[0]

        if label_match(label, "end month"):
            data = WE[0] if ("work" in section or "experience" in section) else (EDU[0] if EDU else None)
            if data: return fuzzy_pick(opts, data.get("end_month","")) or opts[0]

        # Currently working here checkbox-style dropdown
        if label_match(label, "current", "present", "still work", "still attend"):
            data = WE[0] if ("work" in section or "experience" in section) else (EDU[0] if EDU else None)
            if data:
                is_current = data.get("current_status","").lower() in ("currently work here","still attending")
                return fuzzy_pick(opts, "Yes" if is_current else "No") or opts[0]

        return None

    # ── (checkbox handled above before text block) ────────────────────────────

    return None


async def rule_based_fill_page(fields: list[dict], context_hint: str = "") -> list[dict]:
    """Apply rule-based matching to all fields. Returns [{index, value}]."""
    answers = []
    for f in fields:
        val = rule_based_answer(f, context_hint)
        if val is not None and val != "":
            answers.append({"index": f["index"], "value": val})
    return answers


# ── DOM scanner ───────────────────────────────────────────────────────────────

SCAN_JS = r"""(rootSel) => {
    const root = (rootSel && document.querySelector(rootSel)) || document;
    const isVis = el => {
        const s = window.getComputedStyle(el);
        if (s.display==='none'||s.visibility==='hidden'||s.opacity==='0') return false;
        const r = el.getBoundingClientRect();
        return r.width>0 && r.height>0;
    };
    const getLabel = el => {
        // 1) aria-labelledby (check for real text, not UUIDs)
        const lblBy = el.getAttribute('aria-labelledby');
        if (lblBy) {
            const parts = lblBy.split(' ').map(id => {
                const t = document.getElementById(id);
                return t ? t.innerText.trim() : '';
            }).filter(t => t && !/^select one/i.test(t) && !/^[0-9a-f]{20,}$/i.test(t));
            if (parts.length) return parts.join(' ').replace(/\*$/, '').trim();
        }
        // 2) aria-label (skip generic placeholders and UUIDs, but strip leading whitespace)
        let t = (el.getAttribute('aria-label')||'').trim();
        const isGeneric = /^\d+$/.test(t) || /^select one/i.test(t) || /^required$/i.test(t)
                          || /^[0-9a-f]{20,}$/i.test(t);
        if (t && !isGeneric) return t;
        // 3) label[for=id] — individual label (prioritized for radio/checkbox to distinguish options)
        if (el.id) {
            const l = document.querySelector(`label[for="${el.id}"]`);
            if (l) {
                const lt = l.innerText.trim();
                if (lt && !/^[0-9a-f]{20,}$/i.test(lt) && !/^select one/i.test(lt)) return lt;
            }
        }
        // 3b) For radio/checkbox: check closest ancestor label (input nested inside label)
        if (el.type === 'radio' || el.type === 'checkbox') {
            const closestLbl = el.closest('label');
            if (closestLbl) {
                const lt = closestLbl.innerText.trim().replace(/\*$/, '').trim();
                if (lt && !/^[0-9a-f]{20,}$/i.test(lt) && !/^select one/i.test(lt)) return lt;
            }
        }
        // 4) Closest fieldset — question text is first line (Application Questions pattern)
        const fieldset = el.closest('fieldset');
        if (fieldset) {
            const firstLine = fieldset.innerText.split('\n')[0].trim().replace(/\*$/, '').trim();
            if (firstLine && !/^select one/i.test(firstLine) && !/^[0-9a-f]{20,}$/i.test(firstLine))
                return firstLine;
        }
        // 5) formField context (prefix match for "formField-XXXX" and exact "formField")
        const fw = el.closest('[data-automation-id^="formField"]') ||
                   el.closest('[data-uxi-widget-type="formField"]');
        if (fw) {
            for (const sel of [
                '[data-automation-id="questionText"] p',
                '[data-automation-id="questionText"]',
                '[data-automation-id="formLabel"]',
                'label', 'legend'
            ]) {
                const l = fw.querySelector(sel);
                if (l) {
                    const lt = l.innerText.trim().replace(/\*$/, '').trim();
                    if (lt && !/^[0-9a-f]{20,}$/i.test(lt) && !/^select one/i.test(lt)) return lt;
                }
            }
        }
        return (el.placeholder||el.name||'').trim();
    };
    const getSection = el => {
        let node = el.parentElement;
        while (node && node !== document.body) {
            const h = node.querySelector('h3,h4,[data-automation-id="sectionTitle"],[data-automation-id="groupTitle"]');
            if (h) return h.innerText.trim();
            node = node.parentElement;
        }
        return '';
    };
    const SKIP = new Set(['beecatcher','click_filter','utilityMenuButton','backToJobPosting',
        'navigationItem-Search for Jobs','navigationItem-Candidate Home','navigationItem-Careers Home',
        'pageFooterBackButton','selectedItem','selectedItemList','menuItem','promptIcon']);
    const results = []; let idx = 0;
    root.querySelectorAll(
        'input:not([type=hidden]):not([type=submit]):not([type=file]),' +
        'textarea,select,button:not([aria-hidden="true"]),' +
        '[role="combobox"],[data-uxi-widget-type="selectinput"],' +
        'input[type="checkbox"],[role="checkbox"],' +
        '[role="radio"],input[type="radio"]'
    ).forEach(el => {
        // Native checkboxes and radios are often CSS-hidden (opacity:0/size:0) for custom styling
        // but their parent wrapper IS visible. Skip isVis for these and check parent instead.
        const isRadio = el.getAttribute('role') === 'radio' || el.getAttribute('type') === 'radio';
        const isCheckbox = el.getAttribute('role') === 'checkbox' || el.getAttribute('type') === 'checkbox';
        if (!isRadio && !isCheckbox && !isVis(el)) return;
        if (isRadio || isCheckbox) {
            // Only include if the parent wrapper is actually visible on screen
            const parent = el.parentElement?.parentElement || el.parentElement;
            if (!parent) return;
            const pr = parent.getBoundingClientRect();
            if (pr.height === 0) return;
        }
        const auto = el.getAttribute('data-automation-id')||'';
        if (SKIP.has(auto)) return;
        const tag = el.tagName.toLowerCase();
        const type = el.getAttribute('type')||tag;
        if (tag==='button') {
            const txt=(el.innerText||'').trim().toLowerCase();
            if (!txt||/save|next|back|submit|continue|cancel/.test(txt)) return;
            // Allow buttons with id, data-automation-id, OR inside a formField wrapper with a label
            // (Language NAME dropdown lacks id and aid but is inside a formField)
            const aid = el.getAttribute('data-automation-id')||'';
            if (!el.id && !aid) {
                const fw = el.closest('[data-automation-id^="formField"]');
                if (!fw || !fw.querySelector('[data-automation-id="formLabel"],label')) return;
            }
        }
        const label = getLabel(el);
        let options = [];
        if (tag==='select') options = Array.from(el.options).map(o=>o.text.trim()).filter(Boolean);

        // Detect if this input is inside a Workday selectinput widget
        // NOTE: On some tenants (RH), the <input> itself has data-uxi-widget-type="selectinput"
        const inSelectInput = !!(
            el.getAttribute('data-uxi-widget-type') === 'selectinput' ||
            el.closest('[data-uxi-widget-type="selectinput"]')
        );

        // For selectinput: collect available options from the listbox if already open,
        // and fix label lookup — the input IS the widget, so check aria-describedby paragraph for label
        if (inSelectInput && !label) {
            const descId = el.getAttribute('aria-describedby');
            if (descId) {
                const desc = document.getElementById(descId);
                if (desc) label = desc.innerText.trim().replace(/\*$/,'').trim();
            }
            // Also try parent formField
            const fw2 = el.closest('[data-automation-id="formField"]') ||
                        el.parentElement?.closest('[data-automation-id]');
            if (!label && fw2) {
                const lbl2 = fw2.querySelector('[data-automation-id="formLabel"],label');
                if (lbl2) label = lbl2.innerText.trim().replace(/\*$/,'').trim();
            }
        }

        // For radio buttons — only process the FIRST one in each group and collect all siblings
        if (el.getAttribute('role') === 'radio' || el.getAttribute('type') === 'radio') {
            // Match formField or formField-* (RH uses formField-candidateIsPreviousWorker etc.)
            const fw = el.closest('[data-automation-id^="formField"]') ||
                       el.closest('[data-uxi-widget-type="formField"]') ||
                       el.closest('[data-automation-id]') ||
                       el.parentElement?.parentElement;
            if (!fw) return;
            // Skip if we already tagged this group
            const siblings = Array.from(fw.querySelectorAll('[role="radio"],input[type="radio"]'));
            if (siblings.some(r => r !== el && r.hasAttribute('data-fill-idx'))) return;
            if (siblings[0] !== el) return;
            // Get option labels — for input[type=radio], use the span/div sibling text
            const radioOpts = siblings.map(r => {
                const lbl = r.getAttribute('aria-label');
                if (lbl) return lbl.trim();
                // Walk up to find the label text next to this radio
                const parent = r.parentElement;
                const text = parent?.innerText?.trim() || '';
                // Also check value attribute as fallback mapping
                const val = r.getAttribute('value');
                if (text) return text;
                if (val === 'true') return 'Yes';
                if (val === 'false') return 'No';
                return val || '';
            }).filter(Boolean);
            const radioValues = siblings.map(r => r.getAttribute('value') || '');
            // For label: try multiple strategies
            let groupLabel = '';
            // 1. Standard formLabel child
            const fwLabel = fw.querySelector('[data-automation-id="formLabel"],label');
            if (fwLabel) groupLabel = fwLabel.innerText.trim().replace(/\*$/,'').trim();
            // 2. aria-label on fw
            if (!groupLabel) groupLabel = (fw.getAttribute('aria-label') || '').trim();
            // 3. First <p> or <span> text in fw that's not a radio option
            if (!groupLabel) {
                const optTexts = new Set(radioOpts.map(o => o.toLowerCase()));
                for (const node of Array.from(fw.querySelectorAll('p,span,div,[class*="label"]'))) {
                    const t = node.innerText?.trim().replace(/\*$/,'').trim();
                    if (t && !optTexts.has(t.toLowerCase()) && t.length > 3 && t.length < 200) {
                        groupLabel = t; break;
                    }
                }
            }
            // 4. Derive from data-automation-id ("formField-candidateIsPreviousWorker" → "candidate Is Previous Worker")
            if (!groupLabel) {
                const autoId = fw.getAttribute('data-automation-id') || '';
                groupLabel = autoId.replace(/^formField-/, '').replace(/([A-Z])/g,' $1').trim();
            }
            const radioName = el.getAttribute('name') || '';
            el.setAttribute('data-fill-idx', idx);
            results.push({index:idx++, tag:'input', type:'radio', role:'radio',
                id: el.id||'', auto, name: radioName,
                label: groupLabel, section: getSection(el),
                options: radioOpts, radioValues, value: '', isSelectInput: false});
            return;
        }

        let value = '';
        if (type==='checkbox') value = el.checked ? 'true' : 'false';
        else if (tag==='select') value = el.options[el.selectedIndex]?.text.trim()||'';
        else if (inSelectInput) {
            // Read current pill selection
            // Strategy 1: selectedItem pill (most reliable)
            const fwSI = el.closest('[data-automation-id^="formField"]') || el.parentElement;
            const pill = fwSI?.querySelector('[data-automation-id="selectedItem"]');
            if (pill) { value = pill.innerText.trim(); }
            // Strategy 2: promptAriaInstruction (e.g. "1 item selected, Website / Job Board Posting")
            if (!value) {
                const aria = el.closest('[data-automation-id^="formField"]')?.querySelector('[data-automation-id="promptAriaInstruction"]')
                    || document.getElementById(el.getAttribute('aria-describedby') || '');
                if (aria) {
                    const txt = aria.innerText.trim();
                    if (txt && !txt.includes('0 items') && !txt.includes('Expanded') && txt !== '') {
                        const m = txt.match(/item[s]? selected,\s*(.+)/i);
                        value = m ? m[1].trim() : txt;
                    }
                }
            }
            // Strategy 3: the input's own value
            if (!value) value = (el.value || '').trim();
        }
        else value = (el.value||'').trim();

        el.setAttribute('data-fill-idx', idx);
        results.push({index:idx++, tag, type, id:el.id||'', auto,
            label, section:getSection(el), options, value,
            isSelectInput: inSelectInput});
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

# ── Low-level executors ───────────────────────────────────────────────────────

MONTH_NUM = {"january":"01","february":"02","march":"03","april":"04","may":"05","june":"06",
             "july":"07","august":"08","september":"09","october":"10","november":"11","december":"12"}

async def exec_text(page: Page, field: dict, value: str):
    # Convert month name to number if it looks like a date section input
    label_l = field.get("label","").lower()
    if label_l in ("month","month*") and value.lower() in MONTH_NUM:
        value = MONTH_NUM[value.lower()]

    idx = field["index"]
    fid = field.get("id","")

    # Date spinbutton fields (Month/Year/Day) live inside dialog containers; using
    # scroll_into_view causes repeated re-scrolling. Fill directly via JS instead.
    is_date_part = label_l.strip("* ") in ("month", "year", "day")
    if is_date_part:
        await page.evaluate(f"""() => {{
            const el = document.querySelector('[data-fill-idx="{idx}"]') || document.getElementById('{fid}');
            if (!el) return;
            el.focus();
            const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
            if (setter) setter.call(el, '{value}');
            else el.value = '{value}';
            el.dispatchEvent(new Event('input', {{bubbles: true}}));
            el.dispatchEvent(new Event('change', {{bubbles: true}}));
        }}""")
        print(f"    ✓ text  [{idx}] {field['label']!r} = {value!r} (JS date fill)")
        return

    sel = f"[data-fill-idx='{idx}']"
    el = page.locator(sel).first
    if not await el.is_visible() and fid:
        el = page.locator(f"#{fid}").first

    try:
        await el.scroll_into_view_if_needed(timeout=5000)
        await page.keyboard.press("Escape")   # close any open dropdown first
        await page.wait_for_timeout(200)
        await el.click(click_count=3, timeout=8000)
    except Exception:
        # JS fallback: scroll + dispatch click
        await page.evaluate(f"""() => {{
            const el = document.querySelector('[data-fill-idx="{idx}"]') || document.getElementById('{fid}');
            if (el) {{ el.scrollIntoView({{block:'center'}}); el.click(); }}
        }}""")
        await page.wait_for_timeout(200)

    await el.fill(value)
    # Trigger React synthetic events (handles both input and textarea)
    await page.evaluate(f"""() => {{
        const el = document.querySelector('[data-fill-idx="{idx}"]') || document.getElementById('{fid}');
        if (!el) return;
        try {{
            const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
            const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
            if (setter) setter.call(el, el.value);
        }} catch(e) {{}}
        el.dispatchEvent(new Event('input', {{bubbles: true}}));
        el.dispatchEvent(new Event('change', {{bubbles: true}}));
    }}""")
    print(f"    ✓ text  [{idx}] {field['label']!r} = {value!r}")

async def exec_button_dropdown(page: Page, field: dict, value: str):
    sel = f"[data-fill-idx='{field['index']}']"
    btn = page.locator(sel).first
    fid = field.get("id","")
    faid = field.get("auto","")  # data-automation-id fallback (for buttons without id)
    if not await btn.is_visible():
        if fid:
            btn = page.locator(f"button#{fid}").first
        elif faid:
            btn = page.locator(f"button[data-automation-id='{faid}']").first
    await btn.scroll_into_view_if_needed()
    # If button is already expanded (still open from prefetch), close it first
    try:
        is_expanded = await page.evaluate(f"""() => {{
            const el = document.querySelector('[data-fill-idx="{field["index"]}"]');
            return el ? el.getAttribute('aria-expanded') === 'true' : false;
        }}""")
        if is_expanded:
            await btn.click()
            await page.wait_for_timeout(600)
    except Exception:
        pass
    await btn.click()
    # Wait for options to fully load (retry up to 5x if only disabled "Select One" visible)
    opts = []
    for attempt in range(5):
        await page.wait_for_timeout(800 if attempt > 0 else 1200)
        opts = await page.evaluate("()=>Array.from(document.querySelectorAll(\"li[role='option']\")).map(l=>l.innerText.trim()).filter(Boolean)")
        real_opts = [o for o in opts if o.lower() not in ('select one', '')]
        if real_opts:
            break
    match = fuzzy_pick(opts, value)
    if not match:
        # Skip disabled "Select One" — fall back to first non-disabled option
        non_disabled = [o for o in opts if o.lower() not in ('select one', '')]
        if non_disabled:
            match = fuzzy_pick(non_disabled, value) or non_disabled[0]
        elif opts:
            match = opts[0]
    if match and match.lower() != 'select one':
        try:
            await page.locator(f"li[role='option']:has-text('{match[:50]}')").first.wait_for(state="visible", timeout=5000)
            await page.locator(f"li[role='option']:has-text('{match[:50]}')").first.click()
            print(f"    ✓ drop  [{field['index']}] {field['label']!r} = {match!r}")
        except Exception as e:
            await page.keyboard.press("Escape")
            print(f"    ~ err   [{field['index']}] {field['label']!r}: {e}")
    else:
        await page.keyboard.press("Escape")
        print(f"    ~ drop  [{field['index']}] {field['label']!r} — no real options (got {opts})")

async def exec_selectinput(page: Page, field: dict, value: str):
    """Workday selectinput: click → type search → Enter → wait for results → click match.
    value may contain newline-separated fallback terms (tried in order until results found)."""
    idx = field['index']
    fid = field.get('id', '')

    # Support multi-term fallback: "Primary Term\nFallback1\nFallback2"
    terms = [t.strip() for t in value.split("\n") if t.strip()]
    if not terms:
        print(f"    ~ sel   [{idx}] {field['label']!r} — empty value")
        return

    # Locate input
    if fid:
        inp = page.locator(f"input#{fid}").first
    else:
        inp = page.locator(f"[data-fill-idx='{idx}']").first

    try:
        await inp.scroll_into_view_if_needed(timeout=5000)
        await inp.click(force=True, timeout=5000)
    except Exception:
        await page.evaluate(f"""() => {{
            const el = document.querySelector('[data-fill-idx="{idx}"]');
            if (el) {{ el.scrollIntoView({{block:'center'}}); el.click(); }}
        }}""")
    await page.wait_for_timeout(400)

    for term_idx, term in enumerate(terms):
        # Clear any existing text and type the search term
        await inp.click(click_count=3, force=True)
        await inp.type(term, delay=70)
        await page.wait_for_timeout(300)

        # Press Enter to trigger Workday's server-side search filter
        await inp.press("Enter")
        await page.wait_for_timeout(1200)  # wait for search results

        # Read only VISIBLE options (height > 0) — critical for Workday virtual scroll:
        # filtered results show height > 0; non-matching hidden items have height = 0
        results = await page.evaluate(f"""() => {{
            const getVisible = (c) => Array.from(c.querySelectorAll('[role="option"]'))
                .filter(e => e.getBoundingClientRect().height > 0)
                .map(e => e.innerText.trim()).filter(Boolean);
            const c1 = Array.from(document.querySelectorAll('[data-automation-id="activeListContainer"]'))
                .find(x => x.getBoundingClientRect().height > 0);
            if (c1) {{ const o = getVisible(c1); if (o.length) return o; }}
            const poppers = Array.from(document.querySelectorAll('[data-popper-placement]'))
                .filter(x => x.getBoundingClientRect().height > 0);
            for (const p of poppers) {{ const o = getVisible(p); if (o.length) return o; }}
            return [];
        }}""")

        print(f"    [sel] search={term!r} results ({len(results)}): {results[:5]}")

        if not results:
            # Maybe auto-filled as single pill (Workday collapses single-match to pill)
            pill = await page.evaluate(f"""() => {{
                const fid = '{fid}';
                const el = fid ? document.getElementById(fid) : document.querySelector('[data-fill-idx="{idx}"]');
                const fw = el?.closest('[data-automation-id^="formField"]') || el?.parentElement;
                const pill = fw?.querySelector('[data-automation-id="selectedItem"]');
                if (pill && pill.getBoundingClientRect().height > 0) return pill.innerText.trim();
                return null;
            }}""")
            if pill:
                print(f"    ✓ sel   [{idx}] {field['label']!r} = {pill!r} (auto-filled single result)")
                return
            # Try next fallback term
            if term_idx < len(terms) - 1:
                print(f"    ~ sel   no results for {term!r}, trying fallback {terms[term_idx+1]!r}...")
                await inp.press("Escape")
                await page.wait_for_timeout(300)
                continue
            print(f"    ~ sel   [{idx}] {field['label']!r} — no results for any term: {terms}")
            await inp.press("Escape")
            return

        # If results look unfiltered (first result doesn't contain any word from search term),
        # Workday returned all options (no match for this term) — try next fallback term
        term_words = set(term.lower().split())
        first_l = results[0].lower()
        if not any(w in first_l for w in term_words) and term_idx < len(terms) - 1:
            print(f"    ~ sel   results unfiltered for {term!r} (first: {results[0]!r}), trying fallback {terms[term_idx+1]!r}...")
            await inp.press("Escape")
            await page.wait_for_timeout(300)
            continue

        # Pick best match from filtered results
        match = fuzzy_pick(results, term) or results[0]
        match_lower = match[:60].lower()
        clicked = await page.evaluate(f"""() => {{
            const getVisible = (c) => Array.from(c.querySelectorAll('[role="option"]'))
                .filter(e => e.getBoundingClientRect().height > 0);
            let opts = [];
            const c1 = Array.from(document.querySelectorAll('[data-automation-id="activeListContainer"]'))
                .find(x => x.getBoundingClientRect().height > 0);
            if (c1) opts = getVisible(c1);
            if (!opts.length) {{
                const poppers = Array.from(document.querySelectorAll('[data-popper-placement]'))
                    .filter(x => x.getBoundingClientRect().height > 0);
                for (const p of poppers) {{ const o = getVisible(p); if (o.length) {{ opts = o; break; }} }}
            }}
            const target = opts.find(e => e.innerText.trim().toLowerCase() === '{match_lower}') ||
                           opts.find(e => e.innerText.trim().toLowerCase().includes('{match_lower}')) ||
                           opts[0];
            if (target) {{
                target.scrollIntoView({{block:'nearest'}});
                target.dispatchEvent(new MouseEvent('mousedown', {{bubbles:true}}));
                target.click();
                return target.innerText.trim();
            }}
            return null;
        }}""")
        await page.wait_for_timeout(400)
        # Close dropdown cleanly
        await inp.press("Escape")
        await page.wait_for_timeout(200)
        print(f"    {'✓' if clicked else '~'} sel   [{idx}] {field['label']!r} = {clicked or match!r}")
        return

async def exec_radio(page: Page, field: dict, value: str):
    """Click the correct radio in a group, works for both [role=radio] and input[type=radio]."""
    texts = field.get("options") or []
    radio_values = field.get("radioValues") or []
    radio_name = field.get("name") or ""

    match = fuzzy_pick(texts, value) or (texts[0] if texts else value)
    match_idx = texts.index(match) if match in texts else 0
    target_value = radio_values[match_idx] if match_idx < len(radio_values) else None

    # Strategy 1: input[type=radio][name=...][value=...] — stable for RH-style
    if radio_name and target_value is not None:
        clicked = await page.evaluate(f"""() => {{
            const r = document.querySelector('input[type="radio"][name="{radio_name}"][value="{target_value}"]');
            if (r) {{ r.click(); return true; }}
            return false;
        }}""")
        if clicked:
            print(f"    ✓ radio [{field['index']}] {field['label']!r} = {match!r} (name/value)")
            return

    # Strategy 2: [role=radio] by aria-label
    match_lower = match[:30].lower()
    escaped = match[:50].replace("'", "\\'")
    for sel in [f"[role='radio'][aria-label='{escaped}']", f"[role='radio']:has-text('{escaped}')"]:
        opt = page.locator(sel).first
        if await opt.count():
            await opt.scroll_into_view_if_needed()
            await opt.click(force=True)
            print(f"    ✓ radio [{field['index']}] {field['label']!r} = {match!r} (role/aria-label)")
            return

    # Strategy 3: JS walk up from tagged element
    clicked = await page.evaluate(f"""() => {{
        const tagged = document.querySelector('[data-fill-idx="{field["index"]}"]');
        if (!tagged) return false;
        let parent = tagged.parentElement;
        for (let i = 0; i < 8 && parent; i++) {{
            const radios = Array.from(parent.querySelectorAll('[role="radio"],input[type="radio"]'));
            if (radios.length) {{
                const r = radios.find(r => {{
                    const lbl = (r.getAttribute('aria-label') || r.parentElement?.innerText || '').toLowerCase();
                    return lbl.includes('{match_lower}');
                }}) || radios[{match_idx}];
                if (r) {{ r.click(); return true; }}
            }}
            parent = parent.parentElement;
        }}
        return false;
    }}""")
    print(f"    {'✓' if clicked else '~'} radio [{field['index']}] {field['label']!r} = {match!r} (options: {texts})")
    await page.wait_for_timeout(500)


async def exec_checkbox(page: Page, field: dict, value: str):
    want = value.lower() in ("true","yes","on","checked","1")
    idx = field["index"]; fid = field.get("id","")
    sel = f"[data-fill-idx='{idx}']"
    el = page.locator(sel).first
    if not await el.count() and fid:
        el = page.locator(f"#{fid}").first
    # Check current state
    current_checked = await page.evaluate(f"""() => {{
        const el = document.querySelector('[data-fill-idx="{idx}"]') || document.getElementById('{fid}');
        if (!el) return null;
        return el.checked || el.getAttribute('aria-checked') === 'true';
    }}""")
    if (want and current_checked) or (not want and not current_checked):
        print(f"    ✓ check [{idx}] {field['label']!r} = {value!r} (already set)")
        return
    # Try multiple click strategies for Workday custom checkboxes
    clicked = False
    # Strategy 1: Playwright click (not check) — triggers visual checkbox
    try:
        await el.scroll_into_view_if_needed(timeout=3000)
        await el.click(force=True, timeout=3000)
        clicked = True
    except Exception:
        pass
    if not clicked:
        # Strategy 2: Find and click the visible wrapper/label
        clicked = await page.evaluate(f"""() => {{
            const el = document.querySelector('[data-fill-idx="{idx}"]') || document.getElementById('{fid}');
            if (!el) return false;
            // Try associated label first
            const lbl = document.querySelector('label[for="{fid}"]');
            if (lbl && lbl.getBoundingClientRect().height > 0) {{ lbl.click(); return true; }}
            // Walk up to find visible parent wrapper
            let node = el.parentElement;
            for (let i=0; i<6 && node; i++) {{
                const rect = node.getBoundingClientRect();
                if (rect.height > 5 && rect.width > 5) {{
                    // Prefer Workday checkbox automation containers
                    const cbChild = node.querySelector('[data-automation-id*="checkbox"],[class*="checkbox"],[role="checkbox"]');
                    if (cbChild) {{ cbChild.click(); return true; }}
                    node.click();
                    return true;
                }}
                node = node.parentElement;
            }}
            el.click();
            el.dispatchEvent(new Event('change', {{bubbles: true}}));
            return true;
        }}""")
    await page.wait_for_timeout(400)
    # Verify the state changed
    after = await page.evaluate(f"""() => {{
        const el = document.querySelector('[data-fill-idx="{idx}"]') || document.getElementById('{fid}');
        return el ? (el.checked || el.getAttribute('aria-checked') === 'true') : null;
    }}""")
    print(f"    ✓ check [{idx}] {field['label']!r} = {value!r} (verified={after})")

async def execute_answer(page: Page, field: dict, value: str):
    if not value: return
    tag = field.get("tag",""); ftype = field.get("type","")
    role = field.get("role","")
    try:
        if ftype == "checkbox" or role == "checkbox":
            await exec_checkbox(page, field, value)
        elif role == "radio":
            await exec_radio(page, field, value)
        elif field.get("isSelectInput"):
            # Workday selectinput widget — type-to-search dropdown
            await exec_selectinput(page, field, value)
        elif tag == "button" and (field.get("id") or field.get("automation_id")):
            await exec_button_dropdown(page, field, value)
        elif tag in ("input","textarea") or ftype in ("text","email","tel","number","url","search"):
            await exec_text(page, field, value)
        elif tag == "select":
            el = page.locator(f"[data-fill-idx='{field['index']}']").first
            try: await el.select_option(label=value, timeout=3000)
            except: await exec_button_dropdown(page, field, value)
            print(f"    ✓ sel   [{field['index']}] {field['label']!r} = {value!r}")
        else:
            await exec_text(page, field, value)
    except Exception as e:
        print(f"    ~ err   [{field['index']}] {field['label']!r}: {e}")

async def save_and_continue(page: Page) -> bool:
    """Click Save and Continue. Returns True if page advanced, False if validation error."""
    url_before = page.url
    heading_before = await get_heading(page)
    clicked = await page.evaluate("""() => {
        const btn = document.querySelector('[data-automation-id="pageFooterNextButton"]');
        if (btn) { btn.click(); return true; }
        return false;
    }""")
    if not clicked:
        print(f"  [NAV] No Next button found — page may not be ready")
        await page.wait_for_timeout(2000)
        return False
    await page.wait_for_timeout(3000)
    # Check for validation errors (errorMessage only — aria-invalid unreliable on selectinputs)
    errors = await page.evaluate("""() => {
        const errs = [];
        document.querySelectorAll('[data-automation-id="errorMessage"]').forEach(e => {
            if (e.getBoundingClientRect().height > 0) {
                const t = e.innerText?.trim();
                if (t) errs.push(t);
            }
        });
        // Also check general visible validation-related text
        document.querySelectorAll('[class*="error"],[class*="Error"],[class*="invalid"],[class*="Invalid"]').forEach(e => {
            if (e.getBoundingClientRect().height > 0 && e.childElementCount === 0) {
                const t = e.innerText?.trim();
                if (t && t.length < 200) errs.push(t);
            }
        });
        return [...new Set(errs)].slice(0, 10);
    }""")
    if errors:
        print(f"  [NAV] Validation errors: {errors}")
        # Also print any visible "required" field labels
        req_labels = await page.evaluate("""() => {
            return Array.from(document.querySelectorAll('[aria-required="true"],[aria-invalid="true"]'))
                .filter(e => e.getBoundingClientRect().height > 0)
                .map(e => {
                    const fw = e.closest('[data-automation-id="formField"]');
                    const lbl = fw?.querySelector('[data-automation-id="formLabel"],label')?.innerText || e.getAttribute('aria-label') || '';
                    return lbl.trim();
                }).filter(Boolean).slice(0, 10);
        }""")
        if req_labels:
            print(f"  [NAV] Invalid/required fields: {req_labels}")
        return False
    heading_after = await get_heading(page)
    url_after = page.url
    print(f"  [NAV] heading: {heading_before!r} → {heading_after!r} | url changed: {url_before != url_after}")
    if heading_after != heading_before or url_after != url_before:
        return True
    # Same heading/url — wait a bit more and take screenshot for diagnostics
    await page.wait_for_timeout(2500)
    heading_after2 = await get_heading(page)
    url_after2 = page.url
    print(f"  [NAV] (retry) heading: {heading_after2!r} | url changed: {url_before != url_after2}")
    if heading_after2 != heading_before or url_after2 != url_before:
        return True
    # Still stuck — screenshot for diagnostics
    shot = str(ARTIFACTS / f"stuck_{heading_before.replace(' ','_')}.png")
    await page.screenshot(path=shot, full_page=False)
    print(f"  [NAV] screenshot: {shot}")
    # Print all visible text that looks like errors or required hints
    page_hints = await page.evaluate("""() => {
        const sel = ['[data-automation-id="errorMessage"]','[aria-invalid="true"]',
                     '[data-automation-id="validationError"]'];
        const found = [];
        sel.forEach(s => document.querySelectorAll(s).forEach(e => {
            const r = e.getBoundingClientRect();
            if (r.height > 0) {
                const fw = e.closest('[data-automation-id="formField"]');
                const lbl = fw?.querySelector('[data-automation-id="formLabel"],label')?.innerText
                          || e.getAttribute('aria-label') || e.innerText || '';
                if (lbl.trim()) found.push(lbl.trim().slice(0,100));
            }
        }));
        return [...new Set(found)].slice(0,15);
    }""")
    if page_hints:
        print(f"  [NAV] page error hints: {page_hints}")
    return False

# ── Pre-fetch options for all button-dropdowns on a page ─────────────────────

async def prefetch_options(page: Page, fields: list[dict]):
    for f in fields:
        if f["tag"] == "button" and not f["options"]:
            fid = f.get("id","")
            faid = f.get("auto","")
            fidx = f.get("index","")
            try:
                # Locate by id > auto-id > data-fill-idx
                if fid:
                    btn = page.locator(f"button#{fid}").first
                elif faid:
                    btn = page.locator(f"button[data-automation-id='{faid}']").first
                else:
                    btn = page.locator(f"[data-fill-idx='{fidx}']").first
                await btn.scroll_into_view_if_needed()
                await btn.click()
                await page.wait_for_timeout(900)
                opts = await page.evaluate("()=>Array.from(document.querySelectorAll(\"li[role='option']\")).map(l=>l.innerText.trim()).filter(Boolean)")
                f["options"] = opts
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(400)
                print(f"    [{f['index']:2}] opts {f['label']!r}: {opts[:5]}{'...' if len(opts)>5 else ''}")
            except: pass

# ── Smart page filler: DeepSeek primary, rule-based fallback ─────────────────

async def smart_fill_page(page: Page, heading: str, context_hint: str = ""):
    print(f"\n  [SCAN] '{heading}'...")

    # Scroll to bottom then top to trigger lazy-rendering of all form sections
    await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
    await page.wait_for_timeout(600)
    await page.evaluate("() => window.scrollTo(0, 0)")
    await page.wait_for_timeout(400)

    fields = await page.evaluate(SCAN_JS, None)

    fillable = [f for f in fields if f["label"]]
    # If 0 fields found, page might still be loading — retry up to 3 times
    for retry in range(3):
        if fillable: break
        await page.wait_for_timeout(2000)
        fields = await page.evaluate(SCAN_JS, None)
        fillable = [f for f in fields if f["label"]]
    print(f"  [SCAN] {len(fillable)} fields:")
    for f in fillable:
        print(f"    [{f['index']:2}] {f['tag']:8} {f['label']!r:45} val={f['value']!r}")

    if not fillable: return

    # Pre-fetch options for all button-dropdowns
    await prefetch_options(page, fillable)
    await page.wait_for_timeout(600)  # let page settle after prefetch opens/closes

    # Primary: DeepSeek
    if DEEPSEEK_KEY:
        print(f"  [LLM] Sending {len(fillable)} fields to DeepSeek...")
        answers = await deepseek_fill_page(fillable)
        print(f"  [LLM] Got {len(answers)} answers")
    else:
        print(f"  [RULES] DeepSeek unavailable — using label-matching rules")
        answers = await rule_based_fill_page(fillable, context_hint or heading)

    field_map = {f["index"]: f for f in fillable}
    for ans in answers:
        idx = ans.get("index"); val = ans.get("value","")
        if idx is None or not val: continue
        field = field_map.get(idx)
        if field:
            await execute_answer(page, field, val)
            await page.wait_for_timeout(200)

# ── Sign-in ───────────────────────────────────────────────────────────────────

async def sign_in(page: Page):
    print("[AUTH] Signing in...")
    await page.locator("[data-automation-id='signInLink']").first.click(force=True)
    await page.wait_for_timeout(1200)
    e = page.locator("[data-automation-id='email']").first
    await e.wait_for(state="visible", timeout=8000)
    await e.click(); await e.type(EMAIL, delay=60)
    p = page.locator("[data-automation-id='password']").first
    await p.click(); await p.type(PASSWORD, delay=50)
    await page.wait_for_timeout(400)
    await page.locator("[data-automation-id='click_filter']").first.click()
    for _ in range(25):
        await page.wait_for_timeout(1000)
        if await page.locator("[data-automation-id='pageFooterNextButton']").count(): break
        if not await page.locator("[data-automation-id='signInSubmitButton']").count(): break
    await page.wait_for_timeout(1000)
    await page.reload(wait_until="domcontentloaded")
    await page.wait_for_timeout(3000)
    print("[AUTH] ✓ Signed in.")

# ── Add-dialog filler (My Experience modals) ─────────────────────────────────

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
            # Language NAME: either labeled "language/lang name" or unlabeled selectinput (first in dialog)
            if label_match(label, "language","lang name","name") or (not label and field.get("isSelectInput")):
                return entry.get("language","")
            # Reading/Speaking/Writing proficiency — all map to the entry proficiency value
            if label_match(label, "proficiency","fluency","level","ability",
                           "reading","writing","speaking"):
                return entry.get("proficiency","")
        if is_work:
            # Description check MUST come before title/role — "Role Description" contains "role"
            # and would incorrectly match the title rule if order were reversed
            if label_match(label, "description","responsibilities","summary","duties","role description"):
                return entry.get("description","")[:500]
            if label_match(label, "job title","title","position") or \
               (label_match(label, "role") and not label_match(label, "description")):
                return entry.get("role","")
            if label_match(label, "company", "employer", "organization"):
                return entry.get("company","")
            if label_match(label, "city", "location") and not label_match(label, "country","state"):
                return entry.get("city","")
            if label_match(label, "description","responsibilities","summary","duties"):
                return entry.get("description","")[:500]
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
                # Return primary + fallback terms separated by \n so exec_selectinput tries each
                terms = [entry.get("major_search_term", "")] + entry.get("major_variants", [])
                terms = list(dict.fromkeys(t for t in terms if t))  # dedupe, preserve order
                return "\n".join(terms) if terms else None
            if label_match(label, "gpa","grade"):
                return str(entry.get("gpa",""))
            if label_match(label, "start month") or (label_match(label,"month") and date_seq=="start"):
                return entry.get("start_month","")
            if label_match(label, "end month","graduation month") or (label_match(label,"month") and date_seq=="end"):
                cur = entry.get("current_status","").lower()
                return "" if "attending" in cur else entry.get("end_month","")
            if label_match(label, "start year","year from") or (label_match(label,"year") and date_seq=="start"):
                return str(entry.get("start_year",""))
            if label_match(label, "end year","year to","graduation year") or (label_match(label,"year") and date_seq=="end"):
                cur = entry.get("current_status","").lower()
                return "" if "attending" in cur else str(entry.get("end_year",""))

    # ── Button dropdowns (month/degree pickers) ───────────────────────────────
    if tag == "button" or ftype == "select-one":
        if not opts:
            return None
        if is_lang:
            if label_match(label, "language","lang name","name"):
                return fuzzy_pick(opts, entry.get("language","")) or opts[0]
            # Reading/Speaking/Writing/Overall proficiency dropdowns all use entry proficiency
            if label_match(label, "proficiency","fluency","level","ability",
                           "reading","writing","speaking","overall"):
                return fuzzy_pick(opts, entry.get("proficiency","")) or opts[0]
        if is_work:
            if label_match(label, "start month") or (label_match(label,"month") and date_seq=="start"):
                return fuzzy_pick(opts, entry.get("start_month","")) or opts[0]
            if label_match(label, "end month") or (label_match(label,"month") and date_seq=="end"):
                cur = entry.get("current_status","").lower()
                return "" if "current" in cur else (fuzzy_pick(opts, entry.get("end_month","")) or opts[0])
            if label_match(label, "currently work","still work","current job","present"):
                cur = entry.get("current_status","").lower()
                want = "Yes" if "current" in cur else "No"
                return fuzzy_pick(opts, want) or opts[0]
        if is_edu:
            if label_match(label, "degree","degree type","level of education"):
                # Try abbreviation first, then variants, then full name
                abbrev = entry.get("degree_abbreviation","")
                if abbrev:
                    hit = fuzzy_pick(opts, abbrev)
                    if hit: return hit
                for variant in entry.get("degree_search_variants",[]):
                    hit = fuzzy_pick(opts, variant)
                    if hit: return hit
                return fuzzy_pick(opts, entry.get("degree_type","")) or opts[0]
            if label_match(label, "start month") or (label_match(label,"month") and date_seq=="start"):
                return fuzzy_pick(opts, entry.get("start_month","")) or opts[0]
            if label_match(label, "end month","graduation month") or (label_match(label,"month") and date_seq=="end"):
                cur = entry.get("current_status","").lower()
                return "" if "attending" in cur else (fuzzy_pick(opts, entry.get("end_month","")) or opts[0])
            if label_match(label, "currently attend","still attend","current student","present"):
                cur = entry.get("current_status","").lower()
                want = "Yes" if "attending" in cur else "No"
                return fuzzy_pick(opts, want) or opts[0]

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
            return fuzzy_pick(radio_opts, want) or (radio_opts[0] if radio_opts else None)
        if is_edu and label_match(label, "currently attend","still attend","present"):
            cur = entry.get("current_status","").lower()
            want = "Yes" if "attending" in cur else "No"
            return fuzzy_pick(radio_opts, want) or (radio_opts[0] if radio_opts else None)

    return None


async def fill_add_dialog(page: Page, dialog_label: str, entry: dict = None, section_type: str = "",
                          count_before: int = 0, fields_before: list = None):
    """Scan an open add-dialog, fill only the LAST (newly added) entry group."""
    await page.wait_for_timeout(1500)  # give Workday's JS time to initialize dialog fields
    all_fields = await page.evaluate(SCAN_JS, None)

    # Identify the NEW entry by finding the last occurrence of its anchor field.
    # The anchor is the first field in each repeated entry block:
    #   WE: "Job Title" | EDU: "School or University"
    if section_type == "work":
        anchor_kws = ["job title"]
    elif section_type == "edu":
        anchor_kws = ["school or university", "school", "university"]
    elif section_type == "lang":
        # Language dialog: new fields (Language NAME, Overall, R/S/W) are inserted
        # at DOM positions that may be BELOW count_before (before Skills/resume section).
        # Use proper field-diff to find new fields instead of count_before slice.
        anchor_kws = []
    else:
        anchor_kws = []

    if anchor_kws:
        # Section stop keywords — stop taking fields when we hit a field from the NEXT section
        if section_type == "work":
            stop_kws = ["school or university", "school", "university", "degree",
                        "type to add skills", "language", "website"]
        elif section_type == "edu":
            stop_kws = ["type to add skills", "language", "website", "job title"]
        else:  # lang
            stop_kws = ["type to add skills", "website", "job title", "school",
                        "upload a file", "linkedin", "provide your linkedin"]

        anchor_positions = [
            i for i, f in enumerate(all_fields)
            if any(kw in f.get("label","").lower() for kw in anchor_kws)
        ]
        if anchor_positions:
            last_start = anchor_positions[-1]
            # Take from last anchor until the NEXT other-section boundary
            stop_pos = None
            for i in range(last_start + 1, len(all_fields)):
                lbl = all_fields[i].get("label","").lower()
                if any(kw in lbl for kw in stop_kws):
                    stop_pos = i
                    break
            if stop_pos:
                fields = all_fields[last_start: stop_pos]
            else:
                fields = all_fields[last_start:]
        else:
            fields = all_fields[count_before:] if count_before > 0 else all_fields
    elif section_type == "lang" and fields_before is not None:
        # Diff: find fields in all_fields that weren't in fields_before
        # Build identity set for before-fields: (id|auto|label+tag)
        def field_id(f):
            if f.get("id"): return "id:" + f["id"]
            if f.get("auto"): return "auto:" + f["auto"]
            return "lt:" + f.get("label","") + "|" + f.get("tag","")
        before_ids = set(field_id(f) for f in fields_before)
        fields = [f for f in all_fields if field_id(f) not in before_ids]
        # Limit: stop at "Type to Add Skills" / LinkedIn (outside lang section)
        stop_kws_lang = ["type to add skills", "upload a file", "linkedin", "provide your linkedin", "website"]
        trimmed = []
        for f in fields:
            if any(kw in f.get("label","").lower() for kw in stop_kws_lang):
                break
            trimmed.append(f)
        fields = trimmed
    else:
        # Not a WE/EDU dialog — use count_before tail (e.g. language, website)
        fields = all_fields[count_before:] if count_before > 0 else all_fields
    for f in fields:
        f["page_heading"] = dialog_label
        f["section"] = dialog_label

    # Annotate ambiguous "Month"/"Year" labels with start/end position
    # (Workday uses generic "Month"/"Year" labels — first occurrence = start, second = end)
    month_seq = 0; year_seq = 0
    for f in fields:
        lbl = f.get("label","").lower()
        if lbl in ("month","month*"):
            f["date_seq"] = "start" if month_seq == 0 else "end"
            month_seq += 1
        elif lbl in ("year","year*"):
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

    if DEEPSEEK_KEY:
        # Pass entry context so LLM knows which specific WE/EDU/LANG entry it's filling
        answers = await deepseek_fill_page(fillable, entry=entry, section_type=section_type)
    elif entry and section_type:
        # Use entry-specific answers
        answers = []
        for f in fillable:
            val = entry_answer(f, entry, section_type)
            if val is None:
                # Fall back to generic rules for non-entry fields
                val = rule_based_answer(f, dialog_label)
            if val is not None and val != "":
                answers.append({"index": f["index"], "value": val})
    else:
        answers = await rule_based_fill_page(fillable, dialog_label)

    field_map = {f["index"]: f for f in fillable}
    # Process checkbox/radio first (e.g. "I currently work here" hides end-date fields)
    for ans in answers:
        idx = ans.get("index"); val = ans.get("value","")
        if idx is None or not val: continue
        field = field_map.get(idx)
        if field and (field.get("type") in ("checkbox","radio") or field.get("role") in ("checkbox","radio")):
            await execute_answer(page, field, val)
            await page.wait_for_timeout(400)
    # Then remaining fields
    for ans in answers:
        idx = ans.get("index"); val = ans.get("value","")
        if idx is None or not val: continue
        field = field_map.get(idx)
        if field and field.get("type") not in ("checkbox","radio") and field.get("role") not in ("checkbox","radio"):
            await execute_answer(page, field, val)
            await page.wait_for_timeout(200)

    await page.wait_for_timeout(500)
    # Check for explicit per-entry Save/OK buttons (modal dialogs)
    # For inline accordion forms (like RH), there is NO per-entry save button — skip
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

async def exec_skills_field(page: Page, field: dict, skills: list[str]):
    """Fill a skills selectinput: for each skill, type → Enter → pick best match.
    Checks already-selected pills before clicking to avoid accidental deselection."""
    fid = field.get('id', '')
    idx = field['index']
    for skill in skills:
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

        # Poll for dropdown results (up to 3s) — server search can be slow
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
            await page.wait_for_timeout(600)  # flush any pending network responses
            print(f"    ~ skill '{skill}' — no results")
            continue

        # Get already-selected pills BEFORE picking (used by both DeepSeek and rule-based paths)
        already_pills = await page.evaluate(f"""() => {{
            const fid = '{fid}';
            const el = fid ? document.getElementById(fid) : document.querySelector('[data-fill-idx="{idx}"]');
            const fw = el?.closest('[data-automation-id^="formField"]') || el?.parentElement;
            const items = Array.from(fw?.querySelectorAll('[data-automation-id="selectedItem"]') || []);
            return items.map(e => e.innerText.trim().toLowerCase());
        }}""")

        # Pick best match: DeepSeek if available (with rule-based fallback), else rule-based only
        def rule_score(opt):
            o = opt.lower()
            skill_l = skill.lower()
            if o == skill_l: return 0
            if o.startswith(skill_l + " "): return 1
            if re.search(r'\b' + re.escape(skill_l) + r'\b', o): return 2
            if o.startswith(skill_l): return 3
            return 99

        if DEEPSEEK_KEY:
            best = await deepseek_pick_skill(skill, results, already_pills)
            if best is None:
                # DeepSeek said no match or errored — fall back to rule-based rather than skip
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

            # DeepSeek API took ~1-2s — dropdown may have closed; re-type to reopen it
            await inp.click(click_count=3, force=True)
            await inp.type(best[:30], delay=50)  # type the chosen option to filter to it
            await page.wait_for_timeout(800)
            # Re-read results to confirm dropdown is open with matching options
            results = await page.evaluate(READ_JS)
            if not results:
                await page.wait_for_timeout(800)
                results = await page.evaluate(READ_JS)
        else:
            # Rule-based only
            best = min(results, key=rule_score)
            if rule_score(best) >= 99:
                await inp.press("Escape")
                await page.wait_for_timeout(400)
                print(f"    ~ skill '{skill}' → no relevant match (best: {best!r}), skipping")
                continue
            print(f"    → skill '{skill}' → rule picked {best!r}")

            # Guard against double-clicking an already-selected pill (deselects it)
            best_l = best.lower()
            if already_pills and any(best_l in p or p in best_l for p in already_pills):
                print(f"    ~ skill '{skill}' → {best!r} already selected, skipping")
                await inp.press("Escape")
                await page.wait_for_timeout(300)
                continue

        best_lower = best[:50].lower()
        clicked = await page.evaluate(f"""() => {{
            const getOpts = (c) => Array.from(c.querySelectorAll('[role="option"],[data-automation-id="promptLeafNode"]'))
                .filter(e => e.getBoundingClientRect().height > 0);
            let opts = [];
            const c1 = Array.from(document.querySelectorAll('[data-automation-id="activeListContainer"]'))
                .find(x => x.getBoundingClientRect().height > 0);
            if (c1) opts = getOpts(c1);
            if (!opts.length) {{
                const poppers = Array.from(document.querySelectorAll('[data-popper-placement]'))
                    .filter(x => x.getBoundingClientRect().height > 0);
                for (const p of poppers) {{ const o = getOpts(p); if (o.length) {{ opts = o; break; }} }}
            }}
            if (!opts.length) return null;
            // Prefer exact match on LLM-chosen text, then partial, then first
            const exact = opts.find(e => e.innerText.trim().toLowerCase() === '{best_lower}');
            const partial = opts.find(e => e.innerText.trim().toLowerCase().includes('{best_lower}'));
            const t = exact || partial || opts[0];
            if (t) {{ t.dispatchEvent(new MouseEvent('mousedown',{{bubbles:true}})); t.click(); return t.innerText.trim(); }}
            return null;
        }}""")
        await page.wait_for_timeout(600)
        if clicked:
            print(f"    ✓ skill '{skill}' → clicked {clicked!r}")
        else:
            print(f"    ~ skill '{skill}' → dropdown closed before click (LLM={best!r})")
        # Close dropdown before next skill
        await inp.press("Escape")
        await page.wait_for_timeout(300)


async def handle_my_experience(page: Page):
    print("\n[PAGE] My Experience")

    # Resume upload
    if await page.locator("input[type='file']").count():
        await page.locator("input[type='file']").first.set_input_files(RESUME_PATH)
        await page.wait_for_timeout(2500)
        print(f"    ✓ resume uploaded")

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
        "education": ("edu", EDU), "school": ("edu", EDU), "degree": ("edu", EDU),
        "language": ("lang", LANG),
    }

    for btn_info in add_btns_info:
        label_l = btn_info["label"].lower()

        # Websites — skip (no library data needed)
        if "website" in label_l:
            print(f"  [ADD] Skipping '{btn_info['label']}' (websites not required)")
            continue

        # Skills section — handled separately after all add-buttons
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

        for entry_idx, entry in enumerate(data_list):
            # Re-query add-buttons each iteration (DOM updates after each dialog)
            add_btns = page.locator("[data-automation-id='add-button']")
            total = await add_btns.count()
            if btn_info["index"] >= total:
                print(f"  [ADD] Button index {btn_info['index']} out of range ({total}), stopping")
                break

            entry_label = entry.get("role") or entry.get("degree_type") or entry.get("language", "entry")
            company = entry.get("company") or (entry.get("institution_variants",[""])[0]) or ""
            dialog_label = f"{btn_info['label']}: {entry_label} at {company}"
            print(f"\n  [ADD {entry_idx+1}/{len(data_list)}] '{btn_info['label']}' → {dialog_label}")

            # Count all scannable fields BEFORE clicking Add
            fields_before = await page.evaluate(SCAN_JS, None)
            count_before = len(fields_before)

            btn = add_btns.nth(btn_info["index"])
            await btn.scroll_into_view_if_needed()
            await btn.click()
            await page.wait_for_timeout(1500)
            await fill_add_dialog(page, dialog_label, entry=entry, section_type=section_type,
                                  count_before=count_before, fields_before=fields_before)
            await page.wait_for_timeout(500)

    # ── Standalone Skills field (not behind an add-button on this listing) ────
    skills = LIBRARY.get("skills", [])
    if skills:
        await page.wait_for_timeout(500)
        fields = await page.evaluate(SCAN_JS, None)
        skill_field = next(
            (f for f in fields if f.get("isSelectInput") and
             any(kw in f.get("label","").lower() for kw in ["skill", "type to add"])),
            None
        )
        if skill_field:
            print(f"\n  [SKILLS] Filling {len(skills)} skills via standalone field...")
            await exec_skills_field(page, skill_field, skills)
        else:
            print(f"  [SKILLS] No standalone skills selectinput found on page")
    else:
        print(f"  [SKILLS] No skills in library")

    await save_and_continue(page)

# ── Voluntary Disclosures handler ─────────────────────────────────────────────

async def handle_voluntary_disclosures(page: Page):
    await smart_fill_page(page, "Voluntary Disclosures")
    # T&C checkbox — try multiple strategies since Workday uses custom React checkboxes
    tc_id = "termsAndConditions--acceptTermsAndAgreements"
    tc_checked = await page.evaluate(f"""() => {{
        const el = document.getElementById('{tc_id}');
        return el ? (el.checked || el.getAttribute('aria-checked') === 'true') : false;
    }}""")
    if not tc_checked:
        # Strategy 1: scroll to view and click the visible wrapper
        clicked = await page.evaluate(f"""() => {{
            const input = document.getElementById('{tc_id}');
            if (!input) return false;
            // Walk up to find a visible ancestor with clickable size
            let node = input.parentElement;
            for (let i=0; i<8 && node && node !== document.body; i++) {{
                const rect = node.getBoundingClientRect();
                if (rect.height > 10 && rect.width > 10) {{
                    const cbTarget = node.querySelector('[data-automation-id*="checkbox"],[class*="checkbox"],[class*="Checkbox"]')
                                  || node;
                    cbTarget.scrollIntoView({{block:'center'}});
                    cbTarget.click();
                    return true;
                }}
                node = node.parentElement;
            }}
            return false;
        }}""")
        await page.wait_for_timeout(400)
        tc_checked = await page.evaluate(f"""() => {{
            const el = document.getElementById('{tc_id}');
            return el ? (el.checked || el.getAttribute('aria-checked') === 'true') : false;
        }}""")
        if not tc_checked:
            # Strategy 2: Playwright mouse click at element bounding box
            try:
                el = page.locator(f"#{tc_id}").first
                if await el.count():
                    await el.scroll_into_view_if_needed(timeout=3000)
                    await el.click(force=True, timeout=3000)
                    await page.wait_for_timeout(400)
                    tc_checked = await el.evaluate("el => el.checked || el.getAttribute('aria-checked') === 'true'")
            except Exception as e:
                print(f"    ~ T&C click2: {e}")
        print(f"    {'✓' if tc_checked else '~'} T&C checked (verified={tc_checked})")
    else:
        print(f"    ✓ T&C already checked")
    await save_and_continue(page)

# ── Self Identify handler ─────────────────────────────────────────────────────

async def handle_self_identify(page: Page):
    print("\n[PAGE] Self Identify")
    today = datetime.today()

    # Fill name if the specific Workday Self-ID field exists
    name_el = page.locator("#selfIdentifiedDisabilityData--name").first
    if await name_el.count():
        await page.evaluate("el => el.scrollIntoView({block:'center'})", await name_el.element_handle())
        await page.wait_for_timeout(300)
        await name_el.fill(f"{PI['first_name']} {PI['last_name']}")
        print(f"    ✓ name = '{PI['first_name']} {PI['last_name']}'")

    # Fill signature date (today) — use nativeInputValueSetter directly; el.fill() on
    # React spinbuttons is unreliable (fails on Windows with "outside viewport" errors)
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
            e.dispatchEvent(new Event('input',  {{bubbles:true}}));
            e.dispatchEvent(new Event('change', {{bubbles:true}}));
            return true;
        }}""")
        if filled:
            print(f"    ✓ date {sfx} = {val!r}")

    # smart_fill_page handles disability radio + language dropdown;
    # pass context "self identify signature" so the month/day/year rule
    # knows these are TODAY (signature), not an availability/start date
    await smart_fill_page(page, "Self Identify", context_hint="self identify signature")
    await save_and_continue(page)

# ── Main ──────────────────────────────────────────────────────────────────────

async def main(job_url: str = DEFAULT_JOB_URL, headed: bool = False):
    mode = "DeepSeek" if DEEPSEEK_KEY else "rule-based fallback"
    key_hint = f"sk-...{DEEPSEEK_KEY[-4:]}" if DEEPSEEK_KEY else "NOT SET (add DEEPSEEK_API_KEY to agent_test/.env)"
    print(f"[BOT] Workday Application Bot")
    print(f"[BOT] Fill mode  : {mode}")
    print(f"[BOT] DeepSeek   : {key_hint}")
    print(f"[BOT] Chrome     : {CHROME_PATH or '(Playwright bundled Chromium)'}")
    print(f"[BOT] Resume     : {RESUME_PATH}")
    print(f"[BOT] Job        : {job_url}")
    print(f"[BOT] Display    : {'headed (visible)' if headed else 'headless (background)'}\n")

    async with async_playwright() as pw:
        launch_kwargs = dict(
            headless=not headed,
            args=["--disable-blink-features=AutomationControlled","--no-first-run",
                  "--disable-web-security","--no-sandbox"])
        if CHROME_PATH:
            launch_kwargs["executable_path"] = CHROME_PATH
        browser = await pw.chromium.launch(**launch_kwargs)
        ctx = await browser.new_context(
            viewport={"width":1280,"height":900},
            user_agent=USER_AGENT)
        await ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        page = await ctx.new_page()

        print("[NAV] Loading job listing...")
        await page.goto(job_url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)

        await page.locator("[data-automation-id='adventureButton']").first.click()
        await page.wait_for_timeout(1500)

        am = page.locator("[data-automation-id='applyManually']").first
        href = await am.get_attribute("href")
        if href:
            await page.goto(href, wait_until="domcontentloaded", timeout=30000)
        else:
            await am.click(force=True)
        await page.wait_for_timeout(2500)

        if await page.locator("[data-automation-id='signInLink']").count():
            await sign_in(page)

        # Wait for application form to fully load (progress bar indicates form is ready)
        try:
            await page.wait_for_selector('[data-automation-id="progressBarActiveStep"]', timeout=25000)
        except Exception:
            pass
        await page.wait_for_timeout(1000)

        for page_num in range(1, 12):
            await page.wait_for_timeout(1500)
            heading = await get_heading(page)
            print(f"\n{'='*60}\n[PAGE {page_num}] {heading}\n{'='*60}")

            if not heading or "My Tasks" in heading:
                print("[BOT] ✓ Application complete."); break

            ss = ARTIFACTS / f"run_{page_num:02d}_{re.sub(r'[^a-zA-Z0-9]','_',heading)[:30]}.png"
            await page.screenshot(path=str(ss))

            # ── Detect page type by CONTENT, not heading text ──────────────────
            # This makes the bot work across different Workday tenants that use
            # different heading names ("Work History" vs "My Experience", etc.)
            has_add_buttons  = await page.locator("[data-automation-id='add-button']").count() > 0
            has_tc_checkbox  = await page.locator("[id*='termsAndConditions'],[data-automation-id='termsAndConditions']").count() > 0
            # Detect by heading keywords OR known DOM ids (covers any tenant naming)
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
                    print("[BOT] ✓ Application complete."); break

                elif is_review:
                    print("\n" + "="*60)
                    print("  ⚠️  REVIEW — all fields filled. Check the browser.")
                    print("="*60)
                    review_shot = str(ARTIFACTS / "review_page.png")
                    await page.screenshot(path=review_shot)
                    print(f"  Screenshot saved: {review_shot}")
                    try:
                        await asyncio.to_thread(input, "  → Press [Enter] to submit, Ctrl+C to abort: ")
                        await save_and_continue(page)
                        print("  ✓ Submitted!")
                    except (EOFError, KeyboardInterrupt):
                        print(f"  ⚠️  Non-interactive mode — NOT submitting. Review at {review_shot}")
                    break

                elif has_add_buttons:
                    # Experience/Education/Languages page — any tenant name
                    print(f"  → Detected as experience/education page (add-buttons present)")
                    await handle_my_experience(page)

                elif has_tc_checkbox:
                    # Voluntary Disclosures / T&C page — any tenant name
                    print(f"  → Detected as voluntary disclosures page (T&C checkbox present)")
                    await handle_voluntary_disclosures(page)

                elif has_self_id:
                    # Self Identification / EEO page — any tenant name
                    print(f"  → Detected as self-identify page")
                    await handle_self_identify(page)

                else:
                    # Generic page (My Information, Application Questions, custom pages)
                    # smart_fill_page + save handles all standard form-field pages
                    print(f"  → Generic form page — smart fill")
                    await smart_fill_page(page, heading)
                    ok = await save_and_continue(page)
                    if not ok:
                        # Validation revealed hidden fields (e.g. RH radio on My Information)
                        print("  [NAV] Re-scanning for newly visible fields after validation...")
                        await smart_fill_page(page, heading)
                        ok = await save_and_continue(page)
                    if not ok:
                        print("  [NAV] Still blocked — taking screenshot and breaking")
                        await page.screenshot(path=str(ARTIFACTS / f"blocked_{page_num:02d}.png"))
                        break

            except Exception as e:
                print(f"  [ERR] {e}")
                await page.screenshot(path=str(ARTIFACTS / f"error_p{page_num}.png"))
                break

        print("\n[BOT] Done.")
        await browser.close()

if __name__ == "__main__":
    # Windows requires ProactorEventLoop for Playwright subprocess communication
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    args = sys.argv[1:]
    headed = "--show" in args
    url_args = [a for a in args if not a.startswith("--")]
    job_url = url_args[0] if url_args else DEFAULT_JOB_URL
    asyncio.run(main(job_url, headed=headed))
