# aosp-a10 — QASSA 2.4 (Android 10) for Nokia 6.1 (PL2), built free on GitHub Actions

A **chained-build harness** that compiles a full Android 10 custom ROM on
GitHub's free public-repo runners (4 vCPU / 16 GB RAM / ~6 h job wall) —
**total cost: ₹0** — by slicing the impossible 15-20 h build into resumable
160-minute slices whose progress is carried between runs by a
**Release-backed ccache**.

```
   ┌────────────────────────────────────────────────────────────────────┐
   │  one slice (≤ 5.6 h inside a 340 min job)                          │
   │                                                                    │
   │  restore source ─► restore ccache ─► BUILD ─► re-bank ccache ─► ✓  │
   │  (release)         (release)        (160 min)   (release)          │
   └────────────────────────────────────────────────────────────────────┘
          ▲                                            │
          └────────── next run resumes from cache ──────┘
   (schedule fires every 6 h; a CHAIN_PAT secret makes it instant)

   Final slice: `bacon` completes ─► ROM zip ─► release `rom` ─► workflow auto-disables
```

- **ROM**: QASSA 2.4 (Android 10 / Q) — `keepQASSA/manifest`, branch `Q`
- **Device**: Nokia 6.1 — `PL2` / `PL2_sprout` / `Plate2` (SDM660)
- **Lunch**: `qassa_PL2-userdebug`
- **Device repos**: `Zoro-15/{android_device_nokia_PL2, android_device_nokia_sdm660-common, android_kernel_nokia_sdm660, proprietary_vendor_nokia}` @ `lineage-17.1`

---

## Quickstart (5 steps, ~10 minutes of your time — the robots do the rest)

1. **Create the repo** — new **public** repo named `aosp-a10` (must be public:
   public repos get unlimited Actions minutes for free; private burns
   2,000 min/month and this build needs ~20+ hours).
   Push everything in this folder to the default branch.

2. **Allow releases** — repo *Settings → Actions → General → Workflow
   permissions* → select **Read and write permissions**.
   (The workflow already requests `contents: write`, but repo defaults vary —
   if a run fails with `gh: 403` on release upload, this is the switch.)

3. **Smoke-test the tree (recommended)** — *Actions → QASSA 2.4 … → Run
   workflow* → `build_target = bootimage`. ~1-1.5 h, proves the tree links.
   Optional but saves debugging later.

4. **Fire the real build** — `Run workflow` → `build_target = bacon`.
   - Bootstrap run syncs source (~1 h), snapshots it to the `source-cache`
     release, then builds its first cold 160-min slice.
   - Later runs restore source + ccache and keep building.
   - With no further input from you, the 6-hourly schedule converges the
     chain in **~1-2 days**. Add the optional `CHAIN_PAT` secret (below) and
     slices chain back-to-back — **~12-18 h** total.

5. **Collect the ROM** — done is when the **`rom`** release appears with your
   flashable zip + `SHA256SUMS`. The workflow disables itself at that point.

### Optional: instant chaining with `CHAIN_PAT`

The built-in `github.token` cannot trigger new workflow runs (GitHub's
anti-recursion rule), so by default the chain advances on the 6-hourly cron.
To make each slice re-dispatch the next one immediately:

1. GitHub → *Settings → Developer settings → Personal access tokens (classic)*
   → *Generate new*: scope **`workflow`** only. (Fine-grained tokens don't
   cover workflow dispatch for classic repos.)
2. Repo → *Settings → Secrets and variables → Actions* → new secret
   **`CHAIN_PAT`** = that token.

Every slice then dispatches the next one the moment it finishes uploading its
ccache.

---

## Repository layout

