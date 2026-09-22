# Technical Report — Building Android 10 (QASSA 2.4) for Nokia 6.1 at ₹0
### Chained-build architecture on free GitHub-hosted runners

**Prepared for:** the PL2 build campaign (repo `aosp-a10`)
**Status:** implementation complete — this report documents the engineering
**Total infrastructure cost:** ₹0

---

## 1. Executive summary

Compiling an Android 10 custom ROM requires roughly 15–20 CPU-hours on the
free GitHub-hosted runner (4 vCPU / 16 GB RAM), but GitHub kills any single
job at the 6-hour wall. A naive "run `mka bacon` in Actions" therefore dies
at ~30–40 % and loses all progress — the exact failure mode observed when
this project was attempted on Google Colab (9 h → 40 %, then storage and
session limits ended it).

The harness in this repository solves the problem with a **chained-build
state machine**: the build is cut into 160-minute *slices*, and progress is
carried between slices by a **ccache banked in GitHub Releases**. Because
ccache is content-addressed, a killed slice loses at most the file being
compiled; the next slice restores the cache and continues at near-zero
marginal compile cost. Source, ccache and the final ROM all live in
dedicated Releases (free, effectively uncapped on public repos), not in
`actions/cache` (capped at 10 GB/repo with LRU eviction — unusable here).

**Expected campaign:** 1 bootstrap run (sync + first cold slice) plus 2–4
chained slices; 18–36 h wall-clock on the built-in 6-hourly schedule, or
12–18 h with the optional `CHAIN_PAT` instant-chaining secret. The chain
auto-terminates by publishing the ROM to the `rom` release and disabling the
workflow.

---

## 2. Failure forensics — why previous attempts died

### 2.1 Google Colab (the 9 h → 40 % run)

| Constraint | Value | Consequence |
|---|---|---|
| vCPU (free tier) | ~2 | ~22 h extrapolated for the full build |
| Session wall | 12 h (worst case) | Build cannot finish in one session |
| Disk | ~70–100 GB ephemeral, no persistence | Synced tree + `out/` + ccache all die with the VM |
| Checkpointing | none | Every restart begins from 0 % — the process **never converges** |

The notebook itself was correct — manifest, device repos, product makefile
and lunch target all validated. The failure was **physics, not engineering**:
without persistent state, a build longer than the session wall cannot
complete, regardless of how many retries are spent.

### 2.2 ServerHive (and why "no Android 10 / non-RBE" is structural)

Non-RBE (non-remote-execution) Android 10 builds pin 100 % of a server's
CPU for 15–20 h with heavy sequential disk I/O. On shared commercial
infrastructure that is a low-yield, high-abuse-potential workload, so
resellers simply refuse it (or price it accordingly). It is not a
configuration issue on our side — it must be routed around, not fixed.

### 2.3 Naive GitHub Actions

Three traps kill the obvious approach; all three are addressed by this
harness:

1. **The 6-hour job wall** — any single-job build dies at ~35–45 %.
   *Countermeasure:* slicing + ccache chaining (this report).
2. **Disk** — a standard runner offers ~84 GB total with only ~14 GB
   guaranteed free; a full AOSP 10 tree + `out/` + ccache ≈ 60–75 GB.
   *Countermeasure:* the storage ledger in §5.
3. **Private repos meter minutes** (2,000/month, 2× multiplier on Linux) —
   the campaign needs ~20+ h. *Countermeasure:* public repo, where Actions
   minutes are unlimited and free, and Release storage is free.

---

## 3. Constraint envelope (verified)

| Resource | Standard Linux runner (public repo) | Note |
|---|---|---|
| vCPU | 4 | soong_ui auto-tunes to ~6 ninja jobs |
| RAM | 16 GB | at AOSP 10's documented minimum; 8 GB swap added |
| Job wall | 360 min hard | harness uses `timeout-minutes: 340` for save-buffer |
| Disk | ~84 GB SSD, ~14 GB free pre-cleanup | ~60–70 GB free after cleanup (§5) |
| Minutes | unlimited (public repo) | the entire economic basis |
| Release asset | 2 GiB max per asset | archives split at 1.9 GB |
| `actions/cache` | 10 GB/repo, LRU eviction | deliberately NOT used |
| Network | GitHub backbone; GitHub→GitHub syncs are fast | ~30–60 min shallow sync |

