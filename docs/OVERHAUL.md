# ROMForge OVERHAUL — The v3.1 Extreme Systems Correction

```
Target repo      : zephyr4289/aosp-a10
Branch           : feat/romforge-v3 (HEAD 84d5721, as of 2026-10-09)
Scope            : Runs #58–#63 slot-1 failure chain (six consecutive crash loops),
                   the frozen campaign, the turbo lane, and the banking protocol
Evidence base    : 24 raw GitHub Actions job logs (runs #58–#63, ~6 MB),
                   the release store inventory (29 tags via API),
                   INDEX.json, the A17-experiment era logs (runs #54–#57),
                   and a 210,000-entry streaming tar scan of state-s2
Posture          : radical correction — the v3 blueprint was the right idea,
                   implemented against the wrong filenames, with a memory
                   shield that never reached the process it was built for
```

---

## 0. Executive Summary

Six dispatches, six dead slot-1s, one frozen campaign. Between 03:20 and 11:35 on 2026-10-09, runs #58 through #63 burned roughly **9.5 runner-hours on the critical-path slot alone** and produced **zero bytes of durable build progress**: the INDEX still points at `state-lineageosgoogleshibaa17-s2` (banked 2026-10-08 07:37), slice counter still reads 2, and the campaign is still 60.4% from done. The Direct Ninja Bypass — the centerpiece of the v3 blueprint, the thing that was supposed to delete the Soong memory bomb from every slot — has **never executed once**, in any run, on any job. That is not an exaggeration and it is not rhetoric; it is verifiable in three independent places: every slot log says `slice mode: soong (full soong_ui pipeline)`, the release store contains no `graph-*` tag, and the reason is a single hardcoded filename.

**The one-line cause**: this A17 tree (LineageOS 23.2, fused soong+ckati) emits its Soong ninja graph at `out/soong/build.lineage_shiba.ninja`, while `engine.bypass_ready()` gates on `out/soong/build.ninja` and `graph.bank_graph()` banks on `out/soong/build.ninja` — a file that does not exist in this source tree, and never will. Every downstream defense inherits the failure: the bypass silently falls back to the full `soong_ui` pipeline, which launches the fused `soong_build` monster with a **live set measured at 32.3–32.6 GiB** on a runner whose total memory capacity (15.6 GiB RAM + 17 GiB swap = 32.6 GiB) sits *exactly* at that number. The margin is zero. The kernel swap-thrashes for thirty minutes, the watchdog fires after the infra eviction is already in flight, the post-crash banking gets two of its required eight minutes, and the job dies with exit 143 having banked nothing. The next run restores the same state, hits the same filename, and dies the same death.

**The deeper pattern** this overhaul corrects is systemic: the v3 blueprint added five defenses (graph bypass, GOMEMLIMIT tuning, zram tier-1, cgroup envelopes, dual-condition watchdog) and **four of the five are structurally inert** — the bypass gates on a phantom filename, the Go runtime tuning is stripped by `soong_ui`'s own `env -i` hermetic launcher before it reaches `soong_build`, zram's `modprobe` fails on every GitHub runner with no zswap fallback, and the cgroup shield is gated behind `FORGE_CGROUP=1` which is never set anywhere in `forge.yml`. Only the watchdog fired, and it fired too late to matter. The machine was defended by air.

**What this document delivers**: a P0 patch stack (six changes, all small, all anchored to exact lines) that makes the bypass real, makes the graph bank exist, makes banking survive runner eviction, makes the memory shield actually shield, and unfreezes the INDEX; a P1 stack that repairs the turbo lane (invalid ninja goal names, swallowed error output, turbo-as-graph-minter); and a P2 stack that compresses the cycle time. With P0+P1 landed, the projected cold campaign is **3 fusion slots ≈ 16–17 hours** (down from the current *unbounded* crash loop, and down from the 12–18 h A17 baseline measured in the previous report), with the warm-campaign path at **2 slots ≈ 11 hours**, and the minter problem — who survives the 32.6 GiB analysis once, ever — solved by the turbo lane, which this evidence shows is the only job class on this repository that *has* survived the full fused analysis on a stock 4-vCPU runner (25 GiB swap headroom, 55 GiB disk free, zero out/ state to restore).

The physics that no configuration can escape, stated once and honestly: **`soong_build` for LineageOS 23.2/shiba needs ~32.6 GiB of anonymous memory, a stock GitHub runner offers 32.6 GiB total capacity, and a zero-margin system is a system that dies.** The fix is not to tune the monster. The fix is to never wake it up again after the first time it draws the graph.
---

## 1. The Canonical Sixty-Minute Crash — Run #63, Slot 1, candidate c11, second by second

This is the death of a perfectly healthy job on a perfectly healthy runner, reconstructed at 1 Hz from the raw log (job 113780189642, 2026-10-09, 10:34:25 → 11:35:02). Every other slot-1 failure in the six-run window is a variation of this same curve with the same ending.

| Clock | Elapsed | Event | Evidence |
|---|---|---|---|
| 10:34:25 | 0:00 | Runner up (Azure, ubuntu-24.04, EPYC 9V45 Zen 5) | job log header |
| 10:37:37 | 3:12 | btrfs zstd:1 volume mounted, 106 GiB cap | `storage v2: btrfs zstd:1 volume` |
| 10:37:38–10:49:26 | 3:13–14:61 | **src restore**: 17 parts, 32.56 GB, parallel streams | `[RESTORE] unpacked part n/17` |
| 10:50:47–10:50:53 | 16:22 | prebuilt chromium-webview arm64/arm fetched (98.6 MB) — **source-tree mtimes bumped here** | `restored prebuilt chromium-webview` |
| 10:50:54–10:58:14 | 16:29–23:49 | **out state restore**: 9 parts, 16.08 GB from `state-…-s2` | `out/ state restored from state-lineageosgoogleshibaa17-s2` |
| 10:58:15 | 23:50 | fusion slot loop opens: 320 m wall budget, slice 1 at 275 m | `Starting fusion slice 1` |
| 10:58:16 | 23:51 | `-j 8` selected (Zen 5 + AVX-512 + 22 GiB class) | `dynamic parallel jobs: -j 8` |
| 10:58:16 | 23:51 | **`slice mode: soong (full soong_ui pipeline)`** — the bypass refused, silently | engine log line |
| 10:58:28 | 24:03 | bootstrap graph enters: `[100% 1/1] bootstrap blueprint` — **1 edge**, i.e. tools up-to-date, only the soong regeneration edge is dirty | status line |
| 10:58:28–10:59:16 | 24:03–24:51 | `soong_build` heap ramp: RAM 2.3 → 13.1 GiB in **48 seconds**; swap opens | RAM telemetry |
| 10:59:16–11:02:06 | 24:51–27:41 | RAM pins at ~15.2/15.6; swap climbs 0.1 → 7.4 GiB | RAM telemetry |
| 11:02:06–11:30:34 | 27:41–56:09 | **The long thrash**: swap 7.4 → 17.0 GiB over 28 minutes with RAM pinned at 15.4–15.6/15.6; kswapd0 at full CPU; ninja progress frozen at `[100% 1/1]` the entire time | RAM telemetry |
| 11:30:34 | 56:09 | **Total saturation: RAM 15.6/15.6 + Swap 17.0/17.0 = 32.6 GiB** | RAM telemetry |
| 11:32:48–49 | 58:23 | memory watchdog finally fires: SIGINT to pgid → `soong_ui` dumps ~21,000 goroutines (tracer flush, NinjaReader close, a cosmetic shutdown panic) | goroutine dump block |
| 11:32:49 | 58:24 | `classification=mem-stall`, `stop_reason=memory` | `OUT:` lines |
| 11:32:53 | 58:28 | forensics dump printed; **banking begins**: `stream-packing out -> sink (zero staging)` | WARN/OK lines |
| 11:35:02 | 60:37 | **Infra eviction lands mid-bank**: `exit code 143`, "runner has received a shutdown signal" — ~2 of the ~8 minutes the 16 GB bank needed | `##[error]` lines |

