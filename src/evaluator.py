"""
evaluator.py - Job matching and LLM evaluation pipeline.
Evaluates candidate suitability using DeepSeek API against the profile in library.json,
prunes non-US/Canada and low-match (score >= 3) listings into filtered_history.json.
"""

import os
import json
import re
import datetime
import httpx
from dataclasses import dataclass
from pathlib import Path

from tracker_db import (
    DATA_DIR, DB_PATH, PROFILE_PATH, get_db, export_db_to_csv,
    record_filtered_jobs
)
from scraper import (
    fetch_job_description, is_us_or_canada, EXPIRED_INDICATORS
)

@dataclass
class JobEvaluation:
    score: int
    suitability_reason: str
    salary_midpoint: int | None = None
    salary_raw: str | None = None
    is_remote: bool = False
    standardized_locations: list[dict] = None
    ordered_locations: list[str] = None
    target_skills: list[str] = None
    eval_metadata: dict = None

SCORING_RUBRIC = """Scoring Criteria — score 1, 2, or 3:
- Score 1 (High Priority / Strong Match): target titles (SWE/DS/MLE/DE), degree <= Master's in CS/DS, 0-3 yrs experience, multiple skill matches. (DO NOT give Score 1 if >3 years experience is required!)
- Score 2 (Medium Priority): requires >3 years of experience, OR major/experience aligns but missing some non-critical skills.
- Score 3 (Low Priority / Ineligible): requires PhD, unrelated background, OR requires a letter of recommendation / letter of reference / writing sample (candidate cannot provide these).
"""

EVAL_OUTPUT_SCHEMA = {
    "score": "1 | 2 | 3",
    "suitability_reason": "+match1 +match2 | -gap1 -gap2 (max 80 chars, compact tag format, no prose)",
    "salary": {
        "has_salary": "true | false",
        "raw": "raw string like '$110,000 - $140,000' or null",
        "min": "integer or null",
        "max": "integer or null",
        "midpoint": "integer or null",
        "is_hourly": "true | false"
    },
    "locations": [
        {
            "city": "City name or 'NA'",
            "state": "State code like 'CA' or 'NA'",
            "country": "United States | Canada | other",
            "raw": "raw string from posting",
            "is_remote": "true | false"
        }
    ],
    "is_remote": "true | false",
    "skills": {
        "library_matches": ["relevant skills from candidate library"],
        "explicit_jd_skills": ["key skills explicitly required in job description"]
    }
}

EVAL_PENDING_PATH = Path(__file__).resolve().parent.parent / "artifacts" / "eval_pending.json"
EVAL_SCORES_PATH = Path(__file__).resolve().parent.parent / "artifacts" / "eval_scores.json"

def rank_and_order_locations(locations: list[dict], is_remote: bool, profile_data: dict) -> list[str]:
    """Rank physical locations by income/priority ladder, then append remote preference ladder."""
    default_income_order = [
        "san francisco", "san jose", "bay area", "palo alto", "sunnyvale", "mountain view",
        "seattle", "bellevue", "new york", "nyc", "manhattan", "boston", "cambridge",
        "denver", "boulder", "los angeles", "san diego", "austin", "washington", "arlington",
        "chicago", "dallas", "phoenix", "tempe", "scottsdale"
    ]

    physical_loc_strings = []
    for loc in (locations or []):
        if loc.get("is_remote"):
            continue
        c = (loc.get("city") or "").strip()
        s = (loc.get("state") or "").strip()
        co = (loc.get("country") or "United States").strip()
        if not c and not s:
            continue
        formatted = f"{c}, {s}, {co}" if c and c.upper() != "NA" else f"{s}, {co}"
        if formatted not in physical_loc_strings:
            physical_loc_strings.append(formatted)

    def _rank_physical(loc_str: str) -> int:
        l_lower = loc_str.lower()
        for idx, key in enumerate(default_income_order):
            if key in l_lower:
                return idx
        return 999  # other physical location

    sorted_physical = sorted(physical_loc_strings, key=_rank_physical)

    remote_defaults = [
        "San Jose, CA, United States",
        "San Francisco, CA, United States",
        "Phoenix, AZ, United States",
        "New York, NY, United States",
        "Seattle, WA, United States",
        "Remote"
    ]

    result = []
    # 1. Physical options first (highest to lowest income)
    for p in sorted_physical:
        if p not in result:
            result.append(p)

    # 2. If listing is remote or if no physical locations were found, append remote ladder
    if is_remote or not result:
        for r in remote_defaults:
            if r not in result:
                result.append(r)

    return result

