#!/usr/bin/env python3
"""
job_tracker.py - Unified CLI for Job Ingestion, Match Evaluation & Application Tracking.

Commands:
  python src/job_tracker.py ingest              Pull latest listings & mark removed jobs
  python src/job_tracker.py evaluate [--limit]  Scrape & score pending jobs with DeepSeek
  python src/job_tracker.py status              Summary of active & filtered applications
  python src/job_tracker.py list [--priority]   Show Priority 1 or 2 eligible jobs
  python src/job_tracker.py apply [--id|--url]  Mark a job as Applied
  python src/job_tracker.py apply-loop          Interactive prompt to apply to P1 jobs
  python src/job_tracker.py csv                 Synchronize DB with jobs_tracker.csv
"""

import sys
import os
import json
import sqlite3
import argparse
import asyncio
import datetime
import subprocess
import requests
from pathlib import Path
from dotenv import load_dotenv

# Re-exports for backward compatibility with automation bots (app_workday, app_greenhouse, run_batch)
from tracker_db import (
    DATA_DIR, DB_PATH, CSV_PATH, DETAILS_PATH, PROFILE_PATH,
    clean_url, load_applied_manifest, save_to_applied_manifest,
    load_filtered_history, record_filtered_jobs, is_filtered_status,
    load_snoozed_jobs, snooze_job_by_url, is_job_snoozed,
    get_db, import_csv_to_db, export_db_to_csv, sync_applied_manifest,
    mark_applied_by_url, mark_closed_expired_by_url
)
from scraper import (
    EXPIRED_INDICATORS, ALREADY_APPLIED_INDICATORS, is_us_or_canada, parse_jobs_from_markdown,
    detect_applicator
)
from evaluator import (
    SCORING_RUBRIC, JobEvaluation, run_evaluate, run_export_prompts, run_import_scores
)

# Load environment variables
load_dotenv(dotenv_path=DATA_DIR / ".env")

README_URL = "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/README.md"

# --- Ingestion Commands ---

def run_ingest():
    """Download latest SimplifyJobs README dev branch and parse into active tracker."""
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

    filtered_manifest = load_filtered_history()
    active_urls = set()
    new_jobs_count = 0
    closed_jobs_count = 0

    conn = get_db()
    cursor = conn.cursor()

    for job in jobs:
        url = job['apply_url']
        c_url = clean_url(url)

        # Skip if previously rejected/filtered
        if c_url in filtered_manifest or url in filtered_manifest:
            continue

        # If marked closed with lock symbol in markdown, archive and remove from DB
        if job['is_closed']:
            record_filtered_jobs({url: {
                "reason": "Closed (Lock symbol in Simplify)",
                "company": job['company'],
                "role": job['role']
            }})
            cursor.execute("DELETE FROM jobs WHERE apply_url = ?", (url,))
            closed_jobs_count += 1
            continue

        active_urls.add(url)

        try:
            cursor.execute("SELECT id, status FROM jobs WHERE apply_url = ?", (url,))
            existing = cursor.fetchone()
            if not existing:
                cursor.execute("""
                    INSERT INTO jobs (company, role, location, apply_url, category, status, score)
                    VALUES (?, ?, ?, ?, ?, 'Pending Evaluation', NULL)
                """, (job['company'], job['role'], job['location'], url, job['category']))
                new_jobs_count += 1
        except Exception as e:
            print(f"[!] Error inserting job {url}: {e}")

    # Remove active jobs that disappeared from Simplify list
    cursor.execute("""
        SELECT id, company, role, apply_url FROM jobs
        WHERE status IN ('Pending Evaluation', 'Eligible (Priority 1)', 'Eligible (Priority 2)', 'Fetch Failed / Manual Review', 'Evaluation Error')
    """)
    db_active_jobs = cursor.fetchall()

    removed_count = 0
    removed_entries = {}
    for job_id, company, role, apply_url in db_active_jobs:
        if apply_url not in active_urls:
            removed_entries[apply_url] = {
                "reason": "Closed (Removed from List)",
                "company": company,
                "role": role
            }
            cursor.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            removed_count += 1

    if removed_entries:
        record_filtered_jobs(removed_entries)

    conn.commit()
    conn.close()

    print(f"[+] Ingestion complete:")
    print(f"    - Added {new_jobs_count} new unique jobs.")
    print(f"    - Purged {closed_jobs_count} closed jobs (lock symbol).")
    print(f"    - Purged {removed_count} inactive jobs (removed from upstream list).")
    export_db_to_csv()

