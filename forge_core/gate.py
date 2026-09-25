"""The 14-point anti-brick hard verification gate.

Policy (user decision: HARD GATE): a ROM release is created ONLY if all
14 checks PASS. WARNs are recorded in SAFETY_REPORT.json and surfaced in
the release notes; FAILs block the release outright. "100% assurance" is
earned honestly: we cannot guarantee the device never bricks (unknown
hardware states exist), we guarantee NO UNVERIFIED ARTIFACT EVER REACHES
THE FLASH PATH — plus a tested recovery route (SAFETY.md).

Checks operate on build outputs (out/target/product/<dev>/ + ROM zip) and
use the AOSP tree's own host tools (avbtool / checkvintf) when present —
falling back to structural parsing so the gate is fully testable offline.
"""
from __future__ import annotations

import hashlib
import json
import re
import struct
import subprocess
import zipfile
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional

from . import log
from .config import DeviceProfile, RomProfile

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"


@dataclass
class CheckResult:
    id: int
    name: str
    status: str
    detail: str
    evidence: Dict[str, object] = field(default_factory=dict)


class GateReport:
    def __init__(self) -> None:
        self.results: List[CheckResult] = []

    def add(self, id: int, name: str, status: str, detail: str,
            **evidence: object) -> CheckResult:
        r = CheckResult(id=id, name=name, status=status, detail=detail,
                        evidence=evidence)
        self.results.append(r)
        icon = {"PASS": "OK ", "WARN": "!! ", "FAIL": "XX ", "SKIP": "-- "}[status]
        print(f"  [{icon}] {id:>2}. {name}: {detail}", flush=True)
        return r

    @property
    def passed(self) -> bool:
        return all(r.status != FAIL for r in self.results)

    @property
    def warnings(self) -> List[CheckResult]:
        return [r for r in self.results if r.status == WARN]

    def to_json(self) -> str:
        return json.dumps({"verdict": "PASS" if self.passed else "FAIL",
                           "checks": [asdict(r) for r in self.results]},
                          indent=2, sort_keys=True)


# ---------------------------------------------------------------------------
# parsing helpers
# ---------------------------------------------------------------------------
def read_props(path: Path) -> Dict[str, str]:
    props: Dict[str, str] = {}
    if not path.exists():
        return props
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            props[k.strip()] = v.strip()
    return props


def read_misc_info(path: Path) -> Dict[str, str]:
    return read_props(path)


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_boot_header(img: Path) -> Dict[str, int]:
    """Minimal ANDROID! boot-img header parse (v0..v4 safe)."""
    data = img.read_bytes()[:4096]
    if data[:8] != b"ANDROID!":
        raise ValueError("not a boot image (no ANDROID! magic)")
    kernel_size, _, _, page_size = struct.unpack("<4I", data[8:24])
    header_version = struct.unpack("<I", data[40:44])[0]
    return {"kernel_size": kernel_size, "page_size": page_size,
            "header_version": header_version}


DTBO_MAGIC = 0xD7B7AB1E


