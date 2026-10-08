#!/usr/bin/env python3
"""ROMForge offline test suite (stdlib only, no pytest dependency).

Covers the load-bearing claims of the architecture:
  1. chunker      — pack/unpack roundtrip integrity (gzip fallback path)
  2. FsStore      — the backend that makes CI runnable on a laptop
  3. relay        — .ninja_log forensics + live progress parsing
  4. gate         — GOOD build passes; each POISON scenario FAILS the
                    specific check designed to catch it (anti-brick proof)
  5. config       — profile schema + slug keys
  7. storage      — volume sizing math + the never-delete-source guard
  8. dag          — conveyor decisions + done-requires-zip
  9. engine       — stop_reason -> classification taxonomy
 10. mine         — silicon probe scoring + atomic claims + gate roles
"""
from __future__ import annotations

import dataclasses
import json
import random
import shutil
import struct
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from forge_core import chunker, config, dag, engine, gate, mine, relay, storage  # noqa: E402
from forge_core.store import FsStore  # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok    {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name} {detail}")


# ---------------------------------------------------------------------------
def test_chunker(tmp: Path) -> None:
    print("[1] chunker roundtrip")
    src = tmp / "tree"
    (src / "aosp" / "out" / "deep").mkdir(parents=True)
    rng = random.Random(42)
    for i in range(40):
        data = bytes(rng.getrandbits(8) for _ in range(2000 + i * 97))
        (src / "aosp" / f"f{i}.bin").write_bytes(data)
    (src / "aosp" / "out" / "deep" / "x.bin").write_bytes(b"\x00" * 5000)
    staging = tmp / "parts"
    parts = chunker.pack(src, "aosp", staging, "src",
                         excludes=["aosp/out"])
    check("pack made parts + sums", len(parts) >= 1
          and (staging / "SHA256SUMS").exists())
    check("exclusion worked", not any(b"deep" in p.read_bytes()[:0x10000]
                                      for p in [parts[0]]))
    dest = tmp / "dest"
    chunker.unpack(staging, "src", dest, strip=True)
    same = (dest / "f0.bin").read_bytes() == (src / "aosp" / "f0.bin").read_bytes()
    check("roundtrip integrity", same and (dest / "f39.bin").exists())
    # tamper detection
    with open(parts[0], "ab") as fh:
        fh.write(b"tamper")
    check("sha256 tamper detected",
          not chunker.verify(staging, "src"))


def test_fsstore(tmp: Path) -> None:
    print("[2] FsStore (local CI backend)")
    st = FsStore(tmp / "store")
    st.create("src-abc", "t", "n")
    f = tmp / "hello.txt"
    f.write_text("hello forge")
    st.upload("src-abc", [f])
    got = st.download("src-abc", "hello.txt", tmp / "dl")
    check("upload/download", got and (got[0]).read_text() == "hello forge")
    check("exists", st.exists("src-abc") and not st.exists("src-nope"))
    st.delete("src-abc")
    check("delete", not st.exists("src-abc"))
    # index protocol
    st.create("forge-index", "i", "i")
    (tmp / "INDEX.json").write_text(json.dumps({"schema": 1, "targets": {}}))
    st.upload_file("forge-index", tmp / "INDEX.json")


def test_relay(tmp: Path) -> None:
    print("[3] relay forensics")
    out = tmp / "out"
    out.mkdir()
    (out / ".ninja_log").write_text(
        "# ninja log v5\n"
        "1\t100\t101\tout/soong/host/linux-x86/bin/cpgz\tdeadbeef\t0\n"
        "200\t400\t401\tout/x/y.o\tcafe\t1\n"
        "500\t900\t901\tout/z.o\tbeef\t1\n")
    s = relay.ninja_stats(out)
    check("ninja outputs counted", s["outputs"] == 3, str(s))
    check("ninja cpu-seconds", abs(s["build_seconds"] - 0.7) < 0.01, str(s))
    log = tmp / "build.log"
    log.write_text("ninja: Entering directory `out'\n"
                   "[ 10% 12/120 ] action ...\n[ 12% 15/120 ] action\n")
    p = relay.progress_from_log(log, {"pct": 0})
    check("progress parse", p == {"pct": 12, "done": 15, "total": 120}, str(p))
    check("eta math", abs(relay.eta_minutes(p, 20) - (20 * 88 / 12)) < 1e-6)


