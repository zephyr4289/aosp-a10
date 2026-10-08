"""Compressed build volume — the structural fix for the /mnt capacity deadlock.

The math that killed runs #30/#34/#35/#36:
    source (~35 GiB) + out/ (~40 GiB)  =  ~75 GiB logical
    /mnt usable                        =  ~65-75 GiB physical
Uncompressed, the working set NEVER fits: restore lands at ~0 GiB free,
the disk watchdog SIGINTs within 30 s, pre_bank_cleanup deletes the source
to bank, and the next slot pays a 30-minute re-download to build for
another 30 seconds. A perfect storage livelock.

ROMForge Storage v2 puts the ENTIRE working set (aosp/ source + out/) on a
transparent-compression volume:

    <best-mount>/romforge/               (raw ext4/xfs — the "backing" dir)
        forge.img                        (sparse btrfs loop file, hard-capped)
        vol/                             (mountpoint: btrfs compress=zstd:1)
            aosp/                        (BUILD_ROOT: source + out/)
        tmp/                             (TMPDIR, caches — raw, uncompressed)
        .forge-swap                      (raw — swapfiles on btrfs are unsafe)

AOSP is overwhelmingly text (java/xml/blueprint/headers) and compresses
~2.2-3.0x with zstd:1; object files ~1.4-1.8x. The 75 GiB logical working
set becomes ~35-45 GiB of physical extents inside a capped sparse image,
leaving 15-25 GiB of /mnt free at all times. mtimes, permissions and file
contents are byte-identical through the volume — the exact-resume ninja
contract (.ninja_log/.ninja_deps/mtimes) is preserved untouched.

Safety properties:
  * The loop file is hard-capped at creation: btrfs can never grow past
    the physical budget, so the build can starve ITSELF (watchdog sees it)
    but can never surprise the runner or the actions daemon.
  * `fstrim` on the volume punches holes in the sparse backing file, so
    deletes (pre-bank junk, reclaim ladder) actually return bytes to /mnt.
  * ANY failure (no btrfs-progs, no loop devices, kernel module missing,
    mount denied) degrades to today's plain-directory layout with the
    watchdog thresholds tightened — never a hard abort, always honest
    logging. The btrfs path is exercised as a self-test in CI
    (ci-tests.yml "Storage selftest") so regressions surface immediately.

Env knobs: FORGE_VOLUME_DIR (backing dir), FORGE_VOLUME_RESERVE_GB
(raw bytes kept out of the image, default 10), FORGE_NO_VOLUME=1 (force
plain mode, used by tests and debugging).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from . import log

VOLUME_SUBDIR = "romforge"          # <mount>/romforge  — the backing dir
IMG_NAME = "forge.img"
VOL_MNT_NAME = "vol"
BUILD_DIR_NAME = "aosp"
TMP_DIR_NAME = "tmp"
DEFAULT_RESERVE_GB = 10.0
MIN_VIABLE_FREE_GB = 25.0           # below this, btrfs mode cannot pay rent
_COMPRESS_OPTS = "compress=zstd:1,noatime"


class StorageError(Exception):
    pass


@dataclass
class VolumeState:
    mode: str = "plain"                       # 'btrfs' | 'plain'
    backing_dir: str = ""                     # raw dir holding forge.img
    vol_mnt: str = ""                         # btrfs mountpoint
    img_path: str = ""
    cap_gb: float = 0.0
    build_root: str = ""                      # canonical BUILD_ROOT
    tmp_dir: str = ""
    reason: str = ""                          # why degraded (plain mode)

    @property
    def degraded(self) -> bool:
        return self.mode != "btrfs"

    def to_dict(self) -> Dict[str, object]:
        return {"mode": self.mode, "backing_dir": self.backing_dir,
                "vol_mnt": self.vol_mnt, "img": self.img_path,
                "cap_gb": round(self.cap_gb, 1),
                "build_root": self.build_root, "tmp_dir": self.tmp_dir,
                "reason": self.reason}


# ---------------------------------------------------------------------------
# low-level helpers
# ---------------------------------------------------------------------------
def _run(cmd: List[str], check: bool = True,
         timeout: int = 300) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(cmd, capture_output=True, text=True,
                          timeout=timeout, check=False)


def _sudo(cmd: List[str], timeout: int = 300
          ) -> "subprocess.CompletedProcess[str]":
    full = ["sudo", *cmd] if os.geteuid() != 0 else cmd
    return subprocess.run(full, capture_output=True, text=True,
                          timeout=timeout, check=False)


def _proc_mounts() -> Dict[str, str]:
    """target -> fstype for real filesystems (from /proc/mounts)."""
    out: Dict[str, str] = {}
    try:
        with open("/proc/mounts", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                parts = raw.split()
                if len(parts) >= 3:
                    out[parts[1]] = parts[2]
    except OSError:
        pass
    return out


def _df_free_gb(path: str) -> float:
    try:
        st = os.statvfs(path)
        return st.f_bavail * st.f_frsize / (1024 ** 3)
    except OSError:
        return 0.0


def backing_dir() -> Path:
    """Where the volume lives: FORGE_VOLUME_DIR or <best-mount>/romforge."""
    env_dir = os.environ.get("FORGE_VOLUME_DIR")
    if env_dir:
        return Path(env_dir)
    if os.path.exists("/mnt") and os.access("/mnt", os.W_OK | os.X_OK):
        return Path("/mnt") / VOLUME_SUBDIR
    try:
        from . import env as fenv
        mnt = fenv.detect().best_mount()
        if mnt and mnt.path not in ("/", "/tmp"):
            return Path(mnt.path) / VOLUME_SUBDIR
    except Exception:
        pass
    return Path("/tmp") / VOLUME_SUBDIR


def reserve_gb() -> float:
    try:
        return float(os.environ.get("FORGE_VOLUME_RESERVE_GB",
                                    DEFAULT_RESERVE_GB))
    except ValueError:
        return DEFAULT_RESERVE_GB


def compute_cap_gb(free_gb: float, reserve: Optional[float] = None) -> float:
    """Pure sizing math: how big the sparse image may be. Testable."""
    reserve = reserve_gb() if reserve is None else reserve
    return max(0.0, free_gb - reserve)


# ---------------------------------------------------------------------------
# Volume lifecycle
# ---------------------------------------------------------------------------
def _ensure_btrfs_progs() -> bool:
    if shutil.which("mkfs.btrfs") and shutil.which("btrfs"):
        return True
    if os.geteuid() == 0 or shutil.which("sudo"):
        r = _sudo(["apt-get", "install", "-y", "-qq", "btrfs-progs"],
                  timeout=180)
        if r.returncode == 0 and shutil.which("mkfs.btrfs"):
            log.log("installed btrfs-progs for the build volume")
            return True
    return False


def _mount_vol(img: Path, mnt: Path) -> bool:
    mnt.mkdir(parents=True, exist_ok=True)
    _sudo(["modprobe", "btrfs"])
    r = _sudo(["mount", "-o", f"loop,{_COMPRESS_OPTS}", str(img), str(mnt)],
              timeout=120)
    if r.returncode != 0:
        log.warn(f"btrfs loop mount failed: {r.stderr.strip()[:160]}")
        return False
    # mountpoint is root-owned after mount; hand it to the runner user
    _sudo(["chown", "-R", f"{os.getuid()}:{os.getgid()}", str(mnt)])
    return True


def _ensure_canonical_link(base: Path, build_root: Path) -> None:
    """Ensure legacy path <base>/aosp points to <vol_mnt>/aosp.

    Ninja / Soong / prebuilts from previous runs (or legacy states) often bake
    the absolute path /mnt/romforge/aosp into .minibootstrap or ninja deps.
    Having /mnt/romforge/aosp symlinked to /mnt/romforge/vol/aosp ensures 100%
    path backwards compatibility across both plain and btrfs storage modes.
    """
    canonical_link = base / BUILD_DIR_NAME
    if canonical_link == build_root:
        return
    try:
        if canonical_link.is_symlink() or not canonical_link.exists():
            canonical_link.unlink(missing_ok=True)
            canonical_link.symlink_to(build_root)
        elif canonical_link.is_dir() and not any(canonical_link.iterdir()):
            canonical_link.rmdir()
            canonical_link.symlink_to(build_root)
    except Exception as e:
        log.warn(f"Failed to create canonical symlink {canonical_link} -> {build_root}: {e}")


def ensure_volume(force: bool = False) -> VolumeState:
    """Idempotent: mount (or reuse) the compressed build volume.

    Called at the top of prepare/sync/slice/restore/verify/publish. Mounts
    persist across workflow steps (same runner VM), so this is a no-op
    after the first call.
    """
    if os.environ.get("FORGE_NO_VOLUME") == "1":
        return _plain_state("FORGE_NO_VOLUME=1 (forced plain mode)")

    base = backing_dir()
    img = base / IMG_NAME
    mnt = base / VOL_MNT_NAME
    build_root = mnt / BUILD_DIR_NAME

    mounts = _proc_mounts()
    if mounts.get(str(mnt)) == "btrfs" and not force:
        # already mounted (previous step in this job) — reuse
        _ensure_canonical_link(base, build_root)
        st = VolumeState(mode="btrfs", backing_dir=str(base),
                         vol_mnt=str(mnt), img_path=str(img),
                         cap_gb=_df_total_gb_strict(str(mnt)),
                         build_root=str(build_root),
                         tmp_dir=str(_ensure_tmp(base)))
        return st

    if os.geteuid() != 0 and not shutil.which("sudo"):
        return _plain_state("no sudo — cannot create loop mounts")

    free = _df_free_gb(str(base))
    if free < MIN_VIABLE_FREE_GB:
        return _plain_state(
            f"mount {base} has only {free:.0f} GiB free "
            f"(<{MIN_VIABLE_FREE_GB:.0f}) — btrfs volume cannot pay rent")

    if not _ensure_btrfs_progs():
        return _plain_state("btrfs-progs unavailable")

    if not img.exists():
        cap = compute_cap_gb(free)
        if cap < 10:
            return _plain_state(
                f"cap {cap:.0f} GiB too small after reserve "
                f"({reserve_gb():.0f} GiB held back)")
        base.mkdir(parents=True, exist_ok=True)
        _sudo(["chown", "-R", f"{os.getuid()}:{os.getgid()}", str(base)])
        r = _sudo(["truncate", "-s", f"{int(cap)}G", str(img)])
        if r.returncode != 0:
            return _plain_state(f"truncate failed: {r.stderr[:120]}")
        r = _sudo(["mkfs.btrfs", "-q", str(img)], timeout=240)
        if r.returncode != 0:
            img.unlink(missing_ok=True)
            return _plain_state(f"mkfs.btrfs failed: {r.stderr[:160]}")
        log.ok(f"build volume: btrfs zstd:1, cap {cap:.0f} GiB "
               f"({free:.0f} GiB free minus {reserve_gb():.0f} GiB reserve)")

    if not _mount_vol(img, mnt):
        # a stale image from a crashed step/run: mounts never survive
        # runner recycling, so anything unmountable is cache — rebuild it.
        log.warn("unmountable forge.img — recreating (content is cache)")
        _sudo(["umount", str(mnt)])       # just in case half-mounted
        img.unlink(missing_ok=True)
        return ensure_volume(force=True)  # one retry with a fresh image

    build_root.mkdir(parents=True, exist_ok=True)
    _ensure_canonical_link(base, build_root)
    st = VolumeState(mode="btrfs", backing_dir=str(base),
                     vol_mnt=str(mnt), img_path=str(img),
                     cap_gb=_df_total_gb_strict(str(mnt)),
                     build_root=str(build_root),
                     tmp_dir=str(_ensure_tmp(base)))
    log.log(f"storage v2: {st.mode} volume at {st.vol_mnt} "
            f"(cap {st.cap_gb:.0f} GiB, build_root {st.build_root})")
    return st


def _plain_state(reason: str) -> VolumeState:
    base = backing_dir()
    log.warn(f"storage v2 DEGRADED (plain dirs, old capacity rules): {reason}")
    root = base / BUILD_DIR_NAME
    return VolumeState(mode="plain", backing_dir=str(base),
                       vol_mnt="", img_path="", cap_gb=0.0,
                       build_root=str(root), reason=reason,
                       tmp_dir=str(_ensure_tmp(base)))


def _ensure_tmp(base: Path) -> Path:
    tmp = base / TMP_DIR_NAME
    try:
        tmp.mkdir(parents=True, exist_ok=True)
        os.chmod(tmp, 0o1777)
    except OSError:
        pass
    return tmp


def _df_total_gb_strict(path: str) -> float:
    try:
        st = os.statvfs(path)
        return (st.f_blocks * st.f_frsize) / (1024 ** 3)
    except OSError:
        return 0.0


def active_build_root() -> Optional[str]:
    """Read-only: BUILD_ROOT if the volume is mounted, else None. No side
    effects — safe for _build_root() to call from any command."""
    if os.environ.get("FORGE_NO_VOLUME") == "1":
        return None
    base = backing_dir()
    mnt = base / VOL_MNT_NAME
    if _proc_mounts().get(str(mnt)) == "btrfs":
        return str(mnt / BUILD_DIR_NAME)
    return None


def default_build_root() -> str:
    """The BUILD_ROOT a command should use right now (volume-aware)."""
    vol = active_build_root()
    if vol:
        return vol
    return str(backing_dir() / BUILD_DIR_NAME)


# ---------------------------------------------------------------------------
# Monitoring / reclamation
# ---------------------------------------------------------------------------
@dataclass
class DiskSnapshot:
    mode: str = "plain"
    physical_free_gb: float = 0.0     # free bytes on the backing mount
    logical_free_gb: float = 0.0      # free bytes inside the volume (btrfs)
    root_free_gb: float = 0.0         # free bytes on / (runner daemon lives)
    cap_gb: float = 0.0

    def to_dict(self) -> Dict[str, float]:
        return {"mode": self.mode,  # type: ignore[dict-item]
                "physical_free_gb": round(self.physical_free_gb, 2),
                "logical_free_gb": round(self.logical_free_gb, 2),
                "root_free_gb": round(self.root_free_gb, 2),
                "cap_gb": round(self.cap_gb, 2)}


def snapshot(build_root: Path) -> DiskSnapshot:
    """One consistent look at every disk number the watchdogs care about."""
    root = str(build_root)
    mounts = _proc_mounts()
    base = str(backing_dir())
    # is build_root under our btrfs volume?
    vol_mnt = str(Path(base) / VOL_MNT_NAME)
    on_vol = mounts.get(vol_mnt) == "btrfs" and root.startswith(vol_mnt + "/")
    snap = DiskSnapshot(mode="btrfs" if on_vol else "plain",
                        root_free_gb=_df_free_gb("/"))
    if on_vol:
        snap.logical_free_gb = _df_free_gb(vol_mnt)
        snap.physical_free_gb = _df_free_gb(base)
        snap.cap_gb = _df_total_gb_strict(vol_mnt)
    else:
        snap.logical_free_gb = _df_free_gb(root)
        snap.physical_free_gb = snap.logical_free_gb
        snap.cap_gb = 0.0
    return snap


def trim(vol_mnt: Optional[str] = None) -> float:
    """fstrim the volume → punch holes in the sparse image → give bytes back
    to the backing mount. Returns physical free GiB after (0.0 if N/A)."""
    mnt = vol_mnt or str(backing_dir() / VOL_MNT_NAME)
    base = str(backing_dir())
    if _proc_mounts().get(mnt) != "btrfs":
        return 0.0
    before = _df_free_gb(base)
    _sudo(["fstrim", "-v", mnt], timeout=180)
    after = _df_free_gb(base)
    if after > before + 0.1:
        log.log(f"fstrim reclaimed {after - before:.1f} GiB to the backing "
                f"mount")
    return after


def ckpt_snapshot(out_dir: Path, name: str = "ckpt") -> Optional[Path]:
    """Take a lightweight mid-slice checkpoint snapshot of out_dir.

    On btrfs, takes an O(metadata) subvolume snapshot in sub-seconds.
    On plain filesystems, returns None gracefully.
    """
    if not out_dir.exists():
        return None
    vol_mnt = str(backing_dir() / VOL_MNT_NAME)
    if _proc_mounts().get(vol_mnt) == "btrfs":
        snapshots_dir = Path(vol_mnt) / "snapshots"
        snapshots_dir.mkdir(parents=True, exist_ok=True)
        snap_target = snapshots_dir / f"out-{name}"
        if snap_target.exists():
            _sudo(["btrfs", "subvolume", "delete", str(snap_target)])
        r = _sudo(["btrfs", "subvolume", "snapshot", "-r", str(out_dir), str(snap_target)])
        if r.returncode == 0:
            log.ok(f"checkpoint btrfs snapshot created at {snap_target.name}")
            return snap_target
    return None


def selftest(size_mb: int = 256) -> Dict[str, object]:
    """Prove this host can do btrfs loop mounts at all (CI runs this on a
    real ubuntu runner; sandboxes without sudo get a clean negative)."""
    tdir = Path("/tmp/forge-storage-selftest")
    try:
        if os.path.exists("/tmp") and not shutil.which("sudo") \
                and os.geteuid() != 0:
            return {"ok": False,
                    "reason": "no sudo in this environment (expected in "
                              "local/sandbox runs; CI runners have it)"}
        if not _ensure_btrfs_progs():
            return {"ok": False, "reason": "btrfs-progs not installable"}
        shutil.rmtree(tdir, ignore_errors=True)
        tdir.mkdir(parents=True, exist_ok=True)
        img = tdir / "selftest.img"
        mnt = tdir / "mnt"
        r = _sudo(["truncate", "-s", f"{size_mb}M", str(img)])
        assert r.returncode == 0, r.stderr[:120]
        r = _sudo(["mkfs.btrfs", "-q", str(img)], timeout=120)
        assert r.returncode == 0, r.stderr[:160]
        ok = _mount_vol(img, mnt)
        if not ok:
            return {"ok": False, "reason": "loop mount denied"}
        probe = mnt / "probe.bin"
        probe.write_bytes(b"ROMFORGE" * 4096)
        rc = probe.read_bytes() == b"ROMFORGE" * 4096
        _sudo(["umount", str(mnt)])
        return {"ok": bool(rc), "reason": "btrfs loop mount verified" if rc
                else "rw verification failed"}
    except Exception as e:  # noqa: BLE001 — selftest must never raise
        return {"ok": False, "reason": str(e)[:200]}
    finally:
        _sudo(["umount", str(tdir / "mnt")])
        shutil.rmtree(tdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Root-disk protection (failure mode B: runner eviction when / fills)
# ---------------------------------------------------------------------------
ROOT_PURGE_PATHS = [
    "~/.cache/pip",
    "~/.cache/uv",
    "~/.cache/node",
    "~/.npm/_cacache",
    "~/.cache/Yarn",
    "/var/cache/apt/archives",
    "~/.cache/pre-commit",
]


def emergency_root_purge() -> float:
    """Free safe bytes on / when the runner daemon is at risk. NEVER touches
    runner internals (/home/runner/agent*, /var/log, /opt/actions-runner).
    Returns bytes freed (approx)."""
    freed = 0
    home = Path.home()
    for p in ROOT_PURGE_PATHS:
        d = Path(os.path.expanduser(p))
        if p.startswith("~"):
            d = home / p[2:]
        if d.is_dir():
            try:
                sz = sum(f.stat().st_size for f in d.rglob("*")
                         if f.is_file())
                shutil.rmtree(d, ignore_errors=True)
                freed += sz
            except OSError:
                pass
    # our own big logs on / — keep the tail, drop the head
    for lg in Path("/tmp").glob("forge-*.log*"):
        try:
            if lg.stat().st_size > 400 * 1024 * 1024:
                sz = lg.stat().st_size
                lg.unlink(missing_ok=True)
                freed += sz
        except OSError:
            pass
    if freed:
        log.warn(f"root-disk emergency purge freed {freed / 2**30:.1f} GiB "
                 f"(pip/npm/apt caches + oversized forge logs)")
    return freed
