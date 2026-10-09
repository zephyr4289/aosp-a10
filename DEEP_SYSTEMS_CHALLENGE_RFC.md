# ROMForge Extreme Systems Engineering Challenge: Solving the 4-vCPU Android Compilation Frontier

**Author / Dispatcher:** ROMForge Core Infrastructure Team  
**Repository:** `zephyr4289/aosp-a10`  
**Target Branch (v3):** [`feat/romforge-v3`](https://github.com/zephyr4289/aosp-a10/tree/feat/romforge-v3)  
**Baseline Branch:** [`A17-experiment`](https://github.com/zephyr4289/aosp-a10/tree/A17-experiment)  
**API Access Token (Read/Inspect):** `<GITHUB_INSPECTION_TOKEN>`  

---

## 1. Executive Mission

We are building modern Android (LineageOS 21 / Android 14/15, target `lineage-a17-shiba`, 82,735 ninja build graph targets) completely within **ephemeral, 4-vCPU, 16 GiB RAM cloud runners** (GitHub Standard Tier) and optional self-hosted 16-core nodes.

While our storage tier (Btrfs with transparent `zstd:1` compression) has completely eliminated the disk `ENOSPC` deadlock, we have encountered the **fundamental architectural barrier of modern AOSP**:

> **The Soong AST Memory Saturation & Swap Livelock:**  
> When resuming a slice, the Soong Blueprint AST parser (`soong_build` / `blueprint_package`) allocates **15.3 GiB RAM + 11.0 GiB swap** (exhausting total physical memory ~26.5 GiB). The Linux kernel hits 100% swap thrashing, freezing the VM until the runner daemon drops heartbeats and GitHub kills the VM with `SIGTERM` / `exit code 143` after 30 minutes.

We are calling upon our principal systems/compilers engineer to think **completely out of the box** to formulate an extreme, radical breakthrough across:
1. **RAM & Memory Thrashing**: Sub-second AST parsing or bypassing Soong analysis on resume entirely.
2. **I/O & Disk Throughput**: Zero-overhead incremental resume.
3. **Compilation Speed & Threading**: Cutting total build time from 15 hours to <2 hours on 4 vCPUs or instant fusion on self-hosted.

---

## 2. Hard Forensics & Log Evidence from Real 5-Hour Runs

You can use the provided token (`<GITHUB_INSPECTION_TOKEN>`) to pull full raw logs using GitHub CLI:

```bash
# Fetch full raw logs for 15-hour Run #54
gh api repos/zephyr4289/aosp-a10/actions/runs/37759829808/jobs > run54_jobs.json
gh api repos/zephyr4289/aosp-a10/actions/jobs/113404552754/logs > slot2_success_5hr.log
gh api repos/zephyr4289/aosp-a10/actions/jobs/113541508682/logs > slot3_failed_oom.log

# Fetch full raw logs for Run #58 (feat/romforge-v3)
gh api repos/zephyr4289/aosp-a10/actions/jobs/113653446227/logs > run58_slot1_failed_oom.log
```

---

### Anatomy of Failure: Run #54 vs Run #58

#### 1. Run #54 Timeline (`A17-experiment`, 15h 13m total duration)
* **Slot 1 (`c09`, Job `113274153274`):** Ran for **5 hours 0 minutes** (10:53:59Z ➔ 15:52:41Z). Compiled C/C++ core, HALs, native libraries. Successfully banked to `state-lineageosgoogleshibaa17-s1`.
* **Slot 2 (`c05`, Job `113404552754`):** Ran for **5 hours 10 minutes** (15:56:21Z ➔ 21:06:15Z). Resumed cleanly, built up to **39.6% (32,780 / 82,735 targets)** (`KeyChain align`, Java/APK frameworks). Successfully banked to `state-lineageosgoogleshibaa17-s2`.
* **Slot 3 (`c02`, Job `113541508682`):** Booted at 21:08:23Z. Restored `src` and `out`. Executed `soong_ui --make-mode`.
  * Instantly entered `bootstrap blueprint`.
  * RAM rose to `15.3/15.6G`, Swap rose to `9.0/9.0G` (100% full).
  * Sat on `[100% 1/1] bootstrap blueprint` for 25 minutes without completing a single target.
  * Died at 21:40:04Z with:
    ```text
    21:40:03.122Z [21:40:03] [100% 1/1] bootstrap blueprint | RAM: 15.3/15.6G (Swap: 9.0/9.0G) | Disk(vol): 9.1G free
    21:40:04.192Z ##[error]Process completed with exit code 143.
    21:40:04.205Z ##[error]The runner has received a shutdown signal. This can happen when the runner service is stopped, or a manually started runner is canceled.
    ```
* **Slot 4 & Slot 5:** Repeated the exact same failure pattern.

#### 2. Run #58 Timeline (`feat/romforge-v3`)
* **Slot 1 (`c16`, Job `113653446227`):** Booted at 03:23:09Z.
  * Even with `GOMEMLIMIT=11GiB` and 11 GiB Swap, `soong_build` and Go AST workers consumed **15.3 GiB RAM + 11.0 GiB Swap**.
  * At 03:54:34Z (31 minutes in), the runner kernel locked up in swap thrashing, the runner daemon dropped offline, and GitHub killed the job with exit code 143.

---

## 3. The Core Dilemmas We Need Solved

### Dilemma 1: Why is Soong Re-Evaluating from Scratch on Resume?
When `soong_ui --make-mode` runs:
1. It executes `out/soong/.bootstrap/bin/soong_build` to generate `out/soong/build.ninja`.
2. In Android 14/15, the Blueprint AST is massive (~80k definitions). Loading all `.bp` files into Go data structures consumes **14–22 GB of virtual heap**.
3. On a cold run (Slot 1), memory is clean, but when restoring from a banked state, Soong detects mtime changes or environment deltas and tries to perform an incremental graph re-parse, causing pathological memory allocation and GC thrash.
4. **Questions for the Engineer:**
   * Can we **freeze the generated `build.ninja` and `ninja.build` graph** once generated in Slot 1 and force `soong_ui` / `ninja` to **never invoke `soong_build` again** during subsequent resume slices?
   * Can we invoke `ninja` directly (`ninja -f out/combined-lineage_shiba.ninja`) bypassing `soong_ui` completely after Slot 1?

### Dilemma 2: Memory Ceiling vs Swap Thrashing (The 16GB Reality)
* GitHub runners provide exactly **15.6 GiB of physical DDR4/DDR5 RAM** and 4 vCPUs.
* When Swap exceeds ~4–6 GB on an SSD, Linux page fault latency degrades from nanoseconds to milliseconds. A garbage-collected runtime (Go / Java) doing mark-and-sweep across swapped pages will bring CPU utilization to ~1% compilation and 99% `kswapd0` / `iowait`.
* **Questions for the Engineer:**
   * What compiler flags, Go GC tunings (`GOGC=20`, `GOMEMLIMIT=8GiB`), or JVM flags (`-Xmx4g -XX:+UseG1GC`) will guarantee that compilation never spills into slow swap?
   * Can we use memory cgroups (`cgroups v2`) or process nice/oom_score_adj to pin the actions-runner daemon to high priority and kill runaway AST workers before the VM dies?

### Dilemma 3: Extreme Speedups & Subgraph Pre-compilation
* Building the entire 82k targets in a single sequential pipeline on 4 vCPUs takes ~15 hours of compute.
* How can we distribute or pre-compile independent Android modules?
  * Framework vs Native vs Vendor?
  * Turbo partition images (`ALLOW_MISSING_DEPENDENCIES=true`) — why did `vendor_boot` and `dtboimage` fail in turbo prewarm?
  * Compiler caching / prebuilt module injection: Can we substitute heavy compilation steps with prebuilt artifacts or content-addressed module caches?

---

## 4. Expected Deliverable

We request a comprehensive, deeply technical proposal in an RFC Markdown file (`V3_EXTREME_SYSTEMS_BLUEPRINT.md`) detailing:
1. **Architectural Diagnosis:** Deep-dive analysis of why Soong/Go AST blows through 26 GB of memory and how to bypass or bound it.
2. **The Direct Ninja Bypass (Pure Incremental Execution):** Exact steps to run `ninja` directly against banked `out/` without ever re-invoking `soong_ui` or Go AST parsers on resume.
3. **Memory & I/O Shield Architecture:** Kernel tunings, cgroups, swap priority, Go runtime envs, and JVM configurations for zero-thrash execution.
4. **Extreme Acceleration Strategies:** Out-of-the-box techniques to reduce total build time from 15h to under 3h on free tier.
