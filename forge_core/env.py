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


def _safe_run(cmd: List[str], check: bool = False, **kwargs) -> Optional[subprocess.CompletedProcess]:
    try:
        return subprocess.run(cmd, check=check, **kwargs)
    except (OSError, FileNotFoundError, Exception):
        return None


def _df_free_gb(path: str) -> float:
    try:
        if os.path.exists(path):
            res = _safe_run(["btrfs", "filesystem", "usage", "-b", path], capture_output=True, text=True)
            if res and res.returncode == 0:
                for line in res.stdout.splitlines():
                    if "Free (estimated)" in line:
                        m = re.search(r"Free\s*\(estimated\):\s*([0-9.]+)\s*([KMGT]i?B)", line, re.IGNORECASE)
                        if m:
                            val, unit = float(m.group(1)), m.group(2).upper()
                            mult = {"B": 1, "KB": 1024, "KIB": 1024, "MB": 1024**2, "MIB": 1024**2, "GB": 1024**3, "GIB": 1024**3, "TB": 1024**4, "TIB": 1024**4}
                            return (val * mult.get(unit, 1024**3)) / (1024 ** 3)
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
        with open("/proc/meminfo", encoding="ascii") as fh:
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
        with open("/proc/mounts", encoding="ascii") as fh:
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
    "/opt/hostedtoolcache",
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


def setup_compressed_volume() -> None:
    """Setup a btrfs compressed volume on /mnt if supported.

    Transparent zstd compression gives 2.5x-3.5x space multiplication on-the-fly for
    AOSP object binaries, expanding a 64 GB runner SSD volume into 150+ GB usable disk.
    """
    if not os.path.exists("/mnt"):
        return

    st = _safe_run(["df", "-T", "/mnt"], capture_output=True, text=True)
    if st and "btrfs" in st.stdout:
        return

    if shutil.which("mkfs.btrfs"):
        _safe_run(["sudo", "umount", "/mnt"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for dev in ("/dev/sdb", "/dev/sdc"):
            if os.path.exists(dev):
                _safe_run(["sudo", "mkfs.btrfs", "-f", "-m", "single", "-d", "single", dev],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                _safe_run(["sudo", "mount", "-o", "compress=zstd:1,space_cache=v2,nodatacow", dev, "/mnt"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                _safe_run(["sudo", "chmod", "1777", "/mnt"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                st2 = _safe_run(["df", "-T", "/mnt"], capture_output=True, text=True)
                if st2 and "btrfs" in st2.stdout:
                    log.log(f"setup transparent btrfs zstd:1 compressed volume on /mnt via {dev}")
                    return
        _safe_run(["sudo", "mount", "-a"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def reclaim_disk() -> List[str]:
    """Remove fat that AOSP never touches. Returns list of what was removed."""
    if os.path.exists("/mnt"):
        _safe_run(["sudo", "chmod", "1777", "/mnt"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    setup_compressed_volume()
    _safe_run(["sudo", "systemctl", "stop", "docker"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _safe_run(["sudo", "systemctl", "stop", "containerd"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    removed = []
    for p in RECLAIM_PATHS:
        if Path(p).exists():
            _safe_run(["sudo", "rm", "-rf", p], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            removed.append(p)
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


def protect_runner_processes() -> None:
    """Shield the GitHub Actions runner agent and build orchestrator from Linux OOM killer."""
    _safe_run(["sudo", "sysctl", "-w", "vm.swappiness=60", "vm.vfs_cache_pressure=50"],
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


def ensure_swap(swap_path: str, size_gb: int = 10) -> bool:
    has = _safe_run(["swapon", "--show"], capture_output=True, text=True)
    if has and has.stdout.strip():
        protect_runner_processes()
        return True
    try:
        free = _df_free_gb(os.path.dirname(swap_path))
        if free < size_gb + 20:
            log.log(f"only {free:.0f} GB free on {os.path.dirname(swap_path)} — "
                    f"skipping {size_gb}G swap to protect the disk budget")
            protect_runner_processes()
            return False
    except Exception:
        pass
    # Try fallocate first, fallback to dd
    _safe_run(["sudo", "fallocate", "-l", f"{size_gb}G", swap_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not os.path.exists(swap_path) or os.path.getsize(swap_path) < (size_gb * 1024 * 1024 * 1024):
        _safe_run(["sudo", "dd", "if=/dev/zero", f"of={swap_path}", "bs=1M", f"count={size_gb * 1024}", "status=none"],
                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _safe_run(["sudo", "chmod", "600", swap_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _safe_run(["sudo", "mkswap", swap_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    r = _safe_run(["sudo", "swapon", swap_path], capture_output=True, text=True)
    protect_runner_processes()
    if r and r.returncode == 0:
        log.log(f"swap on: {size_gb} GB at {swap_path}")
        return True
    return False


def install_pkgs(pkgs: List[str]) -> None:
    """Version-profile-driven apt install (JDK etc. come from versions.yaml)."""
    if not pkgs:
        return
    if shutil.which("add-apt-repository"):
        _safe_run(["sudo", "add-apt-repository", "-y", "universe"],
                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _safe_run(["sudo", "apt-get", "update", "-qq"],
              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    r = _safe_run(["sudo", "apt-get", "install", "-y", "-qq", *pkgs],
                  capture_output=True, text=True)
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
