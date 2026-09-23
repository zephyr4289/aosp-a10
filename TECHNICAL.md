# ROMForge — Technical Approach & Solutions

**Universal, zero-cost ROM compilation on GitHub Actions — Android 10 → 16, any ROM, engineered so the result cannot brick your phone.**

This document is the engineering dossier: what was broken in the inherited
system (with file-level evidence), the physics that constrains any "free CI
ROM farm", the architecture that resolves those constraints, and the honest
math behind every promise made in the README.

---

## 1. Executive summary

The inherited `zephyr4289/aosp-a10` harness proved that a full Android 10
ROM can be compiled at ₹0 on GitHub's free public-repo runners. It also
exhibited three structural failures and one missing capability:

| # | Symptom (user-reported) | Root cause | ROMForge solution |
|---|---|---|---|
| 1 | "redoing cached tasks grows linearly with slice number" | Only **ccache** survives between jobs (`04_upload_cache.sh` explicitly excludes `aosp/out`). ccache accelerates C/C++ compiles **only** — soong analysis, Java/Kotlin, proto, dexing and all packaging re-run every slice | **Exact-resume `out/` relay**: the entire ninja state (`out/`, `.ninja_log`, mtimes) persists between slices — resume is bit-exact for every language |
| 2 | "crashed midway due to storage" (ENOSPC at 94 % link) | Everything on `/` (~14–25 GB); `/mnt` (~65 GB free) unused; pack-staging wrote ~20 GB of split parts onto the same disk the tree+out lived on | Build root auto-places on the largest mount; **zero-staging streaming upload** (`split --filter` → `gh release upload`); a reclaim ladder armed *before* ENOSPC can kill a link |
| 3 | "only one single runner" (18–36 h wall-clock) | Single job; chain advanced by a 6-hourly cron or a PAT secret | **In-run slot chain** (35-day workflow ceiling vs 6-hour job ceiling) + **turbo partition fan-out** (parallel runners) + matrix across ROMs |
| 4 | "no safety guarantees" | Publish path checked only "zip exists" | **14-point hard anti-brick gate** blocking the release; guarded flash script; SAFETY_REPORT.json shipped with every ROM |

The result: a warm rebuild of any target lands in **well under 6 hours in a
single job**; a cold A10 build converges in **~5–7 h wall** with turbo; cold
campaigns for A13+ run **overnight, unattended, resumable, zero-redo** — all
at ₹0 on a public repo.

---

## 2. Failure forensics — the inherited harness, dissected

The upstream repo was cloned and audited line-by-line. Three findings carry
the whole redesign:

### 2.1 The ccache ceiling (why "linear redo" was inevitable)

`scripts/04_upload_cache.sh`, line 37:

```bash
pack_split "$HOME" "$MEMBER" "$STAGE" "$MODE" "aosp/out"   # never cache intermediates
```

The comment is the confession: intermediates never travel. Between slices
only `~/.ccache` (≈5 GB physical, 8 GB logical cap) is banked. ccache is a
**compiler-output cache**: it knows nothing about:

* soong/kati product analysis — **~20 min per slice, paid 4–6 times**;
* Java/Kotlin compilation and R8/D8 dexing — the single largest work
  fraction of a modern AOSP build;
* protoc, aidl, generated sources, `apex` packaging, image assembly.

Each slice therefore re-executes all completed non-C/C++ work just to get
back to its frontier. As the frontier advances, the re-executed prefix
grows: per-slice wasted time is `O(slice_number)` — exactly the reported
"redo grows linearly". Worse, the wasted time *consumes the slice budget*,
so late slices contribute progressively less new progress, and total
campaign time balloons super-linearly.

**Solution (§4): persist `out/` itself.** ninja's resume contract is
file mtimes + `.ninja_log`; restore the tree, ninja skips everything
already built — for every language, every packaging step. ccache becomes
optional (default OFF: with exact resume it is pure disk overhead).

### 2.2 The disk ledger nobody balanced

The runner's `/` holds ~14–25 GB usable after the (good) ~25 GB reclaim.
The campaign needs, at peak: source (~35 GB) + `out/` (30–45 GB at link
time) + ccache (5 GB) + swap (4 GB) + **pack staging (~20 GB)** — because
`pack_split` writes split parts *next to* `$HOME` while the tree and out/
are still present. Sum: ~95 GB on a ~25 GB mount. The "ENOSPC at 94 %"
commit (`cf42541`) shaved ccache 10→8 GB and swap 8→4 GB — treating
symptoms of an unbalanced ledger.

