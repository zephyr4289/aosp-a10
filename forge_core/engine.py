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
import shlex
import shutil
import signal
import subprocess
import sys
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
# memory    -> classification 'mem-stall': PSI memory stall, re-route or slow profile
STOP_BUDGET = "budget"
STOP_DISK = "disk"
STOP_ROOT_DISK = "root-disk"
STOP_MEMORY = "memory"

# watchdog thresholds (GiB) — module constants so tests can reason about them
ROOT_PURGE_GB = 1.5             # / below this -> emergency purge caches
ROOT_STOP_GB = 0.8             # / below this after purge -> SIGINT (eviction
                               #    of the runner daemon loses EVERYTHING)
PHYS_WARN_GB = 4.0             # backing mount low -> fstrim + ladder
PHYS_STOP_GB = 2.0             # backing mount critical -> stop
LOGICAL_STOP_GB = 2.0          # free space inside the btrfs volume

# cgroup v2 phase limits (memory.max, memory.swap.max, cpu.max)
CG_PHASE_LIMITS = {
    "analysis": ("14G", "12G", "350000 100000"),
    "exec":     ("13G", "6G",  "380000 100000"),
}


def _read_psi() -> Optional[float]:
    """Read full avg60 from /proc/pressure/memory, or None if PSI unsupported."""
    try:
        psi_file = "/proc/pressure/memory"
        if os.path.exists(psi_file):
            with open(psi_file, "r", encoding="utf-8", errors="replace") as f:
                txt = f.read()
            m = re.search(r"full\s+.*?avg60=(\d+\.\d+)", txt)
            if m:
                return float(m.group(1))
            m_some = re.search(r"some\s+.*?avg60=(\d+\.\d+)", txt)
            if m_some:
                return float(m_some.group(1))
    except Exception:
        pass
    return None


def _cgroup_run_prefix(phase: str = "exec") -> Optional[List[str]]:
    """Rung 1: delegated cgroupv2 dir; Rung 2: systemd-run; Rung 3: None."""
    limits = CG_PHASE_LIMITS.get(phase, CG_PHASE_LIMITS["exec"])
    mem, swp, cpu = limits
    uid = os.getuid()
    gid = os.getgid()
    if shutil.which("systemd-run"):
        try:
            return ["sudo", "-E", "systemd-run", f"--uid={uid}", f"--gid={gid}", "--scope", "--quiet",
                    "-p", f"MemoryMax={mem}", f"-p", f"MemorySwapMax={swp}",
                    "-p", f"CPUQuota={int(cpu.split()[0]) // 1000}%", "bash", "-c"]
        except Exception:
            pass
    return None


FASTBOOT_ZIP_PAT = re.compile(r"(-img-.*|fastboot|target_files|otatools|symbols|apps).*\.zip$", re.IGNORECASE)


def _root_free_gb() -> float:
    try:
        st = os.statvfs("/")
        return st.f_bavail * st.f_frsize / (1024 ** 3)
    except OSError:
        return 999.0   # unknown -> never trigger the root watchdog


def build_env(plan, build_root: Path, use_ccache: bool = False, phase: str = "exec") -> Dict[str, str]:
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
            e.get("ALLOW_MISSING_DEPENDENCIES", "true"),
        "JAVA_TOOL_OPTIONS": e.get("JAVA_TOOL_OPTIONS", "-Xmx2560m -XX:+UseG1GC -XX:MaxGCPauseMillis=200"),
    })

    if phase == "analysis":
        # Analysis phase: AST heap grows to 30-34 GiB; soft sub-limits cause GC death spiral.
        # Unset GOMEMLIMIT unless explicitly forced via FORGE_SOONG_MEM_LIMIT.
        limit = os.environ.get("FORGE_SOONG_MEM_LIMIT")
        if limit:
            e["GOMEMLIMIT"] = limit
        elif "GOMEMLIMIT" in e:
            del e["GOMEMLIMIT"]
        e["GOGC"] = os.environ.get("FORGE_GOGC", "400")
        e["GOMAXPROCS"] = os.environ.get("FORGE_GOMAXPROCS", str(min(4, os.cpu_count() or 4)))
        if os.environ.get("FORGE_GCTRACE", "1") == "1":
            e["GODEBUG"] = "gctrace=1"
    elif phase == "bootstrap":
        e["GOFLAGS"] = os.environ.get("FORGE_GOFLAGS", "-p=2")
        e["GOMEMLIMIT"] = os.environ.get("FORGE_BOOTSTRAP_MEM_LIMIT", "3GiB")
        e["GOGC"] = "50"
        e["GOMAXPROCS"] = "2"
    else:
        # Default exec phase: honor explicit override if given
        if "FORGE_SOONG_MEM_LIMIT" in os.environ:
            e["GOMEMLIMIT"] = os.environ["FORGE_SOONG_MEM_LIMIT"]

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


