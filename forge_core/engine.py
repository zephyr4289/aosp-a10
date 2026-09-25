"""The slice build engine — a hardened port of upstream's proven design.

Upstream insight kept: SIGINT to the WHOLE process group is the only
race-free way to stop soong_ui+ninja mid-flight with a consistent out/
(soong finishes writing its metadata, ninja finishes in-flight commands).

ROMForge upgrades:
  * process group via start_new_session (python setsid equivalent)
  * budget watchdog  -> graceful SIGINT, +grace -> SIGKILL backstop
  * disk watchdog   -> reclaim ladder BEFORE ENOSPC can kill a link
    (upstream's ENOSPC-at-94% was the designed-in outcome of no ladder)
  * progress from the live log with ETA (ninja % lines)
  * classification: done | sliced | error  (drives the workflow DAG)
  * ccache OFF by default: with exact-resume out/ relay it is pure disk
    overhead; opt-in for paranoia configurations via rom.env.
"""
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from . import env as fenv
from . import log, relay


class BuildError(Exception):
    pass


FASTBOOT_ZIP_PAT = re.compile(r"(-img-.*|fastboot|target_files|otatools|symbols|apps).*\.zip$", re.IGNORECASE)


def build_env(plan, build_root: Path, use_ccache: bool = False) -> Dict[str, str]:
    e = dict(os.environ)
    # Route temporary file creation to high-capacity storage volume if present
    tmp_path = Path("/mnt/romforge/tmp")
    if Path("/mnt/romforge").exists():
        tmp_path.mkdir(parents=True, exist_ok=True)
        e["TMPDIR"] = str(tmp_path)
    e.update({
        "LC_ALL": "C",
        "OUT_DIR": str(build_root / "out"),
        "ALLOW_MISSING_DEPENDENCIES":
            e.get("ALLOW_MISSING_DEPENDENCIES", "false"),
    })
    for k, v in plan.rom.env.items():
        e[str(k)] = str(v)
    if use_ccache:
        e["USE_CCACHE"] = "1"
        e["CCACHE_EXEC"] = subprocess.run(
            ["bash", "-lc", "command -v ccache"], capture_output=True,
            text=True).stdout.strip() or "ccache"
        e["CCACHE_DIR"] = e.get("CCACHE_DIR",
                                str(Path.home() / ".ccache"))
    return e


def lunch_combo(plan) -> str:
    return plan.rom.lunch


