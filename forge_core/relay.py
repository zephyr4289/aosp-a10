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

# cheap-to-regenerate fat that never travels in the relay
STATE_EXCLUDES = [
    "out/target/product/*/symbols",
    "out/target/product/*/obj/*/oat_x86*",
    "out/target/product/*/*.img.new",
    "out/soong/.temp-dir*",
    "out/.reclaim_tmp",
]


class RelayError(Exception):
    pass


def bank(build_root: Path, store, tag: str, key: str, slice_no: int,
         notes: str = "") -> int:
    """Pack out/ into release tag `tag`. Returns parts shipped."""
    if not (build_root / "out").exists():
        log.warn("no out/ to bank — skipping relay push")
        return 0
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
                               excludes=STATE_EXCLUDES)
        store.upload_file(tag, sums_tmp, "SHA256SUMS")
        sums_tmp.unlink(missing_ok=True)
    else:  # fs store / stage mode
        staging = build_root.parent / ".forge-parts-state"
        parts = chunker.pack(build_root, "out", staging, "out",
                             excludes=STATE_EXCLUDES)
        store.upload(tag, [staging / "SHA256SUMS", *parts])
        for p in parts + [staging / "SHA256SUMS"]:
            p.unlink(missing_ok=True)
        n = len(parts)
    return n


def restore(build_root: Path, store, tag: str) -> bool:
    """Unpack a banked out/ back into the tree. False = tag absent."""
    if not store.exists(tag):
        return False
    tmp = build_root.parent / ".forge-dl-state"
    if tmp.exists():
        shutil.rmtree(tmp)
    try:
        store.download(tag, "out.part.*", tmp)
    except Exception as e:
        log.warn(f"downloading state parts from {tag} failed: {e}")
        return False
    try:
        store.download(tag, "SHA256SUMS", tmp)
    except Exception:
        pass
    out_dir = build_root / "out"
    if out_dir.exists():
        # merge semantics: restored (older) state wins on mtime via tar
        # extraction overwriting; ninja re-runs whatever is stale. In
        # practice restore happens on a fresh runner with no out/.
        shutil.rmtree(out_dir)
    try:
        chunker.unpack(tmp, "out", out_dir, strip=True)
    except Exception as e:
        log.warn(f"unpacking out state from {tag} failed: {e}")
        return False
    shutil.rmtree(tmp, ignore_errors=True)
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
        text = build_log.read_text(encoding="utf-8", errors="replace")
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