**Runner image:** pinned `ubuntu-22.04`. Android 10's build stack targets
Ubuntu 18.04/20.04; jammy is the closest image GitHub still hosts that ships
`openjdk-8` and Python 3.10 (24.04 drops JDK 8 and Python < 3.12 breaks the
Q-era tooling). Two 20.04-era gaps are patched by `00_prepare_runner.sh`:
`libncurses.so.5`/`libtinfo.so.5` compat symlinks → v6, and
`python-is-python3`/`python3-distutils`.

---

## 4. System architecture

### 4.1 The state machine

```
                 ┌──────────────────────────────────────────────────┐
                 │                workflow_dispatch                  │
                 │      (bacon | bootimage | systemimage | nothing)  │
                 └───────────────┬──────────────────────────────────┘
                                 ▼
                      ┌────────────────────┐   exists    ┌─────────────┐
                      │  gate: release     ├────────────►│ no-op exit  │
                      │  'rom' present?    │             │ (30 s)      │
                      └─────────┬──────────┘             └─────────────┘
                                │ no
                                ▼
                      ┌────────────────────┐
                      │ 00 prepare runner  │  ~25 GB reclaimed, swap, JDK8
                      └─────────┬──────────┘
                                ▼
                      ┌────────────────────┐  cache hit   ┌──────────────┐
                      │ 05 restore caches  ├──────────────► tar+zstd from│
                      │  (source, ccache)  │              │ Releases     │
                      └─────────┬──────────┘              └──────────────┘
                                │ cache miss / force_sync
                                ▼
                      ┌────────────────────┐
                      │ 01 sync source     │  repo sync + device clones
                      └─────────┬──────────┘  + .git strip
                                ▼
                      ┌────────────────────┐
                      │ 02 patches + lunch │  fail-fast validation
                      └─────────┬──────────┘
                                ▼
                ┌────────────────────────────────┐
                │ 03 build slice                 │
                │  setsid soong_ui --make-mode   │
                │  watchdog: SIGINT group @160m  │
                └───────┬───────────────┬────────┘
              rc=0      │               │ budget spent (elapsed ≥ budget−5 s)
                        ▼               ▼
             ┌────────────────┐   ┌─────────────────────────────┐
             │ 06 publish ROM │   │ 04 bank ccache (+src on     │
             │ release 'rom'  │   │    bootstrap) to Releases   │
             │ + artifact     │   └──────────────┬──────────────┘
             │ auto-disable   │                  ▼
             └────────────────┘   CHAIN_PAT? ── instant re-dispatch
                                        │ no
                                        ▼
                          cron every 6 h resumes chain
```

### 4.2 Design decisions and their rationale

**Why ccache (not `out/` restore) as the resume substrate.**
Two candidates can carry progress between jobs: the build output directory
(`out/`, ~25–30 GB, resumes ninja exactly) and ccache (~10 GB logical,
recompiles from cache hits). `out/` restore converges in fewer slices but is
fragile: soong regeneration over a restored `out/` frequently triggers
mystery no-op rebuilds, absolute-path assumptions, and ninja state
mismatches. ccache chaining is the community-proven pattern (crDroid CI,
LineageOS forks on Actions) and is robust to tree changes because it is
keyed by preprocessed-content hash, not paths. Cost: one extra slice, on
average — a good trade for determinism.

**Why Releases and not `actions/cache`.**
`actions/cache` caps at 10 GB per repository with best-effort LRU eviction.
The source snapshot alone is ~12–16 GB compressed; a hot ccache adds ~6–10 GB.
A silent eviction mid-campaign would revert the chain to a cold build with
no error. Release assets on public repos are free, durable, first-class
objects with a 2 GiB per-asset cap — hence 1.9 GB `split` parts, sha256
manifests, and atomic delete-and-recreate semantics (`release_reset`) so a
changing part-count can never leave stale fragments behind.

**Why a process-group watchdog instead of `timeout`.**
Three subtle failure modes make `timeout 9600 m bacon` incorrect:
(1) `m` is a *shell function* from `envsetup.sh` — `timeout` can only exec
binaries, so it would fail instantly with "command not found";
(2) invoking a wrapper script instead means `timeout` signals only the
wrapper, and non-interactive bash defers SIGINT while a foreground child
runs — the soong/ninja process group would be **orphaned**, keep compiling
in the background, and we would tar a ccache that is still being written;
(3) an orphaned ninja also keeps burning a second runner concurrently.
The harness therefore launches soong_ui under `setsid` (own process group)
and runs a watchdog that `kill -INT -- -$pid` **the whole group** at budget
expiry — soong_ui traps SIGINT and stops ninja gracefully — with a SIGKILL
backstop five minutes later. Slice classification is clock-based:
`elapsed ≥ budget − 5 s` ⇒ sliced; any earlier non-zero exit ⇒ real error.

