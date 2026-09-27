#!/usr/bin/env python3
"""
Batch applicator runner — runs the right applicator script (Workday or Greenhouse)
on a list of URLs one-by-one, always headed (visible Chrome) so you can watch/interject
and review each application before it submits. Pauses between jobs so you can review
the report / submit / skip.
Usage:
    python3 src/run_batch.py                        # all P1 jobs with known applicator (Ashby excluded by default)
    python3 src/run_batch.py --include-ashby        # include Ashby jobs in batch
    python3 src/run_batch.py --host ashby           # only jobs routed to app_ashby.py
    python3 src/run_batch.py --ids 257 269 272      # specific job IDs
    python3 src/run_batch.py --start-id 257         # resume from a specific ID
    python3 src/run_batch.py --sim-ds               # rule-based only (no DeepSeek); Workday only
"""
import argparse
import csv
import datetime
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent))
from job_tracker import (
    detect_applicator, clean_url, load_applied_manifest,
    load_snoozed_jobs, snooze_job_by_url
)

ROOT = Path(__file__).parent.parent
CSV_PATH = ROOT / "data" / "jobs_tracker.csv"


def load_p1_jobs(include_snoozed: bool = False) -> tuple[list[dict], int]:
    applied_manifest = load_applied_manifest()
    applied_clean_urls = {clean_url(k) for k in applied_manifest.keys()}
    for entry in applied_manifest.values():
        if entry.get("raw_url"):
            applied_clean_urls.add(clean_url(entry["raw_url"]))

    snoozed = load_snoozed_jobs()
    today_str = datetime.date.today().isoformat()
    snoozed_clean_urls = {
        clean_url(k) for k, v in snoozed.items()
        if v.get("snooze_until", "") >= today_str
    }

    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    eligible = [
        r for r in rows
        if r.get("Status", "") == "Eligible (Priority 1)"
        and clean_url(r.get("Apply URL", "")) not in applied_clean_urls
        and detect_applicator(r.get("Apply URL", "")) is not None
    ]

    active = [r for r in eligible if include_snoozed or clean_url(r.get("Apply URL", "")) not in snoozed_clean_urls]
    snoozed_count = len(eligible) - len(active)
    return active, snoozed_count


def tenant(url: str) -> str:
    return urlparse(url).netloc.split(".")[0]


def print_banner(idx: int, total: int, job: dict):
    url = job["Apply URL"]
    script = detect_applicator(url)
    applicator = Path(script).stem if script else "unknown"
    print()
    print("=" * 70)
    print(f"  JOB {idx}/{total}  [ID {job['ID']}]  {job['Company']}")
    print(f"  Role    : {job['Role']}")
    print(f"  Location: {job['Location']}")
    print(f"  Reason  : {job.get('Suitability Reason', '')[:80]}")
    print(f"  Tenant  : {tenant(url)}  ({applicator})")
    print(f"  URL     : {url}")
    print("=" * 70)


JOB_TIMEOUT_SEC = 20 * 60  # 20 min: covers manual account creation + review inspection


def run_job(job: dict, extra_args: list[str]) -> int:
    script = detect_applicator(job["Apply URL"])
    if script is None:
        print(f"[BATCH] No applicator for URL — skipping: {job['Apply URL']}")
        return -1
    args_for_script = extra_args
    cmd = [
        sys.executable, "-u",
        script,
        job["Apply URL"],
        "--show",
        *args_for_script,
    ]
    print(f"\n[BATCH] Running: {' '.join(cmd)}\n")
    try:
        result = subprocess.run(cmd, cwd=str(ROOT), timeout=JOB_TIMEOUT_SEC)
        return result.returncode
    except subprocess.TimeoutExpired:
        print(f"\n[BATCH] ⚠ Job exceeded {JOB_TIMEOUT_SEC // 60} min — killed, moving on.")
        return -1


