"""Graph banking — persist Soong/Blueprint graph across slices and campaigns.

Keyed by (mhash, lunch). Contains:
  - out/soong/build.ninja
  - out/soong/.bootstrap
  - out/soong/.glob
  - out/soong/.minibootstrap
  - out/soong/soong.variables

Persisting the parsed graph (50-300 MiB) saves 8-18 min of AST re-parsing
and 14+ GiB memory spikes on subsequent slots and campaigns.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional

from . import chunker, log


def graph_tag(mhash: str, lunch: str) -> str:
    slug = lunch.replace("_", "-").replace("/", "-")
    return f"graph-{mhash[:16]}-{slug}"


def bank_graph(build_root: Path, store, mhash: str, lunch: str) -> bool:
    """Bank the generated Soong graph into store keyed by (mhash, lunch)."""
    soong_dir = build_root / "out" / "soong"
    if not (soong_dir / "build.ninja").exists():
        return False
    tag = graph_tag(mhash, lunch)
    if store.exists(tag):
        return True  # immutable graph already banked

    staging = build_root.parent / f".forge-graph-stage-{mhash[:8]}"
    shutil.rmtree(staging, ignore_errors=True)
    graph_tree = staging / "soong_graph" / "out" / "soong"
    graph_tree.mkdir(parents=True, exist_ok=True)

    # Copy relevant graph files
    for entry in ("build.ninja", ".bootstrap", ".glob", ".minibootstrap", "soong.variables"):
        src = soong_dir / entry
        dst = graph_tree / entry
        if src.is_dir():
            shutil.copytree(src, dst, symlinks=True, ignore_dangling_symlinks=True)
        elif src.is_file():
            shutil.copy2(src, dst)

    store.create(tag, f"soong-graph {lunch} {mhash[:16]}",
                 "Banked Soong/Blueprint AST & ninja graph.")
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
    log.ok(f"banked Soong graph to {tag} ({n} parts)")
    return True


def restore_graph(build_root: Path, store, mhash: str, lunch: str) -> bool:
    """Restore banked Soong graph if available."""
    tag = graph_tag(mhash, lunch)
    if not store.exists(tag):
        return False
    dest = build_root / "out"
    dest.mkdir(parents=True, exist_ok=True)
    try:
        chunker.unpack_from_store(store, tag, "graph", dest, strip=True)
        log.ok(f"restored banked Soong graph from {tag}")
        return True
    except Exception as e:
        log.warn(f"restoring graph from {tag} failed: {e}")
        return False
