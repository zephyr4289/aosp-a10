#!/usr/bin/env python3
"""
AOSP Build Live Terminal Monitor (TUI)
Monitors GitHub Actions runs for zephyr4289/aosp-a10 with 1-second live clock updates,
rate-limit protection, and pipeline stage progress tracking.
"""

import os
import sys
import time
import json
import urllib.request
from datetime import datetime

REPO_OWNER = "zephyr4289"
REPO_NAME = "aosp-a10"
SLICE_BUDGET_SECONDS = 9600  # 160 min

# ANSI styling
CLEAR_SCREEN = "\033[2J\033[H"
BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
RESET = "\033[0m"

STAGE_KEYS = [
    ("Gate", "00. Gate Check (already built?)"),
    ("00 - Prepare", "00. Prepare Runner (disk & swap)"),
    ("05 - Restore", "05. Restore Caches (source & ccache)"),
    ("01 - Sync", "01. Sync Source (QASSA + PL2)"),
    ("02 - Apply", "02. Apply Patches & Validate Lunch"),
    ("03 - Build", "03. Build Slice (160 min watchdog)"),
    ("06 - Publish", "06. Publish ROM (if completed)"),
    ("Persist ccache", "04. Persist Ccache to Release"),
    ("Persist source", "04. Persist Source Cache"),
]

def format_duration(seconds):
    if seconds is None or seconds < 0:
        return "00:00:00"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"

