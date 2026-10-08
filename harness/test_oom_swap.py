"""Engine E: RAM, Heap & OOM-Killer Survival Simulator.

Simulates heavy Java/Dex heap limits and verifies that swap configuration,
_JAVA_OPTIONS, and toolchain worker ceilings are strictly validated.
"""
import subprocess
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
        # Hermetic: simulate a host with NO active swap. GitHub runners ship
        # with /mnt/swapfile already swapon'd, which short-circuits
        # ensure_swap() to True before the disk-budget branch can run. This
        # only surfaced once ci-tests.yml triggers were repaired and the
        # test executed on a real runner for the first time.
        no_swap = subprocess.CompletedProcess(["swapon", "--show"], 0,
                                              stdout="", stderr="")

        def fake_safe_run(cmd, **kwargs):
            return no_swap if cmd and cmd[0] == "swapon" else None

        with patch("forge_core.env._df_free_gb", return_value=5.0), \
             patch("forge_core.env._safe_run", side_effect=fake_safe_run):
            res = fenv.ensure_swap("/tmp/test_swap", size_gb=4)
        self.assertFalse(res)

    def test_dynamic_swap_chunk_lifecycle(self):
        """Dynamic swap chunks must activate under sufficient disk and deactivate cleanly."""
        succ = subprocess.CompletedProcess(["swapon"], 0, stdout="", stderr="")

        def fake_safe_run(cmd, **kwargs):
            return succ

        # Test activation skips if free disk is too low
        with patch("forge_core.env._df_free_gb", return_value=5.0), \
             patch("forge_core.env._safe_run", side_effect=fake_safe_run):
            res = fenv.activate_swap_chunk("/tmp/chunk1", chunk_size_gb=2)
        self.assertFalse(res)

        # Test activation succeeds when free disk is healthy
        with patch("forge_core.env._df_free_gb", return_value=30.0), \
             patch("forge_core.env._safe_run", side_effect=fake_safe_run), \
             patch("os.path.exists", return_value=True), \
             patch("os.path.getsize", return_value=2 * 1024 * 1024 * 1024):
            res = fenv.activate_swap_chunk("/tmp/chunk1", chunk_size_gb=2)
        self.assertTrue(res)

    def test_gomemlimit_and_soong_memory_controls(self):
        """F11 / 1.5: engine.build_env must set GOMEMLIMIT to shield runner memory from Soong OOM spikes."""
        from forge_core import engine
        plan = config.build_plan(self.root, self.root / "configs" / "roms" / "qassa-a10.yaml")
        env = engine.build_env(plan, self.root / "build_test")
        self.assertIn("GOMEMLIMIT", env)
        self.assertEqual(env["GOMEMLIMIT"], "11GiB")

        # Verify environment override knob
        with patch.dict("os.environ", {"FORGE_SOONG_MEM_LIMIT": "12GiB"}):
            env2 = engine.build_env(plan, self.root / "build_test")
            self.assertEqual(env2["GOMEMLIMIT"], "12GiB")

    def test_ensure_zram_lifecycle_and_fallback(self):
        """F11 / 1.5: ensure_zram sets up compressed swap device and handles fallback gracefully."""
        succ = subprocess.CompletedProcess(["swapon"], 0, stdout="", stderr="")

        def fake_safe_run(cmd, **kwargs):
            return succ

        # Graceful fallback when /dev/zram0 does not exist and modprobe fails
        with patch("os.path.exists", return_value=False), \
             patch("forge_core.env._safe_run", side_effect=fake_safe_run):
            res = fenv.ensure_zram(size_gb=8)
            self.assertFalse(res)

        # Success path when /dev/zram0 is available and swapon succeeds
        with patch("os.path.exists", return_value=True), \
             patch("forge_core.env._safe_run", side_effect=fake_safe_run), \
             patch("shutil.which", return_value="/usr/sbin/zramctl"):
            res = fenv.ensure_zram(size_gb=8)
            self.assertTrue(res)


if __name__ == "__main__":
    unittest.main()
