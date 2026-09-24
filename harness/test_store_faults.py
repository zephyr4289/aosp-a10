"""Engine D: Mock Store Fault Injection Engine.

Injects simulated network drops, corrupted chunk checksums, and transient failures
to guarantee that retry mechanisms recover seamlessly without human intervention.
"""
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from forge_core import chunker, store


class TestStoreFaults(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="forge-faults-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_transient_download_failure_retry_recovery(self):
        """Simulated transient download failure on first 2 attempts must succeed on 3rd attempt."""
        rel_store = store.ReleaseStore(repo="test/repo")
        dest_dir = self.tmp / "dl"

        # Mock _gh to fail twice then succeed
        call_count = 0
        def mock_gh(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            res = MagicMock()
            if call_count < 3:
                res.returncode = 1
                res.stderr = "HTTP 503 Service Unavailable"
            else:
                res.returncode = 0
                res.stdout = ""
                # Create fake downloaded file
                (dest_dir / "out.part.aa").write_bytes(b"test data")
            return res

        with patch.object(rel_store, "_gh", side_effect=mock_gh), patch("time.sleep", return_value=None):
            files = rel_store.download("tag-v1", "out.part.*", dest_dir)
            self.assertEqual(len(files), 1)
            self.assertEqual(call_count, 3)

    def test_checksum_mismatch_detection(self):
        """Corrupted part hash must be detected by verify()."""
        parts_dir = self.tmp / "corrupt_parts"
        parts_dir.mkdir()
        part = parts_dir / "out.part.aa"
        part.write_bytes(b"corrupted bytes")
        sums = parts_dir / "SHA256SUMS"
        sums.write_text("0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef  out.part.aa\n")

        self.assertFalse(chunker.verify(parts_dir, "out"))


if __name__ == "__main__":
    unittest.main()