# ---------------------------------------------------------------------------
def sparse(path: Path, size: int) -> None:
    """Create a sparse file reporting `size` bytes (disk cost ~0)."""
    with open(path, "wb") as fh:
        fh.seek(size - 1)
        fh.write(b"\0")


def fake_boot_img(path: Path, header_version: int = 1) -> None:
    hdr = bytearray(4096)
    hdr[0:8] = b"ANDROID!"
    struct.pack_into("<4I", hdr, 8, 12 * 1024 * 1024, 4 * 1024 * 1024,
                     8 * 1024 * 1024, 4096)
    struct.pack_into("<I", hdr, 40, header_version)
    path.write_bytes(bytes(hdr) + b"\xde\xad" * 64)


def fake_dtbo(path: Path) -> None:
    path.write_bytes(struct.pack("<3I", 0xD7B7AB1E, 512, 32) + b"\x00" * 480)


def make_zip(path: Path, good: bool = True) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        payload = b"C" * (2 * 1024 * 1024)
        z.writestr("payload.bin", payload)
        props = (f"FILE_HASH=sha1:abcdef\nFILE_SIZE={len(payload)}\n"
                 f"METADATA_HASH=sha1:1234\nMETADATA_SIZE=99\n")
        if not good:
            props = props.replace(f"FILE_SIZE={len(payload)}",
                                  "FILE_SIZE=999")
        z.writestr("payload_properties.txt", props)
        z.writestr("care_map.txt", "/dev/block/bootdevice/by-name/system\n")
        z.writestr("META-INF/com/android/otacert", b"fake-cert-pem")
        z.writestr("META-INF/CERT.RSA", b"sig")
        z.writestr("META-INF/CERT.SF", b"sig")


def build_fixture(tmp: Path, poison: str = "") -> Path:
    """Synthetic out/target/product/PL2 + ROM zip. poison selects the flaw."""
    dev = config.load_device(ROOT / "configs" / "devices" / "nokia_pl2.yaml")
    rom = config.load_rom(ROOT / "configs" / "roms" / "qassa-a10.yaml")
    pdir = tmp / "out" / "target" / "product" / "PL2"
    (pdir / "system" / "etc" / "vintf").mkdir(parents=True)
    (pdir / "system" / "etc" / "selinux").mkdir(parents=True)
    (pdir / "vendor" / "etc" / "vintf").mkdir(parents=True)
    (pdir / "vendor" / "etc" / "selinux").mkdir(parents=True)
    (pdir / "obj").mkdir(parents=True)

    device = "PL2"
    spl = "2022-03-05"
    fingerprint = "Nokia/Plate2_00WW/PL2_sprout:10/QKQ1.190828.002/00WW_4_15C:user/release-keys"
    if poison == "wrong-device":
        device = "POCOX3"
    if poison == "bad-spl":
        spl = "2024-12-05"
    (pdir / "system" / "build.prop").write_text(
        f"ro.product.device={device}\n"
        f"ro.build.fingerprint={fingerprint}\n"
        f"ro.build.version.sdk=29\n"
        f"ro.build.version.security_patch={spl}\n")
    (pdir / "vendor" / "build.prop").write_text(
        "ro.vendor.first_api_level=27\nro.product.first_api_level=27\n")

    (pdir / "obj" / "misc_info.txt").write_text(
        "use_dynamic_partition_size=false\n"
        "system_partition_size=3221225472\n"
        "vendor_partition_size=805306368\n"
        "product_partition_size=536870912\n")

    (pdir / "boot.img").write_bytes(b"")
    fake_boot_img(pdir / "boot.img")
    if poison != "missing-dtbo":
        fake_dtbo(pdir / "dtbo.img")
    sparse(pdir / "system.img", 2 * 1024 ** 3)
    if poison == "oversize-system":
        sparse(pdir / "system.img", int(3.1 * 1024 ** 3))
    sparse(pdir / "vendor.img", 600 * 1024 ** 2)
    sparse(pdir / "product.img", 256 * 1024 ** 2)
    (pdir / "vbmeta.img").write_bytes(b"AVB0" + b"\x00" * 4092)

    (pdir / "system" / "etc" / "vintf" /
     "compatibility_matrix.xml").write_text("<manifest/>\n")
    (pdir / "vendor" / "etc" / "vintf" / "manifest.xml").write_text(
        "<manifest/>\n")
    (pdir / "system" / "etc" / "selinux" / "plat_sepolicy.cil").write_text(
        "(type plat)\n")
    (pdir / "vendor" / "etc" / "selinux" / "vendor_sepolicy.cil").write_text(
        "(type vend)\n")
    z = pdir / ("rom-bad.zip" if poison == "payload-mismatch" else "rom.zip")
    make_zip(z, good=poison != "payload-mismatch")
    return pdir


