#!/usr/bin/env python3
"""P0 Regression Pins (V7) — the OVERHAUL.md validation ladder, offline.

Pins the three contracts that runs #58-#63 broke:
1. Graph discovery finds per-target names (R1 — the phantom filename).
2. The DAG gives mem-stall a voice: one re-dispatch, then red (R7).
3. The banking integrity protocol refuses Frankenstein banks (P0-4).
4. Mtime normalization actually re-stamps (P0-3 — the bypass enabler).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))

import forge_core.dag as dag
import forge_core.relay as relay
import forge_core.syncer as syncer
from forge_core import engine
from forge_core.store import FsStore


def _mk(out: Path, name: str, size: int = 64, mtime: float = None) -> Path:
    p = out / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


class TestGraphDiscovery(unittest.TestCase):
    """R1: the tree emits per-target names — discovery must find them."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="forge-v7-disc-"))
        self.build_root = self.tmp / "aosp"
        self.out = self.build_root / "out"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _mk_graph(self, product: str):
        _mk(self.out / "soong", f"build.{product}.ninja")
        _mk(self.out / "soong", f"soong.{product}.variables")
        _mk(self.out / "soong", "soong.environment.available")
        _mk(self.out, f"combined-{product}.ninja")
        _mk(self.out, f"build-{product}.ninja")
        _mk(self.out, ".ninja_log")
        _mk(self.out, ".ninja_deps")
        # stub the prebuilt ninja so G2 passes off-runner
        nb = self.build_root / "prebuilts/build-tools/linux-x86/bin/ninja"
        nb.parent.mkdir(parents=True, exist_ok=True)
        nb.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        nb.chmod(0o755)

    def test_discovers_product_suffixed_graph(self):
        """The A17/LineageOS 23.2 layout: build.<product>.ninja etc."""
        self._mk_graph("lineage_shiba")
        graphs = engine.discover_graph(self.build_root)
        self.assertIsNotNone(graphs, "discovery must find per-target names")
        self.assertEqual(graphs["soong"].name, "build.lineage_shiba.ninja")
        self.assertEqual(graphs["combined"].name, "combined-lineage_shiba.ninja")
        self.assertEqual(graphs["kati"].name, "build-lineage_shiba.ninja")

    def test_discovers_classic_graph(self):
        """The classic layout: unsuffixed build.ninja still works."""
        self._mk_graph("unused")
        (self.out / "soong" / "build.unused.ninja").unlink()
        _mk(self.out / "soong", "build.ninja")
        graphs = engine.discover_graph(self.build_root)
        self.assertIsNotNone(graphs)
        self.assertEqual(graphs["soong"].name, "build.ninja")

    def test_refuses_without_incremental_state(self):
        self._mk_graph("lineage_shiba")
        (self.out / ".ninja_deps").unlink()
        self.assertIsNone(engine.discover_graph(self.build_root))

    def test_bypass_ready_g3_refusal_is_loud(self):
        """A tree with .bp/.mk newer than the graph must REFUSE (G3), and
        the refusal must be visible — the six-run crash loop was silent."""
        self._mk_graph("lineage_shiba")
        old = time.time() - 3600
        os.utime(self.out / "soong" / "build.lineage_shiba.ninja", (old, old))
        bp = _mk(self.build_root, "foo/Android.bp", mtime=time.time())
        combined = engine.bypass_ready(self.build_root)
        self.assertIsNone(combined)

    def test_bypass_ready_passes_frozen_tree(self):
        self._mk_graph("lineage_shiba")
        t = time.time() - 3600
        _mk(self.build_root, "foo/Android.bp", mtime=t)
        os.utime(self.out / "soong" / "build.lineage_shiba.ninja", (t, t))
        combined = engine.bypass_ready(self.build_root)
        self.assertIsNotNone(combined)
        self.assertEqual(combined.name, "combined-lineage_shiba.ninja")


class TestDagMemStall(unittest.TestCase):
    """R7: mem-stall gets ONE re-dispatch, then fail red."""

    def test_first_mem_stall_redispatches(self):
        r = dag.next_action({"done": False, "last_classification": "mem-stall",
                             "slice": 3, "mem_stall_retries": 0})
        self.assertEqual(r["phase"], "slice")
        self.assertIn("one re-dispatch", r["reason"])

    def test_second_mem_stall_fails_red(self):
        r = dag.next_action({"done": False, "last_classification": "mem-stall",
                             "slice": 3, "mem_stall_retries": 1})
        self.assertEqual(r["phase"], "fail")
        self.assertIn("mint", r["reason"].lower())

    def test_sliced_still_resumes(self):
        r = dag.next_action({"done": False, "last_classification": "sliced",
                             "slice": 3})
        self.assertEqual(r["phase"], "slice")

    def test_capacity_still_reds(self):
        r = dag.next_action({"done": False, "last_classification": "capacity",
                             "slice": 3})
        self.assertEqual(r["phase"], "fail")

    def test_classify_exit_mem_stall(self):
        self.assertEqual(
            engine.classify_exit(1, True, engine.STOP_MEMORY, 100, 10000),
            "mem-stall")


