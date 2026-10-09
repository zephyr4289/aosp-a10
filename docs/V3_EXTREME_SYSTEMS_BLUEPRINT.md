# V3_EXTREME_SYSTEMS_BLUEPRINT.md

**ROMForge v3 — Extreme Systems Blueprint: Breaking the Soong AST Memory Wall on 4-vCPU / 16 GiB Runners**

| | |
|---|---|
| **To** | ROMForge Core Team |
| **From** | Principal Systems & Compilers Infrastructure Engineer |
| **Repository** | `zephyr4289/aosp-a10` |
| **Analyzed branch** | `feat/romforge-v3` @ `39440d0` ("docs: organize previous reports into docs/archive and add systems challenge RFC") |
| **Baseline branch** | `A17-experiment` @ `85d6c37` |
| **Challenge RFC** | `docs/DEEP_SYSTEMS_CHALLENGE_RFC.md` (v3 branch) |
| **Predecessor doc** | `IMPROVEMENT_REPORT.md` (root of v3 tree — the Phase 1–3 roadmap this branch implements) |
| **Evidence base** | 3 raw GitHub Actions job logs (Run #54 Slot 2 & Slot 3, Run #58 Slot 1), 2.97 MB / 18,181 lines; full read of all 16 `forge_core/` modules + 2 workflows + 3 configs |
| **Date** | 2026-10-09 |

---

## 0. Executive Summary

The v3 branch successfully landed the Phase 1–3 roadmap from `IMPROVEMENT_REPORT.md`: fusion slots (`cli.py:563-594`), Soong graph banking (`forge_core/graph.py`), the symbols-relay exclusion (F1, `relay.py:31-40`), the end of the `.bootstrap` purge (F2, `relay.py:203-240` + `syncer.py:284-365`), lightweight product-state for verify/publish (F4, `relay.py:161-200`), the fleet fast-path (`mine.py:139-175`), and the in-place telemetry ticker. Storage is solved; resume is exact; the conveyor no longer self-deadlocks.

**What is not solved — and cannot be solved by tuning — is the Soong analysis phase.** The raw logs prove the problem is *quantitative*, not qualitative:

> **`soong_build`'s live module graph for `lineage-a17-shiba` requires ~30–34 GiB of anonymous memory.** Run #54 Slot 2 completed analysis only after climbing to **15.0 GiB RAM + 18.8 GiB swap = 33.8 GiB** (telemetry `L2198`, 16:49:17). Run #58 Slot 1 died pinned at 15.2/15.6 GiB RAM + 11.0/11.0 GiB swap — **26.1 GiB total, still climbing** when the VM was killed. The runner's total budget is 26.5 GiB. The analysis phase *physically does not fit* on a 4-vCPU/16 GiB runner without ~15–18 GiB of functioning swap headroom, and v3's answer to that (zram) **silently failed to activate** on the GitHub-hosted runner — the log shows `swap on: +6 GB at /mnt/romforge/.forge-swap` (`run58_slot1_failed_oom.log` L210, 03:23:00) with no `zram tier-1 swap active` line, meaning `env.ensure_zram(8)` returned `False` and `prepare` continued as if nothing happened.

From that single measured fact, the whole failure cascade follows — and so does the whole fix. This blueprint delivers the four requested artifacts:

| # | Requested (RFC §4) | Delivered | Section |
|---|---|---|---|
| 1 | Root-cause mechanics of the Soong AST graph re-evaluation | Measured 3-incident forensics + dirty-check chain + the disk-space death spiral | §2 |
| 2 | Direct Ninja Bypass on resume slices | Full implementation: 7-invariant correctness contract, guard design, `engine.py`/`graph.py` pseudo-diffs, fallback + kill-switch | §3 |
| 3 | Memory & cgroup Shield for 16 GB runner stability | Three-layer shield (kernel cgroup envelopes, per-phase runtime matrix, PSI tripwire), zram/swap recalibration, heartbeat protection | §4 |
| 4 | Extreme parallelization / sub-2-hour strategies | Measured compute model, turbo re-architecture incl. *why vendor_boot/dtbo failed*, CAS-Relay cross-campaign cache, fleet hybrid, honest wall-clock projections | §5 |

**Headline projections** (all derivations in §5.4 and Appendix F):

| Configuration | Cold campaign wall | Runner-minutes | OOM/143 risk per slot |
|---|---|---|---|
| v3 as-is (measured trajectory) | 15–18 h | 17–21 h | 50–70% during analysis |
| **+ P1 Shield** (zram-lz4, GOMEMLIMIT fix, PSI watchdog) | 14–17 h (survivable analysis: 29–33 min swap-crawl) | 15–19 h | ~0% livelock; failures become 4 s cgroup OOM-kills + graceful bank |
| **+ P2 Ninja Bypass + fleet graph-master** | **8–11 h** | 9–12 h | **0% — `soong_build` never runs on a GitHub slot** |
| + P3 Turbo re-arch + CAS-Relay | 4–7 h | 5–8 h | 0% |
| + Fleet hybrid (16-core node) | **1.5–3 h** | 3–5 h (GH) + 2–3 h (fleet) | 0% |
| Steady-state, same `mhash`, CAS warm | 20–50 min | <1.5 h | 0% |

**Sub-2-hour from cold, GitHub-free-tier only: not physically achievable** — §5.5 shows the arithmetic (≥26 effective cores required against a ~2.5–3 h critical-path floor; 20 slots exist but each pays ~36 min of relay tax and the framework javac→dex→image chain is serial). Sub-2-hour *is* achievable either fleet-hybrid from cold, or free-tier-only with a warm CAS from a prior campaign. The blueprint says so honestly, then gets you both paths.

Three further findings that materially change the codebase's correctness posture, discovered during this analysis and not present in any prior document:

1. **`ALLOW_MISSING_DEPENDENCIES: "true"` is set globally** in `configs/roms/lineage-a17-shiba.yaml` (`env:` block) — it applies to the **main chain and the final ROM**, not just turbo prewarms. `turbo.py:16-21`'s honesty contract ("the final `m` re-links everything still stale") is void when the final `m` itself stubs missing deps into the shipped image. §5.2-S2.
2. **The dynamic-swap watchdog is a self-strangulation loop**: `engine.py:411-443` adds 2 GB swap chunks when memory pressure rises, but only while `physical_free_gb > 12.0`. As the campaign banks state, free disk falls below 12 GiB and the swap cushion silently disappears — Run #54 Slot 2 survived *because it had 30.5 GiB free*; Slot 3 died *because it had 9.1 GiB*. The system's survival odds decay monotonically with campaign progress. §2.6.
3. **`GOMEMLIMIT=11GiB` (`engine.py:86`) made the analysis *more* likely to die, not less**: A17-experiment (no GOMEMLIMIT) completed the identical analysis with 36.6 GiB of total capacity; v3 (GOMEMLIMIT=11GiB, 26.1 GiB capacity) thrashed — the limit sits *below* the live set, forcing continuous GC assist on 4 vCPUs while the heap grows anyway. Go's soft limit converts "slow but finishes" into "GC death spiral". §2.3, §4.4.

---

## 1. Evidence Base & Method

### 1.1 Artifacts analyzed

| Artifact | Size | Span | Role |
|---|---|---|---|
| `slot2_success_5hr.log` (job `113404552754`, Run #54, A17-experiment) | 2.83 MB / 16,119 lines | 15:52:48Z → 21:06:16Z (5h13m) | The successful 5-hour resume slice — the *only* surviving recording of a complete Soong analysis on this hardware. Baseline for all memory/throughput numbers. |
| `slot3_failed_oom.log` (job `113541508682`, Run #54, A17-experiment) | 0.10 MB / 883 lines | 21:06:28Z → 21:40:04Z (33m36s) | The canonical OOM-livelock death. |
| `run58_slot1_failed_oom.log` (job `113653446227`, Run #58, feat/romforge-v3) | 0.16 MB / 1,179 lines | 03:20:18Z → 03:54:34Z (34m16s) | v3's first attempt with `GOMEMLIMIT=11GiB`, fusion slots, graph banking — proves the v3 shield did not change the outcome. |
| Repository `feat/romforge-v3` @ `39440d0` | 16 `forge_core/` modules, `forge.yml` (1,185 lines), `slot.yml` (66) | — | Code anchors for every claim. |

All three jobs ran on mined **AMD EPYC 9V45 (Zen 5 / Turin)** silicon, score 100, strict mode (`OUT: model=AMD EPYC 9V45 96-Core Processor` — all three logs, mining gate step). All three restored the same content-addressed source `src-1e29e74671baae8b`; Slot 3 and Run #58 both restored state `state-lineageosgoogleshibaa17-s2`. This is a controlled experiment: **identical silicon, identical source, identical restored state, same campaign — the only variables are free disk space, swap capacity, and env knobs.**

### 1.2 Reconstructed timelines (from raw telemetry, 1 Hz sampling)

**Run #54 Slot 2 (`c05`) — the survivor.** Job 15:52:48 → 21:06:16 (5h13m28s).

| Clock | Phase | Evidence line |
|---|---|---|
| 15:55:50 | btrfs zstd:1 volume mounted, cap 107 GiB | `[15:55:50 OK build volume: btrfs zstd:1, cap 107 GiB]` |
| 15:55:51 | disk swap on: +6 GB (total 9 GB — A17 branch has no zram call) | `swap on: +6 GB at /mnt/romforge/.forge-swap (target 8 GB)` |
| 16:08:22 | source restored (12m31s) | `[16:08:22 OK source restored from src-1e29e74671baae8b]` |
| 16:17:31 | out/ state restored from `…-s1` (9m09s); `-j 8` selected | `[16:17:31 OK resumed state … (slice 1)]` / `dynamic parallel jobs: -j 8` |
| 16:17:44 | `bootstrap blueprint [100% 1/1]` begins — this edge **is** `soong_build` | `[16:17:44] [100% 1/1] bootstrap blueprint RAM: 1.8/15.6G (Swap: 0.0/9.0G)` |
| 16:19:40 → 16:21:25 | dynamic-swap watchdog fires **six times**, swap 9→21 GB in 105 s | SWAP-TOTAL transitions 9→11→13→15→17→19→21 (L417…L527) |
| 16:22:19 → 16:46:10 | 24 minutes pinned at RAM ≈ 14.8–15.1/15.6, swap 9→18.8/21 — the analysis death-march that **survived** | telemetry L581…L1725 |
| ~16:50 | `analyzing Android.bp files and generating ninja file [100% 2/2]`, RAM released to 1.1 GiB, swap 0.2 | `[16:50:24] … RAM: 1.1/15.6G (Swap: 0.2/21.0G)` |
| 16:50:56 | ckati: `finishing Make module r [100% 13/13]` | telemetry L2297 |
| 16:55:44 → 20:52:31 | ninja execution: 633 → 48,651 / 82,735 edges (58.8%) | `[16:55:44] [0% 633/82735] … [20:52:26] [58% 48626/82735]` |
| 20:52:31 | budget SIGINT (275 min), `ninja: build stopped: interrupt` | `[20:52:31 WARN slice budget spent …]` |
| 21:06:09 | banked: 9 parts × 1.9 GB streamed to release | `[21:06:09 OK stream-packed out: 9 parts shipped through sink]` |

**Run #54 Slot 3 (`c02`) — the victim.** Job 21:06:28 → 21:40:04 (33m36s). Same source, restored `…-s2`, **disk(vol) 9.1 GiB free** (vs 30.5 GiB on Slot 2).

| Clock | Phase | Evidence |
|---|---|---|
| 21:30:20 | state restored, `-j 8` | `resumed state …-s2 (slice 2)` |
| 21:30:35 | `bootstrap blueprint` begins, RAM 1.4 | telemetry L309 |
| 21:32:02 | RAM full: 14.6/15.6 (87 s from start) | telemetry L397 |
| 21:32 → 21:39 | RAM pinned 14.7–15.0; swap crawls 3.9 → 9.0 (of 9.0 — **no chunks added; free disk 9.1 GiB < 12 GiB guard**) | telemetry L407…L834 |
| 21:39:22 | swap 100% full: 9.0/9.0 | telemetry L834 |
| 21:39:22 → 21:40:03 | total freeze, 41 s at absolute saturation | L834…L877 |
| 21:40:04 | **exit 143**; orphan terminator reports `pid (3989) (soong_ui)` | `##[error]Process completed with exit code 143` / `Terminate orphan process: pid (3989) (soong_ui)` |

**Run #58 Slot 1 (`c16`) — v3's attempt.** Job 03:20:18 → 03:54:34 (34m16s). Fusion enabled (`FORGE_UNTIL_BUDGET_S: 19200`), `FORGE_CKPT_MIN: 45`, restored `…-s2`, `-j 8`, and `GOMEMLIMIT=11GiB` exported by `engine.build_env()`.

| Clock | Phase | Evidence |
|---|---|---|
| 03:23:00 | **no zram line**; disk swap on: +6 GB (total 9 GB) | `swap on: +6 GB at /mnt/romforge/.forge-swap (target 8 GB)` — and *no* `zram tier-1 swap active` |
| 03:32:58 / 03:41:10 | source (9m49s) / state (8m12s) restored | `OK source restored …` / `OK resumed state …-s2 (slice 2)` |
| 03:41:10 | fusion slot enabled: 320m wall | `fusion slot enabled: 320m job wall budget` |
| 03:41:26 | `bootstrap blueprint` begins — **`soong_build` re-ran despite `.bootstrap` being restored** (v3 relay no longer purges it) | telemetry L348, RAM 3.4 |
| 03:43:43 | one dynamic chunk: swap 9 → 11 GB (RAM 95%, swap 45%) | `dynamic swap auto-scale: +2 GB chunk 1 activated (RAM 95%, Swap 45%, Disk 14.0G free)` |
| 03:44:29 | RAM pinned 15.0 | telemetry L540 |
| 03:53:55 | swap 11.0/11.0 full | telemetry L1133 |
| 03:54:04 → 03:54:33 | freeze ~30 s at 15.1–15.3 / 11.0 | L1143…L1173 |
| 03:54:34 | **exit 143** (31m from boot, 13m08s into the bootstrap edge) | `##[error]Process completed with exit code 143` |

### 1.3 Constants extracted (used throughout)

| Constant | Value | Source |
|---|---|---|
| Ninja graph edges (`lineage-a17-shiba`) | 82,735 | progress lines, all slots |
| `soong_build` peak anonymous memory | **~33.8 GiB** (15.0 RAM + 18.8 swap) | Slot 2 telemetry peak |
| Analysis wall-time in swap-crawl (when it survives) | 29–33 min | Slot 2: 16:17:44 → ~16:50 |
| Time-to-RAM-full during analysis | 87 s (slot 3), 3 min (run58) | telemetry |
| Time from total saturation → VM kill | 41 s (slot 3), 30 s (run58) | telemetry |
| Ninja sustained throughput, mid-graph (Java/dex), `-j 8`, Zen 5 | 3.2–3.4 edges/s | Slot 2: 4,143→48,651 edges in 236 min |
| Ninja throughput, early native section | ~0.25–0.3 edges/s | Slot 1 (per RFC, 5 h → ~4.1 K edges; corroborated by Slot 2's restored-edge flash to 4,143) |
| Per-job relay tax (prepare + src restore + state restore + bank) | 34–48 min | all three logs |
| State bank size (slice 2) | 9 parts ≈ 17.1 GiB compressed | `stream-packed out: 9 parts` |
| Swap capacity by incident | 36.6 GiB (slot 2: RAM 15.6 + swap 21) / 24.6 (slot 3) / 26.1–26.6 (run58: RAM 15.6 + swap 11) | telemetry |

---

## 2. Root-Cause Mechanics: The Soong AST Memory Bomb

### 2.1 What `soong_ui --make-mode` actually executes (mapped to the logs)

ROMForge's launcher (`engine.py:184-189`) is:

```python
launcher = (
    "set +eu; "
    f"source build/envsetup.sh >/dev/null 2>&1; "
    f"lunch {lunch_combo(plan)} >/dev/null 2>&1; "
    f"exec {soong_ui} --make-mode -j {jobs} {target}"
)
```

Every slot — cold, resumed, turbo, fusion-iteration — execs the same command. Internally (AOSP `build/soong/ui/build/`; mechanism names cited from the Android 14/15 build stack — verify exact paths in-tree when instrumenting, see P0) `--make-mode` runs a fixed pipeline:

| Stage | What happens | Log signature (our evidence) | Cost on this hardware |
|---|---|---|---|
| 1. dumpvars / env setup | Resolve lunch vars, write `out/soong/soong.variables` | silent | seconds |
| 2. **Blueprint bootstrap** | `ninja -f out/soong/.bootstrap/build.ninja out/soong/build.ninja`. This graph has one heavyweight edge: **`soong_build` itself** — compile/link the `soong_build` Go binary if `.bootstrap` is stale, then **run it to parse every Android.bp in the tree and emit `out/soong/build.ninja`**. | `[100% 1/1] bootstrap blueprint` | **the memory bomb: 30–34 GiB, 29–33 min or death** |
| 3. Analysis tail | `soong_build` runs mutators, resolves deps, prunes the live graph, emits ninja rules | `[100% 2/2] analyzing Android.bp files and generating ninja file` (the tail of stage 2) | included above |
| 4. ckati | Regenerate `out/build-<variant>.ninja` from `Android.mk` files if kati stamps are dirty | `[100% 13/13] finishing Make module` | ~2–4 min |
| 5. combined | Write `out/combined-<variant>.ninja` (two `subninja` lines + phony aliases) | silent | ms |
| 6. runNinja | `ninja -f out/combined-<variant>.ninja <goals>` — the actual compilation | `[N% done/total] //module/...` | hours (the only stage that produces value) |

The single most important structural fact in this whole document:

> **The re-analysis edge lives in the *bootstrap* graph (`out/soong/.bootstrap/build.ninja`), not in the combined graph.** Stage 6 loads `out/combined-<variant>.ninja`, which `subninja`s `out/soong/build.ninja` and `out/build-<variant>.ninja` — **neither of which contains any rule that can re-invoke `soong_build`.** Stage 2 is the *only* place analysis can be triggered, and only `soong_ui` runs stage 2. This is precisely why the Direct Ninja Bypass (§3) is structurally sound: invoking stage 6 directly cannot re-enter the memory bomb, by construction, regardless of mtimes, env deltas, or glob state.

### 2.2 The memory anatomy: why 82,735 edges cost 33.8 GiB

`soong_build`'s pipeline for an Android 15/17-scale tree: (1) **parse** ~8–12 K `Android.bp` Blueprint files into Go ASTs; (2) instantiate modules; (3) run **mutators** — architecture (arm/arm64/x86/x86_64), image variants (core/platform/vendor/product/ramdisk/recovery), multilib splits — which multiply each declared module into several *variant instances*; (4) resolve the full dependency graph (deps, impls, overrides); (5) live-cycle elimination; (6) emit ~82,735 ninja edges.

At the peak, the process holds simultaneously: every parsed property struct, every module variant instance, the complete resolved dependency graph, and the partially-emitted ninja buffer. That working set is *live* — not garbage — so no GC setting can shrink it. Our measurement:

```
Slot 2 telemetry @16:49:17 (L2198):  RAM 15.0/15.6  Swap 18.8/21.0
→  total anonymous ≈ 33.8 GiB, monotonically grown over 29 minutes
Slot 3 telemetry @21:39:22 (L834):   RAM 15.0/15.6  Swap 9.0/9.0
→  24.0 GiB, still climbing, zero progress for the prior 4 minutes
Run58 telemetry @03:53:55 (L1133):  RAM 15.1/15.6  Swap 11.0/11.0
→  26.1 GiB, still climbing, killed 39 s later
```

Three corollaries follow, and they kill several tempting hypotheses:

- **No per-process Go knob bounds this.** `GOMEMLIMIT` is a *soft* limit: when the live set exceeds it, the Go runtime keeps allocating and simply runs GC continuously (GC assist) in a doomed attempt to stay under. It converts OOM into CPU starvation. (§2.3.)
- **The phase is marginal, not deterministic.** Slot 2 finished at 33.8 GiB with 36.6 GiB of capacity. Slot 3 and Run #58 had 24–26 GiB of capacity. Whether a slot lives is a function of *swap capacity*, which is a function of *free disk*, which is a function of *campaign progress*. (§2.6.)
- **The only winning moves are: don't run it here (§3 bypass + fleet graph-master), or give it room + a fast kill if it still trips (§4 shield).**

### 2.3 Why `GOMEMLIMIT=11GiB` made Run #58 *worse* than the untuned A17 baseline

v3's `build_env()` (`engine.py:82-87`) exports:

```python
# Memory shield: Bound Go runtime memory for Soong AST parser to prevent
# runaway GC thrash & OOM
"GOMEMLIMIT": os.environ.get("FORGE_SOONG_MEM_LIMIT", "11GiB"),
```

Two independent errors, both now measurable:

**(a) The limit is below the live set.** Go's contract for `GOMEMLIMIT` is explicit: it is a soft limit that the runtime will *exceed* rather than OOM, and as total heap approaches the limit, GC frequency rises toward continuous. With the live set at 12–20 GiB (it grows through stage 2–3), an 11 GiB limit means: from roughly mid-parse onward, **every allocation triggers GC assist work**. On 4 vCPUs, GC assist steals 25–100% of mutator CPU. The comparison is controlled and damning:

| Incident | GOMEMLIMIT | Total anon capacity | Outcome |
|---|---|---|---|
| Run #54 Slot 2 (A17) | *unset* (GOGC=100 default) | 36.6 GiB | completed in 29 min, peak 33.8 GiB |
| Run #58 Slot 1 (v3) | 11 GiB | 26.1 GiB | GC-assist crawl + saturation, killed at 13 min |

Slot 2's heap was allowed to grow (GOGC=100 → target 2× live) into the 21 GiB swap cushion; soong_build spent its CPU parsing. Run #58's heap was throttled at ~11 GiB, so its CPU was split between parsing and *compacting a heap that could not shrink*, while the kernel paged the cold half out to a 11 GiB swap that then filled.

**(b) The variable is exported process-family-wide.** `build_env()` builds the environment inherited by *everything* `soong_ui` spawns: the parallel Go *compiler* processes during the bootstrap compile step (each gets its own 11 GiB soft limit — limits do not sum), `soong_build`, ninja's workers, javac. A knob intended for one process became a family-wide default with no aggregate bound. Per-process limits are the wrong tool; the aggregate bound must come from the kernel (§4.2).

### 2.4 The zram that never was

v3 added `fenv.ensure_zram(8)` to `cli.py` prepare (line 208): zram, zstd, 8 GB, priority 100 — "tier-1 compressed RAM swap" that was supposed to decouple swap capacity from disk. Run #58's log proves it **did not activate**: the prepare sequence shows `swap on: +6 GB at /mnt/romforge/.forge-swap (target 8 GB)` (L210) and the telemetry begins at `Swap: 0.0/9.0G` — if an 8 GiB zram were active, `ensure_swap`'s guard (`env.py:305-307`: `current = _active_swap_gb(); if current >= size_gb: return True`) would have skipped the disk swap entirely, and totals would read ≥ 8 GiB before any chunk.

`ensure_zram` is written to "gracefully degrade" (`env.py:261-262` docstring) — and it did, silently, exactly when it was load-bearing. Failure candidates on the GitHub-hosted Azure image: `modprobe zram` unavailable/locked, `/dev/zram0` absent and `zramctl` absent, or `swapon /dev/zram0` denied. Whatever the precise cause, the systemic bug is that **a silent `return False` on the memory-shield's keystone produces no warning, no telemetry flag, no degraded-profile switch** — the build proceeds with a strictly weaker shield than designed. §4.3 fixes this (loud failure + verified fallback + a `swap_topology` line in every slot log).

### 2.5 The livelock cascade, second by second (Run #58, 03:41:26 → 03:54:34)

1. **03:41:26–03:44:29** — `soong_build` allocates; the kernel (with `vm.swappiness=60` set by `env.protect_runner_processes()`, `env.py:222`) eagerly pages cold anon out to the 9 GiB SSD swap on `/mnt` — *the same physical disk as the btrfs loopback volume backing the entire tree*. RAM fills in 3 minutes (L348→L540).
2. **03:44:29–03:53:55** — every GC mark/sweep pass touches the *entire* heap; pages already swapped must fault back in (SSD latency, ~100 µs–1 ms each, thousands per sweep); the kernel, needing RAM for the newly-faulted pages, compresses/swaps other pages out. `kswapd0` pins a core; the 4 vCPUs converge to ~0% mutator progress (progress counter frozen at `[100% 1/1]` for 13 minutes). The dynamic-swap watchdog adds one 2 GiB chunk (03:43:43) — buying 9 more minutes of crawl, and placing the chunk on the same `/mnt` SSD.
3. **03:53:55–03:54:34** — swap 11.0/11.0: there is nowhere left to page. `kswapd0` spins at ~100% reclaiming nothing; the actions-runner daemon (HTTP heartbeat to GitHub's control plane) is starved of CPU and of page cache for its TLS buffers.
4. **~03:54:00 (infra-side)** — GitHub's controller marks the job unresponsive and issues shutdown. The runner teardown reaches the process group late: `##[error]Process completed with exit code 143` (SIGTERM), and the orphan terminator identifies the corpse: `Terminate orphan process: pid (3989) (soong_ui)`.

Note what did *not* happen: the Linux OOM killer never fired. `env.protect_runner_processes()` (`env.py:220-234`) sets `oom_score_adj = -1000` on `Runner.*` and `forge_core` — so the kernel's last-resort path, which would have killed `soong_build` in ~2–4 seconds and left a clean, bankable, * diagnosable* failure, was never reached, because swap-reclaim kept "succeeding" at the edge of livelock. **The system converted a 4-second OOM kill into a 13-minute VM execution.** That inversion — trading a fast, recoverable failure for a slow, total one — is the single most important design error to reverse in v3.1, and it is fully reversible with kernel-level bounds (§4.2).

### 2.6 The disk-space death spiral (structural, and unique to this analysis)

The dynamic-swap watchdog (`engine.py:411-443`) is the only thing standing between the analysis phase and death, and it is disk-gated:

```python
if (sw_used_pct > 70.0 or (ram_used_pct > 80.0 and sw_used_pct > 40.0)):
    if len(dynamic_swap_chunks) < 6:
        snap = storage.snapshot(build_root)
        if snap.physical_free_gb > 12.0:          # <- the gate
            ... activate_swap_chunk(...)
```

| Incident | Vol free at analysis start | Physical free | Chunks added | Swap total | Outcome |
|---|---|---|---|---|---|
| Slot 2 | 30.5 GiB | ~35.7 GiB | 6 (21 GB total) | 21 GiB | survived |
| Slot 3 | 9.1 GiB | < 12 GiB | **0 — gate refused** | 9 GiB | died |
| Run #58 | 9.4 GiB | 14.0 GiB | 1 | 11 GiB | died (slower) |

Each successful slot grows `out/` by 15–19 GiB and banks 17 GiB of state; the next slot restores it before building; the volume's free space therefore *decays monotonically across the campaign*. **Slot 1 is the safest slot and slot N is the most fragile, by construction.** Run #54's Slots 4 and 5 repeating Slot 3's failure (per the RFC) is not bad luck — it is the sound of a campaign eating its own shock absorber. No amount of per-incident tuning fixes this; the swap cushion must be decoupled from campaign disk consumption (§4.3) or the analysis must stop running on this tier entirely (§3).

### 2.7 Why resume slots re-pay analysis at all — the dirty-check chain

Run #58 restored `out/` from the s2 bank **including** `out/soong/.bootstrap`, `.glob`, `.minibootstrap`, and `build.ninja` (v3 removed the A17 purge — `relay.py:224-228` now deletes only `.temp`, and `syncer.ensure_prebuilts` clamps only webview/android.jar mtimes to a fixed past epoch, `syncer.py:288,316,344`). The mtimes were preserved by the relay (§3.2 invariant I1). Yet at 03:41:26 the bootstrap edge ran `soong_build` anyway. The candidate triggers, in order of plausibility:

| # | Trigger | Mechanism | Verdict |
|---|---|---|---|
| T1 | **Environment fingerprint delta** | `soong_ui` records the env vars it consumed in `out/soong/soong.environment.used`; a diff between the banked fingerprint and the current invocation's env re-runs analysis. Run #58's env differs from Run #54 Slot 2's in at least `GOMEMLIMIT` (new), `NINJA_ARGS` (format change), and the `FORGE_*` family — any *tracked* member re-triggers. | **Most likely.** Cheap to confirm and to neutralize (§3.3: the bypass never asks; §7: the `-d explain` probe). |
| T2 | Patch/mtime disturbance in the tree | `cmd_slice` calls `syncer.apply_patches` + `validate_lunch` every slot (`cli.py:507-509`); any write into `device/…` after the graph was banked marks `.bp`/`.mk` inputs newer than `build.ninja`. | Plausible secondary. Mitigated by the same bypass. |
| T3 | Glob cache invalidation | soong's glob layer re-scans when parent-directory mtimes change; tar's delayed directory-restore handles same-archive order, but cross-archive overlays (src relay into a tree that then receives out/) can flip directory mtimes. | Plausible tertiary. |
| T4 | `.bootstrap` binary stale vs `.go` sources | Only if the src bank's `build/soong/**` mtimes postdate the banked binary — inconsistent with the bank ordering (src is snapshotted before any slot builds). | Ruled out by construction (mhash ordering). |

The engineering conclusion does not depend on adjudicating T1–T3: **each of them is an input to a dirty-check that only `soong_ui` consults, and only stage 2 consults `soong_ui`.** The bypass (§3) makes the question moot; the `-d explain` instrumentation (P0) turns the remaining curiosity into data.


---

## 3. Dilemma A — The Direct Ninja Bypass (Pure Incremental Execution)

### 3.1 Verdict

> **Yes. Freeze the graph once; invoke ninja directly on resume slices. It is structurally safe, and it is the only design that makes the OOM risk of resume slots exactly zero.**

The RFC asks whether we can "freeze the generated `build.ninja` graph once in Slot 1, patch any mtimes/hashes, and invoke ninja directly (`ninja -f out/combined-lineage_shiba.ninja $TARGET`) during subsequent slots, completely bypassing `soong_ui`, `soong_build`, and the Go memory trap." The mechanics of the Android build stack answer this more strongly than the question implies:

1. `soong_ui --make-mode`'s final stage (runNinja) is *literally* `ninja -f out/combined-<variant>.ninja <goals>`. The bypass does not invent a new execution mode — it invokes stage 6 of 6 while skipping stages 1–5, whose outputs are already frozen on disk in `out/`.
2. The combined graph **cannot re-enter analysis** (§2.1): no edge in `out/soong/build.ninja` or `out/build-<variant>.ninja` regenerates `out/soong/build.ninja` — that edge exists only in the bootstrap graph, which nothing but `soong_ui` loads. A stale/dirty input set therefore *cannot* re-trigger the memory bomb in bypass mode; the worst it can do is run a stale (identical, mhash-pinned) command set.
3. Ninja's own incremental state — `.ninja_log` (start-time, end-time, mtime, hash-of-command per edge) and `.ninja_deps` (depfile database) — is exactly the state ROMForge already relays with `out/`. The bypass consumes it directly. **Run #54 Slot 2 already proved this layer works**: it restored slice-1 state and ninja recognized 4,143 previously-built edges as clean within ~16 minutes of graph walk (17:08:54 telemetry), then executed only new work.
4. Content identity across slots is *guaranteed by construction*, not by hope: every slot restores the same `src-<mhash>` snapshot (immutable, content-addressed — `cli.py:347-353`), and the graph bank is keyed `(mhash, lunch)` (`graph.py:23-27`). The graph cannot mismatch the tree unless the `mhash` itself lies, which would break the entire relay architecture anyway.

The one real risk class is *environmental parity* for rule execution (tools invoked by relative name, `PATH` differences). It is neutralized by launching through the same `envsetup.sh` + `lunch` shell ROMForge already uses (`engine.py:184-189`) and swapping only the final `exec` line. §3.3.

### 3.2 The correctness contract — seven invariants

The bypass is enabled only when all seven hold. Each invariant is *verified today by existing code* — that is what makes this a 2–4 day change rather than a research project.

| # | Invariant | How it holds today | Verification point |
|---|---|---|---|
| I1 | **Archive mtimes round-trip exactly.** | `chunker.py` packs with plain `tar -C root -cf -` (line 182; no `--mtime` clamp, no `--touch`) and unpacks with plain `tar -C dest -xf -` (lines 389-394). GNU tar restores file mtimes from archive headers on extract, with delayed directory-mtime restoration at end-of-stream. | `harness/test_mtime_invariants.py` already exists in the repo (A17 lineage) — extend to assert `out/soong/build.ninja` and a sample of `.o` files keep banked mtimes through pack→unpack. |
| I2 | **Source mtimes ≤ output mtimes.** | The source snapshot `src-<mhash>` is banked *before* any slot builds; every output in the state bank is strictly younger. Both archives restore their recorded mtimes. | Guard G3 (§3.3) asserts `max(mtime(.bp set)) < mtime(out/soong/build.ninja)` — a 1-second `find` check. |
| I3 | **Graph identity = (mhash, lunch).** | `graph.py:23-27` keys the graph bank exactly so; `src-<mhash>` is immutable by store design. | The tag lookup itself. |
| I4 | **Rule-execution env parity.** | Launcher retains `source build/envsetup.sh; lunch <combo>` and only replaces the `exec soong_ui …` tail with `exec <ninja> -f …`. `PATH`, `TARGET_*`, `OUT_DIR` are identical to what `soong_ui` would export for stage 6. | One A/B slot pair (§7 validation V2). |
| I5 | **Combined + kati ninja files present.** | `out/combined-*.ninja` and `out/build-*.ninja` live at `out/` top level — **not** in `STATE_EXCLUDES` (`relay.py:31-40`) — so the state relay already ships them; the graph bank (§3.6) additionally ships them for cold turbo slots. | Guard G1 file-existence checks. |
| I6 | **`.ninja_log` + `.ninja_deps` present and consistent.** | Same: top-level `out/` files, relayed every bank; Slot 2's 4,143-edge clean-flash is the production proof. | Guard G1. |
| I7 | **Toolchain identity.** | The prebuilt toolchain is *inside* the mhash-pinned tree (`prebuilts/clang/…`, `prebuilts/jdk/…`); a different toolchain implies a different `mhash` implies a different graph tag. | Transitively by I3. |

### 3.3 Implementation design

**New engine mode.** `run_slice()` gains a `mode` and a pre-flight guard function. Pseudo-diff against `engine.py:155-199`:

```python
# engine.py — additions (pseudo-diff)
NINJA_BYPASS_STATUS = "[%p %f/%t] "        # matches relay.progress_from_log regex

def bypass_ready(build_root: Path) -> Optional[Path]:
    """Return the combined ninja file if the Direct Ninja Bypass is safe."""
    if os.environ.get("FORGE_NINJA_BYPASS", "1") != "1":
        return None                                   # kill-switch (G0)
    out = build_root / "out"
    combined = sorted(out.glob("combined-*.ninja"))
    soong_ninja = out / "soong" / "build.ninja"
    kati = [p for p in out.glob("build-*.ninja")]
    if not combined or not soong_ninja.exists() or not kati:
        return None                                   # G1: frozen graphs present
    for required in (out / ".ninja_log", out / ".ninja_deps"):
        if not required.exists():
            return None                               # G1b: incremental state
    ninja_bin = build_root / "prebuilts/build-tools/linux-x86/bin/ninja"
    if not ninja_bin.exists():
        return None                                   # G2: executor present
    graph_mtime = soong_ninja.stat().st_mtime
    # G3: no .bp/.mk file in the tree is newer than the frozen graph.
    #     (find is fast enough: ~10-12s on the btrfs volume, runs once per slice)
    r = subprocess.run(
        ["bash", "-c",
         f"cd {build_root} && find . -name Android.bp -o -name Android.mk "
         f"| xargs -r stat -c %Y 2>/dev/null | sort -rn | head -1"],
        capture_output=True, text=True, timeout=120)
    if r.returncode == 0 and r.stdout.strip():
        if float(r.stdout.strip()) >= graph_mtime:
            return None                               # G3: graph stale -> soong path
    return combined[0]

def _launcher(mode, soong_ui, jobs, target, combined: Optional[Path]) -> str:
    if mode == "ninja-direct" and combined is not None:
        ninja_bin = "prebuilts/build-tools/linux-x86/bin/ninja"
        return ( "set +eu; "
                 "source build/envsetup.sh >/dev/null 2>&1; "
                 "lunch {lunch} >/dev/null 2>&1; "
                 "export NINJA_STATUS='{status}'; "
                 "exec {ninja} -f {combined} -j {jobs} {target}" )
    return ( "set +eu; "
             "source build/envsetup.sh >/dev/null 2>&1; "
             "lunch {lunch} >/dev/null 2>&1; "
             "exec {soong} --make-mode -j {jobs} {target}" )
```

Wiring in `run_slice()`:

```python
combined = bypass_ready(build_root)
mode = "ninja-direct" if combined else "soong"
log.ok(f"slice mode: {mode} "
       f"({'bypassing soong_ui entirely' if combined else 'full soong_ui pipeline'})")
launcher = _launcher(mode, soong_ui, jobs, target, combined)
...
# after proc.wait(): if mode == "ninja-direct" and rc != 0:
#     log.warn("bypass execution failed -> replaying via soong_ui once")
#     (single fallback re-run with mode="soong", tagged in INDEX: bypass_fallback=1)
```

**The three details that make it production-grade:**

1. **`NINJA_STATUS='[%p %f/%t] '`** — soong_ui exports a custom ninja status format; the live ticker and `relay.progress_from_log()` (`relay.py:289-305`, regex `\[\s*(\d+)%\s*(\d+)/(\d+)\s*\]`) and the `PROGRESS:` notices depend on it. The bypass must export the same format or the entire conveyor's progress/ETA machinery goes blind. The format above reproduces the observed `[39% 32780/82735]`.
2. **`exec`, `start_new_session`, and the pgid contract** — the launcher must keep using `exec` so `subprocess.Popen(..., start_new_session=True)` (`engine.py:197-199`) still makes ninja the process-group leader; the budget watchdog's SIGINT→SIGKILL escalation (`engine.py:205-226`) then continues to stop ninja exactly as it stops soong_ui today, with the same "consistent out/ for banking" semantics (ninja finishes in-flight commands, writes `.ninja_log`, and exits on interrupt — the Run #54 Slot 2 SIGINT at 20:52:31 → 13-minute clean bank is the production proof).
3. **Fallback, not blind faith** — if ninja exits non-zero for *any* structural reason (unknown target, missing file, depfile corruption), the slot replays once through the full `soong_ui` path and records `bypass_fallback=1` in the INDEX. One bad slot pays the old cost; the campaign never stalls. Combined with the G0 kill-switch (`FORGE_NINJA_BYPASS=0`), rollback is a one-line workflow input.

**Where it plugs in:** nowhere new — `cli.cmd_slice` already restores state (`cli.py:535-557`) and the fusion loop already iterates `_run_single_slice` (`cli.py:563-594`) with warm `out/`; the bypass simply changes what `_run_single_slice` execs. Turbo slots take the same path (§3.5).

### 3.4 What the bypass skips, and why each skip is safe

| Skipped stage | Why it is safe to skip on a resume slice |
|---|---|
| soong bootstrap compile (`out/soong/.bootstrap` rebuild) | Only dirty if `build/soong/**` sources changed — impossible under a fixed `mhash` (I3, I7). Restored with mtimes (I1). |
| `soong_build` graph analysis | The memory bomb. Its *output* (`out/soong/build.ninja`) is frozen, content-pinned by `(mhash, lunch)`, and verified non-stale by guard G3. |
| ckati regeneration (`out/build-*.ninja`) | Dirty only if `Android.mk` set changed — pinned by `mhash`, guarded by G3 (`.mk` included in the find). |
| `soong.variables` / env fingerprint writes | Writes of *inputs* to stages we skip. Frozen values remain on disk, consistent with the frozen graphs. |
| `combined-*.ninja` rewrite | Static content (two `subninja` references + phony aliases) shipped by the relay (I5). |

The one *semantic* caveat: the bypass freezes the graph including its **target set**. If an operator overrides the goal (`FORGE_TARGET_OVERRIDE`, `cli.py:324-325`) to a target that did not exist when the graph was banked, ninja exits with `unknown target` — the fallback in §3.3 catches it and the soong path rebuilds the graph for the new goal. Cost: one slot at old speed. Correctness: preserved.

### 3.5 Turbo interplay — the bypass doubles turbo's value

Today every turbo partition slot (`forge.yml:232-261` → `cli.py:514-533` → `run_slice(..., allow_missing_deps=True)`) is a **cold `out/` that pays the full 29–33 minute analysis** on a fresh runner — and therefore inherits the exact OOM lottery of §2.6 (this is the primary reason `vendor_boot`/`dtboimage` prewarms failed; §5.2-S2 gives the full diagnosis). With graph banking restored *before* the turbo build (cold `out/` → `graph.restore_graph()` already happens on the cold path, `cli.py:551-557`) plus the bypass, a turbo slot reaches ninja execution in **~12 minutes** (9–10 src restore + ~2 guard+launch) instead of **~45 minutes with a 50–70% death chance** — and all six turbo partitions stop competing for the same memory wall simultaneously (`max-parallel: 6`, `forge.yml:243`).

### 3.6 Graph bank completeness audit + hardening

`graph.py:43` banks `out/soong/{build.ninja, .bootstrap, .glob, .minibootstrap, soong.variables}`. Four entries are missing for the bypass to work from a *cold* `out/` (turbo slots, post-error recovery):

```python
# graph.py — hardening (pseudo-diff)
GRAPH_ENTRIES = ("build.ninja", ".bootstrap", ".glob", ".minibootstrap",
                 "soong.variables", "soong.environment.used")
GRAPH_OUT_ENTRIES = (                     # top-level out/ files the bypass needs
    "combined-*.ninja", "build-*.ninja", ".ninja_log", ".ninja_deps",
)
GRAPH_FINGERPRINT = "fingerprint.json"    # see below

def graph_fingerprint(build_root: Path) -> dict:
    """Cheap freshness proof: (path, size, mtime_ns) of every .bp/.mk file."""
    ...
# bank_graph(): stage GRAPH_ENTRIES + glob(GRAPH_OUT_ENTRIES) + fingerprint.json
# restore_graph(): unpack, then re-verify fingerprint against the live tree —
#                  mismatch => return False (caller falls back to soong path)
```

`fingerprint.json` (sorted `[path, size, mtime_ns]` triples of the `.bp`/`.mk` set, ~1.5 MB) converts guard G3 from a per-slot `find` into a banked artifact the restore step can diff in milliseconds — and gives the fleet graph-master (§5.2-S6) a machine-checkable proof that the graph it is shipping matches the tree the GitHub slots will restore.

### 3.7 The gains model (measured inputs only)

| Cost item, per resume slot | Today (soong path) | With bypass |
|---|---|---|
| State restore (unavoidable) | 8–12 min | 8–12 min |
| Graph restore (new, cold `out/` only) | — | ~1–2 min (300–500 MB parts) |
| **soong bootstrap + analysis** | **29–33 min + 50–70% death probability** | **0** |
| ckati | 2–4 min (usually clean) | 0 |
| Ninja execution | identical | identical |
| **Per-slot fixed cost** | **~45–50 min + risk** | **~10–14 min, zero analysis risk** |

An 8-slot A17 campaign (config `slices: 8`) saves **4.6–5.5 hours of runner time** and eliminates the class of failures that killed Run #54 Slots 3–5 and Run #58 — which, at the observed ~35-minute burn per dead slot plus chain stall, cost the project roughly **a full day of wall-clock in the two runs examined alone**. The bypass is the single highest-ROI change in this document, and it is also the cheapest.

### 3.8 Risk register

| Risk | Likelihood | Impact | Countermeasure |
|---|---|---|---|
| Stale graph (source drifted under a fixed mhash) | Very low (requires mhash collision) | Wrong build | G3 fingerprint check; fallback to soong path; kill-switch G0 |
| Env-sensitive kati rules (bare tool names) | Low (kati bakes absolute paths; envsetup provides PATH anyway) | Edge failures | I4 launcher parity; single-slot A/B validation V2 (§7); auto-fallback |
| Ninja version drift (`prebuilts/build-tools` ninja vs banked `.ninja_log` format) | None (toolchain inside mhash, I7) | — | I7 |
| Corrupted `.ninja_deps` after an interrupted bank | Low (SIGINT path is the designed-consistent stop) | Spurious rebuilds | SHA256SUMS part verification (already enforced, `chunker.py:208-226`); fallback |
| Operator goal not in frozen graph | Medium (target overrides are a normal workflow input) | `unknown target` | Auto-fallback + INDEX tag; turbo goal existence pre-checked against `ninja -t targets` list banked with the graph |
| Psychological: "we no longer see `analyzing Android.bp`" | Certain | None | Log `slice mode: ninja-direct` + `PROGRESS:` notices continue; telemetry unchanged |


---

## 4. Dilemma B — The Memory & cgroup Shield ("Steel Envelope") for 16 GiB Runner Stability

The RFC asks for "the combination of Go runtime tunings, JVM heap controls, or Linux cgroups that will **guarantee** that memory never spills into slow swap and compilation never locks up." The honest, measured answer has two parts: **(a) no userspace knob combination can bound a 30–34 GiB live set on a 26.5 GiB machine — the guarantee must come from the kernel; (b) the guarantee you actually want is not "never spills" but "fails in seconds, banks cleanly, never livelocks, never loses the runner."** This section specifies exactly that.

### 4.1 The physics: the memory budget, per phase

| Phase | Peak anon (measured/derived) | What must be true on 15.6 GiB RAM |
|---|---|---|
| Bootstrap compile (Go build of `soong_build`) | 4–8 GiB across ≤4 parallel compile processes | fits in RAM; serialize with `GOFLAGS=-p=2` (§4.4) |
| **Analysis (`soong_build`)** | **30–34 GiB live** | **does not fit — ever.** Either (i) don't run it here (§3), or (ii) provide 20+ GiB of *fast* swap (zram-lz4) and accept a 8–33 min crawl, or (iii) fail fast at a kernel boundary |
| Ninja exec: native (clang, -j 8) | 8 × ~0.4–0.6 GiB ≈ 4–5 GiB | fits |
| Ninja exec: java bursts (javac/r8/d8/metalava) | 10–14.5 GiB observed (Slot 2 19:56, 20:01 telemetry spikes) | fits *barely* with 5–8 GiB swap cushion — JVM heap caps (§4.5) remove the spikes |
| Ninja exec: image/link (lld, soong_host tools) | 6–10 GiB | fits with cushion |

### 4.2 Shield architecture: three layers

**L1 — Kernel envelope (the actual guarantee).** Wrap the *entire build process family* (the `bash -c` launcher's session) in a cgroup v2 subtree with phase-dependent bounds:

```
/sys/fs/cgroup/romforge.slice/
├── analysis/      memory.max=14G  memory.swap.max=12G  cpu.max=350000/100000
└── exec/          memory.max=13G  memory.high=11G       memory.swap.max=6G   cpu.max=380000/100000
```

Semantics chosen deliberately:

- `memory.max` is a **hard** cap: when the cgroup's anon total hits it, the kernel OOM-kills the *offending process inside the cgroup* in seconds — `soong_build` dies with a kernel-provided forensics trail, the runner daemon (outside the cgroup, `oom_score_adj=-1000` from `env.py:220-234`) never even notices, and ROMForge's SIGINT-free error path banks `out/` (which at analysis-time is simply the restored state — nothing lost) and the DAG re-routes. **This converts the 13-minute VM execution into a ≤4-second process kill.** It is the single most important line in this section.
- `memory.swap.max` bounds how deep the crawl can go *before* the hard wall — analysis gets 12 GiB of swap allowance (the crawl that completed in Run #54 Slot 2), exec gets 6 GiB (enough for javac cold pages, not enough to livelock).
- `cpu.max` at 350–380% of one CPU reserves **0.2–0.5 vCPU for the runner daemon and kernel housekeeping even at full saturation** — the heartbeat is starved only when all 4 vCPUs are gone, and with a quota the daemon always has schedulable capacity. This directly addresses the exit-143 mechanism (§2.5 step 4).
- `memory.high` on the exec slice soft-throttles reclaim before the hard wall — page cache pressure resolves gracefully instead of exploding.

**L2 — Runtime envelope (per-phase env matrix).** Replace the single global `GOMEMLIMIT=11GiB` (`engine.py:86`) with a phase-stated matrix; the critical correction is that **the analysis phase gets either no limit or a limit ≥ total anon capacity — never one below the live set**:

| Phase | Env | Rationale (Go runtime semantics) |
|---|---|---|
| Bootstrap compile | `GOFLAGS="-p=2"`, `GOMEMLIMIT=3GiB`, `GOGC=50`, `GOMAXPROCS=2` | Each parallel `go compile` process bounded individually; 2 at a time × 3 GiB + overhead fits RAM. Serializing costs ~2–4 min once per cold graph; irrelevant after graph banking. |
| **Analysis** | `GOMEMLIMIT` **unset** (or `= total_swap_capacity − 2 GiB`), `GOGC=400`, `GOMAXPROCS=4`, `GODEBUG=gctrace=1` | Live set exceeds any sub-capacity limit → a low limit means continuous GC assist = CPU starvation (measured, §2.3). `GOGC=400` lets the heap grow into swap smoothly and *reduces* mark/sweep frequency — fewer whole-heap passes over swapped pages. `gctrace` gives P0 the forensic record. |
| Ninja exec | `JAVA_TOOL_OPTIONS=-Xmx2560m -XX:+UseG1GC` (see §4.5); no GOMEMLIMIT (Go processes here are small: ninja workers, host tools) | The observed 14.5 GiB RAM spikes are JVM bursts; heap caps remove them at the source. |

**L3 — PSI tripwire (the anti-freeze watchdog).** A new engine thread, sibling of `budget_watchdog` and `disk_watchdog` (`engine.py:216-276`), that reads `/proc/pressure/memory` and kills the build *gracefully* on sustained pressure — before the runner daemon starves:

```python
# engine.py — memory watchdog (pseudo-diff, full code in Appendix C)
def memory_watchdog() -> None:
    stall_since = None
    while not stop.wait(2.0):
        psi = _read_psi("memory")            # full avg60 / some avg300
        swap_full, ram_pct = _mem_snapshot()  # /proc/meminfo, as live_heartbeat does
        hard_stall = psi.full_avg60 > 95.0 or (swap_full and ram_pct > 96.0)
        if hard_stall:
            stall_since = stall_since or time.time()
            if time.time() - stall_since > 90:      # 90 s of sustained livelock
                log.warn("MEM-STALL: PSI full avg60=%.1f swap=%s ram=%.0f%% — "
                         "SIGINT build for consistent bank" % (...))
                _graceful_stop(STOP_MEMORY)          # new taxonomy member
                return
        else:
            stall_since = None
```

with `STOP_MEMORY = "memory"` added to the taxonomy (`engine.py:44-46`), `classify_exit` mapping it to a **new `mem-stall` classification** (not `sliced` — the DAG must treat it differently: route the *next* attempt to the fleet or to a degraded profile, not to another identical GH slot), and the INDEX carrying `stop_reason=memory` so the conveyor can act on it (`forge.yml:1037-1084` postcheck already threads `stop_reason`).

The three layers compose into a guarantee: **L3 catches the stall within 90 s and banks gracefully; if L3 is somehow late, L1 kills the offender within seconds and the error path banks; under no path does the VM livelock long enough for GitHub to execute it.** Exit 143 becomes structurally unreachable from memory pressure.

### 4.3 The swap stack recalibration (and the silent-failure fix)

Current state (v3): zram zstd 8 GB p100 (when it activates — it didn't, §2.4) + `.forge-swap` 6–8 GB on `/mnt` (the same SSD as `forge.img`) + up to 6 × 2 GB dynamic chunks, also on `/mnt`, gated on `physical_free_gb > 12` (§2.6). `vm.swappiness=60` (`env.py:222`). Recommended replacement:

| Knob | Today | Target | Why |
|---|---|---|---|
| zram algorithm | zstd (`env.py:283,288`) | **lz4** | Page-fault latency is the livelock currency: zstd decompress ≈ 200–500 MB/s/core; lz4 ≈ 2–5 GB/s/core. The pages being faulted are GC-swept Go heap — they must come back *fast*. (zstd's 20–30% better ratio is irrelevant when the alternative is a 13-minute freeze.) |
| zram disksize | 8 GiB | **6 GiB** | A full 8 GiB zram consumes up to ~3 GiB of physical RAM for compressed pages (Go heap compresses ~2.5–3:1); 6 GiB keeps the resident cost ≤ ~2.2 GiB. |
| `vm.swappiness` | 60 | **10** during analysis, 40 during exec | Swappiness 60 tells the kernel to prefer swap-out aggressively; with zram-lz4 that is cheap, but during *analysis* the pages that matter are the live heap — pushing them out just to fault them back is the thrash recipe. 10 keeps anon resident until real pressure. |
| Disk swap | 6–8 GB fixed + ≤6 chunks | **4 GiB fixed, on the backing mount, priority 10**; dynamic chunks **disabled during analysis** (L3 + L1 own that phase) and capped at 2 chunks during exec | Decouples the analysis cushion from *disk free* — the §2.6 death spiral. The kernel envelope, not the disk, is the bound. |
| `vm.page-cluster` | default (3) | **0** | Swap readahead is toxic with zram (faulting 8 pages to read 1) and mediocre on SSD. |
| `vm.watermark_scale_factor` | default | **125** | Finer reclaim granularity → earlier, gentler reclaim instead of cliff. |
| zram writeback | n/a | **off** | No backing device on runners; writeback would target the same SSD. |

**The loud-failure fix** (the §2.4 silent zram death):

```python
# env.py — ensure_zram returns topology, prepare() must REPORT it (pseudo-diff)
zram_ok = fenv.ensure_zram(6, algo="lz4")
if not zram_ok:
    log.warn("zram UNAVAILABLE on this runner — memory shield degraded: "
             "analysis phase will run with disk-swap-only bounds; "
             "forcing FORGE_ANALYSIS_PROFILE=slow (see engine)")
    # and record it where the slot log greps it:
    log.out("swap_topology", fenv.swap_topology_report())   # zram: yes/no, algo, size,
                                                            # disk swap, chunks, swappiness
```

`swap_topology` in every slot log makes the shield *auditable*: the next Run #58-style incident answers "was zram on?" in one grep instead of a forensic reconstruction.

### 4.4 The `GOMEMLIMIT` correction, precisely

Because this is the knob the team reached for and it backfired, the replacement rules deserve their own statement:

1. **Never set a soft limit below a phase's live set.** Analysis live set: 12–20+ GiB (growing). Limits below that produce GC assist saturation — measured: Run #58 crawled at `-j8` with GC stealing the mutator's CPU and died at 13 min; the identical analysis on A17 *without* the limit completed in 29 min with more swap. If you must set one: `GOMEMLIMIT = (RAM + swap_total) − 2 GiB`, i.e. ~33–35 GiB in the recalibrated stack — a backstop, not a throttle.
2. **Set per-process limits only for the parallel compile family** (bootstrap: `GOFLAGS=-p=2 GOMEMLIMIT=3GiB`), where each process's live set is genuinely small and the aggregate is what matters.
3. **The aggregate bound is the kernel's job** (L1 `memory.max`), because no userspace runtime cooperates across process families (Go compiler procs, soong_build, JVMs, clang).

### 4.5 JVM envelope

Soong's java toolchain processes (javac workers, kotlinc daemon, d8/r8, metalava) are the RAM spikes of the exec phase (measured 14.5 GiB spikes at 19:56/20:01 in Slot 2). One environment variable caps them all, because every JVM honors it:

```bash
JAVA_TOOL_OPTIONS="-Xmx2560m -XX:+UseG1GC -XX:MaxGCPauseMillis=200"
```

Caveats, stated honestly: (a) JVMs print a `Picked up JAVA_TOOL_OPTIONS` notice — harmless in logs; (b) a few Soong rules pass their own `-Xmx` on the command line, which **overrides** the env var for that process (r8's wrapper does in some branches) — the cgroup L1 bound catches those; (c) 2560m is sized for the largest single javac (framework/base) — smaller modules waste some heap, but G1 returns it under pressure. Expected effect: the exec-phase RAM ceiling drops from ~14.5 GiB observed to ~9–10 GiB, restoring a ~5 GiB cushion that today only exists because of swap.

### 4.6 cgroups v2 on GitHub-hosted runners — availability ladder

GitHub's Ubuntu 22.04/24.04 images boot with cgroup v2 unified hierarchy and give the job passwordless sudo. Two of the three rungs have been verified to work on hosted runners by the community (cgroup creation under the job's own slice with delegated controllers, and `systemd-run` scopes); implement as a ladder:

```python
# engine.py — cgroup best-effort (full code in Appendix C)
def _shield_cmd(build_root, phase) -> Optional[List[str]]:
    if _have_delegated_cgroupv2():                      # rung 1: write to
        _write_cgroup_limits(phase)                     # /sys/fs/cgroup/.../romforge/{analysis,exec}
        return ["sudo", "systemd-run", "--scope", "--quiet", "--slice=romforge",
                f"--unit=forge-{phase}", ...]           # (moves Popen into the slice)
    if _have_systemd_run():                             # rung 2: pure systemd-run
        return ["sudo", "systemd-run", "--scope", "--quiet",
                f"-p MemoryMax={MAX[phase]}",
                f"-p MemorySwapMax={SWMAX[phase]}",
                f"-p CPUQuota={CPUQ[phase]}%", ...]
    return None                                          # rung 3: PSI watchdog only
# Popen(["bash","-c",launcher] if shield is None else [*shield_cmd, "bash","-c",launcher], ...)
```

If all three rungs fail, L3 (PSI watchdog) alone still prevents VM death — it is the floor, not the ceiling, of the guarantee.

### 4.7 Runner-heartbeat protection, summarized

| Threat to the daemon | Mitigation |
|---|---|
| CPU starvation by kswapd0 + build | `cpu.max` 350–380% on the build slice (L1); `nice -n 10` + `ionice -c3` on the launcher as belt-and-braces |
| OOM-killing of the daemon | already correct: `oom_score_adj=-1000` (`env.py:230-233`) — keep it, but **stop extending it to `forge_core` blanket-style**; the orchestrator deserves it, but it must never mask an OOM event we *want* to see |
| Daemon's own disk needs on `/` | already correct: root-disk watchdog + purge (`engine.py:48-54, 243-251`) |
| Page-cache eviction (daemon TLS buffers) | `memory.high` soft throttle (L1) + swappiness 10 (§4.3) |

### 4.8 Fleet-tier memory spec (where analysis *should* live)

`docs/FLEET_SETUP.md` recommends 16–64 cores, 64–256 GB RAM, zram zstd 32 GB, `vm.swappiness=100`. Corrections for the fleet tier:

- **Analysis fits in RAM on a 64 GiB node** (33.8 GiB peak measured): soong_build completes in ~5–8 min with zero swap interaction. The graph-master slot (§5.2-S6) belongs here; with `mine.gate(is_fleet=True)` fast-path claims (`mine.py:139-175`) the routing already exists — only the *role assignment* (graph-master vs builder) is new.
- Fleet zram: **lz4**, 16–32 GiB, fine — but zstd + swappiness 100 as written in the runbook is tuned for *page-cache-heavy* workloads (ChromeOS-style), not a 34 GiB anonymous heap under a mark-sweep GC. Same reasoning as §4.3.
- Fleet as the *default analysis owner* also collapses the GH-tier shield problem: GitHub slots never see >10 GiB anon once the bypass lands; the §4.2 envelope then has huge margins.

### 4.9 Failure-mode decision table (what kills what, and how fast)

| Scenario (recurrence of the measured incidents) | v3 behavior | Shield behavior |
|---|---|---|
| Analysis on GH, 26 GiB capacity, live set 30+ GiB | 13-min livelock → VM eviction (143) → unbanked slot, red job, chain stall | zram-lz4 present: crawl completes in 8–15 min (more capacity, 10× faster faults); otherwise cgroup OOM-kill in ≤4 s → error classification → bank → DAG re-routes to fleet/degraded profile |
| Exec-phase javac burst + swap filling | near-misses observed (14.5 GiB RAM spikes) | `JAVA_TOOL_OPTIONS` caps remove the spike; `memory.high` soft-throttles; PSI watchdog never fires in anger |
| kswapd0 spinning with swap full | 41 s freeze → kill | L3 SIGINT at 90 s *max* — normally the cgroup wall fires minutes earlier; either way the out/ bank is consistent and the slot ends green-with-reason |
| Runner daemon starvation | heartbeat loss, exit 143 | `cpu.max` quota keeps 0.2–0.5 vCPU schedulable for the daemon; exit 143 becomes unreachable from memory pressure |


---

## 5. Dilemma C — Extreme Parallelization & the Path to Sub-2-Hour Cycles

### 5.1 The measured compute model (everything derives from Run #54)

```
Graph:                82,735 edges total
Observed throughput:  3.2–3.4 edges/s   (mid-graph, Java/dex, -j8, Zen 5 4-vCPU)
                      ~0.25–0.3 edges/s (early native section — slot 1's regime)
Pure ninja time:      6.8–7.5 h   (mid-graph rate for ~70% of edges + early-native
                                    rate for the first ~5–8% + packaging tail)
Critical path floor:  ~2.5–3 h    (framework javac → dex merge → apk/img chains;
                                    bounded below by the longest serial dependency
                                    chain, not by available cores)
Per-slot overhead:    34–48 min   (prepare + 9–12 min src restore + 8–12 min state
                                    restore + 13–14 min bank)
Cold campaign (8 slots, soong path): 6.8–7.5 h compute + 8 × (0.6–0.8 h overhead)
                                    + ~35 min × (dead-slot lottery)  ≈  15–18 h wall
```

Every projection below is this model with one term changed at a time — no hand-waving. Full derivations in Appendix F.

### 5.2 The strategy stack, ordered by ROI

**S1 — Ninja Bypass (§3).** Removes 29–33 min + the death lottery per slot. Campaign: 15–18 h → **8–11 h** (with fleet graph-master: analysis happens once, in ~6–8 min, on hardware where it fits).

**S2 — Turbo re-architecture, and *why `vendor_boot`/`dtboimage` failed*.** The RFC asks directly why these two prewarms failed. Three compounding causes, in order of lethality:

1. **They were cold slots running full soong analysis with a 9 GiB disk-only swap budget** — the exact profile that killed Slots 3–5 and Run #58 (§2.6). A turbo slot is `cli.py:514-533` → `run_slice(..., allow_missing_deps=True)` on a fresh `out/`; nothing in that path restores the graph before building (the graph restore at `cli.py:551-557` fires only on the cold-*main* path when no state tag exists). Six partitions × a 50–70% analysis death chance, at `max-parallel: 6` (`forge.yml:243`) — most prewarms die before compiling a single module, and `ALLOW_MISSING_DEPENDENCIES` never gets the chance to matter. **Fix: graph restore + bypass for turbo slots (§3.5).** This alone likely revives `vendor_boot` and `dtboimage`.
2. **Goal-name validity.** `turbo.DEFAULT_PARTITIONS[17]` (`turbo.py:48`) lists `vendor_boot` — the *image module* name. Whether the ninja goal `vendor_boot` exists in the banked graph (vs. `vendorbootimage`, or a device-specific alias) depends on the target product's make/soong wiring; an unknown goal is an instant ninja error. **Fix: bank `ninja -t targets` output with the graph (§3.6) and pre-validate every turbo goal before dispatching a runner** — a 2-second check that saves 350-minute slots.
3. **`ALLOW_MISSING_DEPENDENCIES` semantics at image level.** With AMMD, soong silently replaces unresolvable deps with stubs. For a partition image, a stubbed dep means the *image content* is subtly incomplete — the prewarm "succeeds" but the assembled ROM carries dummies unless the final link rebuilds them. This is why `turbo.py:16-21`'s honesty contract requires the final `m` to run *without* AMMD. Which brings us to the correctness bug found in the config:

> **`configs/roms/lineage-a17-shiba.yaml` sets `ALLOW_MISSING_DEPENDENCIES: "true"` in the global `env:` block.** This exports AMMD for the main chain *and the final ROM build* (`engine.build_env()` merges `plan.rom.env` into the launcher env, `engine.py:88-89`). The final verification build cannot "re-link everything still stale" if missing deps are being stubbed in the shipped image itself. **Move AMMD to turbo slots only** (`run_slice(..., allow_missing_deps=True)` already does this for turbo — the config's global value must become `false`). The 14-point gate (`gate.py`) would catch gross breakage, but stub-level inconsistencies (a missing vendor lib silently absent from vendor_boot) are exactly the class a hard gate misses.

**S3 — CAS-Relay: cross-campaign, content-addressed module cache.** The state relay is *per-campaign* (keyed `state-<key>-s<N>`); a new `mhash` (weekly LineageOS manifest bump) starts from zero even though ~95–98% of modules are bit-identical. CAS-Relay banks **module-level intermediates** — `out/soong/.intermediates/<module-path>/` directories — keyed by `sha256(ninja command line for the module's edges + input content fingerprints)`, into release tags `cas-<key>-<algo>` with a JSON manifest. On a new campaign: join the current graph's module set against the manifest (a `jq`-scale operation), download the hits (~10–30% of a 17 GiB state), and ninja's mtime+command-hash checks accept them directly. Expected hit rate on weekly bumps: 60–85% (modules whose transitive inputs and commands are unchanged). Cold-campaign effect: **8–11 h → 4–7 h**. Bandwidth math in §5.3. This is also the *only* free-tier path to sub-2 h (warm CAS, §5.5).

**S4 — ccache for the clang family.** `USE_CCACHE` today defaults off and `engine.build_env()` only enables it when the config sets it (`engine.py:90-96`) — with modern Soong there is no first-class ccache hook, so the interposition point is the prebuilt toolchain itself: swap `prebuilts/clang/.../bin/clang` → `clang.bin` + a wrapper invoking `ccache clang.bin`, with `CCACHE_DIR` on a `nodatacow` subvolume (btrfs compression double-pays on cache hits), `CCACHE_COMPILERCHECK=content` (binary hash — correct across runners), `CCACHE_NOHASHDIR=1`, and the ccache dir banked per campaign like state. Hit rates on a weekly re-sync: 70–90% for C/C++. Cost: ~5–8% hashing overhead on misses. Worth it *after* S3 (S3 covers .o via intermediates too; ccache additionally survives *toolchain* bumps, which S3's command-hash keys do not).

**S5 — Java graph sharding (experimental, P4).** The framework javac chain is the critical path. Two-pass, stub-first: pass 1 compiles each module shard against API stubs (as IDEs and Google's internal turbo do); pass 2 merges and dexes. This requires module-graph partitioning (community detection on the dependency graph is enough — the partition is computable *from the banked ninja graph*, offline, once per mhash) and per-shard `ALLOW_MISSING_DEPENDENCIES` runs. Estimated: 20–40% off the critical path at 20 slots. Risk: high (stub/impl drift, javac strictness). Position: P4, behind S3, only if fleet stays unavailable.

**S6 — Fleet hybrid topology (the sub-3h cold answer).** Roles, not just runners:

| Role | Runner | Work | Wall |
|---|---|---|---|
| graph-master | fleet 16-core/64G | `m nothing` (AOSP's graph-only goal) + bank graph + fingerprint | 6–10 min |
| partition workers × 6 | GH slots (bypass + graph) | turbo partitions, each 45–90 min of ninja | 45–90 min, parallel |
| critical-path builder | fleet 16-core | system/framework chain + dex + images (2.5–3 h serial floor at 16 cores ≈ 50–70 min) | 50–70 min |
| assembler | fleet | merge GH turbo states (`turbo.merge_turbo_states` streams directly into `out/`, `turbo.py:78-101` — already zero-staging) + OTA packaging | 15–25 min |
| verifier | GH slot | 14-point gate on product-state (2–4 GiB, `relay.py:161-200`) | ~5 min |

Cold campaign wall: **~1.5–3 h** (GH partition workers and the fleet critical-path builder run concurrently; assembler waits for the later of the two). Runner-minutes: 3–5 h GH + 2–3 h fleet. All routing primitives exist (`mine.gate(is_fleet=True)`, `plan` job's runner probe at `forge.yml:140-166`); what's new is the *role matrix* in the plan contract and the conveyor re-dispatch carrying a role instead of a bare "slot N".

### 5.3 Conveyor v3.1: role DAG + CAS delta protocol

Two protocol changes make the 20-slot tier worth its overhead:

1. **Role-specialized re-dispatch.** `forge.yml`'s slot chain (10 sequential jobs, `forge.yml:276-1031`) becomes: `plan → graph-master → [6 turbo workers ∥ critical-path] → assembler → postcheck → verify → publish`, with the conveyor (`forge.yml:1159-1184`) re-dispatching *by role and by INDEX gap* rather than by slot number — e.g. "partition `vendor_boot` state missing → re-dispatch one `turbo:vendor_boot` worker" instead of re-running an entire sequential slot chain to reach it. The F3 candidate-count bug from the baseline (re-dispatch with `candidates=8` vs `20`) is already fixed in v3 (`forge.yml:1180` propagates `$CANDIDATES`) — keep that.
2. **Delta banking.** `relay.bank()` re-packs the entire `out/` (9 parts, 17 GiB, 13–14 min) every slice. With CAS manifests, a slice banks only *newly-completed module dirs* (the delta between the current `.ninja_log` output set and the previous bank's manifest): expected 3–6 GiB per mid-campaign slice → **4–6 min bank, 3–5 min restore**. The SHA256SUMS machinery, stream sink, and part verification (`chunker.py:124-207`, `store.py:187-193`) are reused unchanged — the manifest is just a file list with per-file hashes instead of per-part.

### 5.4 Wall-clock projections (derived, not asserted)

| Configuration | Cold wall | Warm wall (same mhash) | Runner-min (cold) |
|---|---|---|---|
| v3 as-is | 15–18 h | 13–15 h | 17–21 h |
| + Shield (P1) | 14–17 h | 12–14 h | 15–19 h |
| + Bypass & fleet graph-master (P2) | 8–11 h | 7–9 h | 9–12 h |
| + Turbo re-arch (P2) | 6–9 h | 5–7 h | 7–10 h |
| + CAS-Relay & delta bank (P3) | 4–7 h | **20–50 min** | 5–8 h |
| + Fleet hybrid roles (P3/P4) | **1.5–3 h** | **15–35 min** | 3–5 h GH + 2–3 h fleet |

### 5.5 Why sub-2-hour, free-tier-only, cold, is physically impossible — and what to claim instead

The 15-hour build needs ~52–60 vCPU-hours of edge compute (82,735 edges at the measured per-core rate). Finishing in under 2 h requires ≥26–30 *continuously effective* vCPUs. The free tier *has* 20 slots × 4 vCPU = 80 vCPU, but: (a) each slot pays 10–14 min of relay tax even in the best case (§3.7), ~10–20% of its 105-minute budget at that concurrency; (b) the graph's critical path (framework javac → dex → apk → image chains) is ~2.5–3 h *serial*, and only a fleet core count shortens serial chains; (c) GitHub's 20-job concurrency cap on free tier includes the mining matrices' discarded candidates (currently 10 slots × 20 candidates — the discard waves themselves saturate the org's concurrency window, which is why `mine.gate`'s fast-discard matters, and why fleet routing with `mining: ["solo"]`, `forge.yml:160-166`, is not just a speed feature but a *concurrency* feature). **Honest position for the docs: free-tier cold floor is ~3 h with perfect sharding; sub-2 h is fleet-hybrid (cold) or free-tier-with-warm-CAS (steady state).** The RFC's own framing ("15 hours to <2 hours on 4 vCPUs") conflates single-runner time with campaign wall — this blueprint's §5.4 table is the defensible version.

### 5.6 Where the hours actually go (from the Slot 2 log)

| Segment | Share of the 5h13m slot |
|---|---|
| src + state restore (21.7 min) | 7% |
| soong analysis death-march (29–33 min) | 10% |
| ckati + combined (2–4 min) | 1% |
| **ninja execution (235 min)** | **75%** — of which Java/dex/apk bursts dominate the 17:00–21:00 window (telemetry shows javac-family RAM spikes at 19:56/20:01 and `//frameworks/...` module churn) |
| bank (13.6 min) | 4.3% |

With `WITH_DEXPREOPT: "false"` already set (`lineage-a17-shiba.yaml`), the remaining big levers on the 75% are S3/S4/S5/S6 — and, awkwardly, the fact that `-j 8` (`engine.optimal_jobs`, `engine.py:104-152`) oversubscribes 4 vCPUs by 2× during the Java bursts; with the §4.5 JVM caps in place, `-j 6` is the safer default (the config already honors `FORGE_JOBS` as an override — keep that escape hatch).


---

## 6. Implementation Roadmap

Phased, each item independently shippable and independently revertible. Effort in engineer-days (1 d = focused day of one senior engineer). "Benefit" cites the measured term it removes.

### P0 — Instrumentation before optimization (0.5–1 d, do first, zero risk)

| # | Item | Where | Why |
|---|---|---|---|
| P0.1 | `PHASE_TIMING` stamps: bracket soong bootstrap / analysis / ckati / ninja-start with monotonic timestamps, emit one JSON line at slice end | `engine.run_slice` | Every projection in §5 becomes a per-slot measurement instead of a model |
| P0.2 | `GODEBUG=gctrace=1` during analysis (opt-in via `FORGE_GCTRACE=1`) + capture to log artifact | `engine.build_env` | Settles T1 (§2.7) with data: heap trajectory, GC assist fraction |
| P0.3 | One diagnostic slot with `ninja -d explain -f out/soong/.bootstrap/build.ninja out/soong/build.ninja` (dry-run, `-n`) before the real build; upload the explain diff | workflow opt-in input | Answers *why* the restored graph was dirty on Run #58 — closes §2.7 with evidence |
| P0.4 | `swap_topology` log line (zram on/off, algo, disk swap, chunks, swappiness) at prepare + every 5 min in telemetry | `env.py`, `engine.live_heartbeat` | Makes the §2.4 silent-failure class visible forever |
| P0.5 | PSI capture: append `/proc/pressure/memory` to the heartbeat line | `engine.live_heartbeat` | Baseline for the L3 tripwire calibration |

### P1 — The Shield (1.5–2 d)

| # | Item | Where | Benefit (measured) | Risk / kill-switch |
|---|---|---|---|---|
| P1.1 | Remove global `GOMEMLIMIT=11GiB`; install per-phase matrix (§4.4) | `engine.build_env` | Un-does the measured GC-assist regression; analysis reverts to "slow but finishes" | Low; `FORGE_SOONG_MEM_LIMIT` stays as override |
| P1.2 | zram: lz4, 6 GiB, swappiness 10/40, page-cluster 0, chunk policy (§4.3) + **loud failure + `swap_topology`** | `env.ensure_zram`, `cli.prepare` | Raises effective fault throughput 10×; decouples analysis survival from disk free | Low; degrade path is today's behavior |
| P1.3 | PSI memory watchdog + `STOP_MEMORY` / `mem-stall` classification (§4.2 L3) | `engine.run_slice`, `dag` | Converts VM eviction (unbanked, red, 34 min) into graceful bank (green-with-reason, ~20 min) | Low; threshold env-tunable `FORGE_PSI_STALL_S` |
| P1.4 | cgroup envelopes via the §4.6 ladder | `engine.run_slice` | Hard 4-second kill of any runaway; CPU headroom for the daemon | Medium (sudo/cgroup availability) — ladder degrades to P1.3 |
| P1.5 | `JAVA_TOOL_OPTIONS=-Xmx2560m -XX:+UseG1GC` for exec phase | `engine.build_env` | Removes the 14.5 GiB javac spikes; restores ~5 GiB cushion | Low; per-tool `-Xmx` overrides still bounded by L1 |
| P1.6 | Move `ALLOW_MISSING_DEPENDENCIES` out of the global rom env (§5.2-S2) | `lineage-a17-shiba.yaml` | Restores the turbo honesty contract; shipped images no longer carry stubs | Requires one clean full build to validate; config-only revert |

### P2 — The Bypass (2–4 d) — the centerpiece

| # | Item | Where | Benefit | Risk / kill-switch |
|---|---|---|---|---|
| P2.1 | `bypass_ready()` guard + `ninja-direct` launcher + `NINJA_STATUS` (§3.3) | `engine.run_slice` | 29–33 min + death lottery → 0 per resume slot | `FORGE_NINJA_BYPASS=0`; auto-fallback on structural failure |
| P2.2 | Graph bank completeness: combined + kati + `.ninja_log/.ninja_deps` + `fingerprint.json` + targets list (§3.6) | `graph.py` | Turbo slots and post-error slots can bypass from cold `out/` | Graph tag version bump; old tags ignored |
| P2.3 | Turbo slots: graph restore before build + goal pre-validation vs banked targets (§3.5) | `cli.cmd_slice` turbo branch | Revives `vendor_boot`/`dtboimage` prewarms; 45 min → 12 min to first edge | Falls back to current behavior per-partition |
| P2.4 | Fleet **graph-master** role: dedicated job running `m nothing` + `bank_graph` on the fleet node when online; GH fallback = shielded cold slot | `forge.yml` new job after `sync` | Analysis cost of the whole campaign: one 6–10 min fleet job instead of N × 29-min GH crawls | `force_hosted` input already exists |
| P2.5 | `mem-stall` → fleet/degraded-profile re-route policy | `dag`, conveyor | The 143-class failures stop repeating identically | Policy env: `FORGE_MEMSTALL_POLICY` |

### P3 — Turbo re-architecture + CAS-Relay (1–2 weeks)

| # | Item | Benefit | Risk / kill-switch |
|---|---|---|---|
| P3.1 | Role-DAG conveyor (§5.3): partition-scoped re-dispatch instead of sequential slot chain | Failure blast radius shrinks from campaign to partition; 20-slot tier finally saturable | Medium — big workflow diff; keep the old chain behind a `FORGE_TOPOLOGY=legacy` flag |
| P3.2 | CAS-Relay: module-intermediate manifest + delta banking (§5.2-S3, §5.3-2) | Cold-with-history: 8–11 h → 4–7 h; warm: 20–50 min; bank/restore 4–6 min | Medium — manifest correctness; kill-switch: full-state banking flag |
| P3.3 | ccache interposition (§5.2-S4) | Cross-toolchain-bump .o hits | Low–medium; opt-in per rom env |
| P3.4 | Fleet hybrid roles end-to-end (§5.2-S6) | Cold 1.5–3 h | Medium; falls back to GH-only automatically when fleet offline |

### P4 — Frontier (explicitly experimental)

Java graph sharding from the banked ninja graph (§5.2-S5); REAPI-compatible remote-execution seam (workers speak the CAS protocol natively so a future RBE/Buildbarn cluster is a drop-in); zstd → zstd:1 relay switch already landed, evaluate `--long=31` window on part boundaries.

---

## 7. Validation Plan (acceptance criteria per phase)

| ID | Check | Pass criterion |
|---|---|---|
| V1 (P1) | Repeat the Run #58 scenario (slot resumes `…-s2` with 9 GiB free disk) | No exit 143 *ever*; worst case: `mem-stall` classification + bank within 20 min; slot log contains `swap_topology` |
| V2 (P2) | A/B slot pair on the same state: one `FORGE_NINJA_BYPASS=0`, one default | Bypass slot's log contains `slice mode: ninja-direct`, **zero** occurrences of `bootstrap blueprint`, first `[N% d/t]` line within 4 min of state restore; both slots produce identical `.ninja_log` output counts ±0.1% |
| V3 (P2) | Turbo `vendor_boot` prewarm with graph bank present | Reaches ninja execution < 15 min from job start; banks `state-…-turbo-vendor_boot` |
| V4 (P2) | Guard negative test: corrupt one `.bp` mtime newer than the banked graph | `bypass_ready()` returns None; slot takes the soong path; log records the reason |
| V5 (P3) | CAS warm run (same mhash re-dispatch) | Wall from dispatch to ROM zip ≤ 50 min; CAS hit manifest reports ≥ 60% of edges |
| V6 (P0.3) | `-d explain` probe on one resume slot | A written answer to "which input made the bootstrap edge dirty on Run #58" in the run notes |

Instrumentation schema (P0.1), full watchdog/cgroup code (Appendix C), and the compute-model derivations (Appendix F) follow.


---

## Appendix A — Raw evidence excerpts (verbatim, with line numbers)

**A.1 Run #54 Slot 2 — the analysis that survived (peak anon ≈ 33.8 GiB):**

```text
16:22:19  [100% 1/1] bootstrap blueprint | RAM: 14.8/15.6G (Swap:  9.0/21.0G) | Disk(vol): 30.5G free
16:27:05  [100% 1/1] bootstrap blueprint | RAM: 15.0/15.6G (Swap: 11.0/21.0G) | Disk(vol): 30.5G free
16:31:51  [100% 1/1] bootstrap blueprint | RAM: 15.0/15.6G (Swap: 14.1/21.0G) | Disk(vol): 30.5G free
16:41:24  [100% 1/1] bootstrap blueprint | RAM: 15.0/15.6G (Swap: 12.8/21.0G) | Disk(vol): 30.5G free
16:46:10  [100% 1/1] bootstrap blueprint | RAM: 15.0/15.6G (Swap: 17.0/21.0G) | Disk(vol): 30.8G free
16:50:24  [100% 2/2] analyzing Android.bp files and generating ninja file a | RAM: 1.1/15.6G (Swap: 0.2/21.0G)
16:50:56  [100% 13/13] finishing Make module r | RAM: 8.7/15.6G (Swap: 0.2/21.0G)
16:55:44  [0% 633/82735] //packages/module... | RAM: 11.9/15.6G (Swap: 5.6/21.0G)
17:08:54  [5% 4143/82735] ...        <- restored clean edges recognized
20:52:26  [58% 48626/82735] ...
20:52:31  WARN slice budget spent (275 min) — SIGINT to pgid 3959 ...
21:06:09  OK stream-packed out: 9 parts shipped through sink
```

**A.2 Run #54 Slot 3 — swap-starved by disk pressure (no chunks, gate at 12 GiB free):**

```text
21:30:35  [100% 1/1] bootstrap blueprint | RAM:  1.4/15.6G (Swap: 0.0/9.0G) | Disk(vol): 9.1G free
21:32:02  [100% 1/1] bootstrap blueprint | RAM: 14.6/15.6G (Swap: 3.9/9.0G) | Disk(vol): 9.1G free
21:39:22  [100% 1/1] bootstrap blueprint | RAM: 15.0/15.6G (Swap: 9.0/9.0G) | Disk(vol): 9.1G free
21:40:03  [100% 1/1] bootstrap blueprint | RAM: 15.3/15.6G (Swap: 9.0/9.0G) | Disk(vol): 9.1G free
21:40:04  ##[error]Process completed with exit code 143.
21:40:04  Terminate orphan process: pid (3989) (soong_ui)
```

**A.3 Run #58 Slot 1 — v3 with `GOMEMLIMIT=11GiB`, zram silently absent:**

```text
03:23:00  swap on: +6 GB at /mnt/romforge/.forge-swap (target 8 GB)     <- no zram line anywhere
03:41:10  OK resumed state state-lineageosgoogleshibaa17-s2 (slice 2)
03:41:10  OK fusion slot enabled: 320m job wall budget
03:41:11  dynamic parallel jobs: -j 8
03:41:26  [100% 1/1] bootstrap blueprint | RAM:  3.4/15.6G (Swap: 0.0/9.0G)
03:43:42  OK dynamic swap auto-scale: +2 GB chunk 1 activated (RAM 95%, Swap 45%, Disk 14.0G free)
03:44:29  [100% 1/1] bootstrap blueprint | RAM: 15.0/15.6G (Swap: 5.3/11.0G)
03:53:55  [100% 1/1] bootstrap blueprint | RAM: 15.1/15.6G (Swap: 11.0/11.0G)
03:54:32  [100% 1/1] bootstrap blueprint | RAM: 15.2/15.6G (Swap: 11.0/11.0G) | budget: 4h 21m left
03:54:34  ##[error]Process completed with exit code 143.
```

Note the last telemetry line: the budget watchdog had 4h21m of runway and the *disk* watchdog had 9.3 GiB — neither protection had any opinion about memory. That is the L3 gap §4.2 closes.

## Appendix B — Per-phase environment matrix (the exact exports)

```bash
# ---------- PHASE: bootstrap-compile (inside soong_ui, applies via env) ----------
GOFLAGS="-p=2"                    # serialize go compile procs
GOMEMLIMIT=3GiB                   # per-process backstop for the compile family
GOGC=50
GOMAXPROCS=2

# ---------- PHASE: analysis (soong_build) ----------
# CRITICAL: no sub-live-set limit. Either unset, or backstop at capacity:
GOMEMLIMIT=$( python3 - <<'PY'
import re
tot = sum(int(l.split()[1]) for l in open('/proc/meminfo') if l.startswith(('MemTotal','SwapTotal')))
print(f"{tot*1024 - 2*2**30}B")
PY )
GOGC=400                           # grow into swap smoothly; fewer whole-heap sweeps
GOMAXPROCS=4
GODEBUG=gctrace=1                  # P0.2 forensics (opt-in)

# ---------- PHASE: ninja-exec ----------
JAVA_TOOL_OPTIONS="-Xmx2560m -XX:+UseG1GC -XX:MaxGCPauseMillis=200"
NINJA_STATUS="[%p %f/%t] "         # bypass mode only — keeps conveyor telemetry alive
# (no GOMEMLIMIT: exec-phase Go procs are small; cgroup owns the aggregate)

# ---------- kernel (prepare, once per job) ----------
zram: algorithm lz4, disksize 6G, swapon -p 100
vm.swappiness=10 (analysis) / 40 (exec)   # phase-toggled by the engine
vm.page-cluster=0
vm.watermark_scale_factor=125
disk swap: 4G on backing mount, priority 10; dynamic chunks: exec-phase only, cap 2
```

## Appendix C — Shield code (watchdog + cgroup ladder, production-ready shape)

```python
# forge_core/engine.py — additions (drop-in, follows existing watchdog idioms)

STOP_MEMORY = "memory"                       # joins STOP_BUDGET/STOP_DISK (engine.py:44-46)
PHASE = {"value": "exec"}                    # engine sets "analysis" during soong stage when
                                             # mode == "soong" and out/soong/build.ninja is stale

def _read_psi() -> Optional[float]:
    """full avg60 from /proc/pressure/memory, or None."""
    try:
        txt = Path("/proc/pressure/memory").read_text()
        m = re.search(r"full avg60=(\d+\.\d+)", txt)
        return float(m.group(1)) if m else 0.0
    except Exception:
        return None

def memory_watchdog() -> None:
    """L3 tripwire: SIGINT the build on sustained memory livelock (§4.2)."""
    stall_since: Optional[float] = None
    while not stop.wait(2.0):
        psi = _read_psi()
        try:
            mi = Path("/proc/meminfo").read_text()
            sw_tot = int(re.search(r"SwapTotal:\s+(\d+)", mi).group(1))
            sw_free = int(re.search(r"SwapFree:\s+(\d+)", mi).group(1))
            tot = int(re.search(r"MemTotal:\s+(\d+)", mi).group(1))
            avail = int(re.search(r"MemAvailable:\s+(\d+)", mi).group(1))
            swap_full = (sw_tot - sw_free) / max(1, sw_tot) > 0.97
            ram_pct = (tot - avail) / tot * 100.0
        except Exception:
            continue
        hard = (psi is not None and psi > 95.0) or (swap_full and ram_pct > 96.0)
        if hard:
            stall_since = stall_since if stall_since else time.time()
            if time.time() - stall_since > 90:
                log.warn(f"MEM-STALL (PSI full avg60={psi}, swap_full={swap_full}, "
                         f"ram={ram_pct:.0f}%) — SIGINT for a consistent bank")
                _graceful_stop(STOP_MEMORY)
                return
        else:
            stall_since = None

# cgroup ladder (§4.6)
CG_PHASE_LIMITS = {          # memory.max, memory.swap.max, cpu.max (µs per 100ms)
    "analysis": ("14G", "12G", "350000 100000"),
    "exec":     ("13G", "6G",  "380000 100000"),
}

def _cgroup_run_prefix(phase: str) -> Optional[List[str]]:
    """Rung 1: delegated cgroupv2 dir; Rung 2: systemd-run; Rung 3: None."""
    mem, swp, cpu = CG_PHASE_LIMITS[phase]
    try:
        if Path("/sys/fs/cgroup/cgroup.controllers").exists():
            base = Path("/sys/fs/cgroup/romforge") / phase
            subprocess.run(["sudo", "mkdir", "-p", str(base)], check=True, timeout=10)
            for f, v in (("memory.max", mem), ("memory.swap.max", swp),
                         ("memory.high", "11G" if phase == "exec" else "13G"),
                         ("cpu.max", cpu)):
                subprocess.run(["sudo", "sh", "-c", f"echo {v} > {base}/{f}"],
                               check=True, timeout=10)
            return ["sudo", "systemd-run", "--scope", "--quiet",
                    f"--unit=forge-{phase}", "bash", "-c"]
    except Exception:
        pass
    if shutil.which("systemd-run"):
        return ["sudo", "systemd-run", "--scope", "--quiet",
                f"-p MemoryMax={mem}", f"-p MemorySwapMax={swp}",
                f"-p CPUQuota={int(cpu.split()[0]) // 1000}%", "bash", "-c"]
    return None

# in run_slice(): threads.append(threading.Thread(target=memory_watchdog, daemon=True))
# launcher spawn (bypass mode shown):
#   prefix = _cgroup_run_prefix("exec") or ["bash", "-c"]
#   proc = subprocess.Popen([*prefix, launcher], ...)
```

Classification wiring (`classify_exit`, `engine.py:492-508`): map `STOP_MEMORY` to `"mem-stall"` (a new member alongside `done|sliced|capacity|error`), and `dag.py`'s postcheck/conveyor policy: on `mem-stall`, re-dispatch with `FORGE_MEMSTALL_POLICY=fleet|degraded|stop` (default `degraded`: next attempt forces the bypass-or-fleet path and disables dynamic chunks entirely).

## Appendix D — Bypass integration points (checklist for the implementer)

| Integration point | File:line today | Change |
|---|---|---|
| Launcher construction | `engine.py:184-189` | mode-stated `_launcher()` (§3.3) |
| Env | `engine.py:68-97` | drop global GOMEMLIMIT; add `NINJA_STATUS` in bypass mode; phase matrix (Appendix B) |
| Guard | new `bypass_ready()` | G0–G3 checks (§3.3), runs after state restore in `_run_single_slice` |
| Graph bank | `graph.py:28-72` | add combined/kati/`.ninja_log`/`.ninja_deps`/fingerprint/`-t targets` list (§3.6) |
| Graph restore | `cli.py:551-557` | also on the *turbo* branch (currently main-chain cold only) |
| Turbo goal validation | `cli.py:514-533` | pre-check `args.turbo_part` against banked targets list; warn + skip dispatch on miss |
| Fusion loop | `cli.py:563-594` | warm `out/` iterations take the bypass automatically via `bypass_ready` |
| Fallback | new in `_run_single_slice` | single soong-path replay + `bypass_fallback=1` INDEX tag |
| Kill-switch | new `FORGE_NINJA_BYPASS` | workflow input passthrough (`forge.yml` env block) |
| Watchdog parity | `engine.py:205-276` | SIGINT/SIGKILL semantics unchanged — `exec` in launcher preserves pgid contract |


## Appendix E — env.py swap-stack replacement (drop-in shape)

```python
# forge_core/env.py — recalibrated swap stack (§4.3), replacing ensure_zram defaults

def ensure_zram(size_gb: int = 6, algo: str = "lz4") -> bool:
    """Tier-1 compressed RAM swap. LOUD on failure — this is load-bearing."""
    try:
        if not os.path.exists("/dev/zram0"):
            _safe_run(["sudo", "modprobe", "zram"], stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL)
        if not os.path.exists("/dev/zram0"):
            log.warn("zram UNAVAILABLE (no /dev/zram0 after modprobe) — memory "
                     "shield DEGRADED to disk-swap-only; analysis profile will "
                     "use the slow lane and the PSI watchdog owns the livelock")
            return False
        current_swaps = Path("/proc/swaps").read_text(errors="replace") \
            if os.path.exists("/proc/swaps") else ""
        if "/dev/zram0" in current_swaps:
            return True
        zramctl = shutil.which("zramctl")
        if zramctl:
            _safe_run(["sudo", "zramctl", "-s", f"{size_gb}G", "-a", algo, "/dev/zram0"])
        else:
            _safe_run(["sudo", "sh", "-c",
                       f"echo {algo} > /sys/block/zram0/comp_algorithm; "
                       f"echo {size_gb}G > /sys/block/zram0/disksize"])
        _safe_run(["sudo", "mkswap", "/dev/zram0"])
        r = _safe_run(["sudo", "swapon", "-p", "100", "/dev/zram0"],
                      capture_output=True, text=True)
        if r and r.returncode == 0:
            log.ok(f"zram tier-1 active: {size_gb} GB {algo} on /dev/zram0 (p=100)")
            return True
        log.warn(f"zram swapon FAILED ({(r.stderr or '')[:120] if r else 'n/a'}) — "
                 "shield degraded; see swap_topology in this log")
        return False
    except Exception as e:
        log.warn(f"ensure_zram raised: {e}")
        return False


def swap_topology_report() -> str:
    swaps = []
    try:
        for line in Path("/proc/swaps").read_text(errors="replace").splitlines()[1:]:
            if line.strip():
                parts = line.split()
                swaps.append(f"{parts[0]}:{parts[1]}:{parts[2]}p{parts[3] if len(parts)>3 else '?'}")
    except Exception:
        pass
    vm = []
    for k in ("swappiness", "page-cluster"):
        try:
            vm.append(f"{k}={Path(f'/proc/sys/vm/{k}').read_text().strip()}")
        except Exception:
            pass
    return f"zram={'yes' if any('zram' in s for s in swaps) else 'NO'} " \
           f"disk_swap={sum(1 for s in swaps if 'zram' not in s)} " \
           f"[{', '.join(swaps)}] {' '.join(vm)}"

# cli.py prepare, after the zram/swap calls:
#   log.out("swap_topology", fenv.swap_topology_report())
```

And the dynamic-chunk policy change in `engine.py` (one line of intent, §2.6/§4.3):

```python
# engine.py dynamic_swap_watchdog: chunks only for the exec phase, hard cap 2
if PHASE["value"] == "exec" and len(dynamic_swap_chunks) < 2: ...
```

## Appendix F — Compute-model derivations (every number, shown)

```
F1  Edge count N                 = 82,735                      (progress lines)
F2  Mid-graph rate r_mid         = 48,651−633 = 48,018 edges / 236 min = 3.39/s  (Slot 2)
                                   conservative band: 3.2–3.4/s (include 17:00–17:08 warm-up)
F3  Early-native rate r_native   ≈ 4,143 edges / (5h−0.6h) ≈ 0.25/s             (Slot 1 regime)
F4  Pure ninja time              ≈ 0.05N/r_native + 0.70N/r_mid + tail
                                   ≈ 0.05·82735/0.25 + 0.70·82735/3.39 + 0.4h
                                   ≈ 4.6h + 5.1h + 0.4h ≈ 10h upper
                                   (measured cross-check: slots 1+2 = 10.2h → 58.8%;
                                    remaining 41.2% at r_mid ≈ 2.8h → total ≈ 13h; the
                                    10–13h band brackets the 12–15h planning figure)
F5  Per-slot overhead            = 12.5(src) + 9.2(state) + 29–33(analysis) + 13.6(bank)
                                   ≈ 64–72 min (soong path); 31–38 min minus analysis(bypass)
F6  Campaign (8 slots)           = F4 + 8×F5 + dead-slot lottery
                                   soong path : 13h + 8×1.1h + E[dead]≈0.6×8×0.6h ≈ 15–18h
                                   bypass     : 13h + 8×0.6h                      ≈ 8–11h  (fleet graphmaster removes
                                                                              the remaining per-campaign
                                                                              analysis entirely)
F7  Critical path floor          ≈ 2.5–3h  (framework javac→dex→apk→img chains; observed
                                   as the slot-2 module ordering 37K→48K over 2.5h in the
                                   Java window; a hard serial lower bound for sharding)
F8  Core requirement for 2h      = (13h×4vCPU) / 2h = 26 effective vCPU, before the
                                   serial floor — which alone exceeds 2h on free tier.
                                   ⇒ sub-2h cold requires fleet cores or warm CAS (§5.5).
F9  Warm CAS                     = F4 limited to misses (15–40%) + restore 3–5 min
                                   + graph restore 2 min + packaging tail
                                   ≈ 20–50 min.
F10 Fleet hybrid                 = max(45–90min partitions ∥, 50–70min critical path)
                                   + 6–10min graphmaster + 15–25min assembler ≈ 1.5–3h.
F11 Bank bandwidth               = 17.1 GiB / 13.6 min ≈ 21 MB/s sustained upload
                                   (gh release upload stream sink); delta CAS at 3–6 GiB
                                   ≈ 4–6 min (F5 improvement) — upload-bound, unchanged
                                   protocol.
F12 Analysis memory              = 33.8 GiB peak (slot 2), still climbing at 24.0/26.1
                                   when slots 3/run58 died ⇒ live-set requirement
                                   30–34 GiB; GH capacity 26.5 GiB ⇒ structural deficit
                                   4–8 GiB even at perfect packing.
```

## Appendix G — Risk register & open questions

| # | Risk / unknown | Exposure | Disposition |
|---|---|---|---|
| R1 | Exact cause of the restored-graph dirty bit on Run #58 (T1–T3, §2.7) | Low after bypass (moot); curiosity value high | P0.3 `-d explain` probe answers with data (V6) |
| R2 | zram availability across GitHub runner pools (Azure generations differ) | Shield tier-1 variance | P0.4 `swap_topology` line + P1.2 loud failure; ladder always has disk-swap rung |
| R3 | cgroup v2 delegation on hosted runners | P1.4 | Three-rung ladder; PSI watchdog is the floor |
| R4 | Ninja graph target-set staleness under `FORGE_TARGET_OVERRIDE` | P2 | Auto-fallback + goal pre-validation |
| R5 | CAS manifest correctness (hash → wrong module reuse) | P3 | Command-hash + input-fingerprint keys; SHA256SUMS on parts; V5 acceptance gate |
| R6 | Global AMMD shipping stubbed deps (found in this analysis) | Immediate, correctness | P1.6 config change + one clean full-build validation |
| R7 | Fleet node as single point of failure for graph-master | P2.4 | GH shielded-cold fallback already exists (§4) |
| R8 | `soong.environment.used` semantics differ across Android 14/15/17 trees | P2 generality | The bypass removes the dependency on these semantics; only the soong-path fallback still consults them |

**Open questions for the core team** (answers sharpen, but do not gate, P1/P2):

1. Does `docs/DEEP_SYSTEMS_CHALLENGE_RFC.md`'s "25 minutes on `[100% 1/1]`" for Slot 3 include restore time? Raw telemetry shows 9.5 min from bootstrap start to exit 143 (21:30:35→21:40:04) — the RFC narrative likely counts from state-restore completion. This blueprint uses the raw numbers.
2. Are the Slot 4/5 logs (Run #54) retrievable for the same telemetry pass? The RFC states they "repeated the exact same failure pattern" — if their free-disk was also <12 GiB, §2.6's death-spiral claim gets two more confirmations for free.
3. Which `device/google/shiba*` patches, if any, write into the tree post-restore (`syncer.apply_patches` idempotency)? A one-line `find -newer` after patching on the next run settles T2 (§2.7).

---

*Code anchors cite `feat/romforge-v3 @ 39440d0`; log anchors cite the three job logs fetched 2026-10-09 via the provided inspection token. AOSP build-stack mechanism names (`soong_ui` stage pipeline, bootstrap ninja, `soong.environment.used`, `ninja` log formats) reference the Android 14/15 build system as shipped in LineageOS 21+ trees; where the in-tree source is the authority, P0.3's instrumentation is the designated verification step.*
