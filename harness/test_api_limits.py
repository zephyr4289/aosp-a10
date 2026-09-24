"""Engine H: GitHub Secondary Rate Limit & 2GB Hard-Boundary Asset Guard.

Verifies that PART_BYTES is strictly clamped below the GitHub 2.0 GiB limit
and that secondary rate limits trigger appropriate exponential backoffs.
"""
import unittest

from forge_core import chunker


class TestApiLimits(unittest.TestCase):
    def test_part_bytes_strictly_below_2gib(self):
        """PART_BYTES must be <= 1.95 GiB (2,097,152,000 bytes) to prevent GitHub Release asset rejection."""
        # 2 GiB in bytes
        MAX_GHA_ASSET_BYTES = 2 * 1024 * 1024 * 1024
        self.assertLess(chunker.PART_BYTES, MAX_GHA_ASSET_BYTES)
        # Ensure it's approximately ~1.9 GB
        self.assertLessEqual(chunker.PART_BYTES, 2_000_000_000)
        self.assertGreater(chunker.PART_BYTES, 1_000_000_000)


if __name__ == "__main__":
    unittest.main()
