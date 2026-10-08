"""The out/ exact-resume relay — ROMForge's core fix.

Upstream banked ONLY ccache between slices, so every slice re-paid:
soong/kati analysis (~20 min) + every Java/Kotlin/proto/dex step + every
packaging step. ccache only accelerates C/C++ compiles, which is a minority
of AOSP work — hence per-slice wasted time grew LINEARLY with the number
of already-done modules. That is precisely the "redo grows with slice
number" pathology.

The fix: persist the ENTIRE ninja build state (out/) — .ninja_log + mtimes
make resume EXACT: the next slice's ninja sees every finished output with a
valid mtime and skips it, regardless of language. Only genuinely-new work
runs. We also parse .ninja_log to report progress + ETA, and EXCLUDE
rebuild-cheap fat from the relay (unstripped symbols dirs) to keep state
small — if ninja misses them it just re-runs the cheap copy rules.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import chunker, log
from . import env as fenv
from . import storage

# cheap-to-regenerate fat that never travels in the relay
STATE_EXCLUDES = [
    "out/target/product/*/obj/*/oat_x86*",
    "out/target/product/*/*.img.new",
    "out/target/product/*/symbols*",
    "out/target/product/*/*/symbols*",
    "out/soong/.temp-dir*",
    "out/soong/.temp*",
    "out/soong/.temp",
    "out/.reclaim_tmp",
]

# Minimal product-state exclusions for verify & publish (drops huge obj/ intermediate trees)
PRODUCT_STATE_EXCLUDES = [
    *STATE_EXCLUDES,
    "out/target/product/*/obj*",
]


class RelayError(Exception):
    pass


# ---- pre-bank cleanup decision (pure — offline-testable) --------------------
def pre_bank_actions(free_gb: float, protect_source: bool,
                     floor_gb: float = 4.0) -> List[str]:
    """Decide how to make room for banking. Ordered actions.

    The runs #30/#34-#36 deadlock was this function (in spirit): free < 15
    -> delete the source tree -> next slot re-downloads 35 GiB -> builds 30
    s -> hits the floor again -> repeat forever. Volume mode ('protect_
    source=True') must NEVER take the delete-source action — space comes
    from fstrim + the reclaim ladder inside out/ instead, and if that is
    not enough the slice is classified 'capacity' and the conveyor stops
    the campaign honestly.
    """
    acts = ["purge-tmp"]
    if free_gb < floor_gb:
        acts.append("reclaim-ladder")
        if free_gb < floor_gb / 2:
            acts.append("fstrim")
    if free_gb < 2.0:
        if protect_source:
            acts.append("stop:capacity")   # NEVER delete source in volume mode
        else:
            acts.append("delete-source")   # degraded/plain last resort
    return acts


def pre_bank_cleanup(build_root: Path) -> None:
    """Free disk space before packing out/ so split --filter never hits
    ENOSPC. Volume mode: fstrim + ladder only — the source tree is sacred
    (deleting it is the storage-deadlock, not a remedy)."""
    for tmp in ("/tmp", "/var/tmp"):
        try:
            for child in Path(tmp).glob("*"):
                if child.is_file():
                    child.unlink(missing_ok=True)
        except Exception:
            pass
    try:
        vol_root = storage.active_build_root()
        protect = bool(vol_root) and str(build_root).startswith(vol_root)
        free = fenv._df_free_gb(str(build_root))
        acts = pre_bank_actions(free, protect)
        if "reclaim-ladder" in acts:
            fenv.reclaim_ladder(build_root, want_gb=8.0)
        if "fstrim" in acts or protect:
            # volume mode always trims before a bank: deletes from this
            # slice + the ladder punch holes in the sparse image, giving
            # the backing mount its bytes back for the next slice's stream
            storage.trim()
        if "delete-source" in acts:
            log.warn("DEGRADED (plain) mode: pre-bank cleanup must drop the "
                     "source tree — expect a re-download next slot. Fix the "
                     "volume (see storage.py) to stop this.")
            for child in build_root.iterdir():
                if child.name != "out":
                    if child.is_dir():
                        shutil.rmtree(child, ignore_errors=True)
                    else:
                        child.unlink(missing_ok=True)
            log.ok(f"pre-bank cleanup freed workspace to "
                   f"{fenv._df_free_gb(str(build_root)):.1f} GB")
        if "stop:capacity" in acts:
            log.warn("volume out of space before banking — continuing "
                     "(streaming sink needs no staging); the engine's "
                     "capacity classification will stop re-dispatch")
    except Exception:  # noqa: BLE001 — cleanup must never break banking
        pass


def bank(build_root: Path, store, tag: str, key: str, slice_no: int,
         notes: str = "") -> int:
    """Pack out/ into release tag `tag`. Returns parts shipped."""
    if not (build_root / "out").exists():
        log.warn("no out/ to bank — skipping relay push")
        return 0
    pre_bank_cleanup(build_root)
    if store.exists(tag):
        try:
            store.reset(tag, f"out-state {key} slice {slice_no}",
                        notes or "Exact-resume ninja state. GC'd automatically.")
        except Exception:
            pass
    else:
        store.create(tag, f"out-state {key} slice {slice_no}",
                     notes or "Exact-resume ninja state. GC'd automatically.")
    try:
        sink = store.sink_command(tag)
    except Exception:  # noqa: BLE001 — unsupported backend degrades to stage
        sink = None
    if not sink:
        sink = None
    sums_tmp = build_root / ".forge_sums.tmp"
    if sink:
        n = chunker.stream_pack(build_root, "out", "out", sink, sums_tmp,
                               excludes=STATE_EXCLUDES, level=1, extra_args=["--long"])
        store.upload_file(tag, sums_tmp, "SHA256SUMS")
        sums_tmp.unlink(missing_ok=True)
    else:  # fs store / stage mode
        staging = build_root.parent / ".forge-parts-state"
        parts = chunker.pack(build_root, "out", staging, "out",
                             excludes=STATE_EXCLUDES, level=1, extra_args=["--long"])
        store.upload(tag, [staging / "SHA256SUMS", *parts])
        for p in parts + [staging / "SHA256SUMS"]:
            p.unlink(missing_ok=True)
        n = len(parts)
    return n


def bank_product_state(build_root: Path, store, tag: str, key: str,
                       notes: str = "") -> int:
    """Pack lightweight product-state (ROM zip, partition images, host tools) for verify/publish.

    Excludes huge intermediate obj/ trees and symbols, reducing state size from 35-50 GB
    down to ~2-4 GB, cutting gate/publish restore time from 20-30 min to under 1 min.
    """
    if not (build_root / "out").exists():
        log.warn("no out/ to bank — skipping product-state push")
        return 0
    pre_bank_cleanup(build_root)
    if store.exists(tag):
        try:
            store.reset(tag, f"product-state {key}",
                        notes or "Lightweight product state for verification and publication.")
        except Exception:
            pass
    else:
        store.create(tag, f"product-state {key}",
                     notes or "Lightweight product state for verification and publication.")
    try:
        sink = store.sink_command(tag)
    except Exception:  # noqa: BLE001
        sink = None
    sums_tmp = build_root / ".forge_sums_product.tmp"
    if sink:
        n = chunker.stream_pack(build_root, "out", "out", sink, sums_tmp,
                               excludes=PRODUCT_STATE_EXCLUDES, level=1, extra_args=["--long"])
        store.upload_file(tag, sums_tmp, "SHA256SUMS")
        sums_tmp.unlink(missing_ok=True)
    else:  # fs store / stage mode
        staging = build_root.parent / ".forge-parts-product"
        parts = chunker.pack(build_root, "out", staging, "out",
                             excludes=PRODUCT_STATE_EXCLUDES, level=1, extra_args=["--long"])
        store.upload(tag, [staging / "SHA256SUMS", *parts])
        for p in parts + [staging / "SHA256SUMS"]:
            p.unlink(missing_ok=True)
        n = len(parts)
    log.ok(f"banked product state {tag} ({n} parts)")
    return n


def restore(build_root: Path, store, tag: str) -> bool:
    """Unpack a banked out/ back into the tree. False = tag absent."""
    if not store.exists(tag):
        return False
    out_dir = build_root / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    if not out_dir.is_symlink():
        for child in out_dir.iterdir():
            try:
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
            except OSError:
                pass
    dest = out_dir.resolve() if out_dir.is_symlink() else out_dir
    try:
        chunker.unpack_from_store(store, tag, "out", dest, strip=True)
    except Exception as e:
        log.warn(f"unpacking out state from {tag} failed: {e}")
        return False
    # Clean stale temporary directories inside out/soong
    for soong_stale in (".temp",):
        p = dest / "soong" / soong_stale
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)
    # Ensure host tool binaries retain execution bit
    for b_dir in (dest / "soong" / "host" / "linux-x86" / "bin",
                 dest / "host" / "linux-x86" / "bin"):
        if b_dir.exists():
            for f in b_dir.glob("*"):
                if f.is_file():
                    try:
                        f.chmod(f.stat().st_mode | 0o755)
                    except OSError:
                        pass
    log.ok(f"out/ state restored from {tag}")
    return True


def merge(build_root: Path, donor_out: Path) -> int:
    """rsync-merge a turbo partition's out/ into the main out/.

    --ignore-existing keeps the base (system) build's copy of any file both
    built; adds the donor's module outputs the base lacks. Inconsistencies
    self-heal: ninja re-runs stale rules; the hard gate catches anything
    that survives to an image.
    """
    if not donor_out.exists():
        return 0
    r = subprocess.run(["rsync", "-a", "--ignore-existing", "--exclude",
                        "symbols", str(donor_out) + "/", str(build_root / "out") + "/"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RelayError(f"turbo merge failed: {r.stderr[:200]}")
    n = sum(len(list(p.rglob("*"))) for p in donor_out.glob("*"))
    log.ok(f"merged turbo state from {donor_out} (~{n} paths)")
    return n


# ---------------------------------------------------------------------------
# .ninja_log forensics — progress + ETA
# ---------------------------------------------------------------------------
_NINJA_LINE = re.compile(
    r"^\s*(\d+)\s+(\d+)\s+(\d+)\s+(\S+)\s+([0-9a-f]+)")


def ninja_stats(out_dir: Path) -> Dict[str, object]:
    """Aggregate .ninja_log -> {outputs, build_seconds} for progress math."""
    logf = out_dir / ".ninja_log"
    if not logf.exists():
        return {"outputs": 0, "build_seconds": 0.0}
    outputs = 0
    seconds = 0.0
    with open(logf, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.startswith("#") or not line.strip():
                continue
            m = _NINJA_LINE.match(line)
            if not m:
                continue
            outputs += 1
            seconds += (int(m.group(2)) - int(m.group(1))) / 1000.0
    return {"outputs": outputs, "build_seconds": round(seconds, 1)}


def progress_from_log(build_log: Path, last: Dict[str, int]) -> Dict[str, int]:
    """Parse soong/ninja `[ N% done/total ]` lines from the live build log."""
    best = dict(last)
    try:
        if not build_log.exists():
            return best
        size = build_log.stat().st_size
        with open(build_log, "r", encoding="utf-8", errors="replace") as fh:
            if size > 2 * 1024 * 1024:
                fh.seek(size - 2 * 1024 * 1024)
            text = fh.read()
    except OSError:
        return best
    for m in re.finditer(r"\[\s*(\d+)%\s*(\d+)/(\d+)\s*\]", text):
        best = {"pct": int(m.group(1)), "done": int(m.group(2)),
                "total": int(m.group(3))}
    return best


def eta_minutes(prog: Dict[str, int], elapsed_min: float) -> Optional[float]:
    if not prog or prog.get("pct", 0) <= 2:
        return None
    pct = prog["pct"]
    return elapsed_min * (100 - pct) / pct
