# ROMForge

**Deterministic, Zero-Staging Distributed AOSP Compilation Engine on Ephemeral GitHub Actions Compute**

[![Test Suite](https://github.com/zephyr4289/aosp-a10/actions/workflows/test.yml/badge.svg)](https://github.com/zephyr4289/aosp-a10/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![AOSP Compatibility](https://img.shields.io/badge/Android-10%20%E2%86%92%2016-green.svg)](configs/versions.yaml)
[![Infrastructure Cost](https://img.shields.io/badge/Cost-%240%20(Free%20Tier)-brightgreen.svg)](TECHNICAL.md)

ROMForge is a continuous integration compilation framework designed to build production-grade Android Open Source Project (AOSP) system images (Android 10 through Android 16) within the structural constraints of standard GitHub Actions ephemeral runners (4 vCPU, 16 GiB RAM, 6-hour job execution ceiling, constrained `/` root partition) at zero infrastructure cost.

---

## Architecture & Execution Flow

```
                      ┌────────────────────────────────────────────────────────┐
                      │              ROMForge Workflow DAG Execution           │
                      └────────────────────────────────────────────────────────┘
                                                  │
                                                  ▼
                                      [ plan & validate ]
                                    (Compute contract & hashes)
                                                  │
                                                  ▼
                                      [ source sync & snapshot ]
                           (Content-addressed manifest snapshot: src-<mhash>)
                                                  │
                  ┌───────────────────────────────┼───────────────────────────────┐
                  ▼                               ▼                               ▼
        [ turbo: bootimage ]            [ turbo: vendorimage ]          [ turbo: productimage ]
      (ALLOW_MISSING_DEPS=true)       (ALLOW_MISSING_DEPS=true)       (ALLOW_MISSING_DEPS=true)
                  │                               │                               │
                  └───────────────────────────────┼───────────────────────────────┘
                                                  ▼
                                          [ slot-1 (build) ]
                             (Merge turbo partition states into out/ tree;
                              execute framework/ART/system critical path)
                                                  │
                                                  ▼
                                          [ slot-2 (build) ]
                            (Exact-resume relay: restore out/ + .ninja_log)
                                                  │
                                                  ▼
                                          [ verify & gate ]
                                (14-Point Anti-Brick Verification Suite)
                                                  │
                                                  ▼
                                         [ publish release ]
                             (ROM payload, SHA256SUMS, flash-guarded.sh,
                              SAFETY_REPORT.json, rescue boot/dtbo/vbmeta)
```

---

## Core Engineering Systems

### 1. Exact-Resume Ninja State Relay (`forge_core/relay.py`)

Traditional multi-job CI compilers rely on compiler-level caches (such as `ccache`). While `ccache` caches C/C++ object outputs, it cannot cache:
* Kati / Soong product configuration analysis (15–25 minutes recomputed per slice).
* Java / Kotlin compilation and R8/D8 dexing pipelines (the dominant compute phase in Android 10+).
* Protobuf, AIDL, generated IPC bindings, and APEX container packaging.

Under a `ccache`-only relay model, slice $k$ pays a cumulative rebuild prefix $P(k)$ that scales with tree progress, causing total campaign duration to grow as $O(k)$ per slice.

```
Relay Efficiency:
  Traditional ccache:   T_total = W_work + Σ P(k)         [Super-linear redo]
  ROMForge out/ relay:  T_total = W_work + k * O(1)_relay  [Constant flat overhead]
```

**ROMForge Mechanism:**
* Persists the complete `out/` filesystem state, including `.ninja_log`, `.ninja_deps`, dependency databases, and nanosecond-precision file modification timestamps (`mtimes`).
* Selectively filters transient, rebuild-trivial artifacts via exclusion filters (`symbols/` debugging directories, host test oat artifacts) to maintain a compact transfer envelope.
* Clean Process Group Signaling: Builds execute in a dedicated session (`os.setsid`). Upon budget expiration or storage warnings, `SIGINT` is broadcast to the process group (`os.killpg`), enabling Soong/Ninja to complete pending atomic file operations and flush `.ninja_log` cleanly before state archiving.

---

### 2. Zero-Staging Streaming Chunker (`forge_core/chunker.py`, `forge_core/store.py`)

Ephemeral CI nodes have limited disk allocations. Writing multi-gigabyte tarballs to disk prior to cloud upload frequently triggers `ENOSPC` (No space left on device) disk failures.

```
Pipeline:
  tar (out/ or aosp/) ──► zstd -T0 -3 ──► split -b 1900M --filter 'gh release upload ...'
```

* **Zero Local Intermediate Storage:** Uses GNU `split --filter` to stream compressed chunks directly into GitHub Release assets without staging split parts on the runner's local filesystem.
* **Content-Addressed Immutability:** Manifest configurations, local hardware overrides, and patch series are hashed into a deterministic identifier (`mhash = sha256(manifest + repos + patches)[:16]`). The base source tree snapshot is uploaded once as `src-<mhash>` and shared across all subsequent rebuilds and turbo jobs.
* **Fault-Tolerant Asset Reassembly:** Assets are chunked into 1.90 GiB boundaries (below GitHub's 2.00 GiB release asset ceiling) accompanied by cryptographically verified `SHA256SUMS` manifests.

---

### 3. Ephemeral Mount Layout & Reclaim Ladder (`forge_core/env.py`, `forge_core/engine.py`)

GitHub Actions standard runners supply two primary storage locations:
* `/` (Root filesystem): ~14–25 GiB usable capacity.
* `/mnt` (Secondary ephemeral mount): ~65 GiB usable capacity.

```
Storage Ledger (Typical Android 10/11 Tree):
┌────────────────────────────────────────────────────────┬─────────────┐
│ Component                                              │ Footprint   │
├────────────────────────────────────────────────────────┼─────────────┤
│ Git-stripped, shallow source tree (AOSP core + vendor) │ ~33–35 GiB  │
│ Active out/ target directory (excluding symbols)       │ ~28–36 GiB  │
│ Swap allocation (dd block-allocated, non-sparse)       │ 4.0 GiB     │
│ Zero-staging streaming relay parts in flight           │ 0.0 GiB     │
├────────────────────────────────────────────────────────┼─────────────┤
│ Total Peak Allocated Footprint                         │ ~65–75 GiB  │
└────────────────────────────────────────────────────────┴─────────────┘
```

**Proactive Disk Watchdog & Reclaim Ladder:**
A background monitoring thread polls available mount capacity every 60 seconds. If storage crosses the safety margin, the reclaim ladder executes safe, non-critical purges in descending order:
1. `out/target/product/*/symbols`: Unstripped binary copies (Ninja re-links stripped outputs in minutes if requested).
2. `out/target/product/*/obj/*/oat_x86*`: Host unit-testing dex caches.
3. `out/target/product/*/*.img.new`: Stale intermediate filesystem image assemblies.
4. **Early Bank Trigger:** If storage remains critical, the engine issues a controlled `SIGINT` to gracefully serialize the Ninja build graph and finalize the slice, preventing unrecoverable linker `ENOSPC` crashes.

---

### 4. Turbo Partition Decomposition (`forge_core/turbo.py`)

AOSP module graphs allow isolated compilation of decoupled partition images when dependency strictness is relaxed via `ALLOW_MISSING_DEPENDENCIES=true`.

* **Parallel Matrix Fanout:** Partition subgraphs (`bootimage`, `vendorimage`, `productimage`) compile concurrently on dedicated ephemeral runners while `Slot 1` advances the system and framework graph.
* **Non-Destructive Additive Merging:** Turbo partition states are uploaded to `state-<key>-turbo-<partition>` and merged into the primary runner's `out/` tree via additive synchronization (`rsync --ignore-existing`).
* **Authoritative Relink Phase:** The primary slot runner re-evaluates the complete graph against the authoritative system configuration, forcing Ninja to reconcile and re-link any divergent interfaces before image generation.

---

### 5. 14-Point Anti-Brick Safety Verification Gate (`forge_core/gate.py`)

Prior to publishing any flashable release, ROMForge runs a strict, automated 14-point validation gate on the target image closure. If any assertion fails, release publishing is halted.

| # | Inspection Gate | Technical Assertion |
|---|---|---|
| **1** | Device Identity | `ro.product.device` and build fingerprint match target hardware allowlist |
| **2** | OTA Metadata Assert | Updater scripts contain hardware assertions matching target device codenames |
| **3** | Partition Budget | Image byte counts conform strictly to partition allocations (stock dump or device tree declared) |
| **4** | Dynamic Partitions | Aggregate size of sub-partitions in `super` does not exceed maximum group capacity |
| **5** | AVB 2.0 & VBMeta | Android Verified Boot hashtree descriptors and VBMeta header flags are internally consistent |
| **6** | Boot Anatomy | Boot image header magic, kernel offsets, page alignments, and required `dtbo.img` structures validate |
| **7** | VINTF Manifests | Vendor Interface manifests pass schema syntax and matrix compatibility (`checkvintf`) |
| **8** | SPL Window | `ro.build.version.security_patch` satisfies device monotonic anti-rollback constraints |
| **9** | VNDK Interface | Vendor API level corresponds to system VNDK snapshot availability |
| **10** | SELinux Policy | Monolithic/split binary `sepolicy` and `file_contexts` are fully compiled and valid |
| **11** | OTA Payload Integrity | `payload.bin` cryptographic checksum and byte length match `payload_properties.txt` |
| **12** | Archive Topology | ZIP filesystem layout strictly matches target partition scheme (A/B payload vs. non-A/B block map) |
| **13** | Keystore & Signature | Distribution packages contain valid `META-INF` cryptographic release signatures |
| **14** | Flash Plan Dry-Run | Execution simulation verifies all referenced partition blocks exist and map correctly |

Every published release includes `SAFETY_REPORT.json` and `flash-guarded.sh`, which re-executes hardware identity and hash checks directly on the host machine prior to flashing.

---

## Repository Structure

```
.
├── .github/
│   └── workflows/
│       ├── forge.yml             # Primary campaign orchestrator DAG
│       ├── slot.yml              # Reusable idempotent build slot workflow
│       └── test.yml              # Offline test suite workflow
├── configs/
│   ├── devices/                  # Device safety profiles and partition budgets
│   │   ├── nokia_pl2.yaml
│   │   └── ...
│   ├── roms/                     # Declarative ROM definitions
│   │   ├── _template.yaml
│   │   ├── qassa-a10.yaml
│   │   └── lineage-17.1-pl2.yaml
│   └── versions.yaml             # Host toolchain and Android platform matrix
├── forge_core/                   # ROMForge core engine
│   ├── chunker.py                # Zero-staging streaming tar/zstd/split pipeline
│   ├── cli.py                    # Unified command-line interface
│   ├── config.py                 # YAML schema validators and profile parser
│   ├── engine.py                 # Build lifecycle, watchdog, and process supervisor
│   ├── env.py                    # Host mount discovery and storage layout
│   ├── gate.py                   # 14-point anti-brick safety verification suite
│   ├── relay.py                  # Exact-resume out/ state serialization
│   ├── store.py                  # GitHub Release & local filesystem storage backends
│   ├── syncer.py                 # Shallow source synchronization and patch manager
│   └── turbo.py                  # Parallel partition fanout and state merging
├── docs/
│   ├── ADD_DEVICE.md             # Guide for provisioning new device safety profiles
│   ├── ADD_ROM.md                # Guide for adding new ROM definitions
│   └── SAFETY.md                 # Unbrick procedures, safety invariants, and flashing
├── patches/                      # Injectable device/vendor hardware patches
├── tests/                        # 40-assertion offline unit & integration test suite
│   └── run_tests.sh
└── forge                         # CLI entrypoint executable
```

---

## Configuration Specification

ROM targets and device hardware constraints are configured through declarative YAML schemas.

### Target ROM Profile (`configs/roms/qassa-a10.yaml`)

```yaml
schema: 1
target_key: qassa-pl2-a10
android_version: 10
rom_name: qassa
device_slug: nokia_pl2

manifest:
  url: https://github.com/zephyr4289/qassa_manifest
  branch: ten
  depth: 1

lunch:
  flavor: qassa_PL2-userdebug
  target: bacon

device_repos:
  - path: device/nokia/PL2
    url: https://github.com/zephyr4289/android_device_nokia_PL2
    branch: lineage-17.1
  - path: kernel/nokia/SDM660
    url: https://github.com/zephyr4289/android_kernel_nokia_sdm660
    branch: lineage-17.1
  - path: vendor/nokia/PL2
    url: https://github.com/zephyr4289/proprietary_vendor_nokia_PL2
    branch: lineage-17.1

env:
  ALLOW_MISSING_DEPENDENCIES: "false"
  USE_CCACHE: "0"

turbo:
  enabled: true
  partitions:
    - bootimage
    - vendorimage
    - productimage

budget_minutes: 275
```

### Device Safety Profile (`configs/devices/nokia_pl2.yaml`)

```yaml
schema: 1
device_slug: nokia_pl2
family: nokia_sdm660
market_names:
  - "Nokia 6.1"
  - "Nokia 6 (2018)"
assert_devices:
  - PL2
  - PL2_sprout
  - Plate2

ab_device: true
dynamic_partitions: false

budgets_authority: declared
budgets:
  boot: 67108864       # 64 MiB
  system: 2684354560   # 2.5 GiB
  vendor: 536870912    # 512 MiB
  dtbo: 8388608        # 8 MiB

avb:
  enabled: true
  verify_vbmeta: true

spl:
  min_date: "2019-10-01"
  max_date: "2024-01-01"
```

---

## Local Development & Testing

ROMForge is self-contained and CI-agnostic. All subsystems can be executed locally or verified offline without external network dependencies:

```bash
# Verify environment, available mounts, and host toolchains
./forge doctor

# Validate configuration schemas and target matrices
./forge validate

# Run dry-run execution plan and calculate mhash fingerprint
./forge plan --rom qassa-a10

# Run complete 40-assertion offline unit and integration suite
bash tests/run_tests.sh
```

### Offline Test Coverage

The test harness validates all core components against synthesized trees and poison scenarios:
1. **Chunker Pipeline:** Stream packing, multi-part chunk boundary hashing, and tamper detection.
2. **FsStore Storage Backend:** Deterministic state indexing, tagging, and local retrieval.
3. **Relay Forensics:** `.ninja_log` parsing, rule completion metrics, and ETA modeling.
4. **14-Point Safety Gate:** Nominal valid build verification.
5. **Poison Injection Suite:** Confirms gate rejection across five isolated failure conditions:
   * Mismatched target device identifiers (`ro.product.device`).
   * Partition size boundary overrun.
   * Missing required `dtbo.img` binary structure.
   * Security Patch Level regression / invalid bounds.
   * Cryptographic mismatch in `payload.bin` metadata.
6. **Configuration Validation:** Declarative YAML schema verification.

---

## Performance & Resource Utilization

| Target Configuration | Compute Allocation | Source Snapshot | Turbo Concurrency | Total Wall-Clock |
|---|---|---|---|---|
| **Android 10 (Cold, with Turbo)** | 4 vCPU / runner | 55.1 GB (`src-<mhash>`) | 3 Parallel Runners (`boot`, `vendor`, `product`) | **~5.5 – 6.5 Hours** |
| **Android 10 (Warm Rebuild)** | 4 vCPU / runner | Reused | None (Single Job) | **~1.5 – 2.5 Hours** |
| **Android 13/14 (Cold)** | 4 vCPU / runner | ~75 GB (`src-<mhash>`) | 4 Parallel Runners | **~8 – 11 Hours (Resumable)** |
| **Android 15/16 (Cold)** | 4 vCPU / runner | ~95 GB (`src-<mhash>`) | 4 Parallel Runners | **~14 – 18 Hours (Resumable)** |

---

## Operational Safety Principles

1. **Deterministic Release Gate:** Releases are blocked automatically unless the full artifact closure achieves an explicit `PASS` across all 14 points of the verification suite.
2. **Client-Side Pre-Flash Verification:** The included `flash-guarded.sh` checks device identity properties via `fastboot getvar` and verifies SHA256 image hashes prior to executing partition writes.
3. **Rescue Image Provisioning:** Every generated release includes standalone rescue images (`boot.img`, `dtbo.img`, and `vbmeta.img`) to facilitate immediate recovery in fastboot if needed.

---

## License

The ROMForge compiler and CI harness are released under the [MIT License](LICENSE). Source code for target operating system frameworks, device kernels, and vendor blobs are governed by their respective upstream licenses.
