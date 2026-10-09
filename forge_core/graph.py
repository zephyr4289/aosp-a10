"""Graph banking — persist Soong/Blueprint graph across slices and campaigns.

Keyed by (mhash, lunch). Contains:
  - out/soong/build.ninja
  - out/soong/.bootstrap
  - out/soong/.glob
  - out/soong/.minibootstrap
  - out/soong/soong.variables
  - out/soong/soong.environment.used
  - out/combined-*.ninja
  - out/build-*.ninja
  - out/.ninja_log
  - out/.ninja_deps
  - out/fingerprint.json

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

GRAPH_ENTRIES = (
    "build.ninja", ".bootstrap", ".glob", ".minibootstrap",
    "soong.variables", "soong.environment.used", "soong.environment.available"
)
GRAPH_OUT_PATTERNS = (
    "combined-*.ninja", "build-*.ninja", ".ninja_log", ".ninja_deps"
)
GRAPH_FINGERPRINT = "fingerprint.json"


def graph_tag(mhash: str, lunch: str) -> str:
    slug = lunch.replace("_", "-").replace("/", "-")
    return f"graph-{mhash[:16]}-{slug}"


def graph_fingerprint(build_root: Path) -> Dict[str, List[int]]:
    """Generate a lightweight {path: [size, mtime_ns]} fingerprint of the .bp/.mk files."""
    fps: Dict[str, List[int]] = {}
    try:
        for p in build_root.rglob("*.bp"):
            if "out/" in p.as_posix():
                continue
            try:
                st = p.stat()
                rel = p.relative_to(build_root).as_posix()
                fps[rel] = [st.st_size, getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))]
            except OSError:
                pass
        for p in build_root.rglob("*.mk"):
            if "out/" in p.as_posix():
                continue
            try:
                st = p.stat()
                rel = p.relative_to(build_root).as_posix()
                fps[rel] = [st.st_size, getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))]
            except OSError:
                pass
    except Exception:
        pass
    return fps


def bank_graph(build_root: Path, store, mhash: str, lunch: str) -> bool:
    """Bank the generated complete ninja graph into store keyed by (mhash, lunch)."""
    out_dir = build_root / "out"
    soong_dir = out_dir / "soong"
    if not (soong_dir / "build.ninja").exists():
        return False
    tag = graph_tag(mhash, lunch)
    if store.exists(tag):
        return True  # immutable graph already banked

    staging = build_root.parent / f".forge-graph-stage-{mhash[:8]}"
    shutil.rmtree(staging, ignore_errors=True)
    out_tree = staging / "soong_graph" / "out"
    graph_tree = out_tree / "soong"
    graph_tree.mkdir(parents=True, exist_ok=True)

    # 1. Copy relevant out/soong/ entries
    for entry in GRAPH_ENTRIES:
        src = soong_dir / entry
        dst = graph_tree / entry
        if src.is_dir():
            shutil.copytree(src, dst, symlinks=True, ignore_dangling_symlinks=True)
        elif src.is_file():
            shutil.copy2(src, dst)

    # 2. Copy top-level out/ files needed for direct ninja bypass
    for pat in GRAPH_OUT_PATTERNS:
        for match in out_dir.glob(pat):
            if match.is_file():
                dst = out_tree / match.name
                shutil.copy2(match, dst)

    # 3. Generate and record fingerprint
    fp = graph_fingerprint(build_root)
    (out_tree / GRAPH_FINGERPRINT).write_text(json.dumps(fp), encoding="utf-8")

    # 4. Generate targets list if ninja is present
    ninja_bin = build_root / "prebuilts/build-tools/linux-x86/bin/ninja"
    ninja_cmd = str(ninja_bin) if ninja_bin.exists() else (shutil.which("ninja") or "")
    combined = sorted(out_dir.glob("combined-*.ninja"))
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
    log.ok(f"banked complete ninja graph to {tag} ({n} parts)")
    return True


def restore_graph(build_root: Path, store, mhash: str, lunch: str) -> bool:
    """Restore banked complete ninja graph if available."""
    tag = graph_tag(mhash, lunch)
    if not store.exists(tag):
        return False
    dest = build_root / "out"
    dest.mkdir(parents=True, exist_ok=True)
    try:
        chunker.unpack_from_store(store, tag, "graph", dest, strip=True)
        log.ok(f"restored banked ninja graph from {tag}")
        return True
    except Exception as e:
        log.warn(f"restoring graph from {tag} failed: {e}")
        return False
