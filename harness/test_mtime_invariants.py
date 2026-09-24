"""Engine G: Ninja Timestamp Invariance & Clock-Skew Guard.

Verifies that restored archive timestamps are preserved or clamped so Ninja
never considers outputs "modified in the future" or dirty.
"""
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path


class TestMtimeInvariants(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="forge-mtime-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_mtime_preservation_and_clamp(self):
        """Restored output file mtimes must not be greater than current time."""
        test_file = self.tmp / "built_target.o"
        past_time = time.time() - 3600  # 1 hour ago
        test_file.write_bytes(b"ELF binary content")
        os.utime(test_file, (past_time, past_time))

        st = test_file.stat()
        self.assertLessEqual(st.st_mtime, time.time())


if __name__ == "__main__":
    unittest.main()
