"""
Simplify Jobs Aggregator & Match Tracker
=========================================
Tracks, evaluates, and applies to new-grad software jobs scraped from the
SimplifyJobs/New-Grad-Positions GitHub repository.

TYPICAL WORKFLOW
----------------
  1. python3 job_tracker.py ingest
       Pull latest job list, mark closed/removed jobs.

  2. python3 job_tracker.py evaluate --limit -1
       Scrape + score pending jobs with DeepSeek (requires DEEPSEEK_API_KEY in .env).
       Use --dry-run to validate scraping WITHOUT calling DeepSeek.

  3. python3 job_tracker.py list --priority 1
       Show all Priority-1 (strongest match) eligible jobs.

  4. python3 job_tracker.py apply-loop
       Interactively launch the Workday / Greenhouse bot for each eligible job.

COMMANDS
--------
  ingest        Download latest README from SimplifyJobs, insert new jobs,
                mark removed/closed listings.

  evaluate      Scrape job descriptions + score fit with DeepSeek.
    --limit N       Jobs to process (default 10, -1 = all pending).
    --dry-run       Scrape only — skip DeepSeek calls (no API key needed).

  status        Print count summary grouped by status.

  list          List eligible jobs sorted by fit score.
    --priority 1|2|3  Filter by priority score (default 1 = strongest match).

  apply         Mark a single job as Applied.
    --id N          Job ID to mark (required).

  apply-loop    Step through eligible jobs and optionally launch the applicator.

  csv           Re-export DB → jobs_tracker.csv + jobs_details.json.

OUTPUTS
-------
  jobs_tracker.csv    Apply URL first column, then Company / Role / Status / etc.
  jobs_details.json   Full scraped job descriptions per URL (avoids re-scraping on ingest).
"""

import os
import sys
import json
import sqlite3
import re
import asyncio
import argparse
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from playwright.async_api import async_playwright

# Import shared Chrome-path helper from app_common
sys.path.insert(0, str(Path(__file__).parent))
from app_common import _find_chrome, DATA_DIR

# Force UTF-8 encoding on standard output/error
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True)
else:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

# Load environment variables from .env file
env_path = DATA_DIR / ".env"
load_dotenv(dotenv_path=env_path)

DB_PATH = DATA_DIR / "jobs.db"
PROFILE_PATH = DATA_DIR / "library.json"  # candidate profile (library.json)
README_URL = "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/README.md"

# --- 1. Data Classes ---

@dataclass
class JobEvaluation:
    score: int
    suitability_reason: str

# --- 1b. Module-level constants ---

EXPIRED_INDICATORS = [
    "page not found", "job not found", "job is no longer available",
    "no longer accepting applications", "this job is closed",
    "the page you are looking for doesn't exist", "link you followed may be broken",
    "couldn't find that page", "couldn’t find that page", "could not find that page",
    "job has been filled", "position has been filled",
    "no longer accepting", "job listing is no longer", "this job is no longer",
    "posting has expired", "job has expired", "position is no longer available",
    "successfully closed", "not currently accepting", "opening has been filled",
]

# --- 2. Database Helpers ---

SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company TEXT,
        role TEXT,
        location TEXT,
        apply_url TEXT UNIQUE,
        category TEXT,
        status TEXT,
        score INTEGER,
        suitability_reason TEXT,
        job_description TEXT,
        date_added DATETIME DEFAULT CURRENT_TIMESTAMP,
        date_evaluated DATETIME,
        date_applied DATETIME
    )
"""

def ensure_schema(conn):
    conn.cursor().execute(SCHEMA_SQL)
    conn.commit()

def init_db():
    conn = sqlite3.connect(DB_PATH)
    ensure_schema(conn)
    return conn

def import_csv_to_db():
    """Import the jobs_tracker.csv and jobs_details.json files into the SQLite database."""
    csv_path = DATA_DIR / "jobs_tracker.csv"
    details_path = DATA_DIR / "jobs_details.json"
    
    if not csv_path.exists():
        return
        
    # Load details from JSON if it exists
    details_cache = {}
    if details_path.exists():
        try:
            with open(details_path, "r", encoding="utf-8") as f:
                details_cache = json.load(f)
        except Exception as e:
            print(f"[!] Warning: Failed to load jobs_details.json: {e}")
            
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Re-initialize the database schema
    ensure_schema(conn)
    
    import csv
    with open(csv_path, "r", newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        headers = next(reader, None)
        if not headers:
            conn.close()
            return
            
        header_map = {h: i for i, h in enumerate(headers)}
        
        for row in reader:
            if not row:
                continue
                
            def get_val(col_name, default=None):
                if col_name in header_map and header_map[col_name] < len(row):
                    return row[header_map[col_name]]
                return default

            job_id_str = get_val("ID")
            job_id = int(job_id_str) if job_id_str and job_id_str.isdigit() else None
            
            company = get_val("Company")
            role = get_val("Role")
            location = get_val("Location")
            category = get_val("Category")
            status = get_val("Status")
            
            score_str = get_val("Score")
            score = int(score_str) if score_str and score_str.isdigit() else None

            date_added = get_val("Date Added")
            date_evaluated = get_val("Date Evaluated")
            date_applied = get_val("Date Applied")
            suitability_reason = get_val("Suitability Reason")
            apply_url = get_val("Apply URL")

            # Retrieve description from either the CSV (if present for legacy reasons) or JSON cache
            job_description = get_val("Job Description")
            if not job_description and apply_url in details_cache:
                job_description = details_cache[apply_url].get("job_description", "")
            job_description = job_description or ""

            cursor.execute("""
                INSERT OR REPLACE INTO jobs (
                    id, company, role, location, apply_url, category, status, score,
                    suitability_reason, job_description,
                    date_added, date_evaluated, date_applied
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                job_id, company, role, location, apply_url, category, status, score,
                suitability_reason, job_description, date_added, date_evaluated,
                date_applied
            ))
            
    conn.commit()
    conn.close()
    print(f"[+] DB Update: Imported all jobs from '{csv_path}'.")

# --- 3. URL and HTML Scraping Helpers ---

def clean_url(url):
    """Remove simplify tracking parameters from a URL."""
    try:
        parsed = urlparse(url)
        qsl = parse_qsl(parsed.query)
        cleaned_qsl = []
        tracking_params = {'utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content', 'ref', 'ref_id', 'click_id'}
        for k, v in qsl:
            if k.lower() not in tracking_params:
                cleaned_qsl.append((k, v))
        cleaned_query = urlencode(cleaned_qsl)
        return urlunparse(parsed._replace(query=cleaned_query))
    except Exception:
        return url

