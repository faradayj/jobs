"""
app_common.py  —  Shared Application Bot Infrastructure
========================================================
Shared configuration, candidate library loading, DeepSeek evaluation,
and browser utilities.

Contains:
  - Library loading + profile constants
  - PROFILE_SUMMARY / SYSTEM_PROMPT for DeepSeek
  - deepseek_fill_page() / deepseek_pick_option()
  - label_match() / pick_decline()
  - launch_browser() / write_json_report() / scrape_salary()

No Playwright imports here — executors live in each applicator script
because DOM strategies differ between ATSes.
"""

import datetime, json, os, re, sys
from pathlib import Path
from dotenv import load_dotenv

# ── Platform UTF-8 fix ────────────────────────────────────────────────────────
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)


REPO_ROOT     = Path(__file__).resolve().parent.parent   # src/ → repo root
DATA_DIR      = REPO_ROOT / "data"
ARTIFACTS_DIR = REPO_ROOT / "artifacts"
load_dotenv(DATA_DIR / ".env")
from tracker_db import snooze_job_by_url, is_job_snoozed, load_snoozed_jobs

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

# Transcript: resolve to absolute path for set_input_files
_transcript_rel = LIBRARY.get("transcript_path", "data/transcript.pdf")
if _transcript_rel:
    _tp = Path(_transcript_rel) if Path(_transcript_rel).is_absolute() else REPO_ROOT / _transcript_rel
    TRANSCRIPT_PATH = str(_tp.resolve()) if _tp.exists() else ""
else:
    TRANSCRIPT_PATH = ""

# User-agent matching platform so pages render correctly
import platform as _plat
_ua_os = {
    "Darwin":  "Macintosh; Intel Mac OS X 10_15_7",
    "Windows": "Windows NT 10.0; Win64; x64",
    "Linux":   "X11; Linux x86_64",
}.get(_plat.system(), "Windows NT 10.0; Win64; x64")
USER_AGENT = (f"Mozilla/5.0 ({_ua_os}) AppleWebKit/537.36 "
              f"(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36")

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
    "personal_projects":        LIBRARY.get("personal_projects", []),
    "academic_capstone_projects": LIBRARY.get("projects", []),
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
- Suffix fields (e.g. 'Suffix', 'Name Suffix'): The candidate has NO suffix. ALWAYS select 'Select One' or leave empty. NEVER select 'II', 'III', 'IV', 'Jr.', etc.
- "I have a preferred name" checkbox: Unless explicitly asked to set a nickname, leave unchecked ('false') to avoid duplicate name validation issues on Workday; candidate legal name ('Joshua Li') is standard.
- "First Name" / "Last Name" / "Address" / "City" / "State" / "Zip" / "Postal Code" / "Country" (in personal/contact address section) → use personal_info residence (United States of America), NEVER job listing location.
- Address Line 1 and Line 2: If the form provides separate Address Line 1 and Address Line 2 (or Apt/Suite/Unit) fields, put '800 S Abel St' in Address Line 1 and 'Unit 506' in Address Line 2.
- "How did you hear" → "LinkedIn" or closest available option.
- Visa/sponsorship → use job_board_mappings.requires_visa_sponsorship.
- Salary/compensation → use job_listing_salary if set (pick the range option closest to it); otherwise use compensation_rules.baseline_target_pay.
- EEO fields (gender, sex, race, ethnicity, hispanic/latino, veteran, disability) → follow regulatory_self_identification rules:
    * Race / Ethnicity: ALWAYS pick the option representing "Decline to State", "I do not wish to self-identify", "Choose not to disclose", or "Decline to answer". Only if no decline option exists, fall back to "Asian".
    * Gender / Sex: ALWAYS pick the option representing "Decline to State", "I do not wish to self-identify", "Not declared", or "Prefer not to say". Only if no decline option exists, fall back to "Male".
    * Hispanic / Latino: pick "Decline to State" if available, otherwise "No".
    * Veteran status: match veteran_status_selection ("I am not a protected veteran").
    * Disability status: match disability_status_rule (affirmative / "Yes, I have a disability, or have had one in the past" on CC-305).
