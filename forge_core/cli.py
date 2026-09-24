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
from pathlib import Path
from typing import List, Optional

from . import __version__
from . import chunker, config, engine, env as fenv, gate as fgate
from . import log, relay, syncer, turbo
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
    if os.path.exists("/mnt"):
        return Path("/mnt/romforge/aosp")
    try:
        mnt = fenv.detect().best_mount()
        return Path(mnt.path) / "romforge" / "aosp"
    except Exception:
        return Path("/tmp/romforge/aosp")


# ---------------------------------------------------------------------------
def cmd_probe(args, root: Path) -> int:
    """Lightweight INDEX probe: emits src_needed / state / done for the DAG."""
    plan = _plan_from_args(args, root)
    store = _store(args, root)
    t = store.target(plan.rom.key)
    src_tag = t.get("src_tag", "")
    if not src_tag:
        try:
            cand = [tag for tag in store.list_tags("src-") if store.exists(tag)]
            if cand:
                src_tag = cand[0]
                store.target_update(plan.rom.key, src_tag=src_tag)
        except Exception:
            pass
    src_ok = bool(src_tag) and store.exists(src_tag) and not args.force
    log.out("src_needed", "false" if src_ok else "true")
    log.out("src_tag", src_tag)
    log.out("done", "true" if t.get("done") else "false")
    log.out("slice", str(t.get("slice", 0)))
    log.out("state_tag", t.get("state_tag", ""))
    log.out("runner", plan.runner_image)
    log.out("target_key", plan.rom.key)
    return 0


def cmd_doctor(args, root: Path) -> int:
    e = fenv.detect()
    print(e.report())
    for tool in ("git", "gh", "zstd", "rsync", "tar", "ccache"):
        w = shutil.which(tool)
        print(f"  tool {tool:8s} -> {w or 'MISSING'}")
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
    build_root = _build_root(args)
    fenv._safe_run(["sudo", "mkdir", "-p", str(build_root.parent)])
    fenv._safe_run(["sudo", "chmod", "1777", str(build_root.parent)])
    try:
        build_root.parent.mkdir(parents=True, exist_ok=True)
        build_root.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    if os.path.exists("/mnt"):
        opt_aosp = Path("/opt/romforge/aosp")
        mnt_out_storage = Path("/mnt/romforge/out_storage")
        fenv._safe_run(["sudo", "mkdir", "-p", str(opt_aosp), str(mnt_out_storage), str(build_root)])
        fenv._safe_run(["sudo", "chmod", "1777", "/opt/romforge", str(opt_aosp), "/mnt/romforge", str(mnt_out_storage), str(build_root)])
        if not os.path.ismount(str(build_root)):
            fenv._safe_run(["sudo", "mount", "--bind", str(opt_aosp), str(build_root)])
        out_dir = build_root / "out"
        fenv._safe_run(["sudo", "mkdir", "-p", str(out_dir)])
        fenv._safe_run(["sudo", "chmod", "1777", str(out_dir)])
        if not os.path.ismount(str(out_dir)):
            fenv._safe_run(["sudo", "mount", "--bind", str(mnt_out_storage), str(out_dir)])
    if plan:
        try:
            swap_path = str(Path(build_root).parent / ".forge-swap")
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
    log.ok(f"runner prepared at {build_root}")
    return 0


