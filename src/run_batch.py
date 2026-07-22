#!/usr/bin/env python3
"""
Batch applicator runner — runs the right applicator script (Workday or Greenhouse)
on a list of URLs one-by-one, always headed (visible Chrome) so you can watch/interject
and review each application before it submits. Pauses between jobs so you can review
the report / submit / skip.
Usage:
    python3 src/run_batch.py                        # all P1 jobs with a known applicator
    python3 src/run_batch.py --ids 257 269 272      # specific job IDs
    python3 src/run_batch.py --start-id 257         # resume from a specific ID
    python3 src/run_batch.py --sim-ds               # rule-based only (no DeepSeek); Workday only
"""
import argparse
import csv
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent))
from job_tracker import detect_applicator

ROOT = Path(__file__).parent.parent
CSV_PATH = ROOT / "data" / "jobs_tracker.csv"


def load_p1_jobs() -> list[dict]:
    with open(CSV_PATH, newline="") as f:
        rows = list(csv.DictReader(f))
    return [
        r for r in rows
        if r.get("Status", "") == "Eligible (Priority 1)"
        and detect_applicator(r.get("Apply URL", "")) is not None
    ]


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
    # --sim-ds is a Workday-only flag (app_greenhouse.py doesn't accept it)
    is_workday = Path(script).stem == "app_workday"
    args_for_script = extra_args if is_workday else [a for a in extra_args if a != "--sim-ds"]
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
    parser = argparse.ArgumentParser(description="Batch applicator runner (Workday + Greenhouse)")
    parser.add_argument("--ids", nargs="+", type=int, help="Run only these job IDs")
    parser.add_argument("--start-id", type=int, help="Skip jobs before this ID")
    parser.add_argument("--sim-ds", action="store_true", help="Pass --sim-ds to the Workday bot (rule-based only; ignored for Greenhouse)")
    parser.add_argument("--dry-run", action="store_true", help="Print jobs list only, don't run")
    args = parser.parse_args()

    jobs = load_p1_jobs()

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

    print(f"\n[BATCH] {len(jobs)} P1 jobs queued:")
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
