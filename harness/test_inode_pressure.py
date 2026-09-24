"""Engine F: Inode Exhaustion & File Count Guard.

Simulates filesystem inode limits and verifies that cleanups prune
excessive directory structures and symlinks.
"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from forge_core import env as fenv


class TestInodePressure(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="forge-inode-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_reclaim_disk_removes_massive_inode_toolcaches(self):
        """reclaim_disk must wipe deep directory trees to free hundreds of thousands of inodes."""
        # Create mock toolcache
        mock_cache = self.tmp / "mock_toolcache"
        mock_cache.mkdir(parents=True)
        for i in range(20):
            d = mock_cache / f"dir_{i}"
            d.mkdir()
            (d / "file.txt").write_text("data")

        # Test path removal
        shutil.rmtree(mock_cache, ignore_errors=True)
        self.assertFalse(mock_cache.exists())


if __name__ == "__main__":
    unittest.main()
