#!/usr/bin/env python3
"""ROMForge Fleet Silicon & Storage Profiler.

Gathers full hardware metrics, CPU microarch, clock speeds, RAM bandwidth,
disk I/O throughput, directory structure sizes, and cloud VM metadata.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def _safe_cmd(cmd: List[str], timeout: int = 15) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""


def _read_file(path: str) -> str:
    try:
        if os.path.exists(path) and os.access(path, os.R_OK):
            return Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except Exception:
        pass
    return ""


def profile_cpu() -> Dict[str, Any]:
    info: Dict[str, Any] = {
        "model": "unknown",
        "vendor": "unknown",
        "family": "",
        "model_id": "",
        "stepping": "",
        "logical_cores": os.cpu_count() or 1,
        "physical_cores": 0,
        "sockets": 0,
        "current_mhz": 0.0,
        "min_mhz": 0.0,
        "max_mhz": 0.0,
        "flags": [],
        "avx512": False,
        "avx2": False,
        "amx": False,
        "aes_ni": False,
        "sha_ni": False,
        "cache": {},
        "microarch_class": "unknown",
        "score": 30,
    }

    try:
        cpuinfo = _read_file("/proc/cpuinfo")
        for line in cpuinfo.splitlines():
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            k = k.strip()
            v = v.strip()
            if k == "model name" and info["model"] == "unknown":
                info["model"] = v
            elif k == "vendor_id" and info["vendor"] == "unknown":
                info["vendor"] = v
            elif k == "cpu family" and not info["family"]:
                info["family"] = v
            elif k == "model" and not info["model_id"]:
                info["model_id"] = v
            elif k == "stepping" and not info["stepping"]:
                info["stepping"] = v
            elif k == "cpu MHz":
                try:
                    info["current_mhz"] = max(info["current_mhz"], float(v))
                except ValueError:
                    pass
            elif k == "flags" and not info["flags"]:
                flags = v.split()
                info["flags"] = flags
                info["avx2"] = "avx2" in flags
                info["avx512"] = any(f.startswith("avx512") for f in flags)
                info["amx"] = any(f.startswith("amx") for f in flags)
                info["aes_ni"] = "aes" in flags
                info["sha_ni"] = "sha_ni" in flags
    except Exception:
        pass

    # Sockets and physical cores via lscpu
    try:
        lscpu_txt = _safe_cmd(["lscpu"])
        for line in lscpu_txt.splitlines():
            if ":" in line:
                k, v = [x.strip() for x in line.split(":", 1)]
                if k in ("CPU max MHz", "CPU(s) scaling MHz max"):
                    try:
                        info["max_mhz"] = float(v)
                    except ValueError:
                        pass
                elif k in ("CPU min MHz", "CPU(s) scaling MHz min"):
                    try:
                        info["min_mhz"] = float(v)
                    except ValueError:
                        pass
                elif k == "Socket(s)":
                    try:
                        info["sockets"] = int(v)
                    except ValueError:
                        pass
                elif k == "Core(s) per socket":
                    try:
                        info["physical_cores"] = int(v) * max(1, info["sockets"])
                    except ValueError:
                        pass
    except Exception:
        pass

    # CPU cache from sysfs
    try:
        cache_base = Path("/sys/devices/system/cpu/cpu0/cache")
        if cache_base.exists():
            for idx in cache_base.glob("index*"):
                level = _read_file(str(idx / "level"))
                ctype = _read_file(str(idx / "type"))
                size = _read_file(str(idx / "size"))
                if level and size:
                    info["cache"][f"L{level}_{ctype}"] = size
    except Exception:
        pass

    # Microarchitecture classification & scoring
    model = info["model"]
    if re.search(r"EPYC\s+9V4[45]|EPYC\s+9V5[0-9]", model):
        info["microarch_class"] = "AMD Zen 5 Turin (4.3-4.6 GHz)"
        info["score"] = 100
    elif re.search(r"6980P|6973P|6972P|6971P", model):
        info["microarch_class"] = "Intel Xeon Granite Rapids (4.0-4.2 GHz)"
        info["score"] = 95
    elif re.search(r"EPYC\s+9V74", model):
        info["microarch_class"] = "AMD Zen 4c Genoa-X (3.7 GHz)"
        info["score"] = 85
    elif re.search(r"EPYC\s+9[34567][56]4|EPYC\s+9\d{3}\b", model):
        info["microarch_class"] = "AMD Zen 4 Genoa (3.5-3.7 GHz)"
        info["score"] = 80
    elif re.search(r"Platinum\s*8[45]\d{2}", model):
        info["microarch_class"] = "Intel Xeon Emerald/Sapphire Rapids"
        info["score"] = 70
    elif re.search(r"Platinum\s*83\d{2}", model):
        info["microarch_class"] = "Intel Xeon Ice Lake"
        info["score"] = 55
    elif re.search(r"EPYC\s+7[2-9]\d{2}|EPYC\s+7[BR]1\d", model):
        info["microarch_class"] = "AMD Zen 2/Zen 3 Rome/Milan"
        info["score"] = 40
    elif re.search(r"Gold\s*[56]\d{3}", model):
        info["microarch_class"] = "Intel Xeon Cascade Lake"
        info["score"] = 32
    else:
        info["microarch_class"] = f"Other ({model[:30]})"
        info["score"] = 30

    if info["avx512"] and info["score"] < 90:
        info["score"] += 8

    return info


def benchmark_cpu() -> Dict[str, Any]:
    """Micro-benchmarks for single-core and multi-core CPU throughput."""
    try:
        # 1. Single core SHA-256 throughput
        data_chunk = b"A" * (4 * 1024 * 1024)  # 4 MB
        t0 = time.perf_counter()
        iterations = 25
        for _ in range(iterations):
            hashlib.sha256(data_chunk).hexdigest()
        t_single = time.perf_counter() - t0
        single_mb_s = (iterations * 4) / max(0.0001, t_single)

        # 2. Integer Math / ALU ops
        t0 = time.perf_counter()
        val = 0
        for i in range(1_500_000):
            val = (val ^ (i * 1664525 + 1013904223)) & 0xFFFFFFFF
        t_math = time.perf_counter() - t0
        math_ops_m = (1.5 / max(0.0001, t_math))

        return {
            "sha256_single_mb_s": round(single_mb_s, 1),
            "math_mops_s": round(math_ops_m, 2),
        }
    except Exception as e:
        return {"error": str(e)}


def profile_memory() -> Dict[str, Any]:
    mem: Dict[str, Any] = {
        "mem_total_gb": 0.0,
        "mem_free_gb": 0.0,
        "mem_available_gb": 0.0,
        "swap_total_gb": 0.0,
        "swap_free_gb": 0.0,
        "hugepages_total": 0,
        "hugepage_size_kb": 0,
    }
    try:
        meminfo = _read_file("/proc/meminfo")
        for line in meminfo.splitlines():
            if ":" not in line:
                continue
            k, v = [x.strip() for x in line.split(":", 1)]
            v_num = re.sub(r"[^\d]", "", v)
            if not v_num:
                continue
            val_kb = int(v_num)
            if k == "MemTotal":
                mem["mem_total_gb"] = round(val_kb / (1024 * 1024), 2)
            elif k == "MemFree":
                mem["mem_free_gb"] = round(val_kb / (1024 * 1024), 2)
            elif k == "MemAvailable":
                mem["mem_available_gb"] = round(val_kb / (1024 * 1024), 2)
            elif k == "SwapTotal":
                mem["swap_total_gb"] = round(val_kb / (1024 * 1024), 2)
            elif k == "SwapFree":
                mem["swap_free_gb"] = round(val_kb / (1024 * 1024), 2)
            elif k == "HugePages_Total":
                mem["hugepages_total"] = val_kb
            elif k == "Hugepagesize":
                mem["hugepage_size_kb"] = val_kb
    except Exception:
        pass

    return mem


def benchmark_memory() -> Dict[str, Any]:
    """Measures sequential memory write and read bandwidth in GB/s."""
    try:
        size_mb = 128
        block = bytearray(size_mb * 1024 * 1024)

        # Sequential write
        t0 = time.perf_counter()
        for i in range(0, len(block), 4096):
            block[i] = 0xAA
        t_write = time.perf_counter() - t0
        write_gb_s = (size_mb / 1024) / max(0.0001, t_write)

        # Sequential read / hash
        t0 = time.perf_counter()
        h = hashlib.md5(block).hexdigest()
        t_read = time.perf_counter() - t0
        read_gb_s = (size_mb / 1024) / max(0.0001, t_read)

        return {
            "mem_write_gb_s": round(write_gb_s, 2),
            "mem_read_gb_s": round(read_gb_s, 2),
        }
    except Exception as e:
        return {"error": str(e)}


def profile_storage() -> Dict[str, Any]:
    """Profiles mounts, disk capacities, and block devices."""
    mounts = []
    try:
        df_txt = _safe_cmd(["df", "-hT"])
        for line in df_txt.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 7:
                mounts.append({
                    "fs": parts[0],
                    "type": parts[1],
                    "size": parts[2],
                    "used": parts[3],
                    "avail": parts[4],
                    "use_pct": parts[5],
                    "mount": parts[6],
                })
    except Exception:
        pass

    block_devices = []
    try:
        lsblk_txt = _safe_cmd(["lsblk", "-o", "NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,ROTA,MODEL"])
        for line in lsblk_txt.splitlines():
            if line.strip():
                block_devices.append(line.strip())
    except Exception:
        pass

    return {
        "mounts": mounts,
        "block_devices": block_devices,
    }


def benchmark_disk(target_dir: str) -> Dict[str, Any]:
    """Benchmarks sequential write/read speed and 4K random IOPS in target_dir."""
    try:
        p = Path(target_dir)
        if not p.exists() or not os.access(str(p), os.W_OK):
            return {"error": f"target_dir {target_dir} not writable"}

        test_file = p / f".profile_bench_{os.getpid()}_{time.time_ns()}.tmp"
        bench_data = b"X" * (1024 * 1024)  # 1 MB chunk
        total_chunks = 256  # 256 MB total

        # 1. Sequential Write
        t0 = time.perf_counter()
        with open(test_file, "wb") as f:
            for _ in range(total_chunks):
                f.write(bench_data)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        t_write = time.perf_counter() - t0
        write_mb_s = (total_chunks) / max(0.0001, t_write)

        # 2. Sequential Read
        t0 = time.perf_counter()
        with open(test_file, "rb") as f:
            while f.read(1024 * 1024):
                pass
        t_read = time.perf_counter() - t0
        read_mb_s = (total_chunks) / max(0.0001, t_read)

        # 3. Random 4K IOPS (500 writes + 500 reads)
        t0 = time.perf_counter()
        with open(test_file, "r+b") as f:
            for i in range(500):
                offset = ((i * 37) % (total_chunks - 1)) * 1024 * 1024
                f.seek(offset)
                f.write(b"4096" * 1024)
                f.seek(offset)
                _ = f.read(4096)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        t_iops = time.perf_counter() - t0
        iops = 1000 / max(0.0001, t_iops)

        test_file.unlink(missing_ok=True)

        return {
            "seq_write_mb_s": round(write_mb_s, 1),
            "seq_read_mb_s": round(read_mb_s, 1),
            "random_4k_iops": round(iops, 0),
        }
    except Exception as e:
        return {"error": str(e)}


def profile_directory_structure() -> Dict[str, Any]:
    """Scans and computes directory sizes across root subdirectories and toolchains."""
    dir_sizes: Dict[str, str] = {}
    target_dirs = ["/usr", "/opt", "/var", "/home", "/tmp", "/mnt", "/etc", "/root", "/boot"]
    for d in target_dirs:
        try:
            if os.path.exists(d):
                sz = _safe_cmd(["du", "-sh", d], timeout=2)
                if sz:
                    dir_sizes[d] = sz.split()[0]
                else:
                    usage = shutil.disk_usage(d)
                    dir_sizes[d] = f"{usage.used / (1024**3):.1f}G (partition used)"
        except Exception:
            pass

    # Inspect specific developer SDK footprints
    bloat_candidates = [
        "/usr/local/lib/android",
        "/usr/share/dotnet",
        "/opt/ghc",
        "/opt/hostedtoolcache",
        "/usr/share/swift",
        "/usr/local/share/boost",
        "/usr/local/share/powershell",
        "/usr/local/share/chromium",
        "/var/lib/docker",
        "/var/lib/containerd",
        "/home/runner",
    ]
    tool_sizes: Dict[str, str] = {}
    for p in bloat_candidates:
        try:
            if os.path.exists(p):
                sz = _safe_cmd(["du", "-sh", p], timeout=2)
                if sz:
                    tool_sizes[p] = sz.split()[0]
        except Exception:
            pass

    return {
        "root_subdirectories": dir_sizes,
        "preinstalled_toolchains": tool_sizes,
    }


def profile_cloud_and_vm() -> Dict[str, Any]:
    """Detects hypervisor, Azure metadata, OS kernel, and network."""
    cgroup_v = "unknown"
    try:
        if os.path.exists("/sys/fs/cgroup/cgroup.controllers"):
            cgroup_v = "v2"
        elif os.path.exists("/sys/fs/cgroup"):
            cgroup_v = "v1"
    except Exception:
        pass

    data: Dict[str, Any] = {
        "hostname": platform.node(),
        "kernel": platform.release(),
        "os": platform.platform(),
        "virt": _safe_cmd(["systemd-detect-virt"]),
        "dmi_product": _read_file("/sys/class/dmi/id/product_name"),
        "dmi_vendor": _read_file("/sys/class/dmi/id/sys_vendor"),
        "cgroup_version": cgroup_v,
        "azure": {},
        "public_ip_info": {},
    }

    # Azure IMDS
    try:
        req = urllib.request.Request(
            "http://169.254.169.254/metadata/instance?api-version=2021-02-01",
            headers={"Metadata": "true"},
        )
        with urllib.request.urlopen(req, timeout=2) as resp:
            imds = json.loads(resp.read().decode())
            compute = imds.get("compute", {})
            data["azure"] = {
                "vm_size": compute.get("vmSize", ""),
                "location": compute.get("location", ""),
                "zone": compute.get("zone", ""),
                "sku": compute.get("sku", ""),
                "os_type": compute.get("osType", ""),
            }
    except Exception:
        pass

    # Public IP / Geolocation
    try:
        req = urllib.request.Request(
            "https://ipinfo.io/json",
            headers={"User-Agent": "curl/7.68.0"},
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            data["public_ip_info"] = json.loads(resp.read().decode())
    except Exception:
        pass

    return data


def run_full_profile(shard_id: str = "1", output_dir: str = ".") -> Dict[str, Any]:
    print(f"============================================================")
    print(f" ROMForge Profiler — Shard #{shard_id} Starting Investigation")
    print(f"============================================================")

    out_p = Path(output_dir)
    out_p.mkdir(parents=True, exist_ok=True)

    print("[1/6] Probing CPU hardware & clock speeds...")
    cpu_info = profile_cpu()
    cpu_bench = benchmark_cpu()

    print("[2/6] Probing RAM capacity & memory throughput...")
    mem_info = profile_memory()
    mem_bench = benchmark_memory()

    print("[3/6] Probing storage mounts & block devices...")
    storage_info = profile_storage()

    print("[4/6] Running disk benchmarks on root (/) and /mnt...")
    bench_root = benchmark_disk("/tmp")
    bench_mnt = benchmark_disk("/mnt") if Path("/mnt").exists() and os.access("/mnt", os.W_OK) else {"error": "/mnt not writable"}

    print("[5/6] Analyzing directory structure and SDK bloat...")
    dir_info = profile_directory_structure()

    print("[6/6] Probing VM hypervisor and cloud metadata...")
    cloud_info = profile_cloud_and_vm()

    profile = {
        "shard_id": shard_id,
        "timestamp": time.time(),
        "timestamp_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "cpu": cpu_info,
        "cpu_benchmark": cpu_bench,
        "memory": mem_info,
        "memory_benchmark": mem_bench,
        "storage": storage_info,
        "disk_benchmark_root": bench_root,
        "disk_benchmark_mnt": bench_mnt,
        "directory_structure": dir_info,
        "system": cloud_info,
    }

    out_file = out_p / f"profile-shard-{shard_id}.json"
    out_file.write_text(json.dumps(profile, indent=2), encoding="utf-8")
    print(f"\n[OK] Profiling complete! Saved JSON to {out_file.resolve()}")

    # Print a summary
    print("\n" + "=" * 60)
    print(f" SHARD #{shard_id} PROFILING SUMMARY")
    print("=" * 60)
    print(f" CPU Model        : {cpu_info['model']}")
    print(f" Microarch Class  : {cpu_info['microarch_class']} (Score: {cpu_info['score']})")
    print(f" Cores / Clock    : {cpu_info['logical_cores']} cores @ {cpu_info['current_mhz']} MHz (Max: {cpu_info['max_mhz']} MHz)")
    print(f" AVX-512 Support  : {'YES' if cpu_info['avx512'] else 'NO'}")
    print(f" CPU SHA-256 Rate : {cpu_bench.get('sha256_single_mb_s', 'N/A')} MB/s")
    print(f" RAM Total / Avail: {mem_info['mem_total_gb']} GB / {mem_info['mem_available_gb']} GB")
    print(f" RAM Read / Write : {mem_bench.get('mem_read_gb_s', 'N/A')} GB/s read | {mem_bench.get('mem_write_gb_s', 'N/A')} GB/s write")
    print(f" Root (/) Seq R/W : {bench_root.get('seq_read_mb_s', 'N/A')} MB/s read | {bench_root.get('seq_write_mb_s', 'N/A')} MB/s write")
    if "seq_write_mb_s" in bench_mnt:
        print(f" /mnt SSD Seq R/W : {bench_mnt.get('seq_read_mb_s')} MB/s read | {bench_mnt.get('seq_write_mb_s')} MB/s write (IOPS: {bench_mnt.get('random_4k_iops')})")
    if cloud_info.get("azure"):
        print(f" Azure VM SKU/Loc : {cloud_info['azure'].get('vm_size')} in {cloud_info['azure'].get('location')}")
    print("=" * 60 + "\n")

    return profile


if __name__ == "__main__":
    shard = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("SHARD_ID", "1")
    out_dir = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("OUTPUT_DIR", ".")
    run_full_profile(shard_id=shard, output_dir=out_dir)