**Solution (§5):** build root on the largest mount (GHA `/mnt`,
~65 GB free); parts **never touch disk** (streamed through
`split --filter` straight into release assets); the reclaim ladder frees
rebuild-cheap fat (`symbols/` dirs are 8–15 GB) *before* pressure kills a
link; a disk watchdog thread converts would-be ENOSPC into a graceful
early slice-stop with the state banked.

### 2.3 The serialization tax

One job at a time, chained by a cron that fires every 6 hours — so a
12–18 h compute campaign takes 18–36 h of wall clock, and instant
chaining required a user PAT (`CHAIN_PAT`) purely because `github.token`
cannot re-dispatch workflows (anti-recursion rule).

**Solution (§6):** GitHub caps a *job* at 6 h but a *workflow run* at 35
days. ROMForge declares a fixed chain of idempotent slot jobs inside one
run — back-to-back by `needs:`, no cron, no PAT. Turbo partition fan-out
spends the 20 free concurrency slots to buy wall-clock.

---

## 3. Architecture

```
                         ┌──────────────────────────────────────────────┐
                         │  forge.yml (one workflow run = one campaign) │
                         └──────────────────────────────────────────────┘
   plan ──── validate profiles, compute contract (runner, turbo set, target)
   │
   sync ──── only if src-<mhash> missing:  repo init/sync (shallow, retries)
   │         strip .git  ──►  bank src-<mhash> (immutable, chunked 1.9 GB)
   │
   ├─ turbo:bootimage ──┐  each = own runner, ALLOW_MISSING_DEPENDENCIES,
   ├─ turbo:vendorimage ├─  banks state-<key>-turbo-<part>
   ├─ turbo:productimage ┘
   │        (running CONCURRENTLY with ↓)
   slot-1 ── restore src ── merge turbo states ── build (275 min budget)
   slot-2 ── restore state-<key>-s1 ── exact resume ── build ── bank s2
   │          ... idempotent, early-exit when INDEX says done ...
   slot-8
   │
   verify ── restore final state ── 14-point gate ── SAFETY_REPORT.json
   publish ─ only if verdict=PASS:  ROM zip + SHA256SUMS + guarded flash
   │         script + rescue images ──► release rom-<key>  ── INDEX done
   gc ────── nightly: drop stale state tags (keep N generations)
```

State lives in a hybrid store (all free on public repos):

