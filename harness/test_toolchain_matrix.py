"""Engine J: Multi-Version Toolchain & Runtime Compatibility Guard.

Validates that every Android version profile in configs/versions.yaml maps to
a supported, tested Ubuntu runner image and apt package set.
"""
import unittest
from pathlib import Path
import yaml


class TestToolchainMatrix(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parent.parent

    def test_version_matrix_images_and_packages(self):
        """Version configs must map to valid runner images (e.g. ubuntu-22.04 or ubuntu-20.04)."""
        ver_file = self.root / "configs" / "versions.yaml"
        self.assertTrue(ver_file.exists())
        with open(ver_file, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)

        self.assertIsInstance(data, dict)
        self.assertGreater(len(data), 0)
        valid_runners = {"ubuntu-20.04", "ubuntu-22.04", "ubuntu-24.04", "ubuntu-latest"}
        for ver_num, conf in data.items():
            if isinstance(conf, dict) and "runner" in conf:
                runner = conf["runner"]
                self.assertIn(runner, valid_runners,
                              f"Version {ver_num} uses unrecognized runner {runner}")
                self.assertIsInstance(conf.get("apt_packages", []), list)


if __name__ == "__main__":
    unittest.main()