def parse_iso(dt_str):
    if not dt_str:
        return None
    try:
        return datetime.fromisoformat(dt_str.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None

def fetch_json(url, token=None):
    req = urllib.request.Request(url, headers={"User-Agent": "AOSP-Terminal-Monitor", "Accept": "application/vnd.github.v3+json"})
    if token:
        req.add_header("Authorization", f"token {token}")
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
            rem = resp.headers.get("x-ratelimit-remaining", "?")
            return data, rem
    except Exception as e:
        return None, None

def render_progress_bar(percent, width=30):
    percent = max(0.0, min(100.0, percent))
    filled = int(width * (percent / 100.0))
    bar = "=" * filled + (">" if filled < width else "") + " " * (width - filled - (1 if filled < width else 0))
    return f"[{CYAN}{bar}{RESET}] {percent:5.1f}%"

def main():
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    poll_interval = 10 if token else 20
    last_poll = 0
    
    current_run = None
    current_jobs = []
    campaign_start = None
    api_remaining = "?"

    print("Starting AOSP Build Terminal Monitor... (Press Ctrl+C to exit)")
    time.sleep(1)

    try:
        while True:
            now = time.time()
            
            # Periodic API poll
            if now - last_poll >= poll_interval:
                runs_data, rem = fetch_json(f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/actions/runs?per_page=10", token)
                if rem:
                    api_remaining = rem
                if runs_data and "workflow_runs" in runs_data and runs_data["workflow_runs"]:
                    runs = runs_data["workflow_runs"]
                    current_run = runs[0]
                    # Earliest run for campaign total elapsed time
                    earliest_start = parse_iso(runs[-1].get("created_at"))
                    if earliest_start:
                        campaign_start = earliest_start
                    
                    # Fetch job steps
                    if "jobs_url" in current_run:
                        jobs_data, _ = fetch_json(current_run["jobs_url"], token)
                        if jobs_data and "jobs" in jobs_data:
                            current_jobs = jobs_data["jobs"]
                last_poll = now

            # Calculate live timers
            total_campaign_elapsed = (now - campaign_start) if campaign_start else 0
            
            slice_elapsed = 0
            slice_percent = 0.0
            slice_remaining = SLICE_BUDGET_SECONDS
            if current_run:
                run_start = parse_iso(current_run.get("run_started_at") or current_run.get("created_at"))
                if run_start:
                    if current_run.get("status") == "in_progress":
                        slice_elapsed = max(0, now - run_start)
                    else:
                        run_end = parse_iso(current_run.get("updated_at")) or now
                        slice_elapsed = max(0, run_end - run_start)
                    slice_percent = min(100.0, (slice_elapsed / SLICE_BUDGET_SECONDS) * 100.0)
                    slice_remaining = max(0, SLICE_BUDGET_SECONDS - slice_elapsed)

            # Build UI buffer
            lines = []
            lines.append(f"{BOLD}╔══════════════════════════════════════════════════════════════════════════╗{RESET}")
            lines.append(f"{BOLD}║  QASSA 2.4 (Android 10) · Nokia 6.1 (PL2) · LIVE BUILD MONITOR           ║{RESET}")
            lines.append(f"{BOLD}║  Repo: {CYAN}{REPO_OWNER}/{REPO_NAME}{RESET}  Target: {BOLD}qassa_PL2-userdebug (bacon){RESET}        ║")
            lines.append(f"{BOLD}╠══════════════════════════════════════════════════════════════════════════╣{RESET}")
            
            # Run Status
            status_str = "IDLE / NO RUNS"
            status_color = YELLOW
            run_num = "--"
            if current_run:
                run_num = f"#{current_run.get('run_number')}"
                st = current_run.get("status")
                conc = current_run.get("conclusion")
                if st == "in_progress":
                    status_str = f"RUNNING"
                    status_color = GREEN
                elif st == "completed":
                    status_str = f"COMPLETED ({conc.upper() if conc else 'DONE'})"
                    status_color = GREEN if conc == "success" else RED
            
            lines.append(f"  {BOLD}Run:{RESET} {run_num}  |  {BOLD}Status:{RESET} {status_color}{status_str}{RESET}  |  {BOLD}API Limit Rem:{RESET} {api_remaining}")
            lines.append(f"  {BOLD}Total Campaign Elapsed:{RESET}  {BOLD}{GREEN}{format_duration(total_campaign_elapsed)}{RESET} (since campaign dispatch)")
            lines.append(f"  {BOLD}Current Slice Elapsed:{RESET}   {BOLD}{CYAN}{format_duration(slice_elapsed)}{RESET} / {format_duration(SLICE_BUDGET_SECONDS)}")
            lines.append(f"  {BOLD}Slice Remaining Budget:{RESET}  {format_duration(slice_remaining)}")
            lines.append(f"  {BOLD}Slice Watchdog:{RESET}          {render_progress_bar(slice_percent, 28)}")
            lines.append(f"{BOLD}╟──────────────────────────────────────────────────────────────────────────╢{RESET}")
            lines.append(f"  {BOLD}PIPELINE STAGES:{RESET}")

            steps = current_jobs[0].get("steps", []) if current_jobs else []
            for key, name in STAGE_KEYS:
                matched = next((s for s in steps if key.lower() in s.get("name", "").lower()), None)
                if not matched:
                    lines.append(f"    {DIM}○  {name:<42} Pending{RESET}")
                elif matched.get("status") == "in_progress":
                    st_time = parse_iso(matched.get("started_at"))
                    dur = format_duration(now - st_time) if st_time else "--"
                    lines.append(f"    {BOLD}{CYAN}⚡  {name:<42} RUNNING ({dur}){RESET}")
                elif matched.get("status") == "completed":
                    conc = matched.get("conclusion")
                    if conc == "success":
                        st_time = parse_iso(matched.get("started_at"))
                        end_time = parse_iso(matched.get("completed_at"))
                        dur = format_duration(end_time - st_time) if st_time and end_time else "Done"
                        lines.append(f"    {GREEN}✓  {name:<42} Finished ({dur}){RESET}")
                    elif conc == "skipped":
                        lines.append(f"    {DIM}↷  {name:<42} Skipped (Cached){RESET}")
                    else:
                        lines.append(f"    {RED}✗  {name:<42} Failed{RESET}")

            lines.append(f"{BOLD}╚══════════════════════════════════════════════════════════════════════════╝{RESET}")
            lines.append(f" {DIM}Refreshes live every 1s · API poll: {poll_interval}s · Press Ctrl+C to stop{RESET}")

            sys.stdout.write(CLEAR_SCREEN + "\n".join(lines) + "\n")
            sys.stdout.flush()
            time.sleep(1)

    except KeyboardInterrupt:
        print("\nExited monitor.")

if __name__ == "__main__":
    main()
