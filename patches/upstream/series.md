# Upstream patch series — upgrading `zephyr4289/aosp-a10` in place

Two surgical, self-contained fixes for the inherited harness, generated
with `git format-patch` against upstream `cf42541` and verified with
`git apply --check`. They exist for maintainers who want to keep the
original repo shape (bash harness, cron chain) rather than migrate to
ROMForge. ROMForge implements both ideas (and everything else in
TECHNICAL.md) natively.

Apply:

```bash
git clone https://github.com/zephyr4289/aosp-a10 && cd aosp-a10
git am ../romforge/patches/upstream/00*.patch
```

## 0001 — relay: bank/restore the full out/ ninja state between slices

**Fixes:** "time wasted in redoing cached tasks, growing linearly with
slice number."

Root cause (upstream `scripts/04_upload_cache.sh` line 37):
`pack_split … "aosp/out"` — intermediates never persisted, only ccache.
ccache accelerates C/C++ compiles only; every slice re-paid soong/kati
analysis (~20 min), all Java/Kotlin compilation, dexing, proto, and
every packaging step, so the wasted prefix grew with the frontier.

The patch:

* adds `scripts/lib_stream.sh` — a zero-staging `tar | zstd | split
  --filter 'gh release upload'` streamer (parts never pile up on disk —
  also removes the biggest ENOSPC contributor);
* `04_upload_cache.sh out` — banks `out/` (minus rebuild-cheap fat:
  `symbols/`, host test dex) under `state-cache-s<N>` tags, keeping the
  newest two generations;
* `05_restore_cache.sh` — restores the newest state before the slice;
  emits `state=<tag>|miss` and the workflow derives the next slice
  number;
* `build.yml` — persists out/ state after every non-final slice; ccache
  banking becomes opt-in (`vars.FORGE_CCACHE_TOO`).

With this, ninja resumes exactly (mtimes + `.ninja_log`) for every
language: per-slice overhead drops from O(frontier) to a flat ~20-30
min, and campaigns converge instead of stalling.

## 0002 — env: auto-relocate workdir to the largest mount (anti-ENOSPC)

**Fixes:** "crashed midway due to storage issues" (the ENOSPC-at-94 %
commit `cf42541` shaved ccache/swap instead of fixing the layout).

`scripts/lib.sh` now relocates `AOSP_ROOT`/`CCACHE_DIR` to the writable
mount with the most free space (on GHA runners: `/mnt`, ~65 GB vs
`/home`'s ~14–25 GB), guarded by a marker file for stability and by a
30 GB advantage threshold so explicitly-configured roots and small
local disks are never surprised. Combined with 0001's zero-staging
uploads, the disk ledger balances: tree + out + swap fit on /mnt with
ladder headroom.

## What the series deliberately does NOT carry

These need the ROMForge architecture (they are not one-file fixes):

* the in-run slot chain (kills the 6-hourly cron / CHAIN_PAT wait —
  needs the 35-day workflow-run ceiling trick),
* turbo partition fan-out (needs the state-merge + gate machinery),
* the 14-point anti-brick gate (needs device profiles),
* content-addressed source snapshots (needs the INDEX protocol),
* the versions.yaml A10→A16 hosting matrix.

Those are all in this repo: `forge_core/`, `.github/workflows/`,
`configs/` — with the full dossier in `TECHNICAL.md`.
