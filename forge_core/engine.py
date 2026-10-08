"""The slice build engine — a hardened port of upstream's proven design.

Upstream insight kept: SIGINT to the WHOLE process group is the only
race-free way to stop soong_ui+ninja mid-flight with a consistent out/
(soong finishes writing its metadata, ninja finishes in-flight commands).

ROMForge upgrades:
  * process group via start_new_session (python setsid equivalent)
  * budget watchdog  -> graceful SIGINT, +grace -> SIGKILL backstop
  * disk watchdogs  -> three surfaces (volume-logical / backing-physical /
    root) with a reclaim ladder + fstrim BEFORE ENOSPC; root-disk purge
    protects the actions-runner daemon from eviction (runs #16-#21)
  * progress from the live log with ETA (ninja % lines)
  * classification: done | sliced | capacity | error + stop_reason taxonomy
    (drives the workflow DAG; 'capacity' makes the conveyor refuse to loop)
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
from . import log, relay, storage


class BuildError(Exception):
    pass


# ---- stop_reason taxonomy (drives the DAG conveyor; see forge_core/dag.py) --
# budget    -> classification 'sliced'   : resume next slot, all healthy
# disk      -> classification 'capacity' : REFUSE to loop (deadlock guard)
# root-disk -> classification 'sliced'   : / pressure, purge recovers it
STOP_BUDGET = "budget"
STOP_DISK = "disk"
STOP_ROOT_DISK = "root-disk"

# watchdog thresholds (GiB) — module constants so tests can reason about them
ROOT_PURGE_GB = 1.5             # / below this -> emergency purge caches
ROOT_STOP_GB = 0.8             # / below this after purge -> SIGINT (eviction
                               #    of the runner daemon loses EVERYTHING)
PHYS_WARN_GB = 4.0             # backing mount low -> fstrim + ladder
PHYS_STOP_GB = 2.0             # backing mount critical -> stop
LOGICAL_STOP_GB = 2.0          # free space inside the btrfs volume


FASTBOOT_ZIP_PAT = re.compile(r"(-img-.*|fastboot|target_files|otatools|symbols|apps).*\.zip$", re.IGNORECASE)


def _root_free_gb() -> float:
    try:
        st = os.statvfs("/")
        return st.f_bavail * st.f_frsize / (1024 ** 3)
    except OSError:
        return 999.0   # unknown -> never trigger the root watchdog


def build_env(plan, build_root: Path, use_ccache: bool = False) -> Dict[str, str]:
    e = dict(os.environ)
    # Route temporary file creation to the storage layout's raw tmp dir
    # (uncompressed — next to the volume backing, NOT inside the btrfs:
    # soong temp churn would burn compressed extents for nothing)
    tmp_dir = storage.backing_dir() / storage.TMP_DIR_NAME
    try:
        tmp_dir.mkdir(parents=True, exist_ok=True)
        e["TMPDIR"] = str(tmp_dir)
        e["TMP"] = str(tmp_dir)
    except OSError:
        pass  # fall back to the runner default /tmp
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
    """Run ONE build slice. Returns {rc, elapsed_s, classification,
    stop_reason, disk, rom_zip}.

    classification: 'done' | 'sliced' | 'capacity' | 'error'
      done      — ninja exited 0 (cmd_slice still requires a ROM zip)
      sliced    — stopped for a TRANSIENT reason (budget spent, root-disk
                  purge failed): resume next slot, progress is guaranteed
      capacity  — stopped because disk is structurally full: the DAG
                  conveyor REFUSES to re-dispatch (the runs #30/#34-#36
                  infinite re-download+30-second-build loop, fixed)
      error     — real build failure; state is still banked for triage
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
    stop_reason: Dict[str, str] = {"reason": ""}   # set before every SIGINT

    def _pg(sig: int) -> None:
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    def _graceful_stop(reason: str) -> None:
        stop_reason["reason"] = reason
        stopped_by_watchdog.set()
        _pg(signal.SIGINT)

    # ---- budget watchdog: graceful SIGINT, then KILL backstop ---------------
    def budget_watchdog() -> None:
        if stop.wait(budget_s):
            return
        log.warn(f"slice budget spent ({budget_s // 60} min) — SIGINT to pgid "
                 f"{proc.pid} for a consistent out/ bank")
        _graceful_stop(STOP_BUDGET)
        if stop.wait(300):
            return
        log.warn("grace period over — SIGKILL backstop")
        _pg(signal.SIGKILL)

    # ---- disk watchdogs (three surfaces, see storage.py) ---------------------
    #  logical  : free space INSIDE the btrfs volume (or plain free at
    #             build_root) — the build's own air supply.
    #  physical : free space on the backing mount — what the runner shares
    #             with the sparse image + everything else on /mnt.
    #  root (/) : the actions-runner daemon's disk. When it hits zero GitHub
    #             evicts the VM mid-build and we lose the WHOLE slice (failure
    #             class B, runs #16-#21) — so we purge, then stop early and
    #             bank instead of dying unbanked.
    def disk_watchdog() -> None:
        while not stop.wait(15):
            try:
                snap = storage.snapshot(build_root)
            except Exception:  # noqa: BLE001 — monitoring must never kill us
                continue
            # (a) root-disk: purge caches, then stop if still critical
            if snap.root_free_gb < ROOT_PURGE_GB:
                storage.emergency_root_purge()
                snap.root_free_gb = _root_free_gb()
            if snap.root_free_gb < ROOT_STOP_GB:
                log.warn(f"root disk critical ({snap.root_free_gb:.1f} GiB) — "
                         "stopping early to bank before runner eviction")
                _graceful_stop(STOP_ROOT_DISK)
                continue
            # (b) logical: reclaim ladder buys space inside the volume
            if snap.logical_free_gb < min_free_gb + 4:
                freed = fenv.reclaim_ladder(build_root,
                                            want_gb=min_free_gb + 4)
                if freed > 0:
                    log.warn(f"volume low ({snap.logical_free_gb:.1f} GiB) — "
                             f"ladder freed {freed / 2**30:.1f} GiB")
            try:
                snap = storage.snapshot(build_root)
            except Exception:  # noqa: BLE001
                continue
            # (c) physical: fstrim punches holes in the sparse image
            if snap.physical_free_gb < PHYS_WARN_GB:
                storage.trim()
            # (d) hard stops — the old deadlocked behavior silently looped
            #     here forever; now the classification carries the reason.
            if snap.logical_free_gb < LOGICAL_STOP_GB or \
                    snap.physical_free_gb < PHYS_STOP_GB:
                log.warn(f"disk capacity exhausted "
                         f"(logical {snap.logical_free_gb:.1f} GiB / "
                         f"physical {snap.physical_free_gb:.1f} GiB) — "
                         "stopping; the DAG will refuse to re-dispatch "
                         "on capacity")
                _graceful_stop(STOP_DISK)
                continue

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
            disk_label = ""
            try:
                hs = storage.snapshot(build_root)
                free_disk = hs.logical_free_gb
                disk_label = ("vol" if hs.mode == "btrfs" else "plain")
            except Exception:
                pass

            now_str = time.strftime("%H:%M:%S")
            prog_label = f"[{pct}% {done}/{total}]" if total > 0 else "[building]"
            print(f"[{now_str}] {prog_label} {latest_action} | {mem_str} | "
                  f"Disk({disk_label}): {free_disk:.1f}G free", flush=True)

            if pct - notified["pct"] >= 5:
                notified["pct"] = pct
                log.notice(
                    f"PROGRESS:{pct}:{done}/{total}",
                    title="Build-Progress")

    # ---- dynamic adaptive swap watchdog (OOM immunity on surge) -------------
    dynamic_swap_chunks: List[Path] = []

    def dynamic_swap_watchdog() -> None:
        while not stop.wait(3.0):
            try:
                with open("/proc/meminfo", "r") as mf:
                    mem_data = mf.read()
                    sw_tot_m = re.search(r"SwapTotal:\s+(\d+)", mem_data)
                    sw_free_m = re.search(r"SwapFree:\s+(\d+)", mem_data)
                    tot_m = re.search(r"MemTotal:\s+(\d+)", mem_data)
                    avail_m = re.search(r"MemAvailable:\s+(\d+)", mem_data)
                    if not (sw_tot_m and sw_free_m and tot_m and avail_m):
                        continue
                    sw_tot_kb = int(sw_tot_m.group(1))
                    sw_free_kb = int(sw_free_m.group(1))
                    tot_kb = int(tot_m.group(1))
                    avail_kb = int(avail_m.group(1))

                    sw_used_kb = sw_tot_kb - sw_free_kb
                    sw_used_pct = (sw_used_kb / sw_tot_kb * 100.0) if sw_tot_kb > 0 else 0.0
                    ram_used_pct = ((tot_kb - avail_kb) / tot_kb * 100.0) if tot_kb > 0 else 0.0

                    if (sw_used_pct > 70.0 or (ram_used_pct > 80.0 and sw_used_pct > 40.0)):
                        if len(dynamic_swap_chunks) < 6:
                            snap = storage.snapshot(build_root)
                            if snap.physical_free_gb > 12.0:
                                idx = len(dynamic_swap_chunks) + 1
                                chunk_path = storage.backing_dir() / f".forge-swap.chunk.{idx}"
                                if fenv.activate_swap_chunk(str(chunk_path), chunk_size_gb=2):
                                    dynamic_swap_chunks.append(chunk_path)
                                    log.ok(f"dynamic swap auto-scale: +2 GB chunk {idx} activated "
                                           f"(RAM {ram_used_pct:.0f}%, Swap {sw_used_pct:.0f}%, "
                                           f"Disk {snap.physical_free_gb:.1f}G free)")
            except Exception:
                pass

    threads = [threading.Thread(target=budget_watchdog, daemon=True),
               threading.Thread(target=disk_watchdog, daemon=True),
               threading.Thread(target=dynamic_swap_watchdog, daemon=True),
               threading.Thread(target=live_heartbeat, daemon=True)]
    for t in threads:
        t.start()

    rc = proc.wait()
    stop.set()
    for t in threads:
        t.join(timeout=5)
    fenv.deactivate_swap_chunks(dynamic_swap_chunks)
    elapsed = time.time() - t0

    # ---- classify -------------------------------------------------------------
    # The stop_reason decides whether resuming is SAFE:
    #   budget / root-disk -> sliced   (transient; next slot makes progress)
    #   disk               -> capacity (structural; the conveyor refuses to
    #                                  re-dispatch — the storage-deadlock fix)
    try:
        final_snap = storage.snapshot(build_root)
        disk_state = final_snap.to_dict()
    except Exception:  # noqa: BLE001
        disk_state = {}
    reason = stop_reason["reason"]
    classification = classify_exit(rc, stopped_by_watchdog.is_set(), reason,
                                   elapsed, budget_s)
    result: Dict[str, object] = {"rc": rc, "elapsed_s": int(elapsed),
                                 "classification": classification,
                                 "rom_zip": "",
                                 "stop_reason": reason, "disk": disk_state}
    return result