- Age 18+ / authorized to work → "Yes".
- Non-compete / prior employment at this company → "No" unless profile says otherwise.
- Security clearance: holds_security_clearance="No". NEVER select any option indicating a current clearance.
- Credit hours completed / anticipated towards degree: derive from current education_history (e.g. anticipated_completed_credit_hours).
- Student status / currently enrolled in degree program: "Yes" if any education_history entry is "Still attending", else "No".
- Current degree program: match the active education_history entry's degree_type / degree_abbreviation.
- Cumulative GPA: use GPA from active education_history (e.g. "3.5 or higher" / "4.0").
- Conditional major: use major_search_term or primary major_variant from active education_history.
- Conditional permanent address: use candidate's full personal_info address (street, unit, city, state, zip, country).
- PROSE & OPEN-ENDED TEXT WRITING RULES (STRICT COMPLIANCE REQUIRED):
  * Concise, direct, and active voice: Focus exclusively on what was built, how it was architected, and the measured outcome.
  * ZERO conversational preamble: NEVER start with phrases like "My favorite thing I've built is...", "I am drawn to this role because it sits at the intersection of...", "I would welcome the opportunity to...", "When thinking about...", or "As an engineer...". Start immediately with the action and subject (e.g. "Built an open-source LLM browser automation agent in Python...").
  * ABSOLUTELY NO EM DASHES OR EN DASHES: NEVER use '—' or '–' anywhere in your response. Use standard periods, commas, or semicolons instead.
  * ABSOLUTELY NO 'NOT X, BUT Y' / ANTITHESIS PHRASING: NEVER write sentences contrasting what something is NOT versus what it IS (e.g. avoid "not just X, but Y", "not in offline validation, but in production", "rather than just experimenting", "not from tuning in isolation, but from tight feedback loops"). State what was done directly, affirmatively, and objectively.
  * ZERO AI CLICHÉS OR MARKETING PUFFERY: Never use buzzwords such as 'testament', 'delve', 'tapestry', 'seamlessly', 'furthermore', 'spearheaded', 'poised', 'holistic', 'foster', 'beacon', 'leverage', 'passion', or 'thrilled'.
  * Concrete numbers & metrics: Ground claims in real measurements from candidate profile (e.g. 90K organizations, 91K edges, 93.5% precision, 99.4% recall, 89.3% runtime reduction, 38.8% GPU memory savings).
- Skills fields → use the skills array; pick the closest matching option from available choices.
- Language fields → use the languages array for language name and proficiency level.
- If filling a specific Work Experience / Education / Language entry, an "Entry Context" block
  will be provided — use ONLY that entry's data for fields in that dialog, not other entries.