def optimal_jobs(plan) -> int:
    """Calculate optimal parallel jobs based on silicon capability and RAM+swap.

    On high-performance Zen 5 Turin/Genoa runners (EPYC 9V45) with >=14GB RAM and >=6GB swap,
    AOSP ninja throughput scales significantly at -j 6 or -j 8, cutting build wall time by 25-35%.
    Auto-throttles if memory is constrained.
    """
    override = os.environ.get("FORGE_JOBS")
    if override:
        try:
            return max(1, int(override))
        except ValueError:
            pass
    cores = os.cpu_count() or 4
    if cores < 4:
        return max(1, cores)

    mem_gb = 0.0
    swap_gb = 0.0
    try:
        with open("/proc/meminfo", "r") as mf:
            data = mf.read()
            m_tot = re.search(r"MemTotal:\s+(\d+)", data)
            sw_tot = re.search(r"SwapTotal:\s+(\d+)", data)
            if m_tot:
                mem_gb = int(m_tot.group(1)) / (1024 * 1024)
            if sw_tot:
                swap_gb = int(sw_tot.group(1)) / (1024 * 1024)
    except Exception:
        pass

    # Check CPU capabilities (AVX-512 / Turin / Zen 5)
    has_avx512 = False
    is_zen = False
    try:
        with open("/proc/cpuinfo", "r") as cf:
            cdata = cf.read()
            has_avx512 = "avx512" in cdata or "avx512f" in cdata
            is_zen = "AMD" in cdata or "Zen" in cdata or "EPYC" in cdata
    except Exception:
        pass

    total_mem_gb = mem_gb + swap_gb
    # Zen 5 / EPYC with AVX-512 and ample RAM+Swap buffer
    if (is_zen or has_avx512) and total_mem_gb >= 20.0 and cores >= 4:
        return 8 if has_avx512 and total_mem_gb >= 22.0 else 6
    if total_mem_gb >= 18.0 and cores >= 4:
        return 6
    return 4


NINJA_BYPASS_STATUS = "[%p %f/%t] "


def bypass_ready(build_root: Path) -> Optional[Path]:
    """Return the combined ninja file Path if Direct Ninja Bypass is safe, else None.

    Checks the 7 invariants:
      G0: kill-switch FORGE_NINJA_BYPASS != '0'
      G1: frozen graphs present (out/combined-*.ninja, out/soong/build.ninja, out/build-*.ninja)
      G1b: incremental state present (out/.ninja_log, out/.ninja_deps)
      G2: ninja binary available in prebuilts or PATH
      G3: no .bp / .mk file is newer than out/soong/build.ninja
    """
    if os.environ.get("FORGE_NINJA_BYPASS", "1") == "0":
        return None
    out = build_root / "out"
    if not out.exists():
        return None
    combined = sorted(out.glob("combined-*.ninja"))
    soong_ninja = out / "soong" / "build.ninja"
    kati = [p for p in out.glob("build-*.ninja")]
    if not combined or not soong_ninja.exists() or not kati:
        return None
    for required in (out / ".ninja_log", out / ".ninja_deps"):
        if not required.exists():
            return None
    ninja_bin = build_root / "prebuilts/build-tools/linux-x86/bin/ninja"
    if not ninja_bin.exists():
        if not shutil.which("ninja"):
            return None
    graph_mtime = soong_ninja.stat().st_mtime
    # G3: check freshness of Android.bp / Android.mk vs graph mtime
    try:
        r = subprocess.run(
            ["bash", "-c",
             f"cd {build_root} && find . -maxdepth 6 -name 'out' -prune -o "
             r"\( -name 'Android.bp' -o -name 'Android.mk' \) -printf '%T@\n' 2>/dev/null "
             "| sort -rn | head -1"],
            capture_output=True, text=True, timeout=30)
        if r.returncode == 0 and r.stdout.strip():
            latest_mtime = float(r.stdout.strip())
            if latest_mtime > graph_mtime + 1.0:
                log.log(f"bypass: .bp/.mk newer than graph ({latest_mtime:.1f} > {graph_mtime:.1f}) — falling back to soong")
                return None
    except Exception:
        pass
    return combined[0]


