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

    def test_gomemlimit_and_phase_envelopes(self):
        """P1: engine.build_env must provide phase-specific runtime memory envelopes."""
        from forge_core import engine
        plan = config.build_plan(self.root, self.root / "configs" / "roms" / "qassa-a10.yaml")

        # 1. Exec phase (default): JAVA_TOOL_OPTIONS capped, GOMEMLIMIT unset unless overridden
        env_exec = engine.build_env(plan, self.root / "build_test", phase="exec")
        self.assertIn("JAVA_TOOL_OPTIONS", env_exec)
        self.assertIn("-Xmx2560m", env_exec["JAVA_TOOL_OPTIONS"])

        # 2. Bootstrap phase: bounded Go compiler jobs and limit
        env_boot = engine.build_env(plan, self.root / "build_test", phase="bootstrap")
        self.assertEqual(env_boot.get("GOFLAGS"), "-p=2")
        self.assertEqual(env_boot.get("GOMEMLIMIT"), "3GiB")
        self.assertEqual(env_boot.get("GOGC"), "50")

        # 3. Analysis phase: GOMEMLIMIT unset by default (prevents GC thrash), GOGC=400, gctrace on
        env_analysis = engine.build_env(plan, self.root / "build_test", phase="analysis")
        self.assertNotIn("GOMEMLIMIT", env_analysis)
        self.assertEqual(env_analysis.get("GOGC"), "400")
        self.assertEqual(env_analysis.get("GODEBUG"), "gctrace=1")

        # 4. Explicit override honored across phases
        with patch.dict("os.environ", {"FORGE_SOONG_MEM_LIMIT": "12GiB"}):
            env_override = engine.build_env(plan, self.root / "build_test", phase="analysis")
            self.assertEqual(env_override["GOMEMLIMIT"], "12GiB")

    def test_ensure_zram_lifecycle_and_fallback(self):
        """P1: ensure_zram sets up compressed swap device (lz4) and handles fallback gracefully."""
        succ = subprocess.CompletedProcess(["swapon"], 0, stdout="", stderr="")

        def fake_safe_run(cmd, **kwargs):
            return succ

        # Graceful fallback when /dev/zram0 does not exist and modprobe fails
        with patch("os.path.exists", return_value=False), \
             patch("forge_core.env._safe_run", side_effect=fake_safe_run):
            res = fenv.ensure_zram(size_gb=6, algo="lz4")
            self.assertFalse(res)

        # Success path when /dev/zram0 is available and swapon succeeds
        with patch("os.path.exists", return_value=True), \
             patch("forge_core.env._safe_run", side_effect=fake_safe_run), \
             patch("shutil.which", return_value="/usr/sbin/zramctl"):
            res = fenv.ensure_zram(size_gb=6, algo="lz4")
            self.assertTrue(res)

    def test_swap_topology_report_and_summary(self):
        """P1: swap topology report inspects zram and disk swap accurately."""
        fake_swaps = (
            "Filename\t\t\t\tType\t\tSize\t\tUsed\t\tPriority\n"
            "/dev/zram0                              partition\t6291456\t\t102400\t\t100\n"
            "/mnt/romforge/.forge-swap               file\t\t4194304\t\t0\t\t10\n"
        )
        with patch("os.path.exists", return_value=True), \
             patch("pathlib.Path.read_text", return_value=fake_swaps), \
             patch("forge_core.env._active_swap_gb", return_value=10.0):
            report = fenv.swap_topology_report()
            self.assertTrue(report["zram_active"])
            self.assertEqual(report["zram_size_gb"], 6.0)
            self.assertEqual(report["disk_swap_gb"], 4.0)
            self.assertEqual(report["total_swap_gb"], 10.0)

            summary = fenv.swap_topology_summary()
            self.assertIn("zram: 6.0G", summary)
            self.assertIn("disk: 4.0G (p10)", summary)
            self.assertIn("total_swap: 10.0G", summary)

    def test_psi_and_memory_stall_classification(self):
        """P1: PSI reader and mem-stall taxonomy classification."""
        from forge_core import engine

        # 1. Classification: STOP_MEMORY -> mem-stall
        self.assertEqual(
            engine.classify_exit(1, True, engine.STOP_MEMORY, 100, 300),
            "mem-stall"
        )

        # 2. PSI parsing logic
        fake_psi = "some avg10=0.00 avg60=0.50 avg300=0.10 total=1234\nfull avg10=96.50 avg60=95.80 avg300=80.10 total=5678"
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", unittest.mock.mock_open(read_data=fake_psi)):
            psi_val = engine._read_psi()
            self.assertIsNotNone(psi_val)
            self.assertAlmostEqual(psi_val, 95.80)

    def test_cgroup_ladder_generation(self):
        """P1: cgroup run prefix builds correct limits for phases."""
        from forge_core import engine

        # systemd-run fallback check
        with patch("pathlib.Path.exists", return_value=False), \
             patch("shutil.which", return_value="/usr/bin/systemd-run"):
            cmd = engine._cgroup_run_prefix("analysis")
            self.assertIsNotNone(cmd)
            self.assertIn("MemoryMax=14G", " ".join(cmd))
            self.assertIn("MemorySwapMax=12G", " ".join(cmd))


if __name__ == "__main__":
    unittest.main()
