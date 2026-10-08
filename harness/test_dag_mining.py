"""Engine L: DAG Conveyor, Silicon Mining & Workflow Wiring Invariants.

The premature-verification bug (runs #30/#34/#35/#36) was a SEMANTIC
error in the workflow wiring, not in Python. So this engine asserts the
decisions in BOTH layers:

  Python layer (pure, exhaustive):
    * dag.next_action: every INDEX state maps to the right phase, and
      capacity/error/exhausted NEVER map to a resume (the anti-loop
      guarantee for the storage deadlock),
    * dag.finalize_classification: done requires a ROM zip,
    * engine.classify_exit: the stop_reason -> classification taxonomy,
    * mine.probe: /proc/cpuinfo scoring for the fleet census models,
    * mine gate: scoreboard fallback never stalls a slot.

  YAML layer (the wiring the forensics actually blamed):
    * forge.yml: verify is gated on postcheck phase == 'verify' (INDEX
      authority), never on slot count alone,
    * forge.yml: postcheck + conveyor jobs exist (the re-dispatch loop
      with an honest halt),
    * forge.yml: slot jobs run the silicon mining gate with an atomic
      lock claim before building,
    * ci-tests.yml: pushes/PRs actually trigger CI (the corrupted
      `branches: ain, master]` bug would have silently disabled it).
"""
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from forge_core import dag, engine, mine            # noqa: E402
from forge_core.store import FsStore, Router        # noqa: E402

try:
    import yaml
    HAVE_YAML = True
except ImportError:                                  # pragma: no cover
    HAVE_YAML = False

WORKFLOWS = ROOT / ".github" / "workflows"


class TestDagDecisionTable(unittest.TestCase):
    """Exhaustive decision table: no INDEX state can resume a deadlock."""

    def test_capacity_never_resumes(self):
        for cls in ("capacity",):
            for slice_n in (1, 5, 12, 23):
                with self.subTest(cls=cls, slice=slice_n):
                    d = dag.next_action({"done": False, "slice": slice_n,
                                         "last_classification": cls})
                    self.assertEqual(d["phase"], "fail")

    def test_error_never_resumes(self):
        d = dag.next_action({"done": False, "slice": 3,
                             "last_classification": "error"})
        self.assertEqual(d["phase"], "fail")

    def test_budget_exhaustion_fails_honestly(self):
        self.assertEqual(
            dag.next_action({"done": False, "slice": 24})["phase"], "fail")
        self.assertEqual(
            dag.next_action({"done": False, "slice": 23})["phase"], "slice")

    def test_sliced_and_cold_resume(self):
        self.assertEqual(dag.next_action({"done": False, "slice": 3,
                                          "last_classification": "sliced"})
                         ["phase"], "slice")
        self.assertEqual(dag.next_action({"done": False, "slice": 0})
                         ["phase"], "slice")

    def test_done_goes_to_verify(self):
        self.assertEqual(dag.next_action({"done": True})["phase"], "verify")
        # done wins even when a stale classification lingers
        self.assertEqual(dag.next_action({"done": True,
                                          "last_classification": "sliced"})
                         ["phase"], "verify")

    def test_done_requires_zip(self):
        self.assertEqual(
            dag.finalize_classification("done", None)["classification"],
            "error")
        self.assertEqual(
            dag.finalize_classification("done", Path("/x/rom.zip"))
            ["classification"], "done")
        self.assertEqual(
            dag.finalize_classification("sliced", None)["classification"],
            "sliced")

    def test_mining_matrix_shapes(self):
        self.assertEqual(dag.mining_matrix(False), ["solo"])
        self.assertEqual(len(dag.mining_matrix(True, 8)), 8)
        self.assertEqual(dag.mining_matrix(True, 8)[0], "c01")
        # cap enforcement: never more rows than the slice budget
        self.assertLessEqual(len(dag.mining_matrix(True, 999)),
                             dag.DEFAULT_MAX_SLICES)

    def test_lock_tag_is_flat_and_run_scoped(self):
        tag = dag.lock_tag("qassapl2a10", "88123155", "3")
        self.assertTrue(re.fullmatch(r"lock-qassapl2a10-r88123155-s3", tag))
        self.assertNotIn("/", tag)  # FsStore list_tags scans one level


