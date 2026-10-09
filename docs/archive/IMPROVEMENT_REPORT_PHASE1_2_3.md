# IMPROVEMENT_REPORT — ROMForge Pipeline Telemetry & Bottleneck Analysis

**Repository:** `zephyr4289/aosp-a10` · **Branch:** `A17-experiment` (audited at HEAD of shallow clone, 2026-10-08 tree)
**Scope:** `forge_core/cli.py`, `engine.py`, `dag.py`, `relay.py`, `storage.py`, `syncer.py`, `.github/workflows/forge.yml`, `mine.py`, `gate.py` — plus supporting modules actually on the load path: `chunker.py`, `store.py`, `turbo.py`, `env.py`, `config.py`, `configs/*`, `TECHNICAL.md`, `docs/OPTIMIZATION_PLAN.md`
**Posture:** radical rework (per maintainer decision) — recommendations assume freedom to rearchitect, including self-hosted compute and protocol-level caching, while keeping the free-tier GitHub-only path as the always-available fallback.
**Audience:** the maintainer. No hand-holding, no re-explanation of ROMForge basics; every claim is anchored to a file, a function, or a stated assumption.

---

## Contents

1. [Executive Summary & Findings Ledger](#1-executive-summary--findings-ledger)
2. [Baseline: What the Code Actually Does Today](#2-baseline-what-the-code-actually-does-today)
3. [B1 — Cross-Slot State Relay: the Full Re-Pack Tax](#3-b1--cross-slot-state-relay-the-full-re-pack-tax)
4. [B2 — Soong/Blueprint AST: Memory Spike & Per-Slot Repetition](#4-b2--soongblueprint-ast-memory-spike--per-slot-repetition)
5. [B3 — Silicon Mining Fan-Out: 200 Runner Spins per Run](#5-b3--silicon-mining-fan-out-200-runner-spins-per-run)
6. [B4 — Loopback btrfs Volume & I/O Overhead](#6-b4--loopback-btrfs-volume--io-overhead)
7. [B5 — Slot-Chain Scheduling: One Slice per Job Is the Structural Mistake](#7-b5--slot-chain-scheduling-one-slice-per-job-is-the-structural-mistake)
8. [B6 — Source Hydration, Gate I/O, Coordination & Telemetry Drift](#8-b6--source-hydration-gate-io-coordination--telemetry-drift)
9. [Target Architecture v3 — Radical Rework](#9-target-architecture-v3--radical-rework)
10. [Phased Refactoring Roadmap](#10-phased-refactoring-roadmap)
11. [Appendix A — Configuration Templates](#appendix-a--configuration-templates)
12. [Appendix B — Pseudo-Diffs for Key Changes](#appendix-b--pseudo-diffs-for-key-changes)
13. [Appendix C — Migration Runbook](#appendix-c--migration-runbook)
14. [Appendix D — Cost Model Mathematics](#appendix-d--cost-model-mathematics)
15. [Appendix E — Risk Register](#appendix-e--risk-register)
16. [Appendix F — Citation Index (function → file:lines)](#appendix-f--citation-index-function--filelines)

---

## 1. Executive Summary & Findings Ledger

ROMForge is, without exaggeration, the most competently engineered free-tier AOSP conveyor I have audited: exact-resume `out/` relay (`relay.py`), a btrfs-compressed build volume (`storage.py`), a deadlock-refusing DAG taxonomy (`dag.py`), an atomic silicon lottery (`mine.py`), and a 14-point hard anti-brick gate (`gate.py`) that actually blocks publication. The problem is no longer any single component. The problem is that the architecture is a **local optimum** on the "free GitHub-hosted runners only" constraint surface — and the next order of improvement is unreachable without moving the constraint itself. Every remaining tax decomposes into one of three root causes:

1. **Job-boundary = VM-boundary.** One slice per job (`forge.yml` slot-1…slot-10, each `cli.py cmd_slice` invoked once) forces a full state bank/restore across *every* slice even though consecutive slices share ~85–95% of `out/` byte-identically.
2. **Blob storage pretending to be a cache.** GitHub Releases stores opaque 1.9 GiB parts (`chunker.py:25`); there is no content addressing *below* the snapshot, so "unchanged intermediate targets" are re-packed, re-uploaded, re-downloaded, and re-decompressed wholesale.
3. **Heterogeneous compute bought with a lottery.** 20 candidate runners per slot (`forge.yml:63`, `dag.mining_matrix`) buy Zen 5 throughput at the price of ~190 wasted runner spins, queue latency, and a strict-mining retry spiral on the ~3% of slots where no Zen 5 lands.

The radical rework (§9) removes all three: **fusion slots** (2–3 slices per job, warm `out/` in-job), a **content-addressed delta relay** (only the frontier travels), **per-job mining**, a **banked Soong graph** keyed by `(mhash, lunch)`, and an optional **self-hosted fleet tier** that turns GitHub Actions into the orchestrator while a 16-core box does the compiling.

### 1.1 Findings ledger

Costs are for the A17/shiba cold campaign profile (`configs/versions.yaml:166-187`, `configs/roms/lineage-a17-shiba.yaml`) unless stated. "Tier" is the fix ladder: **T1** = config/one-line, **T2** = local refactor, **T3** = architectural.

| # | Finding (verified in code) | Where | Cost today | Fix tier |
|---|---|---|---|---|
| F1 | `STATE_EXCLUDES` never got the `symbols/` exclusion that `env.py` ladder and TECHNICAL.md §4.1 both assume — every state bank ships 8–15 GiB of rebuild-cheap unstripped symbols | `relay.py:31-38` vs `env.py:362-366` | +8–15 GiB per bank → +2–5 min bank *and* restore per slot, +8–15 GiB volume pressure | T1 |
| F2 | `relay.restore` deletes `out/soong/.temp`, `.minibootstrap`, `.bootstrap`, `.glob` on every slot (duplicated in `syncer.ensure_prebuilts`) → Soong re-bootstraps itself + re-globs per slot, on top of the unavoidable AST re-parse | `relay.py:174-178`, `syncer.py:367-371` | +2–6 min per slot; destroys graph-cache opportunities | T2 |
| F3 | Conveyor re-dispatch passes `candidates='8'` while initial dispatch defaults to `20`; combined with strict mining, a no-builder slot triggers `phase=slice` → re-dispatch → another lottery — a retry spiral that burns ~1–2 runner-hours per cycle with zero build progress | `forge.yml:1062` vs `forge.yml:63`; `dag.py:40-43` | ~3% of slots × 20 × 240 s scoreboard wait; each retry re-runs plan/probe/postcheck | T2 |
| F4 | `verify` and `publish` each restore the full 20–50 GiB final state on fresh runners; the gate needs only `out/target/product/<dev>/` + host tools + the zip | `forge.yml:975-1037`, `cli.py:472-510` (`_ensure_out`) | 2 × (prepare + 15–30 min restore) ≈ 40–80 min per campaign | T2 |
| F5 | One slice per job → job boundary = VM boundary → forced full bank/restore between *identical-state* consecutive slices | `forge.yml:236-917` (10 slot jobs), `cli.py:299-469` | 20–30 min × (slices − jobs) ≈ 1.5–3.5 h wall + 60–120 runner-min per campaign | T3 |
| F6 | Upload sink is serial per 1.9 GiB part (`split --filter` waits for each `gh release upload`); downloads are serial per part too — no pipelining in either direction | `chunker.py:151-159`, `chunker.py:326-351` | ~25–40% of bank/restore wall is pipeline bubble | T2 |
| F7 | `compress=zstd:1` at mount applies to *all* volume writes including `.o`/`.jar` churn (1.4–1.8× ratio, real CPU tax); no `nodatacow` for `out/`; swap chunk file shares the physical disk with `forge.img` | `storage.py:63`, `engine.py:348-380`, `env.py:287-304` | est. 5–15% compile-phase CPU; swap thrash competes with build I/O | T2 |
| F8 | `list_tags` caps at 200 releases — long campaigns (24 slices + turbo + locks + roms) can exceed it; the `cli.py` newest-state fallback scan then silently misses tags | `store.py:176-181`, `cli.py:387` | silent cold-start regression risk on old campaigns | T1 |
| F9 | `monitor.py` telemetry is stale vs the current pipeline (160-min budget constant, pre-ROMForge step names); no structured phase timing exists anywhere — only `PROGRESS:` log lines and the slice summary table | `monitor.py:17,29-39`, `engine.py:456-482` | bottleneck accounting is anecdotal; every estimate in this report had to be reconstructed from logs | T1 |
| F10 | 10 slot jobs × 20 candidates = up to 200 runner spins per run; ~190 fast-discards + per-job fixed tax (checkout, pip, `reclaim_disk` rm -rf of 35–45 GiB, volume mount) × 10 | `forge.yml:246-250`, `env.py:182-217` | ~5 h of discard runner-minutes + queue latency per run; energy waste | T3 |
| F11 | Soong analysis phase re-runs per slot: full AST parse of ~60k `Android.bp` modules, 14.2–15.1 GiB RSS on a 16 GiB runner → swap thrash; OOM kill aborts the slot (state banks, but the cycle repeats) | `engine.py:152-413` (no graph persistence), F2 | 8–18 min per slot + OOM-abort risk on every slot | T3 |
| F12 | `turbo.merge_turbo_states` unpacks each donor state to a full temp dir, then `rsync` merges — double I/O per partition; also `store.list_tags` called per donor | `turbo.py:78-104` | 10–25 min extra on cold slot-1 when turbo states exist | T2 |

### 1.2 TL;DR impact projection

Assumptions and derivations in Appendix D. "GH-only" = stays on free public-repo runners; "Fleet" = Phase 3 self-hosted tier for compile jobs.

| Metric (A17/shiba cold campaign) | Today (A17-experiment) | After Phase 1–2 (GH-only) | After Phase 3 (Fleet) |
|---|---|---|---|
| Wall-clock, cold | 12–18 h typical (20–40 h pessimistic, turbo-miss) | **6–10 h** | **4–6 h** |
| Wall-clock, warm (one source change) | 1.5–3 h single slot | 50–90 min | 25–45 min |
| Runner-minutes per campaign | 65–75 h (incl. ~5 h discards, ~20 h turbo) | **25–35 h** | **3–5 h** (GitHub: plan/postcheck/gate only) |
| Relay events per 8-slice campaign | 8 banks + 8 restores + 2 verify/publish restores | 3 banks + 3 restores + 1 product-state restore | same, but LAN-speed on fleet |
| Bytes shipped via Releases per campaign | ~150–350 GiB (8 × full out/ snapshots) | ~40–90 GiB (frontier deltas) | ~40–90 GiB (unchanged; storage is not the fleet win) |
| OOM abort probability per slot | material (Soong peak 14.2–15.1 GiB vs 16 GiB RAM) | **−50 to −70%** (GOMEMLIMIT + zram + graph banking) | near zero (fleet RAM) |
| Mining runner spins per run | up to 200 | 16–24 | 0 (labeled runners) |

The single highest-leverage change is **B5/F5 (fusion slots)** — it is also the one that most people get wrong by trying to optimize compression first. Compression tuning (already largely landed as OPTIMIZATION_PLAN S3: `zstd -1 --long`, `relay.py:139`) was worth ~5–8 min per slice; eliminating the *need* to compress at all between slices is worth the entire 20–30 min.

---

## 2. Baseline: What the Code Actually Does Today

This section fixes the factual baseline so later sections can argue deltas. Everything here is verified against the audited tree, not against documentation claims (several of which have drifted — see F1/F9).

### 2.1 Pipeline topology (forge.yml, 1,074 lines)

```
plan (15m)  ──►  sync (only if src-<mhash> missing, 350m cap)
                    ├──► turbo:<partition> ×4–6 (max-parallel 6, each own runner,
                    │        ALLOW_MISSING_DEPENDENCIES, banks state-<key>-turbo-<part>)
                    └──► slot-1 ──► slot-2 ──► … ──► slot-10   (sequential, needs-chained;
                             each = 20-candidate matrix, 350m cap, one 275m build budget)
                                   └──► postcheck (INDEX → phase)  ──► verify (120m) ──► publish (60m)
                                          └── phase=slice ──► conveyor (re-dispatch forge.yml;
                                                              PAT optional, else daily/weekly cron resumes)
```

- Triggers: `workflow_dispatch` (rom, target, force_sync, skip_turbo, candidates default **20**, strict_mining default **true**) + daily `17 3 * * *` and weekly `17 3 * * 1` crons (`forge.yml:39-71`). Concurrency group per ROM, `cancel-in-progress: false` (`forge.yml:73-75`).
- Each slot job: checkout → `mine.gate` (probe `/proc/cpuinfo`, atomic claim via `gh release create`, strict → non-target candidates discard after a 240 s scoreboard) → only the builder role proceeds to prepare/`slice` (`forge.yml:258-270`).
- `postcheck` is the single DAG authority: reads INDEX via `forge probe`, emits `phase` (verify | slice | fail), GCs silicon locks, goes red on fail (`forge.yml:927-970`).
- The conveyor re-dispatches with `candidates || '8'` — note the mismatch with the initial 20 (F3).

### 2.2 State plane

- **Source:** `syncer.sync_tree` shallow-syncs (`repo init --depth=1`, `sync -c -j<sync_jobs=16>` with 3 escalating retries), strips `.git`/`.repo`/non-Linux GCC prebuilts (`syncer.py:223-229`), fingerprints the resolved manifest into `mhash` (16 hex, `syncer.py:79-91`), and banks `src-<mhash>` as ~17×1.9 GiB zstd-3 parts (`syncer.py:94-113`). Every slot restores it in 6–9 min unless `.source_ready` exists (`cli.py:342-348`).
- **Build state:** after every slice `relay.bank` packs the *entire* `out/` (minus `STATE_EXCLUDES` — which, per F1, wrongly *includes* `symbols/`) through `tar | zstd -T0 -1 --long | split -b 1900M --filter 'gh release upload'` (`relay.py:114-150`, `chunker.py:123-172`). Restore is `wipe out/ → download all parts serially → sha256 each → zstd -d → tar -x` (`relay.py:153-190`, `chunker.py:284-371`).
- **Coordination:** `forge-index` release holds `INDEX.json` (+`.bak`): per-target `mhash/slice/state_tag/done/rom_zip/last_classification` (`store.py:398-457`). Written only by the strictly-sequential slot chain (race-free by construction, per `store.py:16-17` docstring).
- **GC:** `gc_state` keeps newest 2 slice states, drops turbo states unconditionally (`store.py:460-482`); publish calls `gc_state(keep=1)` (`cli.py:565`).

### 2.3 Storage & memory plane

- Storage v2: `<best-mount>/romforge/forge.img` sparse btrfs loop file, hard-capped at `free − 10 GiB reserve`, mounted `compress=zstd:1,noatime` (`storage.py:56-63,212-285`). Source compresses ~2.2–3.0×, objects ~1.4–1.8× → the 75 GiB logical set fits in ~35–45 GiB physical. `fstrim` returns deleted bytes to the backing mount (`storage.py:376-389`). Any provisioning failure degrades to plain dirs honestly (`_plain_state`).
- Swap: static 8 GiB via `ensure_swap` (on the raw backing mount, *not* inside btrfs — correct, COW+swap is a deadlock) plus a dynamic watchdog adding 2 GiB chunks ×6 when swap >70% used or RAM >80% & swap >40% (`engine.py:348-380`). `protect_runner_processes` sets `oom_score_adj=-1000` for the runner agent and `forge_core` — deliberately leaving Soong as the OOM victim (`env.py:220-234`).
- Parallelism: `optimal_jobs` returns -j8 on AVX-512 silicon with ≥22 GiB RAM+swap, -j6 at ≥20, else 4 (`engine.py:101-149`) — OPTIMIZATION_PLAN S1, landed.

### 2.4 What already landed from OPTIMIZATION_PLAN.md (don't re-do these)

The A17-experiment branch already contains: **S1** dynamic `-j` (`engine.py:177`), **S3** state-relay `zstd -1 --long` (`relay.py:139`), **S4** 2 MiB-tail log parsing (`relay.py:246-249`), **S7** slots 1–10 (`forge.yml:236-917`), **S8-partial** `symbols*` in the *ladder* (`env.py:362-366`) but **not** in `STATE_EXCLUDES` (F1), **R2** `INDEX.json.bak` (`store.py:416-441`), **R3** newest-state fallback restore (`cli.py:386-392`). Shiba turbo is enabled with 4 partition targets (`lineage-a17-shiba.yaml:46-49`). What has *not* landed is everything structural: S2-fully (wider turbo + `max-parallel: 6` is there, but merge I/O is still double), and the entire P2 measurement tier. This report therefore concentrates on the T2/T3 strata.

### 2.5 Current campaign cost model (A17/shiba cold)

Derived in Appendix D from the above; headline: **8 build slots × (25–45 min fixed overhead + 275 min budget) + 4 turbo jobs × ~300 min + sync ~90 min + verify/publish 40–80 min ≈ 65–75 runner-hours, 12–18 h wall typical**, with ~5 h of that being candidate discards and 1.5–3.5 h being relay tax that fusion slots eliminate outright.

---
## 3. B1 — Cross-Slot State Relay: the Full Re-Pack Tax

### 3.1 Mechanism, precisely

The relay is the load-bearing wall of ROMForge — the fix for the upstream "redo grows linearly with slice number" pathology (`relay.py:1-16`, `TECHNICAL.md §2.1`). It works: ninja's resume contract (mtimes + `.ninja_log`) is honored end-to-end, and the exclusions are (mostly) rebuild-cheap fat. The tax is in *how* the state travels:

**Bank path** (`relay.bank` → `chunker.stream_pack`):
```
tar -h -C $BUILD_ROOT -cf - --exclude … out      # full walk of 20–50 GiB, ~1.0–1.5 M files
  | zstd -T0 -1 --long -c                          # ~300–500 MB/s per thread on Zen 5
  | split -b 1900M --filter '… gh release upload …'# SERIAL: tar+zstd stall while each part uploads
```
Each finished 1.9 GiB part is uploaded by a *fresh* `gh` process inside the filter (`chunker.py:151-159`), with 3 retries × 20 s backoff baked into the shell snippet. Because `split`'s filter is synchronous, the tar walk and zstd pipeline **pause for the entire HTTP upload duration of every part** — at 50–80 MB/s effective, that is 25–40 s of stall per ~1.9 GiB part, repeated ~11–26 times per bank. The pipeline is compute-bound exactly when it should be network-bound and vice versa.

**Restore path** (`relay.restore` → `chunker.unpack_from_store`): wipes `out/` (`relay.py:159-167`), then the feeder thread downloads parts **serially** — one `gh release download` process per part, then sha256 (≈4 s/GiB single-thread), then feeds `zstd -d` → `tar -x` (`chunker.py:326-351`). Download, hash, and decompress never overlap across parts. 20–50 GiB at effective 60–100 MB/s end-to-end = 8–15 min of pure I/O before the first compiler action — matching the brief's 8–15 min figure.

**And then it happens again**: per-slot × 8 slices, plus once more in `verify` and once more in `publish` (`cli._ensure_out`, `forge.yml:975-1037`) — the same 20–50 GiB is downloaded and decompressed **ten times per campaign** for a `out/` that only ever grows monotonically at the frontier (ninja almost never rewrites already-complete outputs mid-campaign).

### 3.2 Cost model

Per-slice relay cost today (A17, 30–45 GiB `out/` after the `oat_x86`/`.img.new`/temp excludes, symbols **still included** per F1):

| Stage | Range | Notes |
|---|---|---|
| pre-bank cleanup + fstrim | 1–3 min | `relay.pre_bank_cleanup` (`relay.py:71-111`), volume mode always trims |
| tar walk + zstd -1 | 3–6 min | ~5,000–8,000 files/s tar, ~1.0–1.5 GiB/min zstd on 4 vCPU |
| serial part upload | 4–10 min | 16–26 parts × (upload 25–40 s + gh process churn 2–5 s), *serialized with* compression |
| restore download (serial) | 5–10 min | parts one-by-one, no prefetch |
| sha256 + zstd -d + tar -x | 4–8 min | decompression ~1.5–2× faster than compression, but single zstd thread on -d pipeline here |
| **Total per slot** | **17–37 min** | TECHNICAL.md §4.2 says 20–30 min; brief says 14–27; same order |

Across an 8-slice campaign + verify + publish: **2.5–5 h of campaign wall and 5–7 h of runner-minutes** spent moving bytes that are 85–95% identical to bytes moved one slot earlier.

### 3.3 Root cause

GitHub Releases is an **append-only blob store** with a 2 GiB/asset cap — not a content-addressed store, not a differential store, and (for `gh` CLI) not a concurrent-put/get store. The relay treats "state" as a single opaque tarball, so the unit of deduplication is the *whole snapshot*. Additionally, the one-slice-per-job topology (B5) makes the relay run at all between slices that could have shared a live filesystem.

### 3.4 Fix ladder

**T1 — same shift, one line (ship this week):**
1. Add `"out/target/product/*/symbols*"`, `"out/target/product/*/*/symbols*"` to `STATE_EXCLUDES` (`relay.py:31-38`) — closes F1. Ninja re-materializes symbols via copy rules from `obj/` in minutes (`env.py:362-366` already trusts this). Effect: state shrinks 8–15 GiB; bank −2–5 min, restore −2–4 min, volume headroom +8–15 GiB (also directly reduces `capacity` classification risk at link time).
2. Raise `gh release list --limit 200` → paginate in `list_tags` (`store.py:176-181`) — closes F8 before a 24-slice campaign trips it.

**T2 — pipeline the I/O (days):**
3. **Parallel upload pool:** replace the synchronous filter with a 2–3 slot bounded queue — the filter writes the part to `$FILE` and enqueues; worker processes run `gh release upload` concurrently; tar+zstd never stall. Bank wall −25–40%.
4. **Prefetch downloads:** in `unpack_from_store`, download part *k+1..k+2* while feeding part *k* to zstd (a 2–3 thread downloader + the existing feeder). Restore wall −30–50%. Combined with (3), relay events drop to ~10–20 min each.
5. **Split zstd decompression from tar:** restore currently runs one `zstd -d` thread (zstd decompress is single-threaded per stream, but you can shard the stream at part boundaries and run N decoders into a re-assembled tar stream — or simply pre-decompress parts in parallel to a staging area on the *other* mount). −2–4 min per restore at the cost of 2 GiB transient disk.

**T3 — change what travels (the radical fix): content-addressed delta relay**

Stop shipping snapshots; ship a **manifest + frontier deltas**. Design constraints that make this workable:

- ninja's resume contract needs `(path, size, mtime, mode)` — **content hashes are not needed for correctness** (mtime is the oracle), but they are needed for *deduplication across runners*. A `sha256` of every file in a 1.5 M-file tree costs 2–4 min single-threaded (§Appendix D); acceptable only if computed incrementally (mtime+size as change detector, hash only on first sight — exactly ccache's trick).
- Between consecutive slices, only three deltas exist: files **added** at the frontier (the majority — ninja only appends new outputs), files **touched in-flight** when a slice stops mid-rule (a handful), and files **regenerated** by cheap copy rules (symbols — now excluded). Deleted files are rare and tracked by manifest diff.
- GitHub Releases asset count is not a practical CAS (per-asset `gh` round trip ≈ 2–5 s; a per-file store of 10⁵–10⁶ objects is dead on arrival). Therefore the delta unit must stay **coarse**: directory-bucketed archives (e.g., `out/soong`, `out/host`, `out/target/product/<dev>/obj/<top-dir>` × ~20–40 buckets), each content-addressed by the bucket's manifest hash.

Concretely — **CAS-Relay v3**:

```
state-<key>-s<N> release assets:
  manifest.json          # [{path, size, mtime_ns, mode, sha256?, bucket}] + base = s<N-1> tag
  b.<bhash>.part.aa…     # bucket archives, content-addressed, SHARED across slices
  INDEX delta            # which buckets changed vs base
```

Bank: walk `out/` (cheap stat-only pass), diff manifest vs s<N−1>, pack only buckets whose file set changed (tar+zstd per bucket, 2 parallel workers), upload new buckets + manifest. Restore: fetch base manifest (from `state_tag` chain) + deltas, download only buckets listed, unpack. Unchanged buckets (typically `out/soong` after slice 2, most of `obj/` once built) never travel again.

Expected effect (assumes frontier adds 3–6 GiB/slot, buckets 85–95% stable after slot 2): **bank 2–4 min, restore 3–6 min, per-campaign Release traffic 150–350 GiB → 40–90 GiB.** Bonus: `verify`/`publish` restore only the product bucket + host-tools bucket (F4 collapses too).

Alternative T3 variants worth knowing (and why I rank them below bucket-CAS):
- **zstd `--patch-from`:** compress the new tar against the previous tar as dictionary — excellent ratio, but requires the *previous full tar* present at bank time (runner doesn't have it: it has the *unpacked* tree) and at restore time; and zstd's window (`--long=31`, needs ~2 GiB RAM per stream) is strained by 40 GiB streams. Use it *inside* buckets if you want byte-level dedup for large in-place-changed files (rare in ninja output).
- **casync / OSTree-style chunking:** proper content-defined chunking with a chunk store. The right tool if the backend were a real CAS. On GitHub Releases, per-chunk round trips kill it; you would end up re-batching chunks into buckets — i.e., reinventing the design above with extra steps.
- **git-as-CAS (`git bundle` / bare repo on Releases):** object-level dedup and delta compression for free, but ninja mtimes are not representable in git (no per-file mtime tracking) — you'd need a sidecar mtime manifest and `git checkout` + `touch` pass, which is exactly the manifest machinery of CAS-Relay plus an unhelpful VCS. Rejected.
- **Bazel Remote Execution API (REAPI) cache:** the *protocol* is right (CAS + action cache), the *backends* are wrong for $0 (ENGFlow/BuildBarn need hosting; GCS bucket needs billing). Keep as the seam: see §9.5 — the CAS-Relay manifest/bucket layout deliberately mirrors REAPI's `Digest` shape so a real CAS can replace the Releases backend later with no orchestration change.

**T3 — orthogonal:** collapse relay *events* via fusion slots (B5/§7). If 2–3 slices run in one job with warm `out/`, the relay happens at job boundaries only: 8 events → 3. Combined with delta relay, the total campaign tax lands at **~15–35 min** (from 2.5–5 h).

---
## 4. B2 — Soong/Blueprint AST: Memory Spike & Per-Slot Repetition

### 4.1 The two separate problems

The brief lumps "Soong memory" into one bottleneck; the code shows two distinct costs that need different fixes:

**(a) Per-slot repetition.** `soong_ui` re-runs the *entire* Blueprint pipeline on every slot: glob → parse ~60k `Android.bp` → resolve ~60k modules → generate `out/soong/build.ninja`. This is O(tree), not O(delta): Soong has no incremental AST mode. On top of that, ROMForge *actively destroys* its own bootstrap caches every restore: `relay.restore` deletes `out/soong/.temp`, `.minibootstrap`, `.bootstrap`, `.glob` (`relay.py:174-178`), and `syncer.ensure_prebuilts` repeats the same purge (`syncer.py:367-371`). `.bootstrap` holds the *compiled `soong_build` binary itself* — deleting it means minibootstrap re-compiles Soong from source (~1–3 min) before the analysis phase even starts, and `.glob` deletion forces a full glob cache rebuild. The purge was a defensible anti-staleness hack for a world where states were opaque blobs of unknown provenance; under content addressing (`mhash`) it is pure loss. Cost: **2–6 min of forced re-bootstrap + glob, then 8–18 min of AST analysis, on every slot and every turbo job** (turbo jobs pay it too — `cli.py:357-366` runs the same restore path).

**(b) The memory spike.** During analysis, the Go heap holds the full module graph: **14.2–15.1 GiB RSS** (brief; consistent with ~60k modules and Go's 2× heap-overhead habits) on a 16 GiB runner. The existing defenses are good but reactive: 8 GiB static swap (`env.ensure_swap`), a dynamic +2 GiB×6 swap scaler (`engine.py:348-380`), `oom_score_adj=-1000` for the runner agent and `forge_core` so the OOM killer *correctly* selects Soong as the victim (`env.py:220-234`), and swap-on-raw-backing-dir (never inside btrfs — COW+swap deadlock, correctly documented in `cli.py:186-189`). The failure mode is not the OOM kill itself (state banks on the error path, `cli.py:458-468`) — it is the **OOM-retry cycle**: the next slot restores, re-runs the identical analysis, and re-enters the same spike with fresh swap while the previous slot's progress is preserved but nothing else is learned. Each cycle costs 15–30 min.

### 4.2 Why the spike is survivable — and how to make it boring

Three independent levers, in order of bang-for-effort:

1. **`GOMEMLIMIT` soft cap (T1, one env var).** Soong is a Go program; Go ≥1.19 honors `GOMEMLIMIT` by running GC harder as the heap approaches the limit. Setting `GOMEMLIMIT=11–12 GiB` (leave ~4 GiB for the runner, ninja's own RSS, and page cache) converts the hard 15 GiB spike into a longer, GC-churned analysis phase that **does not enter swap death-spiral territory**. Expected: analysis +10–20% wall, swap activity −50–80%, OOM-abort probability per slot drops from "material" to "rare" (bounds: Soong's live heap genuinely needs what it needs; if live-set > limit, GC thrashes — detect via `GODEBUG=gctrace=1` and fall back to raising swap). Wire it in `engine.build_env` (`engine.py:67-94`) alongside `LC_ALL`/`OUT_DIR`.
2. **zram first-tier swap (T1).** The runner's NVMe is fast, but swap-in on the *same physical disk* as `forge.img` + the part streams competes with build I/O (F7). A zram device (e.g., 8 GiB, `zstd` compressor, `swapon -p 100`) gives the spike a RAM-speed spill tier for compressible Go heaps (Go heaps compress well, ~2.5–3.5×). The dynamic scaler stays as tier-2 on NVMe. `prepare` already has sudo; adding `modprobe zram` + `zramctl` is 6 lines (`fenv.ensure_zram()`), with graceful degradation like everything else in `env.py`.
3. **Don't re-run analysis at all when nothing changed (T3 — graph banking).** The campaign's `mhash` pins the source tree; `lunch` pins the product config; the toolchain pins are in-tree. Therefore the *generated graph* is a pure function of `(mhash, lunch, soong_version)`. Bank `out/soong/build.ninja` + `.bootstrap/` + `.glob/` + `out/soong/module-dependencies.json` (A15+ trees) as `graph-<mhash>-<lunch>` — a *tiny* state (50–300 MiB compressed). Slots restore the graph and **skip soong_ui's analysis phase entirely**, invoking ninja directly:

```
# bypass (simplified):
ninja -C out -f out/combined-<product>.ninja <target>
```

soong_ui's own orchestration (envsetup validation, kati stubs, combined ninja file assembly) must be replicated — this is the one genuinely delicate part, and why it is T3: the bypass must *prove* its precondition (stamp files: `mhash`, lunch, tree mtimes of `Android.bp` files newer than graph stamp → fall back to full soong_ui). Effect on a matched slot: **−8–18 min and the entire memory spike disappears from every slot after the first.** OOM risk concentrates into the single graph-generating slot, where lever 1+2 protect it. This also composes with turbo: turbo jobs restore the same graph bank and run only their partition subgraph.

Guardrails for the bypass: (i) stamp = sha of `.forge-manifest.xml` + lunch + `out/soong/soong_build` binary hash; (ii) `ninja -d explain` spot-audit in CI comparing bypass vs full path outputs on a small tree (`tests/` already has the offline pattern for this — `tests/test_all.py` runs the DAG table offline); (iii) env flag `FORGE_SOONG_BYPASS=0` to disable per campaign; (iv) the hard gate already catches any correctness escape at the image level — the same safety argument turbo uses (`TECHNICAL.md §6.3`).

### 4.3 What *not* to do

- **Don't reach for larger GitHub runners.** `ubuntu-latest-large` (8 vCPU/32 GiB) is paid; the project's premise is $0 on public repos, and the fleet tier (§9.4) solves memory *and* compute for less operational risk than a metered dependency.
- **Don't try to shard Soong's analysis** (partitioned Blueprint parsing, `bp2build`-style per-module graphs). It is a research project, not an engineering change; AOSP itself hasn't shipped it. The graph bank gets you the same wall-clock win for the 95% case (unchanged tree mid-campaign).
- **Don't over-tune `GOGC`.** `GOMEMLIMIT` is the correct lever; `GOGC` lower than default trades throughput for peak-RSS in a way that mostly just stretches the analysis phase.

### 4.4 Expected effect (combined)

| Scenario | Analysis cost today | After GOMEMLIMIT+zram | After graph banking |
|---|---|---|---|
| Slot 1 (cold, graph miss) | 10–20 min, spike 14–15 GiB | 12–24 min, spike capped, swap-light | 10–20 min (generates + banks graph) |
| Slots 2–N (warm) | 10–20 min, spike each time | 12–24 min each | **0–2 min stamp check, no spike** |
| Turbo jobs (×4–6) | each pays full analysis | each pays capped analysis | each restores graph, 0–2 min |
| 8-slice campaign total | 80–200 min of analysis | 100–220 min (safer) | **~15–25 min** |

---
## 5. B3 — Silicon Mining Fan-Out: 200 Runner Spins per Run

### 5.1 What the lottery actually buys — and what it costs

The mechanism is sound and well-documented (`mine.py:1-27`, `TECHNICAL.md §6.4`): 20 identical candidates self-select; target silicon (score ≥100, Zen 5 Turin / EPYC 9V4x) claims instantly via the atomic `gh release create` lock; non-targets poll the lock every 10 s during a 240 s scoreboard window, then either fallback-claim (non-strict) or discard (strict). Census math: P(≥1 Zen 5 in 20) ≈ 97%; Zen 5 runs soong/javac/ninja ~1.55–1.65× faster than Zen 3 — effectively converting six 275-min slots into roughly four.

The costs, itemized from code:

| Cost | Where | Magnitude |
|---|---|---|
| **Per-slot fan-out** — the lottery re-runs for every slot job | `forge.yml:246-250` (matrix per slot) | 10 slots × 20 candidates = **200 runner spins/run**; ~190 discards |
| **Discard floor** — a discard is *not* 40 s | checkout (~20–40 s) + `mine.gate` probe/claim (~5–15 s) + job teardown + **queue wait** | 60–120 s runner time each, plus queue latency ahead of real jobs |
| **Scoreboard wait on misses** — with strict mining and no Zen 5 present, all 20 candidates burn the full 240 s before discarding | `mine.py:167-186` (`wait_s=240`, strict reject) | 20 × 4 min = **80 candidate-minutes per no-builder slot**, zero build |
| **Strict-miss spiral (F3)** — no-builder → `classification='no-builder'` → `phase=slice` → conveyor re-dispatch (with `candidates='8'`!) → new lottery | `dag.py:40-43`, `forge.yml:1062` | each retry = plan + probe + postcheck + 8–20 candidates × up to 240 s ≈ **1–2 runner-hours, zero progress**, repeated |
| **Energy/infra waste** — 190 VM boots for nothing | — | est. 5 h runner-min/run + queue pollution for other campaigns (concurrency group is per-ROM, but the shared pool feels it) |
| **Queue latency ahead of the winner** — 20 matrix entries of the same job start together; GitHub queues them as a burst | `strategy.max-parallel: 20` (`forge.yml:248`) | winner start delayed by scheduling jitter; on free pools, minutes |

The design insight the current topology misses: **the silicon lottery is per-VM, but the slot chain is per-job — and those are coupled unnecessarily.** Each slot is a separate job *solely* to get a fresh lottery draw per slice; with fusion slots (§7) one job holds 2–3 slices and needs **one** draw per job.

### 5.2 Fix ladder

**T1 — tune the lottery tonight:**
1. **Candidates 20 → 8** for slots 2+ (slot-1 keeps 12–20 as the "cold path pacer"): P(≥1 Zen 5 in 8) ≈ 82–90% per `mine.py:17` census; the *fallback* path (non-strict) or the fusion-slot redraw at the next job boundary covers the remainder. Saves ~120 spins/run.
2. **Fix the conveyor candidate mismatch (F3):** pass the original `candidates` through the conveyor re-dispatch (`forge.yml:1062`) instead of hardcoded `'8'`.
3. **Scoreboard wait 240 → 90–120 s** for slots ≥2: the winner-claims-instantly dynamic means non-targets exit on the first lock poll; the long window only ever fully elapses on a no-builder slot, where it is pure waste. (Keep 240 s for slot-1 if strict cold pacing matters.)

**T2 — make retries cheap:**
4. **No-builder → immediate re-dispatch, no scoreboard burn:** when postcheck counts zero `builder` roles, the next run should start *now* with `candidates=20` — the current spiral burns the full scoreboard before retrying. Implementation: mine.gate exit code / role counting surfaced in `postcheck`, `dag.next_action` already returns `phase=slice` for `no-builder` (`dag.py:40-43`) — add `phase_reason=no-builder` routing so the conveyor dispatches without waiting for the next cron pulse.
5. **Stagger candidate starts** (`strategy` doesn't support it directly; emulate with a 0–90 s random `sleep` in the gate step before probing) to smooth the queue burst and let the fastest-probing candidates claim earlier.

**T3 — eliminate the lottery (fleet tier):**
6. A single self-hosted runner with a `romforge` label makes `runs-on: [self-hosted, romforge]` deterministic — no probe, no claim, no discard, no 240 s window. GitHub-hosted runners remain the fallback pool. See §9.4.

### 5.3 Honest accounting of what mining is worth

If the fleet tier lands (Phase 3), delete mining for fleet-dispatched jobs entirely and keep it only for the GitHub fallback pool. If the fleet never lands, the T1/T2 tuning alone cuts fan-out runner-minutes from ~5 h/run to ~1.5–2 h/run while keeping ≥90% Zen 5 hit rate on slot-1 and covering misses at job boundaries. The lottery is a clever instrument for a constraint you no longer need to accept.

---

## 6. B4 — Loopback btrfs Volume & I/O Overhead

### 6.1 Mechanism and real costs

Storage v2 (`storage.py`) is the reason the campaign fits at all: the 75 GiB logical working set becomes ~35–45 GiB physical inside a hard-capped sparse image, and `fstrim` returns deleted extents to the backing mount. But the implementation pays three taxes that a *split-policy* volume would not:

**(a) Write-side compression CPU on compile output.** `mount -o loop,compress=zstd:1,noatime` (`storage.py:63`, `_mount_vol:180`) compresses *every* extent written — including the 1.4–1.8×-ratio `.o`/`.a`/`.jar` churn that dominates ninja's write volume. zstd:1 costs ~1–3 cores-worth of work per GiB/s; on a 4-vCPU runner with -j6..8 that is a measurable steal from compilers (est. 5–15% compile-phase throughput; A/B measurable with `FORGE_NO_VOLUME=1` on a small tree vs zstd:1 — add to harness as engine M). Text-heavy source *reads* decompress cheaply, so the read side is fine.

**(b) COW write amplification on hot files.** btrfs copies-on-write every overwrite; ninja's in-place updates to `.ninja_log`, `.ninja_deps`, and mid-rule partial outputs rewrite the same extents repeatedly (each write → new extent + metadata churn → fragmentation of the hottest files in the volume). There is no `chattr +C` (nodatacow) anywhere in the tree.

**(c) Inode/fragmentation pressure.** Tens of thousands of small headers + intermediate files during multi-threaded ninja runs churn btrfs metadata (b-trees per-subvol); combined with (b), late-campaign files scatter. The harness already exercises inode pressure (`harness/test_inode_pressure.py`) — the mitigation today is "watchdogs + ladder," which is reactive.

**(d) Swap co-location (F7).** The dynamic swap chunks live on the raw backing dir (`engine.py:373` — `storage.backing_dir() / .forge-swap.chunk.N`) — same physical disk as `forge.img`. Under Soong's memory spike (B2), swap-in traffic contends with build I/O on a single NVMe queue. zram (§4.2) fixes the tier-1 case; nothing fixes the physics of one disk, which is why the fleet tier wins on memory *and* I/O simultaneously.

### 6.2 Fix ladder

**T2 — split the volume by access pattern (the big one):**

```
/mnt/romforge/
  src.img     btrfs compress-force=zstd:3,noatime   # source: read-mostly, 2.2–3× ratio, write-once
  out.img     btrfs nodatacow,noatime               # out/: write-hot, compress-off, autodefrag off
  tmp/        raw                                    # TMPDIR (already raw — engine.build_env:70-76, correct)
```

- Source volume: `compress-force` (compress at write regardless of heuristics — text always wins) at zstd:3 for better ratio; read-side cost is identical.
- `out` volume: **nodatacow by mount** (no per-file chattr needed) — kills write amplification on `.ninja_log`/`.ninja_deps`/partial outputs; compression off because `.o` churn costs more CPU than it saves in disk (and with CAS-Relay, the *state* is compressed at bank time anyway — belt-and-suspenders double compression is exactly what we remove).
- Sizing: source image ~25–30 GiB (75 GiB logical ÷ ~2.6×), out image sized to `free − src − reserve`. This preserves the capacity-deadlock guarantee (`compute_cap_gb`, `MIN_VIABLE_FREE_GB`) with the same degrade-to-plain honesty. `ensure_volume` grows from ~70 lines to ~110 — mechanical, testable with the existing `selftest` + `harness` K engine.

Expected: compile-phase I/O CPU −5–15%, late-campaign fragmentation risk down, watchdog ladder trips rarer. Reliability note: two loop devices instead of one doubles the (already handled) mount-failure surface — both degrade independently to plain dirs.

**T2 — snapshot-based local checkpointing (reliability + fusion-slot enabler):**

The volume is btrfs precisely so you can have this for free:

```
btrfs subvolume snapshot -r vol/aosp/out  vol/snapshots/s<N>-ckpt
```

A read-only snapshot is O(metadata) — **sub-second**, vs 6–12 min for a full relay bank. Use cases:
1. **Mid-slice checkpoint:** snapshot every 30–60 min during a long slice; if the job dies (OOM, eviction, network loss), the *next* slot can mount the image? No — the image is per-VM and ephemeral. Correction: snapshots help *within* a job (fork/rollback), and become powerful when combined with CAS-Relay: snapshot at slice boundary → diff snapshot vs previous snapshot (both local) → pack only changed buckets → upload. The local snapshot makes the *manifest diff* O(changed files) instead of O(tree) — this is the mechanism that makes in-job fusion banking cheap.
2. **Turbo merge without double I/O (F12):** donors merge via `btrfs send/receive` or snapshot diffs instead of full unpack + rsync.

**T1 — zram as swap tier-1** (already argued in §4.2): `zramctl` in `prepare`, priority above the NVMe chunks.

**T3 — GHCR as a CAS tier (optional, radical):** GitHub Container Registry is free for public repos, stores OCI blobs (content-addressed by digest, unlimited total, per-blob limits generous), and `gh` already ships authenticated. CAS-Relay buckets map 1:1 to OCI blobs (`oras` CLI or a 100-line HTTP layer). This gives the delta relay a *real* CAS with dedup across campaigns/ROMs sharing buckets (`out/soong`, host tools, common `obj/` trees across devices on the same Android version). Risk: GHCR blob limits & ToS on non-container usage are greyer than Releases; treat as Phase 3+ experiment, keep Releases as default backend. See §9.5.

---
## 7. B5 — Slot-Chain Scheduling: One Slice per Job Is the Structural Mistake

### 7.1 The coupling that creates the relay tax

Follow one slice through `forge.yml`:

```
slot-N job (350m cap) on a fresh VM:
  checkout → pip → reclaim_disk (rm -rf 35–45 GiB, 2–4 min) → ensure_volume (mount btrfs)
  → mining gate (20 candidates, one wins) → prepare → source hydrate (6–9 min)
  → state restore (8–15 min) → soong bootstrap+analysis (8–18 min) → build (275m budget)
  → bank (6–12 min) → job ends → VM wiped
slot-N+1 job on ANOTHER fresh VM: the same 25–45 min of prelude, to resume a tree
  that is 85–95% byte-identical to the one just wiped.
```

The slot chain exists to beat the 6-hour *job* wall by chaining inside a 35-day *workflow run* (`TECHNICAL.md §6.5`) — correct. But the chain granularity was set to **one slice per job**, which conflates two boundaries that have no business being equal: the *resume unit* (a slice, 275 min budget — sized for safe banking) and the *VM unit* (a job, 350 min cap). A 350-minute job can hold **two to three** 95–120-minute slices, or one 275-minute slice plus a 60–90-minute follow-on, with the `out/` tree **staying live on disk between them** — no bank, no restore, no re-analysis, no re-mining.

Why wasn't it built that way? Three defensible reasons, each now solvable:

| Concern | Why it forced one-slice-per-job | Now solvable by |
|---|---|---|
| Wall safety: job dies at 350m → lose the whole in-progress slice | bank after *every* slice was the only checkpoint granularity | **Mid-slice checkpoint banks**: snapshot + delta-bank every 30–60 min (§6.2); worst-case loss 30–60 min, *better* than today's 275-min exposure |
| Silicon: fresh lottery per slice | each job = new VM = new draw | **Per-job mining** — one draw covers 2–3 slices; misses redraw at the next job boundary; strict spiral fixed (§5.2) |
| DAG semantics: `needs:` chain gives natural sequencing + `INDEX` race-freedom | slots were the serialization mechanism | The in-job loop *is* the same strict sequence — INDEX semantics unchanged (`store.py:16-17` docstring holds: only one builder process mutates it, now for a longer stretch) |

### 7.2 Fusion slots — the design

One job = one "fusion slot" that runs as many slices as fit its wall budget:

```
forge slice --rom <rom> --until-budget 320m     # NEW: loop inside cmd_slice
  loop:
    probe INDEX → done? exit 0
    classification=capacity? exit 1 (unchanged deadlock guard)
    run_slice(budget = min(remaining_wall − reserve, slice_build_seconds))
    bank state (full today; delta under CAS-Relay)
    checkpoint every 30–60 min mid-slice (btrfs snapshot + manifest diff)
```

Changes required:

1. `engine.run_slice` gains a **wall-clock governor** distinct from the per-slice build budget: the job-level `--until-budget` (engine already has all the watchdog machinery — `budget_watchdog` is exactly this pattern, `engine.py:214-223`; it just needs to fire at "job wall minus reserve" and *loop back* instead of exiting).
2. `cli.cmd_slice` wraps the existing slice body in a `while` loop driven by INDEX state — the body itself (`cli.py:299-469`) is already idempotent per-slice; the loop only changes *when the process exits*.
3. `forge.yml` collapses slot-1…slot-10 (≈78 lines each, ~780 lines of copy-paste) into **one job repeated via the conveyor**, or better: a small matrix of 2–4 fusion jobs chained by `needs:`. The conveyor (already correct) re-dispatches when INDEX says more slices remain.
4. Banking cadence becomes: **bank at job boundary (full fidelity) + checkpoint banks mid-job (delta)**. The next fusion job restores base + deltas.

### 7.3 What this buys (and what it costs)

**Buys:**
- Relay events per 8-slice campaign: 8 → 3 (two job boundaries + final). At today's 20–30 min/event: **−100–150 min wall**; under CAS-Relay: −20–35 min.
- Soong analysis: paid per *job*, not per slice — and with graph banking (§4.2), effectively once per campaign. **−60–160 min wall** on an 8-slice campaign.
- Mining: 200 spins → 20–60 (per-job, 8–12 candidates). **−3–4 h runner-minutes per run.**
- Fixed per-job prelude (reclaim + volume + hydrate): ×10 → ×2–3. **−40–90 min wall.**
- Turbo overlap stays: turbo jobs run concurrently with fusion slot-1 exactly as today (`forge.yml:192-221` semantics unchanged).

**Costs (be honest about them):**
- **Blast radius:** a job-level failure (runner eviction, 350m kill) now loses the in-job progress since the last checkpoint — mitigated to 30–60 min by mid-slice checkpointing; today's design loses up to 275 min of unbaked work under the same failure (a killed slot banks *nothing* mid-flight; only the SIGINT path banks — `engine.py:208-211` graceful stop requires the watchdog to *fire*, an OOM-kill or eviction does not). Net: fusion slots with checkpointing are **strictly safer** than today.
- **Strict-mining redraw granularity:** a Zen 3 VM is stuck with 2–3 slices instead of 1. With Zen 5 hit rates of 82–97% per draw, the expected penalty is small; compensate by keeping slot-1 at higher candidate counts and letting postcheck's no-builder counter drive the next dispatch's `candidates` (§5.2).
- **`timeout-minutes: 350` discipline:** the governor must reserve 20–30 min for the boundary bank *inside* the job (bank-after-slice-2 runs while the clock still ticks). Pure arithmetic in `cmd_slice`.

### 7.4 Sequence risk: don't land fusion slots without checkpoint banks

The one ordering constraint in the whole roadmap: **fusion slots (B5) must land together with (or after) mid-slice checkpoint banking (§6.2) and the wall governor.** Landing the loop first "works" but regresses worst-case failure economics; landing checkpoints first is useful even today (protects the 275-minute exposure). The roadmap in §10 sequences them as one combined phase.

---

## 8. B6 — Source Hydration, Gate I/O, Coordination & Telemetry Drift

### 8.1 Source hydration (bottleneck B in the brief)

`src-<mhash>` restore is already streaming and single-part-on-disk (`chunker.unpack_from_store`) — good — but:

- **Serial part downloads (F6):** 17 parts, one `gh` process each, no prefetch: 6–9 min. With 2–3-way prefetch: **4–6 min.** T2, same change as the state relay.
- **Double-decompression tax:** the src snapshot is zstd-3 (immutable — correct choice), decompress ~1.5–2× faster than compress, but the restore pipeline runs a single zstd -d feeding tar. Shard the decode across parts (decode part k+1 while tar consumes part k) — piggybacks on the prefetch change. **−1–2 min.**
- **Restored-then-moved** (`syncer.restore_source` unpacks to `.forge-src-incoming` then `shutil.move` per top-level entry, `syncer.py:116-142`): the move is same-filesystem rename — cheap — but the *staging dir* doubles peak inode churn. Unpack directly into place with a `--keep-newer` guard; minor.
- **Turbo/device-repo fetch per slot:** `ensure_device_repos` re-validates per slot (idempotent clones are skipped when dst exists — `syncer.py:271-281` — fine), and `ensure_prebuilts` re-fetches `webview.apk`/`android.jar` when invalid (rare; `syncer.py:284-344`). OK as-is; move into the src snapshot proper: bake a `prebuilts-ok` stamp into `src-<mhash>` at sync time so slots skip the validation walk (T1: stamp file + one `if` in `cmd_slice`).

### 8.2 Gate I/O (F4) — verify/publish pay the full-state toll twice

`verify` (forge.yml:975-1008) and `publish` (forge.yml:1010-1037) each: fresh VM → prepare → **full final-state restore** (20–50 GiB) → do work that touches at most `out/target/product/<dev>/` + `out/host/linux-x86/bin` + the ROM zip. The gate's own code confirms the narrow surface: `Gate.__init__` reads product-dir build props + misc_info (`gate.py:120-139`); checks 1–14 use `pdir`, the zip, and `host_tools` (`avbtool`, `checkvintf` — `gate.py:142-149`); nothing else in `out/` is ever opened.

Fix (T2): **product-state banking** — at the final slice, bank `out/target/product/<dev>` + `out/host/linux-x86/bin` as `state-<key>-final-product` (2–6 GiB) alongside (or instead of, given gc keeps 2) the full state. `cmd_verify`/`cmd_publish` restore that tag. `publish` needs the same product dir for rescue images (`cli.py:543-549`). **−30–70 min per campaign, and removes the last two full restores.**

Under CAS-Relay this is automatic (verify restores only the product + host-tools buckets) — the explicit product-state bank is the cheap T2 bridge until CAS-Relay lands.

### 8.3 Coordination round-trips

- Every `store.target_update` does `index_load` + `index_save` = 2 release round trips (download INDEX.json, upload INDEX.json + .bak) per mutation; `cmd_slice` calls it once per slice — fine. But `mine.gate`'s done short-circuit (`mine.py:139-148`) and each candidate's `store.exists(tag)` poll every 10 s across 20 candidates hammer `gh release view` — 2 req/s sustained during scoreboards; harmless for correctness, mild for API secondary-rate limits (the harness covers this: `harness/test_api_limits.py`). Post-fix (§5.2): shorter scoreboards + fewer candidates cut this 3–5×.
- `list_tags --limit 200` (F8): paginate via `--json tagName` + cursor (`gh release list --limit` max 100/page? — actually `gh` supports up to 400; still paginate to be safe). T1.

### 8.4 Telemetry drift (F9) — you cannot optimize what you don't measure

Current instrumentation: `PROGRESS:{pct}:{done}/{total}` notices (`engine.py:339-343`), the 1 s heartbeat line (`engine.py:279-343`), `slice_summary` tables (`engine.py:456-482`), `[RESTORE] downloading part` prints (`chunker.py:331-344`). All ephemeral log lines; `monitor.py` — the thing named "monitor" — tracks the *pre-ROMForge* step names and a 160-minute budget constant (`monitor.py:17,29-39`), i.e., it monitors a pipeline that no longer exists.

**T1, ship with Phase 1: `PHASE_TIMING.json` per slot.** One structured document, machine-aggregatable into campaign rollups (postcheck already aggregates everything else):

```json
{
  "slot": 3, "runner": "EPYC 9V45", "jobs": 8,
  "phases": {
    "prepare_reclaim_s": 187, "volume_mount_s": 41, "source_restore_s": 412,
    "state_restore_s": 743, "soong_bootstrap_s": 96, "soong_analysis_s": 611,
    "build_s": 16483, "bank_s": 388, "checkpoint_banks_s": 121
  },
  "bytes": {"state_parts_gib": 28.4, "state_delta_gib": 4.1},
  "class": "sliced", "stop_reason": "budget"
}
```

Wire points: `cli.cmd_slice` phase timer context-manager; `engine.run_slice` already times itself; `chunker` bank/restore return bytes; upload as workflow artifact + attach to the `forge-index` release (small). Then the dashboard (`docs/dashboard.html`, `monitor.py`) has something real to plot, and the next optimization round argues from distributions, not anecdotes. **Every projected number in §1.2 becomes measurable the day this lands.**

---
## 9. Target Architecture v3 — Radical Rework

### 9.1 Design thesis

Keep every hard-won correctness property — exact-resume contract, capacity taxonomy, done-requires-zip, INDEX race-freedom, the 14-point gate, degrade-never-abort storage — and change only the three planes that carry the taxes identified above. v3 separates **orchestration** (GitHub Actions, unchanged), **state** (content-addressed delta CAS over Releases, GHCR-optional), and **compute** (GitHub-hosted runners for graph/gate/fallback + optional self-hosted fleet for compile).

```
┌────────────────────────── GitHub Actions (orchestrator, $0) ─────────────────────────┐
│ plan → sync(once per mhash) → turbo(×4–6, graph-restored) → fusion-slot-A → fusion-  │
│ slot-B → postcheck(INDEX→phase) → verify(product-state) → publish → conveyor          │
└──────────┬──────────────────────────┬───────────────────────────────┬────────────────┘
           │ CAS-Relay (state plane)  │ graph bank (mhash,lunch)      │ PHASE_TIMING
           ▼                          ▼                               ▼
  Releases/GHCR:               graph-<mhash>-<lunch>           forge-index + artifacts
  src-<mhash> (immutable)      out/soong/build.ninja            (telemetry rollups)
  state-<key>-s<N>:            + .bootstrap + .glob
    manifest.json  ← deltas →   (50–300 MiB, once per campaign)
    b.<bhash> buckets
    (content-addressed, shared across slices/campaigns)

Compute plane:  [GH runners]  fusion slots when fleet absent/disabled (fallback, always)
               [self-hosted, label: romforge]  fusion slots + turbo (Phase 3) —
                   deterministic silicon, 16+ cores, 32–64 GiB, LAN-speed state
```

### 9.2 State plane: CAS-Relay (B1's T3, specified)

- **Manifest** (JSON, gzipped, ≤2 MiB): flat array of `{p, s, m(ns), mode, b}` — path, size, mtime, mode, bucket id. Full manifest banked per slice; restore = base manifest + N deltas.
- **Buckets:** directory-partitioned archives (`out/soong`, `out/host`, `out/target/product/<dev>/obj/<top>`, `out/target/product/<dev>/{system,vendor,…}`, …) — 20–40 buckets, each tar+zstd-3, content-addressed `b.<sha16>` (its manifest hash), chunked at 1.9 GiB only if a bucket alone exceeds the asset cap. Buckets are **immutable and shared** across slices, turbo jobs, campaigns, even ROMs on the same Android version (same `out/soong` bucket when mhash matches).
- **Bank:** stat-walk `out/` (~30–60 s for 1–1.5 M files with `os.scandir` recursion — measure; fallback: walk at snapshot-diff time via btrfs snapshot pairs, §6.2), diff vs previous manifest, pack changed buckets (2 workers), parallel-upload pool (2–3 concurrent `gh`/`oras` uploads), write manifest + delta list. Target: **2–4 min per slice boundary** (frontier 3–6 GiB).
- **Restore:** fetch manifest chain, download listed buckets (prefetch 2–3), unpack, apply mtimes from manifest (tar preserves them — manifest is the audit + diff substrate, not the restore oracle). Target: **3–6 min warm.**
- **GC:** reference-count buckets by manifest reachability across *all* targets' kept states; unreferenced buckets deleted. (Straight extension of `store.gc_state`, `store.py:460-482`.)
- **Correctness invariant (unchanged):** the exact-resume contract is still mtimes + `.ninja_log` — the manifest never overrides ninja's own resume decision; a missed bucket manifests as "ninja re-runs cheap copy rules," the same self-healing property the exclusions already rely on (`relay.py:10-15`, `TECHNICAL.md §6.3`).

### 9.3 Compute plane: fusion slots + per-job mining

Fully specified in §7 (loop), §5.2 (mining per job), §6.2 (checkpoint snapshots). One addition: the **wall governor** (`--until-budget`) makes slice *size* adaptive — late-campaign slices can grow beyond `slice_build_seconds` when the wall allows (the 275-min budget exists to bound unbaked-loss, which checkpointing now bounds at 30–60 min instead).

### 9.4 Compute plane, Phase 3: the fleet tier

The brief's "silicon mining" exists because GitHub rents heterogeneous CPUs blind. A **self-hosted runner** (label `romforge`) ends the lottery: `runs-on: [self-hosted, romforge]` in the slot/turbo jobs, `runs-on: ubuntu-latest` retained via a `plan`-computed output for the fallback path when the fleet is offline (the `plan` job already computes the contract — add a fleet-probe step pinging the runner's `GET /repos/:repo/actions-runners` label list, route accordingly).

Hardware target for A15–A17 campaigns: 16 cores / 32–64 GiB / 1–2 TiB NVMe (a single consumer desktop class box; alternatively a 2×8-core used EPYC). Expected A17 cold on such a node: **4–6 h single-machine**, using the identical `forge` CLI — the fleet is just a *runner*, not a fork of the codebase. Safety properties hold: state still banks through the CAS; the gate still blocks publish; `protect_runner_processes` still shields the agent.

Fleet economics honesty: this exits the "$0" premise. Two mitigations: (i) GitHub remains the *complete* fallback — `FORGE_FLEET=0` flips every job back to hosted runners; (ii) the fleet also serves the graph bank + turbo prewarms, so even a *small* fleet node (8 cores) absorbs B2 and the mining lottery while hosted runners keep the long compile tail.

### 9.5 The REAPI seam (why not Bazel, but steal its protocol)

TECHNICAL.md §11 already rejects goma/reclient (no free backend) and full Bazel migration is out of the question for AOSP — `bp2build` covers a fraction of the tree, and the ninja/soong graph *is* the build system. But Bazel's **Remote Execution API** got one thing profoundly right: a universal **CAS + action-cache** protocol. CAS-Relay's `(bucket digest, manifest)` layout deliberately mirrors `Digest{name, hash, size}` so that:

- a future BuildBarn/BuildGrid/EngFlow cluster can *replace* the backend with zero orchestration changes (upload/download become `ByteStream` stubs);
- `ccache`'s remote-storage mode (sccache-style HTTP CAS) can layer *on the same bucket store* for cross-mhash C/C++ hits — the one place ccache still adds value once CAS-Relay exists (cross-campaign source bumps), and it is opt-in today (`engine.py:16-17`, `USE_CCACHE` env, `engine.build_env:87-93`).

Do **not** build the REAPI server now. Build the seam: keep backend interfaces at `store.py`'s narrow surface (`exists/create/upload/download/list`) — which is already the case — and keep CAS-Relay's wire format digest-shaped.

### 9.6 Why not: the rejected radical alternatives (for the record)

| Alternative | Why rejected |
|---|---|
| Full Bazel/bp2build migration | covers a module subset; 6–12 months of work to *maybe* get caching semantics that CAS-Relay gives the ninja world in weeks; AOSP itself doesn't build the platform this way |
| Distributed ninja (sninja/n2 remote) | soong's graph assumes local-fs semantics (TECHNICAL.md §11 is right); the turbo partition approximation is strictly safer |
| GitLab CI mirror for parallel free compute | second orchestrator to maintain, duplicate state plane, and its free runners are the same 4-vCPU class — solves nothing GitHub doesn't |
| `actions/cache` for state | 10 GiB LRU-evicted, silently vanishes mid-campaign (upstream already burned by this; TECHNICAL.md §11) |
| Oracle free tier VMs as build compute | Ampere A1 is ARM — host toolchain is x86_64; the AMD micro instances are 1/8 OCPU — unusable |
| Colab/Azure free hours | session walls, no NVMe persistence, ToS-hostile to CI (documented in the inherited upstream report) |

---
## 10. Phased Refactoring Roadmap

Ordering rationale: Phase 0 verifies and instruments (you cannot safely rework what you don't measure); Phase 1 removes the *event count* and the lottery waste (wall + minutes, no protocol change); Phase 2 changes *what travels* and kills the Soong repetition (protocol + graph); Phase 3 moves the compute constraint (fleet); Phase 4 is optional exotic. Every phase is independently shippable and independently reversible — each row names its kill-switch.

Effort: **S** ≤ 1 day · **M** 2–5 days · **L** 1–3 weeks · **XL** > 3 weeks. Gains are ranges with assumptions in Appendix D.

| # | Phase | Change | Files touched | Effort | Expected gain (A17 campaign) | Risk (mitigation) | Kill-switch |
|---|---|---|---|---|---|---|---|
| 0.1 | P0 Verify | Land nothing; confirm F1/F3/F8 by reading + one dry-run campaign with `PHASE_TIMING` manual collection | — | S | baseline truth | none | — |
| 0.2 | P0 Instrument | `PHASE_TIMING.json` per slot + artifact upload + postcheck rollup; update `monitor.py` stage table (or delete it in favor of dashboard) | `cli.py`, `engine.py`, `chunker.py`, `forge.yml` (upload step) | S–M | all later claims become measurable | trivial | `FORGE_TELEMETRY=0` |
| 1.1 | P1 | **F1 fix:** add `symbols*` patterns to `STATE_EXCLUDES` (+ unit test mirroring ladder's) | `relay.py:31-38` | S | bank/restore −2–5 min each; volume +8–15 GiB headroom; fewer `capacity` stops | low — ninja re-runs copy rules (documented safe) | revert one line |
| 1.2 | P1 | **F3 fix:** conveyor passes original `candidates`; no-builder → immediate re-dispatch w/ `candidates=20`, scoreboard 240→90 s (slots ≥2), candidates 20→8 | `forge.yml`, `mine.py` defaults | S | fan-out minutes −60–70% (~5 h → ~1.5–2 h/run); no-builder cycle −1–2 runner-h | low | `candidates` input |
| 1.3 | P1 | **Parallel upload pool + prefetch downloads** (2–3 workers both directions; part-parallel zstd -d) | `chunker.py` | M | bank −25–40%, restore −30–50% (both state & src) | med — retry/backoff semantics per worker; keep 3×20 s backoff per part | `FORGE_IO_SER=0` |
| 1.4 | P1 | **Product-state bank** for verify/publish (`state-<key>-final-product`) | `cli.py` (cmd_slice done-path, `_ensure_out`), `relay.py` | M | −30–70 min/campaign; removes 2 full restores | low — fallback to full state exists | restore-order list |
| 1.5 | P1 | **zram swap tier-1 + GOMEMLIMIT=11–12g** in build env | `env.py` (`ensure_zram`), `engine.py:67-94` | S | OOM-abort risk −50–70%/slot; swap thrash −50–80%; analysis +10–20% wall | low-med (GC thrash if live-set > limit — gctrace detect, env-off) | env vars |
| 2.1 | P2 | **Fusion slots**: `forge slice --until-budget` loop + wall governor + collapse slot-1..10 → 2–3 fusion jobs + conveyor-driven repeats; per-job mining | `cli.py`, `engine.py`, `forge.yml` (−700 lines) | L | relay events 8→3 (−100–150 min wall w/ today's relay); soong per-job (−60–160 min); spins 200→~30 | **must co-ship 2.2** (blast radius) | `FORGE_FUSION=0` → one-slice-per-job path |
| 2.2 | P2 | **Mid-slice checkpointing**: btrfs snapshot every 30–60 min + delta bank (shares 2.3 machinery) | `storage.py` (snapshot helper), `relay.py`, `engine.py` | M–L | worst-case loss 275→30–60 min; enables 2.1 safely; also improves *today's* failure economics | med — snapshot diff correctness (property tests vs full bank) | `FORGE_CKPT_MIN=0` |
| 2.3 | P2 | **CAS-Relay**: manifest + bucket digests + delta banks/restores; GC reachability; `FsStore` parity; src snapshot stays opaque | `relay.py`, `chunker.py`, `store.py`, `cli.py` | XL | bank 2–4 min / restore 3–6 min; Release traffic −60–75%; cross-campaign bucket reuse | **high** — new protocol; gate on property tests + A/B one campaign (`FORGE_RELAY=legacy`) | dual-path env flag, keep legacy relay one release cycle |
| 2.4 | P2 | **Graph banking**: stop purging `.bootstrap/.glob` when mhash matches (fix F2); bank `graph-<mhash>-<lunch>`; `FORGE_SOONG_BYPASS` ninja-direct path with stamp guards | `relay.py:174-178`, `syncer.py:367-371`, `cli.py`, new `forge_core/graph.py` | L | −8–18 min/slot after first; memory spike eliminated on slots 2+; turbo jobs too | **high** — bypass must prove staleness; stamp + fall-back + `ninja -d explain` CI audit + hard gate backstop | `FORGE_SOONG_BYPASS=0` |
| 2.5 | P2 | **Split volumes**: `src.img` (compress-force=zstd:3) + `out.img` (nodatacow) | `storage.py` | M | compile I/O CPU −5–15%; fragmentation/inode risk down | med — two loop mounts (both degrade independently) | `FORGE_NO_VOLUME`/plain mode |
| 2.6 | P2 | **list_tags pagination** + `gc_locks` TTL | `store.py` | S | closes F8 | low | — |
| 3.1 | P3 | **Fleet runner**: self-hosted label `romforge`; plan-job fleet probe routes `runs-on`; fusion slots/turbo prefer fleet | `forge.yml`, `mine.py` (skip when labeled), `docs/` | M | A17 cold 12–18 h → 4–6 h; GH runner-min 65–75 → 3–5 h; zero lottery | med — fleet ops (offline handling, updates); fallback pool always available | `FORGE_FLEET=0` |
| 3.2 | P3 | **Turbo via snapshot-send**: merge donors with `btrfs send/receive` on fleet / diff-packs on GH | `turbo.py`, `relay.py` | M | F12 fix: −10–25 min cold slot-1 | med | legacy rsync path |
| 3.3 | P3 (optional) | **GHCR CAS backend** for buckets (`oras`); keep Releases default | `store.py` (new backend class) | M–L | dedup across campaigns; Releases quota relief | med — ToS greyness on non-container blobs | backend flag |
| 4.1 | P4 (exotic) | REAPI server (BuildBarn) on fleet; wire `store` CAS to it | `store.py`, fleet | XL | remote-execution-class caching for C/C++ | high | don't |
| 4.2 | P4 (exotic) | ccache remote layer over CAS buckets for cross-mhash hits | `engine.py`, configs | M | warm-rebuild after source bump −20–40% | med | `USE_CCACHE` opt-in as today |

**Cumulative projection (GH-only path, P0–P2 complete):** cold A17 wall **6–10 h** (from 12–18), runner-minutes **25–35 h** (from 65–75), OOM risk −50–70%, relay bytes −60–75%. **Fleet path (P3):** cold A17 **4–6 h**, GH minutes **3–5 h**.

### 10.1 What NOT to do (sequencing traps)

1. **Do not land 2.1 (fusion) before 2.2 (checkpoints).** Failure economics regress otherwise (§7.4).
2. **Do not land 2.3 (CAS-Relay) and 2.4 (graph banking) in the same week.** Both touch the state contract; bisectability requires one release cycle between them. CAS-Relay first (it is load-bearing for checkpoints + product-state), graph banking second.
3. **Do not widen turbo before fusion lands.** Turbo spends concurrency slots that fusion slots then need; today's 4-target turbo on shiba is the right size until the slot chain stops paying per-slice preludes.
4. **Do not chase compression levels further.** S3 (`zstd -1 --long`) banked that win; the next 5 minutes lives in event count and delta bytes, not in zstd flags.

---
## Appendix A — Configuration Templates

### A.1 forge.yml v3 — fusion slot job (replaces slot-1…slot-10, ~780 lines → ~90)

```yaml
  fusion-slot-a:
    name: "Fusion slot A (2–3 slices, one VM)"
    needs: [plan, sync]
    if: >-
      always() && needs.plan.result == 'success' &&
      (needs.sync.result == 'success' || needs.sync.result == 'skipped')
    runs-on: ${{ needs.plan.outputs.slot_runner }}   # fleet label or ubuntu-24.04 (plan routes)
    permissions: { contents: write }
    timeout-minutes: 350
    strategy:
      fail-fast: false
      max-parallel: ${{ fromJSON(needs.plan.outputs.candidates) && 12 || 1 }}  # per-JOB mining now
      matrix:
        candidate: ${{ fromJSON(needs.plan.outputs.mining) }}   # 8–12, not 20
    env:
      ROM: ${{ github.event.inputs.rom || 'qassa-a10' }}
      KEY: ${{ needs.plan.outputs.key }}
      LOCK: lock-${{ needs.plan.outputs.key }}-r${{ github.run_id }}-fsA
      FORGE_UNTIL_BUDGET_S: "19200"   # 320 min job budget (30 min reserved for boundary bank)
      FORGE_CKPT_MIN: "45"            # mid-slice checkpoint cadence
      FORGE_RELAY: "cas"              # "legacy" → old full-snapshot relay
    steps:
      - uses: actions/checkout@v4
      - name: "Silicon mining gate (probe + atomic claim)"
        id: build_gate
        env: { GH_TOKEN: "${{ github.token }}" }
        run: |
          set -euo pipefail
          sleep $((RANDOM % 20))       # stagger the burst; fastest prober claims
          python3 -m forge_core.mine gate --tag "$LOCK" --key "$KEY" \
            --min-score 100 --wait-s 120 --strict | tee mine.txt
          ROLE=$(sed -n 's/^OUT: role=//p' mine.txt | tail -1)
          echo "role=$ROLE" >> "$GITHUB_OUTPUT"
          [ "$ROLE" = "builder" ] || { echo "::notice::role=$ROLE — fast-discard"; exit 0; }
      - name: "Prepare storage volume"
        if: steps.build_gate.outputs.role == 'builder'
        run: |
          sudo systemctl stop docker containerd 2>/dev/null || true
          sudo rm -rf /var/lib/docker /var/lib/containerd 2>/dev/null || true
          sudo mkdir -p /mnt/romforge /opt/romforge && sudo chmod 1777 /mnt /mnt/romforge /opt /opt/romforge 2>/dev/null || true
      - uses: actions/setup-python@v5
        if: steps.build_gate.outputs.role == 'builder'
        with: { python-version: "3.11" }
      - run: python3 -m pip install --quiet pyyaml
        if: steps.build_gate.outputs.role == 'builder'
      - name: "Fusion build loop (slices until wall budget)"
        if: steps.build_gate.outputs.role == 'builder'
        env:
          GH_TOKEN: ${{ github.token }}
          FORGE_TELEMETRY: "1"
        run: python3 -m forge_core.cli --root . slice --rom "$ROM" --until-budget 19200
      - name: "Upload slice log + PHASE_TIMING"
        if: always() && steps.build_gate.outputs.role == 'builder'
        uses: actions/upload-artifact@v4
        with:
          name: fusion-a-logs
          path: |
            /tmp/forge-slice.log
            /tmp/forge-phase-timing/*.json
          if-no-files-found: ignore
```

The conveyor (unchanged semantics) re-dispatches while `phase=slice`; postcheck GCs `lock-<key>-*` per run as today (`forge.yml:961-965`). A second fusion job `fusion-slot-b` chains via `needs: [plan, fusion-slot-a]` only if you want two VMs in flight per run (usually unnecessary — the conveyor already serializes campaign resumption).

### A.2 Split-volume layout (storage.py v3 target)

```bash
# <best-mount>/romforge/
#   src.img   btrfs compress-force=zstd:3,noatime   (mounted at vol/src)
#   out.img   btrfs nodatacow,noatime               (mounted at vol/out)
#   tmp/      raw (TMPDIR — unchanged, engine.build_env routes it)
#   .forge-swap[.chunk.N]  raw (zram is tier-1, these are tier-2)
sudo truncate -s ${SRC_CAP}G src.img && sudo mkfs.btrfs -q src.img
sudo mount -o loop,compress-force=zstd:3,noatime src.img vol/src
sudo truncate -s ${OUT_CAP}G out.img && sudo mkfs.btrfs -q out.img
sudo mount -o loop,nodatacow,noatime out.img vol/out
# BUILD_ROOT layout: vol/src/aosp  (source), vol/out/aosp-out → symlinked
# as vol/src/aosp/out (canonical-link compatibility, storage._ensure_canonical_link)
```

Sizing: `SRC_CAP ≈ expected_source_gb / 2.4` (75 GiB → ~32 GiB), `OUT_CAP = free − SRC_CAP − reserve(10)`. Keep the hard-cap + `MIN_VIABLE_FREE_GB=25` + degrade-to-plain semantics identical (`storage.py:62,241-246`).

### A.3 zram tier-1 swap (env.ensure_zram, called from cmd_prepare)

```bash
modprobe zram num_devices=0 || true
zramctl -f -s 8G -a zstd -p 100 /dev/zram0 2>/dev/null || exit 0   # tier-1, priority 100
mkswap /dev/zram0 && swapon -p 100 /dev/zram0
# existing fallocate/NVMe chunks stay at priority 10 (env.activate_swap_chunk already
# uses `swapon -p 10` — env.py:300) — tiering falls out of the priorities.
```

### A.4 Soong memory guards (engine.build_env additions)

```python
e["GOMEMLIMIT"] = os.environ.get("FORGE_SOONG_MEM_LIMIT", "11GiB")  # Go GC soft cap
e["GODEBUG"]    = (e.get("GODEBUG", "") + ",gctrace=1").lstrip(",")  # only when FORGE_TELEMETRY
# leave FORGE_SOONG_MEM_LIMIT="" to disable (fall back to pure-swap behavior)
```

### A.5 ccache layering (opt-in, cross-mhash value only)

```yaml
# rom profile env (lineage-a17-shiba.yaml)
env:
  USE_CCACHE: "1"
  CCACHE_DIR: "/mnt/romforge/tmp/ccache"        # raw dir, not inside btrfs (same rule as swap)
  CCACHE_MAXSIZE: "12G"
  CCACHE_COMPILERCHECK: "content"               # survives toolchain bumps within a branch
  CCACHE_NOHASHDIR: "true"
# bank ccache as its own CAS bucket (b.ccache) — content-addressed by design;
# restore before slots, bank at job boundary only (it changes slowly).
```

### A.6 Fleet runner registration (Phase 3)

```bash
# on the fleet node (Ubuntu 24.04, 16c/64G/2T NVMe):
mkdir ~/actions-runner && cd ~/actions-runner
curl -o actions-runner-linux-x64-*.tar.gz -L \
  https://github.com/actions/runner/releases/download/v2.32x.0/actions-runner-linux-x64-2.32x.0.tar.gz
tar xzf actions-runner-linux-x64-*.tar.gz
./config.sh --url https://github.com/zephyr4289/aosp-a10 \
  --token <REG_TOKEN> --labels romforge --name forge-node-1
sudo ./svc.sh install && sudo ./svc.sh start
# forge.yml plan job routes: outputs.slot_runner = fleet online ? '[self-hosted,romforge]' : 'ubuntu-24.04'
# (probe: gh api /repos/:repo/actions/runners --jq '.runners[].labels[].name' | grep -q romforge)
```

---

## Appendix B — Pseudo-Diffs for Key Changes

### B.1 relay.py — F1 symbols exclusion + incremental manifest scaffolding (Phase 1.1 / 2.3)

```diff
 STATE_EXCLUDES = [
     "out/target/product/*/obj/*/oat_x86*",
     "out/target/product/*/*.img.new",
+    "out/target/product/*/symbols*",          # F1: rebuild-cheap, ladder already
+    "out/target/product/*/*/symbols*",        # trusts regeneration (env.py:362-366)
     "out/soong/.temp-dir*",
     "out/soong/.temp*",
     "out/soong/.temp",
     "out/.reclaim_tmp",
 ]
```

```diff
+# --- CAS-Relay v3 (Phase 2.3) -------------------------------------------
+def manifest_of(out_dir: Path) -> List[Dict]:
+    """(path, size, mtime_ns, mode) walk; bucket = top dirs under out/."""
+    ...
+
+def bank_delta(build_root, store, tag, key, slice_no, base_manifest) -> int:
+    """Pack only buckets whose (files,mtimes) changed vs base_manifest;
+    content-address each bucket as b.<hash16>; upload manifest + deltas."""
+    ...
+    # parallel upload pool: 2-3 workers pulling from a bounded queue
+    # (replaces chunker.stream_pack's serial --filter sink for state banks)
```

### B.2 cli.py — cmd_slice fusion loop (Phase 2.1)

```diff
 def cmd_slice(args, root: Path) -> int:
     ...
+    wall_left = int(getattr(args, "until_budget", 0) or 0)
+    while True:
+        t = store.target(plan.rom.key)
+        if t.get("done") and not args.force:
+            log.out("classification", "done"); return 0
+        if t.get("last_classification") == "capacity" and not args.force:
+            log.out("classification", "capacity"); return 1
+        budget = min(int(args.budget_s or plan.rom.slice_build_seconds),
+                     wall_left - BANK_RESERVE_S) if wall_left else \
+                 int(args.budget_s or plan.rom.slice_build_seconds)
+        if budget < MIN_SLICE_S:
+            return 0   # not enough wall for another slice — exit green, conveyor re-dispatches
+        res = _one_slice(plan, store, build_root, budget, ...)   # extracted current body
+        wall_left -= int(res["elapsed_s"]) + _overhead_s(res)
+        if res["classification"] in ("error", "capacity"):
+            return 1 if res["classification"] == "error" else 0
```

### B.3 engine.py — wall governor + checkpoint hook (Phase 2.1/2.2)

```diff
+CHECKPOINT_MIN = int(os.environ.get("FORGE_CKPT_MIN", "0") or 0)  # 0 = off
+
     def checkpoint_watchdog() -> None:
+        """Every CHECKPOINT_MIN mid-slice: btrfs snapshot (O(metadata)) +
+        optional delta bank. Protects fusion slots' longer exposure."""
         while not stop.wait(max(CHECKPOINT_MIN * 60, 60)):
             try:
-                storage.snapshot(build_root)   # existing disk polling only
+                if CHECKPOINT_MIN:
+                    storage.ckpt_snapshot(build_root / "out")
+                    relay.bank_delta_if_any(...)     # cheap: manifest diff vs last ckpt
+                storage.snapshot(build_root)
             except Exception:
                 pass
```

### B.4 chunker.py — parallel upload pool + prefetch downloads (Phase 1.3)

```diff
-def stream_pack(root, member, prefix, sink_sh, sums_out, ...):
-    inner = ('set -e; cat > "$FILE"; sha256 ...; '
-             'n=0; until sh -c "$FORGE_SINK"; do ...; done; rm -f "$FILE"')
+def stream_pack(root, member, prefix, sink_sh, sums_out, ..., workers: int = 0):
+    # workers=0 → legacy serial filter (FORGE_IO_SER=0). Else:
+    # filter writes part to $FILE and enqueues (path, size); pool of N workers
+    # run the sink concurrently (same 3x20s backoff per part); tar|zstd never stall.
+    inner = ('set -e; cat > "$FILE"; sha256 ... >> "$FORGE_SUMS"; '
+             'mv "$FILE" "$FORGE_Q/$(basename "$FILE")"')

-def unpack_from_store(store, tag, prefix, dest, ...):
-    # feeder: for each part: download → hash → feed → unlink   (serial)
+def unpack_from_store(store, tag, prefix, dest, ..., prefetch: int = 2):
+    # downloader threads keep a ring of `prefetch` parts on disk ahead of the
+    # feeder; feeder hashes+feeds+unlinks as today. Effective bandwidth →
+    # max(network, decompress) instead of network+decompress serialized.
```

### B.5 mine.py / forge.yml — per-job mining + no-builder fast path (Phase 1.2)

```diff
 # forge.yml conveyor
           CANDIDATES: ${{ github.event.inputs.candidates || '20' }}   # was hardcoded '8' (F3)
+          # postcheck adds: builders=N output; phase_reason=no-builder routes here
+          # → immediate re-dispatch (skip cron wait), candidates bumps 20 on retry
```

### B.6 store.py — list_tags pagination (Phase 2.6)

```diff
     def list_tags(self, prefix: str) -> List[str]:
-        r = self._gh("release", "list", "--limit", "200", "--json", "tagName")
-        if r.returncode != 0:
-            return []
-        return [e["tagName"] for e in json.loads(r.stdout or "[]") ...]
+        tags, page = [], 1
+        while True:
+            r = self._gh("release", "list", "--limit", "100", "--json", "tagName",
+                         check=False)
+            if r.returncode != 0:
+                break
+            batch = [e["tagName"] for e in json.loads(r.stdout or "[]")]
+            tags += [t for t in batch if t.startswith(prefix)]
+            if len(batch) < 100:
+                break
+            page += 1
+        return tags
```

---
## Appendix C — Migration Runbook

Phase-gated rollout; every phase has a kill-switch env/input and a validation gate. Run the full local suite before *and* after each phase: `bash tests/run_tests.sh` (93 assertions) + `python3 harness/run_all.py` (engines A–L) — both already wired in `ci-tests.yml` on every push, so a broken phase never leaves the branch.

### C.1 Phase 0 — instrument first (no behavior change)

1. Add `PHASE_TIMING.json` emission: a `Timing` context manager in `cli.py` around each stage of `cmd_slice` (prepare/source/state/soong happens across `cli`+`engine`; the build phase self-times in `engine.run_slice`; bank/restore self-time in `chunker`). Write to `/tmp/forge-phase-timing/slot-<n>.json`; upload as workflow artifact (`if: always()`).
2. postcheck downloads all slot artifacts, aggregates a campaign rollup, attaches it to `forge-index` (tiny asset, same pattern as INDEX.json.bak).
3. Update `monitor.py` STAGE_KEYS to the current pipeline or delete the file and point README at `docs/dashboard.html` with the rollup as data source (F9).
4. **Validation:** one dry campaign (or the daily cron pulse) produces complete timing sets; spot-check that `state_restore_s` and `bank_s` match the manual log deltas. This is your baseline for every later claim.

### C.2 Phase 1 — event-count and I/O wins (kill-switches everywhere)

1. `relay.py:31-38` + symbols patterns (1.1) → unit test: pack a fixture tree with `symbols/`, assert exclusion; mirror the ladder-safety test (`tests/test_all.py` covers `pre_bank_actions` offline already).
2. `forge.yml` conveyor candidate fix + scoreboard 90 s (1.2). Validation: force a strict-miss run (candidates on a `no-builder` loop), observe fast re-dispatch instead of the spiral.
3. `chunker.py` parallel pool + prefetch (1.3). Validation: `tests/test_archive_roundtrip.sh` (exists), plus a new harness engine M: pack/unpack under fault injection (`harness/test_store_faults.py` pattern) with 2–3 workers — assert every part retried ≤3× and SHA256SUMS complete.
4. Product-state bank (1.4). Validation: `forge verify --state product` on a fixture tree; gate still PASSes (the offline gate test builds a synthetic product tree — extend it to consume the product-state tag).
5. zram + GOMEMLIMIT (1.5). Validation: `harness/test_oom_swap.py` extended with a `GOMEMLIMIT`-capped Go hog process — assert swap pressure stays under the old threshold.

**Rollback:** each item is env/flag-gated; revert by flipping flags in the next dispatch, no state migration needed (Phase 1 changes nothing about state format).

### C.3 Phase 2 — protocol + graph (state-format changes; dual-path mandatory)

Order: **2.3 CAS-Relay → 2.2 checkpoints → 2.1 fusion → 2.4 graph bank → 2.5 volumes.** (CAS-Relay first because checkpoints and product-state lean on its diff machinery; fusion last among the three because it leans on checkpoints; graph bank and split volumes are independent.)

1. **CAS-Relay (2.3):** implement `bank_delta`/`restore_manifest_chain` alongside the legacy path; `FORGE_RELAY=cas|legacy` routes. Ship with **A/B campaign**: one target on cas, one on legacy, compare `PHASE_TIMING` rollups + `sha256` of final out/ trees (must be identical — the exact-resume contract is the acceptance test). GC: reachability across kept manifests. Keep legacy path for one full release cycle.
2. **Checkpoints (2.2):** snapshot helper + delta bank; property test: checkpoint-bank chain vs full bank produce identical restored trees after a SIGINT at random times (extend `harness/test_watchdog_chaos.py`).
3. **Fusion slots (2.1):** collapse forge.yml to the A.1 template; keep the old slot jobs in git history (and available via `FORGE_FUSION=0` workflow input for one cycle). Validation: 24-slice worst-case campaign converges; no `capacity`-park regressions (DAG table tests already exhaustive).
4. **Graph bank (2.4):** fix F2 first (stop purging `.bootstrap/.glob` when `.source_ready` mhash matches — this alone is a safe T2 win). Then `graph-<mhash>-<lunch>` banking + `FORGE_SOONG_BYPASS` with stamp guards. Validation: on a small tree (A10 qassa), run full-soong vs bypass on the same restored state; `ninja -d explain` diff must be empty; final zips byte-compare.
5. **Split volumes (2.5):** `storage.selftest` extended to mount both images; degraded modes verified per-image.

**Rollback:** state format is dual-readable (manifest restore falls back to legacy tags); forge.yml keeps one-cycle flag; graph bank stamp-miss falls back to full soong_ui automatically.

### C.4 Phase 3 — fleet

1. Register the runner (A.6); `plan` job probes fleet availability and routes `slot_runner` output.
2. First fleet campaign runs with `FORGE_TELEMETRY=1`; compare PHASE_TIMING vs the GH pool distribution (the fleet should show near-zero `mining`, `source_restore` at LAN speed if CAS backend is co-located… note: state still goes through Releases — fleet does not change the state plane, only compute).
3. Concurrency: `forge.yml` fusion jobs prefer fleet; turbo can run mixed (fleet + GH) — `max-parallel` semantics unchanged.
4. **Rollback:** `FORGE_FLEET=0` input or offline fleet → `plan` routes everything back to hosted runners. Nothing else changes.

---

## Appendix D — Cost Model Mathematics

All ranges use these runner-class assumptions (stated once, referenced everywhere): effective end-to-end Release transfer 50–80 MiB/s (gh CLI, single stream; matches brief's ~50–80 MB/s and observed parts/min in `[RESTORE]` logs); zstd-1 compress ~0.9–1.5 GiB/min and decompress ~1.5–2.5 GiB/min per active pipeline on 4 vCPU; tar walk 5–8k files/s; A17 `out/` 30–45 GiB post-excludes (45–60 GiB pre-F1-fix incl. symbols); frontier growth 3–6 GiB/slice (ninja outputs on 4 vCPU at ~85–95% module-complete slices).

**D.1 Today (A17/shiba cold, 8 slices, turbo 4, strict mining 20):**
- Slot prelude: checkout+pip 0.5–1 + reclaim 2–4 + volume 0.5–1.5 + src hydrate 6–9 + state restore 8–15 + patches/prebuilts 1–2 + soong bootstrap+analysis 8–18 → **25–45 min** (matches brief's totals).
- Build 275 + bank 6–12 → slot total 306–332 min on the winner.
- Runner-minutes: 8 winners ×~5.5 h ≈ 44 h; turbo 4 × ~5 h ≈ 20 h; sync 1.5 h; verify+publish 1.5–2.5 h; discards ~190 × 1–2 min ≈ 3–6 h → **65–75 h total**.
- Wall: winner chain 8 × (25–45 + 275 + 6–12) ≈ 41–56 h serialized, minus turbo overlap (~25–45% of work offloaded) → **12–18 h typical**; strict-miss or turbo-miss pushes 20–40 h.

**D.2 After Phase 1 (no topology change):**
- Relay events unchanged (8+2) but each −25–50% via I/O pipelining + F1: saves 35–90 min wall.
- Fan-out −60–70%: −3–4 h runner-min.
- OOM risk −50–70%.
- Net: wall −5–10%, minutes −10–15% (the phase is mostly reliability + I/O hygiene; the big structural wins are P2).

**D.3 After Phase 2 (GH-only):**
- Relay events: 3 (2 job boundaries + 1 checkpoint avg) × 3–6 min (delta) ≈ 9–18 min vs 2.5–5 h → **−2–4 h wall**.
- Soong: once (graph bank) ≈ −60–160 min.
- Preludes: ×3 not ×10 → −40–90 min.
- Mining: ~30 spins → −3–4 h runner-min.
- Total: wall **6–10 h**; runner-min **25–35 h** (turbo unchanged ~20 h dominates; fleet is what removes it).

**D.4 After Phase 3 (fleet):** compute on 16c/64G: A17 cold ≈ 32 h × (4/16 vCPU-equivalent × 1.2 fleet efficiency) ≈ 4–6 h wall; GH keeps plan/probe/postcheck/verify/publish ≈ 1–2 h runner-min (verify on fleet if routed).

**D.5 Where the numbers are soft:** effective transfer rate variance (±40%) dominates every relay estimate — hence Phase 0 instrumentation before Phase 2 commitment; frontier-growth rate (3–6 GiB/slice) is an assumption from ninja output volumes, not yet measured — CAS-Relay's manifest walk will measure it directly; turbo offload fraction depends on shiba's 4-target set actually overlapping slot-1's critical path (PHASE_TIMING on turbo jobs will show it).

---

## Appendix E — Risk Register

| Risk | Phase | Likelihood | Impact | Mitigation | Kill-switch |
|---|---|---|---|---|---|
| Fusion slot dies mid-job → more unbaked loss than today | 2.1 | med | med | co-ship 2.2 checkpoints (30–60 min loss bound, better than today's 275) | `FORGE_FUSION=0` |
| CAS-Relay manifest/bucket bug corrupts resume state | 2.3 | med | **high** | A/B campaign byte-compare; dual-path for one cycle; sha256 per bucket; self-healing = ninja re-runs (same argument as STATE_EXCLUDES) | `FORGE_RELAY=legacy` |
| Soong bypass uses stale graph after silent source mutation | 2.4 | low-med | **high** | stamp = manifest-hash + lunch + soong binary hash; mtime scan of `Android.bp` newer-than-stamp forces full path; `ninja -d explain` CI audit; 14-point gate backstop | `FORGE_SOONG_BYPASS=0` |
| Parallel upload pool trips Release rate limits / 5xx storms | 1.3 | med | low | keep 3×20 s per-part backoff; cap workers 3; sink already retries | `FORGE_IO_SER=0` |
| GOMEMLIMIT causes GC thrash (live set > limit) | 1.5 | low | med | gctrace in telemetry; auto-raise limit + fall back to swap behavior | unset env |
| GHCR CAS backend ToS greyness (non-container blobs) | 3.3 | med | low (optional) | Releases remains default backend | backend flag |
| Fleet node offline mid-campaign | 3.1 | med | low | plan-job probe reroutes to hosted pool; state plane identical | `FORGE_FLEET=0` |
| list_tags >200 silent miss (pre-fix exposure) | today | low | med | 2.6 pagination ships in P2; until then, gc discipline keeps tag count <200 | — |
| Snapshot diff vs full-pack divergence on hardlinked files | 2.2/2.3 | low | med | tar `-h` already dereferences hardlinks (`chunker.py:73`); property tests cover link-heavy fixtures | legacy path |
| Two loop mounts double mount-failure surface | 2.5 | low | low | independent degrade-to-plain per image (selftest extended) | `FORGE_NO_VOLUME` |

---

## Appendix F — Citation Index (function → file:lines)

All line numbers verified against the A17-experiment working tree at audit time (2026-10-08). `forge.yml` = `.github/workflows/forge.yml`.

| Function / construct | Location |
|---|---|
| `cmd_slice` (slot state machine, 0–7 steps) | `cli.py:299-469` |
| `cmd_slice` warm-restore + newest-state fallback (R3) | `cli.py:378-395` |
| `cmd_slice` done/sliced/capacity/error banking branches | `cli.py:415-469` |
| `_ensure_out` (verify/publish full-state restore) | `cli.py:472-485` |
| `cmd_verify` / `cmd_publish` (gc keep=1) | `cli.py:488-510`, `cli.py:513-566` |
| `cmd_prepare` (volume-first, swap-on-raw) | `cli.py:172-227` |
| `build_env` (TMPDIR routing, ccache opt-in) | `engine.py:67-94` |
| `optimal_jobs` (dynamic -j, S1) | `engine.py:101-149` |
| `run_slice` (pgid SIGINT, watchdogs, heartbeat) | `engine.py:152-413` |
| budget watchdog (SIGINT→KILL 300 s) | `engine.py:214-223` |
| disk watchdogs (3 surfaces) | `engine.py:234-273` |
| dynamic swap scaler (+2 G ×6, phys>12 gate) | `engine.py:348-380` |
| `classify_exit` (taxonomy) | `engine.py:416-432` |
| `slice_summary` | `engine.py:456-482` |
| `next_action` (no-builder→slice, capacity→fail) | `dag.py:31-65` |
| `finalize_classification` (done-requires-zip) | `dag.py:68-77` |
| `mining_matrix` (c01..c20) / `lock_tag` | `dag.py:80-92` |
| `STATE_EXCLUDES` (F1: symbols missing) | `relay.py:31-38` |
| `pre_bank_actions` / `pre_bank_cleanup` (never-delete-source in volume mode) | `relay.py:46-111` |
| `bank` (zstd -1 --long, stream sink) | `relay.py:114-150` |
| `restore` (wipe; **deletes soong bootstrap dirs** F2) | `relay.py:153-190` (purge at `174-178`) |
| `merge` (rsync --ignore-existing) | `relay.py:193-210` |
| `progress_from_log` (2 MiB tail, S4) | `relay.py:239-255` |
| constants: `VOLUME_SUBDIR`, `compress=zstd:1,noatime` | `storage.py:56-63` |
| `compute_cap_gb` / reserve | `storage.py:148-159` |
| `ensure_volume` (mkfs/mount/degrade/reuse) | `storage.py:212-285` |
| `_ensure_canonical_link` (path compat) | `storage.py:190-209` |
| `snapshot` (logical/physical/root) | `storage.py:355-373` |
| `trim` (fstrim→sparse holes) | `storage.py:376-389` |
| `emergency_root_purge` | `storage.py:431-472` |
| `mhash` (manifest fingerprint) | `syncer.py:79-91` |
| `snapshot_source` (zstd-3, immutable src) | `syncer.py:94-113` |
| `restore_source` (incoming-dir + move) | `syncer.py:116-142` |
| `sync_tree` (init/sync retries, git strip) | `syncer.py:158-236` (strip `223-229`) |
| `ensure_prebuilts` (webview/android.jar fetch, **soong purge dup** F2) | `syncer.py:284-371` (purge `367-371`) |
| `probe` / CPU_TABLE / min-score 100 / wait 240 | `mine.py:66-95`, `43-57` |
| `claim` (atomic gh release create) | `mine.py:101-115`, `store.py:191-215` |
| `gate` (strict, scoreboard, fallback) | `mine.py:121-198` (strict `136-138`, scoreboard `167-178`) |
| Gate 1–14 checks / `run` | `gate.py:152-589` / `592-611` |
| `generate_flash_script` / FLASH_TEMPLATE | `gate.py:617-676` |
| ReleaseStore: create×4 / upload×3 / download×3 | `store.py:78-97`, `105-118`, `132-169` |
| `claim` (release-create race) | `store.py:191-215` |
| `list_tags` (**limit 200** F8) | `store.py:176-181` |
| INDEX load/save (+bak, R2) / `target_update` | `store.py:398-457` |
| `gc_state` (keep 2, drops turbo) | `store.py:460-482` |
| `PART_BYTES` 1900 MiB / compress cmds | `chunker.py:25`, `38-51` |
| `stream_pack` (**serial sink** F6) | `chunker.py:123-172` (filter `151-159`) |
| `unpack_from_store` (**serial feeder** F6) | `chunker.py:284-371` (feeder `326-351`) |
| DEFAULT_PARTITIONS (A17 set) / NEVER_TURBO | `turbo.py:35-56` |
| `merge_turbo_states` (full unpack + rsync, F12) | `turbo.py:78-104` |
| workflow topology: triggers/candidates 20/crons | `forge.yml:39-75` |
| turbo job (max-parallel 6) | `forge.yml:192-221` |
| slot-1 job (matrix 20, mining gate) | `forge.yml:236-304` (matrix `246-250`, gate `258-270`) |
| postcheck (INDEX→phase, gc locks, red on fail) | `forge.yml:927-970` |
| verify / publish jobs (full restores, F4) | `forge.yml:975-1037` |
| conveyor (re-dispatch; **candidates '8'** F3) | `forge.yml:1049-1073` (line `1062`) |
| `reclaim_disk` (35–45 GiB runner fat) | `env.py:182-217` |
| `protect_runner_processes` (oom_score_adj −1000) | `env.py:220-234` |
| `ensure_swap` / `activate_swap_chunk` (p10) | `env.py:257-304` |
| LADDER_PATTERNS (symbols included — S8) | `env.py:362-366` |
| A17 capability row (75 GiB source, 32 h cold, 8 slices) | `configs/versions.yaml:166-187` |
| shiba profile (dexpreopt off, turbo 4, slices 8) | `configs/roms/lineage-a17-shiba.yaml:37-49` |
| relay tax math (O(1) = 20–30 min/slice) | `TECHNICAL.md §4.2` (lines 167-181) |
| mining economics (P(≥1 Zen5 in N)) | `mine.py:11-21`, `TECHNICAL.md §6.4` |
| alternatives-rejected ledger (goma, self-hosted, actions/cache) | `TECHNICAL.md §11` |
| stale telemetry (160-min constant, old stages) | `monitor.py:17`, `29-39` |
| OPTIMIZATION_PLAN S/R items + landing status | `docs/OPTIMIZATION_PLAN.md` (status cross-checked §2.4) |