- For selectinput/button-dropdown fields, your value must EXACTLY match one of the provided options.
- Skip navigation/search buttons and fields with no relevant data.
- If a field is already pre-filled:
  * For start date / availability: ALWAYS output the updated target date (today + 14 days, formatted as MM/DD/YYYY), never retain stale dates.
  * For visa sponsorship: ALWAYS output candidate's true answer (NEVER requires sponsorship: pick 'No' or 'I will not now or in the future require sponsorship...'). NEVER keep an affirmative sponsorship answer.
  * For other pre-filled fields: only provide an answer if the existing value is empty, invalid, or contradicts candidate profile.
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
- Ranked preference fields (e.g. '1st / 2nd / 3rd location preference', 'First / Second / Third engineering preference'): Each selection in the group MUST be distinct. NEVER repeat the same location or domain across choices. Follow the candidate's priority ladder in order (1st preference = #1 choice, 2nd preference = #2 choice, 3rd preference = #3 choice).
- Location checkboxes (multi-select, "select all that apply"): select ALL available options.
- Start/available date fields: use today's date from the "today" field plus 14 days (or role_preferences.anticipated_start_date_preference) to compute the target date (MM/DD/YYYY).
- For date spinbutton inputs (e.g. labels ending in '- Month', '- Day', '- Year', or labeled 'Month', 'Day', 'Year'):
  * Month: output 2-digit month string ('01' to '12')
  * Day: output 2-digit day string ('01' to '31')
  * Year: output 4-digit year string (e.g. '2026')
  * Semantically match the date to the question: graduation dates for graduation/degree questions, start date (today + 14 days) for availability/start questions, signature date (today's date) for disability/signature questions, and employment history dates for work experience questions.
  * For graduation date fields when Day is requested: use '15' for Day, and candidate's end_month / end_year for Month / Year.
- For single date text inputs (not broken into - Month / - Day / - Year subfields):
  * If placeholder specifies MM/DD/YYYY (or includes Day / DD): format as MM/DD/YYYY (for graduation date use candidate's end_month/15/end_year e.g. '12/15/2026'; for start date use today + 14 days).
  * If placeholder specifies MM/YYYY: format as MM/YYYY (e.g. '12/2026').
  * If no placeholder is specified: default to MM/DD/YYYY (or MM/YYYY if question only asks for month and year).
- Required conditional text/explanation fields (e.g. 'If Other, please explain:', 'Please specify', 'If yes, details'): If this field is marked required but the preceding question was NOT answered with 'Other' (or did not trigger the conditional need), enter 'N/A' so the field is not left blank and does not block form submission.
- For graduation date radio/dropdown questions (e.g. 'Projected graduation date?', 'When is your expected graduation date?'): Candidate graduates in December 2026. Select the option corresponding to December 2026 (e.g. '2026', 'December 2026', or 'Before spring 2027' if 2027 is the earliest next year option).
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
        raise RuntimeError("DEEPSEEK_API_KEY is required in data/.env.")
    profile = profile_override or PROFILE_SUMMARY
    field_lines = []
    for f in fields:
        line = f"[{f['index']}] type={f['type']} label={f['label']!r}"
        if f.get("date_seq"):    line += f" date_seq={f['date_seq']}"
        if f.get("options"):     line += f" options={f['options']}"
        if f.get("placeholder"): line += f" placeholder={f['placeholder']!r}"
        if f.get("maxlength"):   line += f" maxlength={f['maxlength']}"
        if f.get("value"):       line += f" current={f['value']!r}"
        if f.get("section"):     line += f" section={f['section']!r}"
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
        # Clean markdown code fences if present
        cleaned = re.sub(r'```(?:json)?\s*', '', raw, flags=re.I)
        cleaned = re.sub(r'```\s*$', '', cleaned).strip()

        def _sanitize(ans_list):
            for a in ans_list:
                if isinstance(a, dict) and "value" in a and isinstance(a["value"], str):
                    v = a["value"]
                    v = v.replace("—", ", ").replace("–", "-")
                    v = re.sub(r'\s*--\s*', ', ', v)
                    v = re.sub(r',\s*,', ',', v)
                    a["value"] = v.strip()
            return ans_list

        # Direct JSON parse first
        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, dict) and "answers" in parsed:
                return _sanitize(parsed["answers"])
            if isinstance(parsed, list):
                return _sanitize(parsed)
        except Exception:
            pass

        # Try raw_decode from first '[' or '{'
        idx_brace = cleaned.find('{')
        idx_bracket = cleaned.find('[')

        if idx_bracket != -1 and (idx_brace == -1 or idx_bracket < idx_brace):
            try:
                arr, _ = json.JSONDecoder().raw_decode(cleaned[idx_bracket:])
                if isinstance(arr, list):
                    return _sanitize(arr)
            except Exception:
                pass

        if idx_brace != -1:
            try:
                obj, _ = json.JSONDecoder().raw_decode(cleaned[idx_brace:])
                if isinstance(obj, dict):
                    if "answers" in obj:
                        return _sanitize(obj["answers"])
                    if "index" in obj and "value" in obj:
                        return _sanitize([obj])
                elif isinstance(obj, list):
                    return _sanitize(obj)
            except Exception:
                pass

        m = re.search(r'\{\s*"answers"\s*:\s*\[.*?\]\s*\}', cleaned, re.DOTALL)
        if m:
            return _sanitize(json.loads(m.group()).get("answers", []))
    except Exception as e:
        print(f"  [LLM] DeepSeek error: {e}")
    return []


async def deepseek_pick_option(label: str, options: list[str], current: str = "", context: str = "") -> str | None:
    """Ask DeepSeek to pick the exact matching option for a dropdown or radio question from available options.
    Returns the exact option string from `options` or None."""
    if not DEEPSEEK_KEY:
        raise RuntimeError("DEEPSEEK_API_KEY is required in data/.env.")
    if not options:
        return None

    clean_opts = [o for o in options if o and o.lower() not in ("select one", "select", "")]
    if not clean_opts:
        return None
    if len(clean_opts) == 1:
        return clean_opts[0]

    prompt = (
        f"Candidate Profile:\n{PROFILE_SUMMARY}\n\n"
        f"Context: {context}\n"
        f"Form Question: {label}\n"
        f"Available Options:\n"
        + "\n".join(f"- {o}" for o in clean_opts)
        + "\n\nTask: Pick the single option that accurately and truthfully represents the candidate.\n"
        "Output ONLY the exact option string verbatim. No explanation, no quotes, no markdown."
    )
    try:
        import httpx
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(
                "https://api.deepseek.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {DEEPSEEK_KEY}"},
                json={
                    "model": "deepseek-chat",
                    "temperature": 0.0,
                    "max_tokens": 200,
                    "messages": [
                        {"role": "system", "content": "You are a precise job application form evaluator. Output ONLY the exact matching option string verbatim from the provided list."},
                        {"role": "user", "content": prompt}
                    ]
                }
            )
        chosen = r.json()["choices"][0]["message"]["content"].strip()
        chosen = re.sub(r'^["\'`]+|["\'`]+$', '', chosen).strip()
        # 1. Exact match
        for o in clean_opts:
            if o.strip().lower() == chosen.lower():
                return o
        # 2. Match without punctuation/whitespace
        ch_norm = re.sub(r'[^a-z0-9]', '', chosen.lower())
        for o in clean_opts:
            if re.sub(r'[^a-z0-9]', '', o.lower()) == ch_norm:
                return o
    except Exception as e:
        print(f"  [LLM] deepseek_pick_option error: {e}")
    return None


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

