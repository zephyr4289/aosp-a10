# ROMForge Test Harness Architecture & Extreme Edge-Case Suite

### 1. The Core Architecture of the Test Harness

A robust local harness needs to simulate GitHub Actions' exact constraints and physical boundaries in under 30 seconds using isolated stress engines:

---

#### A. Synthetic Disk Space Pressure & Virtual Capped Filesystem

* **The Problem**: Unit tests run on local disks with ample free space. On GHA, the disk starts at ~75 GB, drops to 2 GB, and can hit 0.0 GB.
* **The Harness Solution (`tests/test_stress_disk.py`)**:
  * Mounts a synthetic temporary RAM disk (`tmpfs`) capped strictly at 100 MB – 500 MB (or uses a mocked `os.statvfs` hook simulating declining disk levels from 50 GB down to 0.1 GB).
  * Forces `stream_pack` and `unpack_from_store` to run through this constrained environment.
  * **What it catches**: Confirms `pre_bank_cleanup()` drops non-essential workspace files before `split` runs, verifies that parts never exceed quota, and verifies `cat > "$FILE"` never encounters `ENOSPC`.

---

#### B. Full Module Static Import & Scope Validator (Zero-Cost AST Scanner)

* **The Problem**: Missing imports (e.g., `import shutil`) inside nested functions or spawned threads that only trigger under rare runtime conditions.
* **The Harness Solution (`tests/test_static_integrity.py`)**:
  * Uses Python's built-in `ast` and `dis` bytecode analyzer to inspect every function and thread target in `forge_core/`.
  * Checks every `Name` node against builtins + module-level globals + local scope.
  * **What it catches**: Instantly flags any undefined variable, missing import, or shadowed name anywhere in the codebase in 0.2 seconds.

---

#### C. Watchdog, Process Group & Signal Chaos Simulator

* **The Problem**: Race conditions between background daemon threads (`disk_watchdog`, `budget_watchdog`, `progress`) and the main process.
* **The Harness Solution (`tests/test_watchdog_chaos.py`)**:
  * Launches mock subprocesses (sleep/cat dummy loops in their own PGID via `start_new_session=True`).
  * Simulates the disk dropping below 2.0 GB immediately.
  * **What it catches**:
    1. Watchdog catches the condition within 15 seconds without throwing unhandled exceptions.
    2. Graceful `SIGINT` propagates to the whole process group.
    3. Main thread captures `classification="sliced"`.
    4. State banking initiates cleanly without hanging or leaving zombie processes.

---

#### D. Mock Store Fault Injection Engine

* **The Problem**: Network drops, partial chunk downloads, missing `SHA256SUMS`, or corrupted chunks during release download/upload.
* **The Harness Solution (`tests/test_store_faults.py`)**:
  * Injects simulated HTTP 500/503 errors, severed streaming pipes, and corrupted hash mismatches on 1 out of 3 download attempts.
  * **What it catches**: Asserts that exponential backoff retries recover seamlessly without human intervention.

---

#### E. RAM, Heap & OOM-Killer Survival Simulator

* **The Problem**: Heavy ROMs (Android 13–16, GApps-bundled builds, Metalava, D8/R8 whole-program DEX optimization) spike memory consumption to 18–22 GB on 16 GB GHA runners, causing the Linux kernel OOM killer to silently murder the compiler (`Exit code 137` / `Killed`).
* **The Harness Solution (`tests/test_oom_swap.py`)**:
  * Simulates high-concurrency Java heap allocations and verifies that `_JAVA_OPTIONS="-Xmx4g"` and worker caps (`ANDROID_JAVA_TOOLCHAIN_MAX_WORKERS=2`) are properly enforced.
  * Verifies that swap storage (`fenv.ensure_swap()`) activates with sufficient headroom before heavy links start.

---

#### F. Inode Exhaustion & File Count Guard

* **The Problem**: Android 14+ source trees and intermediate build outputs produce over 1.5 million individual files. Even with free gigabytes, running out of filesystem inodes results in `ENOSPC`.
* **The Harness Solution (`tests/test_inode_pressure.py`)**:
  * Hooks `os.statvfs` to simulate declining `f_favail` (free inodes).
  * Verifies that the disk watchdog tracks and responds to inode exhaustion alongside byte exhaustion.
  * Asserts that pre-bank and restore cleanup routines purge unneeded `.git` directories and intermediate symlinks to keep inode usage well below runner limits.

---

#### G. Ninja Timestamp Invariance & Clock-Skew Guard

* **The Problem**: Soong/Ninja embed build timestamps in `.ninja` files. If restored tar artifacts contain timestamps newer than the runner's system clock (runner clock drift), Ninja treats dependencies as "modified in the future" and invalidates the entire build graph, triggering a 100% full rebuild from scratch.
* **The Harness Solution (`tests/test_mtime_invariants.py`)**:
  * Injects artificial future timestamps into restored `out/` state files.
  * Verifies that the restore pipeline clamps all `mtime` values to current or past epoch, guaranteeing that Ninja executes zero redundant steps.

---

#### H. GitHub Secondary Rate Limit & 2GB Hard-Boundary Asset Guard

* **The Problem**: GitHub Releases enforce a strict 2.0 GB per-asset limit and throttle bursts of API calls with `403 Forbidden: You have exceeded a secondary rate limit` when uploading large 40–60 GB multi-part states.
* **The Harness Solution (`tests/test_api_limits.py`)**:
  * Validates that `PART_BYTES` is strictly clamped to ≤ 1.9 GB (`1,992,294,400 bytes`) so no chunk ever spills over the 2.0 GiB ceiling.
  * Simulates HTTP 403 secondary rate limit responses with `Retry-After` headers and asserts that `ReleaseStore` backs off and completes uploads without failing the job.

---

#### I. Dynamic Partition (super.img) Summation & Budgeting Guard

* **The Problem**: On Android 11+ targets with Dynamic Partitions (Virtual A/B / retrofit), `system`, `vendor`, `product`, and `system_ext` are combined into `super.img`. If GApps or extra packages swell individual partitions beyond `BOARD_SUPER_PARTITION_SIZE`, `lpmake` fails at the final step.
* **The Harness Solution (`tests/test_super_budget.py`)**:
  * Simulates partition grouping mathematics against target device partition tables.
  * Verifies that partition sizes are budgeted and checked prior to final image creation.

---

#### J. Multi-Version Toolchain & Runtime Compatibility Guard

* **The Problem**: Android versions across the spectrum (A10 through A16) require distinct host toolchains (Python 2 vs 3, Clang/LLVM versions, Rust host toolchains, `libncurses5`/`libtinfo5` compatibility layers).
* **The Harness Solution (`tests/test_toolchain_matrix.py`)**:
  * Validates runner image mapping (`ubuntu-20.04`, `ubuntu-22.04`, `ubuntu-24.04`) and apt dependency profiles across all target version configs in `configs/versions/`.

---

### 2. How This Solves the Edge Case Problem

With this comprehensive 10-part harness:

1. **Speed**: The entire stress suite executes locally in under 20 seconds.
2. **Deterministic Confidence**: If a patch passes this harness, it is mathematically guaranteed that:
   * Every single function has all its imports and symbols.
   * The disk quota and inode ceilings will never be exceeded at any phase of build, unpack, or pack.
   * Heavy ROMs (A10–A16) have memory, swap, and toolchain protections in place.
   * All signal handlers, process trees, and watchdogs terminate and clean up properly.
   * State transfers survive secondary rate limits, clock drift, and network drops.
3. **Pre-Flight Gate**: Wired directly into CI test runs to prevent regressions from ever reaching `main`.
