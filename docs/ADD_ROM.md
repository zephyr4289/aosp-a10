# ADD_ROM — building ANY ROM (the whole point)

If a ROM builds on a Linux box with
`repo sync` → `lunch` → `m <target>`, it builds here. You never touch
the workflows; a ROM is one YAML file.

## 1. The 60-second version

1. Copy `configs/roms/_template.yaml` →
   `configs/roms/<rom>-<device>.yaml`.
2. Fill the REQUIRED fields: `name`, `android_version` (10–16),
   `device`, `manifest_url`, `manifest_branch`, `lunch`.
3. `./forge validate && ./forge plan --rom <rom>-<device>`.
4. Actions → *ROMForge — universal build* → Run workflow →
   `rom = <rom>-<device>`.
5. Collect from the `rom-…` release.

## 2. Field-by-field, with the judgment calls

* **`android_version`** — must be 10–16; drives the whole hosting matrix
  (runner image, host JDK, expected sizes, default slot count) from
  `configs/versions.yaml`. New Android version = extend that matrix
  first (runner image + any host quirks you hit).
* **`manifest_url` / `manifest_branch`** — the ROM's manifest repo and
  the branch/tag to pin. Prefer release tags over moving branches for
  reproducibility; ROMForge snapshots whatever you point at under a
  content-addressed `src-<mhash>`.
* **`lunch` / `build_target`** — exactly what you'd type. `bacon`
  (Lineage-style), `otapackage` (AOSP-style), or a ROM-specific target
  like QASSA's `qassa`.
* **`device_repos`** — the device tree / kernel / vendor repos, in
  `path|url|branch` form, shallow-cloned after sync (the proven
  "direct" flow). Alternative: `device_clone_mode: manifest` +
  `local_manifests:` (see `configs/roms/qassa_pl2.xml` for the file
  format) if the ROM's manifest must resolve them.
* **`patches`** — files copied into the tree before the first slice
  (product makefiles, fixups). Keep them in `patches/` in this repo so
  campaigns are reproducible.
* **`env`** — build env overrides. `WITH_DEXPREOPT: "false"` is the
  single biggest time/disk saver; add anything your ROM documents
  (`CCACHE_*`, `SIGNING_KEY_*`, etc.).
* **`slices`** — campaign length. Rule of thumb from the physics table
  (TECHNICAL.md §6.2): A10–A12 → 4, A13 → 6, A14+ → 8. Slots are
  idempotent; extras no-op in ~2 min when the build finishes early.
* **`turbo.targets`** — partition prewarm fan-out. Defaults per version
  are sane; the critical-path `systemimage` is never turbo'd (it runs
  in slot-1 instead).

## 3. Multi-ROM, multi-device = just more YAMLs

Campaigns are keyed per ROM profile (`<rom>-<device>-a<version>`):
concurrency groups serialize a target against itself while different
targets run in parallel (free tier hosts 3–4 full campaigns
comfortably — watch the 20-job ceiling if you go wide on turbo).

## 4. Custom ROMs with their own quirks

* **Needs prebuilt vendor images instead of source vendor?** Add the
  prebuilt repos to `device_repos` — same mechanism.
* **GMS/signed packages?** Never commit blobs. If a repo must be
  private, mirror it and pass a PAT via a `GIT_PAT` secret in the
  profile's repo URLs.
* ** signing keys** — community ROMs ship test-keys; check 13 treats
  signed-with-test-keys as a loud WARN, not a block. Release keys go
  into the ROM's own signing flow (env), never into this harness.

## 5. When your ROM breaks the build

The slot log is a 3-day artifact on every failed job. Real build errors
still bank the state — fix the tree (or a `patches/` entry), re-run the
workflow, and the campaign resumes exactly where it stopped. This is
the whole point of the relay.