def extract_apply_url(td):
    """Extract the real application link from an HTML table cell."""
    links = td.find_all('a')
    for a in links:
        href = a.get('href', '')
        if 'simplify.jobs/p/' in href or 'simplify.jobs/apply' in href:
            continue
        img = a.find('img')
        if img and img.get('alt', '').lower() == 'apply':
            return href
        if href and 'simplify.jobs' not in href:
            return href
    if links:
        return links[0].get('href', '')
    return ''

def parse_jobs_from_markdown(content):
    """Parse jobs out of the HTML tables contained inside the README Markdown."""
    # Split content by ## to find sections
    sections = re.split(r'^##\s+', content, flags=re.MULTILINE)
    jobs = []
    
    for section in sections:
        lines = section.split('\n')
        if not lines:
            continue
        title = lines[0].strip()
        
        category = None
        if 'software engineering' in title.lower():
            category = 'SWE'
        elif 'data science' in title.lower() or 'machine learning' in title.lower() or 'ai' in title.lower():
            category = 'DS_ML'
        elif 'hardware engineering' in title.lower():
            category = 'Hardware'
        elif 'product management' in title.lower():
            category = 'PM'
        elif 'quantitative finance' in title.lower():
            category = 'Quant'
            
        if not category:
            continue
            
        # Parse the HTML tables in this section
        table_matches = re.findall(r'<table.*?>.*?</table>', section, re.DOTALL)
        for table_html in table_matches:
            soup = BeautifulSoup(table_html, 'html.parser')
            rows = soup.find_all('tr')
            
            previous_company = None
            for row in rows:
                cells = row.find_all('td')
                if len(cells) < 4:
                    continue # header or empty row
                
                # Company name
                company_text = cells[0].get_text(strip=True)
                if '↳' in company_text or company_text == '↳':
                    company = previous_company
                else:
                    a_comp = cells[0].find('a')
                    if a_comp:
                        company = a_comp.get_text(strip=True)
                    else:
                        company = company_text
                    previous_company = company
                
                # Role title
                role = cells[1].get_text(strip=True)
                
                # Location details
                location = cells[2].get_text(separator=', ', strip=True)
                
                # Apply URL
                apply_url = extract_apply_url(cells[3])
                
                if apply_url:
                    apply_url = clean_url(apply_url)
                    jobs.append({
                        'company': company,
                        'role': role,
                        'location': location,
                        'apply_url': apply_url,
                        'category': category,
                        'is_closed': '🔒' in row.get_text()
                    })
    return jobs

# --- 4. Playwright Scraper and LLM Evaluator ---

def is_us_or_canada(location: str) -> bool:
    """Check if the job location is within the United States or Canada."""
    if not location:
        return True  # If location is empty or missing, assume eligible/unrestricted
    
    loc_lower = location.lower()
    
    # Check for explicit countries
    if "united states" in loc_lower or "usa" in loc_lower or "canada" in loc_lower:
        return True
    
    # Standalone state abbreviations (US) and province abbreviations (Canada)
    us_states = [
        'AL', 'AK', 'AZ', 'AR', 'CA', 'CO', 'CT', 'DE', 'FL', 'GA', 'HI', 'ID', 'IL', 'IN', 'IA', 'KS', 'KY', 'LA', 
        'ME', 'MD', 'MA', 'MI', 'MN', 'MS', 'MO', 'MT', 'NE', 'NV', 'NH', 'NJ', 'NM', 'NY', 'NC', 'ND', 'OH', 'OK', 
        'OR', 'PA', 'RI', 'SC', 'SD', 'TN', 'TX', 'UT', 'VT', 'VA', 'WA', 'WV', 'WI', 'WY', 'DC'
    ]
    ca_provinces = [
        'ON', 'BC', 'QC', 'AB', 'MB', 'SK', 'NS', 'NB', 'NL', 'PE', 'NT', 'YT', 'NU'
    ]
    
    # Check if any state/province code matches as a word
    for code in us_states + ca_provinces:
        pattern = r'\b' + re.escape(code.lower()) + r'\b'
        if re.search(pattern, loc_lower):
            return True
            
    # Check for major US/Canada city keywords
    major_cities = [
        'nyc', 'sf', 'la', 'seattle', 'boston', 'chicago', 'austin', 'denver', 'silicon valley', 
        'toronto', 'vancouver', 'montreal', 'calgary', 'ottawa', 'waterloo', 'halifax'
    ]
    for city in major_cities:
        pattern = r'\b' + re.escape(city) + r'\b'
        if re.search(pattern, loc_lower):
            return True
            
    # Check if it mentions major non-US/Canada country keywords
    non_us_indicators = [
        'uk', 'united kingdom', 'europe', 'london', 'germany', 'poland', 'india', 'singapore', 
        'australia', 'ireland', 'netherlands', 'france', 'spain', 'italy', 'switzerland', 'sweden'
    ]
    for country in non_us_indicators:
        pattern = r'\b' + re.escape(country) + r'\b'
        if re.search(pattern, loc_lower):
            return False
            
    # Default to True if no non-US country is mentioned
    return True

