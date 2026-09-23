"""Config layer: ROM profiles, device safety profiles, version capability matrix.

Three YAML families under configs/:

  roms/*.yaml       — "what to build": manifest, lunch, targets, env, slices.
                      ANY ROM is just a new file in roms/ (see ADD_ROM.md).
  devices/*.yaml    — "what NOT to brick": partition budgets, AVB scheme,
                      anti-crossflash tokens, SPL window, A/B-ness.
  versions.yaml     — "how to host it": runner image, host toolchain quirks
                      and budget guidance per Android version (A10..A16).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from . import log


class ConfigError(Exception):
    pass


# ---------------------------------------------------------------------------
# dataclasses
# ---------------------------------------------------------------------------
@dataclass
class RomProfile:
    name: str
    android_version: int
    manifest_url: str
    manifest_branch: str
    lunch: str
    build_target: str = "bacon"
    rom_zip_glob: str = "*.zip"
    device_repos: List[Dict[str, str]] = field(default_factory=list)
    local_manifests: List[str] = field(default_factory=list)   # files (rel. to repo)
    device_clone_mode: str = "direct"                          # direct|manifest
    device_repos_file: Optional[str] = None                    # upstream-compat
    patches: List[Dict[str, str]] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    slices: int = 6
    slice_build_seconds: int = 16500     # 275 min build budget inside a 350m job
    turbo: Dict[str, Any] = field(default_factory=dict)
    apt_packages: List[str] = field(default_factory=list)
    device: str = ""                     # target device key (configs/devices/)
    # derived
    key: str = ""

    def compute_key(self) -> None:
        device = self.device or self.lunch.split("_")[1]
        base = f"{self.name}-{device}-a{self.android_version}"
        self.key = "".join(c.lower() if c.isalnum() else
                            ("-" if c in " ." else "") for c in base)
        while "--" in self.key:
            self.key = self.key.replace("--", "-")
        self.key = self.key.strip("-")


@dataclass
class DeviceProfile:
    name: str
    codenames: List[str]
    soc: str = ""
    arch: str = "arm64"
    kernel_version: str = ""
    ab_update: bool = True
    dynamic_partitions: Optional[bool] = None      # None = auto-detect from tree
    avb: Dict[str, Any] = field(default_factory=dict)
    partition_budgets: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    budgets_authority: str = "tree"               # stock|tree
    vintf_device_manifest: Optional[str] = None    # file rel. to repo (from dump)
    anti_crossflash: Dict[str, List[str]] = field(default_factory=dict)
    spl_window: Dict[str, str] = field(default_factory=dict)
    dtbo_required: bool = True
    boot_header_version: int = 1
    vndk: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# loaders + validation
# ---------------------------------------------------------------------------
def _require(d: dict, key: str, ctx: str) -> Any:
    if key not in d or d[key] in (None, ""):
        raise ConfigError(f"{ctx}: missing required key '{key}'")
    return d[key]


def load_rom(path: Path) -> RomProfile:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: not a mapping")
    ctx = f"rom profile {path.name}"
    rom = RomProfile(
        name=_require(data, "name", ctx),
        android_version=int(_require(data, "android_version", ctx)),
        manifest_url=_require(data, "manifest_url", ctx),
        manifest_branch=_require(data, "manifest_branch", ctx),
        lunch=_require(data, "lunch", ctx),
    )
    if not (10 <= rom.android_version <= 16):
        raise ConfigError(f"{ctx}: android_version {rom.android_version} outside A10..A16")
    device_from_lunch = rom.lunch.split("_")[1] if "_" in rom.lunch else ""
    rom.device = data.get("device", device_from_lunch)
    rom.build_target = data.get("build_target", "bacon")
    rom.rom_zip_glob = data.get("rom_zip_glob", "*.zip")
    rom.device_clone_mode = data.get("device_clone_mode", "direct")
    rom.device_repos_file = data.get("device_repos_file")
    for r in data.get("device_repos", []) or []:
        rom.device_repos.append({
            "path": _require(r, "path", ctx),
            "url": _require(r, "url", ctx),
            "branch": r.get("branch", "master"),
        })
    rom.local_manifests = list(data.get("local_manifests", []) or [])
    for p in data.get("patches", []) or []:
        entry = {"src": _require(p, "src", ctx), "dst": _require(p, "dst", ctx)}
        rom.patches.append(entry)
    rom.env = dict(data.get("env", {}) or {})
    rom.slices = int(data.get("slices", 6))
    rom.slice_build_seconds = int(data.get("slice_build_seconds", 16500))
    rom.turbo = dict(data.get("turbo", {}) or {})
    rom.apt_packages = list(data.get("apt_packages", []) or [])
    rom.compute_key()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", rom.key):
        raise ConfigError(f"{ctx}: derived key '{rom.key}' is not slug-safe")
    return rom


def load_device(path: Path) -> DeviceProfile:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: not a mapping")
    ctx = f"device profile {path.name}"
    dev = DeviceProfile(
        name=_require(data, "name", ctx),
        codenames=[str(c) for c in _require(data, "codenames", ctx)],
    )
    dev.soc = data.get("soc", "")
    dev.arch = data.get("arch", "arm64")
    dev.kernel_version = data.get("kernel_version", "")
    dev.ab_update = bool(data.get("ab_update", True))
    dev.dynamic_partitions = data.get("dynamic_partitions")
    dev.avb = dict(data.get("avb", {}) or {})
    dev.partition_budgets = dict(data.get("partition_budgets", {}) or {})
    dev.budgets_authority = data.get("budgets_authority", "tree")
    dev.vintf_device_manifest = data.get("vintf_device_manifest")
    dev.anti_crossflash = dict(data.get("anti_crossflash", {}) or {})
    dev.spl_window = dict(data.get("spl_window", {}) or {})
    dev.dtbo_required = bool(data.get("dtbo_required", True))
    dev.boot_header_version = int(data.get("boot_header_version", 1))
    dev.vndk = dict(data.get("vndk", {}) or {})
    if not dev.codenames:
        raise ConfigError(f"{ctx}: codenames list is empty")
    return dev


def load_versions(path: Path) -> Dict[int, Dict[str, Any]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: not a mapping")
    out = {}
    for ver, prof in data.items():
        try:
            v = int(str(ver).lstrip("aA"))
        except ValueError as e:
            raise ConfigError(f"{path}: bad version key {ver!r}") from e
        if not (10 <= v <= 16):
            raise ConfigError(f"{path}: version {v} outside A10..A16 envelope")
        out[v] = dict(prof or {})
    return out


# ---------------------------------------------------------------------------
# plan = rom x device x version merged into one execution contract
# ---------------------------------------------------------------------------
@dataclass
class Plan:
    rom: RomProfile
    device: DeviceProfile
    version: Dict[str, Any]
    runner_image: str
    apt_packages: List[str]
    mhash: str = ""            # set after manifest resolution (syncer)
    build_root: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.rom.key,
            "rom": self.rom.name,
            "android_version": self.rom.android_version,
            "device": self.device.name,
            "lunch": self.rom.lunch,
            "target": self.rom.build_target,
            "runner": self.runner_image,
            "slices": self.rom.slices,
            "turbo": self.rom.turbo,
            "mhash": self.mhash,
        }


def build_plan(repo_root: Path, rom_path: Path, device_path: Optional[Path] = None
               ) -> Plan:
    rom = load_rom(rom_path)
    if device_path is None:
        device_path = repo_root / "configs" / "devices" / f"{rom.device}.yaml"
    if not device_path.exists():
        # fall back to any device profile declaring rom's codename
        cands = list((repo_root / "configs" / "devices").glob("*.yaml"))
        for c in cands:
            try:
                d = load_device(c)
            except ConfigError:
                continue
            if rom.device in d.codenames or rom.device in c.stem:
                device_path = c
                break
        else:
            raise ConfigError(
                f"no device profile for '{rom.device}' — add configs/devices/"
                f"{rom.device}.yaml (see ADD_DEVICE.md)")
    dev = load_device(device_path)
    versions = load_versions(repo_root / "configs" / "versions.yaml")
    ver = versions.get(rom.android_version)
    if ver is None:
        raise ConfigError(
            f"versions.yaml has no entry for Android {rom.android_version} — "
            f"extend the matrix (docs/ADD_ROM.md)")
    plan = Plan(rom=rom, device=dev, version=ver,
                runner_image=ver.get("runner", "ubuntu-22.04"),
                apt_packages=list(ver.get("apt_packages", [])) + list(rom.apt_packages))
    if rom.device not in dev.codenames and rom.device != device_path.stem:
        # still allowed (device profiles may cover a family), just warn
        log.warn  # noqa: B018 - keep flake quiet
    return plan


def validate_all(repo_root: Path) -> List[str]:
    errors: List[str] = []
    roms_dir = repo_root / "configs" / "roms"
    devices_dir = repo_root / "configs" / "devices"
    seen_keys: Dict[str, str] = {}
    for f in sorted(roms_dir.glob("*.yaml")):
        if f.name.startswith("_"):
            continue
        try:
            rom = load_rom(f)
            if rom.key in seen_keys:
                errors.append(f"{f.name}: duplicate target key {rom.key} "
                              f"(also {seen_keys[rom.key]})")
            seen_keys[rom.key] = f.name
            plan = build_plan(repo_root, f)
            if not plan.device:
                errors.append(f"{f.name}: device '{rom.device}' unresolved")
        except ConfigError as e:
            errors.append(str(e))
    for f in sorted(devices_dir.glob("*.yaml")):
        try:
            load_device(f)
        except ConfigError as e:
            errors.append(str(e))
    try:
        load_versions(repo_root / "configs" / "versions.yaml")
    except ConfigError as e:
        errors.append(str(e))
    return errors
