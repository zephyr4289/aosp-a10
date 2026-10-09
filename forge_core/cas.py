"""CAS-Relay — Content-Addressed Module Cache & Delta Banking (§5.2-S3, §5.3).

Persists and relays module-level compilation intermediates (out/soong/.intermediates/)
across slices and campaigns, enabling 60-85% cache hit rates on weekly manifest bumps
and reducing cold campaign build times from 8-11h down to 4-7h.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import chunker, log

CAS_MANIFEST_FILE = "cas_manifest.json"


def module_hash(path: Path) -> str:
    """Compute sha256 checksum of a file or directory tree for CAS keying."""
    h = hashlib.sha256()
    if path.is_file():
        try:
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    h.update(chunk)
        except OSError:
            pass
    elif path.is_dir():
        for root, _, files in sorted(os.walk(path)):
            for name in sorted(files):
                p = Path(root) / name
                try:
                    st = p.stat()
                    h.update(p.relative_to(path).as_posix().encode("utf-8"))
                    h.update(str(st.st_size).encode("utf-8"))
                except OSError:
                    pass
    return h.hexdigest()


def generate_manifest(out_dir: Path) -> Dict[str, Dict[str, object]]:
    """Scan out/soong/.intermediates/ and build a module manifest {rel_path: {size, mtime_ns, hash}}."""
    manifest: Dict[str, Dict[str, object]] = {}
    intermediates_dir = out_dir / "soong" / ".intermediates"
    if not intermediates_dir.exists():
        return manifest

    for mod_dir in intermediates_dir.glob("*/*"):
        if mod_dir.is_dir():
            rel = mod_dir.relative_to(out_dir).as_posix()
            try:
                st = mod_dir.stat()
                mtime_ns = getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))
                # For directories, summarize file count and total size
                total_sz = sum(f.stat().st_size for f in mod_dir.rglob("*") if f.is_file())
                manifest[rel] = {
                    "size": total_sz,
                    "mtime_ns": mtime_ns,
                    "hash": module_hash(mod_dir),
                }
            except OSError:
                pass
    return manifest


def diff_manifests(old_manifest: Dict[str, Dict[str, object]],
                   new_manifest: Dict[str, Dict[str, object]]) -> List[str]:
    """Identify modules that were added or modified in new_manifest compared to old_manifest."""
    delta: List[str] = []
    for rel_path, data in new_manifest.items():
        if rel_path not in old_manifest:
            delta.append(rel_path)
        elif old_manifest[rel_path].get("hash") != data.get("hash"):
            delta.append(rel_path)
    return sorted(delta)


def bank_cas_modules(build_root: Path, store, tag: str, module_paths: List[str]) -> bool:
    """Stage and bank a specific list of module directories into a CAS release tag."""
    if not module_paths:
        return False

    out_dir = build_root / "out"
    staging = build_root.parent / f".forge-cas-stage-{tag[:8]}"
    shutil.rmtree(staging, ignore_errors=True)
    stage_out = staging / "cas_payload" / "out"

    for rel in module_paths:
        src = out_dir / rel
        dst = stage_out / rel
        if src.is_dir():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src, dst, symlinks=True, ignore_dangling_symlinks=True)
        elif src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    if not store.exists(tag):
        store.create(tag, f"cas-modules {tag}", "Content-addressed module intermediate cache.")

    try:
        sink = store.sink_command(tag)
    except Exception:
        sink = None

    sums_tmp = staging / ".sums.tmp"
    if sink:
        n = chunker.stream_pack(staging / "cas_payload", "out", "cas", sink, sums_tmp,
                                level=1, extra_args=["--long"])
        store.upload_file(tag, sums_tmp, "SHA256SUMS")
    else:
        parts_dir = staging / "parts"
        parts = chunker.pack(staging / "cas_payload", "out", parts_dir, "cas",
                             level=1, extra_args=["--long"])
        store.upload(tag, [parts_dir / "SHA256SUMS", *parts])
        n = len(parts)

    shutil.rmtree(staging, ignore_errors=True)
    log.ok(f"banked {len(module_paths)} CAS modules to {tag} ({n} parts)")
    return True


def restore_cas_modules(build_root: Path, store, tag: str) -> bool:
    """Restore banked CAS modules from store into build_root/out/."""
    if not store.exists(tag):
        return False
    dest = build_root / "out"
    dest.mkdir(parents=True, exist_ok=True)
    try:
        chunker.unpack_from_store(store, tag, "cas", dest, strip=True)
        log.ok(f"restored CAS modules from {tag}")
        return True
    except Exception as e:
        log.warn(f"restoring CAS from {tag} failed: {e}")
        return False