def _fetch_greenhouse_api(board_token: str, job_id: str) -> str | None:
    """Fetch job content from the Greenhouse boards API. Returns formatted text or None."""
    try:
        api_url = f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs/{job_id}"
        resp = requests.get(api_url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            title = data.get("title", "")
            content_html = data.get("content", "")
            location = data.get("location", {}).get("name", "")
            soup = BeautifulSoup(content_html, 'html.parser')
            body_text = f"{title}\n{location}\n\n{soup.get_text()}"
            return body_text.strip()
    except Exception as e:
        print(f"[!] Greenhouse API fetch exception ({board_token}/{job_id}): {e}")
    return None


def try_fetch_from_greenhouse_api(url):
    """Attempt to parse Greenhouse board token and job ID from URL and fetch via Greenhouse API."""
    try:
        parsed = urlparse(url)
        netloc = parsed.netloc.lower()
        path = parsed.path

        board_token = None
        job_id = None

        DOMAIN_TO_GREENHOUSE_TOKEN = {
            "braincorp.com": "braincorporation",
            "www.braincorp.com": "braincorporation",
            "nuro.ai": "nuro",
            "www.nuro.ai": "nuro",
            "seatgeek.com": "seatgeek",
            "www.seatgeek.com": "seatgeek"
        }

        if "greenhouse.io" in netloc:
            parts = [p for p in path.split('/') if p]
            if len(parts) >= 3 and parts[1] == 'jobs':
                board_token = parts[0]
                job_id = parts[2]
        else:
            qsl = dict(parse_qsl(parsed.query))
            if 'gh_jid' in qsl:
                job_id = qsl['gh_jid']
                if netloc in DOMAIN_TO_GREENHOUSE_TOKEN:
                    board_token = DOMAIN_TO_GREENHOUSE_TOKEN[netloc]
                else:
                    domain_parts = netloc.split('.')
                    if len(domain_parts) >= 2:
                        board_token = domain_parts[-2]
            else:
                parts = [p for p in path.split('/') if p]
                if len(parts) >= 2 and parts[-2] in ('jobs', 'careers', 'job'):
                    last_part = parts[-1]
                    if last_part.isdigit():
                        job_id = last_part
                        if netloc in DOMAIN_TO_GREENHOUSE_TOKEN:
                            board_token = DOMAIN_TO_GREENHOUSE_TOKEN[netloc]
                        else:
                            domain_parts = netloc.split('.')
                            if len(domain_parts) >= 2:
                                board_token = domain_parts[-2]

        if board_token and job_id:
            return _fetch_greenhouse_api(board_token, job_id)
    except Exception as e:
        print(f"[!] Greenhouse API fetch exception for {url}: {e}")
    return None

async def fetch_job_description(apply_url):
    """Scrape the full body text of a job page using Playwright or public APIs."""
    # Try Greenhouse API first
    gh_text = try_fetch_from_greenhouse_api(apply_url)
    if gh_text:
        return gh_text

    # If it's an iCIMS page, use default requests bypass
    if "icims.com" in apply_url:
        try:
            r = requests.get(apply_url, timeout=15)
            if r.status_code == 200:
                soup = BeautifulSoup(r.text, 'html.parser')
                job_content = soup.find(class_="iCIMS_JobContent")
                if job_content:
                    text = job_content.get_text()
                    return "\n".join([line.strip() for line in text.splitlines() if line.strip()])
                else:
                    text = soup.get_text()
                    return "\n".join([line.strip() for line in text.splitlines() if line.strip()])
        except Exception as e:
            print(f"[!] Warning: iCIMS requests fetch failed for {apply_url} - {e}")

    # Fall back to Playwright with system Chrome (C7: use shared _find_chrome)
    chrome_path = _find_chrome()

    async with async_playwright() as p:
        launch_kwargs = dict(
            headless=True,
            timeout=60000,  # 60 s max to launch — default 180 s was causing hung-Chrome crashes
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
        )
        if chrome_path:
            launch_kwargs["executable_path"] = chrome_path
        browser = await p.chromium.launch(**launch_kwargs)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800}
        )
        await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        page = await context.new_page()
        try:
            await page.set_extra_http_headers({
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8",
                "Referer": "https://www.google.com/"
            })
            await page.goto(apply_url, timeout=35000, wait_until="load")
            await page.wait_for_timeout(3000) # Settle React loading spinners

            # Self-healing Greenhouse embed iframe detection
            iframes = await page.locator("iframe").all()
            for iframe in iframes:
                src = await iframe.get_attribute("src")
                if src and "greenhouse.io" in src and ("for=" in src or "token=" in src or "board_token=" in src):
                    parsed_src = urlparse(src)
                    qsl = dict(parse_qsl(parsed_src.query))
                    board_token = qsl.get("for") or qsl.get("board_token")
                    job_id = qsl.get("token") or qsl.get("gh_jid") or qsl.get("job_id")

                    if not job_id:
                        parts = [p for p in parsed_src.path.split('/') if p]
                        if parts and parts[-1].isdigit():
                            job_id = parts[-1]

                    if board_token and job_id:
                        print(f"    [+] Dynamically detected Greenhouse iframe token='{board_token}', job_id='{job_id}'. Fetching API...")
                        result = _fetch_greenhouse_api(board_token, job_id)
                        if result:
                            return result
                            
            text = await page.locator("body").inner_text()
            return text.strip()
        except Exception as e:
            # Retry load state on error
            try:
                await page.wait_for_load_state("load", timeout=10000)
                text = await page.locator("body").inner_text()
                return text.strip()
            except Exception:
                print(f"[!] Warning: Failed to load {apply_url} - {e}")
                return None
        finally:
            await browser.close()

SCORING_RUBRIC = """Scoring Criteria — score 1, 2, or 3:
- Score 1 (High Priority / Strong Match): target titles (SWE/DS/MLE/DE), degree ≤ Master's in CS/DS, 0-3 yrs experience, multiple skill matches.
- Score 2 (Medium Priority): major/experience aligns but missing some non-critical skills.
- Score 3 (Low Priority / Ineligible): requires PhD, 4+ yrs full-time experience, unrelated background, OR requires a letter of recommendation / letter of reference / writing sample (candidate cannot provide these).

Output ONLY a valid JSON object — no markdown, no commentary:
{
  "score": 1,
  "suitability_reason": "+new-grad +Python +ML | -needs-Go -cleared"
}

IMPORTANT — suitability_reason format:
  "+<match1> +<match2> ... | -<gap1> -<gap2> ..."
  • + tokens: key strengths / matching requirements (short kebab, max 4 words each).
  • - tokens: key gaps / disqualifiers (short kebab, max 4 words each).
  • Max length: 80 characters. Do NOT write prose, do NOT include a fit percentage — only
    this compact tag format. The score (1/2/3) already conveys priority."""

SCORING_OUTPUT_SCHEMA = {
    "score": "1 | 2 | 3",
    "suitability_reason": "+match1 +match2 | -gap1 -gap2  (max 80 chars, no prose, no fit%)",
}

async def evaluate_job_with_llm(api_key: str, profile_data: dict, job_description: str) -> JobEvaluation:
    """Evaluate candidate profile fit against scraped job description text using DeepSeek directly."""
    system_prompt = f"""You are an expert technical recruiter. Evaluate a Job Description against the Candidate Profile below.

Candidate Profile:
{json.dumps(profile_data, indent=2)}

{SCORING_RUBRIC}"""

    import httpx
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

    # Strip markdown fences if present
    if content.startswith("```"):
        content = re.sub(r"^```[a-z]*\n?", "", content)
        content = re.sub(r"\n?```$", "", content)
    content = content.strip()

    data = json.loads(content)

    score_val = data.get("score", 3)
    try:
        score_val = int(score_val)
        if score_val not in (1, 2, 3):
            score_val = 3
    except Exception:
        score_val = 3

    raw_reason = str(data.get("suitability_reason", ""))
    suitability_reason = raw_reason[:80].rstrip() if raw_reason else "(no detail)"

    return JobEvaluation(
        score=score_val,
        suitability_reason=suitability_reason,
    )
# --- 5. Main Action Commands ---