class TestEngineTaxonomy(unittest.TestCase):
    def test_full_stop_reason_matrix(self):
        cases = [
            (0, False, "", "done"),
            (1, True, engine.STOP_DISK, "capacity"),
            (1, True, engine.STOP_BUDGET, "sliced"),
            (1, True, engine.STOP_ROOT_DISK, "sliced"),
            (1, False, "", "error"),
            (2, False, "", "error"),
        ]
        for rc, fired, reason, want in cases:
            with self.subTest(rc=rc, reason=reason):
                self.assertEqual(
                    engine.classify_exit(rc, fired, reason, 10, 100), want)

    def test_budget_race_exit_is_sliced(self):
        """SIGINT raced a clean process exit near budget end -> sliced."""
        self.assertEqual(
            engine.classify_exit(1, False, "", 99.5, 100), "sliced")

    def test_capacity_strings_agree_across_layers(self):
        """engine 'capacity' is exactly what dag treats as a halt."""
        cls = engine.classify_exit(1, True, engine.STOP_DISK, 10, 100)
        self.assertEqual(cls, "capacity")
        # ...and dag maps that exact string to a halt, not a resume
        self.assertEqual(
            dag.next_action({"done": False, "slice": 2,
                             "last_classification": cls})["phase"], "fail")


class TestMineScoring(unittest.TestCase):
    def test_fleet_census_models(self):
        cases = [
            # (model, avx512 present, min score, max score)
            ("AMD EPYC 9V45 24-Core Processor", True, 90, 100),    # Zen5 Turin
            ("AMD EPYC 9V74 192-Core Processor", False, 80, 89),   # Zen4c 3.7GHz
            ("AMD EPYC 7763 64-Core Processor", False, 0, 89),     # Zen3 baseline
            ("Intel(R) Xeon(R) Gold 6230R CPU @ 2.10GHz", False, 0, 89),
        ]
        for model, avx512, lo, hi in cases:
            with self.subTest(model=model):
                flags = "avx512f " if avx512 else ""
                ci = (f"processor\t: 0\nmodel name\t: {model}\n"
                      f"cpu MHz\t\t: 3500.0\nflags\t\t: fpu {flags}avx2\n")
                tmp = Path(tempfile.mkdtemp(prefix="forge-mine-"))
                try:
                    p = tmp / "cpuinfo"
                    p.write_text(ci)
                    info = mine.probe(str(p))
                    self.assertGreaterEqual(info["score"], lo)
                    self.assertLessEqual(info["score"], hi)
                    self.assertEqual(info["avx512"], avx512)
                finally:
                    shutil.rmtree(tmp, ignore_errors=True)

    def test_gate_fallback_never_stalls(self):
        """Non-target silicon with no winner -> fallback claim, not a wait."""
        tmp = Path(tempfile.mkdtemp(prefix="forge-gate-"))
        try:
            st = Router(backend="fs", fs_root=tmp / "st")
            real_probe = mine.probe
            try:
                mine.probe = lambda path="/proc/cpuinfo": {
                    "model": "EPYC 7763", "score": 40, "class": "Zen3",
                    "avx512": False, "mhz": 2400, "cores": 4}
                clock = {"t": 0.0}

                def fake_clock():
                    clock["t"] += 5
                    return clock["t"]

                r = mine.gate(st, "lock-L1", key="", min_score=90,
                              wait_s=30, sleep_fn=lambda s: None,
                              clock_fn=fake_clock)
                self.assertEqual(r["role"], "builder")
                self.assertIn("fallback", r["reason"])
            finally:
                mine.probe = real_probe
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_claim_via_router_routes_to_fs_backend(self):
        """Router.claim must route to the active backend (forge wiring)."""
        tmp = Path(tempfile.mkdtemp(prefix="forge-router-"))
        try:
            st = Router(backend="fs", fs_root=tmp / "st")
            self.assertTrue(st.claim("lock-x", "t", "n"))
            self.assertFalse(st.claim("lock-x", "t", "n"))  # lost race
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