The shape of the curve matters more than the timestamps. RAM reaches its ceiling in under a minute, and from there the process spends **thirty-two minutes doing nothing but page-faulting its own heap in and out of swap** — zero sub-status progress (`soong_build` never printed a single phase message: no "analyzing", no "glob"), zero ninja edges, zero disk growth. That is not a slow build; that is a system past its knee, burning wall clock at 100% CPU for negative progress. The slot then gets exactly two chances to salvage value — the watchdog and the bank — and loses both: the watchdog's trigger condition (§3, R5) is mathematically incapable of firing before the infra eviction, and the bank needs eight minutes on a runner that has two left.

Also note what is *not* wrong here: the restore pipeline (23:49 for 48.6 GB of src+state via the new parallel chunker) is fine; the mining matrix worked (c11 won the slot, 19 siblings fast-discarded); the volume had 9.4 GiB free the whole time; the fusion loop logic held. The v3 machinery around the build is healthy. Everything inside the `soong_ui` invocation is fatal.

---

## 2. The Six-Run Evidence Matrix

Every slot-1 job across the six dispatches, with the swap topology actually provisioned at runtime, the mode selected, and how it died. Duration is wall time from job start to terminal state.

| Run | Commit / era | Slot-1 job | Wall | Swap base | Total mem capacity | Mode | Terminal classification | Death mechanism |
|---|---|---|---|---|---|---|---|---|
| #58 | e37ca8c4 (pre-blueprint v3) | c16 | 34 m | +6 GB (target 8) | ~24.6 GiB | soong | error (job red) | swap exhaustion + eviction mid-bank |
| #59 | 2db4ead5 (blueprint P1–P4) | c11 | 32 m | +6 GB (target 8) | ~24.6 GiB | soong | cancelled (user killed after 32 m) | same trajectory, user aborted |
| #60 | d062dedb (AMMD restore) | c01 | 30 m | +6 GB (target 8) | ~24.6 GiB | soong | cancelled | same, user aborted |
| #61 | 88573f3 (user-space build) | c06 | 35 m | +6 GB → total 9.0 GiB | ~24.6 GiB | soong | **mem-stall** | watchdog fired, eviction won anyway |
| #62 | 73a1f94 (parallel chunker) | c15 | 69 m | +14 GB (target 16) → 17.0 GiB | 32.6 GiB | soong | **mem-stall** | 31 min thrash, SIGINT at saturation+120 s, exit 143 mid-bank |
| #63 | 84d5721 (GOMEMLIMIT=12 GiB, GOGC=60) | c06 | 57 m | 17.0 GiB | 32.6 GiB | soong | **mem-stall** | identical |
| #63 | 84d5721 (same, second slot-1 candidate) | c11 | 60 m | 17.0 GiB | 32.6 GiB | soong | **mem-stall** | the canonical crash of §1 |

Three structural facts fall out of this table. First, **death time scales with swap size and nothing else**: 24.6 GiB capacity dies at ~34 m, 32.6 GiB dies at ~57–69 m, with the same ~31-minute tail of thrash in both cases once the knee is crossed. No commit between #58 and #63 changed the outcome — the two memory commits (43b41e0, 84d5721) moved the death *later* without moving it *away*, exactly as predicted for a live set pinned at the capacity line. Second, **GOMEMLIMIT=12 GiB did nothing observable** — the c11 process still sailed straight through 12 GiB to 32.6 GiB, because the variable never reached it (§3, R2). Third, all six slots restored the *same* `state-…-s2` bank and all six re-ran the *same* soong analysis that the s2 state already contained a completed graph for — the bypass had every opportunity and took none of them.

The turbo lane in the same window tells the complementary story:

| Run | Turbo bootimage | Turbo vendorimage | Turbo dtboimage | Turbo vendor_boot |
|---|---|---|---|---|
| #58 (old code) | **1 h 03 m, success** (real build) | **1 h 44 m, success** (real build) | 47 m, failure | 49 m, failure |
| #59–#63 (v3) | 13–21 m, "success" — **cache skip**: `turbo state … exists — skipping` | 13–16 m, "success" — cache skip | 13–53 m, **failure** | 14–60 m, **failure** |

The apparent 5× turbo improvement in v3 is an illusion: bootimage and vendorimage succeed in minutes because their *turbo state releases already exist* (banked 2026-10-08 19:51 and 2026-10-09 04:27 by the run-#58-era real builds) and the turbo path short-circuits on `store.exists(tag)`. The two turbos that still actually build — dtboimage and vendor_boot — burn a full 36–50 minute fused analysis at ~9–12 GiB (they run with cold out/, 25 GiB swap after growth, and 55 GiB free disk) and then die on `ninja: unknown target 'dtboimage'` — an invalid goal name — with the actual error text swallowed because the turbo path never prints the build log on failure (§3, R8). So today the turbo lane is simultaneously: two fake successes, two deterministic failures, and — unnoticed by anyone — **the only jobs in this repository that have ever survived the full 32.6 GiB analysis on free-runner hardware**, which §5.2 turns from an accident into the graph-minter role.
---

## 3. Root-Cause Chain — R1 through R8

Eight findings, ordered by causal depth. R1 is the single bug without which none of the six runs die; R2–R6 are the reasons the other defenses did not save the day; R7–R8 are the loop-closers and the lane rot. Each is anchored to code, log, or store evidence collected in this session.

### R1 — The phantom filename: `out/soong/build.ninja` does not exist in this tree (THE bug)

The decisive evidence is the actual `soong_build` command line, recovered verbatim from run #62 slot-1 c15's crash forensics (and confirmed in run #61 c06):

```
[100% 2/2] analyzing Android.bp files and generating ninja file at
  /mnt/romforge/vol/aosp/out/soong/build.lineage_shiba.ninja
FAILED: /mnt/romforge/vol/aosp/out/soong/build.lineage_shiba.ninja
cd "$(dirname …/out/host/linux-x86/bin/soong_build")" … && env -i "$BUILDER" \
  --top "$TOP" \
  --soong_out out/soong --out out \
  --soong_variables out/soong/soong.lineage_shiba.variables \
  -o out/soong/build.lineage_shiba.ninja \
  --kati_suffix -lineage_shiba --kati_enabled \
  -l out/.module_paths/Android.bp.list \
  --available_env out/soong/soong.environment.available \
  --used_env out/soong/soong.environment.used.lineage_shiba.build \
  Android.bp
```

This A17/LineageOS 23.2 tree uses **per-target Soong graph names**: the graph is `out/soong/build.<product>.ninja`, the variables file is `out/soong/soong.<product>.variables`, and the used-env fingerprint is `out/soong/soong.environment.used.<product>.build`. Now read the two v3 functions that were written against the classic (pre-product-suffix) layout:

`forge_core/engine.py:219–262` — `bypass_ready()`:

```python
combined = sorted(out.glob("combined-*.ninja"))
soong_ninja = out / "soong" / "build.ninja"          # ← line 235: WRONG for this tree
kati = [p for p in out.glob("build-*.ninja")]
if not combined or not soong_ninja.exists() or not kati:
    return None                                       # ← line 237: SILENT refusal
```

`forge_core/graph.py:73–78` — `bank_graph()`:

```python
out_dir = build_root / "out"
soong_dir = out_dir / "soong"
if not (soong_dir / "build.ninja").exists():          # ← line 77: WRONG for this tree
    return False                                      # ← graph bank NEVER created
```

The consequences propagate everywhere at once. `bypass_ready()` fails G1 on every slot and every turbo, with **no log line** (the only G3 refusal path that logs is never reached), so `run_slice` prints `slice mode: soong (full soong_ui pipeline)` and re-launches the 32.6 GiB monster on a 32.6 GiB machine. `bank_graph()` returns `False` before doing anything on every call, so the `graph-<mhash>-<lunch>` release is never created — confirmed against the live store: 29 release tags exist, zero of them are `graph-*`. And `graph.restore_graph()` therefore always no-ops on `store.exists(tag)` for the turbo lane. The v3 graph machinery — `GRAPH_ENTRIES`, `GRAPH_OUT_PATTERNS`, `graph_fingerprint`, targets.txt capture — is complete, well-designed, and 100% dead, because its keystone path check looks for a file this fork cannot produce. One additional detail from the same command line: `--kati_enabled` means this `soong_build` is the **fused soong+ckati** build (Blueprint ASTs + Make module graph + packaging rules in one process), which is precisely why its live set (32.6 GiB) exceeds the classic A15-era soong_build measurements (~22–26 GiB) from the earlier campaigns.

### R2 — `env -i` strips every Go runtime variable: two memory commits are dead code

The launcher in `engine.py` exports `GOMEMLIMIT=12GiB`, `GOGC=60`, `GOMAXPROCS=4` (build_env, lines 131–138) into the environment of `soong_ui` — and `soong_ui` then invokes `soong_build` under `env -i` with **zero environment variables** (the command line above shows no assignments after `env -i`). This is soong's hermetic-build contract: the graph generator must not observe ambient environment. The practical consequence is that commit 84d5721's entire thesis ("set GOMEMLIMIT=12GiB, GOGC=60 … to eliminate AST swap thrashing") never had a mechanism: the tuned variables die at the `env -i` boundary, the process behaves exactly like an untuned one, and the telemetry proves it — the c11 heap crossed 12 GiB in 48 seconds without the slightest GC-assist flattening. Note this also means the *upward* tuning story is equally dead: no GOGC, GOMEMLIMIT, or GOMAXPROCS value set outside soong_ui can reach the process that matters, by design. Any real memory shaping of `soong_build` must come from **outside the process** (cgroups, swap topology, or patching the generated bootstrap ninja rule), which is exactly what §5.5 does.

### R3 — The physics: fused soong_build live set = 32.3–32.6 GiB, runner capacity = 32.6 GiB

Two independent measurements pin the live set. Slot-1 c11 (warm restore, 17 GiB swap): RAM pinned at 15.6/15.6 with swap filling to 17.0/17.0 — 32.6 GiB of anonymous footprint at the moment of death, still growing. Turbo dtbo (cold out/, 25 GiB swap after dynamic growth): the same analysis **completed**, peaking at RAM 15.3 + swap 17.1 = **32.4 GiB**, then released to 1.7 GiB RAM on exit. The fused generator needs ~32.6 GiB; the stock slot runner has 15.6 + 17 = 32.6. A zero-margin system does not degrade — it livelocks: kswapd0 takes a full CPU, the runner daemon's heartbeat starves, and the hypervisor issues the eviction SIGTERM that lands as exit 143. The turbo runner survived only because it entered the analysis with 55 GiB free disk, which let the dynamic-swap watchdog grow capacity to 40.6 GiB (R4). This number is the design constraint the whole overhaul respects: *no slot may ever enter the fused analysis with less than ~36 GiB of total memory capacity, and after the minter run, no slot should ever enter it at all.*

### R4 — The dynamic-swap gate starves exactly the jobs that need it

`engine.py:582` — the swap auto-scale watchdog grants a +2 GiB chunk only when `snap.physical_free_gb > 12.0`, up to 4 chunks. The turbo jobs (cold out/, 55 GiB free) sail through the gate and reach 25 GiB swap — which is why they survive R3. The critical-path slot restores 48.6 GiB of src+state onto the volume first, enters the analysis with **9.4 GiB physical free**, is refused every chunk, and dies at 32.6 GiB capacity — a margin of zero. The gate's intent (don't spend the disk that banking needs) is legitimate; its calibration (12 GiB free to grant a 2 GiB *sparse* swap file, which only consumes physical bytes as pages are actually written) is not. The slot needed one chunk — literally one 2 GiB grant — to match the turbo's survival envelope, and the gate said no.