def run_ingest_lever(board_token: str):
    """Fetch jobs from a Lever API board (e.g. zoox)."""
    conn = get_db()
    cursor = conn.cursor()

    print(f"[*] Fetching jobs from Lever board: {board_token}...")
    try:
        resp = requests.get(f"https://api.lever.co/v0/postings/{board_token}", timeout=15)
        resp.raise_for_status()
        jobs_data = resp.json()
    except Exception as e:
        print(f"[ERROR] Failed to fetch Lever API for {board_token}: {e}")
        conn.close()
        return

    filtered_manifest = load_filtered_history()
    inserted_count = 0

    for job in jobs_data:
        title = job.get("text", "")
        location = job.get("categories", {}).get("location", "")
        apply_url = clean_url(job.get("applyUrl", job.get("hostedUrl", "")))

        if apply_url in filtered_manifest or clean_url(apply_url) in filtered_manifest:
            continue

        desc = (job.get("descriptionPlain", "") + "\n" + job.get("additionalPlain", "")).strip()

        cursor.execute("SELECT status FROM jobs WHERE apply_url = ?", (apply_url,))
        if cursor.fetchone():
            continue

        cursor.execute("""
            INSERT INTO jobs (company, role, location, apply_url, category, status, job_description)
            VALUES (?, ?, ?, ?, 'SWE', 'Pending Evaluation', ?)
        """, (board_token.capitalize(), title, location, apply_url, desc))
        inserted_count += 1

    conn.commit()
    conn.close()
    print(f"[+] Lever Ingest complete: Added {inserted_count} new jobs for {board_token.capitalize()}.")
    export_db_to_csv()

# --- Status & Tracking Commands ---

def show_status():
    """Display count metrics for jobs stored in the tracker."""
    if not DB_PATH.exists():
        import_csv_to_db()

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status")
    stats = cursor.fetchall()

    cursor.execute("SELECT COUNT(*) FROM jobs")
    total = cursor.fetchone()[0]

    filtered_history = load_filtered_history()

    print("\n=== Job Database Status Summary ===")
    print(f"Active jobs recorded  : {total}")
    print(f"Filtered / Ineligible : {len(filtered_history)} (stored in filtered_history.json)")
    print("-" * 35)
    for status, count in stats:
        print(f"  {status:<30}: {count}")
    print("=" * 35)
    conn.close()

