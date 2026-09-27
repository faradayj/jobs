"""
tracker_db.py - Data management for jobs tracker.
Handles SQLite operations, CSV synchronization, details JSON caching,
applied manifest, and filtered/ineligible history tracking.
"""

import csv
import json
import sqlite3
import datetime
import re
from pathlib import Path
from urllib.parse import urlparse, urlunparse

# Base directories and paths
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
CSV_PATH = DATA_DIR / "jobs_tracker.csv"
DETAILS_PATH = DATA_DIR / "jobs_details.json"
APPLIED_HISTORY_PATH = DATA_DIR / "applied_history.json"
FILTERED_HISTORY_PATH = DATA_DIR / "filtered_history.json"
SNOOZED_HISTORY_PATH = DATA_DIR / "snoozed_jobs.json"
DB_PATH = DATA_DIR / "jobs.db"
PROFILE_PATH = DATA_DIR / "library.json"

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
        eval_metadata TEXT,
        salary_midpoint INTEGER,
        target_skills TEXT,
        date_added DATETIME DEFAULT CURRENT_TIMESTAMP,
        date_evaluated DATETIME,
        date_applied DATETIME
    )
"""

def clean_url(url: str) -> str:
    """Normalize URL by stripping query parameters, trailing slashes, and locale prefixes (e.g. /en-US/)."""
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        path = parsed.path.rstrip("/")
        # Strip Workday/standard locale segment like /en-US/, /en-CA/, etc.
        path = re.sub(r"^/([a-zA-Z]{2}(-[a-zA-Z]{2,4})?)(/.*)$", r"\3", path)
        return urlunparse((parsed.scheme, parsed.netloc.lower(), path, "", "", ""))
    except Exception:
        return url.strip()

# --- Manifests & History ---

def load_applied_manifest() -> dict:
    """Load applied jobs history."""
    if not APPLIED_HISTORY_PATH.exists():
        return {}
    try:
        with open(APPLIED_HISTORY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[!] Warning: Failed to load applied_history.json: {e}")
        return {}

def save_to_applied_manifest(url: str, company: str = "", role: str = "", date_applied: str = None):
    """Save an applied job to the persistent manifest."""
    if not date_applied:
        date_applied = datetime.date.today().isoformat()
    manifest = load_applied_manifest()
    c_url = clean_url(url)
    manifest[c_url] = {
        "raw_url": url,
        "company": company,
        "role": role,
        "date_applied": date_applied,
        "updated_at": datetime.datetime.now().isoformat()
    }
    try:
        with open(APPLIED_HISTORY_PATH, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)
        print(f"  [manifest] Saved {company or 'Job'} to persistent applied_history.json")
    except Exception as e:
        print(f"[!] Warning: Failed to write applied_history.json: {e}")

def load_filtered_history() -> dict:
    """Load ineligible and closed jobs history."""
    if not FILTERED_HISTORY_PATH.exists():
        return {}
    try:
        with open(FILTERED_HISTORY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[!] Warning: Failed to load filtered_history.json: {e}")
        return {}

def record_filtered_jobs(entries: dict):
    """Save ineligible/closed jobs to filtered_history.json."""
    if not entries:
        return
    history = load_filtered_history()
    today_str = datetime.date.today().isoformat()
    for url, info in entries.items():
        if not url:
            continue
        c_url = clean_url(url)
        if isinstance(info, str):
            reason = info
            meta = {}
        else:
            reason = info.get("reason", "Filtered")
            meta = info
        history[c_url] = {
            "raw_url": url,
            "reason": reason,
            "company": meta.get("company", ""),
            "role": meta.get("role", ""),
            "date_filtered": meta.get("date", today_str),
            "updated_at": datetime.datetime.now().isoformat()
        }
    try:
        with open(FILTERED_HISTORY_PATH, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[!] Warning: Failed to write filtered_history.json: {e}")

def load_snoozed_jobs() -> dict:
    """Load snoozed/skipped jobs history."""
    if not SNOOZED_HISTORY_PATH.exists():
        return {}
    try:
        with open(SNOOZED_HISTORY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[!] Warning: Failed to load snoozed_jobs.json: {e}")
        return {}

def snooze_job_by_url(url: str, days: int = 3, reason: str = "Skipped by user", company: str = "", role: str = "") -> str:
    """Snooze a job for N days. Returns the snooze_until date string (YYYY-MM-DD)."""
    snooze_until = (datetime.date.today() + datetime.timedelta(days=days)).isoformat()
    manifest = load_snoozed_jobs()
    c_url = clean_url(url)
    manifest[c_url] = {
        "raw_url": url,
        "company": company,
        "role": role,
        "snooze_until": snooze_until,
        "snoozed_at": datetime.date.today().isoformat(),
        "days": days,
        "reason": reason,
        "updated_at": datetime.datetime.now().isoformat()
    }
    try:
        with open(SNOOZED_HISTORY_PATH, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)
        print(f"  [snooze] Saved to snoozed_jobs.json (snoozed for {days} days until {snooze_until})")
    except Exception as e:
        print(f"[!] Warning: Failed to write snoozed_jobs.json: {e}")
    return snooze_until

def is_job_snoozed(url: str) -> bool:
    """Check if a job is currently within its snooze window."""
    manifest = load_snoozed_jobs()
    c_url = clean_url(url)
    entry = manifest.get(c_url) or manifest.get(url)
    if not entry:
        return False
    until = entry.get("snooze_until", "")
    if not until:
        return False
    return until >= datetime.date.today().isoformat()

def is_filtered_status(status: str, score: int = None) -> bool:
    """Check if a job status/score qualifies as ineligible or closed."""
    if not status:
        return False
    s = status.strip()
    if s.startswith("Ineligible") or s.startswith("Closed"):
        return True
    if score is not None and score >= 3:
        return True
    return False

# --- Database & CSV Management ---

def get_db():
    """Get SQLite connection, ensure schema, and run column migrations."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(SCHEMA_SQL)
    conn.commit()

    # Migration: ensure eval_metadata, salary_midpoint, target_skills columns exist
    for col_def in [
        ("eval_metadata", "TEXT"),
        ("salary_midpoint", "INTEGER"),
        ("target_skills", "TEXT")
    ]:
        try:
            cur.execute(f"ALTER TABLE jobs ADD COLUMN {col_def[0]} {col_def[1]}")
            conn.commit()
        except Exception:
            pass
    return conn