def cleanup_database_locations_and_errors(conn):
    """Clean up locations and reset errored/failed jobs that are eligible."""
    cursor = conn.cursor()
    # 1. Update jobs outside US/Canada to Ineligible (only if already evaluated, not if pending)
    cursor.execute("SELECT id, location, status FROM jobs")
    all_jobs = cursor.fetchall()
    updated_ineligible = 0
    for job_id, loc, status in all_jobs:
        if not is_us_or_canada(loc) and status not in ('Pending Evaluation', 'Ineligible (Non-US/Canada)', 'Closed', 'Closed (Removed from List)', 'Closed (Expired)', 'Applied'):
            cursor.execute("UPDATE jobs SET status = 'Ineligible (Non-US/Canada)', score = 3 WHERE id = ?", (job_id,))
            updated_ineligible += 1
            
    # 2. Reset jobs that failed/errored back to Pending Evaluation
    cursor.execute("""
        SELECT id, location, status FROM jobs 
        WHERE status IN ('Evaluation Error', 'Fetch Failed / Manual Review')
    """)
    failed_jobs = cursor.fetchall()
    reset_count = 0
    for job_id, loc, status in failed_jobs:
        cursor.execute("UPDATE jobs SET status = 'Pending Evaluation', score = NULL WHERE id = ?", (job_id,))
        reset_count += 1
            
    # 3. Reset jobs that were evaluated with invalid descriptions (CAPTCHAs, generic landing pages, or very short texts)
    # NOTE: a short/absent cached description does NOT by itself mean the evaluation was bad —
    # the offline export/import-scores path can leave job_description empty in the DB even
    # though the description was seen and a real score was produced (see run_import_scores).
    # Only treat "short description" as invalid when there is no evaluation worth protecting
    # (no suitability_reason). Explicit CAPTCHA/landing-page reason text always invalidates,
    # regardless of description length.
    cursor.execute("""
        SELECT id, location, status, suitability_reason, length(job_description) FROM jobs
        WHERE status LIKE 'Eligible%' OR status = 'Ineligible' OR status = 'Ineligible (Non-US/Canada)'
    """)
    evaluated_jobs = cursor.fetchall()
    reset_invalid_count = 0
    for job_id, loc, status, reason, desc_len in evaluated_jobs:
        reason_lower = (reason or "").lower()
        has_valid_eval = bool((reason or "").strip())
        is_invalid = False

        # Check for CAPTCHA/bot check or generic landing page indicators
        if desc_len is not None and desc_len < 600 and not has_valid_eval:
            is_invalid = True
        elif "captcha" in reason_lower or "bot check" in reason_lower or "security check" in reason_lower or "human verification" in reason_lower:
            is_invalid = True
        elif "generic company landing page" in reason_lower or "unable to evaluate" in reason_lower or "impossible to evaluate" in reason_lower or "no job description" in reason_lower:
            is_invalid = True

        if is_invalid:
            # But wait, let's make sure we don't reset expired listings that contain "page not found" 
            cursor.execute("SELECT job_description FROM jobs WHERE id = ?", (job_id,))
            desc_row = cursor.fetchone()
            desc_val = (desc_row[0] or "").lower() if desc_row else ""
            if any(term in desc_val for term in ("page not found", "job not found", "job is no longer available", "no longer accepting applications")):
                continue
            cursor.execute("UPDATE jobs SET status = 'Pending Evaluation', score = NULL, job_description = NULL, suitability_reason = NULL WHERE id = ?", (job_id,))
            reset_invalid_count += 1
            
    # 4. Check for expired listings in job descriptions and mark them as Closed
    cursor.execute("SELECT id, job_description, status FROM jobs WHERE job_description IS NOT NULL AND status NOT IN ('Closed', 'Closed (Removed from List)', 'Closed (Expired)')")
    active_jobs_descs = cursor.fetchall()
    expired_count = 0
    for job_id, desc, current_status in active_jobs_descs:
        desc_lower = desc.lower()
        if any(term in desc_lower for term in EXPIRED_INDICATORS):
            cursor.execute("UPDATE jobs SET status = 'Closed (Expired)', score = 3 WHERE id = ?", (job_id,))
            expired_count += 1
            
    if updated_ineligible > 0 or reset_count > 0 or reset_invalid_count > 0 or expired_count > 0:
        conn.commit()
        print(f"[+] Database cleanup:")
        if updated_ineligible > 0:
            print(f"    - Marked {updated_ineligible} jobs outside US/Canada as 'Ineligible (Non-US/Canada)'.")
        if reset_count > 0:
            print(f"    - Reset {reset_count} errored/failed jobs to 'Pending Evaluation'.")
        if reset_invalid_count > 0:
            print(f"    - Reset {reset_invalid_count} jobs with invalid/CAPTCHA descriptions to 'Pending Evaluation'.")
        if expired_count > 0:
            print(f"    - Marked {expired_count} expired listings as 'Closed'.")

