"""Turbo mode: parallel partition prewarm across free runner slots.

Physics: GitHub's free tier gives ~20 concurrent 4-vCPU jobs on public
repos, and a 6 h wall per job. A cold full build needs 12-30 h of 4-vCPU
compute depending on Android version — so a single-job-under-6h cold build
is physically impossible, and "wait for cron" wastes days.

Turbo spends concurrency instead of time: partition image targets
(bootimage / vendorimage / productimage / system_extimage) are largely
independent subgraphs. Each runs on its OWN runner in parallel with
`ALLOW_MISSING_DEPENDENCIES=true` (soong emits stubs for cross-partition
deps it cannot see — the classic SDK partial-build mode), and banks its
out/ state. The assemble slice then starts from the MERGED state and only
builds the system/framework critical path + the OTA packaging.

Honesty (documented, not hidden): stubbed deps mean partition outputs CAN
be subtly inconsistent; that is why (a) the merge uses the system slice as
the authoritative base (turbo outs only ADD missing paths), (b) the final
`m` re-links everything still stale, and (c) the 14-point hard gate must
pass before anything ships. Turbo failures degrade to the slice path —
they cost time, never safety.

Default partition sets (overridable per ROM in rom.turbo.targets):
  A10-A11: bootimage, dtboimage, vendorimage, productimage
  A12+   : + system_extimage
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

from . import log
from .config import Plan

DEFAULT_PARTITIONS = {
    10: ["bootimage", "vendorimage", "productimage"],
    11: ["bootimage", "vendorimage", "productimage"],
    12: ["bootimage", "dtboimage", "vendorimage", "productimage",
         "system_extimage"],
    13: ["bootimage", "dtboimage", "vendorimage", "productimage",
         "system_extimage"],
    14: ["bootimage", "dtboimage", "vendorimage", "productimage",
         "system_extimage"],
    15: ["bootimage", "vendor_boot", "dtboimage", "vendorimage",
         "productimage", "system_extimage"],
    16: ["bootimage", "vendor_boot", "dtboimage", "vendorimage",
         "productimage", "system_extimage"],
}

# targets that should never run in turbo (system/framework = critical path
# of the assemble slice; also the biggest, so slicing it is more efficient
# than stubbing its deps)
NEVER_TURBO = {"systemimage", "bacon", "qassa", "otapackage", "droid",
               "ramdisk", "otatools"}


def partition_plan(plan: Plan) -> List[Dict[str, str]]:
    """Matrix rows for the turbo fan-out."""
    conf = plan.rom.turbo or {}
    if not conf.get("enabled", True):
        return []
    targets = conf.get("targets") or DEFAULT_PARTITIONS.get(
        plan.rom.android_version, DEFAULT_PARTITIONS[13])
    rows = []
    for t in targets:
        if t in NEVER_TURBO:
            log.warn(f"turbo: refusing to fan out '{t}' (critical path) — "
                     f"it belongs to the slice chain")
            continue
        rows.append({"partition": t,
                     "tag": f"state-{plan.rom.key}-turbo-{t}",
                     "budget_s": int(conf.get("budget_s", 16500))})
    return rows


def merge_turbo_states(build_root: Path, store, key: str) -> int:
    """Merge every available turbo state for `key` into the live out/.

    Returns number of donor states merged. Idempotent (safe to call in
    every assemble slice; --ignore-existing makes repeats free).
    """
    from . import relay
    merged = 0
    for tag in store.list_tags(f"state-{key}-turbo-"):
        part = tag.rsplit("-", 1)[-1]
        tmp = build_root.parent / f".forge-turbo-{part}"
        donor = build_root.parent / f".forge-turbo-incoming-{part}"
        if donor.exists():
            continue
        try:
            from . import chunker
            chunker.unpack_from_store(store, tag, "out", donor, strip=False)
            relay.merge(build_root, donor / "out")
            merged += 1
        except Exception as e:
            log.warn(f"turbo state {tag} unpack failed ({e}) — skipping")
        finally:
            import shutil
            shutil.rmtree(donor, ignore_errors=True)
    if merged:
        log.ok(f"turbo: merged {merged} partition states into out/")
    return merged