def cmd_sync(args, root: Path) -> int:
    plan = _plan_from_args(args, root)
    store = _store(args, root)
    build_root = _build_root(args)
    tag = f"src-{args.mhash}" if args.mhash else None
    if not tag:
        try:
            cand = [tg for tg in store.list_tags("src-") if store.exists(tg)]
            if cand:
                tag = cand[0]
        except Exception:
            pass
    if tag and store.exists(tag):
        log.ok(f"source snapshot {tag} already banked — sync skipped")
        store.target_update(plan.rom.key, src_tag=tag)
        log.out("src_tag", tag)
        return 0
    fp = syncer.sync_tree(plan, build_root,
                          sync_jobs=int(plan.version.get("sync_jobs", 8)))
    tag = f"src-{fp}"
    if not store.exists(tag):
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
        src_tag = t.get("src_tag") or (f"src-{args.mhash}" if args.mhash else None)
        if not src_tag:
            try:
                cand = [tg for tg in store.list_tags("src-") if store.exists(tg)]
                if cand:
                    src_tag = cand[0]
            except Exception:
                pass
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
      1. INDEX says done?  -> no-op, emit classification=done
      2. restore source    -> from content-addressed src-<mhash>
      3. patches + lunch sanity
      4. restore out/ state (exact resume); cold start -> merge turbo states
      5. run the slice (or the turbo partition prewarm)
      6. bank out/ state + update INDEX
    """
    plan = _plan_from_args(args, root)
    store = _store(args, root)
    build_root = _build_root(args)
    t = store.target(plan.rom.key)

    # per-run target override (workflow_dispatch input)
    if os.environ.get("FORGE_TARGET_OVERRIDE") and not args.turbo_part:
        plan.rom.build_target = os.environ["FORGE_TARGET_OVERRIDE"]

    if t.get("done") and not args.force:
        log.ok("target already done (INDEX) — slice slot no-ops")
        log.out("classification", "done")
        return 0

    # ---- 1. source ----------------------------------------------------------
    src_tag = t.get("src_tag") or (f"src-{args.mhash}" if args.mhash else None)
    if not src_tag:
        try:
            cand = [tg for tg in store.list_tags("src-") if store.exists(tg)]
            if cand:
                src_tag = cand[0]
                store.target_update(plan.rom.key, src_tag=src_tag)
        except Exception:
            pass
    if not src_tag or not store.exists(src_tag):
        log.die(f"no source snapshot for {plan.rom.key} — the sync job must "
                f"run first (this is a workflow wiring bug otherwise)")
    if not (build_root / ".source_ready").exists():
        if not syncer.restore_source(build_root, store, src_tag):
            log.die(f"source restore failed from {src_tag}")
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

    # ---- 3. main-chain slice --------------------------------------------------
    if (build_root / "out" / ".ninja_log").exists():
        log.ok("warm out/ present — exact resume")
    else:
        if t.get("state_tag") and \
                relay.restore(build_root, store, t["state_tag"]):
            log.ok(f"resumed state {t['state_tag']} (slice "
                   f"{t.get('slice', 0)})")
        else:
            log.log("cold out/ — merging any turbo prewarm states")
            turbo.merge_turbo_states(build_root, store, plan.rom.key)

    syncer.ensure_prebuilts(build_root)

    log_file = Path(args.log or "/tmp/forge-slice.log")
    res = engine.run_slice(plan, build_root, plan.rom.build_target, budget,
                           log_file, use_ccache=use_ccache)
    log.out("classification", str(res["classification"]))
    engine.slice_summary(res, log_file, build_root / "out", budget)

    if res["classification"] == "done":
        rom_zip = engine.find_rom_zip(plan, build_root)
        log.out("rom_zip", str(rom_zip) if rom_zip else "")
        # bank the FINAL out/ too: verify+publish run on fresh runners and
        # restore this state (the gate needs product-dir artifacts + zip)
        n = int(t.get("slice", 0)) + 1
        tag = f"state-{plan.rom.key}-s{n}"
        if not store.exists(tag):
            store.create(tag, f"out-state {plan.rom.key} slice {n} (final)",
                         "Final state: carries the ROM zip for the gate.")
        relay.bank(build_root, store, tag, plan.rom.key, n,
                   notes="final state, classification=done")
        store.target_update(plan.rom.key, slice=n, state_tag=tag, done=True)
        return 0

    if res["classification"] == "sliced":
        n = int(t.get("slice", 0)) + 1
        tag = f"state-{plan.rom.key}-s{n}"
        if not store.exists(tag):
            store.create(tag, f"out-state {plan.rom.key} slice {n}",
                         "Exact-resume ninja state.")
        relay.bank(build_root, store, tag, plan.rom.key, n,
                   notes=f"slice {n}, classification=sliced")
        store.target_update(plan.rom.key, slice=n, state_tag=tag, done=False)
        log.out("slice", str(n))
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
    store.target_update(plan.rom.key, slice=n, state_tag=tag)
    return 1


def _ensure_out(plan, store, build_root: Path) -> None:
    """verify/publish run on fresh runners: restore the banked final out/."""
    dev = plan.rom.lunch.split("_")[1]
    if (build_root / "out" / "target" / "product" / dev).exists():
        return
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
    build_root = _build_root(args)
    _ensure_out(plan, store, build_root)
    dev = plan.rom.lunch.split("_")[1]
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
    build_root = _build_root(args)
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

    dev = plan.rom.lunch.split("_")[1]
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
    store.gc_state(plan.rom.key, keep=int(args.keep))
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
            ("--log", {"default": None}),
            ("--rom-zip", {"default": None}),
            ("--report", {"default": None}),
            ("--out", {"default": None}),
            ("--force", {"action": "store_true"}),
            ("--keep", {"default": "2", "type": int}),
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
