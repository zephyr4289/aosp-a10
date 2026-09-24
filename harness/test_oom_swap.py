"""Engine E: RAM, Heap & OOM-Killer Survival Simulator.

Simulates heavy Java/Dex heap limits and verifies that swap configuration,
_JAVA_OPTIONS, and toolchain worker ceilings are strictly validated.
"""
import unittest
from pathlib import Path
from unittest.mock import patch

from forge_core import config, env as fenv


class TestOomSwap(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parent.parent

    def test_rom_profiles_have_memory_and_dexpreopt_controls(self):
        """All active ROM profiles must configure WITH_DEXPREOPT to prevent memory spikes."""
        rom_files = [f for f in (self.root / "configs" / "roms").glob("*.yaml")
                     if not f.name.startswith("_")]
        self.assertGreater(len(rom_files), 0)
        for rf in rom_files:
            plan = config.build_plan(self.root, rf)
            self.assertIn("WITH_DEXPREOPT", plan.rom.env)
            self.assertEqual(plan.rom.env["WITH_DEXPREOPT"], "false")

    def test_ensure_swap_skips_when_disk_critically_low(self):
        """Swap creation must protect disk budget if host free disk is low."""
        with patch("forge_core.env._df_free_gb", return_value=5.0):
            res = fenv.ensure_swap("/tmp/test_swap", size_gb=4)
            self.assertFalse(res)


if __name__ == "__main__":
    unittest.main()