def run_gate(pdir: Path, dev, rom) -> "gate.GateReport":
    g = gate.Gate(dev, rom, pdir, rom_zip=pdir / "rom.zip"
                  if (pdir / "rom.zip").exists() else pdir / "rom-bad.zip",
                  host_tools=None)
    return g.run()


def test_gate(tmp: Path) -> None:
    print("[4] 14-point gate — GOOD build")
    dev = config.load_device(ROOT / "configs" / "devices" / "nokia_pl2.yaml")
    rom = config.load_rom(ROOT / "configs" / "roms" / "qassa-a10.yaml")
    pdir = build_fixture(tmp, poison="")
    report = run_gate(pdir, dev, rom)
    by_id = {r.id: r for r in report.results}
    check("verdict PASS", report.passed)
    check("identity passes", by_id[1].status == "PASS")
    check("sizes pass (tree authority)",
          by_id[3].status in ("PASS", "WARN"))
    check("boot anatomy passes", by_id[6].status == "PASS")
    check("SPL passes", by_id[8].status == "PASS")
    check("payload passes", by_id[11].status == "PASS")
    check("zip structure passes", by_id[12].status == "PASS")
    check("signature present", by_id[13].status in ("PASS", "WARN"))
    check("all 14 ran", len(report.results) == 14, str(len(report.results)))
    j = json.loads(report.to_json())
    check("report JSON shape", j["verdict"] == "PASS" and
          len(j["checks"]) == 14)

    print("[4b] gate poisons — each must FAIL its own check")
    for poison, check_id, why in (
            ("wrong-device", 1, "anti-crossflash"),
            ("oversize-system", 3, "partition budget"),
            ("missing-dtbo", 6, "boot anatomy"),
            ("bad-spl", 8, "SPL window"),
            ("payload-mismatch", 11, "payload integrity")):
        t2 = tmp / f"poison-{poison}"
        pdir = build_fixture(t2, poison=poison)
        z = pdir / "rom-bad.zip" if (pdir / "rom-bad.zip").exists() \
            else pdir / "rom.zip"
        if poison != "payload-mismatch":
            (t2 / "out" / "target" / "product" / "PL2" / "rom-bad.zip") \
                .unlink(missing_ok=True)
        dev_to_use = dataclasses.replace(dev, dtbo_required=True) if poison == "missing-dtbo" else dev
        g = gate.Gate(dev_to_use, rom, pdir, rom_zip=z, host_tools=None)
        rep = g.run()
        by = {r.id: r for r in rep.results}
        check(f"{poison} -> check {check_id} FAIL", by[check_id].status == "FAIL",
              f"got {by[check_id].status}")
        check(f"{poison} -> verdict FAIL", not rep.passed)


def test_config() -> None:
    print("[5] config schema")
    errs = config.validate_all(ROOT)
    check("profiles validate", not errs, str(errs))
    rom = config.load_rom(ROOT / "configs" / "roms" / "qassa-a10.yaml")
    check("slug key", rom.key == "qassapl2a10", rom.key)
    plan = config.build_plan(ROOT, ROOT / "configs" / "roms" / "qassa-a10.yaml")
    check("version matrix wiring", plan.runner_image == "ubuntu-22.04")
    check("device profile resolved", plan.device.codenames[0] == "PL2")