def _launcher(plan, soong_ui: Path, jobs: int, target: str,
              combined: Optional[Path] = None,
              build_root: Optional[Path] = None,
              env_overrides: Optional[Dict[str, str]] = None) -> str:
    combo = lunch_combo(plan)
    exports = []
    if env_overrides:
        for k in ("ALLOW_MISSING_DEPENDENCIES", "TARGET_RELEASE", "WITH_DEXPREOPT",
                  "DONT_INSTALL_DEX_DEBUG_INFO", "JAVA_TOOL_OPTIONS", "GOGC",
                  "GOMAXPROCS", "GODEBUG", "GOMEMLIMIT", "OUT_DIR", "NINJA_ARGS"):
            if k in env_overrides:
                exports.append(f"export {k}={shlex.quote(str(env_overrides[k]))};")
        for k, v in plan.rom.env.items():
            exports.append(f"export {k}={shlex.quote(str(v))};")
    export_str = (" ".join(exports) + " ") if exports else ""
    if combined is not None:
        ninja_bin = "prebuilts/build-tools/linux-x86/bin/ninja"
        if build_root and not (build_root / ninja_bin).exists():
            ninja_bin = shutil.which("ninja") or "ninja"
        return (
            "set +eu; "
            "source build/envsetup.sh >/dev/null 2>&1; "
            f"lunch {combo} >/dev/null 2>&1; "
            f"{export_str}"
            f"export NINJA_STATUS='{NINJA_BYPASS_STATUS}'; "
            f"exec {ninja_bin} -f {combined} -j {jobs} {target}"
        )
    return (
        "set +eu; "
        "source build/envsetup.sh >/dev/null 2>&1; "
        f"lunch {combo} >/dev/null 2>&1; "
        f"{export_str}"
        f"exec {soong_ui} --make-mode -j {jobs} {target}"
    )


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

    jobs = optimal_jobs(plan)
    log.log(f"dynamic parallel jobs: -j {jobs}")

    # Check for direct ninja bypass readiness (§3)
    combined = bypass_ready(build_root)
    mode = "ninja-direct" if combined else "soong"
    log.ok(f"slice mode: {mode} "
           f"({'bypassing soong_ui entirely' if combined else 'full soong_ui pipeline'})")

    e = build_env(plan, build_root, use_ccache=use_ccache, phase="exec" if combined else "analysis")
    e["NINJA_ARGS"] = f"-j {jobs}"
    if allow_missing_deps:
        e["ALLOW_MISSING_DEPENDENCIES"] = "true"

    launcher = _launcher(plan, soong_ui, jobs, target, combined=combined, build_root=build_root, env_overrides=e)

    prefix = (_cgroup_run_prefix("exec" if combined else "analysis") if os.environ.get("FORGE_CGROUP") == "1" else None) or ["bash", "-c"]
    t0 = time.time()
    with open(build_log, "ab", buffering=0) as logf:
        proc = subprocess.Popen([*prefix, launcher], cwd=str(build_root),
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

    # ---- live in-place ticker & milestone pulse thread -----------------------
    SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
    last: Dict[str, int] = {"pct": 0, "done": 0, "total": 0}
    notified = {"pct": -5}

    def _btrfs_savings() -> str:
        try:
            snap = storage.snapshot(build_root)
            if snap.mode != "btrfs":
                return ""
            logical_used = max(0.0, snap.cap_gb - snap.logical_free_gb)
            img_path = storage.backing_dir() / storage.IMG_NAME
            if img_path.exists():
                physical_used = (img_path.stat().st_blocks * 512) / (1024 ** 3)
            else:
                physical_used = logical_used
            saved = max(0.0, logical_used - physical_used)
            if saved >= 0.1:
                return f" | [Disk: {logical_used:.1f}G log -> {physical_used:.1f}G phys ({saved:.1f}G saved)]"
        except Exception:
            pass
        return ""

    def live_heartbeat() -> None:
        last_log_pos = 0
        latest_action = "compiling"
        disk_tick = 0
        cached_free_disk = 0.0
        cached_disk_label = ""
        spinner_idx = 0
        last_permanent_time = time.time()
        last_milestone_done = 0
        last_milestone_pct = 0

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

            disk_tick += 1
            if disk_tick >= 5 or not cached_disk_label:
                disk_tick = 0
                try:
                    hs = storage.snapshot(build_root)
                    cached_free_disk = hs.logical_free_gb
                    cached_disk_label = ("vol" if hs.mode == "btrfs" else "plain")
                except Exception:
                    pass

            now_str = time.strftime("%H:%M:%S")
            prog_label = f"[{pct}% {done}/{total}]" if total > 0 else "[building]"

            # Calculate remaining budget time
            elapsed_cur = time.time() - t0
            rem_s = max(0, budget_s - elapsed_cur)
            rem_h = int(rem_s) // 3600
            rem_m = (int(rem_s) % 3600) // 60
            budget_str = f"budget: {rem_h}h {rem_m:02d}m left" if rem_h > 0 else f"budget: {rem_m}m left"

            # Check milestone / pulse trigger
            now_time = time.time()
            is_milestone = (total > 0 and (done >= last_milestone_done + 500 or (pct >= last_milestone_pct + 1 and pct > 0)))
            is_pulse = (now_time - last_permanent_time >= 20.0)

            if is_milestone or is_pulse:
                tag_label = "MILESTONE" if is_milestone else "PULSE"
                storage_info = _btrfs_savings()
                sys.stdout.write(
                    f"\r[{now_str}] [{tag_label}] {prog_label} {latest_action} | "
                    f"{budget_str}{storage_info} | {mem_str} | "
                    f"Disk({cached_disk_label}): {cached_free_disk:.1f}G free\x1b[K\n"
                )
                sys.stdout.flush()
                last_permanent_time = now_time
                if is_milestone:
                    last_milestone_done = done
                    last_milestone_pct = pct
            else:
                spinner = SPINNER_FRAMES[spinner_idx]
                sys.stdout.write(
                    f"\r[{now_str}] {spinner} {prog_label} {latest_action} | "
                    f"{budget_str} | {mem_str} | "
                    f"Disk({cached_disk_label}): {cached_free_disk:.1f}G free\x1b[K"
                )
                sys.stdout.flush()
                spinner_idx = (spinner_idx + 1) % len(SPINNER_FRAMES)

            if pct - notified["pct"] >= 5:
                notified["pct"] = pct
                log.notice(
                    f"PROGRESS:{pct}:{done}/{total}",
                    title="Build-Progress")

        sys.stdout.write("\n")
        sys.stdout.flush()

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
                        if len(dynamic_swap_chunks) < 4:
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

    # ---- L3 PSI memory watchdog: graceful SIGINT on sustained thrashing ------
    def memory_watchdog() -> None:
        stall_since: Optional[float] = None
        while not stop.wait(2.0):
            psi = _read_psi()
            swap_full = False
            ram_pct = 0.0
            try:
                with open("/proc/meminfo", "r") as mf:
                    mem_data = mf.read()
                    sw_tot_m = re.search(r"SwapTotal:\s+(\d+)", mem_data)
                    sw_free_m = re.search(r"SwapFree:\s+(\d+)", mem_data)
                    tot_m = re.search(r"MemTotal:\s+(\d+)", mem_data)
                    avail_m = re.search(r"MemAvailable:\s+(\d+)", mem_data)
                    if sw_tot_m and sw_free_m and tot_m and avail_m:
                        sw_tot = int(sw_tot_m.group(1))
                        sw_free = int(sw_free_m.group(1))
                        tot = int(tot_m.group(1))
                        avail = int(avail_m.group(1))
                        swap_full = ((sw_tot - sw_free) / max(1, sw_tot)) > 0.97
                        ram_pct = ((tot - avail) / max(1, tot)) * 100.0
            except Exception:
                continue

            hard = (psi is not None and psi > 95.0) or (swap_full and ram_pct > 96.0)
            if hard:
                stall_since = stall_since or time.time()
                if time.time() - stall_since > 90:
                    log.warn(f"MEM-STALL (PSI full avg60={psi}, swap_full={swap_full}, "
                             f"ram={ram_pct:.0f}%) — SIGINT for consistent bank")
                    _graceful_stop(STOP_MEMORY)
                    return
            else:
                stall_since = None

    # ---- mid-slice checkpoint watchdog (Phase 2.2) --------------------------
    ckpt_min = int(os.environ.get("FORGE_CKPT_MIN", "0") or 0)

    def checkpoint_watchdog() -> None:
        if ckpt_min <= 0:
            return
        while not stop.wait(ckpt_min * 60):
            try:
                storage.ckpt_snapshot(build_root / "out", f"t{int(time.time())}")
            except Exception:
                pass

    threads = [threading.Thread(target=budget_watchdog, daemon=True),
               threading.Thread(target=disk_watchdog, daemon=True),
               threading.Thread(target=dynamic_swap_watchdog, daemon=True),
               threading.Thread(target=memory_watchdog, daemon=True),
               threading.Thread(target=checkpoint_watchdog, daemon=True),
               threading.Thread(target=live_heartbeat, daemon=True)]
    for t in threads:
        t.start()

    rc = proc.wait()
    stop.set()
    for t in threads:
        t.join(timeout=5)
    fenv.deactivate_swap_chunks(dynamic_swap_chunks)
    elapsed = time.time() - t0

    # Single fallback replay if ninja-direct failed non-zero and wasn't stopped by watchdog
    if mode == "ninja-direct" and rc != 0 and not stopped_by_watchdog.is_set() and not stop_reason["reason"]:
        rem_budget = int(budget_s - elapsed)
        if rem_budget > 300:
            log.warn("bypass execution exited non-zero — replaying via soong_ui once")
            soong_launcher = _launcher(plan, soong_ui, jobs, target, combined=None, build_root=build_root)
            t_fb = time.time()
            with open(build_log, "ab", buffering=0) as logf:
                fb_proc = subprocess.Popen(["bash", "-c", soong_launcher], cwd=str(build_root),
                                           stdout=logf, stderr=logf, env=e,
                                           start_new_session=True)
            rc = fb_proc.wait()
            elapsed += (time.time() - t_fb)

    # ---- classify -------------------------------------------------------------
    # The stop_reason decides whether resuming is SAFE:
    #   budget / root-disk -> sliced   (transient; next slot makes progress)
    #   disk               -> capacity (structural; the conveyor refuses to
    #                                  re-dispatch — the storage-deadlock fix)
    #   memory             -> mem-stall (sustained swap/RAM thrashing)
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
        if reason == STOP_DISK:
            return "capacity"
        if reason == STOP_MEMORY:
            return "mem-stall"
        return "sliced"
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


def explain_dirty_graph(build_root: Path) -> Optional[str]:
    """Diagnostic probe (P0.3): run ninja -d explain -n to inspect why a restored graph is dirty."""
    out = build_root / "out"
    bootstrap_ninja = out / "soong" / ".bootstrap" / "build.ninja"
    soong_ninja = out / "soong" / "build.ninja"
    if not (bootstrap_ninja.exists() and soong_ninja.exists()):
        return None
    ninja_bin = build_root / "prebuilts/build-tools/linux-x86/bin/ninja"
    ninja_cmd = str(ninja_bin) if ninja_bin.exists() else (shutil.which("ninja") or "ninja")
    try:
        r = subprocess.run([ninja_cmd, "-d", "explain", "-f", str(bootstrap_ninja), "-n", str(soong_ninja)],
                           capture_output=True, text=True, timeout=60, cwd=str(build_root))
        return r.stdout or r.stderr
    except Exception as e:
        return f"explain probe error: {e}"