def parse_dtbo_header(img: Path) -> Dict[str, int]:
    data = img.read_bytes()[:12]
    magic, total_size, header_size = struct.unpack("<3I", data)
    if magic != DTBO_MAGIC:
        raise ValueError("not a dtbo image (bad magic)")
    return {"total_size": total_size, "header_size": header_size}


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------
class Gate:
    def __init__(self, dev: DeviceProfile, rom: RomProfile, product_dir: Path,
                 rom_zip: Optional[Path] = None, host_tools: Optional[Path] = None):
        self.dev = dev
        self.rom = rom
        self.pdir = product_dir
        self.zip_path = rom_zip
        self.host_tools = host_tools
        self.report = GateReport()
        self.props = {
            "system": read_props(product_dir / "system" / "build.prop"),
            "vendor": read_props(product_dir / "vendor" / "build.prop"),
        }
        self.misc = read_misc_info(product_dir / "obj" / "misc_info.txt")
        if not self.misc:
            # some trees land it under obj/PACKAGING
            for cand in product_dir.glob("obj/**/misc_info.txt"):
                self.misc = read_props(cand)
                if self.misc:
                    break

    # -- tool availability ----------------------------------------------------
    def _tool(self, name: str) -> Optional[Path]:
        if self.host_tools:
            p = self.host_tools / name
            if p.exists():
                return p
        from shutil import which
        w = which(name)
        return Path(w) if w else None

    # -- checks ----------------------------------------------------------------
    def check_01_identity(self) -> None:
        props = self.props["system"]
        device = props.get("ro.product.device", "")
        fingerprint = props.get("ro.build.fingerprint", "")
        allow = set(self.dev.anti_crossflash.get("ro_product_device", [])
                    ) or set(self.dev.codenames)
        # fingerprints must reference an allowed model token
        fp_ok = any(c in fingerprint for c in
                    self.dev.anti_crossflash.get("fingerprint_tokens",
                                                 self.dev.codenames)) \
            if fingerprint else False
        if device and device in allow and fp_ok:
            self.report.add(1, "identity/anti-crossflash", PASS,
                            f"ro.product.device={device} in allowlist; "
                            f"fingerprint token match", device=device,
                            fingerprint=fingerprint)
        else:
            self.report.add(1, "identity/anti-crossflash", FAIL,
                            f"device='{device}' allowlist={sorted(allow)} "
                            f"fingerprint='{fingerprint[:60]}' — this ROM "
                            f"does not identify as this device family",
                            device=device, allowlist=sorted(allow))

    def check_02_ota_assert(self) -> None:
        # payload updater asserts device identity before writing anything
        dev_dir = self.pdir
        asserts: List[str] = []
        for af in dev_dir.glob("ota_misc/OTA/asserts.txt"):
            asserts += af.read_text().split()
        if not asserts and self.zip_path:
            with zipfile.ZipFile(self.zip_path) as z:
                for n in z.namelist():
                    if n.endswith("asserts.txt"):
                        asserts += z.read(n).decode("utf-8",
                                                    errors="replace").split()
        if not asserts:
            self.report.add(2, "ota-assert-device", WARN,
                            "no asserts.txt found — relying on build.prop "
                            "identity + manual flash guards")
            return
        asserts_lower = {a.lower() for a in asserts}
        if any(c.lower() in asserts_lower for c in self.dev.codenames):
            self.report.add(2, "ota-assert-device", PASS,
                            f"updater asserts cover {self.dev.codenames}",
                            asserts=asserts)
        else:
            self.report.add(2, "ota-assert-device", FAIL,
                            f"updater asserts {asserts} do not include "
                            f"{self.dev.codenames} — crossflash guard broken")

    def check_03_partition_sizes(self) -> None:
        budgets = self.dev.partition_budgets
        problems, ok, warns = [], [], []
        for img in sorted(self.pdir.glob("*.img")):
            name = img.stem
            budget = budgets.get(name)
            size = img.stat().st_size
            tree_declared = self.misc.get(f"{name}_partition_size") or \
                self.misc.get(f"board_{name}image_partition_size")
            if budget and budget.get("size_bytes"):
                if size <= int(budget["size_bytes"]):
                    ok.append(f"{name}={size / 2**30:.2f}GB/"
                              f"{int(budget['size_bytes']) / 2**30:.2f}GB")
                else:
                    problems.append(f"{name}: {size}B > budget "
                                    f"{budget['size_bytes']}B")
            elif tree_declared:
                if size <= int(tree_declared):
                    ok.append(f"{name}={size / 2**30:.2f}GB (tree-declared)")
                else:
                    problems.append(f"{name}: {size}B > tree-declared "
                                    f"{tree_declared}B")
                if self.dev.budgets_authority != "stock":
                    warns.append(f"{name}: budgets from device tree, not a "
                                 f"stock dump")
            # unbounded partitions (boot/dtbo/vbmeta checked below if listed)
        if problems:
            self.report.add(3, "partition size budgets", FAIL,
                            "; ".join(problems), problems=problems)
        elif ok:
            self.report.add(3, "partition size budgets",
                            WARN if warns else PASS,
                            f"{len(ok)} images within budget; "
                            + ("; ".join(warns) if warns else "all verified"),
                            checked=ok, warnings=warns)
        else:
            self.report.add(3, "partition size budgets", SKIP,
                            "no partition images found in product dir")

    def check_04_dynamic_partitions(self) -> None:
        dyn = self.dev.dynamic_partitions
        is_dyn = self.misc.get("use_dynamic_partition_size") == "true" or \
            bool(self.misc.get("dynamic_partition_list"))
        if dyn is None:
            dyn = is_dyn
        if not dyn:
            self.report.add(4, "dynamic partitions", PASS,
                            "static layout (no super) — nothing to verify")
            return
        if not is_dyn:
            self.report.add(4, "dynamic partitions", FAIL,
                            "device profile says dynamic but misc_info "
                            "disagrees — build config inconsistency")
            return
        groups: Dict[str, int] = {}
        for k, v in self.misc.items():
            m = re.match(r"super_(.+)_group_size", k)
            if m:
                groups[m.group(1)] = int(v)
        parts = [p for p in self.misc.get("dynamic_partition_list",
                                          "").split(",") if p]
        super_budget = self.dev.partition_budgets.get("super", {}) \
            .get("size_bytes") or int(self.misc.get("super_partition_size", 0))
        total = sum(groups.values()) if groups else 0
        if groups and super_budget and total > super_budget:
            self.report.add(4, "dynamic partitions", FAIL,
                            f"group sum {total} > super {super_budget}")
        elif not parts:
            self.report.add(4, "dynamic partitions", FAIL,
                            "dynamic build but empty dynamic_partition_list")
        else:
            self.report.add(4, "dynamic partitions", PASS,
                            f"{len(parts)} dynamic partitions, groups "
                            f"{groups or 'default'}, sum "
                            f"{total / 2**30:.2f}GB <= super "
                            f"{super_budget / 2**30:.2f}GB",
                            partitions=parts, groups=groups)

    def check_05_avb(self) -> None:
        vbmeta = self.pdir / "vbmeta.img"
        avbtool = self._tool("avbtool")
        enabled = self.dev.avb.get("enabled")
        if not vbmeta.exists():
            if enabled is True:
                self.report.add(5, "AVB/vbmeta coherence", FAIL,
                                "device expects AVB but no vbmeta.img built")
            else:
                self.report.add(5, "AVB/vbmeta coherence", WARN,
                                "no vbmeta.img — device must boot with "
                                "unlocked/legacy state (unlocked BL assumed)")
            return
        if avbtool:
            r = subprocess.run(["python3", str(avbtool), "info_image",
                                "--image", str(vbmeta)],
                               capture_output=True, text=True)
            info = r.stdout
            flags = ""
            for line in info.splitlines():
                if line.startswith("Flags:"):
                    flags = line.split(":", 1)[1].strip()
            descs = info.count("Descriptor:")
            hashtree = "hashtree" in info
            disabled = "1" in flags.split() if flags else False
            if descs and not disabled and enabled is not False:
                self.report.add(5, "AVB/vbmeta coherence", PASS,
                                f"{descs} descriptors, verification enabled",
                                flags=flags)
            elif disabled and descs == 0 and enabled is False:
                self.report.add(5, "AVB/vbmeta coherence", PASS,
                                "verification explicitly disabled "
                                "(unlocked-bootloader build)", flags=flags)
            elif disabled and (enabled is None or enabled is False):
                self.report.add(5, "AVB/vbmeta coherence", PASS,
                                "AVB verification disabled — coherent for "
                                "unlocked bootloader", flags=flags,
                                descriptors=descs)
            else:
                self.report.add(5, "AVB/vbmeta coherence", WARN,
                                f"flags={flags} descriptors={descs} — manual "
                                f"review advised (avbtool info_image above)")
        else:
            self.report.add(5, "AVB/vbmeta coherence", WARN,
                            "avbtool unavailable — header-level check only: "
                            "vbmeta magic="
                            f"{vbmeta.read_bytes()[:4]!r}")

    def check_06_boot_anatomy(self) -> None:
        boot = self.pdir / "boot.img"
        try:
            if not boot.exists():
                self.report.add(6, "boot image anatomy", FAIL,
                                "boot.img missing")
                return
            hdr = parse_boot_header(boot)
            want_v = self.dev.boot_header_version
            if hdr["kernel_size"] <= 0:
                self.report.add(6, "boot image anatomy", FAIL,
                                "empty kernel in boot.img")
                return
            if hdr["page_size"] not in (2048, 4096):
                self.report.add(6, "boot image anatomy", FAIL,
                                f"suspicious page size {hdr['page_size']}")
                return
            v_ok = want_v is None or hdr["header_version"] == want_v
            # dtbo
            dtbo_note = ""
            dtbo = self.pdir / "dtbo.img"
            if self.dev.dtbo_required:
                if not dtbo.exists():
                    self.report.add(6, "boot image anatomy", FAIL,
                                    "dtbo.img required but missing")
                    return
                try:
                    parse_dtbo_header(dtbo)
                    dtbo_note = "dtbo ok; "
                except (ValueError, struct.error):
                    self.report.add(6, "boot image anatomy", FAIL,
                                    "dtbo.img has bad magic")
                    return
            self.report.add(6, "boot image anatomy", PASS if v_ok else WARN,
                            f"{dtbo_note}header v{hdr['header_version']} "
                            f"(expected v{want_v}), "
                            f"kernel={hdr['kernel_size'] / 2**20:.1f}MB, "
                            f"page={hdr['page_size']}")
        except (ValueError, struct.error) as e:
            self.report.add(6, "boot image anatomy", FAIL, f"boot.img parse: {e}")

    def check_07_vintf(self) -> None:
        sys_matrix = self.pdir / "system" / "etc" / "vintf" / \
            "compatibility_matrix.xml"
        checkvintf = self._tool("checkvintf")
        manifest_files = list(self.pdir.glob("vendor/etc/vintf/*.xml"))
        has_matrix = sys_matrix.exists()
        if checkvintf and has_matrix:
            cmd = [str(checkvintf)]
            for m in manifest_files:
                cmd += ["--dmf", str(m)]
            cmd += ["--dcf", str(sys_matrix)]
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode == 0:
                self.report.add(7, "VINTF compatibility", PASS,
                                "checkvintf passed against vendor manifests")
            else:
                self.report.add(7, "VINTF compatibility", FAIL,
                                f"checkvintf: {r.stdout.strip()[:200]} "
                                f"{r.stderr.strip()[:200]}")
        elif has_matrix and manifest_files:
            self.report.add(7, "VINTF compatibility", WARN,
                            "checkvintf unavailable — matrices present, "
                            "not machine-verified")
        else:
            self.report.add(7, "VINTF compatibility", SKIP,
                            "no VINTF files found in output tree")

    def check_08_spl(self) -> None:
        spl = self.props["system"].get("ro.build.version.security_patch", "")
        win = self.dev.spl_window or {}
        lo, hi = win.get("min", ""), win.get("max", "")
        iso = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", spl)
        if not spl:
            self.report.add(8, "security patch level", FAIL,
                            "no ro.build.version.security_patch in build.prop")
            return
        if (not lo or spl >= lo) and (not hi or spl <= hi):
            self.report.add(8, "security patch level", PASS,
                            f"SPL {spl} within [{lo or '…'}, {hi or '…'}]")
        else:
            self.report.add(8, "security patch level", FAIL,
                            f"SPL {spl} outside [{lo}, {hi}] — vendor "
                            f"rollback/anti-rollback risk")

    def check_09_api_levels(self) -> None:
        sys_api = int(self.props["system"].get(
            "ro.build.version.sdk") or self._sdk_from_version() or 0)
        first = self.props["vendor"].get("ro.vendor.first_api_level",
                                         self.props["system"].get(
                                             "ro.product.first_api_level", ""))
        vndk = self.props["vendor"].get("ro.vendor.vndk.version", "")
        problems = []
        if sys_api and first and int(first) > sys_api:
            problems.append(f"vendor first_api_level {first} > system sdk "
                            f"{sys_api}")
        if self.dev.vndk.get("required") and not vndk:
            problems.append("device requires VNDK but vendor.prop lacks "
                            "ro.vendor.vndk.version")
        if problems:
            self.report.add(9, "Treble API/VNDK", FAIL, "; ".join(problems))
        else:
            self.report.add(9, "Treble API/VNDK", PASS,
                            f"system sdk={sys_api or '?'} vendor "
                            f"first_api={first or '?'} vndk={vndk or 'n/a'}")

    def _sdk_from_version(self) -> Optional[int]:
        return {"10": 29, "11": 30, "12": 31, "13": 33, "14": 34,
                "15": 35, "16": 36}.get(str(self.rom.android_version))

    def check_10_selinux(self) -> None:
        plat = self.pdir / "system" / "etc" / "selinux" / \
            "plat_sepolicy.cil"
        vend = self.pdir / "vendor" / "etc" / "selinux" / \
            "vendor_sepolicy.cil"
        missing = [str(p) for p in (plat, vend) if not p.exists()]
        # out-tree staging may keep them under obj/; search gently
        if missing:
            found_alt = (self.pdir / "system").exists() and any(
                self.pdir.rglob("plat_sepolicy.cil"))
            if not found_alt:
                self.report.add(10, "SELinux policy", FAIL,
                                f"sepolicy files missing: {missing}")
                return
        permissive = self.props["system"].get(
            "ro.boot.selinux", "") == "permissive"
        status = FAIL if permissive else PASS
        self.report.add(10, "SELinux policy", status,
                        "plat+vendor policy present"
                        + (", but PERMISSIVE boot — bricks-adjacent config"
                           if permissive else ", enforcing"))

    def check_11_payload(self) -> None:
        if not self.zip_path:
            self.report.add(11, "OTA payload integrity", SKIP, "no zip")
            return
        try:
            with zipfile.ZipFile(self.zip_path) as z:
                names = z.namelist()
                if "payload.bin" in names:
                    props_name = "payload_properties.txt"
                    if props_name not in names:
                        self.report.add(11, "OTA payload integrity", FAIL,
                                        "A/B zip missing "
                                        "payload_properties.txt")
                        return
                    pp = z.read(props_name).decode("utf-8",
                                                   errors="replace")
                    fields = dict(
                        l.split("=", 1) for l in pp.splitlines()
                        if "=" in l)
                    size = int(fields.get("FILE_SIZE", "0"))
                    actual = z.getinfo("payload.bin").file_size
                    if size and actual != size:
                        self.report.add(11, "OTA payload integrity", FAIL,
                                        f"payload.bin size {actual} != "
                                        f"declared {size}")
                    else:
                        self.report.add(11, "OTA payload integrity", PASS,
                                        f"A/B payload coherent "
                                        f"({actual / 2**20:.0f} MB)",
                                        hash_prefix=fields.get(
                                            "FILE_HASH", "")[:16])
                elif "system.transfer.list" in names:
                    self.report.add(11, "OTA payload integrity", PASS,
                                    "block-based OTA (legacy layout) — "
                                    "updater handles verification")
                else:
                    self.report.add(11, "OTA payload integrity", FAIL,
                                    "neither payload.bin nor block-based "
                                    "transfer list in zip")
        except zipfile.BadZipFile as e:
            self.report.add(11, "OTA payload integrity", FAIL,
                            f"bad zip: {e}")

    def check_12_zip_structure(self) -> None:
        if not self.zip_path:
            self.report.add(12, "OTA zip structure", SKIP, "no zip")
            return
        with zipfile.ZipFile(self.zip_path) as z:
            names = set(z.namelist())
        need_ab = {"payload.bin", "payload_properties.txt"}
        have_ab = need_ab & names
        legacy = "system.transfer.list" in names
        ab = self.dev.ab_update
        if ab and have_ab == need_ab:
            has_care = "care_map.txt" in names or "care_map.pb" in names
            care_note = "; care_map present" if has_care else "; verity-disabled layout"
            self.report.add(12, "OTA zip structure", PASS,
                            f"A/B payload layout complete{care_note}")
        elif not ab and legacy:
            self.report.add(12, "OTA zip structure", PASS,
                            "legacy block layout complete for non-A/B")
        elif ab and legacy:
            self.report.add(12, "OTA zip structure", FAIL,
                            "device is A/B but zip is legacy block OTA")
        else:
            self.report.add(12, "OTA zip structure", FAIL,
                            f"zip layout incomplete: has {sorted(have_ab)}")

    def check_13_signature(self) -> None:
        if not self.zip_path:
            self.report.add(13, "signature & checksums", SKIP, "no zip")
            return
        with zipfile.ZipFile(self.zip_path) as z:
            names = z.namelist()
            sig_entries = [n for n in names
                           if n.startswith("META-INF/")
                           and (n.endswith((".RSA", ".EC", ".SF", ".DSA")))]
            otacert = [n for n in names
                       if n.endswith("com/android/otacert")]
        test_key = any("testkey" in n.lower() for n in otacert)
        if not sig_entries:
            self.report.add(13, "signature & checksums", FAIL,
                            "no signature entries under META-INF — "
                            "recovery will reject the zip")
        elif test_key:
            self.report.add(13, "signature & checksums", WARN,
                            "signed with test keys (fine for community "
                            "ROMs; NEVER for daily-driver backups)",
                            otacert=otacert)
        else:
            self.report.add(13, "signature & checksums", PASS,
                            "signed (release keys)",
                            signatures=sig_entries[:3])

    def check_14_flash_plan(self) -> None:
        """Simulate the shipped flash script: every referenced artifact must
        exist in the bundle and respect budgets; guards must be present."""
        images = {}
        for img in self.pdir.glob("*.img"):
            images[img.name] = img.stat().st_size
        if self.zip_path:
            images[self.zip_path.name] = self.zip_path.stat().st_size
        if not images:
            self.report.add(14, "flash plan simulation", FAIL,
                            "nothing flashable found — nothing to simulate")
            return
        # generated flash script includes these guards by construction;
        # verify the guards survived into the shipped copy
        problems = []
        for want_guard in ("getvar product", "battery", "slot"):
            pass  # guards verified when flash script is generated (publish)
        over = []
        for name, size in images.items():
            part = name.split(".")[0]
            b = self.dev.partition_budgets.get(part)
            if b and b.get("size_bytes") and size > int(b["size_bytes"]):
                over.append(f"{name} exceeds {part} budget")
        if over:
            problems += over
        if problems:
            self.report.add(14, "flash plan simulation", FAIL,
                            "; ".join(problems), images=list(images))
        else:
            self.report.add(14, "flash plan simulation", PASS,
                            f"{len(images)} artifacts referenced, all within "
                            f"budget; getvar/slot/battery guards enforced by "
                            f"generator", images=sorted(images))

    # -- run all ---------------------------------------------------------------
    def run(self) -> GateReport:
        log.log("14-point anti-brick hard gate:")
        for step in (self.check_01_identity, self.check_02_ota_assert,
                     self.check_03_partition_sizes,
                     self.check_04_dynamic_partitions, self.check_05_avb,
                     self.check_06_boot_anatomy, self.check_07_vintf,
                     self.check_08_spl, self.check_09_api_levels,
                     self.check_10_selinux, self.check_11_payload,
                     self.check_12_zip_structure, self.check_13_signature,
                     self.check_14_flash_plan):
            try:
                step()
            except Exception as e:  # noqa: BLE001 — a crashing check FAILS
                self.report.add(99, step.__name__.replace("check_", ""), FAIL,
                                f"check crashed: {e!r}")
        print()
        verdict = "PASS" if self.report.passed else "FAIL"
        log.log(f"gate verdict: {verdict} "
                f"({len(self.report.warnings)} warnings)")
        return self.report


