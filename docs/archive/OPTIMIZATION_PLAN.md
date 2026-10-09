# Build Speedup + Continue-Ahead Optimization Plan

> Scope: ROMForge on free-tier GitHub Actions only (₹0/$0).
> Priority: A17 hardest-first (LineageOS 23.2 / shiba + peridot, 75 GB source, 32h cold compute).
> Status: research plan — no code changed yet.

## 0. Decisions (locked)

| Decision | Choice | Rationale |
|---|---|---|
| Primary target | **A17 hardest first** | If A17 converges, A10-A16 follow for free. Optimize where pain is max (32h cold). |
| Silicon mining | **Keep strict Zen5-only** (`min-score 100`, `wait-s 240`, `FORGE_STRICT_MINING=true`) | Max per-slot speed (1.55-1.65x vs Zen3). Accept ~3% no-builder lottery; handle via fast visible retry, not slow-silicon fallback. |
| Speed vs safety | **Aggressive speed** | 14-point gate (`forge_core/gate.py`) + final `m` relink + `rsync --ignore-existing` merge remain the safety net. Turbo wider, `-j` higher, swap earlier are allowed. |
| Infra budget | **Free GHA only** | No paid runners, no external paid caches. Only `github.token` + optional `FORGE_DISPATCH_TOKEN` + free Release storage + free concurrency (20 jobs). |

## 1. Baseline (current code truth)

- A17: `expected_source_gb: 75`, `cold_hours_4vcpu: 32`, `slices_default: 8` (`configs/versions.yaml:166-184`).
- Slice: `slice_build_seconds: 16500s (275m)` in 350m job (`configs/roms/lineage-a17-*.yaml`, `forge.yml`).
- Build: hard-coded `soong_ui --make-mode -j 4` + `NINJA_ARGS=-j 4` (`forge_core/engine.py:131,134`).
- Relay tax: `restore 8-18m + bank 10-15m ≈ 20-30m / 275m ≈ 7-11%` (`TECHNICAL.md §4.2`, `forge_core/relay.py`, `forge_core/chunker.py`).
- Chunker: `PART_BYTES 1900M`, `zstd -T0 -3` always (`forge_core/chunker.py:25,38-40`).
- Chain: `slot-1..6` sequential, `slot-N if: ...slot-N-1==success` (`forge.yml:301,362,423,484,545`). One `exit 1` parks the rest. `DEFAULT_MAX_SLICES=24` (`forge_core/dag.py:27`).
- Mining: 20 candidates/slot (8 on conveyor retry), `P(>=1 Zen5 in 20) ≈ 97%` (`forge_core/mine.py:17`, `forge.yml:63,747`).
- Turbo: A17 default `[boot,vendor,product,+dtbo,system_ext,+vendor_boot]` (`forge_core/turbo.py:35-50`), but `lineage-a17-shiba.yaml:44` sets `turbo.enabled:false`.
- Cold wall today: ~12-20h with turbo, worse on shiba / strict-miss stalls.

## 2. Wins — speedup (quantified, A17)

| # | Win | Change | Saving |
|---|---|---|---|
| S1 | **Dynamic `-j` by silicon+RAM** (`-j4` -> `-j6` Zen5 / `-j8` Zen5+AVX512 when `mem>=14G+swap>=8G`, auto-throttle to `-j4` if `ram>85%`) | `engine.py:build_env/run_slice`, `mine.py:probe` | **25-35% build-phase cut: 32h compute -> 21-24h, wall 18h -> 12-14h.** Biggest single win; 4vCPU Zen5 starved at `-j4`. |
| S2 | **Turbo ON everywhere + wider set** (re-enable shiba, expand to `[boot,vendor,product,system_ext,vendor_boot,odm]` where lunch supports, `max-parallel:4->6`) | `turbo.py`, `lineage-a17-shiba.yaml:44`, `forge.yml:202` | **15-20% cold-wall cut** (offloads ~40% partition subgraphs concurrent with slot-1). |
| S3 | **Bank/restore fast path** (`zstd -3 -> -1 --long` for `out/` relay, keep `-3` for immutable `src-<mhash>`) | `chunker.py:38-47,116-163`, `relay.py:114-150` | **5-8m/slice, 40-60m over 8-slice A17.** +15-25% bytes still <2GiB/part. SHA256 per-part unchanged. |
| S4 | **Eliminate O(n^2) heartbeat** (`progress_from_log` full-file re-read every 1s -> `tail -c 2M` + `snapshot()` every 5s) | `relay.py:240-249`, `engine.py:228,270-274` | **5-10% host CPU back to ninja/javac.** |
| S5 | **Sync cold-start cut** (`repo sync -j8 -> -j16` on Zen5, keep `--optimized-fetch --prune` + strip) | `syncer.py:192-207`, `versions.yaml:sync_jobs` | **90m -> 60-70m cold sync (20-30m once per mhash).** |
| S6 | **Pre-allocate swap + keep JIT scaler** (`ensure_swap 8G` upfront + existing `+2G x6` scaler on raw backing dir, never btrfs) | `cli.py:186-208`, `env.py:257-304`, `engine.py:290-322` | **Unlocks S1/S2**; converts OOM-kill (lost slice) into 5-10% swap-thrash. |
| S7 | **More slots/run** (`slot-1..6 -> slot-1..10`, still 350m each) | `forge.yml:235-610`, `dag.py:mining_matrix` | **20-40m/campaign** (fewer queue+checkout+restore hops; 24-slice worst goes 4 runs -> 3). 35-day run ceiling allows it. |
| S8 | **Ladder actually covers `symbols/`** (add `out/.../symbols` to `LADDER_PATTERNS` + `STATE_EXCLUDES`; docs already claim 8-15GB, code omits it in `env.py:362-364`, `relay.py:31-38`) | `env.py:362`, `relay.py:31` | **Prevents link-time `capacity`** on 75G logical set; avoids 35G re-download loop. Ninja regenerates via copy-rules in minutes. |

