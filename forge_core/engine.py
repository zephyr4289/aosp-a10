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


FASTBOOT_ZIP_PAT = re.compile(r"(-img-.*|fastboot).*\.zip$")


def build_env(plan, build_root: Path, use_ccache: bool = False) -> Dict[str, str]:
    e = dict(os.environ)
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
        f"exec {soong_ui} --make-mode {target}"
    )
    e = build_env(plan, build_root, use_ccache=use_ccache)
    if allow_missing_deps:
        e["ALLOW_MISSING_DEPENDENCIES"] = "true"

    t0 = time.time()
    with open(build_log, "ab", buffering=0) as logf:
        proc = subprocess.Popen(["bash", "-c", launcher], cwd=str(build_root),
                                stdout=logf, stderr=logf, env=e,
                                start_new_session=True)   # <- own pgid

    stop = threading.Event()

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
        _pg(signal.SIGINT)
        if stop.wait(300):
            return
        log.warn("grace period over — SIGKILL backstop")
        _pg(signal.SIGKILL)

    # ---- disk watchdog: ladder BEFORE ENOSPC ----------------------------------
    def disk_watchdog() -> None:
        while not stop.wait(60):
            try:
                free = fenv._df_free_gb(str(build_root))
            except OSError:
                continue
            if free < min_free_gb + 4:
                freed = fenv.reclaim_ladder(build_root, want_gb=min_free_gb + 4)
                log.warn(f"disk low ({free:.1f} GB) — ladder freed "
                         f"{freed / 2**30:.1f} GB")
            if free < 2.0:
                log.warn("disk critical — early graceful slice stop")
                _pg(signal.SIGINT)

    # ---- progress thread ------------------------------------------------------
    last: Dict[str, int] = {"pct": 0, "done": 0, "total": 0}
    notified = {"pct": -5}

    def progress() -> None:
        while not stop.wait(30):
            nonlocal last
            last = relay.progress_from_log(build_log, last)
            pct = last.get("pct", 0)
            if pct - notified["pct"] >= 5:
                notified["pct"] = pct
                log.notice(
                    f"PROGRESS:{pct}:{last.get('done', '?')}/{last.get('total', '?')}",
                    title="Build-Progress")
                log.log(f"progress {pct}% ({last.get('done')}/{last.get('total')})")

    threads = [threading.Thread(target=budget_watchdog, daemon=True),
               threading.Thread(target=disk_watchdog, daemon=True),
               threading.Thread(target=progress, daemon=True)]
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
    elif elapsed >= budget_s - 5:
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
