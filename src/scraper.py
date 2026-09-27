"""
scraper.py - Web scraping and job extraction utilities.
Handles scraping job descriptions via Greenhouse APIs, iCIMS endpoints,
and headless Playwright fallback, as well as parsing SimplifyJobs markdown tables.
"""

import re
import requests
from pathlib import Path
from urllib.parse import urlparse, parse_qsl
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from app_common import _find_chrome
from tracker_db import clean_url

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

ALREADY_APPLIED_INDICATORS = [
    "you've already applied for this job",
    "you have already applied for this job",
    "you've already applied to this job",
    "you have already applied to this job",
    "you've already applied",
    "you have already applied",
    "already applied for this job",
    "already applied to this job",
    "already applied for this position",
    "already applied to this position",
    "already submitted an application for this position",
    "already submitted an application for this job",
    "already submitted an application to this job",
    "already submitted an application",
    "an application has already been submitted",
    "you already applied",
    "application already submitted",
    "already applied",
    "view my applications",
]

def is_us_or_canada(location: str) -> bool:
    """Check if the job location is within the United States or Canada."""
    if not location:
        return True

    loc_lower = location.lower()
    if "united states" in loc_lower or "usa" in loc_lower or "canada" in loc_lower:
        return True

    us_states = [
        'AL', 'AK', 'AZ', 'AR', 'CA', 'CO', 'CT', 'DE', 'FL', 'GA', 'HI', 'ID', 'IL', 'IN', 'IA', 'KS', 'KY', 'LA',
        'ME', 'MD', 'MA', 'MI', 'MN', 'MS', 'MO', 'MT', 'NE', 'NV', 'NH', 'NJ', 'NM', 'NY', 'NC', 'ND', 'OH', 'OK',
        'OR', 'PA', 'RI', 'SC', 'SD', 'TN', 'TX', 'UT', 'VT', 'VA', 'WA', 'WV', 'WI', 'WY', 'DC'
    ]
    ca_provinces = ['ON', 'BC', 'QC', 'AB', 'MB', 'SK', 'NS', 'NB', 'NL', 'PE', 'NT', 'YT', 'NU']

    for code in us_states + ca_provinces:
        if re.search(r'\b' + re.escape(code.lower()) + r'\b', loc_lower):
            return True

    major_cities = [
        'nyc', 'sf', 'la', 'seattle', 'boston', 'chicago', 'austin', 'denver', 'silicon valley',
        'toronto', 'vancouver', 'montreal', 'calgary', 'ottawa', 'waterloo', 'halifax'
    ]
    for city in major_cities:
        if re.search(r'\b' + re.escape(city) + r'\b', loc_lower):
            return True

    non_us_indicators = [
        'uk', 'united kingdom', 'europe', 'london', 'germany', 'poland', 'india', 'singapore',
        'australia', 'ireland', 'netherlands', 'france', 'spain', 'italy', 'switzerland', 'sweden'
    ]
    for country in non_us_indicators:
        if re.search(r'\b' + re.escape(country) + r'\b', loc_lower):
            return False

    return True

def extract_apply_url(td) -> str:
    """Extract application URL from an HTML table cell."""
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

def parse_jobs_from_markdown(content: str) -> list[dict]:
    """Parse jobs out of HTML tables inside the README Markdown."""
    sections = re.split(r'^##\s+', content, flags=re.MULTILINE)
    jobs = []

    for section in sections:
        lines = section.split('\n')
        if not lines:
            continue
        title = lines[0].strip()

        category = None
        t_low = title.lower()
        if 'software engineering' in t_low:
            category = 'SWE'
        elif 'data science' in t_low or 'machine learning' in t_low or 'ai' in t_low:
            category = 'DS_ML'
        elif 'hardware engineering' in t_low:
            category = 'Hardware'
        elif 'product management' in t_low:
            category = 'PM'
        elif 'quantitative finance' in t_low:
            category = 'Quant'

        if not category:
            continue

        table_matches = re.findall(r'<table.*?>.*?</table>', section, re.DOTALL)
        for table_html in table_matches:
            soup = BeautifulSoup(table_html, 'html.parser')
            rows = soup.find_all('tr')
            previous_company = None

            for row in rows:
                cells = row.find_all('td')
                if len(cells) < 4:
                    continue

                company_text = cells[0].get_text(strip=True)
                if '↳' in company_text or company_text == '↳':
                    company = previous_company
                else:
                    a_comp = cells[0].find('a')
                    company = a_comp.get_text(strip=True) if a_comp else company_text
                    previous_company = company

                role = cells[1].get_text(strip=True)
                location = cells[2].get_text(separator=', ', strip=True)
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

