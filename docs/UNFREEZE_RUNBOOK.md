# Campaign Unfreeze Runbook — the two-command INDEX surgery

Status: **operational runbook** (execute once, after pr-01..pr-06 land)
Audience: the repository owner, or the plan job (it holds `contents: write`)
Scope: the frozen `lineageosgoogleshibaa17` campaign — INDEX pinned to
`state-…-s2` since 2026-10-08 21:06, slice counter stuck at 2, six
consecutive slot-1 mem-stall deaths (runs #58–#63, ~9.5 runner-hours,
zero durable progress).

---

## 0. Why the campaign is frozen

Every v3 slot-1 restored `state-…-s2`, fell back to `soong` mode (the
phantom-filename bug — `bypass_ready()` gated on `out/soong/build.ninja`
while this tree emits `out/soong/build.lineage_shiba.ninja`), re-entered
the 32.6 GiB fused analysis on a 32.6 GiB runner, livelocked kswapd0, and
was evicted mid-bank (exit 143). No newer state ever uploaded; the INDEX
never advanced; the conveyor re-dispatched forever because `mem-stall`
had no case in `dag.next_action()` and the evictions killed the job
before any INDEX write. Two store lesions compound it:

- `state-…-s3` is a **Frankenstein**: 6 parts uploaded by two different
  runs (aa/ab/ac from #63 at 10:03–10:09, ad/ae/af from #61 at 07:48–07:52),
  no SHA256SUMS — restorable by nothing, but first in the fallback
  scanner's newest-first ordering.
- The s2 bank is complete (10 assets + SHA256SUMS, legacy protocol) and —
  critically — the streaming scan confirmed it **contains the per-target
  graph files** (`soong.environment.available` at entry 685, warm
  `.bootstrap` tooling): the campaign already paid for the graph; the
  filename bug just made it invisible.

The P0 patches fix all of the machinery. This runbook performs the one
piece of surgery code cannot do retroactively: re-pointing the campaign
at a manifest-clean resume point and purging the lesion.

---

## 1. The surgery (two commands + one purge)

Run from a machine with `gh` authenticated to the repo (or paste into a
`workflow_dispatch` plan-job step). Replace `KEY=lineageosgoogleshibaa17`
and `REPO=zephyr4289/aosp-a10` as needed.

```bash
KEY=lineageosgoogleshibaa17
OLD=state-${KEY}-s2

# (1) Re-bank s2's assets into a fresh manifest-first tag (same parts —
#     the bytes are already good; this only adds the completion marker).
#     Easiest path: copy every asset from the old tag, then regenerate
#     MANIFEST.json from the existing SHA256SUMS.
gh release view $OLD --repo $REPO --json assets -q '.assets[].name' \
  | while read -r a; do
      gh release download $OLD --repo $REPO --pattern "$a" --dir /tmp/s2fix
      gh release upload state-${KEY}-s2-clean --repo $REPO \
        /tmp/s2fix/"$a" --clobber
    done
# regenerate the manifest from the downloaded SHA256SUMS:
python3 - <<'EOF'
import json, pathlib
sums = pathlib.Path("/tmp/s2fix/SHA256SUMS").read_text().splitlines()
parts = {tok[1]: tok[0] for tok in (l.split() for l in sums if l.strip())
         if ".part." in tok[1]}
pathlib.Path("/tmp/s2fix/MANIFEST.json").write_text(json.dumps(
    {"version": 1, "kind": "out-state", "parts": parts,
     "nonce": "runbook-s2-clean", "legacy_rebank": True}, indent=2))
EOF
gh release create state-${KEY}-s2-clean --repo $REPO \
  --title "out-state s2 (manifest-first re-bank)" \
  --notes "Runbook re-bank of the frozen s2 resume point." \
  /tmp/s2fix/MANIFEST.json || \
gh release upload state-${KEY}-s2-clean --repo $REPO /tmp/s2fix/MANIFEST.json --clobber

# (2) Rewrite INDEX.json to point at the clean bank and clear the loop.
gh release download forge-index --repo $REPO --pattern "INDEX.json" --dir /tmp/s2fix
python3 - <<EOF
import json, pathlib
p = pathlib.Path("/tmp/s2fix/INDEX.json")
idx = json.loads(p.read_text())
t = idx["targets"]["$KEY"]
t["state_tag"] = "state-${KEY}-s2-clean"
t["slice"] = 2
t["done"] = False
t["last_classification"] = "sliced"
t["stop_reason"] = "runbook: unfreeze after #58-#63 crash loop"
t.pop("mem_stall_retries", None)
p.write_text(json.dumps(idx, indent=2, sort_keys=True))
EOF
gh release upload forge-index --repo $REPO /tmp/s2fix/INDEX.json --clobber

# (3) Purge the Frankenstein (and any other orphaned partials).
gh release delete state-${KEY}-s3 --repo $REPO --yes --cleanup-tag
```

The first dispatched slot after the surgery: restores the clean s2,
discovers the graph with the P0-1 glob names (`out/soong/build.lineage_shiba.ninja`),
normalizes mtimes with the P0-3 manifest (skipped gracefully until a mint
happens — G3 may refuse once, soong mints, the manifest is cut, every slot
after that bypasses), and executes
`ninja -f out/combined-lineage_shiba.ninja -j 8 bacon` without ever
launching soong_ui.

---

## 2. Faster alternative: let the turbo lane mint the graph first

If you would rather not wait for a main-chain slot to survive the
analysis: dispatch one turbo job for a VALID goal (after the P1-1 lint
lands, `bootimage` is safe — it is a known-good phony in this graph;
`dtboimage` is NOT, it died as `unknown target` in runs #61–#63). The
turbo environment (cold out/, ~55 GiB disk free, dynamic swap growing to
25 GiB) is the only execution environment that has ever completed the
fused analysis on free-tier hardware. With pr-03 it banks
`graph-<mhash>-<lunch>` even if the partition build itself fails — one
turbo job-hour buys every future slot out of the 32.6 GiB gauntlet
permanently. After the mint, every slot (and the s2 surgery becomes
optional) restores the graph and runs direct-ninja from slice 2's
compiled state.

## 3. Verifying the unfreeze worked

Watch the next run's slot-1 log for these lines, in this order:

```
bypass G1..G3 PASS: soong graph=build.lineage_shiba.ninja, combined=combined-lineage_shiba.ninja — direct ninja engaged
slice mode: ninja-direct (bypassing soong_ui entirely — frozen graph combined-lineage_shiba.ninja)
```

If instead you see `bypass G3 REFUSED: … newer than … — graph stale`,
that is the P0-3 mtime discipline doing its job on the first slot (the
graph banks from that slot's soong run, the manifest is cut, and every
subsequent slot passes). The acceptance test for the whole overhaul: a
slot log must never again contain a silent
`slice mode: soong (full soong_ui pipeline)` when a state bank was
restored — every refusal now names its invariant.