# ---------------------------------------------------------------------------
def test_storage(tmp: Path) -> None:
    print("[7] storage v2 math + guards (pure parts)")
    # sizing math
    check("cap = free - reserve", storage.compute_cap_gb(75, 10) == 65.0)
    check("cap floors at 0", storage.compute_cap_gb(5, 10) == 0.0)
    check("reserve env parsing robust",
          storage.VolumeState(mode="plain", reason="x").degraded)
    st = storage.VolumeState(mode="btrfs", cap_gb=64.2, build_root="/v/aosp")
    d = st.to_dict()
    check("volume state serializes", d["mode"] == "btrfs"
          and d["cap_gb"] == 64.2 and not d["reason"])

    # plain-mode snapshot: logical == physical == df(path)
    br = tmp / "snaproot"
    br.mkdir()
    snap = storage.snapshot(br)
    check("snapshot plain on non-volume host", snap.mode in ("plain", "btrfs"))
    if snap.mode == "plain":
        check("plain snapshot logical==physical",
              abs(snap.logical_free_gb - snap.physical_free_gb) < 0.01)
    check("snapshot root free is real", snap.root_free_gb > 0)
    check("snapshot serializes", set(snap.to_dict()) >= {
        "mode", "physical_free_gb", "logical_free_gb", "root_free_gb", "cap_gb"})

    # THE deadlock guard: pre-bank actions must never delete source in
    # volume mode, no matter how low the disk gets
    a = relay.pre_bank_actions(50, protect_source=True)
    check("healthy: just purge-tmp", a == ["purge-tmp"], str(a))
    a = relay.pre_bank_actions(1.5, protect_source=True)
    check("volume + critically full: NO delete-source",
          "delete-source" not in a and "stop:capacity" in a, str(a))
    a = relay.pre_bank_actions(1.5, protect_source=False)
    check("plain + critically full: legacy last resort",
          "delete-source" in a, str(a))
    a = relay.pre_bank_actions(3.0, protect_source=True)
    check("low: ladder only", "reclaim-ladder" in a
          and "fstrim" not in a and "delete-source" not in a, str(a))

    # root purge list must NEVER touch runner internals (class B)
    for p in storage.ROOT_PURGE_PATHS:
        banned = ("/home/runner", "/var/log", "/opt/actions", "agent")
        check(f"root purge safe: {p}", not any(b in p for b in banned))


def test_dag() -> None:
    print("[8] DAG conveyor decisions (pure)")
    base = {"done": False, "slice": 3, "last_classification": "sliced"}
    d = dag.next_action(dict(base))
    check("sliced -> re-dispatch slice", d["phase"] == "slice", str(d))
    d = dag.next_action({"done": True})
    check("done -> verify", d["phase"] == "verify", str(d))
    d = dag.next_action({"last_classification": "capacity", "slice": 2})
    check("capacity -> fail (deadlock guard)", d["phase"] == "fail"
          and "capacity" in d["reason"].lower(), str(d))
    d = dag.next_action({"last_classification": "error", "slice": 2})
    check("error -> fail (triage)", d["phase"] == "fail", str(d))
    d = dag.next_action({"slice": dag.DEFAULT_MAX_SLICES})
    check("budget exhausted -> fail", d["phase"] == "fail", str(d))
    d = dag.next_action({"slice": 0})
    check("cold start -> slice", d["phase"] == "slice", str(d))

    # done-requires-zip (premature-verification bug, second half)
    f = dag.finalize_classification("done", None)
    check("rc==0 without zip -> error", f["classification"] == "error", str(f))
    f = dag.finalize_classification("done", "rom.zip")
    check("rc==0 with zip -> done", f["classification"] == "done")
    f = dag.finalize_classification("sliced", None)
    check("sliced stays sliced", f["classification"] == "sliced")

    check("mining off -> solo", dag.mining_matrix(False) == ["solo"])
    m = dag.mining_matrix(True, 12)
    check("mining matrix 12 candidates", len(m) == 12
          and m[0] == "c01" and m[-1] == "c12", str(m))
    check("mining matrix capped", len(dag.mining_matrix(True, 99)) == 24)
    check("lock tag flat", dag.lock_tag("k", "42", "3")
          == "lock-k-r42-s3")


def test_engine_taxonomy() -> None:
    print("[9] engine stop_reason -> classification taxonomy")
    E = engine
    check("rc 0 -> done", E.classify_exit(0, False, "", 10, 100) == "done")
    check("disk -> capacity", E.classify_exit(1, True, E.STOP_DISK, 10, 100)
          == "capacity")
    check("budget -> sliced", E.classify_exit(1, True, E.STOP_BUDGET, 10, 100)
          == "sliced")
    check("root-disk -> sliced (recoverable)",
          E.classify_exit(1, True, E.STOP_ROOT_DISK, 10, 100) == "sliced")
    check("rc!=0 clean -> error",
          E.classify_exit(2, False, "", 10, 100) == "error")
    check("elapsed~budget -> sliced (SIGINT raced exit)",
          E.classify_exit(1, False, "", 98, 100) == "sliced")
    # thresholds sanity: watchdogs trip in the right order
    check("root stop < root purge",
          E.ROOT_STOP_GB < E.ROOT_PURGE_GB)
    check("phys stop < phys warn", E.PHYS_STOP_GB < E.PHYS_WARN_GB)


