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
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from . import log


def _df_free_gb(path: str) -> float:
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize / (1024 ** 3)


def _df_total_gb(path: str) -> float:
    st = os.statvfs(path)
    return (st.f_blocks * st.f_frsize) / (1024 ** 3)


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
                try:
                    subprocess.run(["sudo", "chmod", "1777", m.path], check=False,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except Exception:
                    pass
            if os.access(m.path, os.W_OK):
                writable.append(m)
        if not writable:
            return MountInfo(path="/tmp", device="tmp", fstype="tmpfs")
        return max(writable, key=lambda m: m.free_gb)

    def best_mount_excluding(self, exclude: str) -> Optional[MountInfo]:
        writable = []
        for m in self.mounts:
            if m.path == exclude:
                continue
            if m.fstype not in ("ext4", "xfs", "btrfs", "overlayfs"):
                continue
            if not os.access(m.path, os.W_OK):
                try:
                    subprocess.run(["sudo", "chmod", "1777", m.path], check=False,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except Exception:
                    pass
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
        try:
            subprocess.run(["sudo", "chmod", "1777", "/mnt"], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    seen = {}
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
    env.mounts = list(seen.values())
    for m in env.mounts:
        try:
            m.total_gb = _df_total_gb(m.path)
            m.free_gb = _df_free_gb(m.path)
        except OSError:
            pass
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
]


def reclaim_disk() -> List[str]:
    """Remove fat that AOSP never touches. Returns list of what was removed."""
    if os.path.exists("/mnt"):
        try:
            subprocess.run(["sudo", "chmod", "1777", "/mnt"], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
    removed = []
    for p in RECLAIM_PATHS:
        if Path(p).exists():
            subprocess.run(["sudo", "rm", "-rf", p], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            removed.append(p)
    for cmd in (["sudo", "docker", "system", "prune", "-af"],
                ["sudo", "apt-get", "clean"]):
        subprocess.run(cmd, check=False, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
    for tmp in ("/var/lib/apt/lists/*", "/tmp/*", "/var/tmp/*"):
        subprocess.run(["sudo", "rm", "-rf", tmp], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if removed:
        log.log(f"reclaimed {len(removed)} runner blobs "
                f"(~25-30 GB): {', '.join(p.split('/')[-1] for p in removed)}")
    return removed


def ensure_swap(swap_path: str, size_gb: int = 4) -> bool:
    has = subprocess.run(["swapon", "--show"], capture_output=True, text=True)
    if has.stdout.strip():
        return True
    free = _df_free_gb(os.path.dirname(swap_path))
    if free < size_gb + 20:
        log.log(f"only {free:.0f} GB free on {os.path.dirname(swap_path)} — "
                f"skipping {size_gb}G swap to protect the disk budget")
        return False
    for cmd in (["sudo", "fallocate", "-l", f"{size_gb}G", swap_path],
                ["sudo", "chmod", "600", swap_path],
                ["sudo", "mkswap", swap_path],
                ["sudo", "swapon", swap_path]):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            log.log(f"swap setup failed at {' '.join(cmd[:2])}: {r.stderr.strip()[:120]}")
            return False
    log.log(f"swap on: {size_gb} GB at {swap_path}")
    return True


def install_pkgs(pkgs: List[str]) -> None:
    """Version-profile-driven apt install (JDK etc. come from versions.yaml)."""
    if not pkgs:
        return
    subprocess.run(["sudo", "add-apt-repository", "-y", "universe"], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["sudo", "apt-get", "update", "-qq"], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    r = subprocess.run(["sudo", "apt-get", "install", "-y", "-qq", *pkgs],
                       capture_output=True, text=True)
    if r.returncode != 0:
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
        hits = [p for p in os.listdir(base) if p.startswith(stem + ".so.6")]
        if hits:
            os.system(f"sudo ln -sf {base}/{hits[0]} {base}/{lib}")
    # upstream-honed symlink for the prebuilt clang stack
    if not os.path.exists(os.path.join(base, "libtinfo.so.5")):
        os.system(f"sudo ln -sf {base}/libtinfo.so.6 "
                  f"{base}/libtinfo.so.5 2>/dev/null || true")


# ---------------------------------------------------------------------------
# Disk watchdog / reclaim ladder
# ---------------------------------------------------------------------------
RECLAIM_LADDER = [
    # (label, glob under BUILD_ROOT, why-it-is-safe)
    ("tmp", "**/.reclaim_tmp", "scratch"),
    ("symbols", "out/target/product/*/symbols", "unstripped copies; install "
     "rules re-run cheaply from obj/ on demand"),
    ("oat-dex", "out/target/product/*/obj/*/oat_x86*", "host-side test dex"),
    ("super-img", "out/target/product/*/*.img.new", "intermediate super builds"),
]

LADDER_PATTERNS = [
    "out/target/product/*/symbols",
    "out/target/product/*/obj/*/oat_x86*",
    "out/target/product/*/*.img.new",
]


def reclaim_ladder(root: Path, want_gb: float = 8.0) -> float:
    """Free rebuildable bytes under `root` until >= want_gb free.

    Returns bytes freed. NEVER touches anything ninja cannot regenerate
    cheaply — that is the entire safety argument (see TECHNICAL.md §5).
    """
    freed = 0.0
    for pattern in LADDER_PATTERNS:
        if _df_free_gb(str(root)) >= want_gb:
            break
        for p in root.glob(pattern):
            if p.is_dir():
                size = _dir_size(p)
                shutil.rmtree(p, ignore_errors=True)
                freed += size
                log.log(f"ladder: reclaimed {size / 2**30:.1f} GB at {p}")
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