@unittest.skipUnless(HAVE_YAML, "PyYAML not installed (pip install pyyaml)")
class TestWorkflowWiring(unittest.TestCase):
    """The four structural fixes must EXIST in the orchestrator YAML."""

    @classmethod
    def setUpClass(cls):
        cls.forge = yaml.safe_load((WORKFLOWS / "forge.yml").read_text())
        cls.ci = yaml.safe_load((WORKFLOWS / "ci-tests.yml").read_text())

    def test_verify_is_phase_gated_not_slot_gated(self):
        v = self.forge["jobs"]["verify"]
        cond = str(v.get("if", ""))
        self.assertIn("postcheck", cond)
        self.assertIn("phase == 'verify'", cond)
        # and it must NOT be an always() unconditioned gate
        self.assertNotIn("always() &&", cond.replace(
            "always() && needs.verify", ""))

    def test_postcheck_job_exists_and_outputs_phase(self):
        pc = self.forge["jobs"]["postcheck"]
        self.assertIn("phase", pc.get("outputs", {}))
        cond = str(pc.get("if", ""))
        self.assertIn("always()", cond)
        # postcheck must NOT require SLOT success (error slots still probe);
        # requiring plan success is correct and expected.
        self.assertIsNone(
            re.search(r"needs\.slot-\d+\.result\s*==\s*'success'", cond),
            f"postcheck must run after red slots: {cond}")

    def test_conveyor_job_exists_for_continuous_mode(self):
        cv = self.forge["jobs"]["conveyor"]
        self.assertIn("phase == 'slice'", str(cv.get("if", "")))
        body = str(cv)
        self.assertIn("gh workflow run", body)      # self re-dispatch

    def test_slots_are_mining_matrices(self):
        for i in range(1, 11):
            job = self.forge["jobs"][f"slot-{i}"]
            matrix = job.get("strategy", {}).get("matrix", {})
            self.assertIn("candidate", matrix,
                          f"slot-{i} must be a mining matrix")
            self.assertGreaterEqual(job.get("timeout-minutes", 0), 340)
            # the mining gate must run BEFORE any build step
            steps = [s.get("name", "") for s in job.get("steps", [])]
            gate_i = next(i for i, s in enumerate(steps)
                          if "mining gate" in s)
            build_i = next(i for i, s in enumerate(steps)
                           if "Build slot" in s)
            self.assertLess(gate_i, build_i, f"slot-{i}: gate before build")

    def test_all_slot_build_steps_gated_on_builder_role(self):
        """Non-builder candidates must never reach the build (fast-discard)."""
        for i in range(1, 11):
            job = self.forge["jobs"][f"slot-{i}"]
            for s in job.get("steps", []):
                if "Build slot" in s.get("name", ""):
                    self.assertIn("build_gate.outputs.role == 'builder'",
                                  str(s.get("if", "")),
                                  f"slot-{i} build step must be role-gated")

    def test_mining_gate_step_has_id(self):
        """The role output must be addressable (id: build_gate)."""
        for i in range(1, 11):
            job = self.forge["jobs"][f"slot-{i}"]
            gate = next(s for s in job.get("steps", [])
                        if "mining gate" in s.get("name", ""))
            self.assertEqual(gate.get("id"), "build_gate", f"slot-{i}")

    def test_ci_triggers_on_main(self):
        """ci-tests.yml must actually fire (the `ain, master]` corruption)."""
        on = self.ci[True] if True in self.ci else self.ci.get("on", {})
        branches = set(on.get("push", {}).get("branches", []))
        self.assertIn("main", branches, f"push branches: {branches}")
        pr = set(on.get("pull_request", {}).get("branches", []))
        self.assertIn("main", pr)

    def test_ci_runs_unit_tests_and_harness_and_selftest(self):
        steps = "\n".join(str(s.get("run", ""))
                          for s in self.ci["jobs"]["tests"]["steps"])
        self.assertIn("tests/run_tests.sh", steps)
        self.assertIn("harness/run_all.py", steps)
        self.assertIn("storage.selftest", steps)


if __name__ == "__main__":
    unittest.main()