def list_priority_jobs(priority: int = 1):
    """List eligible jobs for a specific priority tier."""
    if not DB_PATH.exists():
        import_csv_to_db()

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT id, company, role, location, apply_url, suitability_reason
        FROM jobs
        WHERE score = ? AND status LIKE 'Eligible%'
        ORDER BY date_added DESC
    """, (priority,))
    jobs = cursor.fetchall()
    conn.close()

    print(f"\n=== Priority {priority} Jobs (Count: {len(jobs)}) ===")
    for job_id, company, role, location, url, reason in jobs:
        print(f"[{job_id}] {company} - {role} ({location}) | {reason or ''}")
        print(f"    Link: {url}")
        print("-" * 60)

def apply_job(job_ids=None, urls=None, search=None):
    """Mark jobs as Applied by ID, URL, or text query."""
    if not DB_PATH.exists():
        import_csv_to_db()

    conn = get_db()
    cursor = conn.cursor()
    matched_jobs = []

    if job_ids:
        placeholders = ",".join("?" * len(job_ids))
        cursor.execute(f"SELECT id, company, role, apply_url FROM jobs WHERE id IN ({placeholders})", job_ids)
        matched_jobs.extend(cursor.fetchall())

    if urls:
        for u in urls:
            c_u = clean_url(u)
            cursor.execute("SELECT id, company, role, apply_url FROM jobs WHERE apply_url = ? OR apply_url LIKE ?", (u, f"%{c_u}%"))
            matched_jobs.extend(cursor.fetchall())

    if search:
        query = f"%{search}%"
        cursor.execute("SELECT id, company, role, apply_url FROM jobs WHERE company LIKE ? OR role LIKE ?", (query, query))
        matched_jobs.extend(cursor.fetchall())

    conn.close()

    if not matched_jobs:
        print("[!] No matching jobs found to mark as applied.")
        return

    today_str = datetime.date.today().isoformat()
    unique_matches = {job[0]: job for job in matched_jobs}.values()

    for job_id, company, role, url in unique_matches:
        mark_applied_by_url(url, date_str=today_str)
        print(f"[+] Marked as Applied: [{job_id}] {company} - {role}")

def interactive_apply_loop():
    """Iterate through Eligible Priority 1 jobs and prompt to apply."""
    if not DB_PATH.exists():
        import_csv_to_db()

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT id, company, role, location, apply_url, suitability_reason
        FROM jobs
        WHERE status = 'Eligible (Priority 1)'
        ORDER BY date_added DESC
    """)
    jobs = cursor.fetchall()
    conn.close()

    if not jobs:
        print("[*] No Priority 1 eligible jobs to apply for.")
        return

    print(f"\n=== Interactive Apply Loop ({len(jobs)} Priority 1 Jobs) ===")
    print("Options:")
    print("  [y]es   : Launch applicator bot")
    print("  [s]kip  : Skip this job for now")
    print("  [a]pply : Mark as applied manually")
    print("  [q]uit  : Exit apply loop\n")

    for idx, (job_id, company, role, location, url, reason) in enumerate(jobs, 1):
        app_script = detect_applicator(url)
        app_name = Path(app_script).stem if app_script else "Manual (No bot supported)"

        print(f"\n[{idx}/{len(jobs)}] [ID {job_id}] {company} - {role}")
        print(f"Location: {location}")
        print(f"Reason  : {reason}")
        print(f"Engine  : {app_name}")
        print(f"URL     : {url}")

        choice = input("Action [y/s/a/q]? ").strip().lower()
        if choice == 'q':
            break
        elif choice == 's':
            continue
        elif choice == 'a':
            apply_job(job_ids=[job_id])
            continue
        elif choice != 'y':
            continue

        if not app_script:
            print("[!] No automated applicator available for this link. Please apply manually via URL.")
            continue

        print(f"[*] Launching applicator for {company} - {role}...")
        try:
            subprocess.run([sys.executable, app_script, url], check=True)
            print(f"[+] Finished session for {company} - {role}.")
        except Exception as e:
            print(f"[!] Error running applicator: {e}")

    print("\n[*] Apply loop finished.")

# --- CLI Entry Point ---

def main():
    parser = argparse.ArgumentParser(description="Job Ingestion, Match Tracker & Application CLI")
    parser.add_argument("action", choices=["ingest", "ingest-lever", "evaluate", "status", "list", "apply", "apply-loop", "csv"], help="Action to perform")
    parser.add_argument("--board", type=str, help="Lever board token for ingest-lever (e.g. 'zoox')")
    parser.add_argument("--limit", type=int, default=10, help="Number of pending jobs to evaluate (-1 = all, default 10)")
    parser.add_argument("--priority", type=int, default=1, choices=[1, 2, 3], help="Priority tier to list (1 or 2)")
    parser.add_argument("--id", type=int, nargs="+", help="Job ID(s) to mark as applied")
    parser.add_argument("--url", type=str, nargs="+", help="Job URL(s) to mark as applied")
    parser.add_argument("--search", type=str, help="Search query (company or role) to mark matching jobs as applied")
    parser.add_argument("--dry-run", action="store_true", help="Scrape job descriptions only, skip LLM calls")
    parser.add_argument("--export-prompts", action="store_true", help="Export pending jobs to artifacts/eval_pending.json for offline scoring")
    parser.add_argument("--import-scores", action="store_true", help="Import Claude-scored artifacts/eval_scores.json")

    args = parser.parse_args()

    # Ensure database is synced from CSV on startup
    import_csv_to_db()

    try:
        if args.action == "ingest":
            run_ingest()
        elif args.action == "ingest-lever":
            if not args.board:
                print("[ERROR] Please provide --board <token> (e.g. --board zoox)")
                sys.exit(1)
            run_ingest_lever(args.board)
        elif args.action == "evaluate":
            if args.export_prompts:
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
            apply_job(job_ids=args.id, urls=args.url, search=args.search)
        elif args.action == "apply-loop":
            interactive_apply_loop()
        elif args.action == "csv":
            export_db_to_csv()
            print(f"[+] Database cleanly synchronized with '{CSV_PATH}'.")
    finally:
        # Keep CSV synchronized with latest state
        export_db_to_csv()

if __name__ == "__main__":
    main()
