import requests
import json
import csv
import os
from pathlib import Path

# Base paths relative to project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DETAILS_PATH = DATA_DIR / "jobs_details.json"
TRACKER_PATH = DATA_DIR / "jobs_tracker.csv"

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/plain, */*'
}

QUERIES = ["data science", "machine learning"]

def fetch_jobs(query, offset=0):
    params = {
        'query': query,
        'category': 'data-science',
        'normalized_country_code': 'USA',
        'offset': offset,
        'result_limit': 100
    }
    r = requests.get('https://www.amazon.jobs/en/search.json', headers=HEADERS, params=params)
    if r.status_code == 200:
        return r.json()
    print(f"Failed to fetch {query} offset {offset}: {r.status_code}")
    return None

def main():
    jobs_dict = {}
    
    for q in QUERIES:
        print(f"Querying {q}...")
        offset = 0
        while offset <= 1000:
            data = fetch_jobs(q, offset)
            if not data or not data.get('jobs'):
                break
            
            for job in data.get('jobs', []):
                job_id = job.get('id_icims') or job.get('id')
                apply_url = f"https://www.amazon.jobs/en/jobs/{job_id}"
                
                desc = job.get('description') or ''
                basic = job.get('basic_qualifications') or ''
                preferred = job.get('preferred_qualifications') or ''
                full_desc = f"{desc}\n\nBasic Qualifications:\n{basic}\n\nPreferred Qualifications:\n{preferred}"
                
                title = job.get('title') or ''
                city = job.get('city') or ''
                state = job.get('state') or ''
                location = f"{city}, {state}" if city and state else (city or state or "USA")
                
                jobs_dict[apply_url] = {
                    'title': title,
                    'location': location,
                    'description': full_desc,
                    'id': job_id
                }
            
            offset += 100
            print(f" Fetched up to {offset}")

    print(f"Total unique Amazon jobs found: {len(jobs_dict)}")
    
    details = {}
    if DETAILS_PATH.exists():
        with open(DETAILS_PATH, 'r', encoding='utf-8') as f:
            details = json.load(f)
            
    for url, job in jobs_dict.items():
        details[url] = {'job_description': job['description']}
        
    with open(DETAILS_PATH, 'w', encoding='utf-8') as f:
        json.dump(details, f, indent=2)
        
    existing_urls = set()
    if TRACKER_PATH.exists():
        with open(TRACKER_PATH, 'r', encoding='utf-8') as f:
            reader = csv.reader(f)
            for row in reader:
                if len(row) > 0:
                    existing_urls.add(row[0])
                    
    with open(TRACKER_PATH, 'a', encoding='utf-8', newline='') as f:
        writer = csv.writer(f)
        added = 0
        for url, job in jobs_dict.items():
            if url not in existing_urls:
                writer.writerow([url, '', 'Amazon', job['title'], job['location'], '', 'Not Applied', 'Pending'])
                added += 1
                
    print(f"Added {added} new Amazon jobs to CSV!")

if __name__ == '__main__':
    main()