def extract_and_filter_skills(skills_obj: dict, library_skills: list[str]) -> list[str]:
    """Combine library matches and explicit JD skills into a trimmed, highly-relevant list."""
    lib_matches = skills_obj.get("library_matches", []) if isinstance(skills_obj, dict) else []
    jd_skills = skills_obj.get("explicit_jd_skills", []) if isinstance(skills_obj, dict) else []

    target = []
    # Add valid library matches
    for s in lib_matches:
        if s and s not in target:
            target.append(s)

    # Add explicit JD skills
    for s in jd_skills:
        if s and s not in target:
            target.append(s)

    # Fallback to top library skills if empty
    if not target and library_skills:
        target = library_skills[:8]

    # Limit to top 12 skills max so we avoid slow 50-item loops
    return target[:12]

async def evaluate_job_with_llm(api_key: str, profile_data: dict, job_description: str) -> JobEvaluation:
    """Evaluate candidate fit against job description using DeepSeek chat API with structured schema."""
    candidate_skills = profile_data.get("skills", [])

    system_prompt = f"""You are an expert technical recruiter and job evaluation engine.
Evaluate the Job Description against the Candidate Profile and available Skills Inventory.

Candidate Profile Summary:
- Target Titles: {profile_data.get('role_preferences', {}).get('target_titles', [])}
- Education: {[{'school': e.get('institution_variants',[None])[0], 'degree': e.get('degree_type'), 'major': e.get('major_search_term')} for e in profile_data.get('education_history', [])]}
- Experience: {[{'company': w.get('company'), 'title': w.get('role'), 'years': f"{w.get('start_year')}-{w.get('end_year')}"} for w in profile_data.get('work_experience', [])]}

Candidate Library Skills:
{json.dumps(candidate_skills, indent=2)}

TASK & OUTPUT REQUIREMENTS:
Output ONLY a valid JSON object matching this schema:
{json.dumps(EVAL_OUTPUT_SCHEMA, indent=2)}

CRITICAL EVALUATION RULES:
1. Scoring:
   - Score 1: SWE/DS/MLE/DE, degree <= MS, 0-3 yrs required experience, strong skill alignment. (DO NOT give Score 1 if >3 years required!)
   - Score 2: requires >3 years experience, or slight skill gaps.
   - Score 3: requires PhD, completely unrelated field, or requires letters of recommendation / writing sample.

2. Suitability Reason:
   "+<match1> +<match2> ... | -<gap1> -<gap2> ..." (max 80 chars, compact tag format). No prose.

3. Salary Extraction:
   - Extract actual employee compensation (salary range or hourly wage).
   - CRITICAL: Ignore company revenues, corporate funding, footnotes, copays, or isolated small numbers (e.g. "$1B in revenue", "$1 copay").
   - If no employee compensation is listed, set "has_salary": false, and "raw", "min", "max", "midpoint" to null.
   - If hourly, set "is_hourly": true, and compute annual midpoint as ((min + max) / 2) * 2080.
   - If annual, compute midpoint as round((min + max) / 2). Midpoint must be a realistic salary ($30,000 - $600,000).

4. Location Standardization:
   - Extract city, state/province, country, and whether the job is remote/hybrid.
   - If city is given: {{"city": "City", "state": "ST", "country": "United States" or "Canada"}}.
   - If only state: {{"city": "NA", "state": "ST", "country": "Country"}}.
   - If Canadian city: {{"city": "City", "state": "Province or NA", "country": "Canada"}}.
   - If remote: set "is_remote": true.
   - List all distinct locations mentioned.

5. Skills Prefiltering:
   - "library_matches": Select ONLY the 3-10 skills from Candidate Library Skills that are actually relevant or mentioned in this job description.
   - "explicit_jd_skills": List any key technologies or frameworks explicitly required by the job that should be tested/filled.
"""

    async with httpx.AsyncClient(timeout=45) as client:
        r = await client.post(
            "https://api.deepseek.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": "deepseek-chat",
                "temperature": 0.0,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"Job Description:\n{job_description}"}
                ],
                "response_format": {"type": "json_object"}
            }
        )
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"].strip()

    if content.startswith("```"):
        content = re.sub(r"^```[a-z]*\n?", "", content)
        content = re.sub(r"\n?```$", "", content)
    content = content.strip()

    try:
        data = json.loads(content)
    except Exception:
        data = {}

    score_val = int(data.get("score", 3)) if str(data.get("score", "")).isdigit() else 3
    if score_val not in (1, 2, 3):
        score_val = 3

    raw_reason = str(data.get("suitability_reason", ""))
    suitability_reason = raw_reason[:80].rstrip() if raw_reason else "(no detail)"

    # Salary validation
    salary_data = data.get("salary") or {}
    salary_midpoint = None
    salary_raw = None
    if isinstance(salary_data, dict) and salary_data.get("has_salary"):
        mid = salary_data.get("midpoint")
        if mid and isinstance(mid, (int, float)) and 30000 <= mid <= 600000:
            salary_midpoint = int(round(mid))
            salary_raw = str(salary_data.get("raw") or f"${salary_midpoint}")

    # Location & remote processing
    locations = data.get("locations") or []
    is_remote = bool(data.get("is_remote", False))
    ordered_locs = rank_and_order_locations(locations, is_remote, profile_data)

    # Skills filtering
    skills_obj = data.get("skills") or {}
    target_skills = extract_and_filter_skills(skills_obj, candidate_skills)

    eval_metadata = {
        "score": score_val,
        "suitability_reason": suitability_reason,
        "salary_midpoint": salary_midpoint,
        "salary_raw": salary_raw,
        "is_remote": is_remote,
        "standardized_locations": locations,
        "ordered_locations": ordered_locs,
        "target_skills": target_skills,
    }

    return JobEvaluation(
        score=score_val,
        suitability_reason=suitability_reason,
        salary_midpoint=salary_midpoint,
        salary_raw=salary_raw,
        is_remote=is_remote,
        standardized_locations=locations,
        ordered_locations=ordered_locs,
        target_skills=target_skills,
        eval_metadata=eval_metadata
    )