**Why the gate + auto-disable.**
Once `rom` exists, every future scheduled tick exits in ~30 s via the gate.
As belt-and-braces the workflow then disables itself (`actions: write`
permission), which also sidesteps GitHub's 60-day schedule-auto-disable
rule. Rebuilding is a two-click operation (re-enable + delete `rom`).

**Why `concurrency: qassa-pl2-build` with `cancel-in-progress: false`.**
A cron tick that lands while a manual run is still going must *queue*, never
*cancel* — cancelling mid-slice would waste the entire slice's compile work.
The group serializes all triggers against each other.

**Why direct device clones (default) over a local manifest.**
The proven Colab flow landed the four PL2 repos by wiping the paths and
shallow-cloning them after `repo sync`. A local manifest `add-project`
aborts the whole sync if the ROM manifest already defines the same path —
a failure mode entirely avoided by direct mode. The roomservice XML ships
anyway (`DEVICE_CLONE_MODE=manifest`) for maintainers who prefer it, with
automatic rescue-clone fallback for empty paths.

### 4.3 The convergence math

Let `T` ≈ 18 h be the cold-compile cost of the tree on 4 vCPUs and `B` = 160
min the per-slice build budget. With ccache hit rate `h → 1` on repeated
slices, effective per-slice new work approaches `B·(1)` for cold slices and
drops to link + package + cache-miss work (~2.5–3.5 h) once the cache is
warm. Expected profile:

| Slice | Warm-up state | New work completed | Outcome |
|---|---|---|---|
| 1 (bootstrap) | cold | ~2.7 h compile banked | sliced |
| 2 | ~60–75 % hits | link-heavy, banks rest | sliced (likely) |
| 3 | ~90 %+ hits | relink + package + bacon | **ROM** |
| 4 (margin) | — | — | rarely needed |

Sensitivity: if `SYNC_JOBS` must drop to 4 due to upstream 429s, bootstrap
grows ~20 min — absorbed by the 340-vs-360-minute buffer. If disk pressure
forces `CCACHE_SIZE=8G`, expect one additional slice (lower hit rate), not
failure.

---

## 5. Storage engineering — the ledger

The runner's ~84 GB SSD is the scarcest resource. Every design choice pays
into this ledger:

| # | Item | Raw | After measure | Notes |
|---|---|---|---|---|
| — | Disk total | ~84 GB | — | single volume |
| 1 | Runner image baseline usage | ~34–40 GB | ~14–16 GB | delete Android SDK (~9–12 GB), dotnet, ghc, boost, julia, graalvm, swift, azure CLI; `docker system prune -af` (~8–11 GB); unused Temurin JVMs |
| 2 | Swap file | +8 GB | +8 GB | created only if > 33 GB free post-cleanup; absorbs soong-gen + parallel-lld spikes |
| 3 | AOSP 10 tree, shallow (`-c --depth=1`) | ~40–45 GB | — | includes `.repo` git objects |
| 4 | `.git`/`.repo` strip post-sync | −(4–8 GB) | ~26–30 GB | we never incrementally sync; `force_sync` re-inits from zero by design |
| 5 | Darwin/Windows prebuilt GCC removal | −1.5 GB | ~25–28 GB | host is Linux-only |
| 6 | ccache, 10 GB logical + zstd-level compression | ~10 GB | ~6–7 GB physical | `ccache -o compression=true`; LRU-capped by `-M` |
| 7 | `out/` peak (WITH_DEXPREOPT=false) | — | ~22–28 GB | the largest remaining variable |
| — | **Peak total** | | **~61–71 GB vs ~69–71 GB free** | knife-edge; guarded, not hoped for |