**Combined: ~40-50% cold-wall — A17 18h -> 10-12h typical, 32h-compute worst -> 12-14h over 2 runs. Warm 2-4h -> 1.2-2.5h single slot.**

## 3. Wins — continue-ahead (break -> resume next time)

| # | Win | Change |
|---|---|---|
| R1 | **Chain never parks on transient red** (`slot-N if: always() && plan==success && (slot-N-1==success \|\| INDEX says slice)`) | `forge.yml:301,362,423,484,545` |
| R2 | **INDEX crash-proof** (versioned `INDEX.json` + `INDEX.json.bak` keep-2, checksum on save, `primary -> bak -> {}` + `::warning::` on load) | `store.py:394-413` |
| R3 | **`state-sN -> sN-1` auto-fallback** (try `t[state_tag]` -> newest `state-<key>-s*` -> `merge_turbo`; same fallback `_ensure_out` already has for verify) | `cli.py:377-386`, `relay.py:153-173` |
| R4 | **No-builder = visible fail, fast retry** (`postcheck` counts `builder` roles; zero builders -> `phase=fail(reason=no-builder)` + immediate conveyor re-dispatch with `candidates 20`) | `forge.yml:612-655,734-757`, `dag.py:31-61` |
| R5 | **Stale lock TTL + claim retry** (lock carries timestamp, ignore `>60m`, `claim` 3x/backoff like `create 4x` / `upload 3x`, age-filtered `gc_locks`) | `mine.py:101-198`, `store.py:191-216` |
| R6 | **Always upload forensics** (slots upload `/tmp/forge-slice.log` + ninja tail `if: always()`; today only `slot.yml:59-66` does on failure) | `forge.yml:235-602` |
| R7 | **Error-pattern auto-mitigate** (`ENOSPC -> capacity`, `Killed/137 -> -j4 + swap+2G next`, `missing dependency -> hint`, store `hint` in `INDEX.target`) | `engine.py:classify_exit`, `cli.py:439-460`, `dag.py` |
| R8 | **Conveyor hardening** (`conveyor if: always() && phase==slice`, plus daily cron `17 3 * * *` alongside Mon weekly; PAT stays optional boost) | `forge.yml:70,734-757` |

Current gaps closed: sequential-chain stall, silent INDEX reset to cold, single-corrupt-part cost, crashed-builder 240s block, strict-miss silent loop, lost logs on eviction, single-`gh`-flake red, 7-day cron stall without PAT.

## 4. Plan (phased)

- **P0 (1-2 days, -30% wall + unpark chain):** S1 + S6 + S8 + R1 + R6. Validate: `tests/run_tests.sh` + `harness/run_all.py` + new K/L cases (threshold order, never-delete-source, `next_action` table).
- **P1 (3-5 days, -> -45% + self-healing):** S2 + S3 + S4 + S7 + R2/R3/R4/R5/R7/R8.
- **P2 (optional, needs measurement):** parallel `gh upload -P2` in `split --filter`, `repo sync -j16` A/B on Zen5 vs Milan, btrfs `snapshot -r` mid-slice checkpoint every 60m (local only) cutting worst-case loss 275m -> 60m.

## 5. Risks (strict + aggressive combo)

- S1 `-j8` + S2 6-way turbo on 16GB can OOM before JIT scales (gated on `phys>12G` in `engine.py:290-322`). Mitigation in-plan: preswap 8G + early throttle (`ram>80% -> -j4`) + keep `LOGICAL_STOP 2.0 -> capacity` so DAG still refuses deadlock-loop.
- S3 `-1` inflates parts; worst case +1 part per 25GB `out/`. Capped by `PART_BYTES` + SHA256; cost is upload minutes, not disk.
- S7 10 slots lengthens single-run blast radius; mitigated by R2/R3/R6 (INDEX backup + fallback + logs).

## 6. Acceptance

- `bash tests/run_tests.sh` green (93 assertions) + `python3 harness/run_all.py` green (engines A-L) + `forge doctor` + `forge validate` + `forge plan --rom lineage-a17-shiba` all pass.
- A17 cold campaign resumes after killed slot without manual delete; zero-builder slot surfaces `no-builder` fail with immediate re-dispatch; no `delete-source` in volume mode at any free-space level.