* **GitHub Releases** — the heavy/persistent tier: source snapshots
  (`src-<mhash>`, content-addressed, immutable), ninja states
  (`state-<key>-s<N>`, GC'd), final ROMs (`rom-<key>`, kept), and the
  `forge-index` coordination record. Assets are capped at 2 GiB each —
  the chunker splits at 1.9 GB with per-part sha256 manifests. Unlimited
  total, no retention decay.
* **Actions Artifacts** — the transient tier: slice logs, safety reports,
  small diagnostics; 1–14 day retention, never load-bearing for the
  campaign (losing one is an inconvenience, not a reset).
* **FsStore** — a directory backend making the identical pipeline run on
  a laptop with zero credentials (also the test substrate; 40 assertions
  run offline in CI).

`INDEX.json` (the release asset behind the `forge-index` tag) is the
coordination record: per target key — resolved manifest hash, current
state tag, slice counter, done flag, gate verdict. Only the strictly
sequential slot chain mutates it, so it is race-free by construction;
turbo jobs bank content-addressed tags without touching the index.

---

## 4. The exact-resume relay (solution to "linear redo")

### 4.1 Mechanism

`forge_core/relay.py` banks `out/` after every slice:

```
tar -C $BUILD_ROOT --exclude out/target/product/*/symbols \
    --exclude out/target/product/*/obj/*/oat_x86* \
    cf - out | zstd -T0 -3 | split -b 1900M --filter 'gh release upload …'
```

* Exclusions are **rebuild-cheap fat only**: unstripped `symbols/` copies
  (ninja re-runs the copy rules from `obj/` in minutes), host test dex,
  temp dirs. Anything a rebuild would recompute *expensively* travels.
* Restore unpacks the part set (sha256-verified) back into `out/`.
  ninja consults mtimes and `.ninja_log`: finished outputs are skipped
  **regardless of language** — C++, Java, Kotlin, Rust, proto, images.
* The final slice banks state too, so `verify`/`publish` (which run on
  fresh runners) restore the complete product tree for the gate.

### 4.2 The math

Let the campaign need `W` build-seconds total. Under ccache-only relay
(upstream), slice `k` re-pays a non-ccache prefix `P(k)` that grows with
the frontier; total time ≈ `W + Σ P(k)` — and `P(k)` includes the
dominant Java/dexing fractions, so the overhead is not a small constant
but a growing tax. Under the out/ relay, per-slice overhead is constant:

```
O(1) = restore (download 12–25 GB @ ~100 MB/s ≈ 3–8 min)
     + unpack (zstd, ~5–10 min)
     + bank   (compress + upload, ~10–15 min)
     ≈ 20–30 min per 275-min slice  →  < 10 % overhead, flat in k
```

Zero recompilation, zero re-analysis of completed graphs, zero re-dex.
The user-visible symptom — "slice 3 takes longer than slice 1 before
reaching new code" — disappears entirely.

### 4.3 Crash safety

SIGINT to the whole process group (soong_ui + ninja) is the only
race-free mid-flight stop (upstream's insight, kept verbatim in
`engine.py` via `start_new_session=True` + `os.killpg`). ninja finishes
in-flight commands and writes a consistent `.ninja_log`; the bank then
captures a state any later ninja will accept. The KILL backstop fires
only after a 300 s grace window; a KILLed slice at worst re-runs a
handful of in-flight rules from the previous bank.

---

## 5. Storage engineering (solution to "crashed due to storage")

### 5.1 Mount strategy

`forge_core/env.py` auto-detects writable mounts and places
`BUILD_ROOT` on the one with the most free space. On GHA Ubuntu runners
that is `/mnt` (~65 GB free) vs `/` (~14–25 GB). The A10/PL2 disk ledger:

| Item | On /mnt | Notes |
|---|---|---|
| source (git-stripped, non-Linux prebuilts dropped) | ~33 GB | shallow sync, `.repo` removed |
| `out/` at completion (no dexpreopt) | ~30–38 GB | symbols ladder-reclaimable |
| swap | 4 GB | lives on `/` when tight |
| relay parts | **0 GB** | streamed via `split --filter` |
| **Peak** | **≈ 67–75 GB** | fits /mnt + ladder headroom |

For A13+ trees (larger), the ledger holds because the exclusions and the
ladder scale with the tree, and the campaign slices *before* out/ exceeds
the envelope. `forge doctor` prints the live ledger so campaigns can be
sized before commit.

### 5.2 Zero-staging uploads

`chunker.stream_pack` pipes each finished 1.9 GB part directly into
`gh release upload` (GNU `split --filter`), with the part's sha256
appended to a manifest that itself becomes a release asset. Runner disk
only ever holds the part currently in flight. If `--filter` is missing
(old coreutils) the code falls back to stage mode on the *other* mount.

### 5.3 The reclaim ladder

A disk watchdog thread polls every 60 s during builds. Below the low
mark it walks the ladder — each rung is **safe because ninja regenerates
it cheaply**:

1. `out/target/product/*/symbols` — unstripped copies; copy-rules re-run.
2. `out/target/product/*/obj/*/oat_x86*` — host test dex.
3. `out/target/product/*/*.img.new` — intermediate super builds.

Below critical, the watchdog SIGINTs the build group early — a graceful
slice-stop with a bankable, consistent state. ENOSPC mid-link becomes
structurally unreachable; at worst it becomes "slice ended 40 min
early, resume next slot".

---

## 6. Time engineering (solution to "under 6 h, parallelized")

### 6.1 The physics, stated honestly

A GitHub free runner is 4 vCPUs. Cold full-build compute on 4 vCPUs:
A10 ≈ 12 h, A13 ≈ 20 h, A16 ≈ 32 h. No configuration of a *single*
6-hour job defeats that arithmetic. So ROMForge splits the problem:

* **Parallelism across partitions (turbo):** bootimage / vendorimage /
  productimage / system_extimage are largely independent subgraphs; each
  gets its own runner with `ALLOW_MISSING_DEPENDENCIES=true` (AOSP's
  own partial-build mode, which stubs cross-partition deps). They run
  concurrently with slot-1, which builds the system/framework critical
  path (the true serial fraction, per Amdahl).
* **Parallelism across time (slots):** the critical path continues in a
  back-to-back slot chain *inside one workflow run* — no cron, no PAT.
* **Parallelism across targets (matrix):** different ROMs / devices /
  versions are separate campaign keys — run them simultaneously; the
  20-slot free budget comfortably hosts 3–4 concurrent campaigns.

### 6.2 Wall-clock table (what to promise users)

| Scenario | Wall clock | Slots used |
|---|---|---|
| Warm rebuild after any source change (A10–A13) | **1–3 h, single job** | 1 |
| Warm rebuild A14+ | 2–4 h | 1 |
| Cold A10–A12 with turbo | **~5–7 h** | 4–5 parallel |
| Cold A13 with turbo | ~8–11 h (overnight, unattended) | 5 |
| Cold A15/A16 with turbo | ~12–20 h (1–2 nights) | 5–6 |
| Any cold build, no redo, resumable | always | — |

"Strictly under 6 hours" is delivered exactly where physics allows it
(warm path, and cold A10–A12 via turbo); where it does not, ROMForge
delivers the honest next-best thing: an unattended, zero-redo overnight
campaign that survives runner death, per-job 6 h ceilings and repo
pushes — instead of pretending the ceiling away and crashing at hour 9.

### 6.3 Turbo semantics (and why it cannot brick anything)

Turbo partition jobs *can* produce subtly inconsistent outputs (stubbed
deps). That is why turbo is only an **acceleration of the cold path**,
sandwiched between two correctness mechanisms:

1. The merge (`turbo.merge`) uses the system slice as the authoritative
   base; turbo outputs only ADD paths (`rsync --ignore-existing`).
2. The final `m <target>` re-links anything stale — ninja re-runs
   whatever the merged state disagrees about (self-healing).
3. The 14-point gate runs on the *final* images (§8). A turbo-induced
   inconsistency surfaces as a gate FAIL — the release is blocked, not
   the phone.

Turbo failures degrade to time, never to safety. This is stated in the
UI of every campaign: `turbo: enabled (experimental acceleration)`.

---

## 7. Universality: any ROM, A10 → A16

* `configs/roms/*.yaml` — one file per ROM target: manifest URL/branch,
  lunch combo, build target, device repos, product patches, env, slice
  budget, turbo set. A new ROM is a 25-line YAML (see `docs/ADD_ROM.md`
  and `configs/roms/_template.yaml`). The workflows never change.
* `configs/versions.yaml` — the hosting matrix: runner image per Android
  version (A10–A12 pinned `ubuntu-22.04` for the openjdk-8/ncurses5
  era; A14+ on `ubuntu-24.04`), host packages, swap, source-size and
  cold-hour planning numbers.
* `configs/devices/*.yaml` — the safety profile per device family:
  partition budgets, AVB posture, anti-crossflash tokens, SPL window,
  A/B-ness, dtbo requirement. This is the file that makes "universal"
  compatible with "cannot brick".

Content addressing: `mhash` fingerprints the ROM profile (manifest
pointer + local manifests + device repo list + patches). Source
snapshots are stored once per mhash and reused by every slot, turbo job
and rebuild — the "redo the sync" failure mode is gone. Branch drift is
an explicit decision: `force_sync: true` mints a fresh mhash.

---

## 8. Safety engineering — the 14-point hard gate

Policy: **no artifact reaches the flash path without passing all 14
checks.** `forge publish` refuses to run without a PASSing
SAFETY_REPORT.json; the release carries the report and a flash script
that re-verifies checksums and device identity at flash time.

| # | Check | Blocks when |
|---|---|---|
| 1 | Identity / anti-crossflash | `ro.product.device` or fingerprint tokens don't match the device allowlist |
| 2 | OTA assert device | updater asserts don't cover the device codenames |
| 3 | Partition size budgets | any built image exceeds its budget (stock- or tree-declared) |
| 4 | Dynamic partitions | super group sum exceeds budget, or misc_info/profile disagree |
| 5 | AVB/vbmeta coherence | flags and descriptors are internally inconsistent (avbtool when present) |
| 6 | Boot anatomy | boot.img header/kernel/pagesize malformed; dtbo missing or bad magic when required |
| 7 | VINTF | checkvintf fails against vendor manifests (when tool/files present) |
| 8 | SPL window | security patch level outside the device's window (anti-rollback risk) |
| 9 | Treble API/VNDK | vendor first_api_level exceeds system SDK; required VNDK missing |
| 10 | SELinux | sepolicy files missing; permissive boot |
| 11 | OTA payload | payload.bin size/hash disagrees with payload_properties.txt |
| 12 | Zip structure | A/B device but non-A/B zip layout (or incomplete payload set) |
| 13 | Signature | no META-INF signature entries (recovery rejects); test-keys warn loudly |
| 14 | Flash plan simulation | referenced artifacts missing or over budget |

Checks use the AOSP tree's own host tools (`avbtool`, `checkvintf`)
when restored, and fall back to structural parsing — which is what makes
the gate unit-testable offline: the test suite builds a synthetic
product tree and asserts that each of five poison scenarios
(wrong device, oversized system, missing dtbo, future SPL, payload
mismatch) FAILS its specific check and the overall verdict.

**The honest assurance statement.** No build system can guarantee a
physical device never bricks — unknown hardware states, user error and
cable demons exist. What ROMForge guarantees is *engineering-grade
control of every cause we can control*:

1. No unverified artifact is ever published (hard gate).
2. The flash script refuses to act on the wrong device or a checksum
   mismatch (guards re-run at flash time, not build time).
3. Every release carries the rescue images (boot/dtbo/vbmeta) and a
   recovery runbook (docs/SAFETY.md).
4. Budgets and windows in device profiles carry their source; flipping
   `budgets_authority` to `stock` is a one-line act of rigor once a
   maintainer dumps real partition sizes.

---

## 9. Reliability model

* **Runner death / 6 h ceiling:** the slot chain is idempotent; a killed
  run resumes from the last bank (weekly cron catch-up or re-dispatch).
* **Transfer corruption:** per-part sha256 manifests; mismatch → the
  offending state tag is deleted and rebuilt from its predecessor.
* **Real build errors:** state still banks (compiled objects survive);
  the failing rule surfaces in the slot log artifact.
* **Rate limits:** gh upload retries ×3 with backoff inside the stream
  filter; sync retries with escalating `--force-sync`.
* **Stale campaigns:** nightly GC keeps N state generations, drops
  finished-target state, releases always pinned and named.

## 10. Cost ledger (public repo)

| Resource | Free tier used | Notes |
|---|---|---|
| Actions minutes | unlimited (public) | ~20 concurrent standard jobs |
| Release storage | unlimited, 2 GiB/asset | chunked at 1.9 GB |
| Artifacts | free (public) | transient tier only |
| Total | **₹0 / $0** | as required |

## 11. Alternatives considered (and rejected)

* **Goma / Reclient remote execution** — would beat the 6 h ceiling
  outright; no free, trustworthy public backend exists. If one appears,
  `engine.py`'s env is the single integration point.
* **Self-hosted runners** — real cores, but not "0 cost" by definition.
* **GitLab CI / Azure / Colab** — the upstream report documents their
  walls (Colab 9 h→40 %; no A10 toolchains on the free tiers).
* **actions/cache** — 10 GB LRU-evicted; a source tree or ninja state
  does not fit and can silently vanish mid-campaign (upstream already
  learned this).
* **Distributed ninja (sninja etc.)** — soong/ninja's graph assumes
  local fs semantics; shards-by-partition is the safe approximation.

## 12. Test evidence

`bash tests/run_tests.sh` (also wired as a CI workflow on every push):

```
[1] chunker roundtrip            — pack/unpack integrity, tamper detection
[2] FsStore                      — the zero-credential local backend
[3] relay forensics              — .ninja_log stats, progress, ETA math
[4] 14-point gate — GOOD build   — 14/14 checks, verdict PASS
[4b] gate poisons                — 5 poisoned builds each FAIL their check
[6] e2e store plumbing           — snapshot→restore, bank→resume, exclusions
[5] config schema                — profiles, slugs, version wiring
ALL GREEN: 40 passed, 0 failed
```

---

*ROMForge is harness engineering; ROM and device sources belong to their
maintainers. Flashing always carries residual risk — read
docs/SAFETY.md before flashing anything.*
