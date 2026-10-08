"""Silicon mining — deterministic fast-discard runner selection.

GitHub assigns runners from a shared pool; you cannot request a CPU. But a
matrix of identical candidate shards CAN self-select: each candidate boots,
probes /proc/cpuinfo for ~50 ms, and non-target silicon exits in <1 s
(GitHub then pulls the next runner from the queue). One atomic lock
(a GitHub Release whose tag creation is serialized server-side, or an
os.mkdir for the FsStore backend) guarantees exactly ONE candidate per
slot actually builds; the rest fast-discard. Free on public repos.

Census-informed scoring (HFT-Proj fleet-silicon-census, 2k nodes):
    EPYC 9V45/9V44  Zen5 Turin    14.05%  4.34-4.56 GHz sustained → 100
    Xeon 6973P-C    Granite Rpts   3.20%  4.01-4.20 GHz           →  95
    EPYC 9V74       Zen4 Genoa    16.80%  3.50-3.70 GHz           →  80
    EPYC 7763       Zen3 Milan    55.15%  3.24 GHz baseline       →  40

P(one Zen5+Granite in N candidates) = 1 - 0.83^N: N=12 → 90%, N=20 → 97%.
The scoreboard fallback closes the remaining gap: non-target candidates
WAIT for `wait_s` seconds; if no target silicon has claimed by then, the
best available candidate claims instead. A slice NEVER stalls on the
silicon lottery — it just runs on whatever the pool gave us.

This module imports stdlib + forge_core.log/store ONLY (no PyYAML), so
matrix candidates can run `python3 -m forge_core.mine gate ...` directly
after checkout, before setup-python/pip. That keeps a discarded candidate's
total cost at ~40 s of runner time.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import log
from .store import FsStore, ReleaseStore, Router, StoreError

# (regex on `model name`, score, class label) — first match wins.
CPU_TABLE: List[Tuple[str, int, str]] = [
    (r"EPYC\s+9V4[45]", 100, "Zen5 Turin (4.3-4.6 GHz)"),
    (r"EPYC\s+9V5[0-9]", 100, "Zen5 Turin"),
    (r"(?:Xeon\(R\)?\s+)?(?:Gold|Platinum)?\s*6980P|6973P|6972P|6971P",
     80, "Xeon Granite Rapids (4.0-4.2 GHz)"),
    (r"EPYC\s+9V74", 85, "Zen4c Genoa-X (3.7 GHz)"),
    (r"EPYC\s+9[34567][56]4|EPYC\s+9\d{3}\b", 80, "Zen4 Genoa (3.5-3.7 GHz)"),
    (r"EPYC\s+7[2-9]\d{2}|EPYC\s+7[BR]1\d", 40, "Zen2/Zen3 Rome/Milan"),
    (r"Xeon\(R\)\s+Platinum\s*8\d{3}", 55, "Xeon Ice/Sapphire (3.2-3.6 GHz)"),
    (r"Xeon\(R\)\s+Gold\s*[56]\d{3}", 32, "Xeon Cascade Lake"),
    (r"Xeon\(R\)\s+E5-2\d{3}", 15, "Xeon Broadwell/Haswell"),
]
DEFAULT_MIN_SCORE = 100         # Zen5 Turin class only (AMD EPYC 9V44/9V45)
DEFAULT_WAIT_S = 240
POLL_INTERVAL_S = 10

AVX512_BONUS = 8               # soong/javac/zstd all lean on 512-bit paths


class MineError(Exception):
    pass


def probe(cpuinfo_path: str = "/proc/cpuinfo") -> Dict[str, object]:
    """Parse the local CPU into a score. Pure w.r.t. the filesystem."""
    model, flags, mhz, cores = "", [], 0.0, 0
    try:
        with open(cpuinfo_path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("model name") and not model:
                    model = line.split(":", 1)[1].strip()
                elif line.startswith("flags") and not flags:
                    flags = line.split(":", 1)[1].split()
                elif line.startswith("cpu MHz"):
                    try:
                        mhz = max(mhz, float(line.split(":", 1)[1]))
                    except ValueError:
                        pass
                elif line.startswith("processor"):
                    cores += 1
    except OSError:
        pass
    score, klass = 30, "unknown"
    if model:
        for pat, sc, label in CPU_TABLE:
            if re.search(pat, model):
                score, klass = sc, label
                break
    avx512 = "avx512f" in flags
    if avx512:
        score = min(100, score + AVX512_BONUS)
    return {"model": model or "unknown", "score": score, "class": klass,
            "avx512": avx512, "mhz": round(mhz, 0), "cores": cores}


# ---------------------------------------------------------------------------
# atomic claims
# ---------------------------------------------------------------------------
def claim(store, tag: str, meta: Dict[str, object]) -> bool:
    """Exactly-one-winner claim. True = we hold the lock.

    ReleaseStore: `gh release create` is atomic server-side (the tag is
    created by GitHub during release creation; the loser gets 422
    already_exists). FsStore: os.mkdir is atomic on POSIX.
    """
    notes = json.dumps(meta, sort_keys=True)
    try:
        return store.claim(tag, title=f"silicon lock {tag}",
                           notes=notes)
    except StoreError:
        raise
    except Exception as e:  # noqa: BLE001 — store backends vary
        raise MineError(f"claim on {tag} failed: {e}") from e


# ---------------------------------------------------------------------------
# the gate: probe -> claim-or-wait -> fallback claim
# ---------------------------------------------------------------------------
def gate(store, tag: str, key: str = "", min_score: int = DEFAULT_MIN_SCORE,
         wait_s: int = DEFAULT_WAIT_S,
         poll_s: int = POLL_INTERVAL_S,
         strict: bool = False,
         is_fleet: bool = False,
         sleep_fn=time.sleep, clock_fn=time.time) -> Dict[str, str]:
    """Decide this candidate's role. Returns {role, score, model, reason}.

    role: 'builder'          — claim won, run the slice
          'builder-fallback' — claim won after scoreboard timeout (any CPU)
          'discarded'        — a better (or equal) peer is building (or strict fail)
          'done'             — INDEX already says done, no-op entirely

    Raises MineError when the store is unhealthy (nobody claimed, nobody
    can) so the job goes red instead of silently making no progress.
    """
    if strict or os.environ.get("FORGE_STRICT_MINING") in ("1", "true", "True"):
        strict = True
    if not is_fleet and (
        os.environ.get("FORGE_FLEET_RUNNER") in ("1", "true", "True")
        or os.environ.get("RUNNER_ENVIRONMENT") == "self-hosted"
        or os.environ.get("RUNNER_NAME", "").startswith("romforge")
    ):
        is_fleet = True

    # 0. done short-circuit: when the campaign is finished every candidate
    #    must exit in seconds, without touching the lock.
    if key:
        try:
            t = store.target(key)
            if t.get("done"):
                return {"role": "done", "score": "", "model": "",
                        "reason": "INDEX done=true — slot no-op"}
        except Exception:  # noqa: BLE001 — INDEX is advisory here
            pass

    info = probe()
    meta = {"model": info["model"], "score": info["score"],
            "runner": os.environ.get("RUNNER_NAME", "local"),
            "ts": int(clock_fn())}

    # 0.5. fleet runner fast-path: dedicated self-hosted fleet node claims immediately
    if is_fleet:
        info["score"] = 100
        meta["score"] = 100
        meta["fleet"] = True
        if claim(store, tag, meta):
            log.ok(f"mining: self-hosted fleet runner claimed the slot ({info['model']}, score 100)")
            return {"role": "builder", "score": "100",
                    "model": str(info["model"]),
                    "reason": "self-hosted fleet runner (zero lottery)"}
        return {"role": "discarded", "score": "100",
                "model": str(info["model"]),
                "reason": "fleet runner peer already claimed"}

    # 1. target silicon: claim immediately
    if int(info["score"]) >= min_score:
        if claim(store, tag, meta):
            log.ok(f"mining: TARGET silicon claimed the slot "
                   f"({info['model']}, score {info['score']})")
            return {"role": "builder", "score": str(info["score"]),
                    "model": str(info["model"]),
                    "reason": f"score {info['score']} >= {min_score}"}
        return {"role": "discarded", "score": str(info["score"]),
                "model": str(info["model"]),
                "reason": "a target-silicon peer already claimed"}

    # 2. non-target: scoreboard wait — give target silicon time to land
    deadline = clock_fn() + wait_s
    while clock_fn() < deadline:
        if _exists(store, tag):
            return {"role": "discarded", "score": str(info["score"]),
                    "model": str(info["model"]),
                    "reason": "target silicon claimed during scoreboard wait"}
        sleep_fn(poll_s)
    if _exists(store, tag):
        return {"role": "discarded", "score": str(info["score"]),
                "model": str(info["model"]),
                "reason": "peer claimed at the fallback deadline"}

    # 3. fallback: check strict mode before claiming slower silicon
    if strict:
        log.warn(f"mining [STRICT]: no target silicon (>= {min_score}) appeared in {wait_s}s — "
                 f"rejecting slow silicon ({info['model']}, score {info['score']})")
        return {"role": "discarded", "score": str(info["score"]),
                "model": str(info["model"]),
                "reason": f"strict mining: no target silicon (>= {min_score}) appeared in {wait_s}s"}

    meta["fallback"] = True
    if claim(store, tag, meta):
        log.warn(f"mining: no target silicon appeared in {wait_s}s — "
                 f"fallback claim on {info['model']} "
                 f"(score {info['score']})")
        return {"role": "builder", "score": str(info["score"]),
                "model": str(info["model"]),
                "reason": "fallback claim after scoreboard timeout"}
    return {"role": "discarded", "score": str(info["score"]),
            "model": str(info["model"]),
            "reason": "lost the fallback claim race"}


def _exists(store, tag: str) -> bool:
    try:
        return bool(store.exists(tag))
    except Exception:  # noqa: BLE001 — transient store errors -> keep waiting
        return False


def build_store(repo: Optional[str] = None, fs_root: Optional[str] = None):
    if fs_root:
        return FsStore(Path(fs_root))
    repo = repo or os.environ.get("GITHUB_REPOSITORY", "")
    if repo and (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")):
        return Router(backend="release", repo=repo)
    return Router(backend="fs", fs_root=Path(fs_root or ".forge-store"))


# ---------------------------------------------------------------------------
# CLI: python3 -m forge_core.mine <probe|claim|gate> [..]
# (stdlib-only import chain — safe before pip install)
# ---------------------------------------------------------------------------
def _main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="forge-mine")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_probe = sub.add_parser("probe")
    p_probe.add_argument("--cpuinfo", default="/proc/cpuinfo")

    p_claim = sub.add_parser("claim")
    p_claim.add_argument("--tag", required=True)

    p_gate = sub.add_parser("gate")
    p_gate.add_argument("--tag", required=True)
    p_gate.add_argument("--key", default="")
    p_gate.add_argument("--min-score", type=int, default=DEFAULT_MIN_SCORE)
    p_gate.add_argument("--wait-s", type=int, default=DEFAULT_WAIT_S)
    p_gate.add_argument("--strict", action="store_true", default=False,
                        help="Reject fallback and abort if target silicon is not mined")
    p_gate.add_argument("--fleet", action="store_true", default=False,
                        help="Fast-path claim for dedicated self-hosted fleet runner")
    p_gate.add_argument("--fs-root", default=None)
    p_gate.add_argument("--repo", default=None)

    args = ap.parse_args(argv)
    if args.cmd == "probe":
        info = probe(args.cpuinfo)
        print(json.dumps(info, indent=2))
        log.out("score", str(info["score"]))
        log.out("model", str(info["model"]))
        log.out("class", str(info["class"]))
        return 0

    store = build_store(getattr(args, "repo", None),
                        getattr(args, "fs_root", None))
    if args.cmd == "claim":
        info = probe()
        won = claim(store, args.tag,
                    {"model": info["model"], "score": info["score"]})
        log.out("claimed", "true" if won else "false")
        return 0

    # gate
    res = gate(store, args.tag, key=args.key, min_score=args.min_score,
               wait_s=args.wait_s, strict=args.strict, is_fleet=args.fleet)
    log.out("role", res["role"])
    log.out("score", res["score"])
    log.out("model", res["model"])
    log.log(f"mining gate: role={res['role']} ({res['reason']})")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