**Guards, not hope:** `check_disk` runs at every stage boundary
(`warn ≤ 10 GB`, `abort ≤ 4 GB` — abort *before* ENOSPC corrupts state that
would poison the caches). The pressure valves, in order: lower
`CCACHE_SIZE` to 8 GB → drop swap to 4 GB → reduce `SYNC_JOBS` (smaller
transient `.repo`). The restore path unpacks into a temp directory and
`mv`s atomically, so a corrupted download can never half-overwrite a good
tree, and downloaded split-parts are deleted before the build starts.

Why deleting `/opt/hostedtoolcache` is safe here: the runner agent executes
JS actions via its **bundled `externals/node20`**, not the toolcache —
verified against how `actions/checkout@v4` and `upload-artifact@v4` boot.
The toolcache only serves setup-* actions, which this workflow never uses.

---

## 6. Time engineering — per-slice budget

`timeout-minutes: 340` against the 360 wall leaves a **20-minute save
buffer**, and the build watchdog fires at `BUILD_SLICE_SECONDS=9600`
(160 min). Worst-case slice profile:

| Phase | Bootstrap run | Chained run |
|---|---|---|
| checkout + gate | 1 min | 1 min |
| 00 prepare (apt, cleanup, swap) | 8 min | 8 min |
| 05 restore | — | ≤ 45 min (16 GB src + 8 GB ccache over backbone) |
| 01 sync | ≤ 90 min (3-retry worst case) | — |
| 02 patches + lunch | 8 min | 8 min |
| 03 build slice | 160 min | 160 min |
| 04 bank caches | ≤ 35 min (src 12–16 GB + ccache) | ≤ 20 min (ccache only) |
| **Total** | **≤ 302 min / 340** | **≤ 242 min / 340** |

---

## 7. Compatibility engineering (Ubuntu 22.04 hosting an Android-10 build)

| Gap | Fix (in `00_prepare_runner.sh`) |
|---|---|
| JDK: AOSP 10 needs OpenJDK 8 | `apt install openjdk-8-jdk` (jammy still ships it; noble does not — hence the image pin) |
| `libncurses.so.5` / `libtinfo.so.5` host-tool sonames | guarded compat symlinks to the v6 libraries |
| `python` → python3 | `python-is-python3`, `python3-distutils` |
| Locale crashes in Q-era perl/python scripts | `export LC_ALL=C` in the build env |
| `repo` launcher | fetched to `~/bin`, `PATH`-injected |
| git identity required by `repo` | global user.name/email set in 00 |

**Container alternative (considered, rejected as default):** running the
job in an `ubuntu:20.04` container would give the exact distro the ROM was
designed against — but container-layer overhead hides ~20–30 GB of host
image from the cleanup step, and the disk ledger (§5) has no slack to give.
If GitHub retires the `ubuntu-22.04` image, the container route
(`container: ubuntu:20.04` + the same scripts + apt-installed `gh`) is the
documented fallback; the harness keeps all logic in scripts, not workflow
steps, precisely so the execution substrate stays swappable.

---

## 8. Reliability model

| Failure | Detection | Recovery |
|---|---|---|
| Sync network flake / 429 | non-zero `repo sync` | ×3 retries with backoff; completed projects persist across attempts |
| Corrupted release transfer | `sha256sum -c` on reassembled stream | delete the offending cache release, next run rebuilds it |
| Stale split-parts after cache size change | atomic `release_reset` (delete + recreate) | impossible by construction |
| Slice wall-clock overrun | watchdog SIGINT + 5-min SIGKILL backstop | next slice continues from ccache |
| ENOSPC mid-build | `check_disk` gates at stage boundaries | abort *before* corruption; lower `CCACHE_SIZE`, re-run |
| Cron/manual overlap | `concurrency` group (queue, never cancel) | serialized by construction |
| Runaway schedule after success | gate (30 s no-op) + auto-disable | rebuild = re-enable + delete `rom` |
| Real compile error | clock-based slice classifier (early non-zero ≠ sliced) | job goes red, ccache still banked; fix tree → `force_sync` |
| ccache format break on runner image change | cache misses (never wrong builds — content-hash keyed) | `fresh_ccache = true` |