| Path | Role |
|---|---|
| `.github/workflows/build.yml` | The chained-build state machine (schedule + manual dispatch) |
| `config/build.env` | **Single source of truth** — ROM, device, budgets, paths |
| `config/device_repos.txt` | The 4 PL2 device repos (path\|url\|branch) |
| `config/local_manifests/qassa_pl2.xml` | Optional roomservice manifest (manifest mode) |
| `patches/device/qassa_PL2.mk` | QASSA product makefile (from the working Colab cell) |
| `patches/device/AndroidProducts.mk` | Product/lunch registry for the PL2 tree |
| `scripts/00_prepare_runner.sh` | Reclaims ~25 GB disk, adds 8 GB swap, installs JDK8/ncurses5 compat |
| `scripts/01_sync_source.sh` | Shallow `repo sync` + direct device clones + git-metadata strip |
| `scripts/02_apply_patches.sh` | Injects product mk files, validates `lunch` before burning hours |
| `scripts/03_build.sh` | **The slice engine** — setsid + process-group watchdog around soong_ui |
| `scripts/04_upload_cache.sh` | tar+zstd+split → Releases (`source-cache`, `ccache-cache`) |
| `scripts/05_restore_cache.sh` | sha256-verified restore of both caches (or signals "sync needed") |
| `scripts/06_publish_rom.sh` | Publishes ROM zip + checksums to the `rom` release |
| `scripts/lib.sh` | Shared helpers (logging, disk guards, release utilities) |

`TECHNICAL_REPORT.md` contains the full engineering analysis: constraint
math, storage/time ledgers, risk register, failure playbook, alternatives.

---

## Operating manual

**Watch progress**: each run writes a *job summary* (ccache hit stats, wall
time, slice classification). The interesting number is the ccache hit rate —
it climbs slice over slice until the link phase dominates and `bacon` lands.

| Action | How |
|---|---|
| Re-sync source after tree/manifest changes | Run workflow with `force_sync = true` |
| Drop a poisoned cache | Run with `fresh_ccache = true`, or delete the `ccache-cache` release |
| Full clean slate | Delete releases `source-cache` + `ccache-cache` + `rom` |
| Rebuild a new ROM version | Re-enable the workflow (Actions tab), delete the `rom` release, run `bacon` |
| Build something smaller | `build_target` = `bootimage` / `systemimage` / `nothing` (tree validation) |

**Expect:** bootstrap ~5 h → 2-4 chained slices → ROM. Wall-clock 18-36 h on
schedule-only, 12-18 h with `CHAIN_PAT`.

### Running on your own PC instead

The scripts are CI-agnostic — on any Ubuntu 20.04/22.04 box with ~200 GB free:

```bash
git clone <this-repo> && cd aosp-a10
bash scripts/00_prepare_runner.sh          # deps + swap (skips runner cleanup)
bash scripts/01_sync_source.sh             # full tree to ~/aosp
bash scripts/02_apply_patches.sh
BUILD_SLICE_SECONDS=99999999 bash scripts/03_build.sh bacon
```

(`gh` steps 04-06 need `gh auth login`; on a local box you usually skip them
entirely — the ROM lands in `~/aosp/out/target/product/PL2/`.)

---

## Ground rules (read once, thank yourself later)

- **Public repo or nothing.** The whole economics rest on free unlimited
  minutes for public repos.
- **Never commit proprietary blobs.** The harness pulls `vendor/nokia` from
  Zoro-15's existing public repo at build time; if your vendor ever goes
  private, clone it inside `01` via a PAT secret instead of committing it.
- **Scheduled workflows auto-disable after 60 days of repo inactivity** — any
  push resets the clock; the harness also disables itself after success.
- **Runner image is pinned to `ubuntu-22.04`** (jammy still ships
  `openjdk-8` + python 3.10, which the Android-10 build stack expects). If
  GitHub retires it, see the compatibility section of the technical report.
- **One build at a time** — a `concurrency` group serializes slices; queued
  runs wait, never cancel an in-flight slice.

## Troubleshooting quick table

| Symptom | Fix |
|---|---|
| `gh: 403` on release upload | Settings → Actions → Workflow permissions → Read and write |
| Sync 403/429 bursts | Lower `SYNC_JOBS` to 4 in `config/build.env` |
| Disk-critical abort | Lower `CCACHE_SIZE` to `8G`; re-run |
| `lunch` validation fails | Device tree broken — check `device_repos.txt` branches |
| Slice keeps dying at same file | Real compile error — open the log, fix the tree, `force_sync = true` |
| Restore sha256 mismatch | Transient asset corruption — delete the offending `*-cache` release, re-run |

---

*Harness MIT-licensed — see `LICENSE`. ROM and device sources belong to their
respective maintainers (QASSA team, Zoro-15, LineageOS, AOSP). Flash at your
own risk; always verify `SHA256SUMS`.*
