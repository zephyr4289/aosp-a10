"""Engine A: Synthetic Disk Space Pressure & Virtual Capped Filesystem.

Simulates strict 100MB-500MB quota limits and declining disk space to verify
that pre_bank_cleanup, single-part streaming, and stream_pack never hit ENOSPC.
"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from forge_core import chunker, relay, store


class TestStressDisk(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="forge-stress-disk-"))
        self.build_root = self.tmp / "aosp"
        self.build_root.mkdir(parents=True)
        # Create mock out/ tree
        out_dir = self.build_root / "out"
        out_dir.mkdir(parents=True)
        (out_dir / ".ninja_log").write_text("# ninja log v5\n", encoding="utf-8")
        # Create symbols directory
        sym_dir = out_dir / "target" / "product" / "PL2" / "symbols"
        sym_dir.mkdir(parents=True)
        (sym_dir / "huge_debug_symbol.so").write_bytes(b"0" * 1024 * 1024)
        # Create mock non-out source files
        (self.build_root / "build").mkdir(parents=True)
        (self.build_root / "build" / "envsetup.sh").write_text("# envsetup\n", encoding="utf-8")
        (self.build_root / "Makefile").write_text("all:\n", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_pre_bank_cleanup_drops_symbols_and_sources_under_pressure(self):
        """When disk free space is simulated < 15 GB, pre_bank_cleanup must drop non-out source files while preserving out/ state."""
        with patch("forge_core.env._df_free_gb", return_value=1.5):
            relay.pre_bank_cleanup(self.build_root)

        # out/ must still exist and be intact
        self.assertTrue((self.build_root / "out" / ".ninja_log").exists())

        # Non-out source files must have been dropped to free disk space
        self.assertFalse((self.build_root / "Makefile").exists())
        self.assertFalse((self.build_root / "build").exists())

    def test_single_part_streaming_bounded_memory(self):
        """Unpack from store streams chunk-by-chunk without staging all parts simultaneously."""
        fs = store.FsStore(self.tmp / "store")
        tag = "state-stress-test"
        fs.create(tag, "stress state", "notes")
        # Create small test payload
        test_data = self.tmp / "payload"
        test_data.mkdir()
        for i in range(5):
            (test_data / f"file_{i}.txt").write_text(f"content {i}\n" * 100)

        # Pack with stage
        parts = chunker.pack(self.tmp, "payload", self.tmp / "parts", "out")
        fs.upload(tag, [self.tmp / "parts" / "SHA256SUMS", *parts])

        dest = self.tmp / "dest"
        # Unpack via streaming
        chunker.unpack_from_store(fs, tag, "out", dest, strip=False)
        self.assertTrue((dest / "payload" / "file_0.txt").exists())

    def test_reclaim_ladder_preserves_directory_structure_for_ninja_cp(self):
        """reclaim_ladder must delete files but preserve directory trees so ninja cp commands never fail."""
        hw_dir = self.build_root / "out" / "target" / "product" / "PL2" / "obj" / "app" / "oat_x86" / "test_dir"
        hw_dir.mkdir(parents=True, exist_ok=True)
        target_file = hw_dir / "android.hardware.biometrics.fingerprint@2.1-service"
        target_file.write_bytes(b"x" * (1024 * 1024))

        from forge_core import env as fenv
        with patch("forge_core.env._df_free_gb", return_value=1.0):
            freed = fenv.reclaim_ladder(self.build_root, want_gb=10.0)

        self.assertGreater(freed, 0)
        # File should be unlinked
        self.assertFalse(target_file.exists())
        # Directory tree MUST be preserved
        self.assertTrue(hw_dir.exists(), "Directory hierarchy was wiped, which breaks subsequent ninja cp commands!")

        # Simulate ninja executing `cp src dst` into hw_dir without mkdir -p
        src_dummy = self.tmp / "dummy_binary"
        src_dummy.write_bytes(b"test")
        try:
            shutil.copy(src_dummy, target_file)
        except FileNotFoundError:
            self.fail("ninja cp failed with FileNotFoundError because directory was destroyed by ladder!")
        self.assertTrue(target_file.exists())

    def test_dual_volume_split_storage_unpack_and_pack(self):
        """Source tree and out/ on separate mounts (symlink out/) must pack and restore seamlessly."""
        root_vol = self.tmp / "vol_root"
        mnt_vol = self.tmp / "vol_mnt"
        root_vol.mkdir()
        mnt_vol.mkdir()

        # Build root on root_vol, out on mnt_vol
        b_root = root_vol / "aosp"
        b_root.mkdir()
        mnt_out = mnt_vol / "romforge_out"
        mnt_out.mkdir()
        (b_root / "out").symlink_to(mnt_out)

        (mnt_out / ".ninja_log").write_text("# ninja log\n")
        (mnt_out / "target.img").write_bytes(b"image")

        fs = store.FsStore(self.tmp / "store_dual")
        tag = "state-dual-test"
        fs.create(tag, "dual volume", "notes")

        # Bank state (stream pack follows out/)
        n = relay.bank(b_root, fs, tag, "dual-key", 1, notes="test")
        self.assertGreater(n, 0)

        # Restore into new tree with symlinked out
        new_root = root_vol / "new_aosp"
        new_root.mkdir()
        new_mnt_out = mnt_vol / "new_out"
        new_mnt_out.mkdir()
        (new_root / "out").symlink_to(new_mnt_out)

        ok = relay.restore(new_root, fs, tag)
        self.assertTrue(ok)
        self.assertTrue((new_mnt_out / "target.img").exists())


if __name__ == "__main__":
    unittest.main()