### R5 — The watchdog fires after the eviction is already in the air

`engine.py:617` — the hard trigger is `(swap_full AND ram>96%) sustained 120 s`, polled every 2 s, where `swap_full` means >97% of swap consumed. Trace the c11 timeline: total saturation lands at 11:30:34; the earliest possible trigger is 11:32:34+; the SIGINT goes out ~11:32:48; the infra's shutdown signal arrives 11:35:02, i.e. it was dispatched while the watchdog was still in its 120-second *sustain* window. The design flaw is that the trigger threshold (97% swap + 96% RAM) is the point where the runner daemon is already starving — the eviction pipeline (missed heartbeats → hypervisor SIGTERM → runner drain) begins roughly 30–60 s after total saturation, so any watchdog that waits for *total* saturation plus two minutes of confirmation is a watchdog that arrives after the funeral. Worse, even when it does fire, the slot needs ~8 minutes to bank 16 GB, and has ~2. The watchdog must trip on the *approach* to the knee (PSI and fill-rate), not the arrival.

### R6 — Silent fallbacks: five features, five mute failures

Each of these was verified in this session's evidence and each one emitted nothing at the moment it failed: (a) `bypass_ready()` returns `None` at G1/G1b/G2 with no log line — the single most expensive silent `return` in the repository; (b) `bank_graph()` returns `False` at its first `exists()` check, silently, on every slot since the branch landed; (c) `ensure_zram()` fails (`modprobe zram` unsupported on the Azure runner kernel) and logs one WARN — but there is **no zswap fallback**, so the tier-1 memory shield silently degrades to disk-swap-only on every single job ("zram UNAVAILABLE on this runner", present in all 6 runs' turbo logs); (d) `_cgroup_run_prefix()` (engine.py:87–99) — the cgroup v2 envelope with per-phase `MemoryMax`/`MemorySwapMax` — is gated on `os.environ.get("FORGE_CGROUP") == "1"`, and **`FORGE_CGROUP` appears nowhere in `forge.yml`** (checked: the slot jobs set only `FORGE_UNTIL_BUDGET_S=19200` and `FORGE_CKPT_MIN=45`); (e) the turbo error path (cli.py:539–550) returns rc=1 without printing the build log — which is why dtbo/vendor_boot died for six runs with the actual `ninja: unknown target` message invisible (R8). The house rule this overhaul installs: **no defense may fail silently** — every gate logs its invariant, its verdict, and its fallback.

### R7 — The loop-closers: why the campaign is frozen and how the crash repeats forever