class TestManifestIntegrity(unittest.TestCase):
    """P0-4: the Frankenstein guard."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="forge-v7-bank-"))
        self.build_root = self.tmp / "aosp"
        self.out = self.build_root / "out"
        self.out.mkdir(parents=True)
        _mk(self.out, "marker.txt")
        self.store = FsStore(self.tmp / "store")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _bank(self, tag: str) -> None:
        n = relay.bank(self.build_root, self.store, tag, "k", 1, notes="t")
        self.assertGreater(n, 0)

    def test_complete_bank_has_manifest_and_restores(self):
        self._bank("state-k-s1")
        verdict = relay.bank_is_complete(self.store, "state-k-s1")
        self.assertEqual(verdict, "manifest")
        self.assertTrue(relay.restore(self.build_root, self.store,
                                      "state-k-s1"))
        self.assertTrue((self.out / "marker.txt").exists())

    def test_frankenstein_bank_is_refused(self):
        """Parts without SHA256SUMS or MANIFEST — the s3 lesion."""
        self._bank("state-k-s2")
        # strip both completion markers, keep the parts
        for asset in ("SHA256SUMS", relay.BANK_MANIFEST):
            p = self.store._dir("state-k-s2") / asset
            if p.exists():
                p.unlink()
        self.assertIsNone(relay.bank_is_complete(self.store, "state-k-s2"))
        self.assertFalse(relay.restore(self.build_root, self.store,
                                       "state-k-s2"))

    def test_mismatched_manifest_is_refused(self):
        """Mixed-run parts: manifest expects a different part-set."""
        self._bank("state-k-s3")
        d = self.store._dir("state-k-s3")
        parts = sorted(p for p in d.iterdir() if ".part." in p.name)
        self.assertGreaterEqual(len(parts), 1)
        # drop one part -> exact-set match fails
        parts[0].unlink()
        self.assertIsNone(relay.bank_is_complete(self.store, "state-k-s3"))
        self.assertFalse(relay.restore(self.build_root, self.store,
                                        "state-k-s3"))

    def test_legacy_sums_bank_still_restores(self):
        """Pre-P0-4 banks (parts + SHA256SUMS, e.g. s2) must keep working."""
        self._bank("state-k-s4")
        (self.store._dir("state-k-s4") / relay.BANK_MANIFEST).unlink()
        self.assertEqual(relay.bank_is_complete(self.store, "state-k-s4"),
                         "legacy")
        self.assertTrue(relay.restore(self.build_root, self.store,
                                      "state-k-s4"))

    def test_gc_purges_orphans_keeps_complete(self):
        self._bank("state-k-s5")
        # make an orphan: parts only
        self._bank("state-k-s6")
        for asset in ("SHA256SUMS", relay.BANK_MANIFEST):
            p = self.store._dir("state-k-s6") / asset
            if p.exists():
                p.unlink()
        dropped = relay.gc_orphan_state_banks(self.store, "k")
        self.assertIn("state-k-s6", dropped)
        self.assertNotIn("state-k-s5", dropped)
        self.assertTrue(self.store.exists("state-k-s5"))

    def test_bank_critical_and_crit_restore(self):
        """P0-4.1: the graph-first flush banks a crit-kind tag that
        restores as graph-only state."""
        soong = self.out / "soong"
        _mk(soong, "build.lineage_shiba.ninja")
        _mk(soong, ".bootstrap/bin_soong_build")
        _mk(self.out, "combined-lineage_shiba.ninja")
        _mk(self.out, ".ninja_log")
        _mk(self.out, ".ninja_deps")
        self.assertTrue(relay.bank_critical(self.build_root, self.store,
                                            "state-k-s7", "k"))
        self.assertEqual(relay.bank_is_complete(self.store, "state-k-s7"),
                         "manifest")
        # wipe out/ and restore the crit bank
        for child in self.out.iterdir():
            shutil.rmtree(child) if child.is_dir() else child.unlink()
        self.assertTrue(relay.restore(self.build_root, self.store,
                                      "state-k-s7"))
        self.assertTrue((self.out / "soong" / "build.lineage_shiba.ninja").exists())
        self.assertFalse((self.out / "marker.txt").exists(),
                         "crit bank carries the graph set, not full state")


class TestMtimeNormalization(unittest.TestCase):
    """P0-3: the deterministic-tree discipline."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="forge-v7-mtime-"))
        self.build_root = self.tmp / "aosp"
        self.build_root.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_stamp_and_normalize_roundtrip(self):
        bp = _mk(self.build_root, "device/foo/Android.bp", mtime=1000.0)
        mf = syncer.stamp_manifest(self.build_root)
        self.assertIsNotNone(mf)
        # mutate: re-stamp to NOW (what every slot's patch/prebuilt phase does)
        now = time.time()
        os.utime(bp, (now, now))
        self.assertGreater(bp.stat().st_mtime, 1001)
        n = syncer.normalize_tree_mtimes(self.build_root, mf)
        self.assertGreaterEqual(n, 1)
        self.assertAlmostEqual(bp.stat().st_mtime, 1000.0, delta=2.0,
                               msg="mtime must be re-stamped to the manifest")

    def test_normalize_rejects_escape_paths(self):
        mf = self.build_root / "manifest.txt"
        mf.write_text("../evil/Android.bp\t1000.0\n/tmp/evil\t2000.0\n"
                      "ok/Android.bp\t3000.0\n", encoding="utf-8")
        ok_bp = _mk(self.build_root, "ok/Android.bp", mtime=time.time())
        n = syncer.normalize_tree_mtimes(self.build_root, mf)
        self.assertEqual(n, 1)  # only the safe path re-stamped
        self.assertAlmostEqual(ok_bp.stat().st_mtime, 3000.0, delta=2.0)

    def test_validate_goal(self):
        from forge_core import graph as fgraph
        t = self.build_root / "out" / "targets.txt"
        t.parent.mkdir(parents=True, exist_ok=True)
        t.write_text("bootimage: phony\nvendorbootimage: phony\n"
                     "out/target/product/shiba/dtbo.img: file\n", encoding="utf-8")
        self.assertIsNone(fgraph.validate_goal(self.build_root, "bootimage"))
        bad = fgraph.validate_goal(self.build_root, "dtboimage")
        self.assertIsNotNone(bad)
        self.assertIn("unknown target", bad)


if __name__ == "__main__":
    unittest.main(verbosity=2)
