"""Streaming split archives: tar | zstd | 1.9GB parts, sha256 manifest.

Two modes:
  * stage mode   — parts land in a local dir, then get pushed by the store.
  * stream mode  — GNU split's --filter pipes EACH part straight into a shell
                   sink command (e.g. `gh release upload`), so nothing ever
                   piles up on the runner disk. This is the structural fix for
                   the upstream ENOSPC-at-94% failure: upstream staged ~20 GB
                   of parts on the same disk as tree+out.

Fallback chain: zstd missing -> gzip (local test envs); split --filter
missing -> stage mode.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

PART_BYTES = 1900 * 1024 * 1024  # GitHub release assets cap at 2 GiB per file

from . import log


class ChunkerError(Exception):
    pass


def _have_zstd() -> bool:
    return shutil.which("zstd") is not None


def _compress_cmd(level: int = 1) -> List[str]:
    if _have_zstd():
        return ["zstd", "-T0", f"-{level}", "-c"]
    return ["gzip", "-1", "-c"]      # local-test fallback (GHA always has zstd)


def _decompress_cmd() -> List[str]:
    if _have_zstd():
        return ["zstd", "-d", "-T0", "-c"]
    return ["gzip", "-dc"]


def _exclude_args(excludes: List[str]) -> List[str]:
    args: List[str] = []
    for e in excludes:
        args += ["--exclude", e]
    return args


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def _run_pack_pipeline(root: Path, member: str, excludes: List[str],
                       split_args: List[str], split_stdout=subprocess.DEVNULL
                       ) -> "subprocess.Popen[int]":
    cmd = ["tar", "-h", "-C", str(root), "-cf", "-", *_exclude_args(excludes), member]
    comp = _compress_cmd()
    tar_p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    zst_p = subprocess.Popen(comp, stdin=tar_p.stdout, stdout=subprocess.PIPE)
    assert tar_p.stdout is not None
    tar_p.stdout.close()
    split_p = subprocess.Popen(split_args, stdin=zst_p.stdout,
                               stdout=split_stdout)
    assert zst_p.stdout is not None
    zst_p.stdout.close()
    # propagate failures loudly
    rc_zst = zst_p.wait()
    rc_tar = tar_p.wait()
    rc_split = split_p.wait()
    if rc_tar != 0 or rc_zst != 0 or rc_split != 0:
        raise ChunkerError(f"pack pipeline failed tar={rc_tar} comp={rc_zst} "
                           f"split={rc_split}")
    return split_p


def _write_sums(staging: Path, parts: List[Path]) -> None:
    with open(staging / "SHA256SUMS", "w", encoding="ascii") as fh:
        for p in parts:
            fh.write(f"{_hash_file(p)}  {p.name}\n")


def pack(root: Path, member: str, staging: Path, prefix: str,
         excludes: Optional[List[str]] = None, level: int = 3) -> List[Path]:
    """tar+compress `root/member` into `staging/prefix.part.aa...` (stage mode).

    Returns the list of part files; also writes SHA256SUMS into staging.
    """
    staging.mkdir(parents=True, exist_ok=True)
    prefix_path = staging / prefix
    log.log(f"packing {member} (excludes: {excludes or 'none'}) ...")
    _run_pack_pipeline(
        root, member, excludes or [],
        ["split", "-b", str(PART_BYTES), "-", str(prefix_path) + ".part."])
    part_files = sorted(staging.glob(f"{prefix}.part.*"))
    if not part_files:
        raise ChunkerError("pack produced no parts")
    _write_sums(staging, part_files)
    total = sum(p.stat().st_size for p in part_files)
    log.ok(f"packed {member}: {len(part_files)} parts, "
           f"{total / 2**30:.1f} GB compressed")
    return part_files


def stream_pack(root: Path, member: str, prefix: str,
                sink_sh: str, sums_out: Path,
                excludes: Optional[List[str]] = None, level: int = 3) -> int:
    """Zero-staging pack: split --filter hands each part to `sink_sh`.

    `sink_sh` is a shell snippet in which $FILE is the finished part path
    (GNU split exports it). Recommended sink (ReleaseStore):
        gh release upload <tag> "$FILE" --clobber && rm -f "$FILE"
    Retries are built around the sink: 3 attempts, 20s backoff, then the
    pipeline aborts (state stays consistent — the release tag is rebuilt
    from scratch on the next attempt anyway).
    `sums_out` receives the sha256 manifest as parts finish.
    Returns the number of parts shipped.
    """
    rc = subprocess.run(["split", "--filter", "true"],
                        capture_output=True, stdin=subprocess.DEVNULL)
    if rc.returncode != 0:
        raise ChunkerError("GNU split lacks --filter (old coreutils) — "
                           "use stage mode")
    excludes = excludes or []
    sums_out.parent.mkdir(parents=True, exist_ok=True)
    sums_out.write_text("", encoding="ascii")

    # filter: capture stdin into $FILE, record sha, hand to sink (retries).
    # NOTE: split --filter gives the chunk on STDIN — the filter must WRITE
    # $FILE itself. Env-var injection (FORGE_SUMS/FORGE_SINK), never
    # str.format — shell braces make interpolation a foot-gun.
    inner = (
        'set -e; '
        'cat > "$FILE"; '
        'printf \'%s  %s\\n\' "$(sha256sum "$FILE" | cut -d\' \' -f1)" '
        '"$(basename "$FILE")" >> "$FORGE_SUMS"; '
        'n=0; until sh -c "$FORGE_SINK"; do '
        'n=$((n+1)); [ "$n" -ge 3 ] && exit 1; sleep 20; done; '
        'rm -f "$FILE"'
    )

    log.log(f"stream-packing {member} -> sink (zero staging) ...")
    env = dict(os.environ, FORGE_SUMS=str(sums_out), FORGE_SINK=sink_sh)
    work_dir = root.parent
    work_dir.mkdir(parents=True, exist_ok=True)
    _run_pack_pipeline_env(
        root, member, excludes,
        ["split", "-b", str(PART_BYTES), "--filter", inner, "-",
         str(prefix) + ".part."], env, cwd=work_dir)
    n = sum(1 for line in sums_out.read_text().splitlines() if line.strip())
    log.ok(f"stream-packed {member}: {n} parts shipped through sink")
    return n


def _run_pack_pipeline_env(root: Path, member: str, excludes: List[str],
                           split_args: List[str], env: Dict[str, str],
                           cwd: Optional[Path] = None) -> None:
    work_dir = cwd or root.parent
    work_dir.mkdir(parents=True, exist_ok=True)
    cmd = ["tar", "-C", str(root), "-cf", "-", *_exclude_args(excludes), member]
    comp = _compress_cmd()
    tar_p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    zst_p = subprocess.Popen(comp, stdin=tar_p.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             env=env)
    assert tar_p.stdout is not None
    tar_p.stdout.close()
    split_p = subprocess.Popen(split_args, stdin=zst_p.stdout,
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                               cwd=str(work_dir), env=env)
    assert zst_p.stdout is not None
    zst_p.stdout.close()
    _, split_err = split_p.communicate()
    _, zst_err = zst_p.communicate()
    _, tar_err = tar_p.communicate()
    rc_zst = zst_p.returncode
    rc_tar = tar_p.returncode
    rc_split = split_p.returncode
    if (rc_tar not in (0, 1)) or rc_zst != 0 or rc_split != 0:
        err_msg = (f"pack pipeline failed tar={rc_tar} "
                   f"({tar_err.decode(errors='ignore').strip()[:200]}) "
                   f"comp={rc_zst} ({zst_err.decode(errors='ignore').strip()[:200]}) "
                   f"split={rc_split} ({split_err.decode(errors='ignore').strip()[:200]})")
        raise ChunkerError(err_msg)


def verify(parts_dir: Path, prefix: str) -> bool:
    """sha256-verify a part set (SHA256SUMS lives next to the parts)."""
    sums = parts_dir / "SHA256SUMS"
    if not sums.exists():
        log.warn("no SHA256SUMS asset — proceeding unverified "
                 "(stream/zstd checksums still apply)")
        return True
    for line in sums.read_text().splitlines():
        if not line.strip():
            continue
        want, fname = line.split()
        p = parts_dir / fname
        if not p.exists():
            log.warn(f"missing part {fname} for verification")
            return False
        if _hash_file(p) != want:
            log.warn(f"sha256 mismatch on {fname}")
            return False
    return True


def unpack(parts_dir: Path, prefix: str, dest: Path, strip: bool = False) -> None:
    """Verify then stream-decompress parts into dest with instant part reclamation."""
    if not verify(parts_dir, prefix):
        raise ChunkerError(
            f"sha256 mismatch for {prefix} — state corrupted in transfer; "
            f"delete the offending release tag and rerun the job")
    sums = parts_dir / "SHA256SUMS"
    if sums.exists():
        parts = []
        for line in sums.read_text().splitlines():
            if line.strip():
                p = parts_dir / line.split()[1]
                if p.exists():
                    parts.append(p)
    else:
        parts = sorted(parts_dir.glob(f"{prefix}.part.*"))
    if not parts:
        raise ChunkerError(f"no {prefix} parts found in {parts_dir}")
    dest.mkdir(parents=True, exist_ok=True)

    dec = subprocess.Popen(_decompress_cmd(), stdin=subprocess.PIPE,
                           stdout=subprocess.PIPE)
    assert dec.stdin is not None
    assert dec.stdout is not None

    def feeder():
        try:
            for p in parts:
                with open(p, "rb") as fh:
                    shutil.copyfileobj(fh, dec.stdin, length=16 * 1024 * 1024)
                p.unlink(missing_ok=True)
        except Exception:
            pass
        finally:
            try:
                dec.stdin.close()
            except Exception:
                pass

    feed_thread = threading.Thread(target=feeder, daemon=True)
    feed_thread.start()

    tar_cmd = ["tar", "-C", str(dest)]
    if strip:
        tar_cmd += ["--strip-components=1"]
    tar_cmd += ["-xf", "-"]
    tar_res = subprocess.run(tar_cmd, stdin=dec.stdout, capture_output=True)
    rc = tar_res.returncode
    dec_rc = dec.wait()
    feed_thread.join()
    if rc != 0 or dec_rc != 0:
        tar_err = tar_res.stderr.decode(errors="ignore").strip()[:200]
        raise ChunkerError(f"unpack failed tar={rc} ({tar_err}) comp={dec_rc}")
    log.ok(f"unpacked {prefix} -> {dest}")


def unpack_from_store(store, tag: str, prefix: str, dest: Path,
                      strip: bool = False, tmp_dir: Optional[Path] = None) -> None:
    """Stream-download and unpack parts one-by-one directly from store to dest.

    Guarantees that at most ONE 2GB part file exists on disk at any moment,
    eliminating the 20-30GB staging disk overhead completely.
    """
    tmp_dir = tmp_dir or (dest.parent / f".forge-dl-{prefix}-stream")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    sums_file = tmp_dir / "SHA256SUMS"
    expected_sums: Dict[str, str] = {}
    part_names: List[str] = []

    try:
        store.download_file(tag, "SHA256SUMS", sums_file)
        if sums_file.exists():
            for line in sums_file.read_text(encoding="ascii", errors="ignore").splitlines():
                if not line.strip():
                    continue
                tokens = line.split()
                if len(tokens) >= 2 and f"{prefix}.part." in tokens[1]:
                    h, name = tokens[0], tokens[1]
                    expected_sums[name] = h
                    part_names.append(name)
    except Exception:
        pass

    if not part_names:
        assets = store.list_assets(tag)
        part_names = sorted([a for a in assets if f"{prefix}.part." in a])

    if not part_names:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise ChunkerError(f"no {prefix} parts found in {tag}")

    dest.mkdir(parents=True, exist_ok=True)
    dec = subprocess.Popen(_decompress_cmd(), stdin=subprocess.PIPE,
                           stdout=subprocess.PIPE)
    assert dec.stdin is not None
    assert dec.stdout is not None
    feeder_error = []

    def feeder():
        try:
            total_parts = len(part_names)
            for idx, name in enumerate(part_names, 1):
                part_path = tmp_dir / name
                now_str = time.strftime("%H:%M:%S")
                print(f"[{now_str}] [RESTORE] downloading & decompressing {prefix} part {idx}/{total_parts}: {name} ...", flush=True)
                store.download_file(tag, name, part_path)
                if name in expected_sums:
                    got_h = _hash_file(part_path)
                    if got_h != expected_sums[name]:
                        feeder_error.append(
                            f"sha256 mismatch for {name}: expected {expected_sums[name]} got {got_h}")
                        part_path.unlink(missing_ok=True)
                        return
                with open(part_path, "rb") as fh:
                    shutil.copyfileobj(fh, dec.stdin, length=16 * 1024 * 1024)
                part_path.unlink(missing_ok=True)
                print(f"[{time.strftime('%H:%M:%S')}] [RESTORE] unpacked part {idx}/{total_parts} into destination", flush=True)
        except Exception as e:
            feeder_error.append(str(e))
        finally:
            try:
                dec.stdin.close()
            except Exception:
                pass

    feed_thread = threading.Thread(target=feeder, daemon=True)
    feed_thread.start()

    tar_cmd = ["tar", "-C", str(dest)]
    if strip:
        tar_cmd += ["--strip-components=1"]
    tar_cmd += ["-xf", "-"]
    tar_res = subprocess.run(tar_cmd, stdin=dec.stdout, capture_output=True)
    rc = tar_res.returncode
    dec_rc = dec.wait()
    feed_thread.join()
    shutil.rmtree(tmp_dir, ignore_errors=True)

    if feeder_error:
        raise ChunkerError(f"part download failed during unpack: {feeder_error[0]}")
    if rc != 0 or dec_rc != 0:
        tar_err = tar_res.stderr.decode(errors="ignore").strip()[:200]
        raise ChunkerError(f"unpack failed tar={rc} ({tar_err}) comp={dec_rc}")
    log.ok(f"unpacked {prefix} from {tag} -> {dest}")