def run_ingest():
    """Download latest README.md from Simplify dev branch and parse into DB."""
    init_db()
    
    # Run a database cleanup pass
    conn = sqlite3.connect(DB_PATH)
    cleanup_database_locations_and_errors(conn)
    conn.close()
    
    print("[*] Fetching latest jobs list from Simplify repository...")
    try:
        resp = requests.get(README_URL, timeout=15, verify=False)
        resp.raise_for_status()
        content = resp.text
    except Exception as e:
        print(f"[ERROR] Failed to download README.md: {e}")
        return
        
    jobs = parse_jobs_from_markdown(content)
    print(f"[+] Parsed {len(jobs)} total jobs from markdown.")
    
    # Track active URLs in this ingestion run
    active_urls = set()
    new_jobs_count = 0
    closed_jobs_count = 0
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    for job in jobs:
        url = job['apply_url']
        active_urls.add(url)
        
        status = 'Closed' if job['is_closed'] else 'Pending Evaluation'
        score = 3 if job['is_closed'] else None
        
        try:
            # Check if job already exists
            cursor.execute("SELECT id, status FROM jobs WHERE apply_url = ?", (url,))
            existing = cursor.fetchone()
            if existing:
                job_id, current_status = existing
                # If it's now closed in the README but wasn't marked closed, update it
                if job['is_closed'] and current_status not in ('Closed', 'Closed (Removed from List)', 'Applied'):
                    cursor.execute("UPDATE jobs SET status = 'Closed', score = 3 WHERE id = ?", (job_id,))
                    closed_jobs_count += 1
                # If it was closed/inactive before, but is now active in the README, reopen it
                elif not job['is_closed'] and current_status in ('Closed', 'Closed (Removed from List)'):
                    cursor.execute("UPDATE jobs SET status = 'Pending Evaluation', score = NULL WHERE id = ?", (job_id,))
                    print(f"[+] Reopened job: {job['company']} - {job['role']}")
            else:
                cursor.execute("""
                    INSERT INTO jobs (company, role, location, apply_url, category, status, score)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (job['company'], job['role'], job['location'], url, job['category'], status, score))
                new_jobs_count += 1
                if job['is_closed']:
                    closed_jobs_count += 1
        except Exception as e:
            print(f"[!] Error inserting/updating job {url}: {e}")
            
    # Check for jobs in database that are missing from the parsed active_urls list (i.e. removed from README)
    cursor.execute("SELECT id, company, role, apply_url FROM jobs WHERE status IN ('Pending Evaluation', 'Eligible (Priority 1)', 'Eligible (Priority 2)', 'Fetch Failed / Manual Review', 'Evaluation Error')")
    db_active_jobs = cursor.fetchall()
    
    removed_count = 0
    for job_id, company, role, apply_url in db_active_jobs:
        if apply_url not in active_urls:
            cursor.execute("UPDATE jobs SET status = 'Closed (Removed from List)', score = 3 WHERE id = ?", (job_id,))
            removed_count += 1
            
    conn.commit()
    conn.close()
    
    print(f"[+] Ingestion complete:")
    print(f"    - Added {new_jobs_count} new unique jobs.")
    print(f"    - Marked {closed_jobs_count} jobs as explicitly closed (lock symbol).")
    print(f"    - Marked {removed_count} jobs as closed/inactive (removed from Simplify list).")
    export_db_to_csv()

async def _scrape_job_description(url: str) -> str | None:
    """Fetch/scrape the job description text for a single URL. Returns text or None."""
    return await fetch_job_description(url)


async def _evaluate_single_job(conn, job: tuple, profile_data: dict, api_key: str | None, dry_run: bool) -> tuple[bool, bool]:
    """Scrape + optionally evaluate one pending job row. Returns (dry_run_ok, dry_run_fail) bools."""
    job_id, company, role, location, apply_url = job
    cursor = conn.cursor()

    print(f"\n[*] {'Scraping' if dry_run else 'Evaluating'}: {company} - {role}...")
    print(f"    Location: {location}")
    print(f"    Link: {apply_url}")

    job_desc = await _scrape_job_description(apply_url)
    if not job_desc or len(job_desc) < 300:
        is_expired = False
        if job_desc:
            desc_lower = job_desc.lower()
            if any(term in desc_lower for term in EXPIRED_INDICATORS):
                is_expired = True

        if is_expired:
            print("    [+] Expired listing detected. Marking as Closed (Expired).")
            if not dry_run:
                cursor.execute(
                    "UPDATE jobs SET status = 'Closed (Expired)', score = 3, job_description = ? WHERE id = ?",
                    (job_desc, job_id),
                )
                conn.commit()
        else:
            print("    [!] Fetch failed or too short.")
            if dry_run:
                return False, True  # (ok=False, fail=True)
            else:
                cursor.execute(
                    "UPDATE jobs SET status = 'Fetch Failed / Manual Review' WHERE id = ?",
                    (job_id,),
                )
                conn.commit()
        return False, False

    print(f"    [+] Successfully fetched description ({len(job_desc)} chars).")

    if dry_run:
        preview = job_desc[:400].replace('\n', ' ')
        print(f"    [PREVIEW] {preview}...")
        print(f"    [DRY-RUN] ✓ Scrape OK — DeepSeek would receive {len(job_desc)} chars of context.")
        return True, False  # (ok=True, fail=False)

    print("    Evaluating with DeepSeek...")
    try:
        eval_result = await evaluate_job_with_llm(api_key, profile_data, job_desc)

        if not is_us_or_canada(location):
            status_str = "Ineligible (Non-US/Canada)"
            score_val = 3
        else:
            status_str = f"Eligible (Priority {eval_result.score})" if eval_result.score < 3 else "Ineligible"
            score_val = eval_result.score

        cursor.execute("""
            UPDATE jobs
            SET status = ?,
                score = ?,
                suitability_reason = ?,
                job_description = ?,
                date_evaluated = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (
            status_str,
            score_val,
            eval_result.suitability_reason,
            job_desc,
            job_id,
        ))
        conn.commit()
        print(f"    [+] Evaluated: Score = {score_val} ({status_str})")
        print(f"    [+] Reason: {eval_result.suitability_reason}")
    except Exception as e:
        print(f"    [!] Error during evaluation: {e}")
        cursor.execute("UPDATE jobs SET status = 'Evaluation Error' WHERE id = ?", (job_id,))
        conn.commit()

    return False, False


async def run_evaluate(limit=10, dry_run=False):
    """Fetch and evaluate pending jobs in the database.
    If dry_run=True, scrapes job descriptions but skips LLM evaluation (validates scraping pipeline)."""
    if dry_run:
        print("[DRY-RUN] Scraping job descriptions only — DeepSeek LLM calls SKIPPED.")
        api_key = None
    else:
        api_key = os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            print("[ERROR] No DEEPSEEK_API_KEY found. Check .env file.")
            return

    if not PROFILE_PATH.exists():
        print(f"[ERROR] Candidate profile not found at {PROFILE_PATH}.")
        return

    with open(PROFILE_PATH, "r", encoding="utf-8") as f:
        profile_data = json.load(f)

    conn = sqlite3.connect(DB_PATH)

    # Run cleanup first to filter out non-US/Canada and reset errored/failed
    cleanup_database_locations_and_errors(conn)

    cursor = conn.cursor()

    if limit == -1:
        cursor.execute("SELECT id, company, role, location, apply_url FROM jobs WHERE status = 'Pending Evaluation'")
    else:
        cursor.execute(
            "SELECT id, company, role, location, apply_url FROM jobs WHERE status = 'Pending Evaluation' LIMIT ?",
            (limit,),
        )
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
        print("[DRY-RUN] Pipeline validated — run without --dry-run on Windows with DEEPSEEK_API_KEY to evaluate.")
    else:
        print("\n[+] Evaluation loop complete.")
        export_db_to_csv()

EVAL_PENDING_PATH = Path(__file__).parent.parent / "artifacts" / "eval_pending.json"
EVAL_SCORES_PATH  = Path(__file__).parent.parent / "artifacts" / "eval_scores.json"