def classify_exit(rc: int, watchdog_fired: bool, stop_reason: str,
                  elapsed_s: float, budget_s: float) -> str:
    """Pure classification of a finished slice (offline-testable).

    This mapping IS the deadlock fix: a disk stop must never masquerade
    as a resumable 'sliced' (that is exactly what looped runs #30/#34-#36
    forever), and a clean rc==0 is 'done' only in the engine sense — the
    CLI additionally requires a ROM zip (dag.finalize_classification).
    """
    if rc == 0:
        return "done"
    reason = stop_reason
    if not reason and elapsed_s >= budget_s - 5:
        reason = STOP_BUDGET          # SIGINT raced process exit
    if watchdog_fired or reason:
        return "capacity" if reason == STOP_DISK else "sliced"
    return "error"


def find_rom_zip(plan, build_root: Path) -> Optional[Path]:
    """Largest non-fastboot zip in the product dir (upstream heuristic,
    now config-driven via rom.rom_zip_glob for exclusion)."""
    dev = plan.rom.device or (plan.rom.lunch.split("_")[1].split("-")[0] if "_" in plan.rom.lunch else plan.rom.lunch.split("-")[0])
    rom_dir = build_root / "out" / "target" / "product" / dev
    best: Optional[Path] = None
    candidates = []
    if rom_dir.exists():
        candidates.extend(rom_dir.glob("*.zip"))
    else:
        prod_root = build_root / "out" / "target" / "product"
        if prod_root.exists():
            candidates.extend(prod_root.glob("*/*.zip"))
    for z in candidates:
        if FASTBOOT_ZIP_PAT.search(z.name):
            continue
        if best is None or z.stat().st_size > best.stat().st_size:
            best = z
    return best


def slice_summary(result: Dict[str, object], build_log: Path,
                  out_dir: Path, budget_s: int) -> str:
    stats = relay.ninja_stats(out_dir)
    stop_reason = str(result.get("stop_reason", "")) or "—"
    lines = [
        "## Build slice",
        "",
        "| field | value |",
        "|---|---|",
        f"| classification | **{result['classification']}** |",
        f"| stop_reason | {stop_reason} |",
        f"| rc | {result['rc']} |",
        f"| wall | {int(result['elapsed_s']) // 60} min / "
        f"{budget_s // 60} min budget |",
        f"| ninja outputs (cumulative) | {stats.get('outputs', 0)} |",
        f"| ninja cpu-seconds (cumulative) | "
        f"{stats.get('build_seconds', 0):,.0f}s |",
    ]
    disk = result.get("disk") or {}
    if disk:
        lines.append(f"| disk at stop | logical "
                     f"{disk.get('logical_free_gb', '?')} GiB / physical "
                     f"{disk.get('physical_free_gb', '?')} GiB / root "
                     f"{disk.get('root_free_gb', '?')} GiB ({disk.get('mode', '?')}) |")
    for line in lines:
        log.summary(line)
    return "\n".join(lines)
