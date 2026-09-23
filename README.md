# ROMForge

**Universal, zero-cost ROM compiler for GitHub Actions — Android 10 → 16,
any ROM, one YAML per target — engineered so the result cannot brick your
phone.**

```
   plan ──► sync (content-addressed src snapshot)
             ├─ turbo:bootimage ──┐   partition prewarm, parallel runners
             ├─ turbo:vendorimage ├──►  ... while slot-1 builds ...
             └─ turbo:productimage ┘              │
                     slot-1 ──► slot-2 ──► … ──► verify(14-point gate) ──► publish
```

* **Zero redo.** Build state (`out/` + `.ninja_log`) is relayed between
  jobs — ninja resumes *exactly* where the last slot stopped, for every
  language. No ccache-only re-dexing marathons. (TECHNICAL.md §4)
* **Zero staging.** Archives stream straight from `tar | zstd | split`
  into Release assets. ENOSPC at 94 % is structurally unreachable.
  (§5)
* **Back-to-back slots.** The whole campaign lives in ONE workflow run
  (35-day ceiling), no 6-hourly cron, no CHAIN_PAT. (§6)
* **Any ROM.** A ROM is a 25-line YAML (`configs/roms/_template.yaml`).
  A10→A16 hosting matrix in `configs/versions.yaml`.
* **Cannot-brick policy.** A 14-point hard gate blocks the release of
  anything unverified; the flash script re-checks device identity and
  checksums at flash time; rescue images ship with every ROM.
  (docs/SAFETY.md)
* **₹0.** Public repo → unlimited Actions minutes, unlimited Release
  storage (2 GiB chunks), 20 concurrent jobs.

## Quickstart (5 minutes of your time)

1. **Create a public repo** (must be public — the free tier *is* the
   architecture) and push this tree.
2. **Settings → Actions → General → Workflow permissions** → *Read and
   write permissions*.
3. **Check the device profile** `configs/devices/nokia_pl2.yaml` —
   partition budgets are tree-declared; if you have a stock dump, paste
   the measured values and set `budgets_authority: stock` (the gate
   gets stricter).
4. **Actions → ROMForge → Run workflow** → `rom = qassa-a10`.
   Watch the plan job print the contract, then the campaign run itself.
5. **Collect** from the `rom-…` release: ROM zip, `SAFETY_REPORT.json`,
   `SHA256SUMS`, `flash-guarded.sh`, rescue images.

A second ROM on the same phone = one more YAML (`lineage-17.1-pl2.yaml`
ships as the worked example). A different phone = a device profile
(`docs/ADD_DEVICE.md`).

## Repository layout

| Path | Role |
|---|---|
| `forge_core/` | The engine: env, config, chunker, store, syncer, relay, engine, turbo, gate, cli |
| `configs/roms/` | ROM profiles (any ROM = one YAML) |
| `configs/devices/` | Device safety profiles (what NOT to brick) |
| `configs/versions.yaml` | A10→A16 hosting matrix |
| `.github/workflows/forge.yml` | The campaign orchestrator |
| `.github/workflows/slot.yml` | One idempotent build slot (reusable) |
| `patches/device/` | Product makefiles injected into the tree (QASSA/PL2) |
| `patches/upstream/` | Patch series upgrading the inherited aosp-a10 repo |
| `tests/` | 40-assertion offline suite (`bash tests/run_tests.sh`) |
| `TECHNICAL.md` | The full engineering dossier — read this |
| `docs/SAFETY.md` | Flash safety + unbrick runbook |
| `docs/ADD_ROM.md` / `docs/ADD_DEVICE.md` | Universality guides |

## Operating manual

| Action | How |
|---|---|
| Re-sync after manifest drift | Run workflow with `force_sync: true` (mints a new mhash) |
| Rebuild after a source change | Just run the workflow — warm state restores, rebuild lands in 1–3 h |
| Disable partition fan-out | `skip_turbo: true` |
| Smaller smoke test first | `target: bootimage` |
| Drop a campaign | Delete the target's `state-*` releases + INDEX entry |
| Environment self-report | `./forge doctor` |

## Local runs

Everything is CI-agnostic Python 3 (stdlib + PyYAML):

```bash
./forge doctor                 # mounts, disk ledger, tool report
./forge plan --rom qassa-a10   # the execution contract
./forge validate               # config schemas
bash tests/run_tests.sh        # 40 offline assertions
```

On your own Linux box with ~200 GB: `./forge sync --rom qassa-a10` then
`./forge slice --rom qassa-a10` with `--store fs:/path` — same code path
as CI, no GitHub credentials needed.

## Ground rules

- **Public repo or nothing** — the economics are the architecture.
- **Never commit proprietary blobs** — device/vendor repos are cloned at
  build time from maintainer repos (or via a PAT secret if they go
  private).
- **The gate is not advisory** — `forge publish` refuses without a PASS
  report; do not "fix" that by deleting the report.
- **Honesty about physics** — 4 vCPUs cannot cold-build AOSP 16 in 6 h;
  ROMForge converges it overnight with zero redo instead of lying about
  it (TECHNICAL.md §6.2).

*Harness MIT-licensed. ROM and device sources belong to their respective
maintainers. Flash at your own risk — read docs/SAFETY.md first.*
