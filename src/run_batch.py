#!/usr/bin/env python3
"""
Batch Workday runner — runs app_workday.py on a list of URLs one-by-one, headed.
Pauses between jobs so you can review the report / submit / skip.
Usage:
    python3 src/run_batch.py                        # all P1 Workday jobs
    python3 src/run_batch.py --ids 257 269 272      # specific job IDs
    python3 src/run_batch.py --start-id 257         # resume from a specific ID
    python3 src/run_batch.py --sim-ds               # rule-based only (no DeepSeek)
"""
import argparse
import csv
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).parent.parent
CSV_PATH = ROOT / "data" / "jobs_tracker.csv"


def load_p1_workday_jobs() -> list[dict]:
    with open(CSV_PATH, newline="") as f:
        rows = list(csv.DictReader(f))
    return [
        r for r in rows
        if r.get("Status", "") == "Eligible (Priority 1)"
        and "myworkdayjobs.com" in r.get("Apply URL", "")
    ]


def tenant(url: str) -> str:
    return urlparse(url).netloc.split(".")[0]


def print_banner(idx: int, total: int, job: dict):
    url = job["Apply URL"]
    print()
    print("=" * 70)
    print(f"  JOB {idx}/{total}  [ID {job['ID']}]  {job['Company']}")
    print(f"  Role    : {job['Role']}")
    print(f"  Location: {job['Location']}")
    print(f"  FIT     : {job.get('Fit%', '?')}%  |  {job.get('Suitability', '')[:80]}")
    print(f"  Tenant  : {tenant(url)}")
    print(f"  URL     : {url}")
    print("=" * 70)


JOB_TIMEOUT_SEC = 20 * 60  # 20 min: covers manual account creation + review inspection


def run_job(job: dict, extra_args: list[str]) -> int:
    cmd = [
        sys.executable, "-u",
        str(ROOT / "src" / "app_workday.py"),
        job["Apply URL"],
        "--show",
        *extra_args,
    ]
    print(f"\n[BATCH] Running: {' '.join(cmd)}\n")
    try:
        result = subprocess.run(cmd, cwd=str(ROOT), timeout=JOB_TIMEOUT_SEC)
        return result.returncode
    except subprocess.TimeoutExpired:
        print(f"\n[BATCH] ⚠ Job exceeded {JOB_TIMEOUT_SEC // 60} min — killed, moving on.")
        return -1


def prompt_continue(idx: int, total: int) -> str:
    """Returns 'next' | 'skip' | 'quit'"""
    while True:
        print()
        print(f"[BATCH] Job {idx}/{total} done. What next?")
        print("  [Enter]  → next job")
        print("  s        → skip next job (jump forward)")
        print("  q        → quit batch")
        ans = input("  > ").strip().lower()
        if ans == "":
            return "next"
        if ans == "s":
            return "skip"
        if ans == "q":
            return "quit"


def main():
    parser = argparse.ArgumentParser(description="Batch Workday runner")
    parser.add_argument("--ids", nargs="+", type=int, help="Run only these job IDs")
    parser.add_argument("--start-id", type=int, help="Skip jobs before this ID")
    parser.add_argument("--sim-ds", action="store_true", help="Pass --sim-ds to bot (rule-based only)")
    parser.add_argument("--dry-run", action="store_true", help="Print jobs list only, don't run")
    args = parser.parse_args()

    jobs = load_p1_workday_jobs()

    if args.ids:
        id_set = set(str(i) for i in args.ids)
        jobs = [j for j in jobs if j["ID"] in id_set]
    elif args.start_id:
        jobs = [j for j in jobs if int(j["ID"]) >= args.start_id]

    if not jobs:
        print("[BATCH] No matching jobs found.")
        return

    # Sort by ID ascending so order is deterministic
    jobs.sort(key=lambda r: int(r["ID"]))

    print(f"\n[BATCH] {len(jobs)} Workday P1 jobs queued:")
    for j in jobs:
        print(f"  [{j['ID']:>4}] {j['Company']:<35} {j['Role'][:50]}")
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
        run_job(job, extra)
        if i + 1 >= total:
            print("\n[BATCH] All jobs complete.")
            break
        action = prompt_continue(i + 1, total)
        if action == "quit":
            print("[BATCH] Exiting.")
            break
        elif action == "skip":
            i += 2  # skip next
        else:
            i += 1


if __name__ == "__main__":
    main()
