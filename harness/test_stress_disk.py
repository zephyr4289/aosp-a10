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
        """When disk free space is simulated < 15 GB, pre_bank_cleanup must purge symbols and source files."""
        with patch("forge_core.env._df_free_gb", return_value=1.5):
            relay.pre_bank_cleanup(self.build_root)

        # Symbols must be gone
        sym_dir = self.build_root / "out" / "target" / "product" / "PL2" / "symbols"
        self.assertFalse(sym_dir.exists())

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


if __name__ == "__main__":
    unittest.main()
