import os
import sys
import json
import sqlite3
import re
import asyncio
import argparse
from pathlib import Path
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from playwright.async_api import async_playwright

# Force UTF-8 encoding on standard output/error
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True)
else:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

# Load environment variables from .env file
env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)

DB_PATH = Path(__file__).parent / "jobs.db"
PROFILE_PATH = Path(__file__).parent / "library.json"  # candidate profile (library.json)
README_URL = "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/README.md"

# --- 1. Data Classes ---

class JobMatchMetrics:
    def __init__(self, overall_fit_percentage: float, confidence_score: float):
        self.overall_fit_percentage = overall_fit_percentage
        self.confidence_score = confidence_score

class JobEvaluation:
    def __init__(self, score, suitability_reason, match_metrics, core_alignments, technical_gaps, vulnerability_analysis):
        self.score = score
        self.suitability_reason = suitability_reason
        self.match_metrics = match_metrics
        self.core_alignments = core_alignments
        self.technical_gaps = technical_gaps
        self.vulnerability_analysis = vulnerability_analysis

# --- 2. Database Helpers ---

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
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
            core_alignments TEXT,
            technical_gaps TEXT,
            vulnerability_analysis TEXT,
            overall_fit_percentage REAL,
            confidence_score REAL,
            date_added DATETIME DEFAULT CURRENT_TIMESTAMP,
            date_evaluated DATETIME
        )
    """)
    conn.commit()
    return conn

def import_csv_to_db():
    """Import the jobs_tracker.csv and jobs_details.json files into the SQLite database."""
    csv_path = Path(__file__).parent / "jobs_tracker.csv"
    details_path = Path(__file__).parent / "jobs_details.json"
    
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
    cursor.execute("""
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
            core_alignments TEXT,
            technical_gaps TEXT,
            vulnerability_analysis TEXT,
            overall_fit_percentage REAL,
            confidence_score REAL,
            date_added DATETIME DEFAULT CURRENT_TIMESTAMP,
            date_evaluated DATETIME
        )
    """)
    
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
            
            fit_str = get_val("Overall Fit %")
            overall_fit = float(fit_str) if fit_str and fit_str != 'None' and fit_str != '' else None
            
            conf_str = get_val("Confidence Score")
            confidence = float(conf_str) if conf_str and conf_str != 'None' and conf_str != '' else None
            
            date_added = get_val("Date Added")
            date_evaluated = get_val("Date Evaluated")
            suitability_reason = get_val("Suitability Reason")
            apply_url = get_val("Apply URL")
            
            # Retrieve detailed columns from either the CSV (if present for legacy reasons) or JSON cache
            job_description = get_val("Job Description")
            core_alignments = get_val("Core Alignments")
            technical_gaps = get_val("Technical Gaps")
            vulnerability_analysis = get_val("Vulnerability Analysis")
            
            # Fall back to JSON details cache if not in CSV
            if apply_url in details_cache:
                job_details = details_cache[apply_url]
                if not job_description:
                    job_description = job_details.get("job_description", "")
                if not core_alignments:
                    core_alignments = json.dumps(job_details.get("core_alignments", []))
                if not technical_gaps:
                    technical_gaps = json.dumps(job_details.get("technical_gaps", []))
                if not vulnerability_analysis:
                    vulnerability_analysis = job_details.get("vulnerability_analysis", "")
            
            # Defaults
            job_description = job_description or ""
            core_alignments = core_alignments or "[]"
            technical_gaps = technical_gaps or "[]"
            vulnerability_analysis = vulnerability_analysis or ""
            
            cursor.execute("""
                INSERT OR REPLACE INTO jobs (
                    id, company, role, location, apply_url, category, status, score,
                    suitability_reason, job_description, core_alignments, technical_gaps,
                    vulnerability_analysis, overall_fit_percentage, confidence_score,
                    date_added, date_evaluated
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                job_id, company, role, location, apply_url, category, status, score,
                suitability_reason, job_description, core_alignments, technical_gaps,
                vulnerability_analysis, overall_fit, confidence, date_added, date_evaluated
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

    # Fall back to Playwright with system Chrome
    import platform
    system = platform.system()
    chrome_candidates = []
    if system == "Darwin":
        chrome_candidates = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    elif system == "Windows":
        chrome_candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ]
    else:
        chrome_candidates = ["/usr/bin/google-chrome", "/usr/bin/chromium-browser", "/usr/bin/chromium"]
    chrome_path = next((p for p in chrome_candidates if os.path.exists(p)), "")

    async with async_playwright() as p:
        launch_kwargs = dict(
            headless=True,
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

async def evaluate_job_with_llm(api_key: str, profile_data: dict, job_description: str) -> JobEvaluation:
    """Evaluate candidate profile fit against scraped job description text using DeepSeek directly."""
    system_prompt = f"""You are an expert technical recruiter. Evaluate a Job Description against the Candidate Profile below.

Candidate Profile:
{json.dumps(profile_data, indent=2)}

Scoring Criteria — score 1, 2, or 3:
- Score 1 (High Priority / Strong Match): target titles (SWE/DS/MLE/DE), degree ≤ Master's in CS/DS, 0-3 yrs experience, multiple skill matches.
- Score 2 (Medium Priority): major/experience aligns but missing some non-critical skills.
- Score 3 (Low Priority / Ineligible): requires PhD, 4+ yrs full-time experience, or unrelated background.

Output ONLY a valid JSON object — no markdown, no commentary:
{{
  "score": 1,
  "suitability_reason": "Detailed reasoning referencing specific requirements.",
  "match_metrics": {{"overall_fit_percentage": 85, "confidence_score": 90}},
  "core_alignments": ["requirement that matches candidate experience"],
  "technical_gaps": ["critical technology missing from candidate profile"],
  "vulnerability_analysis": "Where candidate might struggle in coding/system design interview."
}}"""

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
    mm = data.get("match_metrics", {})
    if not isinstance(mm, dict):
        mm = {}

    score_val = data.get("score", 3)
    try:
        score_val = int(score_val)
        if score_val not in (1, 2, 3):
            score_val = 3
    except Exception:
        score_val = 3

    return JobEvaluation(
        score=score_val,
        suitability_reason=str(data.get("suitability_reason", "No reason provided.")),
        match_metrics=JobMatchMetrics(
            overall_fit_percentage=float(mm.get("overall_fit_percentage", 0)),
            confidence_score=float(mm.get("confidence_score", 0))
        ),
        core_alignments=list(data.get("core_alignments", [])),
        technical_gaps=list(data.get("technical_gaps", [])),
        vulnerability_analysis=str(data.get("vulnerability_analysis", ""))
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
    cursor.execute("""
        SELECT id, location, status, suitability_reason, length(job_description) FROM jobs 
        WHERE status LIKE 'Eligible%' OR status = 'Ineligible' OR status = 'Ineligible (Non-US/Canada)'
    """)
    evaluated_jobs = cursor.fetchall()
    reset_invalid_count = 0
    for job_id, loc, status, reason, desc_len in evaluated_jobs:
        reason_lower = (reason or "").lower()
        is_invalid = False
        
        # Check for CAPTCHA/bot check or generic landing page indicators
        if desc_len is not None and desc_len < 600:
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
    expired_indicators = [
        "page not found", "job not found", "job is no longer available", 
        "no longer accepting applications", "this job is closed", 
        "the page you are looking for doesn't exist", "link you followed may be broken",
        "couldn't find that page", "couldn’t find that page", "could not find that page"
    ]
    for job_id, desc, current_status in active_jobs_descs:
        desc_lower = desc.lower()
        if any(term in desc_lower for term in expired_indicators):
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
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
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

async def run_evaluate(limit=10, dry_run=False):
    """Fetch and evaluate pending jobs in the database.
    If dry_run=True, scrapes job descriptions but skips LLM evaluation (validates scraping pipeline)."""
    if dry_run:
        print("[DRY-RUN] Scraping job descriptions only — DeepSeek LLM calls SKIPPED.")
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

    if not dry_run:
        api_key = os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            print("[ERROR] No DEEPSEEK_API_KEY found. Check .env file.")
            return
    else:
        api_key = None
    
    conn = sqlite3.connect(DB_PATH)
    
    # Run cleanup first to filter out non-US/Canada and reset errored/failed
    cleanup_database_locations_and_errors(conn)
    
    cursor = conn.cursor()
    
    if limit == -1:
        cursor.execute("""
            SELECT id, company, role, location, apply_url FROM jobs 
            WHERE status = 'Pending Evaluation'
        """)
    else:
        cursor.execute("""
            SELECT id, company, role, location, apply_url FROM jobs 
            WHERE status = 'Pending Evaluation' 
            LIMIT ?
        """, (limit,))
    pending_jobs = cursor.fetchall()
    
    if not pending_jobs:
        print("[*] No pending jobs to evaluate.")
        conn.close()
        return
        
    print(f"[*] Starting {'dry-run scrape' if dry_run else 'evaluation'} for {len(pending_jobs)} pending jobs...")
    
    dry_run_ok = 0
    dry_run_fail = 0
    for job_id, company, role, location, apply_url in pending_jobs:
        print(f"\n[*] {'Scraping' if dry_run else 'Evaluating'}: {company} - {role}...")
        print(f"    Location: {location}")
        print(f"    Link: {apply_url}")
        
        job_desc = await fetch_job_description(apply_url)
        if not job_desc or len(job_desc) < 300:
            is_expired = False
            if job_desc:
                desc_lower = job_desc.lower()
                expired_indicators = [
                    "page not found", "job not found", "job is no longer available", 
                    "no longer accepting applications", "this job is closed", 
                    "the page you are looking for doesn't exist", "link you followed may be broken",
                    "couldn't find that page", "couldn't find that page", "could not find that page"
                ]
                if any(term in desc_lower for term in expired_indicators):
                    is_expired = True
                    
            if is_expired:
                print("    [+] Expired listing detected. Marking as Closed (Expired).")
                if not dry_run:
                    cursor.execute("""
                        UPDATE jobs 
                        SET status = 'Closed (Expired)', score = 3, job_description = ?
                        WHERE id = ?
                    """, (job_desc, job_id))
                    conn.commit()
            else:
                print("    [!] Fetch failed or too short.")
                if dry_run:
                    dry_run_fail += 1
                else:
                    cursor.execute("""
                        UPDATE jobs 
                        SET status = 'Fetch Failed / Manual Review' 
                        WHERE id = ?
                    """, (job_id,))
                    conn.commit()
            continue
            
        print(f"    [+] Successfully fetched description ({len(job_desc)} chars).")
        
        if dry_run:
            # Just preview — no LLM call
            dry_run_ok += 1
            preview = job_desc[:400].replace('\n', ' ')
            print(f"    [PREVIEW] {preview}...")
            print(f"    [DRY-RUN] ✓ Scrape OK — DeepSeek would receive {len(job_desc)} chars of context.")
            continue

        print(f"    Evaluating with DeepSeek...")
        try:
            eval_result = await evaluate_job_with_llm(api_key, profile_data, job_desc)
            
            # If the job is outside the US or Canada, force status to Ineligible (Non-US/Canada) and score to 3
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
                    core_alignments = ?, 
                    technical_gaps = ?, 
                    vulnerability_analysis = ?, 
                    overall_fit_percentage = ?, 
                    confidence_score = ?, 
                    date_evaluated = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (
                status_str,
                score_val,
                eval_result.suitability_reason,
                job_desc,
                json.dumps(eval_result.core_alignments),
                json.dumps(eval_result.technical_gaps),
                eval_result.vulnerability_analysis,
                eval_result.match_metrics.overall_fit_percentage,
                eval_result.match_metrics.confidence_score,
                job_id
            ))
            conn.commit()
            print(f"    [+] Evaluated: Score = {score_val} ({status_str})")
            print(f"    [+] Reason: {eval_result.suitability_reason}")
        except Exception as e:
            print(f"    [!] Error during evaluation: {e}")
            cursor.execute("""
                UPDATE jobs 
                SET status = 'Evaluation Error' 
                WHERE id = ?
            """, (job_id,))
            conn.commit()
            
    conn.close()
    if dry_run:
        print(f"\n[DRY-RUN] Done. Scraped OK: {dry_run_ok}, Failed: {dry_run_fail}")
        print(f"[DRY-RUN] Pipeline validated — run without --dry-run on Windows with DEEPSEEK_API_KEY to evaluate.")
    else:
        print("\n[+] Evaluation loop complete.")
        export_db_to_csv()

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
        SELECT id, company, role, location, apply_url, overall_fit_percentage 
        FROM jobs 
        WHERE score = ? AND status LIKE 'Eligible%'
        ORDER BY overall_fit_percentage DESC
    """, (priority,))
    jobs = cursor.fetchall()
    
    print(f"\n=== Priority {priority} Jobs (Count: {len(jobs)}) ===")
    for job_id, company, role, location, url, fit in jobs:
        print(f"[{job_id}] {company} - {role} ({location}) | Fit: {fit}%")
        print(f"    Link: {url}")
        print("-" * 60)
    conn.close()

def mark_applied(job_id):
    """Manually mark a job as Applied in the database."""
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
        
    cursor.execute("UPDATE jobs SET status = 'Applied' WHERE id = ?", (job_id,))
    conn.commit()
    conn.close()
    print(f"[+] Successfully marked {job[0]} - {job[1]} as 'Applied'.")
    export_db_to_csv()

def detect_applicator(url: str) -> str | None:
    """Return the applicator script path for a given job URL, or None if unsupported."""
    base = Path(__file__).parent
    if "myworkdayjobs.com" in url or "workday.com" in url:
        return str(base / "app_workday_v3.py")
    # Add more handlers here as they are built:
    # if "greenhouse.io" in url: return str(base / "app_greenhouse.py")
    return None

def run_apply_loop():
    """Loop through high priority eligible jobs from highest score to lowest and launch the applicator."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Select all active jobs that are eligible (Priority 1 or 2)
    cursor.execute("""
        SELECT id, company, role, location, score, overall_fit_percentage, apply_url 
        FROM jobs 
        WHERE status LIKE 'Eligible (Priority %'
        ORDER BY score ASC, overall_fit_percentage DESC, date_added DESC
    """)
    jobs = cursor.fetchall()
    conn.close()
    
    if not jobs:
        print("[*] No eligible jobs found to apply.")
        return
        
    print(f"\n[*] Found {len(jobs)} eligible jobs for application loop.")
    print("[*] Starting loop from highest score/priority to lowest...")
    
    import subprocess
    import sys
    
    for idx, (job_id, company, role, location, score, fit, url) in enumerate(jobs):
        print("\n" + "="*80)
        print(f"[{idx+1}/{len(jobs)}] Job ID {job_id}: {company} - {role}")
        print(f"      Location: {location} | Priority Score: {score} | Fit: {fit}%")
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
    csv_path = Path(__file__).parent / "jobs_tracker.csv"
    details_path = Path(__file__).parent / "jobs_details.json"
    
    try:
        import csv
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Fetch summary columns for CSV
        cursor.execute("""
            SELECT id, company, role, location, category, status, score, 
                   overall_fit_percentage, confidence_score, date_added, date_evaluated, 
                   suitability_reason, apply_url
            FROM jobs
            ORDER BY score ASC, overall_fit_percentage DESC, date_added DESC
        """)
        csv_rows = cursor.fetchall()
        
        csv_headers = [
            "ID", "Company", "Role", "Location", "Category", "Status", "Score", 
            "Overall Fit %", "Confidence Score", "Date Added", "Date Evaluated", 
            "Suitability Reason", "Apply URL"
        ]
        
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(csv_headers)
            writer.writerows(csv_rows)
        print(f"[+] CSV Update: Exported clean jobs list to '{csv_path}'.")
        
        # Fetch details columns for JSON
        cursor.execute("""
            SELECT apply_url, job_description, core_alignments, technical_gaps, vulnerability_analysis
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
                
        for apply_url, desc, alignments_json, gaps_json, vuln in details_rows:
            try:
                alignments = json.loads(alignments_json) if alignments_json else []
            except Exception:
                alignments = []
            try:
                gaps = json.loads(gaps_json) if gaps_json else []
            except Exception:
                gaps = []
                
            existing_details[apply_url] = {
                "job_description": desc,
                "core_alignments": alignments,
                "technical_gaps": gaps,
                "vulnerability_analysis": vuln
            }
            
        with open(details_path, "w", encoding="utf-8") as f:
            json.dump(existing_details, f, indent=2, ensure_ascii=False)
        print(f"[+] Details Cache: Exported detailed job descriptions to '{details_path}'.")
        
        conn.close()
    except Exception as e:
        print(f"[!] Warning: Failed to export database data: {e}")

# --- 6. CLI Entry Point ---

def main():
    parser = argparse.ArgumentParser(description="Simplify Jobs Aggregator & Match Tracker")
    parser.add_argument("action", choices=["ingest", "evaluate", "status", "list", "apply", "apply-loop", "csv"], help="Action to perform")
    parser.add_argument("--limit", type=int, default=10, help="Number of pending jobs to evaluate. Set to -1 to evaluate all pending. (default 10)")
    parser.add_argument("--priority", type=int, default=1, choices=[1, 2, 3], help="Priority level to list (default 1)")
    parser.add_argument("--id", type=int, help="Job ID to mark as applied")
    parser.add_argument("--dry-run", action="store_true", help="For 'evaluate': scrape job descriptions only, skip DeepSeek LLM calls (validates pipeline)")
    
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
        if args.action in ("ingest", "apply", "csv", "status") or (args.action == "evaluate" and not args.dry_run):
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