The store is the coordination truth, and it has been static since 2026-10-08 21:06: `INDEX.json` → `slice: 2, state_tag: state-…-s2, last_classification: sliced, stop_reason: budget`. Every v3 slot-1 restores s2 (16.08 GB, 10 assets), dies inside soong, and is evicted mid-bank — so no newer state is ever uploaded and the INDEX never advances. Three mechanisms then guarantee repetition. First, `dag.next_action()` (dag.py:34–68) has explicit cases for `done`, `no-builder`, `capacity`, `error`, and slice-cap — but **`mem-stall` is not among them**, so it falls through to the default "resume: dispatch next run" branch, meaning a mem-stall death is treated exactly like healthy progress and the conveyor (and the in-run slot-2 chain, whose gate is merely `needs.slot-1.result != 'cancelled'`) keeps spawning successors into the same wall. Second, the failed banks leave **orphaned partial releases**: `state-…-s3` currently holds 6 parts uploaded by *two different runs* (aa/ab/ac at 10:03–10:09 from run #63's slot-1, ad/ae/af at 07:48–07:52 from run #61's slot-1) with **no SHA256SUMS** — a Frankenstein bank that the fallback scanner (cli.py:562–567, which sorts tags descending, s3 before s2) would happily unpack if the primary restore ever failed. Third, the turbo lane's skip-if-bank-exists semantics (cli.py:530–533) turned run #58's real bootimage/vendorimage builds into permanent "successes" — so the lane reports green while contributing nothing new, and nobody notices that the only jobs capable of minting a graph are being wasted on cache hits.

### R8 — The turbo lane is aiming at targets that do not exist

`configs/roms/lineage-a17-shiba.yaml` declares `turbo.targets: [bootimage, vendor_boot, vendorimage, dtboimage]`. The combined ninja graph for this tree disagrees: turbo dtboimage in runs #61–#63 burns the entire 36–50 minute analysis and then dies with `FAILED: ninja: unknown target 'dtboimage', did you mean 'libimage'` — the goal is not a phony in the generated graph. vendor_boot dies in the same phase window with its error text swallowed by R6(e), and given bootimage/vendorimage (the two goals that *are* valid phony names) succeed once given real hardware, the working hypothesis — to be confirmed by the V3 validation step in §9 — is that the correct spellings are the classic make phony forms (`vendorbootimage` and the board-config-gated dtbo rule) or the `droid`-adjacent composite names. The deeper failure is structural: nothing validates goal names against the graph *before* committing a runner-hour to regenerate it, even though `graph.py` already captures a `targets.txt` (`ninja -t targets all`) alongside every banked graph — the validator was designed and never wired in.
---

## 4. The Store Autopsy — coordination state, exactly as it stands

The release store is the campaign's single source of truth, so the overhaul plan needs its exact current state on the table. Inventoried via API on 2026-10-09 ~12:00 UTC (29 tags, token-scoped read):

| Tag | Assets | Size | Last write | Meaning |
|---|---|---|---|---|
| `state-lineageosgoogleshibaa17-s2` | 10 | 16.08 GB | parts 2026-10-08 20:54–21:06 | **The frozen resume point**; INDEX points here |
| `state-lineageosgoogleshibaa17-s3` | 6 | 11.95 GB | aa/ab/ac 10-09 10:03–10:09 (run #63), ad/ae/af 10-09 07:48–07:52 (run #61) | **Frankenstein partial bank**: two different out/ trees, no SHA256SUMS — must be purged (P0-4) |
| `state-…-turbo-bootimage` | 2 | 0.56 GB | 2026-10-08 19:51 | real turbo build (run #58 era) |
| `state-…-turbo-vendorimage` | 3 | 3.84 GB | 2026-10-09 04:27 | real turbo build (run #58 era) |
| `src-1e29e74671baae8b` | 18 | 32.56 GB | 2026-10-08 09:53 | current source snapshot (mhash `1e29e74671baae8b`) |
| `lock-…-r<run>-s<n>` | 0 | 0 | 6 tags | slot lease markers (empty releases) |
| `forge-index` | 2 | ~1.1 KB each | 2026-10-08 21:06 | INDEX.json + .bak — **frozen** |
| `graph-*` | — | — | — | **DOES NOT EXIST** (the R1 casualty) |
| (legacy) `state-qassapl2a10-s10`, `rom-qassapl2a10`, `src-*` ×3, `source-cache`, `ccache-cache` | | 55+ GB | Sep 22 – Oct 8 | prior campaigns; `ccache-cache` (4.33 GB) is an A10-era ccache, unused by shiba |

Two properties of this table drive P0. The first is that **everything needed to unfreeze the campaign already exists inside s2** — the streaming scan of the s2 archive (210,984 entries walked before the session's scan budget expired) confirms the bank carries deep Soong state (`soong.environment.available` present at entry 685; the restored slots find warm `.bootstrap` tooling, hence the `[1/1]` bootstrap graph), and the s2 bank was cut from a build whose soong phase had completed — the per-target graph files are in the stream, merely named the way this tree names them. The filename fix alone converts s2 from "16 GB of dead weight every slot re-downloads and then re-derives" into "the graph cold-start the campaign already paid for". The second is that **the s2 tar preserves mtimes** (GNU tar semantics, verified by the warm-toolchain behavior), so the freshness problem for the bypass is not the archive — it is the three deliberate source-tree mutations every slot performs after restore (webview prebuilt fetch, device repo validation, patch application), which bump `.bp`/`.mk` mtimes to *now* and make both the G3 check and ninja's own restat regard the graph as stale. That is what P0-3 exists to end.

---

## 5. P0 — Make the Bypass Real (six patches, land as one PR series)

The P0 stack is ordered by dependency, not by size: P0-1 and P0-2 make the machinery exist, P0-3 makes it stay valid, P0-4 makes it survive eviction, P0-5 makes the one unavoidable analysis run survivable, P0-6 unfreezes the campaign. Everything else in the document is secondary to these six.

### 5.1 P0-1 — Filename discovery + loud gates (`engine.py`, ~30 lines)

Replace every hardcoded Soong-artifact name with discovery, and make every refusal speak. The tree's actual names — `out/soong/build.<product>.ninja`, `out/soong/soong.<product>.variables`, `out/soong/soong.environment.used.<product>.build`, `out/.module_paths/Android.bp.list` — are derivable from the lunch combo (`lineage_shiba-trunk_staging-userdebug` → product `lineage_shiba`), but globs are safer than derivation because they survive future renames:

```python
# engine.py — replaces the G1/G1b block in bypass_ready()
def _discover_graph(out: Path) -> Optional[Dict[str, Path]]:
    soong = out / "soong"
    graphs = sorted(soong.glob("build*.ninja"))          # build.ninja OR build.<product>.ninja
    combined = sorted(out.glob("combined*.ninja"))
    kati = sorted(p for p in out.glob("build-*.ninja"))
    if not graphs or not combined or not kati:
        log.warn(f"bypass G1 REFUSED: soong graph={[g.name for g in graphs]} "
                 f"combined={[c.name for c in combined]} kati={[k.name for k in kati]}")
        return None
    for required in (out / ".ninja_log", out / ".ninja_deps"):
        if not required.exists():
            log.warn(f"bypass G1b REFUSED: missing {required.name}")
            return None
    return {"soong": graphs[0], "combined": combined[0], "kati": kati[0]}
```

Every other exit in `bypass_ready()` gets the same treatment: G0 logs the kill-switch, G2 logs which ninja binary was searched where, and G3 logs the offending file, its mtime, and the graph's mtime instead of a bare number. The acceptance test is behavioral, not stylistic: **a slot log must never again contain the string `slice mode: soong (full soong_ui pipeline)` when a state bank was restored** — if the bypass is refused, the log must say which invariant failed and the job summary must surface it as a GitHub annotation. This is the difference between a six-run silent crash loop and a one-run loud misconfiguration.

### 5.2 P0-2 — Graph banking v2, and the turbo lane becomes the minter (`graph.py`, `cli.py`)

First, `GRAPH_ENTRIES`/`GRAPH_OUT_PATTERNS` (graph.py:23–33) move to glob-relative discovery — bank whatever `build*.ninja`, `soong*.variables`, `soong.environment*`, `.bootstrap/`, `.glob/`, `.module_paths/Android.bp.list` actually exist, plus `combined*.ninja`, `build-*.ninja`, `.ninja_log`, `.ninja_deps` from the out root, plus the fingerprint and `targets.txt`. The `bank_graph()` keystone check becomes `any(soong_dir.glob("build*.ninja"))` instead of `(soong_dir / "build.ninja").exists()` (graph.py:77). The bank_graph call site in `cli.py:414–421` stays where it is (it runs after every main-chain slice regardless of classification — correct design, wrong filenames until now).

Second — and this is the structural insight this session's evidence buys — **add the graph-minting call to the turbo path** (cli.py:526–550), because the turbo jobs are the only execution environment on this repository that has *ever* completed the 32.6 GiB fused analysis on free-tier hardware (R3: 25 GiB swap, 55 GiB disk, cold out/). The mint flow: one turbo job (vendor_boot, after P1-1 fixes its goal name) runs the full pipeline once, and `cli.py`'s turbo branch calls `graph.bank_graph(build_root, store, mhash, plan.rom.lunch)` immediately after `run_slice` returns — minting the `graph-<mhash>-<lunch>` release even if the partition target itself fails (the graph is generated *before* ninja rejects the goal). One turbo job-hour buys every future slot out of the 32.6 GiB gauntlet permanently. The cold main-chain slot remains a fallback minter — with P0-5's swap recalibration, a *cold* slot (no 16 GB out-state to restore, ~55 GiB disk free at analysis time) has the same survival envelope as today's turbo jobs.

Third, make `restore_graph()` loud on every failure mode it currently swallows (`store.exists` false → log "graph bank absent — this slot will mint or run full soong").

### 5.3 P0-3 — Mtime normalization: the enabler nobody wired (`cli.py`, `syncer.py`, ~60 lines)

Even with correct filenames, two freshness checks will refuse a perfectly good restored graph: v3's own G3 (`find … -name Android.bp -printf '%T@'` vs graph mtime) and — more fundamentally — **ninja's own restat**, because the combined graph embeds the soong regeneration edge whose recorded input mtimes (in `.ninja_deps`) predate the slot's deliberate source-tree mutations. Every slot bumps a handful of `.bp`/`.mk` files to *now*: the chromium-webview prebuilt fetch (10:50:47 in the canonical crash), device-repo shenanigans, and `apply_patches`. Content is byte-identical to what the graph was generated from (same src snapshot, same deterministic patch set) — only mtimes lie.

The fix is a **deterministic-tree discipline**: the src bank ships an mtime manifest, and every post-restore mutation re-stamps the files it touched back to the manifest's recorded mtimes.

```python
# syncer.py — new
def stamp_manifest(build_root: Path, mhash: str) -> None:
    # cut once at src-bank creation: find <tree> \( -name Android.bp -o -name Android.mk \)
    #   -printf '%P\t%T@\n' > src-mtimes-<mhash>.txt   (~4 MB, 80k lines)
    ...

def normalize_tree_mtimes(build_root: Path, mhash: str) -> int:
    # after apply_patches + prebuilt fetches: re-stamp every listed path to its
    # recorded mtime (xargs -P4 touch -d @<ts>). Content-identity makes this sound:
    # the graph was generated from this exact (src-snapshot + patch-set) content.
    ...
```

`cmd_slice` calls `normalize_tree_mtimes` after `syncer.apply_patches` and before the state restore, so by the time `bypass_ready()` runs, the source tree is mtime-indistinguishable from the tree the graph was generated from — G3 passes, ninja's restat passes, and the frozen graph executes. As defense in depth, the fingerprint check in `graph.py` moves from `[size, mtime_ns]` to content hash (xxh64 over the `.bp`/`.mk` set, ~30–40 s for ~5 GB of text) with mtime as the fast pre-filter. The invariant to hold forever after: **the patched source tree is byte- and mtime-deterministic across every slot of a campaign** — same bytes in, same bytes out, same graph valid. (Note the corollary: the launcher's exported env must also be deterministic slot-to-slot, because `soong.environment.used.<product>.build` fingerprints the variables soong cares about; the current hardening commits 88573f3/d062ded export a stable set, which P0-3 locks in as a regression test.)

### 5.4 P0-4 — Banking that survives eviction (`relay.py`, `cli.py`, `store.py`)

The eviction race (R5's second half) is a protocol problem, and it has a protocol answer. Four changes, in landing order:

1. **Graph-first flush.** On any watchdog stop (mem-stall, budget, disk), the first thing banked is the *cheap critical set*: `out/soong/**` + `out/.ninja_log` + `out/.ninja_deps` + `out/.module_paths/**` + `out/combined*.ninja` ≈ 1.5–3 GB ≈ 40–90 s of upload — before the full 16 GB `stream-pack` begins. Even when the runner dies mid-bank two minutes later (as c11 did), the next slot gets a usable graph and the campaign advances. Implementation: a `relay.bank_critical(build_root, store, tag)` called at the top of the error/sliced path, writing parts named `crit.part.*` into the same state tag; `relay.restore` checks for and applies the critical parts first.
2. **Manifest-first, sums-last is a bug — make it manifest-first, sums-first-too.** Today `stream_pack` uploads `SHA256SUMS` as the *final* asset, so every interrupted bank leaves a tag that looks complete-minus-nothing and is actually complete-minus-everything (the s3 Frankenstein). The release gains a `MANIFEST.json` asset uploaded *before* the parts (expected part names, sizes, and a nonce for the banking slot), each part upload `--clobber`s its slot, and `restore` refuses any tag whose manifest's expected-part set is not exactly the present-asset set. Retroactively: **purge `state-…-s3` now** (it can never restore — no sums — but it poisons the fallback scanner's newest-first ordering), and make `store.list_tags`-driven fallback skip any `state-*` tag without a valid manifest.
3. **Delta banking.** The 8-minute full re-pack is 16 GB of mostly-unchanged bytes. With P0-3's mtime discipline, "changed since the restored bank" is a well-defined set: walk `out/` for files with mtime > bank-restore timestamp (plus the always-hot set: `.ninja_log`, `.ninja_deps`, product artifacts), and stream-pack only those as `state-…-sN.delta`. The restore path applies the base bank then the delta chain. A healthy slot's bank drops from ~8 min to ~1–2 min, which also shrinks `BANK_RESERVE_S` (cli.py:581) from 30 min to ~10, giving each fusion slot 20 more minutes of build per 5h20 wall.
4. **Purge discipline.** Every slot start GCs orphaned partials: tags matching `state-<key>-s*` that lack manifests and predate the current run by more than one dispatch cycle get deleted by the plan job (which already holds `contents: write`).

### 5.5 P0-5 — The memory shield that actually shields (`env.py`, `engine.py`, `forge.yml`)

Five recalibrations, all of them grounded in this session's measurements rather than the blueprint's assumptions:

1. **zswap fallback (new, env.py).** When `ensure_zram()` fails — and on GitHub's Azure kernels it fails every time — enable the kernel's in-RAM swap compression instead, which requires no module and is present on stock Ubuntu 24.04 kernels: `echo 1 > /sys/module/zswap/parameters/enabled`, `echo lz4 > …/compressor`, `echo 25 > …/max_pool_percent`. With swap at 17 GiB and typical AST-page compressibility (~2.5–3.5×), zswap buys ~10–14 GiB of *effective* swap-path capacity for free — enough by itself to move the 32.6 GiB analysis from zero-margin to survivable-margin on the minter run. Log the topology line either way; the degraded state must never again be a one-line WARN nobody reads.
2. **Swap growth recalibration (engine.py:582).** Gate chunk grants at `physical_free_gb > 6.0` (not 12.0), raise the chunk cap from 4 × 2 GiB to 8 × 2 GiB, and trigger growth at `sw_used_pct > 55` (not 70). The 2 GiB chunks are sparse files — they consume physical bytes only as pages land — so a 6 GiB floor still leaves banking its air supply while granting the slot the headroom the turbo lane already proved sufficient. Target standing capacity: **≥ 36 GiB** (R3's live set + 10%).
3. **Watchdog early trigger (engine.py:617).** Replace the saturation-confirmation condition with knee-anticipation: fire when `psi_full_avg60 > 40` for 90 s, OR `swap_used > 80% and ram > 90%` for 60 s, OR a fill-rate extrapolation says "swap saturates within 6 minutes" (`(swap_total−swap_used) / max(1e-6, d(swap_used)/dt)`). The goal is to stop the process while the runner daemon still has a heartbeat, leaving ≥ 5 minutes for the P0-4 graph-first flush — the difference between c11's zero banked bytes and a campaign that advances on every slot.
4. **Anti-livelock scope instead of dead env tuning (engine.py:87–99).** Wire `FORGE_CGROUP=1` into the slot jobs' env — but scope the envelope correctly this time: the **exec phase** (ninja, −j8, ~13 GiB RSS of clang/javac workers) gets `MemoryMax=13G MemorySwapMax=6G`; the **analysis phase** gets no `MemoryMax` at all (R2/R3: capping a 32.6 GiB live set at any lower number is just a slower death) but instead a `MemoryHigh=16G` throttle scope — `memory.high` makes the kernel reclaim *inside* the process (throttling it) rather than globally (starving the runner daemon), which converts the hard livelock into a slow-but-alive process that the early watchdog can then stop cleanly. This is the honest version of what the GOMEMLIMIT commits intended: shaping from outside the `env -i` boundary, where it actually works.
5. **Keep, but re-home, the honest bits.** `protect_runner_processes` (oom_score_adj −1000) stays — it is why the kernel OOM killer never fired and the infra eviction did; with the early watchdog, that shielding becomes correct rather than tragic. The JVM heap cap (`-Xmx2560m`, engine.py:128) stays for the exec phase. The `ALLOW_MISSING_DEPENDENCIES=true` default (engine.py:126–127 + rom config) gets scoped **out of the final packaging slices** — globally-set AMMD ships stubbed dependencies into the release zip, a correctness hazard the gate cannot see (the previous report's finding, still unfixed, now carrying a slot-1 crash-loop discount: with the bypass live, AMMD is only needed for the turbo partition warmups where it belongs).

### 5.6 P0-6 — DAG: give mem-stall a voice, and unfreeze the campaign (`dag.py`, runbook)

`dag.next_action()` gains an explicit `mem-stall` case: re-dispatch **once** (the next slot may land better hardware or a banked graph), then fail red with "memory envelope exhausted — mint graph via turbo or grow capacity". Without this, the conveyor treats a six-times-repeated fatal condition as resumable progress forever. The one-time INDEX surgery that unfreezes the campaign is a two-command runbook (executed by the plan job or manually via `gh`): copy the s2 tag's assets to a fresh `state-…-s2` manifest-first bank (P0-4's format), rewrite `INDEX.json` with `slice: 2, state_tag: <new>, last_classification: sliced`, and delete the s3 Frankenstein — after which the first dispatched slot restores, discovers the graph with P0-1 names, normalizes with P0-3, and executes `ninja -f out/combined-lineage_shiba.ninja -j 8 bacon` without ever launching soong_ui.
---

## 6. P1 — Repair the turbo lane (the lane that minted nothing and lied green)

The turbo lane is currently two fake successes and two deterministic failures (§2's table), and it is also the repository's only proven survivor of the fused analysis. P1 makes it honest and then makes it load-bearing.

**P1-1 — Goal validation against the banked graph (cli.py turbo branch).** Before dispatching a turbo job to a 60-minute analysis, validate the goal name against reality: `ninja -f <combined> -t targets rule | grep -w <goal>` (and the `all` flavor for file targets), using the `targets.txt` captured alongside every banked graph (graph.py already writes it — another designed-but-unwired validator). Invalid goals get name-corrected from the suggestion list (this session's evidence: `dtboimage` unknown, suggestion `libimage`; the expected real spellings to confirm in V3 are `vendorbootimage`, the board-gated dtbo rule, or partition-file targets like `out/target/product/shiba/dtbo.img`), and the plan job fails fast with a config lint instead of a runner-hour. Add the same lint to `cmd_plan` so a typo'd `turbo.targets` entry is a 5-second red, not a 50-minute one.

**P1-2 — Turbo forensics parity (cli.py:539–550).** The turbo branch gets the same last-100-lines dump the main-chain error path has had since the forensics fix — copy the `_run_single_slice` block (cli.py:461–470) into the turbo error path. Six runs of `classification=error` with zero error text is six runs of debugging by vibes; this ends it.

**P1-3 — Turbo-as-minter (already specified in P0-2, called out here for the lane's own contract).** The turbo job's success criterion splits: `graph minted` (bank exists + fingerprint matches) and `partition built` (turbo state banked). A turbo job that mints the graph but fails its partition is a **partial success** — green with a note, not red — because its graph output is now the campaign's most valuable artifact. The postcheck reports both facts distinctly, so the lane stops conflating "skipped because done" with "did work" (the current cache-skip illusion from §2).

**P1-4 — Honest skip semantics.** `turbo state exists — skipping` currently returns before the mode line is even printed, which is why the lane's logs show nothing at all for bootimage/vendorimage. The skip path logs `turbo <goal>: banked <date>, skipping rebuild (pass --force to rebuild)` and the postcheck distinguishes skipped-vs-built in its summary. Cosmetic, but it is precisely the illusion that hid the dtbo/vendor_boot failures for six runs behind two green neighbors.

---

## 7. P2 — Cycle compression (restore, relay, ccache, slot sizing)

With P0 landed, the slot's critical path becomes: restore (21 min) → graph restore/validate (~1 min) → ninja parse (~3–5 min for the combined graph + deps) → ninja exec → bank. The remaining levers, in impact order:

**P2-1 — Overlap the src and state restores.** Today they are strictly serial: 12 min src parts, then 7.3 min out parts (canonical c11 timeline, 10:37:38→10:58:14). They are independent downloads writing independent trees; running both part-streams concurrently (the parallel chunker already parallelizes *within* a bank — this is just running two bank-restores in two threads) cuts the fixed slot tax from ~21 min to ~13 min. Every slot of the campaign pays this, so the campaign saves ~8 min × N slots.

**P2-2 — Delta banking (specified in P0-4.3).** Listed here for its cycle-time value: the healthy-slot bank drops from ~8 min to ~1–2 min, and `BANK_RESERVE_S` drops 30→10 min, which is +20 min of build per slot. On an 8-slice campaign that is +2.7 slot-hours of pure ninja throughput.

**P2-3 — ccache for the native lanes.** The store already carries the pattern (`ccache-cache`, 4.33 GB, A10 era) — the shiba campaign never wired it. Enable `USE_CCACHE=1` in the rom config with `CCACHE_DIR` on the volume, bank it as `ccache-<mhash>` (content-addressed by construction, so it is already delta-friendly), and cap it at ~6 GB to respect volume budget. Expected native compile hit-rate after the first slice: 25–40% (AOSP's own historical numbers for repeat-builds-with-warm-graph), worth ~30–60 min per warm slice at the measured 3.2–3.4 edges/s exec rate.

**P2-4 — Fusion-slice sizing.** `MIN_SLICE_S = 1800` (cli.py:582) exists to prevent a slice so short it can't reach a consistent state; with the bypass active and banking at 1–2 min, the floor drops to ~900 s and the loop can run 3 ninja slices per 5h20 wall instead of 2 — tighter cadence, less per-slice overhead amortization loss, and the conveyor's re-dispatch latency (a full workflow_dispatch round-trip) stops being paid twice per slot-night.

**P2-5 — The 20-lane shard (deferred, and why).** The previous blueprint's Dilemma-C endgame — partition the ninja graph across ~18 parallel slots, each building a disjoint goal-set from the same graph bank, with an assembler slot merging images — remains the only path to a sub-2-hour wall on free-tier hardware. It is deferred behind P0/P1 deliberately: each shard slot still needs the ~13-min restore tax and a 1–2 GB state bank per slice, so 20 lanes × 16 GB of out-state relay per cycle is ~320 GB of release traffic — the relay, not the compiler, becomes the bottleneck. The prerequisite is the CAS-Relay (content-addressed part store with cross-slot dedup) from the v3 blueprint, which P0-4's manifest-first format is the on-ramp for. Ship P0, measure, then decide whether the 16–17-hour cold campaign justifies the shard complexity or the fleet tier (FLEET_SETUP.md) absorbs it.

---

## 8. The Honest Math — projected timelines

Grounded in the two measured constants of this campaign: **ninja exec throughput 3.2–3.4 edges/s at −j8 on Zen 5** (82,735-goal graph ⇒ ~7.0–7.2 h of pure exec) and **fused soong_build analysis 36–50 min when the memory envelope permits** (turbo-observed; 60+ min to death when it does not). Slot wall is 5 h 20 m (FORGE_UNTIL_BUDGET_S=19200) minus restore minus banking.

| Scenario | Soong analysis | Ninja exec | Slots | Projected wall | vs. today |
|---|---|---|---|---|---|
| **Today (measured)** | dies at 60 m, ×∞ | never reached | ∞ (crash loop) | **unbounded** | — |
| **P0 only (cold campaign)** | 1× minter slot (~45 m, zswap+swap-grown envelope) + 0 in later slots | 7.0–7.2 h across slices | 3 fusion slots | **~16–17 h** | unfreezes the campaign |
| **P0 warm (graph banked)** | 0 (bypass) | 7.0–7.2 h | 2 fusion slots | **~11 h** | −35% vs cold |
| **P0+P1 (turbo prewarm overlapped)** | 0 in main chain (turbo minted it) | ~6.0–6.5 h effective (turbo partitions prebuilt during slot-1) | 2 fusion slots | **~9.5–11 h** | prewarm overlap |
| **P0+P1+P2 (ccache + delta + overlap)** | 0 | ~4.5–5.5 h (25–40% native hit + tighter cadence) | 2 fusion slots | **~7.5–9 h** | relay no longer the tax |
| **+ 20-lane shard or fleet (P3)** | 0 | parallelized | 1 build wave + assembler | **~2–3 h** (fleet: ~1.5 h) | requires CAS-Relay / hardware |

The previous report's ceiling analysis stands unchanged: a sub-2-hour *cold* campaign on free-tier 4-vCPU runners remains physically impossible (the 2.5–3 h serial floor in image packaging and the ~26-core-seconds-per-edge arithmetic have not moved), which is why the table's last row carries its prerequisites. What P0 buys immediately is something more urgent than speed: **the campaign ends**. A 16-hour campaign that terminates is infinitely faster than a 60-minute crash that repeats forever.

---

## 9. Validation Ladder — prove each fix before stacking the next

Each step is a cheap, decisive experiment with a binary observable, ordered so that each fix's success is a precondition for testing the next. All of them fit inside normal dispatch cycles; none needs a new workflow.

- **V1 — Discovery truth (5 min, no runner).** On any machine with the repo: assert `bypass_ready`'s discovery glob finds artifacts in a listing of the s2 tar (this session's scanner output doubles as the fixture). Pass: the discovered names include `build.lineage_shiba.ninja` and a `combined*.ninja`. Fail mode caught: glob too narrow for future renames.
- **V2 — The loud gate (one dispatch).** Dispatch a run with the bypass kill-switch *on* but against a deliberately graph-less cold out/. Pass: the log says exactly which G-invariant refused and the job surfaces an annotation. Fail mode caught: any remaining silent `return None`.
- **V3 — The minter mints (one turbo dispatch).** Dispatch turbo vendor_boot with P1-1's corrected goal. Pass: job survives analysis at ≤ 40.6 GiB capacity, `graph-<mhash>-<lunch>` release appears with `MANIFEST.json` + `targets.txt`, and the postcheck reports `graph minted` even if the partition goal still fails. Fail mode caught: swap-growth miscalibration (fix: P0-5.2 constants).
- **V4 — The bypass engages (one slot dispatch).** After V3, dispatch slot-1. Pass: log contains `slice mode: ninja-direct`, the ninja parse completes in < 6 min, and the first fusion slice banks a manifest-complete state. Fail mode caught: mtime normalization gaps (compare `soong.environment.used` fingerprints across slots).
- **V5 — The eviction race is closed (one forced crash).** Force a mem-stall (FORGE_SOONG_MEM_LIMIT high, swap grown small) and watch the graph-first flush land before the infra SIGTERM. Pass: the post-crash tag contains `crit.part.*` + manifest, and the next slot advances without re-running soong.
- **V6 — Campaign completion (the real test).** With V1–V5 green, run the campaign end-to-end: expect INDEX `slice` to advance on every slot, ~3 slots cold / 2 warm, `done=true`, verify+publish green under the 14-point gate.
- **V7 — Regression guard (harness).** Add to `harness/`: a unit test pinning the discovery globs against a fixture tree with per-target names, a `dag.next_action` test asserting the `mem-stall` → dispatch-once-then-fail transition, and a manifest-integrity test that refuses the s3-style partial bank.

---

## 10. Risk Register & Kill Switches

Every change in this document ships with an off-switch, because the conveyor's survival depends on being able to retreat to known behavior at 03:00 UTC when something regresses.

| ID | Change | Primary risk | Mitigation | Kill switch |
|---|---|---|---|---|
| K1 | P0-1 filename discovery | glob matches a stale/hand-edited graph | fingerprint check (P0-3) gates use | `FORGE_NINJA_BYPASS=0` (existing, now actually tested) |
| K2 | P0-2 turbo minting | turbo runner dies before bank_graph (its error path currently banks nothing) | graph-first flush inherited from P0-4 | mint is additive; absence of graph-* degrades to today's behavior |
| K3 | P0-3 mtime normalization | a *genuinely* changed .bp is re-stamped stale → stale graph executes | content-hash fallback in fingerprint; hash mismatch forces full soong (loud) | normalization off → G3 behaves as today (refuse + log) |
| K4 | P0-4 delta banking | base+delta chain diverges from a single-tree state | manifest carries parent hash; mismatch falls back to full bank | `FORGE_DELTA_BANK=0` |
| K5 | P0-5 zswap | zswap compressor unavailable on some kernels | topology report logs it; falls back to plain swap | `FORGE_ZSWAP=0` |
| K6 | P0-5 MemoryHigh throttle scope | throttling slows an otherwise-fine analysis on big-memory fleet nodes | scope only applies when total capacity < live-set + margin (auto-detected) | `FORGE_CGROUP=0` (existing) |
| K7 | P0-5 swap gate 6 GiB | swap growth collides with banking's disk need | sparse chunks + graph-first flush is small; disk watchdog still guards | revert constant to 12 |
| K8 | P0-6 mem-stall DAG case | a transient mem-stall halts the campaign red | dispatch-once semantics + postcheck reason string | revert dag.py case (falls through to today's resume) |
| K9 | P1 goal rewrites | wrong substitute goal prewarms the wrong partition | V3 validates against targets.txt before committing | turbo targets live in rom config (one-line revert) |
| K10 | P2-3 ccache | cache volume pressure on the 106 GiB volume | 6 GB cap + volume watchdog | `USE_CCACHE=0` in rom config |

The standing rule for all of them: **a kill switch is only real if it has been exercised** — V2 and V5 both deliberately walk at least one switch to its failure mode before the campaign depends on the feature it guards.
---

## Appendix A — Raw evidence excerpts (verbatim, as collected)

**A.1 — The mode-selection failure, six for six** (one line per run, from the slot-1 job logs):

```
run58_slot1_c16 : (A17-era code, no mode line — full soong pipeline by construction)
run59_slot1_c11 : [07:08:25 OK slice mode: soong (full soong_ui pipeline)
run60_slot1_c01 : [07:2x:xx OK slice mode: soong (full soong_ui pipeline)
run61_slot1_c06 : [07:5x:xx OK slice mode: soong (full soong_ui pipeline)
run62_slot1_c15 : [09:33:20 OK slice mode: soong (full soong_ui pipeline)
run63_slot1_c06 : [10:5x:xx OK slice mode: soong (full soong_ui pipeline)
run63_slot1_c11 : [10:58:16 OK slice mode: soong (full soong_ui pipeline)
```

No "bypass:" refusal line appears in any of them — the G1/G1b/G2 refusals are mute by construction (engine.py:229–245). The turbo dtbo logs contain the same mode line for the same silent reason, plus the graph-bank absence.

**A.2 — The phantom filename, in soong's own words** (run #62 slot-1 c15, 10:01:05, and confirmed in run #61 c06):

```
10:01:05 soong bootstrap failed with: signal: killed
[100% 2/2] analyzing Android.bp files and generating ninja file at
  /mnt/romforge/vol/aosp/out/soong/build.lineage_shiba.ninja
FAILED: /mnt/romforge/vol/aosp/out/soong/build.lineage_shiba.ninja
cd "$(dirname "/mnt/romforge/vol/aosp/out/host/linux-x86/bin/soong_build")" && \
  BUILDER="$PWD/$(basename "…/soong_build")" && cd / && env -i  "$BUILDER" \
  --top "$TOP" --soong_out "…/out/soong" --out "…/out" \
  --soong_variables …/out/soong/soong.lineage_shiba.variables \
  -o …/out/soong/build.lineage_shiba.ninja \
  --kati_suffix -lineage_shiba --kati_enabled \
  -l …/out/.module_paths/Android.bp.list \
  --available_env …/out/soong/soong.environment.available \
  --used_env …/out/soong/soong.environment.used.lineage_shiba.build  Android.bp
error: action cancelled when ninja exited
```

Read twice, note three things: the graph filename (`build.lineage_shiba.ninja`), the fused kati (`--kati_enabled`), and the empty environment (`env -i` with no assignments — R2's mechanism).

**A.3 — The death curve** (run #63 slot-1 c11, 30-second samples from the 1 Hz telemetry; full series in the job log):

```
[11:00:00] RAM: 12.7/15.6G (Swap:  2.2/17.0G)   ← knee passed
[11:01:03] RAM: 13.9/15.6G (Swap:  6.1/17.0G)
[11:02:06] RAM: 15.2/15.6G (Swap:  7.4/17.0G)   ← RAM pinned from here
[11:08:25] RAM: 15.2/15.6G (Swap: 10.6/17.0G)
[11:13:42] RAM: 15.2/15.6G (Swap: 13.4/17.0G)   ← 32 min of thrash begins
[11:24:45] RAM: 15.5/15.6G (Swap: 15.2/17.0G)
[11:30:34] RAM: 15.6/15.6G (Swap: 17.0/17.0G)   ← total saturation, 32.6 GiB
[11:32:48] watchdog SIGINT (mem-stall)           ← eviction already dispatched
[11:35:02] exit code 143                         ← mid-bank, 2 of ~8 min used
```

**A.4 — The turbo survivor's curve** (run #63 turbo dtbo, same runner class, cold out/): analysis phase peaks at RAM 15.3 + Swap 17.1 = 32.4 GiB *with 25 GiB swap available* (dynamic growth granted — 55 GiB disk free), completes, hands off to kati at RAM ≤ 12.5 GiB, then dies on the goal name:

```
[11:28:05] FAILED: ninja: unknown target 'dtboimage', did you mean 'libimage'
[11:28:06] OUT: classification=error                ← with zero error text in the job log
```

**A.5 — The store's frozen coordination state** (INDEX.json, retrieved 2026-10-09 ~12:00 UTC):

```json
"lineageosgoogleshibaa17": {
  "done": false, "last_classification": "sliced", "slice": 2,
  "src_tag": "src-1e29e74671baae8b",
  "state_tag": "state-lineageosgoogleshibaa17-s2", "stop_reason": "budget"
}
```

Unchanged since 2026-10-08 21:06 — i.e., before any of the six crash-loop runs. The s2 release's parts carry upload timestamps of 2026-10-08 20:54–21:06; the s3 release carries parts from two different runs (07:48–07:52 and 10:03–10:09) with no SHA256SUMS — the Frankenstein bank of R7.

**A.6 — The state-s2 content scan** (streaming tar listing, 210,984 entries walked): confirms deep Soong state in the bank (e.g. `out/soong/soong.environment.available`, 15,397 bytes, at entry 685, with warm `.bootstrap` tooling implied by the restored slots' `[1/1]` bootstrap graph) and a walk order dominated by `out/soong/.intermediates/**` (hundreds of thousands of compiled objects). The scan budget expired before the per-target graph files' offset; their presence is instead attested by the bank's provenance (cut from a soong-complete build) and by the warm-toolchain behavior of every restoring slot.

**A.7 — The shield that wasn't there** (grep of the workflow, and of every job log's environment):

```
$ grep -n FORGE_CGROUP .github/workflows/forge.yml .github/workflows/slot.yml
(no matches — the cgroup envelope never activates)
$ grep -hoE "FORGE_[A-Z_]+" <all 24 job logs> | sort -u
FORGE_UNTIL_BUDGET_S  FORGE_CKPT_MIN               (only)
[zram UNAVAILABLE on this runner — memory shield degraded]   (every run)
```

---

## Appendix B — Patch-by-patch file map

The complete P0/P1 surface, as it would land on `feat/romforge-v3` (one PR per row, sized for review):

| PR | Files touched | Net lines | Depends on | Validates via |
|---|---|---|---|---|
| pr-01 discovery+loud-gates | `forge_core/engine.py` (bypass_ready, ~219–262) | +34 −8 | — | V1, V2 |
| pr-02 graph-banking-v2 | `forge_core/graph.py` (GRAPH_ENTRIES, bank_graph, restore_graph) | +40 −14 | pr-01 | V3 |
| pr-03 turbo-mint+forensics | `forge_core/cli.py` (turbo branch 526–550) | +55 −6 | pr-02 | V3 |
| pr-04 mtime-normalization | `forge_core/syncer.py` (+stamp_manifest, +normalize_tree_mtimes), `forge_core/cli.py` (call site), `forge_core/graph.py` (hash fingerprint) | +80 −4 | pr-02 | V4 |
| pr-05 manifest-first banking | `forge_core/chunker.py`, `forge_core/relay.py` (bank_critical, manifest), `forge_core/store.py` (tag GC) | +120 −18 | — | V5 |
| pr-06 shield-v2 | `forge_core/env.py` (zswap, ensure_swap growth), `forge_core/engine.py` (watchdog 593–626, swap gate 582, cgroup scoping), `.github/workflows/forge.yml` (FORGE_CGROUP=1) | +95 −22 | — | V3, V5 |
| pr-07 dag-mem-stall + INDEX surgery | `forge_core/dag.py` (34–68), runbook in `docs/` | +30 −2 | pr-05 | V6 |
| pr-08 turbo goal lint | `forge_core/cli.py` (cmd_plan lint), `configs/roms/lineage-a17-shiba.yaml` (targets fix after V3 confirms names) | +25 −2 | pr-03 | V3 |
| pr-09 restore overlap + delta bank (P2) | `forge_core/cli.py`, `forge_core/relay.py` | +90 −12 | pr-05 | V6 |
| pr-10 harness regression pins | `harness/test_graph_discovery.py`, `harness/test_dag_memstall.py`, `harness/test_manifest_integrity.py` | +150 | each | V7 |

Landing order is pr-01 → pr-02 → pr-03 (unfreezes the analysis), pr-05 → pr-04 (makes the unfreeze durable), pr-06 (survival envelope), pr-07 (campaign semantics), pr-08 → pr-09 (speed), pr-10 throughout. Total surface: ~719 lines added, ~88 removed across ten review-sized PRs — roughly one focused day of implementation for pr-01…pr-03, which alone converts the six-run crash loop into a functioning warm-bypass campaign.

---

## Appendix C — Why each of the user's five hot-fixes between #58 and #63 could not have worked

For the record, because each of these was a reasonable hypothesis tested at real cost, and the record prevents re-testing them:

| Commit | Hypothesis | Why it could not move the outcome |
|---|---|---|
| d062dedb — restore AMMD + harden env exports | dependency stubs were failing the build | the build never reached dependency resolution — it died inside graph generation; AMMD global-set also ships stubs into the final ROM (correctness debt, now scoped by P0-5.5) |
| 88573f3 — user-space build (ckati fopen) | root namespace broke ckati's file opens | real bug, really fixed (turbo #59/#60's instant error → #61+ progress past it) — but orthogonal to the memory envelope |
| 43b41e0 — swap to 16 GB + scaled chunks | swap starvation | necessary and real (+9 GiB capacity: death moved from 34 m to ~60 m) — but capacity 32.6 vs live set 32.6 is a draw, and a draw is a loss |
| 73a1f94 — parallel chunker downloads | restore was the bottleneck | real win (restore is now ~21 min for 48.6 GB) — but the slot's remaining 39 minutes are spent dying, not restoring |
| 84d5721 — GOMEMLIMIT=12 GiB, GOGC=60, dual watchdog | GC tuning would stop the thrash; watchdog would save the slot | the tuning is stripped by `env -i` before reaching soong_build (R2); the watchdog fires after the eviction is dispatched (R5) |

Every one of them treated a symptom downstream of R1. The bypass was the cure the whole time — it was just aimed at a file that does not exist.
