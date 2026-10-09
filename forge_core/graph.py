"""Graph banking — persist Soong/Blueprint graph across slices and campaigns.

Keyed by (mhash, lunch). Contains (discovered, not hardcoded — R1 fix):
  - out/soong/build*.ninja          (build.ninja OR build.<product>.ninja)
  - out/soong/soong*.variables      (classic OR product-suffixed)
  - out/soong/soong.environment*    (available + used.<product>.build)
  - out/soong/.bootstrap, .glob, .minibootstrap
  - out/combined*.ninja, out/build-*.ninja
  - out/.ninja_log, out/.ninja_deps
  - out/.module_paths/Android.bp.list (when present)
  - fingerprint + targets.txt

Persisting the complete parsed graph (50-300 MiB) allows subsequent slots
and turbo partition prewarms to execute ninja directly, completely bypassing
the 30-34 GiB Soong AST parsing memory trap and saving 30+ minutes per slot.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

from . import chunker, log

# Globs INSIDE out/soong/ — bank whatever actually exists (per-target or classic)
SOONG_GRAPH_GLOBS = (
    "build*.ninja",          # build.ninja | build.<product>.ninja
    "soong*.variables",      # soong.variables | soong.<product>.variables
    "soong.environment*",    # soong.environment.used[.<product>.build]
)
SOONG_GRAPH_DIRS = (".bootstrap", ".glob", ".minibootstrap")
# Globs in the out/ ROOT — the direct-ninja execution surface
GRAPH_OUT_GLOBS = (
    "combined*.ninja", "build-*.ninja", ".ninja_log", ".ninja_deps"
)
# Extra graph-adjacent files (exact names, existence-checked)
GRAPH_EXTRA_FILES = ((".module_paths", "Android.bp.list"),)
GRAPH_FINGERPRINT = "fingerprint.json"

# Retained for API compat with tests/configs that import the old names
GRAPH_ENTRIES = tuple(SOONG_GRAPH_GLOBS) + SOONG_GRAPH_DIRS
GRAPH_OUT_PATTERNS = GRAPH_OUT_GLOBS


def graph_tag(mhash: str, lunch: str) -> str:
    slug = lunch.replace("_", "-").replace("/", "-")
    return f"graph-{mhash[:16]}-{slug}"


def graph_fingerprint(build_root: Path, deep: bool = False) -> Dict[str, List]:
    """Fingerprint the .bp/.mk inputs: {path: [size, mtime_ns(, md5)]}.

    mtime is the fast pre-filter; the content digest (deep=True, md5 over
    files <= 4 MiB, ~30-40 s for the full ~5 GB text set) is the truth —
    P0-4 defense-in-depth: [size, mtime_ns] alone lies when a slot's
    deliberate mutations re-stamp byte-identical files (P0-3 fixes the
    mtimes; this verifies the bytes)."""
    import hashlib
    fps: Dict[str, List] = {}
    for pat in ("*.bp", "*.mk"):
        try:
            for p in build_root.rglob(pat):
                if "out/" in p.as_posix():
                    continue
                try:
                    st = p.stat()
                    rel = p.relative_to(build_root).as_posix()
                    entry: List = [st.st_size,
                                   getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))]
                    if deep and st.st_size <= 4 * 1024 * 1024:
                        h = hashlib.md5()
                        with open(p, "rb") as f:
                            for blk in iter(lambda: f.read(1024 * 1024), b""):
                                h.update(blk)
                        entry.append(h.hexdigest())
                    fps[rel] = entry
                except OSError:
                    pass
        except Exception:
            pass
    return fps


def bank_graph(build_root: Path, store, mhash: str, lunch: str) -> bool:
    """Bank the generated complete ninja graph into store keyed by (mhash, lunch).

    v2 (OVERHAUL.md P0-2): glob-discovery instead of the phantom classic
    names — the keystone check is any(soong/build*.ninja), so per-target
    graphs (build.<product>.ninja) bank exactly like classic ones. Every
    refusal is loud (R6)."""
    out_dir = build_root / "out"
    soong_dir = out_dir / "soong"
    if not any(soong_dir.glob("build*.ninja")):
        found = [p.name for p in soong_dir.glob("*.ninja")] if soong_dir.exists() else []
        log.warn(f"bank_graph REFUSED: no out/soong/build*.ninja "
                 f"(soong dir has: {found[:8]}) — nothing to bank")
        return False
    tag = graph_tag(mhash, lunch)
    if store.exists(tag):
        log.log(f"graph bank {tag} already exists — immutable, skipping")
        return True  # immutable graph already banked

    staging = build_root.parent / f".forge-graph-stage-{mhash[:8]}"
    shutil.rmtree(staging, ignore_errors=True)
    out_tree = staging / "soong_graph" / "out"
    graph_tree = out_tree / "soong"
    graph_tree.mkdir(parents=True, exist_ok=True)

    # 1. Copy whatever graph files actually exist in out/soong/ (globs)
    copied = 0
    for pat in SOONG_GRAPH_GLOBS:
        for match in sorted(soong_dir.glob(pat)):
            if match.is_file():
                shutil.copy2(match, graph_tree / match.name)
                copied += 1
    for d in SOONG_GRAPH_DIRS:
        src = soong_dir / d
        if src.is_dir():
            shutil.copytree(src, graph_tree / d, symlinks=True,
                            ignore_dangling_symlinks=True)
            copied += 1
    # module paths list (lunch -l input for soong_build)
    for rel in GRAPH_EXTRA_FILES:
        src = out_dir.joinpath(*rel)
        if src.is_file():
            dst = out_tree.joinpath(*rel)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied += 1

    # 2. Copy top-level out/ files needed for direct ninja bypass (globs)
    for pat in GRAPH_OUT_GLOBS:
        for match in sorted(out_dir.glob(pat)):
            if match.is_file():
                shutil.copy2(match, out_tree / match.name)
                copied += 1
    if copied == 0:
        log.warn("bank_graph REFUSED: discovery found zero graph files")
        shutil.rmtree(staging, ignore_errors=True)
        return False

    # 3. Generate and record fingerprint
    fp = graph_fingerprint(build_root)
    (out_tree / GRAPH_FINGERPRINT).write_text(json.dumps(fp), encoding="utf-8")

    # 4. Generate targets list if ninja is present (goal lint data — P1-1)
    ninja_bin = build_root / "prebuilts/build-tools/linux-x86/bin/ninja"
    ninja_cmd = str(ninja_bin) if ninja_bin.exists() else (shutil.which("ninja") or "")
    combined = sorted(out_dir.glob("combined*.ninja"))
    if ninja_cmd and combined:
        try:
            r = subprocess.run([ninja_cmd, "-f", str(combined[0]), "-t", "targets", "all"],
                               capture_output=True, text=True, timeout=15)
            if r.returncode == 0 and r.stdout:
                (out_tree / "targets.txt").write_text(r.stdout, encoding="utf-8")
        except Exception:
            pass

    store.create(tag, f"soong-graph {lunch} {mhash[:16]}",
                 "Banked Soong/Blueprint AST & combined ninja graph.")
    try:
        sink = store.sink_command(tag)
    except Exception:
        sink = None

    sums_tmp = staging / ".sums.tmp"
    if sink:
        n = chunker.stream_pack(staging / "soong_graph", "out", "graph", sink, sums_tmp,
                                level=1, extra_args=["--long"])
        store.upload_file(tag, sums_tmp, "SHA256SUMS")
    else:
        parts_dir = staging / "parts"
        parts = chunker.pack(staging / "soong_graph", "out", parts_dir, "graph",
                             level=1, extra_args=["--long"])
        store.upload(tag, [parts_dir / "SHA256SUMS", *parts])
        n = len(parts)

    shutil.rmtree(staging, ignore_errors=True)
    log.ok(f"banked complete ninja graph to {tag} ({n} parts, {copied} graph files)")
    return True


def restore_graph(build_root: Path, store, mhash: str, lunch: str) -> bool:
    """Restore banked complete ninja graph if available — LOUD on every
    failure mode it used to swallow (R6): absent bank, unpack failure."""
    tag = graph_tag(mhash, lunch)
    if not store.exists(tag):
        log.warn(f"graph bank {tag} absent — this slot will mint or run "
                 "full soong")
        return False
    dest = build_root / "out"
    dest.mkdir(parents=True, exist_ok=True)
    try:
        chunker.unpack_from_store(store, tag, "graph", dest, strip=True)
    except Exception as e:
        log.warn(f"graph restore from {tag} failed: {e} — proceeding "
                 "without banked graph")
        return False
    graphs = sorted((dest / "soong").glob("build*.ninja"))
    if not graphs:
        log.warn(f"graph restore from {tag} unpacked but no "
                 "out/soong/build*.ninja present — bank is corrupt")
        return False
    log.ok(f"restored banked ninja graph from {tag} "
           f"(soong graph: {graphs[0].name})")
    return True


def validate_goal(build_root: Path, goal: str) -> Optional[str]:
    """P1-1: validate a ninja goal against the banked graph's targets.txt.

    Returns None if valid (or no targets.txt to check against);
    returns a suggestion string when the goal is unknown."""
    tgt = build_root / "out" / "targets.txt"
    if not tgt.is_file() or not goal:
        return None
    try:
        names = set()
        for line in tgt.read_text(encoding="utf-8", errors="replace").splitlines():
            name = line.split(":", 1)[0].strip()
            if name:
                names.add(name)
        if goal in names:
            return None
        # closest matches for the error message
        import difflib
        cands = difflib.get_close_matches(goal, names, n=3, cutoff=0.5)
        return (f"ninja: unknown target '{goal}' — known spellings: "
                f"{', '.join(cands) if cands else 'no close match'} "
                f"(checked {len(names)} targets in targets.txt)")
    except Exception:
        return None