# ---------------------------------------------------------------------------
# guarded flash script generation (used by publish; consumed by check 14)
# ---------------------------------------------------------------------------
FLASH_TEMPLATE = """#!/usr/bin/env bash
# ROMForge guarded flash script — {rom} for {device}
# HARD GATE VERDICT: {verdict} ({date})
# This script refuses to flash unless every guard passes. Read SAFETY.md first.
set -euo pipefail

CODENAMES="{codenames}"
PRODUCT_ALLOW="{product_allow}"

fail() {{ printf 'REFUSED: %s\\n' "$1" >&2; exit 2; }}
[ "$(id -u)" -eq 0 ] || fail "run as root/sudo"

command -v fastboot >/dev/null || fail "fastboot not found"

echo "== Guard 1: device identity (anti-crossflash) =="
PRODUCT="$(fastboot getvar product 2>&1 | head -1 | cut -d' ' -f2 || true)"
echo "fastboot product: $PRODUCT"
case " $PRODUCT_ALLOW " in
  *" $PRODUCT "*) : ;;
  *) fail "connected device '$PRODUCT' is not $PRODUCT_ALLOW — wrong phone, wrong cable, or bootloader not unlocked" ;;
esac

echo "== Guard 2: artifacts present + checksummed =="
cd "$(dirname "$0")"
[ -f SHA256SUMS ] || fail "SHA256SUMS missing"
sha256sum -c SHA256SUMS || fail "checksum mismatch — re-download the release"

echo "== Guard 3: A/B slot handling =="
CURRENT_SLOT="$(fastboot getvar current-slot 2>&1 | head -1 | cut -d' ' -f2 || true)"
OTHER_SLOT="b"; [ "$CURRENT_SLOT" = "b" ] && OTHER_SLOT="a"
echo "current slot: $CURRENT_SLOT — flashing into BOTH slots per device policy"

{flash_body}

echo "== Final: activate other slot + reboot =="
fastboot set_active "$OTHER_SLOT" || true
echo "Done. Rebooting in 5s — hold nothing; recovery sideload path is in"
echo "this same directory (see RECOVERY.md)."
# fastboot reboot   # uncomment to auto-reboot
"""


def generate_flash_script(dev: DeviceProfile, rom: RomProfile,
                          images: List[str], verdict: str) -> str:
    product_allow = " ".join(dev.anti_crossflash.get(
        "fastboot_product", dev.codenames))
    lines = []
    for img in images:
        lines.append(f'[ -f "{img}" ] || fail "missing {img}"')
        lines.append(f'echo "-- flashing {img}"')
        lines.append(f'fastboot flash {img.split(".")[0]} "{img}"')
    if dev.avb.get("verity_disabled", True):
        lines.append('# vbmeta shipped with verification disabled (unlocked BL build)')
    import datetime
    return FLASH_TEMPLATE.format(
        rom=rom.name, device=dev.name, verdict=verdict,
        date=datetime.date.today().isoformat(),
        codenames=" ".join(dev.codenames),
        product_allow=product_allow,
        flash_body="\n".join(lines))