async def _evaluate_single_job(conn, job: tuple, profile_data: dict, api_key: str | None, dry_run: bool) -> tuple[bool, bool]:
    """Scrape and evaluate a single pending job row."""
    job_id, company, role, location, apply_url, db_desc = job
    cursor = conn.cursor()

    print(f"\n[*] {'Scraping' if dry_run else 'Evaluating'}: {company} — {role}...")
    print(f"    Location: {location}")
    print(f"    Link: {apply_url}")

    if db_desc and len(db_desc) >= 300:
        job_desc = db_desc
        print(f"    [+] Using existing description from database ({len(job_desc)} chars).")
    else:
        job_desc = await fetch_job_description(apply_url)

    if not job_desc or len(job_desc) < 300:
        is_expired = bool(job_desc and any(term in job_desc.lower() for term in EXPIRED_INDICATORS))
        if is_expired:
            print("    [+] Expired listing detected. Removing from tracker and saving to filtered_history.json.")
            if not dry_run:
                record_filtered_jobs({apply_url: {
                    "reason": "Closed (Expired)",
                    "company": company,
                    "role": role
                }})
                cursor.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
                conn.commit()
            return False, False
        else:
            print("    [!] Fetch failed or too short.")
            if dry_run:
                return False, True
            else:
                cursor.execute("UPDATE jobs SET status = 'Fetch Failed / Manual Review' WHERE id = ?", (job_id,))
                conn.commit()
            return False, False

    print(f"    [+] Successfully fetched description ({len(job_desc)} chars).")

    if dry_run:
        preview = job_desc[:400].replace('\n', ' ')
        print(f"    [PREVIEW] {preview}...")
        print(f"    [DRY-RUN] Scrape OK — DeepSeek would receive {len(job_desc)} chars of context.")
        return True, False

    print("    Evaluating with DeepSeek...")
    try:
        eval_result = await evaluate_job_with_llm(api_key, profile_data, job_desc)

        if not is_us_or_canada(location):
            record_filtered_jobs({apply_url: {
                "reason": "Ineligible (Non-US/Canada)",
                "company": company,
                "role": role
            }})
            cursor.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            conn.commit()
            print(f"    [+] Ineligible (Non-US/Canada) -> Removed from tracker and saved to filtered_history.json.")
            return False, False
        elif eval_result.score >= 3:
            record_filtered_jobs({apply_url: {
                "reason": f"Ineligible: {eval_result.suitability_reason}",
                "company": company,
                "role": role
            }})
            cursor.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            conn.commit()
            print(f"    [+] Ineligible (Score 3) -> Removed from tracker and saved to filtered_history.json.")
            return False, False
        else:
            status_str = f"Eligible (Priority {eval_result.score})"
            score_val = eval_result.score

        eval_meta_json = json.dumps(eval_result.eval_metadata) if eval_result.eval_metadata else None
        target_skills_str = ", ".join(eval_result.target_skills) if eval_result.target_skills else None
        cursor.execute("""
            UPDATE jobs
            SET status = ?,
                score = ?,
                suitability_reason = ?,
                job_description = ?,
                eval_metadata = ?,
                salary_midpoint = ?,
                target_skills = ?,
                date_evaluated = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (status_str, score_val, eval_result.suitability_reason, job_desc,
              eval_meta_json, eval_result.salary_midpoint, target_skills_str, job_id))
        conn.commit()
        print(f"    [+] Evaluated: Score = {score_val} ({status_str})")
        print(f"    [+] Reason: {eval_result.suitability_reason}")
        if eval_result.salary_midpoint:
            print(f"    [+] Salary: ${eval_result.salary_midpoint:,} ({eval_result.salary_raw})")
        else:
            print(f"    [+] Salary: NA (no comp listed)")
        if eval_result.ordered_locations:
            print(f"    [+] Locations: {eval_result.ordered_locations[:3]}{'...' if len(eval_result.ordered_locations)>3 else ''}")
        if eval_result.target_skills:
            print(f"    [+] Prefiltered Skills ({len(eval_result.target_skills)}): {eval_result.target_skills}")
    except Exception as e:
        print(f"    [!] Error during evaluation: {e}")
        cursor.execute("UPDATE jobs SET status = 'Evaluation Error' WHERE id = ?", (job_id,))
        conn.commit()

    return False, False

async def run_evaluate(limit: int = 10, dry_run: bool = False):
    """Evaluate pending jobs from the database using DeepSeek."""
    if dry_run:
        print("[DRY-RUN] Scraping job descriptions only — DeepSeek LLM calls SKIPPED.")
        api_key = None
    else:
        api_key = os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            print("[ERROR] No DEEPSEEK_API_KEY found. Check data/.env file.")
            return

    if not PROFILE_PATH.exists():
        print(f"[ERROR] Candidate profile not found at {PROFILE_PATH}.")
        return

    with open(PROFILE_PATH, "r", encoding="utf-8") as f:
        profile_data = json.load(f)

    conn = get_db()
    cursor = conn.cursor()

    if limit == -1:
        cursor.execute("SELECT id, company, role, location, apply_url, job_description FROM jobs WHERE status = 'Pending Evaluation'")
    else:
        cursor.execute("SELECT id, company, role, location, apply_url, job_description FROM jobs WHERE status = 'Pending Evaluation' LIMIT ?", (limit,))

    pending_jobs = cursor.fetchall()
    if not pending_jobs:
        print("[*] No pending jobs to evaluate.")
        conn.close()
        return

    print(f"[*] Starting {'dry-run scrape' if dry_run else 'evaluation'} for {len(pending_jobs)} pending jobs...")

    dry_run_ok = 0
    dry_run_fail = 0
    for job in pending_jobs:
        ok, fail = await _evaluate_single_job(conn, job, profile_data, api_key, dry_run)
        if ok:
            dry_run_ok += 1
        if fail:
            dry_run_fail += 1

    conn.close()
    if dry_run:
        print(f"\n[DRY-RUN] Done. Scraped OK: {dry_run_ok}, Failed: {dry_run_fail}")
    else:
        print("\n[+] Evaluation complete.")
        export_db_to_csv()

async def run_export_prompts(limit: int = -1):
    """Scrape pending jobs and export to artifacts/eval_pending.json for offline scoring."""
    if not PROFILE_PATH.exists():
        print(f"[ERROR] Candidate profile not found at {PROFILE_PATH}.")
        return

    with open(PROFILE_PATH, "r", encoding="utf-8") as f:
        profile_data = json.load(f)

    conn = get_db()
    cursor = conn.cursor()

    if limit == -1:
        cursor.execute("SELECT id, company, role, location, apply_url, job_description FROM jobs WHERE status = 'Pending Evaluation'")
    else:
        cursor.execute("SELECT id, company, role, location, apply_url, job_description FROM jobs WHERE status = 'Pending Evaluation' LIMIT ?", (limit,))

    pending_jobs = cursor.fetchall()
    if not pending_jobs:
        print("[EXPORT] No pending jobs to scrape.")
        conn.close()
        return

    print(f"[EXPORT] Scraping {len(pending_jobs)} pending jobs...")
    exported = []
    for job in pending_jobs:
        job_id, company, role, location, apply_url, db_desc = job
        print(f"  Scraping [{job_id}] {company} — {role}...")
        try:
            job_desc = await fetch_job_description(apply_url)
        except Exception as exc:
            print(f"    [ERROR] Scrape exception: {exc}")
            cursor.execute("UPDATE jobs SET status='Fetch Failed / Manual Review' WHERE id=?", (job_id,))
            conn.commit()
            continue

        if not job_desc or len(job_desc) < 300:
            is_expired = bool(job_desc and any(t in job_desc.lower() for t in EXPIRED_INDICATORS))
            if is_expired:
                print("    [EXPIRED] Marking Closed (Expired).")
                record_filtered_jobs({apply_url: {"reason": "Closed (Expired)", "company": company, "role": role}})
                cursor.execute("DELETE FROM jobs WHERE id=?", (job_id,))
                conn.commit()
            else:
                cursor.execute("UPDATE jobs SET status='Fetch Failed / Manual Review' WHERE id=?", (job_id,))
                conn.commit()
            continue

        exported.append({
            "id": job_id,
            "company": company,
            "role": role,
            "location": location,
            "apply_url": apply_url,
            "job_description": job_desc,
        })

    conn.close()
    export_db_to_csv()

    EVAL_PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated": datetime.datetime.now().isoformat(),
        "rubric": SCORING_RUBRIC,
        "output_schema": SCORING_OUTPUT_SCHEMA,
        "profile": profile_data,
        "jobs": exported,
    }
    with open(EVAL_PENDING_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    print(f"\n[EXPORT] {len(exported)} jobs written to '{EVAL_PENDING_PATH}'.")

def run_import_scores():
    """Read artifacts/eval_scores.json and write results into DB and CSV."""
    if not EVAL_SCORES_PATH.exists():
        print(f"[ERROR] Scores file not found: {EVAL_SCORES_PATH}")
        return

    with open(EVAL_SCORES_PATH, "r", encoding="utf-8") as f:
        scores = json.load(f)

    if not isinstance(scores, list):
        print("[ERROR] eval_scores.json must be a JSON array of score objects.")
        return

    conn = get_db()
    cursor = conn.cursor()

    imported = 0
    skipped = 0
    for entry in scores:
        apply_url = entry.get("apply_url", "")
        job_id = entry.get("id")

        row = None
        if apply_url:
            cursor.execute("SELECT id, company, role, location FROM jobs WHERE apply_url=?", (apply_url,))
            row = cursor.fetchone()
        if not row and job_id:
            cursor.execute("SELECT id, company, role, location FROM jobs WHERE id=?", (job_id,))
            row = cursor.fetchone()

        if not row:
            skipped += 1
            continue

        db_id, company, role, location = row
        try:
            score_val = int(entry.get("score", 3))
            if score_val not in (1, 2, 3):
                score_val = 3
        except Exception:
            score_val = 3

        raw_reason = str(entry.get("suitability_reason", ""))
        reason = raw_reason[:80].rstrip() if raw_reason else "(no detail)"

        if not is_us_or_canada(location):
            record_filtered_jobs({apply_url: {"reason": "Ineligible (Non-US/Canada)", "company": company, "role": role}})
            cursor.execute("DELETE FROM jobs WHERE id=?", (db_id,))
            conn.commit()
            imported += 1
            continue
        elif score_val >= 3:
            record_filtered_jobs({apply_url: {"reason": f"Ineligible: {reason}", "company": company, "role": role}})
            cursor.execute("DELETE FROM jobs WHERE id=?", (db_id,))
            conn.commit()
            imported += 1
            continue

        status_str = f"Eligible (Priority {score_val})"
        cursor.execute("""
            UPDATE jobs
            SET status=?, score=?, suitability_reason=?, date_evaluated=CURRENT_TIMESTAMP
            WHERE id=?
        """, (status_str, score_val, reason, db_id))
        conn.commit()
        imported += 1

    conn.close()
    export_db_to_csv()
    print(f"\n[IMPORT] Done. Imported: {imported}, Skipped: {skipped}.")