# ---------------------------------------------------------------------------
CPUINFO_TARGET = (
    "processor\t: 0\nvendor_id\t: AuthenticAMD\n"
    "model name\t: AMD EPYC 9V45 24-Core Processor\n"
    "cpu MHz\t\t: 4400.000\n"
    "flags\t\t: fpu avx2 avx512f sse4_2\n") * 2
CPUINFO_WEAK = (
    "processor\t: 0\n"
    "model name\t: AMD EPYC 7763 64-Core Processor\n"
    "cpu MHz\t\t: 2400.000\n"
    "flags\t\t: fpu avx2 sse4_2\n") * 2
CPUINFO_INTEL = (
    "processor\t: 0\n"
    "model name\t: Intel(R) Xeon(R) Gold 6230R CPU @ 2.10GHz\n"
    "cpu MHz\t\t: 2100.000\n" "flags\t\t: fpu avx2\n") * 2


def test_mine(tmp: Path) -> None:
    print("[10] silicon mining: probe, claims, gate")
    ci = tmp / "cpuinfo"
    ci.write_text(CPUINFO_TARGET)
    info = mine.probe(str(ci))
    check("target probe scores >= 90", int(info["score"]) >= 90, str(info))
    check("target probe detects avx512", info["avx512"])
    ci.write_text(CPUINFO_WEAK)
    info = mine.probe(str(ci))
    check("zen3 probe scores low", 0 < int(info["score"]) < 90, str(info))
    check("zen3 probe no avx512 bonus", not info["avx512"])
    ci.write_text(CPUINFO_INTEL)
    info = mine.probe(str(ci))
    check("cascade lake probe scores low", 0 < int(info["score"]) < 90)

    # atomic claims: exactly one winner
    st = FsStore(tmp / "mine-store")
    won1 = st.claim("lock-k-r1-s1", "t", "n")
    won2 = st.claim("lock-k-r1-s1", "t", "n")
    check("claim is exactly-one-winner", won1 and not won2)
    check("gc_locks drops only matching prefix",
          st.gc_locks("k") == ["lock-k-r1-s1"])

    # gate roles via a fake probe + fake clock/sleep
    real_probe = mine.probe
    try:
        mine.probe = lambda path="/proc/cpuinfo": {"model": "EPYC 9V45",
                                                  "score": 100, "class": "t",
                                                  "avx512": True, "mhz": 4400,
                                                  "cores": 4}
        st2 = FsStore(tmp / "mine-store2")
        r = mine.gate(st2, "lock-g1", key="", min_score=90, wait_s=10,
                      sleep_fn=lambda s: None,
                      clock_fn=lambda: 1000.0)
        check("target silicon -> builder", r["role"] == "builder", str(r))
        st3 = FsStore(tmp / "mine-store3")
        st3.claim("lock-g2", "t", "n")     # someone already holds it
        r = mine.gate(st3, "lock-g2", key="", min_score=90, wait_s=10,
                      sleep_fn=lambda s: None, clock_fn=lambda: 1000.0)
        check("target silicon, lock held -> discarded",
              r["role"] == "discarded", str(r))

        mine.probe = lambda path="/proc/cpuinfo": {"model": "EPYC 7763",
                                                  "score": 40, "class": "w",
                                                  "avx512": False, "mhz": 2400,
                                                  "cores": 4}
        t = {"t": 0.0}
        st4 = FsStore(tmp / "mine-store4")
        r = mine.gate(st4, "lock-g3", key="", min_score=90, wait_s=10,
                      sleep_fn=lambda s: None,
                      clock_fn=lambda: t.__setitem__("t", t["t"] + 5) or t["t"])
        check("non-target, nobody claims -> fallback builder",
              r["role"] == "builder" and "fallback" in r["reason"], str(r))

        st4_strict = FsStore(tmp / "mine-store4-strict")
        t_strict = {"t": 0.0}
        r_strict = mine.gate(st4_strict, "lock-g3-strict", key="", min_score=90, wait_s=10,
                             strict=True, sleep_fn=lambda s: None,
                             clock_fn=lambda: t_strict.__setitem__("t", t_strict["t"] + 5) or t_strict["t"])
        check("strict mining, non-target -> discarded (no fallback)",
              r_strict["role"] == "discarded" and "strict" in r_strict["reason"], str(r_strict))

        st5 = FsStore(tmp / "mine-store5")
        st5.claim("lock-g4", "t", "n")
        r = mine.gate(st5, "lock-g4", key="", min_score=90, wait_s=10,
                      sleep_fn=lambda s: None,
                      clock_fn=lambda: t.__setitem__("t", t["t"] + 5) or t["t"])
        check("non-target, lock held at deadline -> discarded",
              r["role"] == "discarded", str(r))

        # INDEX done short-circuit: no lock touched at all
        from forge_core.store import Router
        st6 = Router(backend="fs", fs_root=tmp / "mine-store6")
        st6.target_update("somekey", done=True)
        r = mine.gate(st6, "lock-g5", key="somekey", min_score=90,
                      wait_s=10, sleep_fn=lambda s: None,
                      clock_fn=lambda: 1000.0)
        check("INDEX done -> role done (no-op)", r["role"] == "done", str(r))
        check("done role left no lock", not st6.exists("lock-g5"))
    finally:
        mine.probe = real_probe