# ── Shared browser/IO helpers ─────────────────────────────────────────────────

async def launch_browser(p, headed: bool, extra_args: list = None, extra_headers: dict = None, storage_state: str = None):
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
    ctx_kwargs = dict(viewport={"width": 1280, "height": 900}, user_agent=USER_AGENT, locale="en-US")
    if storage_state and Path(storage_state).exists():
        ctx_kwargs["storage_state"] = str(storage_state)
    headers = {"Accept-Language": "en-US,en;q=0.9"}
    if extra_headers:
        headers.update(extra_headers)
    ctx_kwargs["extra_http_headers"] = headers
    context = await browser.new_context(**ctx_kwargs)
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
    """Extract salary midpoint from job-description text. Returns str like '145000' or None."""
    if not text:
        return None

    # 1. Range match: $XX,XXX - $YY,YYY or $XX - $YY /hr
    range_patterns = [
        r'\$([\d,]+(?:\.\d{1,2})?)\s*(?:[-–—]|to)\s*\$([\d,]+(?:\.\d{1,2})?)(?:\s*(?:/\s*(?:hr|hour|yr|year)|per\s+(?:hour|hr|year|yr)|annually|hourly))?',
        r'(?:salary|compensation|pay|rate)\s*(?:range)?:?\s*\$([\d,]+(?:\.\d{1,2})?)\s*(?:[-–—]|to)\s*\$([\d,]+(?:\.\d{1,2})?)'
    ]
    for pat in range_patterns:
        m = re.search(pat, text, re.I)
        if m:
            try:
                lo_f = float(m.group(1).replace(",", ""))
                hi_f = float(m.group(2).replace(",", ""))
                is_hourly = ("hr" in m.group(0).lower() or "hour" in m.group(0).lower() or (lo_f < 300 and hi_f < 300))
                if is_hourly and 15 <= lo_f <= 250:
                    mid = ((lo_f + hi_f) / 2.0) * 2080
                    return str(int(round(mid)))
                elif 30000 <= lo_f <= 600000 and 30000 <= hi_f <= 600000:
                    return str(int(round((lo_f + hi_f) / 2.0)))
            except Exception:
                pass

    # 2. Single figure with explicit annual salary context
    single_patterns = [
        r'(?:base\s+salary|target\s+salary|annual\s+salary|base\s+pay|compensation)\s*(?:is|of|:)?\s*\$([\d,]+)',
        r'\$([\d,]+)\s*(?:per\s+year|/yr|annually)'
    ]
    for pat in single_patterns:
        m = re.search(pat, text, re.I)
        if m:
            try:
                val = int(m.group(1).replace(",", ""))
                if 30000 <= val <= 600000:
                    return str(val)
            except Exception:
                pass

    return None
