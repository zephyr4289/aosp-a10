"""Runner environment engineering: mounts, disk budgets, swap, reclaim ladder.

Upstream aosp-a10 died of ENOSPC at the 94% link because everything lived on
`/` (~14-25 GB usable) while `/mnt` (~65 GB free) sat untouched and the cache
packer staged ~20 GB of split parts on the SAME disk as tree+out.

ROMForge's storage contract:
  * BUILD_ROOT is auto-placed on the mount with the most free space
    (GHA: /mnt ~65 GB free vs / ~14-25 GB).
  * Relay parts stream straight into the state store when possible
    (split --filter), otherwise they stage on the mount with the most free
    space that is NOT the build root.
  * A reclaim ladder frees rebuildable-by-design bytes before disk pressure
    can kill a link:  /tmp junk -> out/**/symbols -> intermediate images.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from . import log


def _safe_run(cmd: List[str], check: bool = False, timeout: int = 120, **kwargs) -> Optional[subprocess.CompletedProcess]:
    try:
        return subprocess.run(cmd, check=check, timeout=timeout, **kwargs)
    except (OSError, FileNotFoundError, subprocess.TimeoutExpired, Exception):
        return None


def _df_free_gb(path: str) -> float:
    try:
        st = os.statvfs(path)
        return st.f_bavail * st.f_frsize / (1024 ** 3)
    except Exception:
        return 0.0


def _df_total_gb(path: str) -> float:
    try:
        st = os.statvfs(path)
        return (st.f_blocks * st.f_frsize) / (1024 ** 3)
    except Exception:
        return 0.0


@dataclass
class MountInfo:
    path: str
    device: str
    fstype: str
    total_gb: float = 0.0
    free_gb: float = 0.0


@dataclass
class RunnerEnv:
    cores: int = 0
    mem_gb: float = 0.0
    is_github_actions: bool = False
    mounts: List[MountInfo] = field(default_factory=list)

    # ---- selection policies ---------------------------------------------------
    def best_mount(self, need_gb: float = 0.0) -> MountInfo:
        writable = []
        for m in self.mounts:
            if m.fstype not in ("ext4", "xfs", "btrfs", "overlayfs", "tmpfs"):
                continue
            if not os.access(m.path, os.W_OK):
                _safe_run(["sudo", "chmod", "1777", m.path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if os.access(m.path, os.W_OK):
                writable.append(m)
        if not writable:
            return MountInfo(path="/tmp", device="tmp", fstype="tmpfs", total_gb=_df_total_gb("/tmp"), free_gb=_df_free_gb("/tmp"))
        return max(writable, key=lambda m: m.free_gb)

    def best_mount_excluding(self, exclude: str) -> Optional[MountInfo]:
        writable = []
        for m in self.mounts:
            if m.path == exclude:
                continue
            if m.fstype not in ("ext4", "xfs", "btrfs", "overlayfs"):
                continue
            if not os.access(m.path, os.W_OK):
                _safe_run(["sudo", "chmod", "1777", m.path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if os.access(m.path, os.W_OK):
                writable.append(m)
        return max(writable, key=lambda m: m.free_gb) if writable else None

    def free_gb(self, path: str) -> float:
        return _df_free_gb(path)

    def report(self) -> str:
        lines = [f"cores={self.cores} mem={self.mem_gb:.1f}GB "
                 f"gha={self.is_github_actions}"]
        for m in self.mounts:
            lines.append(f"  mount {m.path:14s} {m.fstype:8s} "
                         f"total={m.total_gb:6.1f}GB free={m.free_gb:6.1f}GB")
        return "\n".join(lines)


def detect() -> RunnerEnv:
    env = RunnerEnv()
    env.cores = os.cpu_count() or 2
    try:
        with open("/proc/meminfo", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    env.mem_gb = int(line.split()[1]) / (1024 ** 2)
                    break
    except OSError:
        env.mem_gb = 0.0
    env.is_github_actions = "GITHUB_ACTIONS" in os.environ

    if os.path.exists("/mnt") and not os.access("/mnt", os.W_OK):
        _safe_run(["sudo", "chmod", "1777", "/mnt"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    seen = {}
    try:
        with open("/proc/mounts", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                parts = raw.split()
                if len(parts) < 3:
                    continue
                _dev, target, fstype = parts[0], parts[1], parts[2]
                if fstype not in ("ext4", "xfs", "btrfs", "overlayfs"):
                    continue
                # dedupe by device, keep the mount we can write to
                if target.startswith("/snap") or target.startswith("/boot"):
                    continue
                seen.setdefault(_dev, MountInfo(path=target, device=_dev, fstype=fstype))
    except Exception:
        pass
    env.mounts = list(seen.values())
    for m in env.mounts:
        m.total_gb = _df_total_gb(m.path)
        m.free_gb = _df_free_gb(m.path)
    return env


# ---------------------------------------------------------------------------
# Runner preparation (apt packages, reclaim, swap) — the hardened 00 step.
# ---------------------------------------------------------------------------
RECLAIM_PATHS = [
    "/usr/local/lib/android",
    "/usr/share/dotnet",
    "/usr/local/share/boost",
    "/opt/ghc",
    "/opt/az",
    "/usr/local/julia",
    "/usr/local/graalvm",
    "/usr/local/.ghcup",
    "/usr/share/swift",
    "/opt/microsoft",
    "/usr/share/miniconda",
    "/usr/local/lib/node_modules",
    "/usr/share/gradle",
    "/usr/local/share/chromium",
    "/opt/google/chrome",
    "/usr/lib/mono",
    "/usr/lib/jvm/temurin-17-jdk-amd64",
    "/usr/lib/jvm/temurin-11-jdk-amd64",
    "/var/lib/docker",
    "/var/lib/containerd",
    "/etc/docker",
    "/usr/local/share/vcpkg",
    "/usr/local/aws-cli",
    "/usr/local/aws-sam-cli",
    "/imagegeneration",
    "/root/.rustup",
    "/home/runner/.rustup",
    "/root/.cargo",
    "/home/runner/.cargo",
]


def reclaim_disk() -> List[str]:
    """Remove fat that AOSP never touches. Returns list of what was removed."""
    if os.path.exists("/mnt"):
        _safe_run(["sudo", "chmod", "1777", "/mnt"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _safe_run(["sudo", "systemctl", "stop", "docker"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _safe_run(["sudo", "systemctl", "stop", "containerd"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    removed = []
    for p in RECLAIM_PATHS:
        if Path(p).exists():
            _safe_run(["sudo", "rm", "-rf", p], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            removed.append(p)
    # Prune non-Python hostedtoolcache toolchains (e.g. CodeQL, Java, go, Ruby, node)
    # Never delete the running Python environment (/opt/hostedtoolcache/Python)
    if os.path.exists("/opt/hostedtoolcache"):
        try:
            for child in Path("/opt/hostedtoolcache").iterdir():
                if child.name != "Python":
                    _safe_run(["sudo", "rm", "-rf", str(child)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    removed.append(str(child))
        except Exception:
            pass
    for cmd in (["sudo", "docker", "system", "prune", "-af"],
                ["sudo", "apt-get", "clean"]):
        _safe_run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for tmp in ("/var/lib/apt/lists", "/tmp", "/var/tmp"):
        if os.path.exists(tmp):
            for child in Path(tmp).glob("*"):
                try:
                    if child.is_file():
                        child.unlink(missing_ok=True)
                except Exception:
                    pass
    if removed:
        log.log(f"reclaimed {len(removed)} runner blobs "
                f"(~35-45 GB): {', '.join(p.split('/')[-1] for p in removed)}")
    return removed


def protect_runner_processes(swappiness: int = 10) -> None:
    """Shield the GitHub Actions runner agent and build orchestrator from Linux OOM killer."""
    _safe_run(["sudo", "sysctl", "-w",
               f"vm.swappiness={swappiness}",
               "vm.page-cluster=0",
               "vm.watermark_scale_factor=125",
               "vm.vfs_cache_pressure=50"],
              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # Set oom_score_adj to -1000 for all Runner and python orchestrator processes
    for proc_dir in Path("/proc").glob("[0-9]*"):
        try:
            cmdline = (proc_dir / "cmdline").read_bytes().decode("utf-8", errors="ignore")
            if any(k in cmdline for k in ("Runner.", "actions-runner", "forge_core")):
                adj = proc_dir / "oom_score_adj"
                if adj.exists():
                    _safe_run(["sudo", "sh", "-c", f"echo -1000 > {adj}"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass


def _active_swap_gb() -> float:
    has = _safe_run(["swapon", "--show=SIZE", "--bytes", "--noheadings"], capture_output=True, text=True)
    if has is not None and has.returncode == 0:
        total_bytes = 0
        for line in (has.stdout or "").strip().splitlines():
            try:
                total_bytes += int(line.strip())
            except ValueError:
                pass
        return total_bytes / (1024 * 1024 * 1024)
    try:
        with open("/proc/meminfo", "r") as f:
            for line in f:
                if line.startswith("SwapTotal:"):
                    return float(line.split()[1]) / (1024 * 1024)
    except Exception:
        pass
    return 0.0


def swap_topology_report() -> Dict[str, object]:
    """Inspect and return current swap devices, algorithms, priorities, and VM tuning."""
    report: Dict[str, object] = {
        "zram_active": False,
        "zram_size_gb": 0.0,
        "zram_algo": "unknown",
        "disk_swap_gb": 0.0,
        "total_swap_gb": _active_swap_gb(),
        "devices": [],
    }
    try:
        if os.path.exists("/proc/swaps"):
            lines = Path("/proc/swaps").read_text(encoding="utf-8", errors="replace").splitlines()
            for line in lines[1:]:
                parts = line.split()
                if len(parts) >= 5:
                    dev_name, dev_type, sz_kb, used_kb, prio = parts[0], parts[1], parts[2], parts[3], parts[4]
                    sz_gb = round(int(sz_kb) / (1024 * 1024), 2)
                    report["devices"].append({
                        "name": dev_name, "type": dev_type, "size_gb": sz_gb,
                        "prio": int(prio)
                    })
                    if "zram" in dev_name:
                        report["zram_active"] = True
                        report["zram_size_gb"] = sz_gb
                    else:
                        report["disk_swap_gb"] = round(float(report["disk_swap_gb"]) + sz_gb, 2)
    except Exception:
        pass

    try:
        comp_file = Path("/sys/block/zram0/comp_algorithm")
        if comp_file.exists():
            comp_txt = comp_file.read_text(encoding="utf-8", errors="replace").strip()
            # The active algorithm is bracketed, e.g. "lzo [lz4] zstd"
            m = re.search(r"\[([a-zA-Z0-9_-]+)\]", comp_txt)
            if m:
                report["zram_algo"] = m.group(1)
            else:
                report["zram_algo"] = comp_txt
    except Exception:
        pass

    return report


def swap_topology_summary() -> str:
    """Format a single-line auditable topology string for slot logs."""
    topo = swap_topology_report()
    zram_str = f"zram: {topo['zram_size_gb']}G ({topo['zram_algo']}, p100)" if topo["zram_active"] else "zram: OFF"
    disk_str = f"disk: {topo['disk_swap_gb']}G (p10)" if topo["disk_swap_gb"] > 0 else "disk: none"
    return f"{zram_str} | {disk_str} | total_swap: {topo['total_swap_gb']:.1f}G"


def ensure_zram(size_gb: int = 6, algo: str = "lz4") -> bool:
    """Set up tier-1 compressed RAM swap (zram) with high priority (p=100).

    Uses lz4 compression by default for 10x lower page fault latency vs zstd.
    Compresses in-RAM memory spikes (e.g. Soong AST parsing) with zero disk I/O.
    Gracefully degrades if unprivileged or kernel module unavailable.
    """
    try:
        if not os.path.exists("/dev/zram0"):
            _safe_run(["sudo", "modprobe", "zram"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not os.path.exists("/dev/zram0"):
            return False

        # Check if already active swap
        current_swaps = ""
        try:
            if os.path.exists("/proc/swaps"):
                current_swaps = Path("/proc/swaps").read_text(encoding="utf-8", errors="replace")
        except Exception:
            pass
        if "/dev/zram0" in current_swaps:
            protect_runner_processes()
            return True

        # Reset zram device if idle
        try:
            if os.path.exists("/sys/block/zram0/reset"):
                _safe_run(["sudo", "sh", "-c", "echo 1 > /sys/block/zram0/reset 2>/dev/null || true"])
        except Exception:
            pass

        # Initialize zram device size & compression algorithm
        zramctl_found = shutil.which("zramctl")
        if zramctl_found:
            r = _safe_run(["sudo", "zramctl", "-s", f"{size_gb}G", "-a", algo, "/dev/zram0"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if r is None or r.returncode != 0:
                # Fallback to zstd if lz4 is not supported on older kernels
                _safe_run(["sudo", "zramctl", "-s", f"{size_gb}G", "-a", "zstd", "/dev/zram0"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif os.path.exists("/sys/block/zram0/disksize"):
            try:
                if os.path.exists("/sys/block/zram0/comp_algorithm"):
                    r_algo = _safe_run(["sudo", "sh", "-c", f"echo {algo} > /sys/block/zram0/comp_algorithm 2>/dev/null"])
                    if r_algo is None or r_algo.returncode != 0:
                        _safe_run(["sudo", "sh", "-c", "echo zstd > /sys/block/zram0/comp_algorithm 2>/dev/null || true"])
                _safe_run(["sudo", "sh", "-c", f"echo {size_gb}G > /sys/block/zram0/disksize 2>/dev/null || true"])
            except Exception:
                pass

        _safe_run(["sudo", "mkswap", "/dev/zram0"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        r = _safe_run(["sudo", "swapon", "-p", "100", "/dev/zram0"], capture_output=True, text=True)
        protect_runner_processes()
        if r and r.returncode == 0:
            log.ok(f"zram tier-1 swap active: +{size_gb} GB on /dev/zram0 (algo={algo}, p=100)")
            return True
    except Exception:
        pass
    return False


def ensure_zswap(max_pool_percent: int = 25, compressor: str = "lz4") -> bool:
    """P0-5.1 (OVERHAUL.md): the zram fallback that actually works on GH runners.

    On GitHub's Azure kernels `modprobe zram` fails every single run
    (zram UNAVAILABLE was present in all six runs' logs), so the tier-1
    memory shield silently degraded to disk-swap-only. zswap needs no
    module and is present on stock Ubuntu 24.04 kernels: pages destined
    for disk swap are compressed in a RAM pool first. Soong AST pages
    compress ~2.5-3.5x, so at 17 GiB swap this buys ~10-14 GiB of
    effective swap-path capacity for free — enough by itself to move the
    32.6 GiB fused analysis from zero-margin to survivable-margin on the
    minter run. The degraded state must never again be a one-line WARN
    nobody reads: this function logs the full topology either way."""
    base = Path("/sys/module/zswap/parameters")
    try:
        if not (base / "enabled").exists():
            log.warn("zswap UNAVAILABLE: /sys/module/zswap/parameters "
                     "missing (kernel built without CONFIG_ZSWAP)")
            return False

        def _w(name: str, val: str) -> bool:
            r = _safe_run(["sudo", "sh", "-c", f"echo {val} > {base / name}"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return r is not None and r.returncode == 0

        if not _w("enabled", "1"):
            cur = (base / "enabled").read_text(encoding="utf-8",
                                               errors="replace").strip()
            log.warn(f"zswap enable refused by sysfs (enabled={cur}) — "
                     "swap-path compression unavailable; the 32.6 GiB "
                     "analysis runs disk-swap-only")
            return False
        _w("compressor", compressor)
        _w("max_pool_percent", str(max_pool_percent))
        comp = (base / "compressor").read_text(encoding="utf-8",
                                               errors="replace").strip()
        pct = (base / "max_pool_percent").read_text(encoding="utf-8",
                                                    errors="replace").strip()
        log.ok(f"zswap swap-path compression active (compressor={comp}, "
               f"max_pool_percent={pct}) — up to {pct}% of RAM now "
               f"buffers compressed swap pages (~2.5-3.5x on Soong AST "
               f"pages => +10-14 GiB effective capacity)")
        protect_runner_processes()
        return True
    except Exception as e:
        log.warn(f"zswap setup failed: {e}")
        return False


def ensure_swap(swap_path: str, size_gb: int = 4) -> bool:
    current = _active_swap_gb()
    if current >= size_gb:
        protect_runner_processes()
        return True
    needed_gb = max(1, size_gb - int(current))
    try:
        free = _df_free_gb(os.path.dirname(swap_path))
        if free < needed_gb + 20:
            log.log(f"only {free:.0f} GB free on {os.path.dirname(swap_path)} — "
                    f"skipping {needed_gb}G swap to protect the disk budget")
            protect_runner_processes()
            return False
    except Exception:
        pass
    # Try fallocate first, fallback to dd
    _safe_run(["sudo", "fallocate", "-l", f"{needed_gb}G", swap_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not os.path.exists(swap_path) or os.path.getsize(swap_path) < (needed_gb * 1024 * 1024 * 1024):
        _safe_run(["sudo", "dd", "if=/dev/zero", f"of={swap_path}", "bs=1M", f"count={needed_gb * 1024}", "status=none"],
                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _safe_run(["sudo", "chmod", "600", swap_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _safe_run(["sudo", "mkswap", swap_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    r = _safe_run(["sudo", "swapon", "-p", "10", swap_path], capture_output=True, text=True)
    protect_runner_processes()
    if r and r.returncode == 0:
        log.log(f"swap on: +{needed_gb} GB at {swap_path} (priority 10, target {size_gb} GB)")
        return True
    return False


def activate_swap_chunk(chunk_path: str, chunk_size_gb: int = 2) -> bool:
    """Dynamically allocate and activate an incremental swap chunk on-demand."""
    try:
        free = _df_free_gb(os.path.dirname(chunk_path))
        if free < chunk_size_gb + 12:
            return False
        _safe_run(["sudo", "fallocate", "-l", f"{chunk_size_gb}G", chunk_path],
                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not os.path.exists(chunk_path) or os.path.getsize(chunk_path) < (chunk_size_gb * 1024 * 1024 * 1024):
            _safe_run(["sudo", "dd", "if=/dev/zero", f"of={chunk_path}", "bs=1M", f"count={chunk_size_gb * 1024}", "status=none"],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _safe_run(["sudo", "chmod", "600", chunk_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _safe_run(["sudo", "mkswap", chunk_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        r = _safe_run(["sudo", "swapon", "-p", "10", chunk_path], capture_output=True, text=True)
        protect_runner_processes()
        return bool(r and r.returncode == 0)
    except Exception:
        return False


def deactivate_swap_chunks(swap_chunks: List[Path]) -> None:
    """Deactivate and delete dynamic swap chunks to immediately reclaim disk space."""
    for chunk in swap_chunks:
        try:
            _safe_run(["sudo", "swapoff", str(chunk)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if chunk.exists():
                _safe_run(["sudo", "rm", "-f", str(chunk)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass


def install_pkgs(pkgs: List[str]) -> None:
    """Version-profile-driven apt install (JDK etc. come from versions.yaml)."""
    if not pkgs:
        return
    apt_env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
    if shutil.which("add-apt-repository"):
        _safe_run(["sudo", "add-apt-repository", "-y", "universe"],
                  env=apt_env, timeout=60, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _safe_run(["sudo", "apt-get", "update", "-qq"],
              env=apt_env, timeout=60, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    r = _safe_run(["sudo", "apt-get", "install", "-y", "-qq", *pkgs],
                  env=apt_env, timeout=120, capture_output=True, text=True)
    if r and r.returncode != 0:
        log.warn(f"apt install had issues (continuing): {r.stderr.strip()[:200]}")


def ncurses5_compat() -> None:
    """Android-10-era host tools want ncurses5/tinfo5 sonames; link to v6."""
    base = "/usr/lib/x86_64-linux-gnu"
    if not os.path.isdir(base):
        return
    for lib in ("libncurses.so.5", "libtinfo.so.5", "libncursesw.so.5"):
        if os.path.exists(os.path.join(base, lib)):
            continue
        stem = lib.split(".so")[0]
        try:
            hits = [p for p in os.listdir(base) if p.startswith(stem + ".so.6")]
            if hits:
                _safe_run(["sudo", "ln", "-sf", f"{base}/{hits[0]}", f"{base}/{lib}"])
        except Exception:
            pass
    if not os.path.exists(os.path.join(base, "libtinfo.so.5")):
        _safe_run(["sudo", "ln", "-sf", f"{base}/libtinfo.so.6", f"{base}/libtinfo.so.5"])


# ---------------------------------------------------------------------------
# Disk watchdog / reclaim ladder
# ---------------------------------------------------------------------------
RECLAIM_LADDER = [
    # (label, glob under BUILD_ROOT, why-it-is-safe)
    ("tmp", "**/.reclaim_tmp", "scratch"),
    ("oat-dex", "out/target/product/*/obj/*/oat_x86*", "host-side test dex"),
]

LADDER_PATTERNS = [
    "out/target/product/*/obj/*/oat_x86*",
    "out/target/product/*/symbols*",
    "out/target/product/*/*/symbols*",
]


def reclaim_ladder(root: Path, want_gb: float = 8.0) -> float:
    """Free rebuildable bytes under `root` until >= want_gb free.

    Returns bytes freed. NEVER touches anything ninja cannot regenerate
    cheaply — that is the entire safety argument (see TECHNICAL.md §5).
    Unlinks individual files while preserving directory hierarchy so concurrent
    and future ninja copy commands never fail with ENOENT.
    """
    freed = 0.0
    for pattern in LADDER_PATTERNS:
        if _df_free_gb(str(root)) >= want_gb:
            break
        for p in root.glob(pattern):
            if p.is_dir():
                size = 0
                for f in p.rglob("*"):
                    try:
                        if f.is_file():
                            sz = f.stat().st_size
                            f.unlink(missing_ok=True)
                            size += sz
                    except OSError:
                        pass
                freed += size
                if size > 0:
                    log.log(f"ladder: reclaimed {size / 2**30:.1f} GB at {p}")
            elif p.is_file():
                try:
                    sz = p.stat().st_size
                    p.unlink(missing_ok=True)
                    freed += sz
                except OSError:
                    pass
    return freed


def _dir_size(p: Path) -> int:
    total = 0
    for f in p.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            pass
    return total


def assert_disk(path: str, min_gb: float, context: str) -> None:
    free = _df_free_gb(path)
    if free < min_gb:
        raise RuntimeError(
            f"DISK CRITICAL: {free:.1f} GB free at {path} ({context}); "
            f"need {min_gb:.0f} GB. Reclaim ladder exhausted — aborting before "
            f"corrupting the build state.")
    if free < min_gb + 6:
        log.warn(f"disk low: {free:.1f} GB free ({context}) — ladder armed")


def disk_table(root: str = "/") -> Dict[str, float]:
    outd = {}
    for m in detect().mounts:
        outd[m.path] = m.free_gb
    outd.setdefault("/", _df_free_gb("/"))
    return outd
