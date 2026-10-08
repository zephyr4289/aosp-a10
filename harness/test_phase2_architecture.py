"""Engine M: Phase 2 Architecture & Fusion Slot Test Suite.

Verifies:
  1. Graph banking & Soong AST persistence (bank_graph, restore_graph).
  2. Prefetch unpack stream pipeline in chunker.py.
  3. Mid-slice checkpoint snapshotting in storage.py and engine.py.
  4. Fusion slot wall governor loop in cli.py.
"""
from __future__ import annotations

import argparse
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from forge_core import chunker, cli, config, graph, relay, storage, store


class TestPhase2Architecture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="forge-phase2-"))
        self.root = Path(__file__).resolve().parent.parent

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_graph_banking_and_restore(self):
        """Phase 2.4: bank_graph and restore_graph preserve Soong AST graph files."""
        fs = store.FsStore(self.tmp / "store")
        build_root = self.tmp / "aosp"
        soong_dir = build_root / "out" / "soong"
        soong_dir.mkdir(parents=True, exist_ok=True)

        (soong_dir / "build.ninja").write_text("rule cc\n  command = clang\n")
        (soong_dir / ".bootstrap").mkdir()
        (soong_dir / ".bootstrap" / "manifest.json").write_text('{"boot": true}')
        (soong_dir / ".glob").mkdir()
        (soong_dir / ".glob" / "glob.db").write_bytes(b"globdata")

        mhash = "abc12345def67890"
        lunch = "lineage_shiba-userdebug"

        # Bank graph
        ok = graph.bank_graph(build_root, fs, mhash, lunch)
        self.assertTrue(ok)
        tag = graph.graph_tag(mhash, lunch)
        self.assertTrue(fs.exists(tag))

        # Restore into new clean directory
        new_root = self.tmp / "new_aosp"
        restored = graph.restore_graph(new_root, fs, mhash, lunch)
        self.assertTrue(restored)

        new_soong = new_root / "out" / "soong"
        self.assertTrue((new_soong / "build.ninja").exists())
        self.assertEqual((new_soong / "build.ninja").read_text(), "rule cc\n  command = clang\n")
        self.assertTrue((new_soong / ".bootstrap" / "manifest.json").exists())
        self.assertTrue((new_soong / ".glob" / "glob.db").exists())

    def test_checkpoint_snapshot_graceful(self):
        """Phase 2.2: ckpt_snapshot handles non-existent paths and plain mode gracefully."""
        res = storage.ckpt_snapshot(self.tmp / "nonexistent_out")
        self.assertIsNone(res)

        out_dir = self.tmp / "plain_out"
        out_dir.mkdir(parents=True, exist_ok=True)
        res2 = storage.ckpt_snapshot(out_dir)
        # On non-btrfs plain host, returns None without raising
        self.assertIsNone(res2)

    def test_chunker_prefetch_stream_integrity(self):
        """Phase 1.3 / P2: unpack_from_store prefetch queue unpacks multi-part data seamlessly."""
        fs = store.FsStore(self.tmp / "store_prefetch")
        tag = "state-prefetch-test"
        fs.create(tag, "prefetch test", "notes")

        payload_dir = self.tmp / "payload"
        payload_dir.mkdir()
        for i in range(5):
            (payload_dir / f"module_{i}.txt").write_text(f"content {i}\n" * 500)

        parts = chunker.pack(self.tmp, "payload", self.tmp / "parts", "payload",
                             level=1, extra_args=["--long"])
        fs.upload(tag, [self.tmp / "parts" / "SHA256SUMS", *parts])

        dest_dir = self.tmp / "unpacked_payload"
        chunker.unpack_from_store(fs, tag, "payload", dest_dir, prefetch=2)
        for i in range(5):
            self.assertTrue((dest_dir / "payload" / f"module_{i}.txt").exists())

    def test_fusion_loop_wall_governor(self):
        """Phase 2.1: cmd_slice loops consecutively under until_budget and exits cleanly."""
        fs = store.Router(backend="fs", fs_root=self.tmp / "store_fusion")
        rom = config.load_rom(self.root / "configs" / "roms" / "qassa-a10.yaml")
        fs.create("src-fake123", "fake src", "notes")
        fs.target_update(rom.key, src_tag="src-fake123", slice=0, done=False)

        args = argparse.Namespace(
            rom="qassa-a10",
            device=None,
            until_budget=7200,  # 2 hours total job wall
            budget_s=1800,      # 30 min per slice
            force=False,
            mhash="fake123",
            turbo_part=None,
            log=None,
            store="fs:" + str(self.tmp / "store_fusion"),
            repo=None,
            build_root=str(self.tmp / "aosp_fusion"),
            no_volume=True,
        )

        b_root = Path(args.build_root)
        b_root.mkdir(parents=True, exist_ok=True)
        (b_root / ".source_ready").write_text("mhash=fake123\n")
        (b_root / "out").mkdir(parents=True, exist_ok=True)
        (b_root / "out" / ".ninja_log").write_text("# ninja log\n")

        slice_calls = 0

        def mock_run_single(plan, store_obj, build_root_p, budget_s, sub_args, use_ccache):
            nonlocal slice_calls
            slice_calls += 1
            cur_slice = slice_calls
            tag = f"state-{plan.rom.key}-s{cur_slice}"
            fs.target_update(plan.rom.key, slice=cur_slice, state_tag=tag,
                             done=(slice_calls >= 2), last_classification="sliced" if slice_calls < 2 else "done")
            return 0

        with patch("forge_core.cli._ensure_volume", return_value=storage.VolumeState(mode="plain", build_root=str(b_root))), \
             patch("forge_core.syncer.ensure_device_repos"), \
             patch("forge_core.syncer.apply_patches"), \
             patch("forge_core.syncer.validate_lunch"), \
             patch("forge_core.cli._run_single_slice", side_effect=mock_run_single):
            rc = cli.cmd_slice(args, self.root)
            self.assertEqual(rc, 0)
            # Must have executed 2 consecutive slices in the single fusion job run
            self.assertEqual(slice_calls, 2)
            final_t = fs.target(rom.key)
            self.assertTrue(final_t.get("done"))


if __name__ == "__main__":
    unittest.main()
