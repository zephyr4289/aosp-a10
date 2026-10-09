#!/usr/bin/env python3
"""Phase 3 Architecture Validation Suite (Engine N).

Verifies Phase 3 improvements:
1. Self-hosted fleet runner probing & zero-lottery routing in mine.py.
2. Turbo direct stream merging with --skip-old-files.
3. Chunker extra_tar_args support for zero-staging unpacks.
4. Coexistence between cloud-hosted mining and fleet self-hosted runner tiers.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))

import forge_core.chunker as chunker
import forge_core.mine as mine
import forge_core.turbo as turbo
from forge_core.store import FsStore


class TestPhase3Architecture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="forge-test-p3-"))
        self.fs_root = self.tmp / "store"
        self.fs_root.mkdir(parents=True)
        self.store = FsStore(self.fs_root)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -----------------------------------------------------------------------
    # 1. Fleet Runner Fast-Path & Zero-Lottery Mining Tests
    # -----------------------------------------------------------------------
    def test_mine_gate_fleet_flag_claims_instantly(self):
        """When is_fleet=True, gate must immediately claim builder with score 100 without delay."""
        res = mine.gate(self.store, "lock-fleet-test-1", min_score=100, is_fleet=True)
        self.assertEqual(res["role"], "builder")
        self.assertEqual(res["score"], "100")
        self.assertIn("fleet", res["reason"].lower())
        self.assertTrue(self.store.exists("lock-fleet-test-1"))

    def test_mine_gate_fleet_env_variable_claims_instantly(self):
        """When FORGE_FLEET_RUNNER=1 in env, gate must immediately claim builder."""
        with patch.dict(os.environ, {"FORGE_FLEET_RUNNER": "1"}):
            res = mine.gate(self.store, "lock-fleet-test-2", min_score=100)
            self.assertEqual(res["role"], "builder")
            self.assertEqual(res["score"], "100")
            self.assertTrue(self.store.exists("lock-fleet-test-2"))

    def test_mine_gate_fleet_runner_environment_self_hosted(self):
        """When RUNNER_ENVIRONMENT=self-hosted in env, gate must claim builder."""
        with patch.dict(os.environ, {"RUNNER_ENVIRONMENT": "self-hosted"}):
            res = mine.gate(self.store, "lock-fleet-test-3", min_score=100)
            self.assertEqual(res["role"], "builder")
            self.assertEqual(res["score"], "100")

    def test_mine_gate_fleet_candidate_discards_if_peer_already_claimed(self):
        """If a fleet peer already holds the lock, subsequent fleet candidate fast-discards."""
        self.store.create("lock-fleet-test-dup", "lock", "held by peer")
        res = mine.gate(self.store, "lock-fleet-test-dup", min_score=100, is_fleet=True)
        self.assertEqual(res["role"], "discarded")
        self.assertEqual(res["score"], "100")

    # -----------------------------------------------------------------------
    # 2. Chunker extra_tar_args Support Tests
    # -----------------------------------------------------------------------
    def test_chunker_unpack_with_extra_tar_args_skip_old_files(self):
        """chunker.unpack passes extra_tar_args (e.g. --skip-old-files) cleanly to tar."""
        src_dir = self.tmp / "src_payload"
        src_dir.mkdir(parents=True)
        (src_dir / "file_a.txt").write_text("donor_version_a", encoding="utf-8")
        (src_dir / "file_b.txt").write_text("donor_version_b", encoding="utf-8")

        staging = self.tmp / "staging"
        parts = chunker.pack(src_dir, ".", staging, "payload", level=1)

        dest_dir = self.tmp / "dest_payload"
        dest_dir.mkdir(parents=True)
        # Pre-create file_a with base content
        (dest_dir / "file_a.txt").write_text("base_version_a", encoding="utf-8")

        # Unpack with --skip-old-files: file_a must keep base content, file_b must be created
        chunker.unpack(staging, "payload", dest_dir, strip=False,
                       extra_tar_args=["--skip-old-files"])

        self.assertEqual((dest_dir / "file_a.txt").read_text(encoding="utf-8"), "base_version_a")
        self.assertEqual((dest_dir / "file_b.txt").read_text(encoding="utf-8"), "donor_version_b")

    # -----------------------------------------------------------------------
    # 3. Direct Turbo State Stream Merging Tests
    # -----------------------------------------------------------------------
    def test_turbo_merge_turbo_states_direct_stream(self):
        """merge_turbo_states directly unpacks and merges into live out/ without intermediate staging."""
        build_root = self.tmp / "aosp"
        out_dir = build_root / "out" / "target" / "product" / "PL2"
        out_dir.mkdir(parents=True)
        (out_dir / "system.img").write_text("system_data_base", encoding="utf-8")

        # Create a donor turbo state for bootimage
        donor_out = self.tmp / "donor_aosp" / "out" / "target" / "product" / "PL2"
        donor_out.mkdir(parents=True)
        (donor_out / "system.img").write_text("system_data_donor", encoding="utf-8")
        (donor_out / "boot.img").write_text("boot_data_donor", encoding="utf-8")

        staging = self.tmp / "donor_staging"
        # Bank donor into store under tag state-PL2-turbo-bootimage
        tag = "state-PL2-turbo-bootimage"
        self.store.create(tag, "turbo bootimage state")
        parts = chunker.pack(self.tmp / "donor_aosp", "out", staging, "out", level=1)
        self.store.upload(tag, [staging / "SHA256SUMS", *parts])

        # Execute direct turbo merge
        merged_count = turbo.merge_turbo_states(build_root, self.store, "PL2")
        self.assertEqual(merged_count, 1)

        # Verify base system.img preserved while donor boot.img added
        self.assertEqual((out_dir / "system.img").read_text(encoding="utf-8"), "system_data_base")
        self.assertEqual((out_dir / "boot.img").read_text(encoding="utf-8"), "boot_data_donor")

    # -----------------------------------------------------------------------
    # 4. CAS-Relay & Delta Banking Tests (§5.2-S3, §5.3)
    # -----------------------------------------------------------------------
    def test_cas_manifest_generation_and_diff(self):
        """CAS: generate_manifest and diff_manifests accurately track module changes."""
        from forge_core import cas
        out_dir = self.tmp / "cas_out"
        inter_dir = out_dir / "soong" / ".intermediates" / "frameworks" / "base"
        inter_dir.mkdir(parents=True)
        (inter_dir / "classes.jar").write_bytes(b"jar_content_v1")

        # 1. Base manifest
        m1 = cas.generate_manifest(out_dir)
        self.assertIn("soong/.intermediates/frameworks/base", m1)
        self.assertEqual(m1["soong/.intermediates/frameworks/base"]["size"], len(b"jar_content_v1"))

        # 2. Add second module
        inter_dir2 = out_dir / "soong" / ".intermediates" / "services" / "core"
        inter_dir2.mkdir(parents=True)
        (inter_dir2 / "classes.jar").write_bytes(b"jar_content_services")

        m2 = cas.generate_manifest(out_dir)
        delta = cas.diff_manifests(m1, m2)
        self.assertEqual(delta, ["soong/.intermediates/services/core"])

    def test_cas_module_banking_and_restore(self):
        """CAS: bank_cas_modules and restore_cas_modules roundtrip intermediates cleanly."""
        from forge_core import cas
        build_root = self.tmp / "cas_build"
        out_dir = build_root / "out"
        mod_dir = out_dir / "soong" / ".intermediates" / "libart" / "core"
        mod_dir.mkdir(parents=True)
        (mod_dir / "libart.so").write_bytes(b"art_so_binary")

        tag = "cas-shiba-test"
        ok = cas.bank_cas_modules(build_root, self.store, tag, ["soong/.intermediates/libart/core"])
        self.assertTrue(ok)
        self.assertTrue(self.store.exists(tag))

        # Restore into clean build root
        restore_root = self.tmp / "cas_restore"
        res_ok = cas.restore_cas_modules(restore_root, self.store, tag)
        self.assertTrue(res_ok)
        restored_file = restore_root / "out" / "soong" / ".intermediates" / "libart" / "core" / "libart.so"
        self.assertTrue(restored_file.exists())
        self.assertEqual(restored_file.read_bytes(), b"art_so_binary")


if __name__ == "__main__":
    unittest.main()