async def run_export_prompts(limit: int = -1):
    """Scrape pending jobs and export descriptions + rubric to artifacts/eval_pending.json
    for offline scoring (e.g. via Claude). No LLM calls made here."""
    import datetime

    if not PROFILE_PATH.exists():
        print(f"[ERROR] Candidate profile not found at {PROFILE_PATH}.")
        return

    with open(PROFILE_PATH, "r", encoding="utf-8") as f:
        profile_data = json.load(f)

    conn = sqlite3.connect(DB_PATH)
    cleanup_database_locations_and_errors(conn)
    cursor = conn.cursor()

    if limit == -1:
        cursor.execute("SELECT id, company, role, location, apply_url FROM jobs WHERE status = 'Pending Evaluation'")
    else:
        cursor.execute(
            "SELECT id, company, role, location, apply_url FROM jobs WHERE status = 'Pending Evaluation' LIMIT ?",
            (limit,),
        )
    pending_jobs = cursor.fetchall()

    if not pending_jobs:
        print("[EXPORT] No pending jobs to scrape.")
        conn.close()
        return

    print(f"[EXPORT] Scraping {len(pending_jobs)} pending jobs...")
    exported = []
    for job in pending_jobs:
        job_id, company, role, location, apply_url = job
        print(f"  Scraping [{job_id}] {company} — {role}...")
        try:
            job_desc = await _scrape_job_description(apply_url)
        except Exception as _scrape_exc:
            print(f"    [ERROR] Scrape raised exception — marking Fetch Failed. {_scrape_exc!s:.120}")
            cursor.execute(
                "UPDATE jobs SET status='Fetch Failed / Manual Review' WHERE id=?", (job_id,)
            )
            conn.commit()
            continue

        if not job_desc or len(job_desc) < 300:
            is_expired = job_desc and any(t in job_desc.lower() for t in EXPIRED_INDICATORS)
            if is_expired:
                print(f"    [EXPIRED] Marking Closed (Expired).")
                cursor.execute(
                    "UPDATE jobs SET status='Closed (Expired)', score=3, job_description=? WHERE id=?",
                    (job_desc, job_id),
                )
            else:
                print(f"    [FAIL] Fetch failed or too short — marking Fetch Failed.")
                cursor.execute(
                    "UPDATE jobs SET status='Fetch Failed / Manual Review' WHERE id=?", (job_id,)
                )
            conn.commit()
            continue

        print(f"    OK ({len(job_desc)} chars)")

        # Pre-score ineligibility: auto-reject listings that require extra application
        # materials the bot cannot provide (letters of recommendation/interest, writing samples).
        _desc_lower = job_desc.lower()
        _disqualifying_materials = [
            "letter of recommendation", "letters of recommendation",
            "letter of reference", "letters of reference",
            "writing sample", "writing samples",
            "letter of interest required", "letters of interest required",
        ]
        if any(t in _desc_lower for t in _disqualifying_materials):
            print(f"    [INELIGIBLE] Requires extra materials (letter of rec/reference/writing sample) — marking Ineligible.")
            cursor.execute(
                "UPDATE jobs SET status='Ineligible', score=3, "
                "suitability_reason='FIT:0 | -requires-letter-of-rec', job_description=? WHERE id=?",
                (job_desc, job_id),
            )
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
    export_db_to_csv()   # persist expired/failed status changes

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
    print(f"[EXPORT] {len(pending_jobs) - len(exported)} jobs marked expired/failed (not exported).")
    print(f"[EXPORT] Next: have Claude score the file, then run:")
    print(f"         python3 src/job_tracker.py evaluate --import-scores")


def run_import_scores():
    """Read artifacts/eval_scores.json (Claude-scored) and write results into DB → CSV."""
    if not EVAL_SCORES_PATH.exists():
        print(f"[ERROR] Scores file not found: {EVAL_SCORES_PATH}")
        print(f"        Run 'evaluate --export-prompts', have Claude score it, then retry.")
        return

    with open(EVAL_SCORES_PATH, "r", encoding="utf-8") as f:
        scores = json.load(f)

    if not isinstance(scores, list):
        print(f"[ERROR] eval_scores.json must be a JSON array of score objects.")
        return

    # Load the scraped descriptions from the export step so they get persisted alongside the
    # score. Without this, a job scored via export-prompts/import-scores lands in the DB with
    # a real evaluation but no job_description — which a later `ingest` cleanup pass can
    # misread as an invalid/CAPTCHA scrape and wrongly reset (see cleanup_database_locations_and_errors).
    pending_descs_by_url = {}
    pending_descs_by_id = {}
    if EVAL_PENDING_PATH.exists():
        try:
            with open(EVAL_PENDING_PATH, "r", encoding="utf-8") as f:
                pending_data = json.load(f)
            for pj in pending_data.get("jobs", []):
                desc = pj.get("job_description", "")
                if desc and len(desc) >= 300:
                    if pj.get("apply_url"):
                        pending_descs_by_url[pj["apply_url"]] = desc
                    if pj.get("id") is not None:
                        pending_descs_by_id[str(pj["id"])] = desc
        except Exception as e:
            print(f"[!] Warning: could not read {EVAL_PENDING_PATH} for description backfill: {e}")

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    imported = 0
    skipped = 0
    for entry in scores:
        apply_url = entry.get("apply_url", "")
        job_id    = entry.get("id")

        # Locate the DB row
        row = None
        if apply_url:
            cursor.execute("SELECT id, company, role, location FROM jobs WHERE apply_url=?", (apply_url,))
            row = cursor.fetchone()
        if not row and job_id:
            cursor.execute("SELECT id, company, role, location FROM jobs WHERE id=?", (job_id,))
            row = cursor.fetchone()

        if not row:
            print(f"  [SKIP] No DB row for apply_url={apply_url!r} id={job_id}")
            skipped += 1
            continue

        db_id, company, role, location = row

        # Validate + normalise score
        try:
            score_val = int(entry.get("score", 3))
            if score_val not in (1, 2, 3):
                score_val = 3
        except Exception:
            score_val = 3

        raw_reason = str(entry.get("suitability_reason", ""))
        reason = raw_reason[:80].rstrip() if raw_reason else "(no detail)"

        # Apply same status mapping as _evaluate_single_job
        if not is_us_or_canada(location):
            status_str = "Ineligible (Non-US/Canada)"
            score_val  = 3
        else:
            status_str = f"Eligible (Priority {score_val})" if score_val < 3 else "Ineligible"

        job_desc = pending_descs_by_url.get(apply_url) or pending_descs_by_id.get(str(job_id) if job_id is not None else str(db_id))

        if job_desc:
            cursor.execute("""
                UPDATE jobs
                SET status=?, score=?, suitability_reason=?,
                    job_description=?,
                    date_evaluated=CURRENT_TIMESTAMP
                WHERE id=?
            """, (
                status_str, score_val, reason, job_desc, db_id,
            ))
        else:
            cursor.execute("""
                UPDATE jobs
                SET status=?, score=?, suitability_reason=?,
                    date_evaluated=CURRENT_TIMESTAMP
                WHERE id=?
            """, (
                status_str, score_val, reason, db_id,
            ))
        conn.commit()
        print(f"  [IMPORT] [{db_id}] {company} — Score {score_val} ({status_str}) | {reason[:60]}")
        imported += 1

    conn.close()
    print(f"\n[IMPORT] Done. Imported: {imported}, Skipped: {skipped}.")


