"""ROMForge CLI — every pipeline stage, runnable identically in CI and local.

Commands (workflows call exactly these):
  doctor    environment report (mounts, disk, cores, tools)
  validate  schema-check all rom/device/version configs
  plan      print the execution contract for a ROM+device (DAG, budgets)
  prepare   runner prep: reclaim, swap, apt pkgs, ncurses5 compat
  sync      shallow sync + bank content-addressed source snapshot
  restore   source/state/turbo restore plumbing
  patch     apply device patches + lunch sanity
  slice     ONE exact-resume build slice (or a turbo partition prewarm)
  merge     merge turbo partition states into out/
  verify    run the 14-point hard gate -> SAFETY_REPORT.json
  publish   release the ROM (only if gate PASS) + guarded flash script
  gc        drop stale state tags
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional

from . import __version__
from . import chunker, config, dag, engine, env as fenv, gate as fgate
from . import graph, log, mine, relay, storage, syncer, turbo
from .config import ConfigError, build_plan
from .store import FsStore, ReleaseStore, Router, StoreError


# ---------------------------------------------------------------------------
def _plan_from_args(args, root: Path) -> config.Plan:
    rom_path = Path(args.rom)
    if not rom_path.is_absolute():
        rom_path = root / "configs" / "roms" / f"{args.rom}.yaml"
        if not rom_path.exists():
            # allow direct file path too
            alt = Path(args.rom)
            if alt.exists():
                rom_path = alt
            else:
                cand = list((root / "configs" / "roms").glob(
                    f"*{args.rom}*.yaml"))
                if not cand:
                    raise ConfigError(f"no rom profile matching '{args.rom}' "
                                      f"in configs/roms/")
                rom_path = cand[0]
    device_path = None
    if args.device:
        device_path = root / "configs" / "devices" / f"{args.device}.yaml"
        if not device_path.exists():
            raise ConfigError(f"no device profile at {device_path}")
    return build_plan(root, rom_path, device_path)


def _store(args, root: Path) -> Router:
    if args.store.startswith("fs:"):
        return Router(backend="fs", fs_root=Path(args.store[3:]))
    return Router(backend="auto", repo=args.repo)


def _build_root(args) -> Path:
    env_root = os.environ.get("FORGE_BUILD_ROOT")
    if env_root:
        return Path(env_root)
    if getattr(args, "build_root", None):
        return Path(args.build_root)
    # storage.default_build_root(): the btrfs volume mount if active,
    # else <best-mount>/romforge/aosp — identical to the old plain layout
    try:
        return Path(storage.default_build_root())
    except Exception:
        if os.path.exists("/mnt"):
            return Path("/mnt/romforge/aosp")
        return Path("/tmp/romforge/aosp")


def _ensure_volume(args) -> storage.VolumeState:
    """Mount (or reuse) the compressed build volume; returns its state.
    Called by every command that touches the tree. In degraded/plain mode
    this is a no-op that returns an honest reason."""
    try:
        vol = storage.ensure_volume()
    except Exception as e:  # noqa: BLE001 — degrade, never abort
        vol = storage.VolumeState(mode="plain", reason=str(e)[:200])
    if vol.mode == "btrfs":
        log.ok(f"storage v2: btrfs zstd:1 volume at {vol.vol_mnt} "
               f"(cap {vol.cap_gb:.0f} GiB)")
    else:
        log.warn(f"storage v2 DEGRADED (plain dirs): {vol.reason}")
    return vol


# ---------------------------------------------------------------------------
def cmd_probe(args, root: Path) -> int:
    """Lightweight INDEX probe: emits src_needed / state / done / phase.

    `phase` is the DAG conveyor decision (forge_core.dag.next_action):
    verify | slice | fail — the workflow gates verify/publish/conveyor on
    it, so the premature-verification class of bugs becomes structurally
    impossible (the YAML never guesses; it reads the INDEX)."""
    plan = _plan_from_args(args, root)
    store = _store(args, root)
    t = store.target(plan.rom.key)
    src_tag = (f"src-{args.mhash}" if getattr(args, "mhash", None) else "") or t.get("src_tag", "")
    src_ok = bool(src_tag) and store.exists(src_tag) and not getattr(args, "force", False)
    decision = dag.next_action(t)
    log.out("src_needed", "false" if src_ok else "true")
    log.out("src_tag", src_tag)
    log.out("done", "true" if t.get("done") else "false")
    log.out("slice", str(t.get("slice", 0)))
    log.out("state_tag", t.get("state_tag", ""))
    log.out("classification", t.get("last_classification", ""))
    log.out("runner", plan.runner_image)
    log.out("target_key", plan.rom.key)
    log.out("phase", decision["phase"])
    log.out("phase_reason", decision["reason"])
    log.log(f"conveyor decision: {decision['phase']} — {decision['reason']}")
    return 0


def cmd_doctor(args, root: Path) -> int:
    e = fenv.detect()
    print(e.report())
    for tool in ("git", "gh", "zstd", "rsync", "tar", "ccache"):
        w = shutil.which(tool)
        print(f"  tool {tool:8s} -> {w or 'MISSING'}")
    st = storage.selftest()
    print(f"  storage: {st}")
    silicon = mine.probe()
    print(f"  silicon: {silicon['model']} (score {silicon['score']}, "
          f"{silicon['class']}, avx512={silicon['avx512']})")
    snap = storage.snapshot(Path(_build_root(args)))
    print(f"  disks   : {snap.to_dict()}")
    errs = config.validate_all(root)
    print(f"  configs : {'OK (' + str(len(list((root / 'configs' / 'roms').glob('*.yaml')))) + ' roms)' if not errs else 'ERRORS'}")
    for e_ in errs:
        print(f"    - {e_}")
    return 0 if not errs else 1


def cmd_validate(args, root: Path) -> int:
    errs = config.validate_all(root)
    if errs:
        for e in errs:
            print(f"FAIL {e}")
        return 1
    print("all roms/devices/versions configs valid")
    return 0


def cmd_plan(args, root: Path) -> int:
    plan = _plan_from_args(args, root)
    rows = turbo.partition_plan(plan)
    d = plan.to_dict()
    print(json.dumps(d, indent=2))
    print(f"turbo fan-out : {len(rows)} partition jobs "
          f"({', '.join(r['partition'] for r in rows) or 'disabled'})")
    print(f"slice chain   : {plan.rom.slices} x "
          f"{plan.rom.slice_build_seconds // 60} min "
          f"(in-run, back-to-back)")
    print(f"runner        : {plan.runner_image}")
    print(f"gate          : 14-point hard (blocking)")
    log.out("target_key", plan.rom.key)
    return 0


def cmd_prepare(args, root: Path) -> int:
    try:
        plan = _plan_from_args(args, root)
    except Exception as e:
        log.warn(f"plan resolution in prepare: {e}")
        plan = None
    try:
        fenv.reclaim_disk()
    except Exception:
        pass
    # 1. mount the compressed build volume FIRST — everything below lands
    #    on it (or honestly degrades to the plain layout)
    vol = _ensure_volume(args)
    build_root = Path(vol.build_root) if vol.build_root else _build_root(args)
    # 2. swap: ALWAYS on the raw backing mount — swapfiles inside a btrfs
    #    image are unsafe (COW + swap deadlock) and / is too small (14-25G)
    swap_dir = Path(vol.backing_dir) if vol.backing_dir else \
        Path(build_root).parent
    fenv._safe_run(["sudo", "mkdir", "-p", str(swap_dir)])
    if vol.mode != "btrfs":
        # plain mode: legacy layout prep (btrfs mode skips the recursive
        # chmod — it would flatten archive-restored permissions)
        fenv._safe_run(["sudo", "mkdir", "-p", str(build_root.parent)])
        fenv._safe_run(["sudo", "chmod", "1777", str(build_root.parent)])
        if os.path.exists("/mnt"):
            fenv._safe_run(["sudo", "mkdir", "-p", "/mnt/romforge",
                            str(build_root), str(build_root / "out")])
            fenv._safe_run(["sudo", "chmod", "-R", "1777", "/mnt/romforge"])
    try:
        build_root.mkdir(parents=True, exist_ok=True)
        (build_root / "out").mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    if plan:
        try:
            fenv.ensure_zram(8)
        except Exception:
            pass
        try:
            swap_path = str(swap_dir / ".forge-swap")
            fenv.ensure_swap(swap_path, size_gb=int(plan.version.get("swap_gb", 4)))
        except Exception:
            pass
        try:
            fenv.install_pkgs(plan.apt_packages or
                              plan.version.get("apt_packages", []))
        except Exception:
            pass
        if plan.rom.android_version <= 12:
            try:
                fenv.ncurses5_compat()
            except Exception:
                pass
    log.out("build_root", str(build_root))
    log.out("storage_mode", vol.mode)
    if vol.degraded:
        log.warn(f"storage DEGRADED: {vol.reason} — the run continues on "
                 "plain dirs (old capacity rules apply)")
    log.ok(f"runner prepared at {build_root}")
    return 0


def cmd_sync(args, root: Path) -> int:
    plan = _plan_from_args(args, root)
    store = _store(args, root)
    build_root = _build_root(args)
    tag = f"src-{args.mhash}" if getattr(args, "mhash", None) else None
    if not tag:
        t = store.target(plan.rom.key)
        tag = t.get("src_tag")
    if tag and store.exists(tag) and not getattr(args, "force", False):
        log.ok(f"source snapshot {tag} already banked — sync skipped")
        store.target_update(plan.rom.key, src_tag=tag)
        log.out("src_tag", tag)
        return 0
    fp = syncer.sync_tree(plan, build_root,
                          sync_jobs=int(plan.version.get("sync_jobs", 8)))
    tag = f"src-{fp}"
    if not store.exists(tag) or getattr(args, "force", False):
        store.create(tag, f"source {plan.rom.name} {plan.rom.manifest_branch}",
                     "Content-addressed source snapshot (immutable).")
        n = syncer.snapshot_source(build_root, store, tag,
                                   f"source {plan.rom.name}",
                                   f"mhash={fp}", sink=not args.no_stream)
        log.ok(f"banked source: {tag} ({n} parts)")
    store.target_update(plan.rom.key, mhash=fp, src_tag=tag)
    log.out("src_tag", tag)
    log.out("mhash", fp)
    return 0


def cmd_restore(args, root: Path) -> int:
    plan = _plan_from_args(args, root)
    store = _store(args, root)
    build_root = _build_root(args)
    t = store.target(plan.rom.key)
    what = args.what
    if what == "src":
        src_tag = (f"src-{args.mhash}" if getattr(args, "mhash", None) else None) or t.get("src_tag")
        if not src_tag or not store.exists(src_tag):
            log.out("source", "sync")
            return 0
        if syncer.restore_source(build_root, store, src_tag):
            log.out("source", "cache")
        else:
            log.out("source", "sync")
        return 0
    if what == "state":
        if t.get("state_tag") and relay.restore(build_root, store,
                                                t["state_tag"]):
            log.out("state", t["state_tag"])
            log.out("slice", str(t.get("slice", 0)))
        else:
            log.out("state", "cold")
        return 0
    if what == "turbo":
        n = turbo.merge_turbo_states(build_root, store, plan.rom.key)
        log.out("turbo_merged", str(n))
        return 0
    log.die(f"unknown restore target: {what}")
    return 2


def cmd_patch(args, root: Path) -> int:
    plan = _plan_from_args(args, root)
    build_root = _build_root(args)
    syncer.apply_patches(plan, build_root, root)
    syncer.validate_lunch(plan, build_root)
    return 0


def cmd_slice(args, root: Path) -> int:
    """One self-sufficient build slot.

    Idempotent by design so the workflow can define a fixed chain of these:
      0. INDEX says done?   -> no-op, emit classification=done
      1. capacity halt?     -> refuse (the conveyor must stop re-dispatch;
                               see forge_core.dag — the storage-deadlock fix)
      2. mount build volume -> source/out land on btrfs zstd:1 (or plain)
      3. restore source     -> from content-addressed src-<mhash>
      4. patches + lunch sanity
      5. restore out/ state (exact resume); cold start -> merge turbo states
      6. run the slice (or the turbo partition prewarm)
      7. bank out/ state + update INDEX (incl. last_classification +
         stop_reason, which drive the DAG conveyor's next decision)
    """
    plan = _plan_from_args(args, root)
    store = _store(args, root)
    t = store.target(plan.rom.key)

    # per-run target override (workflow_dispatch input)
    if os.environ.get("FORGE_TARGET_OVERRIDE") and not args.turbo_part:
        plan.rom.build_target = os.environ["FORGE_TARGET_OVERRIDE"]

    if t.get("done") and not args.force:
        log.ok("target already done (INDEX) — slice slot no-ops")
        log.out("classification", "done")
        return 0

    # ---- 0. capacity halt: the storage-deadlock guard -----------------------
    if t.get("last_classification") == "capacity" and not args.force:
        log.warn("INDEX says the last slice stopped on DISK CAPACITY — "
                 "refusing to re-run (this is the deadlock guard; re-"
                 "dispatching would burn 30 min to reproduce the same "
                 "stop). Grow the volume (FORGE_VOLUME_RESERVE_GB) or "
                 "prune the working set, then --force.")
        log.out("classification", "capacity")
        return 1

    # ---- 1. storage volume + tree --------------------------------------------
    vol = _ensure_volume(args)
    build_root = Path(vol.build_root) if vol.build_root else _build_root(args)

    # ---- 2. source ----------------------------------------------------------
    src_tag = (f"src-{args.mhash}" if getattr(args, "mhash", None) else None) or t.get("src_tag")
    if not src_tag or not store.exists(src_tag):
        log.die(f"no source snapshot for {plan.rom.key} — the sync job must "
                f"run first (this is a workflow wiring bug otherwise)")
    if not (build_root / ".source_ready").exists():
        if not syncer.restore_source(build_root, store, src_tag):
            log.die(f"source restore failed from {src_tag}")
    syncer.ensure_device_repos(plan, build_root)
    syncer.apply_patches(plan, build_root, root)
    syncer.validate_lunch(plan, build_root)

    use_ccache = plan.rom.env.get("USE_CCACHE") == "1"
    budget = int(args.budget_s or plan.rom.slice_build_seconds)

    # ---- 2. turbo partition prewarm slot -------------------------------------
    if args.turbo_part:
        target = args.turbo_part
        tag = f"state-{plan.rom.key}-turbo-{target}"
        if store.exists(tag) and not args.force:
            log.ok(f"turbo state {tag} exists — skipping")
            log.out("classification", "done")
            return 0
        res = engine.run_slice(plan, build_root, target, budget,
                               Path(args.log or "/tmp/forge-turbo.log"),
                               use_ccache=use_ccache, allow_missing_deps=True)
        log.out("classification", res["classification"])
        if res["classification"] == "done":
            if not store.exists(tag):
                store.create(tag, f"turbo {target} {plan.rom.key}",
                             "Partition prewarm state.")
            relay.bank(build_root, store, tag, plan.rom.key, 0,
                       notes=f"turbo {target}")
            return 0
        return 1 if res["classification"] == "error" else 0

def _run_single_slice(plan, store, build_root: Path, budget: int, args, use_ccache: bool) -> int:
    t = store.target(plan.rom.key)
    syncer.ensure_prebuilts(build_root)

    log_file = Path(args.log or "/tmp/forge-slice.log")
    res = engine.run_slice(plan, build_root, plan.rom.build_target, budget,
                           log_file, use_ccache=use_ccache)
    # done-requires-zip: rc==0 alone is NOT success (the second half of the
    # premature-verification bug — dag.finalize_classification)
    if res["classification"] == "done":
        rom_zip = engine.find_rom_zip(plan, build_root)
        res["classification"] = dag.finalize_classification(
            "done", rom_zip)["classification"]
    else:
        rom_zip = None
    log.out("classification", str(res["classification"]))
    log.out("stop_reason", str(res.get("stop_reason", "")))
    log.out("rom_zip", str(rom_zip) if rom_zip else "")
    engine.slice_summary(res, log_file, build_root / "out", budget)

    # Bank graph if soong created build.ninja and mhash exists
    src_tag = t.get("src_tag", "")
    mhash = src_tag.replace("src-", "") if src_tag.startswith("src-") else getattr(plan, "mhash", "")
    if mhash:
        try:
            graph.bank_graph(build_root, store, mhash, plan.rom.lunch)
        except Exception:
            pass

    if res["classification"] == "done":
        # bank the FINAL out/ too: verify+publish run on fresh runners and
        # restore this state (the gate needs product-dir artifacts + zip)
        n = int(t.get("slice", 0)) + 1
        tag = f"state-{plan.rom.key}-s{n}"
        if not store.exists(tag):
            store.create(tag, f"out-state {plan.rom.key} slice {n} (final)",
                         "Final state: carries the ROM zip for the gate.")
        relay.bank(build_root, store, tag, plan.rom.key, n,
                   notes="final state, classification=done")

        # F4: Also bank the lightweight product-state (2-4 GB) for fast gate verification and publish
        prod_tag = f"state-{plan.rom.key}-final-product"
        relay.bank_product_state(build_root, store, prod_tag, plan.rom.key,
                                 notes=f"lightweight product-state after slice {n}")

        store.target_update(plan.rom.key, slice=n, state_tag=tag, done=True,
                            rom_zip=str(rom_zip),
                            last_classification="done", stop_reason="")
        return 0

    if res["classification"] in ("sliced", "capacity"):
        n = int(t.get("slice", 0)) + 1
        tag = f"state-{plan.rom.key}-s{n}"
        if not store.exists(tag):
            store.create(tag, f"out-state {plan.rom.key} slice {n}",
                         "Exact-resume ninja state.")
        relay.bank(build_root, store, tag, plan.rom.key, n,
                   notes=f"slice {n}, classification={res['classification']}")
        store.target_update(plan.rom.key, slice=n, state_tag=tag, done=False,
                            last_classification=str(res["classification"]),
                            stop_reason=str(res.get("stop_reason", "")))
        log.out("slice", str(n))
        # capacity exits GREEN from the slot itself: the state is banked and
        # the POSTCHECK reads INDEX and red-outs the run — a red slot here
        # would skip the postcheck (which carries the human-readable reason)
        return 0

    # real error — dump forensics tail to console
    if log_file.exists():
        try:
            lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
            log.warn(f"=== BUILD FAILED: last {min(100, len(lines))} lines of {log_file} ===")
            for line in lines[-100:]:
                print(line, file=sys.stderr)
            log.warn("=== END BUILD LOG FORENSICS ===")
        except Exception:
            pass

    # bank what we have anyway (compiled objects are valuable)
    n = int(t.get("slice", 0)) + 1
    tag = f"state-{plan.rom.key}-s{n}"
    if not store.exists(tag):
        store.create(tag, f"out-state {plan.rom.key} slice {n} (post-error)",
                     "Exact-resume ninja state after a build error.")
    relay.bank(build_root, store, tag, plan.rom.key, n,
               notes=f"slice {n}, classification=error")
    store.target_update(plan.rom.key, slice=n, state_tag=tag,
                        last_classification="error",
                        stop_reason=str(res.get("stop_reason", "")))
    return 1


def cmd_slice(args, root: Path) -> int:
    plan = _plan_from_args(args, root)
    store = _store(args, root)
    t = store.target(plan.rom.key)

    # ---- 0. done short-circuit -----------------------------------------------
    if t.get("done") and not args.force:
        log.ok(f"{plan.rom.key} is already marked DONE in INDEX — nothing to slice")
        log.out("classification", "done")
        return 0

    # ---- 0. capacity halt: the storage-deadlock guard -----------------------
    if t.get("last_classification") == "capacity" and not args.force:
        log.warn("INDEX says the last slice stopped on DISK CAPACITY — "
                 "refusing to re-run (this is the deadlock guard; re-"
                 "dispatching would burn 30 min to reproduce the same "
                 "stop). Grow the volume (FORGE_VOLUME_RESERVE_GB) or "
                 "prune the working set, then --force.")
        log.out("classification", "capacity")
        return 1

    # ---- 1. storage volume + tree --------------------------------------------
    vol = _ensure_volume(args)
    build_root = Path(vol.build_root) if vol.build_root else _build_root(args)

    # ---- 2. source ----------------------------------------------------------
    src_tag = (f"src-{args.mhash}" if getattr(args, "mhash", None) else None) or t.get("src_tag")
    if not src_tag or not store.exists(src_tag):
        log.die(f"no source snapshot for {plan.rom.key} — the sync job must "
                f"run first (this is a workflow wiring bug otherwise)")
    if not (build_root / ".source_ready").exists():
        if not syncer.restore_source(build_root, store, src_tag):
            log.die(f"source restore failed from {src_tag}")
    syncer.ensure_device_repos(plan, build_root)
    syncer.apply_patches(plan, build_root, root)
    syncer.validate_lunch(plan, build_root)

    use_ccache = plan.rom.env.get("USE_CCACHE") == "1"
    budget = int(args.budget_s or plan.rom.slice_build_seconds)

    # ---- 2. turbo partition prewarm slot -------------------------------------
    if args.turbo_part:
        target = args.turbo_part
        tag = f"state-{plan.rom.key}-turbo-{target}"
        if store.exists(tag) and not args.force:
            log.ok(f"turbo state {tag} exists — skipping")
            log.out("classification", "done")
            return 0
        res = engine.run_slice(plan, build_root, target, budget,
                               Path(args.log or "/tmp/forge-turbo.log"),
                               use_ccache=use_ccache, allow_missing_deps=True)
        log.out("classification", res["classification"])
        if res["classification"] == "done":
            if not store.exists(tag):
                store.create(tag, f"turbo {target} {plan.rom.key}",
                             "Partition prewarm state.")
            relay.bank(build_root, store, tag, plan.rom.key, 0,
                       notes=f"turbo {target}")
            return 0
        return 1 if res["classification"] == "error" else 0

    # ---- 3. main-chain slice state restore -----------------------------------
    if (build_root / "out" / ".ninja_log").exists():
        log.ok("warm out/ present — exact resume")
    else:
        restored = False
        if t.get("state_tag") and relay.restore(build_root, store, t["state_tag"]):
            log.ok(f"resumed state {t['state_tag']} (slice {t.get('slice', 0)})")
            restored = True
        else:
            # Fallback: scan newest state-<key>-s* tags
            candidate_tags = sorted(store.list_tags(f"state-{plan.rom.key}-s"), reverse=True)
            for ctag in candidate_tags:
                if ctag != t.get("state_tag") and relay.restore(build_root, store, ctag):
                    log.ok(f"fallback resumed newest available state {ctag}")
                    restored = True
                    break
        if not restored:
            log.log("cold out/ — merging any turbo prewarm states")
            turbo.merge_turbo_states(build_root, store, plan.rom.key)
            # Try restoring banked Soong graph if available
            mhash = src_tag.replace("src-", "") if src_tag.startswith("src-") else getattr(plan, "mhash", "")
            if mhash:
                graph.restore_graph(build_root, store, mhash, plan.rom.lunch)

    wall_budget = int(getattr(args, "until_budget", 0) or os.environ.get("FORGE_UNTIL_BUDGET_S", 0) or 0)
    if not wall_budget:
        return _run_single_slice(plan, store, build_root, budget, args, use_ccache)

    # ---- 4. Fusion Slot loop (Phase 2.1) -------------------------------------
    BANK_RESERVE_S = 1800
    MIN_SLICE_S = 1800
    start_wall = time.time()
    slice_count = 0
    log.ok(f"fusion slot enabled: {wall_budget // 60}m job wall budget")

    while True:
        elapsed = time.time() - start_wall
        rem = wall_budget - elapsed
        if rem < MIN_SLICE_S + BANK_RESERVE_S:
            log.log(f"fusion loop: {rem // 60:.0f}m wall left (< reserve {BANK_RESERVE_S // 60}m) — ending job cleanly")
            return 0

        cur_t = store.target(plan.rom.key)
        if cur_t.get("done") and not args.force:
            log.ok("fusion loop: INDEX done=true — completed")
            return 0
        if cur_t.get("last_classification") == "capacity" and not args.force:
            log.warn("fusion loop: capacity stopped — ending job")
            return 0

        this_slice_budget = min(budget, int(rem - BANK_RESERVE_S))
        slice_count += 1
        log.ok(f"=== Starting fusion slice {slice_count} (budget: {this_slice_budget // 60}m, wall left: {rem // 60:.0f}m) ===")
        rc = _run_single_slice(plan, store, build_root, this_slice_budget, args, use_ccache)
        if rc != 0:
            return rc

        after_t = store.target(plan.rom.key)
        if after_t.get("done") or after_t.get("last_classification") == "capacity":
            return 0


def _ensure_out(plan, store, build_root: Path) -> None:
    """verify/publish run on fresh runners: restore the banked final out/."""
    dev = plan.rom.device or (plan.rom.lunch.split("_")[1].split("-")[0] if "_" in plan.rom.lunch else plan.rom.lunch.split("-")[0])
    if (build_root / "out" / "target" / "product" / dev).exists():
        return
    # 1. Prefer lightweight product-state tag if available (2-4 GB vs 35-50 GB full state)
    prod_tag = f"state-{plan.rom.key}-final-product"
    if store.exists(prod_tag):
        log.log(f"restoring lightweight product-state {prod_tag}...")
        if relay.restore(build_root, store, prod_tag):
            log.ok(f"restored product-state from {prod_tag}")
            return

    # 2. Fall back to target INDEX state_tag or newest slice state
    t = store.target(plan.rom.key)
    tag = t.get("state_tag", "")
    if not tag or not store.exists(tag):
        # fall back to the newest state tag on the store
        tags = sorted(store.list_tags(f"state-{plan.rom.key}-s"))
        tag = tags[-1] if tags else ""
    if not tag or not relay.restore(build_root, store, tag):
        log.die(f"no banked out/ state for {plan.rom.key} — nothing to "
                f"verify/publish (did the slice chain run?)")


def cmd_verify(args, root: Path) -> int:
    plan = _plan_from_args(args, root)
    store = _store(args, root)
    vol = _ensure_volume(args)
    build_root = Path(vol.build_root) if vol.build_root else _build_root(args)
    _ensure_out(plan, store, build_root)
    dev = plan.rom.device or (plan.rom.lunch.split("_")[1].split("-")[0] if "_" in plan.rom.lunch else plan.rom.lunch.split("-")[0])
    pdir = build_root / "out" / "target" / "product" / dev
    rom_zip = Path(args.rom_zip) if args.rom_zip else \
        engine.find_rom_zip(plan, build_root)
    if not rom_zip:
        log.die("no ROM zip to verify — run a completed slice first")
    host_tools = build_root / "out" / "host" / "linux-x86" / "bin"
    g = fgate.Gate(plan.device, plan.rom, pdir, rom_zip,
                   host_tools=host_tools if host_tools.exists() else None)
    report = g.run()
    out = Path(args.out or "SAFETY_REPORT.json")
    out.write_text(report.to_json(), encoding="utf-8")
    log.out("gate", "PASS" if report.passed else "FAIL")
    log.out("report", str(out))
    store = _store(args, root)
    store.artifacts.stage("safety-report", [out])
    return 0 if report.passed else 1


def cmd_publish(args, root: Path) -> int:
    plan = _plan_from_args(args, root)
    store = _store(args, root)
    vol = _ensure_volume(args)
    build_root = Path(vol.build_root) if vol.build_root else _build_root(args)
    _ensure_out(plan, store, build_root)
    report_path = Path(args.report or "SAFETY_REPORT.json")
    if not report_path.exists():
        log.die("no SAFETY_REPORT.json — run `forge verify` first "
                "(hard gate cannot be bypassed)")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report.get("verdict") != "PASS":
        log.die(f"hard gate verdict={report.get('verdict')} — publishing "
                f"REFUSED. Fix the build or the profiles.")
    rom_zip = Path(args.rom_zip) if args.rom_zip else \
        engine.find_rom_zip(plan, build_root)
    if not rom_zip or not rom_zip.exists():
        log.die("rom zip missing")

    dev = plan.rom.device or (plan.rom.lunch.split("_")[1].split("-")[0] if "_" in plan.rom.lunch else plan.rom.lunch.split("-")[0])
    pdir = build_root / "out" / "target" / "product" / dev
    tag = f"rom-{plan.rom.key}"
    store.create(tag, f"{plan.rom.name} · {plan.device.name}",
                 f"Gate-verified build. See SAFETY_REPORT.json.")

    bundle = Path(build_root) / ".forge-publish"
    if bundle.exists():
        shutil.rmtree(bundle)
    bundle.mkdir(parents=True)
    shutil.copy2(rom_zip, bundle / rom_zip.name)
    # rescue images for the fastboot path
    images = []
    for img in ("boot.img", "dtbo.img", "vbmeta.img", "vendor_boot.img"):
        p = pdir / img
        if p.exists():
            shutil.copy2(p, bundle / img)
            images.append(img)
    shutil.copy2(report_path, bundle / "SAFETY_REPORT.json")
    flash = fgate.generate_flash_script(
        plan.device, plan.rom, images, verdict="PASS")
    (bundle / "flash-guarded.sh").write_text(flash, encoding="utf-8")
    (bundle / "flash-guarded.sh").chmod(0o755)
    sums = []
    for f in sorted(bundle.iterdir()):
        sums.append(f"{chunker._hash_file(f)}  {f.name}")
    (bundle / "SHA256SUMS").write_text("\n".join(sums) + "\n",
                                       encoding="utf-8")
    store.upload(tag, sorted(bundle.iterdir()))
    store.target_update(plan.rom.key, done=True, rom_tag=tag,
                        last_gate=report.get("verdict"))
    log.ok(f"published {tag} with {len(images) + 2} assets")
    log.out("rom_tag", tag)
    store.gc_state(plan.rom.key, keep=1)
    return 0


def cmd_gc(args, root: Path) -> int:
    plan = _plan_from_args(args, root)
    store = _store(args, root)
    dropped = store.gc_state(plan.rom.key, keep=int(args.keep))
    if getattr(args, "locks", False):
        locks = store.gc_locks(plan.rom.key)
        log.out("locks_dropped", str(len(locks)))
    log.out("state_dropped", str(len(dropped)))
    return 0


# ---------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="forge",
                                 description="ROMForge universal ROM CI")
    ap.add_argument("--root", default=".", help="forge repo root")
    ap.add_argument("--rom", default=None, help="rom profile name/path")
    ap.add_argument("--device", default=None, help="device profile name")
    ap.add_argument("--store", default="auto", help="auto|release|fs:<dir>")
    ap.add_argument("--repo", default=None, help="override github repo")
    ap.add_argument("--build-root", default=None, help="override tree path")
    ap.add_argument("command", help=" ".join(
        c for c in ("doctor validate plan prepare sync restore patch slice "
                    "merge verify publish gc")))
    ap.add_argument("rest", nargs=argparse.REMAINDER)
    args = ap.parse_args([*(argv or sys.argv[1:])])

    root = Path(args.root).resolve()
    # route subcommand-specific flags
    sub = argparse.ArgumentParser(prog=f"forge {args.command}",
                                   allow_abbrev=False)
    for flag, kwargs in (
            ("--rom", {"default": None}),
            ("--mhash", {"default": None}),
            ("--what", {"default": None, "choices": ["src", "state", "turbo"]}),
            ("--turbo-part", {"default": None}),
            ("--budget-s", {"default": None}),
            ("--until-budget", {"default": None, "type": int}),
            ("--log", {"default": None}),
            ("--rom-zip", {"default": None}),
            ("--report", {"default": None}),
            ("--out", {"default": None}),
            ("--force", {"action": "store_true"}),
            ("--keep", {"default": "2", "type": int}),
            ("--locks", {"action": "store_true"}),
            ("--no-stream", {"action": "store_true"})):
        try:
            sub.add_argument(flag, **kwargs)
        except argparse.ArgumentError:
            pass
    subargs = sub.parse_args(args.rest)
    for k, v in vars(subargs).items():
        setattr(args, k, v)

    if args.command not in ("doctor", "validate") and not args.rom:
        log.die("this command needs --rom <profile>")
    try:
        return {
            "doctor": cmd_doctor, "validate": cmd_validate,
            "plan": cmd_plan, "prepare": cmd_prepare, "sync": cmd_sync,
            "restore": cmd_restore, "patch": cmd_patch, "slice": cmd_slice,
            "verify": cmd_verify, "publish": cmd_publish, "gc": cmd_gc,
            "probe": cmd_probe,}[args.command](args, root)
    except (ConfigError, StoreError, chunker.ChunkerError,
            syncer.SyncError, engine.BuildError) as e:
        log.die(str(e))
        return 2
    except KeyError:
        log.die(f"unknown command: {args.command}")
        return 2
    except Exception as e:
        import traceback
        traceback.print_exc()
        log.die(f"{args.command} unexpected failure: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