def import_csv_to_db():
    """Load active jobs from jobs_tracker.csv and jobs_details.json into SQLite DB."""
    if not CSV_PATH.exists():
        return

    details_cache = {}
    if DETAILS_PATH.exists():
        try:
            with open(DETAILS_PATH, "r", encoding="utf-8") as f:
                details_cache = json.load(f)
        except Exception:
            details_cache = {}

    conn = get_db()
    cursor = conn.cursor()

    filtered_to_record = {}
    with open(CSV_PATH, "r", newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        headers = next(reader, None)
        if not headers:
            conn.close()
            return
        header_map = {h: i for i, h in enumerate(headers)}

        for row in reader:
            if not row:
                continue

            def get_val(col, default=None):
                return row[header_map[col]] if col in header_map and header_map[col] < len(row) else default

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
            reason = get_val("Suitability Reason")
            apply_url = get_val("Apply URL")

            # Prune ineligible/closed jobs
            if is_filtered_status(status, score):
                filtered_to_record[apply_url] = {
                    "reason": status or reason or "Ineligible/Closed",
                    "company": company or "",
                    "role": role or "",
                    "date": date_evaluated or date_added
                }
                continue

            desc = get_val("Job Description") or (details_cache.get(apply_url, {}).get("job_description", "") if apply_url else "")

            cursor.execute("""
                INSERT OR REPLACE INTO jobs (
                    id, company, role, location, apply_url, category, status, score,
                    suitability_reason, job_description, date_added, date_evaluated, date_applied
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                job_id, company, role, location, apply_url, category, status, score,
                reason, desc, date_added, date_evaluated, date_applied
            ))

    if filtered_to_record:
        record_filtered_jobs(filtered_to_record)

    conn.commit()
    conn.close()
    sync_applied_manifest()

def export_db_to_csv():
    """Export active jobs from DB to jobs_tracker.csv and jobs_details.json."""
    if not DB_PATH.exists():
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Purge any remaining ineligible/closed jobs before exporting
    cursor.execute("""
        SELECT apply_url, company, role, status, score, suitability_reason, date_evaluated
        FROM jobs
        WHERE status LIKE 'Ineligible%' OR status LIKE 'Closed%' OR (score IS NOT NULL AND score >= 3)
    """)
    to_purge = cursor.fetchall()
    if to_purge:
        entries = {
            r[0]: {
                "reason": r[3] or r[5] or "Ineligible/Closed",
                "company": r[1] or "",
                "role": r[2] or "",
                "date": r[6]
            }
            for r in to_purge if r[0]
        }
        record_filtered_jobs(entries)
        cursor.execute("""
            DELETE FROM jobs
            WHERE status LIKE 'Ineligible%' OR status LIKE 'Closed%' OR (score IS NOT NULL AND score >= 3)
        """)
        conn.commit()

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

    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(csv_headers)
        writer.writerows(csv_rows)

    # Export descriptions and eval metadata for active jobs
    cursor.execute("""
        SELECT apply_url, job_description, eval_metadata
        FROM jobs
        WHERE job_description IS NOT NULL AND job_description != ''
    """)
    details_rows = cursor.fetchall()
    clean_details = {}
    for r in details_rows:
        url = r[0]
        desc = r[1]
        eval_meta = None
        if len(r) > 2 and r[2]:
            try:
                eval_meta = json.loads(r[2])
            except Exception:
                eval_meta = r[2]
        clean_details[url] = {
            "job_description": desc,
            "eval_metadata": eval_meta
        }

    with open(DETAILS_PATH, "w", encoding="utf-8") as f:
        json.dump(clean_details, f, indent=2, ensure_ascii=False)

    conn.close()

def get_job_eval_metadata(url: str) -> dict | None:
    """Fetch stored evaluation metadata (salary, locations, skills) for a job URL."""
    if not url:
        return None
    c_url = clean_url(url)

    # 1. Try DB
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT eval_metadata FROM jobs WHERE apply_url = ? OR apply_url = ?", (url, c_url))
        row = cursor.fetchone()
        conn.close()
        if row and row[0]:
            try:
                return json.loads(row[0])
            except Exception:
                pass
    except Exception:
        pass

    # 2. Try jobs_details.json
    if DETAILS_PATH.exists():
        try:
            with open(DETAILS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                entry = data.get(url) or data.get(c_url)
                if entry and isinstance(entry, dict):
                    return entry.get("eval_metadata")
        except Exception:
            pass

    return None

def sync_applied_manifest():
    """Ensure all jobs in applied_history.json are marked as Applied in DB and CSV."""
    manifest = load_applied_manifest()
    if not manifest or not CSV_PATH.exists():
        return

    with open(CSV_PATH, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)

    if "Date Applied" not in fieldnames:
        fieldnames.append("Date Applied")

    clean_manifest = {}
    for k, entry in manifest.items():
        clean_manifest[clean_url(k)] = entry
        if entry.get("raw_url"):
            clean_manifest[clean_url(entry["raw_url"])] = entry

    updated_count = 0
    for row in rows:
        r_url = row.get("Apply URL", "")
        c_r_url = clean_url(r_url)
        entry = clean_manifest.get(c_r_url)
        if entry:
            if row.get("Status") != "Applied":
                row["Status"] = "Applied"
                row["Date Applied"] = entry.get("date_applied", "")
                updated_count += 1
            elif not row.get("Date Applied") and entry.get("date_applied"):
                row["Date Applied"] = entry.get("date_applied")
                updated_count += 1

    if updated_count > 0:
        with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        print(f"[+] sync_applied_manifest: updated {updated_count} jobs in {CSV_PATH.name}")

    if DB_PATH.exists():
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT id, apply_url FROM jobs WHERE status != 'Applied' OR date_applied IS NULL")
        unapplied_jobs = cursor.fetchall()
        db_updates = 0
        for j_id, j_url in unapplied_jobs:
            c_j = clean_url(j_url)
            entry = clean_manifest.get(c_j)
            if entry:
                cursor.execute(
                    "UPDATE jobs SET status = 'Applied', date_applied = ? WHERE id = ?",
                    (entry.get("date_applied", ""), j_id)
                )
                db_updates += 1
        if db_updates > 0:
            conn.commit()
            print(f"[+] sync_applied_manifest: updated {db_updates} jobs in {DB_PATH.name}")
        conn.close()

# --- Application Helpers (Interface for external bots) ---

def mark_applied_by_url(url: str, date_str: str = None) -> bool:
    """Mark a job as Applied using its URL."""
    if not date_str:
        date_str = datetime.date.today().isoformat()

    c_url = clean_url(url)
    company = ""
    role = ""

    # Check DB or CSV to get metadata
    if DB_PATH.exists():
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT id, company, role, apply_url FROM jobs")
        all_jobs = cursor.fetchall()
        matched_ids = []
        for j_id, j_co, j_ro, j_url in all_jobs:
            if j_url == url or clean_url(j_url) == c_url:
                matched_ids.append(j_id)
                if not company and j_co: company = j_co
                if not role and j_ro: role = j_ro

        for mid in matched_ids:
            cursor.execute("UPDATE jobs SET status = 'Applied', date_applied = ? WHERE id = ?", (date_str, mid))
        conn.commit()
        conn.close()

    save_to_applied_manifest(url, company=company, role=role, date_applied=date_str)
    export_db_to_csv()
    sync_applied_manifest()
    print(f"[+] Successfully marked as Applied: {company or url}")
    return True

def mark_closed_expired_by_url(url: str) -> bool:
    """Record an expired URL into filtered_history.json and delete from active tracker."""
    c_url = clean_url(url)
    company = ""
    role = ""

    if DB_PATH.exists():
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT id, company, role, apply_url FROM jobs")
        all_jobs = cursor.fetchall()
        matched_ids = []
        for j_id, j_co, j_ro, j_url in all_jobs:
            if j_url == url or clean_url(j_url) == c_url:
                matched_ids.append(j_id)
                if not company and j_co: company = j_co
                if not role and j_ro: role = j_ro

        for mid in matched_ids:
            cursor.execute("DELETE FROM jobs WHERE id = ?", (mid,))
        conn.commit()
        conn.close()

    record_filtered_jobs({url: {
        "reason": "Closed (Expired)",
        "company": company,
        "role": role,
        "date": datetime.date.today().isoformat()
    }})
    export_db_to_csv()
    print(f"[+] Recorded expired job to filtered_history and purged from active tracker: {url}")
    return True