def show_status():
    """Display count metrics for jobs stored in the database."""
    if not DB_PATH.exists():
        print("[*] Database file does not exist. Run ingest first.")
        return
        
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status")
    stats = cursor.fetchall()
    
    cursor.execute("SELECT COUNT(*) FROM jobs")
    total = cursor.fetchone()[0]
    
    print("\n=== Job Database Status Summary ===")
    print(f"Total jobs recorded: {total}")
    print("-" * 35)
    for status, count in stats:
        print(f"  {status:<30}: {count}")
    print("=" * 35)
    conn.close()

def list_priority_jobs(priority=1):
    """List jobs with a specific suitability score."""
    if not DB_PATH.exists():
        print("[*] Database file does not exist. Run ingest first.")
        return
        
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT id, company, role, location, apply_url, suitability_reason
        FROM jobs
        WHERE score = ? AND status LIKE 'Eligible%'
        ORDER BY date_added DESC
    """, (priority,))
    jobs = cursor.fetchall()

    print(f"\n=== Priority {priority} Jobs (Count: {len(jobs)}) ===")
    for job_id, company, role, location, url, reason in jobs:
        print(f"[{job_id}] {company} - {role} ({location}) | {reason or ''}")
        print(f"    Link: {url}")
        print("-" * 60)
    conn.close()

def mark_applied(job_id, date_str=None):
    """Manually mark a job as Applied in the database (keyed by numeric id).

    date_str: YYYY-MM-DD string; defaults to today if omitted.
    """
    import datetime as _dt
    if date_str is None:
        date_str = _dt.date.today().isoformat()
    if not DB_PATH.exists():
        print("[*] Database file does not exist.")
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("SELECT company, role FROM jobs WHERE id = ?", (job_id,))
    job = cursor.fetchone()
    if not job:
        print(f"[ERROR] Job with ID {job_id} not found.")
        conn.close()
        return

    cursor.execute(
        "UPDATE jobs SET status = 'Applied', date_applied = ? WHERE id = ?",
        (date_str, job_id)
    )
    conn.commit()
    conn.close()
    print(f"[+] Successfully marked {job[0]} - {job[1]} as 'Applied' ({date_str}).")
    export_db_to_csv()


def set_status_by_url(url, status, date_applied=None):
    """Set a job's Status (and optionally Date Applied) directly in the CSV (no DB needed).

    Used by app_workday.py after submit (Applied) or on dead-listing detection (Closed (Expired)).
    Finds the row via clean_url normalization + exact-match fallback. The Date Applied column
    is injected into the CSV if it predates that feature.

    Returns (company, role) on success, None if URL not found.
    """
    import csv as _csv

    csv_path = DATA_DIR / "jobs_tracker.csv"
    if not csv_path.exists():
        print(f"  [tracker] CSV not found at {csv_path} — cannot update status.")
        return None

    target = clean_url(url)

    with open(csv_path, "r", newline="", encoding="utf-8") as f:
        reader = _csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)

    # Inject "Date Applied" column if the CSV predates this feature.
    if "Date Applied" not in fieldnames:
        try:
            idx = fieldnames.index("Date Evaluated") + 1
        except ValueError:
            idx = len(fieldnames) - 1
        fieldnames.insert(idx, "Date Applied")
        for r in rows:
            r.setdefault("Date Applied", "")

    matched = None
    for row in rows:
        row_url = clean_url(row.get("Apply URL", ""))
        if row_url == target or row.get("Apply URL", "") == url:
            row["Status"] = status
            if date_applied is not None:
                row["Date Applied"] = date_applied
            matched = (row.get("Company", ""), row.get("Role", ""))
            break

    if matched is None:
        print(f"  [tracker] ⚠ URL not found in tracker — status not updated.\n"
              f"           URL: {url}")
        return None

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = _csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    return matched


def mark_applied_by_url(url, date_str=None):
    """Mark a job Applied + record the applied date in the CSV. No DB needed.

    Intended for use by app_workday.py after a successful submit.
    """
    import datetime as _dt
    if date_str is None:
        date_str = _dt.date.today().isoformat()
    matched = set_status_by_url(url, "Applied", date_applied=date_str)
    if matched:
        print(f"  [tracker] ✓ Marked {matched[0]} — {matched[1]} as Applied ({date_str}).")
    return matched


def mark_closed_expired_by_url(url):
    """Mark a job Closed (Expired) in the CSV. No DB needed.

    Intended for use by app_workday.py when a dead/expired listing is detected.
    """
    matched = set_status_by_url(url, "Closed (Expired)")
    if matched:
        print(f"  [tracker] ✓ Marked {matched[0]} — {matched[1]} as Closed (Expired).")
    return matched

def detect_applicator(url: str) -> str | None:
    """Return the applicator script path for a given job URL, or None if unsupported."""
    base = Path(__file__).parent
    if "myworkdayjobs.com" in url or "workday.com" in url:
        return str(base / "app_workday.py")
    if "greenhouse.io" in url or "gh_jid=" in url:
        return str(base / "app_greenhouse.py")
    # Add more handlers here as they are built:
    # if "lever.co" in url: return str(base / "app_lever.py")
    # if "ashbyhq.com" in url: return str(base / "app_ashby.py")
    return None

def run_apply_loop():
    """Loop through high priority eligible jobs from highest score to lowest and launch the applicator."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Select all active jobs that are eligible (Priority 1 or 2)
    cursor.execute("""
        SELECT id, company, role, location, score, apply_url
        FROM jobs
        WHERE status LIKE 'Eligible (Priority %'
        ORDER BY score ASC, date_added DESC
    """)
    jobs = cursor.fetchall()
    conn.close()

    if not jobs:
        print("[*] No eligible jobs found to apply.")
        return

    print(f"\n[*] Found {len(jobs)} eligible jobs for application loop.")
    print("[*] Starting loop from highest score/priority to lowest...")

    import subprocess

    for idx, (job_id, company, role, location, score, url) in enumerate(jobs):
        print("\n" + "="*80)
        print(f"[{idx+1}/{len(jobs)}] Job ID {job_id}: {company} - {role}")
        print(f"      Location: {location} | Priority Score: {score}")
        print(f"      Link: {url}")
        
        app_script = detect_applicator(url)
        if app_script:
            print(f"      Applicator: {Path(app_script).name}")
        else:
            print(f"      [~] No supported applicator for this URL — skipping.")
        print("="*80)
        
        choice = input("Do you want to launch the applicator for this job? [Y/n/skip/exit]: ").strip().lower()
        if choice == 'exit':
            print("[*] Exiting apply loop.")
            break
        elif choice in ('n', 'no', 'skip'):
            print(f"[*] Skipping {company} - {role}.")
            continue
        
        if not app_script:
            print(f"[*] No applicator available — skipping.")
            continue
            
        print(f"[*] Launching applicator for {company} - {role}...")
        try:
            # We run it synchronously so it blocks this loop until the user closes or finishes the applicator session
            subprocess.run([sys.executable, app_script, url], check=True)
            print(f"[+] Finished session for {company} - {role}.")
        except Exception as e:
            print(f"[!] Error running applicator: {e}")
            
    print("\n[*] Apply loop finished.")