def prompt_continue(idx: int, total: int, next_job: dict = None) -> tuple[str, int]:
    """Returns (action, days) where action is 'next' | 'skip' | 'snooze' | 'quit'"""
    while True:
        print()
        print(f"[BATCH] Job {idx}/{total} done. What next?")
        print("  [Enter]  → next job")
        if next_job:
            print(f"  s        → skip next job ({next_job['Company']})")
            print(f"  s3       → snooze next job for 3 days")
        else:
            print("  s        → skip next job")
        print("  q        → quit batch")
        ans = input("  > ").strip().lower()
        if ans == "":
            return ("next", 0)
        if ans == "s":
            return ("skip", 0)
        m = re.match(r"^s(\d+)$", ans)
        if m:
            return ("snooze", int(m.group(1)))
        if ans == "q":
            return ("quit", 0)


def main():
    parser = argparse.ArgumentParser(description="Batch applicator runner (Workday + Greenhouse; Ashby opt-in)")
    parser.add_argument("--ids", nargs="+", type=int, help="Run only these job IDs")
    parser.add_argument("--start-id", type=int, help="Skip jobs before this ID")
    parser.add_argument("--host", help="Run only jobs routed to this applicator, e.g. "
                                        "'ashby', 'greenhouse', 'workday' (matches the "
                                        "app_<host>.py script stem)")
    parser.add_argument("--include-ashby", action="store_true", help="Include Ashby jobs in the batch (excluded by default)")
    parser.add_argument("--sim-ds", action="store_true", help="Pass --sim-ds to the Workday bot (rule-based only; ignored for Greenhouse/Ashby)")
    parser.add_argument("--dry-run", action="store_true", help="Print jobs list only, don't run")
    args = parser.parse_args()

    jobs, snoozed_count = load_p1_jobs()

    if args.ids:
        id_set = set(str(i) for i in args.ids)
        jobs = [j for j in jobs if j["ID"] in id_set]
    else:
        if args.start_id:
            jobs = [j for j in jobs if int(j["ID"]) >= args.start_id]

        if args.host:
            want = f"app_{args.host.lower()}"
            jobs = [j for j in jobs if Path(detect_applicator(j["Apply URL"]) or "").stem == want]
        elif not args.include_ashby:
            # Exclude Ashby jobs by default from general batch runs
            jobs = [j for j in jobs if Path(detect_applicator(j["Apply URL"]) or "").stem != "app_ashby"]

    if not jobs:
        print("[BATCH] No matching jobs found.")
        return

    # Sort by ID ascending so order is deterministic
    jobs.sort(key=lambda r: int(r["ID"]))

    snoozed_info = f" ({snoozed_count} snoozed)" if snoozed_count > 0 else ""
    print(f"\n[BATCH] {len(jobs)} P1 jobs queued{snoozed_info}:")
    for j in jobs:
        script = detect_applicator(j["Apply URL"])
        applicator = Path(script).stem if script else "unknown"
        print(f"  [{j['ID']:>4}] {j['Company']:<35} {j['Role'][:42]:<42} ({applicator})")
    print()

    if args.dry_run:
        return

    input("[BATCH] Press Enter to start, Ctrl+C to cancel... ")

    extra = []
    if args.sim_ds:
        extra.append("--sim-ds")

    total = len(jobs)
    i = 0
    while i < total:
        job = jobs[i]
        print_banner(i + 1, total, job)
        ret = run_job(job, extra)
        if ret == 2:
            print(f"\n[BATCH] ⏸ Job [{job['ID']}] {job['Company']} was snoozed.")

        if i + 1 >= total:
            print("\n[BATCH] All jobs complete.")
            break

        next_j = jobs[i + 1] if i + 1 < total else None
        action, days = prompt_continue(i + 1, total, next_j)
        if action == "quit":
            print("[BATCH] Exiting.")
            break
        elif action == "snooze" and next_j:
            snooze_until = snooze_job_by_url(
                next_j["Apply URL"],
                days=days or 3,
                reason=f"Snoozed in batch for {days or 3} days",
                company=next_j.get("Company", ""),
                role=next_j.get("Role", "")
            )
            print(f"[BATCH] ⏸ Next job [{next_j['ID']}] {next_j['Company']} snoozed until {snooze_until}.")
            i += 2  # skip next
        elif action == "skip":
            i += 2  # skip next
        else:
            i += 1


if __name__ == "__main__":
    main()
