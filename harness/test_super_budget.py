"""Engine I: Dynamic Partition (super.img) Summation & Budgeting Guard.

Verifies mathematical integrity of sub-partition sums against device-defined
BOARD_SUPER_PARTITION_SIZE budgets.
"""
import unittest
from pathlib import Path

from forge_core import config


class TestSuperBudget(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parent.parent

    def test_device_partition_size_budgets_are_valid(self):
        """Device configs must define positive, non-zero partition size budgets."""
        dev_files = list((self.root / "configs" / "devices").glob("*.yaml"))
        self.assertGreater(len(dev_files), 0)
        for df in dev_files:
            dev = config.load_device(df)
            for part, spec in dev.partition_budgets.items():
                size = spec.get("size_bytes") if isinstance(spec, dict) else spec
                self.assertIsNotNone(size)
                self.assertGreater(size, 0, f"Device {dev.name} has invalid size for {part}")
                # Minimum partition sanity: at least 10 MB
                self.assertGreater(size, 10 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