def export_db_to_csv():
    """Export the SQLite database contents to a clean CSV file and detailed columns to a JSON file."""
    csv_path = DATA_DIR / "jobs_tracker.csv"
    details_path = DATA_DIR / "jobs_details.json"

    try:
        import csv
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Fetch summary columns for CSV — Apply URL first for quick clicking
        cursor.execute("""
            SELECT apply_url, id, company, role, location, category, status, score,
                   date_added, date_evaluated, date_applied, suitability_reason
            FROM jobs
            ORDER BY score ASC, date_added DESC
        """)
        csv_rows = cursor.fetchall()

        csv_headers = [
            "Apply URL", "ID", "Company", "Role", "Location", "Category", "Status", "Score",
            "Date Added", "Date Evaluated", "Date Applied", "Suitability Reason",
        ]

        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(csv_headers)
            writer.writerows(csv_rows)
        print(f"[+] CSV Update: Exported clean jobs list to '{csv_path}'.")

        # Fetch descriptions for the details cache (used to avoid re-scraping on ingest)
        cursor.execute("""
            SELECT apply_url, job_description
            FROM jobs
            WHERE job_description IS NOT NULL AND job_description != ''
        """)
        details_rows = cursor.fetchall()

        # Load existing details JSON first to preserve other entries if needed
        existing_details = {}
        if details_path.exists():
            try:
                with open(details_path, "r", encoding="utf-8") as f:
                    existing_details = json.load(f)
            except Exception:
                pass

        for apply_url, desc in details_rows:
            existing_details[apply_url] = {"job_description": desc}

        with open(details_path, "w", encoding="utf-8") as f:
            json.dump(existing_details, f, indent=2, ensure_ascii=False)
        print(f"[+] Details Cache: Exported detailed job descriptions to '{details_path}'.")
        
        conn.close()
    except Exception as e:
        print(f"[!] Warning: Failed to export database data: {e}")

# --- 6. CLI Entry Point ---

def _reset_for_recheck(scope: str):
    """Reset already-evaluated jobs to Pending Evaluation so they get re-scraped + re-scored.

    scope:
      p1p2     – Eligible (Priority 1) and Eligible (Priority 2) only
      eligible – all Eligible% rows
      all      – Eligible% + Ineligible + Ineligible (Non-US/Canada)

    Never touches Applied, Closed, or Closed (Expired) rows.
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    if scope == "p1p2":
        cursor.execute(
            "UPDATE jobs SET status='Pending Evaluation', score=NULL "
            "WHERE status IN ('Eligible (Priority 1)', 'Eligible (Priority 2)')"
        )
    elif scope == "eligible":
        cursor.execute(
            "UPDATE jobs SET status='Pending Evaluation', score=NULL "
            "WHERE status LIKE 'Eligible%'"
        )
    elif scope == "all":
        cursor.execute(
            "UPDATE jobs SET status='Pending Evaluation', score=NULL "
            "WHERE status LIKE 'Eligible%' "
            "   OR status IN ('Ineligible', 'Ineligible (Non-US/Canada)')"
        )

    n = cursor.rowcount
    conn.commit()
    conn.close()
    print(f"[RECHECK] Reset {n} jobs to 'Pending Evaluation' (scope={scope!r}).")


def main():
    parser = argparse.ArgumentParser(description="Simplify Jobs Aggregator & Match Tracker")
    parser.add_argument("action", choices=["ingest", "evaluate", "status", "list", "apply", "apply-loop", "csv"], help="Action to perform")
    parser.add_argument("--limit", type=int, default=10, help="Number of pending jobs to evaluate. Set to -1 to evaluate all pending. (default 10)")
    parser.add_argument("--priority", type=int, default=1, choices=[1, 2, 3], help="Priority level to list (default 1)")
    parser.add_argument("--id", type=int, help="Job ID to mark as applied")
    parser.add_argument("--dry-run", action="store_true", help="For 'evaluate': scrape job descriptions only, skip DeepSeek LLM calls (validates pipeline)")
    parser.add_argument("--export-prompts", action="store_true", help="For 'evaluate': scrape pending jobs and export to artifacts/eval_pending.json for offline scoring (no LLM key needed)")
    parser.add_argument("--import-scores", action="store_true", help="For 'evaluate': import Claude-scored artifacts/eval_scores.json into the database")
    parser.add_argument("--recheck", choices=["p1p2", "eligible", "all"],
                        help="For 'evaluate --export-prompts': reset already-scored jobs back to "
                             "Pending Evaluation so they are re-scraped and re-scored. "
                             "p1p2 = Priority 1+2 only; eligible = all Eligible rows; "
                             "all = Eligible + Ineligible (never touches Applied or Closed).")

    args = parser.parse_args()
    
    # 1. Start from a fresh database by importing CSV
    # If the database file exists, delete it first to ensure we start from the CSV
    if DB_PATH.exists():
        try:
            DB_PATH.unlink()
        except Exception as e:
            print(f"[!] Warning: Could not delete existing database file before run: {e}")
            
    import_csv_to_db()
    
    try:
        if args.action == "ingest":
            run_ingest()
        elif args.action == "evaluate":
            if args.export_prompts:
                if args.recheck:
                    _reset_for_recheck(args.recheck)
                asyncio.run(run_export_prompts(limit=args.limit))
            elif args.import_scores:
                run_import_scores()
            else:
                asyncio.run(run_evaluate(limit=args.limit, dry_run=args.dry_run))
        elif args.action == "status":
            show_status()
        elif args.action == "list":
            list_priority_jobs(priority=args.priority)
        elif args.action == "apply":
            if not args.id:
                print("[ERROR] Please specify a Job ID with --id to mark as applied.")
                sys.exit(1)
            mark_applied(args.id)
        elif args.action == "apply-loop":
            run_apply_loop()
        elif args.action == "csv":
            export_db_to_csv()
    finally:
        # Export back to CSV if we ran an action that could modify or display the DB (excluding apply-loop and dry-run)
        if args.action in ("ingest", "apply", "csv", "status") or (
                args.action == "evaluate" and not args.dry_run and not args.export_prompts):
            export_db_to_csv()
            
        # Clean up database file so it doesn't persist on disk
        if DB_PATH.exists():
            import gc
            gc.collect()
            try:
                DB_PATH.unlink()
                print("[+] Temporary database 'jobs.db' cleaned up from disk.")
            except Exception as e:
                print(f"[!] Warning: Could not delete temporary database file: {e}")

if __name__ == "__main__":
    main()