**Safety invariant:** every artifact that crosses the job boundary
(source tarball, ccache, ROM zip) is either content-hash verified (sha256,
ccache's own hashing) or rebuilt from a verified substrate. No state that
cannot be explained is ever trusted.

---

## 9. Security & compliance

- **No secrets are required** for the default (schedule-chained) mode; the
  workflow's `github.token` is scoped to `contents: write` + `actions: write`
  and lives only for the job's duration.
- `CHAIN_PAT` (optional) needs only the classic `workflow` scope; it is used
  solely to re-dispatch the next slice. Fine-grained PATs do not currently
  cover workflow dispatch for this use-case.
- **Proprietary blobs are never committed.** `vendor/nokia` is fetched at
  build time from Zoro-15's public repository — the same source the Colab
  run used. If it ever goes private, the documented pattern is a PAT-guarded
  clone inside `01_sync_source.sh`, never a commit into this repo.
- Public repo = public build logs. The logs contain tree URLs and build
  output only — no credentials are ever echoed (secrets are masked by
  Actions regardless).

---

## 10. Runbook (condensed)

1. Create **public** repo `aosp-a10`, push this tree. ✔
2. Settings → Actions → General → Workflow permissions → **Read and write**.
3. Optional smoke: dispatch `bootimage` (~1–1.5 h) — proves lunch + kernel
   link before committing to the full chain.
4. Dispatch `bacon`. Wait. The schedule does the rest.
5. ROM lands in release `rom`; workflow disables itself. Verify `SHA256SUMS`,
   sideload from recovery (`adb sideload QASSA-*.zip`), first boot 5–10 min.

**Monitoring cadence:** check the Actions tab after the first run (bootstrap
should end "sliced" with a populated `source-cache`), then roughly every
6–12 h. The ccache hit-rate line in each job summary is the health metric.

---

## 11. Alternatives considered

| Route | Verdict | Reason |
|---|---|---|
| **Crave** (free ROM build farm via Actions) | strong Plan-A alternative | 16–64 core machines, zero maintenance; this harness keeps full control, reproducibility and no third-party dependency — but Crave is the pragmatic shortcut if it has queue capacity |
| `out/`-restore chaining | rejected as primary | exact resume but fragile across soong regen; kept as documented variant |
| `actions/cache` | rejected | 10 GB cap + LRU eviction = silent cold restarts |
| Self-hosted runner on a phone/PC | deferred | works (same scripts), but requires an always-on box with 200 GB — exactly what we don't have |
| Google Colab + Drive-cached ccache | rejected | 2 vCPU floor makes even cached slices ~2 days per attempt; ToS risk |
| Oracle Cloud free ARM (4 OCPU/24 GB) | rejected | ARM host — Q-era x86_64 prebuilt toolchains don't run; 200 GB boot volume costs money beyond the free 200 GB *block* allowance gymnastics |
| Codespaces / GitPod | rejected | 2-core default, metered storage beyond a few GB |
| Paid Indian VPS with UPI | rejected by requirement | ~₹150–300 for one run; the entire point is ₹0 |

---

## 12. Cost ledger

| Item | Cost |
|---|---|
| Actions minutes (public repo, ~5 runs × ≤ 5.7 h) | ₹0 |
| Release storage (source 16 GB + ccache 3×8 GB + ROM 1 GB, clobbered) | ₹0 |
| Runner disk/RAM/bandwidth | included |
| **Total** | **₹0** |

The only non-renewable resource consumed is **wall-clock time** — 12–36 h
depending on chaining mode — which the schedule/`CHAIN_PAT` machinery
minimizes without supervision.

---

## 13. Future work

- **Multi-device matrix** — `build.env` is already fully parameterized; a
  matrix strategy (per-device lunch + device_repos file) generalizes the
  harness to any LineageOS-17.1-era device.
- **Tree-change detection** — hash the ROM manifest's HEAD; auto-`force_sync`
  when it moves, instead of manual dispatch.
- **Telegram/broadcast notifications** — one guarded step using existing
  secrets; intentionally omitted from v1 to keep the default secret-free.
- **Signing** — userdebug builds are test-keyed; a signing stage (private
  repo + PAT) is the correct next step for a distributable release.
- **Container fallback** (§7) if GitHub retires jammy runners — scripts are
  substrate-agnostic by design.

---

## 14. Appendix — artifact map

| Artifact | Where |
|---|---|
| Flashable ROM + SHA256SUMS | release `rom` (+ run artifact backup) |
| Source snapshot (shallow, git-stripped, zstd) | release `source-cache` |
| Compiler cache (ccache, compressed) | release `ccache-cache` |
| Per-run engineering telemetry | job summaries (ccache stats, timing, slice classification) |
| This report | `TECHNICAL_REPORT.md` |