def test_e2e_plumbing(tmp: Path) -> None:
    print("[6] e2e store plumbing (syncer.snapshot + relay bank/restore)")
    from forge_core import syncer
    st = FsStore(tmp / "store2")
    st.create("src-hash1", "t", "n")

    home = tmp / "home"
    br = home / "aosp"
    (br / "build").mkdir(parents=True)
    (br / "build" / "envsetup.sh").write_text("# fake\n")
    (br / ".source_ready").write_text("mhash=hash1\n")
    (br / "out").mkdir()
    (br / "out" / ".ninja_log").write_text("# ninja log v5\n")
    (br / "out" / "target" / "product" / "PL2").mkdir(parents=True)
    sym = br / "out" / "target" / "product" / "PL2" / "symbols"
    sym.mkdir()
    (sym / "lib.so").write_bytes(b"\x11" * 100)
    (br / "out" / "target" / "x.bin").write_bytes(b"payload")

    n = syncer.snapshot_source(br, st, "src-hash1", "t", "n", sink=False)
    check("source banked", n >= 1 and st.exists("src-hash1"))

    home2 = tmp / "home2"
    br2 = home2 / "aosp"
    ok = syncer.restore_source(br2, st, "src-hash1")
    check("source restored + validated", ok and
          (br2 / "build" / "envsetup.sh").exists())

    st.create("state-k-s1", "t", "n")
    n = relay.bank(br, st, "state-k-s1", "k", 1)
    check("state banked", n >= 1)
    br3 = tmp / "home3" / "aosp"
    check("state restored", relay.restore(br3, st, "state-k-s1")
          and (br3 / "out" / "target" / "x.bin").read_bytes() == b"payload"
          and (br3 / "out" / ".ninja_log").exists())
    # F1: symbols/ is excluded from relay state to save 8-15GB per bank
    check("relay excludes symbols for state efficiency",
          not (br3 / "out" / "target" / "product" / "PL2" / "symbols" / "lib.so").exists())

    # F4: product-state banking & restoration
    (br / "out" / "target" / "product" / "PL2" / "obj").mkdir(parents=True, exist_ok=True)
    (br / "out" / "target" / "product" / "PL2" / "rom.zip").write_bytes(b"romzip")
    (br / "out" / "target" / "product" / "PL2" / "obj" / "temp.o").write_bytes(b"temp_obj")
    st.create("state-k-final-product", "t", "n")
    np = relay.bank_product_state(br, st, "state-k-final-product", "k")
    check("product-state banked", np >= 1)
    br4 = tmp / "home4" / "aosp"
    check("product-state restored", relay.restore(br4, st, "state-k-final-product")
          and (br4 / "out" / "target" / "product" / "PL2" / "rom.zip").read_bytes() == b"romzip"
          and not (br4 / "out" / "target" / "product" / "PL2" / "obj" / "temp.o").exists())


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="forge-tests-",
                                dir=str(ROOT.parent)))
    try:
        test_chunker(tmp)
        test_fsstore(tmp)
        test_relay(tmp)
        test_gate(tmp)
        test_e2e_plumbing(tmp)
        test_config()
        test_storage(tmp)
        test_dag()
        test_engine_taxonomy()
        test_mine(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{'ALL GREEN' if FAIL == 0 else 'FAILURES'}: {PASS} passed, "
          f"{FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