def _fetch_greenhouse_api(board_token: str, job_id: str) -> str | None:
    """Fetch job content from the Greenhouse boards API."""
    try:
        api_url = f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs/{job_id}"
        resp = requests.get(api_url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            title = data.get("title", "")
            location = data.get("location", {}).get("name", "")
            soup = BeautifulSoup(data.get("content", ""), 'html.parser')
            return f"{title}\n{location}\n\n{soup.get_text()}".strip()
    except Exception as e:
        print(f"[!] Greenhouse API fetch exception ({board_token}/{job_id}): {e}")
    return None

def try_fetch_from_greenhouse_api(url: str) -> str | None:
    """Attempt to parse Greenhouse token and job ID from URL and fetch via API."""
    try:
        parsed = urlparse(url)
        netloc = parsed.netloc.lower()
        path = parsed.path

        board_token = None
        job_id = None

        DOMAIN_TO_TOKEN = {
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
                board_token = DOMAIN_TO_TOKEN.get(netloc) or (netloc.split('.')[-2] if len(netloc.split('.')) >= 2 else None)
            else:
                parts = [p for p in path.split('/') if p]
                if len(parts) >= 2 and parts[-2] in ('jobs', 'careers', 'job') and parts[-1].isdigit():
                    job_id = parts[-1]
                    board_token = DOMAIN_TO_TOKEN.get(netloc) or (netloc.split('.')[-2] if len(netloc.split('.')) >= 2 else None)

        if board_token and job_id:
            return _fetch_greenhouse_api(board_token, job_id)
    except Exception as e:
        print(f"[!] Greenhouse API URL parse exception for {url}: {e}")
    return None

async def fetch_job_description(apply_url: str) -> str | None:
    """Scrape the full body text of a job page using API, HTTP, or Playwright."""
    gh_text = try_fetch_from_greenhouse_api(apply_url)
    if gh_text:
        return gh_text

    if "icims.com" in apply_url:
        try:
            r = requests.get(apply_url, timeout=15)
            if r.status_code == 200:
                soup = BeautifulSoup(r.text, 'html.parser')
                content = soup.find(class_="iCIMS_JobContent") or soup
                return "\n".join([line.strip() for line in content.get_text().splitlines() if line.strip()])
        except Exception as e:
            print(f"[!] Warning: iCIMS requests fetch failed for {apply_url}: {e}")

    chrome_path = _find_chrome()

    async with async_playwright() as p:
        launch_kwargs = {
            "headless": True,
            "timeout": 60000,
            "args": ["--disable-blink-features=AutomationControlled", "--no-sandbox"]
        }
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
                "Referer": "https://www.google.com/"
            })
            await page.goto(apply_url, timeout=35000, wait_until="load")
            await page.wait_for_timeout(3000)

            # Check for embedded Greenhouse iframe
            for iframe in await page.locator("iframe").all():
                src = await iframe.get_attribute("src")
                if src and "greenhouse.io" in src:
                    parsed_src = urlparse(src)
                    qsl = dict(parse_qsl(parsed_src.query))
                    board_token = qsl.get("for") or qsl.get("board_token")
                    job_id = qsl.get("token") or qsl.get("gh_jid") or qsl.get("job_id")
                    if not job_id:
                        parts = [p for p in parsed_src.path.split('/') if p]
                        if parts and parts[-1].isdigit():
                            job_id = parts[-1]
                    if board_token and job_id:
                        result = _fetch_greenhouse_api(board_token, job_id)
                        if result:
                            return result

            text = await page.locator("body").inner_text()
            return text.strip()
        except Exception as e:
            try:
                await page.wait_for_load_state("load", timeout=10000)
                text = await page.locator("body").inner_text()
                return text.strip()
            except Exception:
                print(f"[!] Warning: Failed to load {apply_url}: {e}")
                return None
        finally:
            await browser.close()

def detect_applicator(url: str) -> str | None:
    """Return the applicator script path for a given job URL, or None if manual."""
    u = url.lower()
    base = Path(__file__).parent
    if "myworkdayjobs.com" in u or "workday.com" in u:
        return str(base / "app_workday.py")
    elif "greenhouse.io" in u or "gh_jid=" in u:
        return str(base / "app_greenhouse.py")
    elif "ashbyhq.com" in u:
        return str(base / "app_ashby.py")
    elif "lifeattiktok.com" in u or "careers.tiktok.com" in u or "jobs.bytedance.com" in u:
        return str(base / "app_tiktok.py")
    return None