def run_slice(plan, build_root: Path, target: str, budget_s: int,
              build_log: Path, use_ccache: bool = False,
              allow_missing_deps: bool = False,
              min_free_gb: float = 6.0) -> Dict[str, object]:
    """Run ONE build slice. Returns {rc, elapsed_s, classification, rom_zip}.

    classification: 'done' | 'sliced' | 'error'
    """
    soong_ui = build_root / "build" / "soong" / "soong_ui.bash"
    if not soong_ui.exists():
        raise BuildError(f"soong_ui not found at {soong_ui}")
    if not (build_root / ".source_ready").exists():
        raise BuildError("source tree not marked ready")

    build_log.parent.mkdir(parents=True, exist_ok=True)
    build_log.write_text("", encoding="utf-8")

    # -- envsetup + lunch are function definitions; source them in bash ------
    launcher = (
        "set +eu; "
        f"source build/envsetup.sh >/dev/null 2>&1; "
        f"lunch {lunch_combo(plan)} >/dev/null 2>&1; "
        f"exec {soong_ui} --make-mode -j 4 {target}"
    )
    e = build_env(plan, build_root, use_ccache=use_ccache)
    e["NINJA_ARGS"] = "-j 4"
    if allow_missing_deps:
        e["ALLOW_MISSING_DEPENDENCIES"] = "true"

    t0 = time.time()
    with open(build_log, "ab", buffering=0) as logf:
        proc = subprocess.Popen(["bash", "-c", launcher], cwd=str(build_root),
                                stdout=logf, stderr=logf, env=e,
                                start_new_session=True)   # <- own pgid

    stop = threading.Event()
    stopped_by_watchdog = threading.Event()

    def _pg(sig: int) -> None:
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    # ---- budget watchdog: graceful SIGINT, then KILL backstop ---------------
    def budget_watchdog() -> None:
        if stop.wait(budget_s):
            return
        log.warn(f"slice budget spent ({budget_s // 60} min) — SIGINT to pgid "
                 f"{proc.pid} for a consistent out/ bank")
        stopped_by_watchdog.set()
        _pg(signal.SIGINT)
        if stop.wait(300):
            return
        log.warn("grace period over — SIGKILL backstop")
        _pg(signal.SIGKILL)

    # ---- disk watchdog: proactive sweeper + ladder BEFORE ENOSPC ------------
    def disk_watchdog() -> None:
        while not stop.wait(15):
            try:
                free = fenv._df_free_gb(str(build_root))
            except OSError:
                continue
            if free < min_free_gb + 4:
                freed = fenv.reclaim_ladder(build_root, want_gb=min_free_gb + 4)
                if freed > 0:
                    log.warn(f"disk low ({free:.1f} GB) — ladder freed "
                             f"{freed / 2**30:.1f} GB")
            try:
                free = fenv._df_free_gb(str(build_root))
            except OSError:
                continue
            if free < 2.0:
                log.warn("disk critical — early graceful slice stop")
                stopped_by_watchdog.set()
                _pg(signal.SIGINT)

    # ---- live 1-second terminal pulse thread ---------------------------------
    last: Dict[str, int] = {"pct": 0, "done": 0, "total": 0}
    notified = {"pct": -5}

    def live_heartbeat() -> None:
        last_log_pos = 0
        latest_action = "compiling"
        while not stop.wait(1.0):
            # 1. Read latest active step from build_log
            try:
                if build_log.exists():
                    with open(build_log, "r", encoding="utf-8", errors="replace") as f:
                        f.seek(last_log_pos)
                        new_lines = f.readlines()
                        last_log_pos = f.tell()
                        for line in new_lines:
                            line_str = line.strip()
                            if line_str.startswith("[") or "ninja:" in line_str or "target" in line_str or "FAILED" in line_str or "Copy:" in line_str or "Install:" in line_str:
                                latest_action = line_str[:65]
            except Exception:
                pass

            # 2. Extract ninja progress numbers
            nonlocal last
            last = relay.progress_from_log(build_log, last)
            pct = last.get("pct", 0)
            done = last.get("done", 0)
            total = last.get("total", 0)

            # 3. Read live memory & swap stats
            mem_str = "RAM: ?"
            try:
                with open("/proc/meminfo", "r") as mf:
                    mem_data = mf.read()
                    tot_kb = int(re.search(r"MemTotal:\s+(\d+)", mem_data).group(1))
                    avail_kb = int(re.search(r"MemAvailable:\s+(\d+)", mem_data).group(1))
                    sw_tot_kb = int(re.search(r"SwapTotal:\s+(\d+)", mem_data).group(1))
                    sw_free_kb = int(re.search(r"SwapFree:\s+(\d+)", mem_data).group(1))
                    used_gb = (tot_kb - avail_kb) / 1024 / 1024
                    tot_gb = tot_kb / 1024 / 1024
                    sw_used_gb = (sw_tot_kb - sw_free_kb) / 1024 / 1024
                    sw_tot_gb = sw_tot_kb / 1024 / 1024
                    mem_str = f"RAM: {used_gb:.1f}/{tot_gb:.1f}G (Swap: {sw_used_gb:.1f}/{sw_tot_gb:.1f}G)"
            except Exception:
                pass

            free_disk = 0.0
            try:
                free_disk = fenv._df_free_gb(str(build_root))
            except Exception:
                pass

            now_str = time.strftime("%H:%M:%S")
            prog_label = f"[{pct}% {done}/{total}]" if total > 0 else "[building]"
            print(f"[{now_str}] {prog_label} {latest_action} | {mem_str} | Disk: {free_disk:.1f}G free", flush=True)

            if pct - notified["pct"] >= 5:
                notified["pct"] = pct
                log.notice(
                    f"PROGRESS:{pct}:{done}/{total}",
                    title="Build-Progress")

    threads = [threading.Thread(target=budget_watchdog, daemon=True),
               threading.Thread(target=disk_watchdog, daemon=True),
               threading.Thread(target=live_heartbeat, daemon=True)]
    for t in threads:
        t.start()

    rc = proc.wait()
    stop.set()
    for t in threads:
        t.join(timeout=5)
    elapsed = time.time() - t0

    # ---- classify -------------------------------------------------------------
    result: Dict[str, object] = {"rc": rc, "elapsed_s": int(elapsed),
                                 "classification": "error", "rom_zip": ""}
    if rc == 0:
        result["classification"] = "done"
    elif stopped_by_watchdog.is_set() or elapsed >= budget_s - 5:
        result["classification"] = "sliced"
    else:
        # real failure — but still bankable state; the workflow decides
        result["classification"] = "error"
    return result


def find_rom_zip(plan, build_root: Path) -> Optional[Path]:
    """Largest non-fastboot zip in the product dir (upstream heuristic,
    now config-driven via rom.rom_zip_glob for exclusion)."""
    dev = plan.rom.lunch.split("_")[1]
    rom_dir = build_root / "out" / "target" / "product" / dev
    best: Optional[Path] = None
    if not rom_dir.exists():
        return None
    for z in rom_dir.glob("*.zip"):
        if FASTBOOT_ZIP_PAT.search(z.name):
            continue
        if best is None or z.stat().st_size > best.stat().st_size:
            best = z
    return best


def slice_summary(result: Dict[str, object], build_log: Path,
                  out_dir: Path, budget_s: int) -> str:
    stats = relay.ninja_stats(out_dir)
    lines = [
        "## Build slice",
        "",
        "| field | value |",
        "|---|---|",
        f"| classification | **{result['classification']}** |",
        f"| rc | {result['rc']} |",
        f"| wall | {int(result['elapsed_s']) // 60} min / "
        f"{budget_s // 60} min budget |",
        f"| ninja outputs (cumulative) | {stats.get('outputs', 0)} |",
        f"| ninja cpu-seconds (cumulative) | "
        f"{stats.get('build_seconds', 0):,.0f}s |",
    ]
    for line in lines:
        log.summary(line)
    return "\n".join(lines)
