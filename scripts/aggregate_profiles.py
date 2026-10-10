#!/usr/bin/env python3
"""ROMForge Fleet Census Aggregator.

Aggregates 100 runner shard profiles into comprehensive fleet statistics,
microarchitectural census distribution, RAM/disk benchmarks, and a rich
GitHub Step Summary report.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List


def aggregate(profile_dir: str, output_md: str = "fleet_report.md", output_json: str = "fleet_census.json") -> Dict[str, Any]:
    p = Path(profile_dir)
    profile_files = sorted(p.glob("**/profile-shard-*.json"))
    if not profile_files:
        print(f"[!] No profile-shard-*.json files found in {profile_dir}")
        # also check current directory or subdirectories
        profile_files = sorted(Path(".").glob("**/profile-shard-*.json"))

    print(f"[*] Found {len(profile_files)} shard profiles to aggregate...")

    shards: List[Dict[str, Any]] = []
    for pf in profile_files:
        try:
            data = json.loads(pf.read_text(encoding="utf-8", errors="replace"))
            shards.append(data)
        except Exception as e:
            print(f"[!] Failed to parse {pf}: {e}")

    if not shards:
        print("[!] No valid profiles loaded.")
        return {}

    # Sort shards by score descending, then by CPU SHA256 speed descending
    shards.sort(
        key=lambda s: (
            s.get("cpu", {}).get("score", 0),
            s.get("cpu_benchmark", {}).get("sha256_single_mb_s", 0),
            s.get("memory_benchmark", {}).get("mem_read_gb_s", 0),
        ),
        reverse=True,
    )

    # Compute Fleet Statistics
    total_shards = len(shards)
    cpu_classes: Dict[str, int] = {}
    azure_skus: Dict[str, int] = {}
    azure_locs: Dict[str, int] = {}
    scores: List[int] = []
    ram_gb: List[float] = []
    sha256_speeds: List[float] = []
    ram_write_speeds: List[float] = []
    disk_write_speeds_mnt: List[float] = []
    disk_write_speeds_root: List[float] = []

    for s in shards:
        cpu = s.get("cpu", {})
        klass = cpu.get("microarch_class", "unknown")
        cpu_classes[klass] = cpu_classes.get(klass, 0) + 1
        scores.append(cpu.get("score", 0))

        mem = s.get("memory", {})
        if "mem_total_gb" in mem:
            ram_gb.append(mem["mem_total_gb"])

        cb = s.get("cpu_benchmark", {})
        if "sha256_single_mb_s" in cb:
            sha256_speeds.append(cb["sha256_single_mb_s"])

        mb = s.get("memory_benchmark", {})
        if "mem_write_gb_s" in mb:
            ram_write_speeds.append(mb["mem_write_gb_s"])

        db_mnt = s.get("disk_benchmark_mnt", {})
        if "seq_write_mb_s" in db_mnt:
            disk_write_speeds_mnt.append(db_mnt["seq_write_mb_s"])

        db_root = s.get("disk_benchmark_root", {})
        if "seq_write_mb_s" in db_root:
            disk_write_speeds_root.append(db_root["seq_write_mb_s"])

        sys_info = s.get("system", {})
        az = sys_info.get("azure", {})
        if az.get("vm_size"):
            sku = az["vm_size"]
            azure_skus[sku] = azure_skus.get(sku, 0) + 1
        if az.get("location"):
            loc = az["location"]
            azure_locs[loc] = azure_locs.get(loc, 0) + 1

    def _avg(lst: List[float]) -> float:
        return round(sum(lst) / len(lst), 1) if lst else 0.0

    def _max(lst: List[float]) -> float:
        return round(max(lst), 1) if lst else 0.0

    def _min(lst: List[float]) -> float:
        return round(min(lst), 1) if lst else 0.0

    zen5_count = sum(cnt for k, cnt in cpu_classes.items() if "Zen 5" in k or "Turin" in k)
    zen4_count = sum(cnt for k, cnt in cpu_classes.items() if "Zen 4" in k or "Genoa" in k)
    granite_count = sum(cnt for k, cnt in cpu_classes.items() if "Granite" in k)
    target_count = zen5_count + granite_count

    census_summary = {
        "total_shards_profiled": total_shards,
        "target_silicon_pct": round((target_count / total_shards) * 100, 1) if total_shards else 0,
        "cpu_microarch_distribution": {k: {"count": v, "pct": round((v / total_shards) * 100, 1)} for k, v in cpu_classes.items()},
        "azure_sku_distribution": azure_skus,
        "azure_locations": azure_locs,
        "performance_stats": {
            "silicon_score": {"min": _min(scores), "avg": _avg(scores), "max": _max(scores)},
            "sha256_mb_s": {"min": _min(sha256_speeds), "avg": _avg(sha256_speeds), "max": _max(sha256_speeds)},
            "ram_write_gb_s": {"min": _min(ram_write_speeds), "avg": _avg(ram_write_speeds), "max": _max(ram_write_speeds)},
            "disk_write_mnt_mb_s": {"min": _min(disk_write_speeds_mnt), "avg": _avg(disk_write_speeds_mnt), "max": _max(disk_write_speeds_mnt)},
            "disk_write_root_mb_s": {"min": _min(disk_write_speeds_root), "avg": _avg(disk_write_speeds_root), "max": _max(disk_write_speeds_root)},
        },
        "all_shards": shards,
    }

    # Write Census JSON
    Path(output_json).write_text(json.dumps(census_summary, indent=2), encoding="utf-8")
    print(f"[OK] Wrote JSON summary to {output_json}")

    # Build Markdown Report
    lines = []
    lines.append(f"# 🚀 ROMForge 100-Shard Fleet Silicon & Storage Profiling Report\n")
    lines.append(f"**Total Shards Profiled:** `{total_shards}` | **Target Silicon (Zen5 / Granite):** `{census_summary['target_silicon_pct']}%`\n")

    lines.append(f"## 📊 1. CPU Microarchitecture Breakdown\n")
    lines.append(f"| Microarchitecture Class | Count | Percentage | Score |")
    lines.append(f"| :--- | :---: | :---: | :---: |")
    for k, info in sorted(census_summary["cpu_microarch_distribution"].items(), key=lambda x: x[1]["count"], reverse=True):
        score_val = next((s.get("cpu", {}).get("score", 0) for s in shards if s.get("cpu", {}).get("microarch_class") == k), "N/A")
        lines.append(f"| **{k}** | {info['count']} | {info['pct']}% | `{score_val}` |")
    lines.append("")

    lines.append(f"## ⚡ 2. Fleet Performance Summary\n")
    lines.append(f"| Metric | Minimum | Average | Maximum |")
    lines.append(f"| :--- | :---: | :---: | :---: |")
    lines.append(f"| **Silicon Score** | `{_min(scores)}` | `{_avg(scores)}` | `{_max(scores)}` |")
    lines.append(f"| **CPU SHA-256 Throughput** | `{_min(sha256_speeds)} MB/s` | `{_avg(sha256_speeds)} MB/s` | `{_max(sha256_speeds)} MB/s` |")
    lines.append(f"| **RAM Write Bandwidth** | `{_min(ram_write_speeds)} GB/s` | `{_avg(ram_write_speeds)} GB/s` | `{_max(ram_write_speeds)} GB/s` |")
    if disk_write_speeds_mnt:
        lines.append(f"| **`/mnt` SSD Write Speed** | `{_min(disk_write_speeds_mnt)} MB/s` | `{_avg(disk_write_speeds_mnt)} MB/s` | `{_max(disk_write_speeds_mnt)} MB/s` |")
    lines.append(f"| **`/` Root Disk Write Speed** | `{_min(disk_write_speeds_root)} MB/s` | `{_avg(disk_write_speeds_root)} MB/s` | `{_max(disk_write_speeds_root)} MB/s` |")
    lines.append("")

    if azure_locs:
        lines.append(f"## 🌍 3. Cloud Provider & Geographic Spread\n")
        lines.append(f"| Location / Region | Count |")
        lines.append(f"| :--- | :---: |")
        for loc, cnt in sorted(azure_locs.items(), key=lambda x: x[1], reverse=True):
            lines.append(f"| `{loc}` | {cnt} |")
        lines.append("")

    lines.append(f"## 🏆 4. Top 15 Fastest Silicon Shards (Leaderboard)\n")
    lines.append(f"| Rank | Shard ID | CPU Model | Microarchitecture | Score | CPU SHA256 | RAM Bandwidth | `/mnt` SSD Write |")
    lines.append(f"| :---: | :---: | :--- | :--- | :---: | :---: | :---: | :---: |")
    for i, s in enumerate(shards[:15], 1):
        cpu = s.get("cpu", {})
        cb = s.get("cpu_benchmark", {})
        mb = s.get("memory_benchmark", {})
        db = s.get("disk_benchmark_mnt", {})
        mnt_w = f"{db.get('seq_write_mb_s')} MB/s" if "seq_write_mb_s" in db else "N/A"
        lines.append(f"| **#{i}** | Shard `{s.get('shard_id')}` | {cpu.get('model', 'unknown')} | {cpu.get('microarch_class', 'unknown')} | `{cpu.get('score')}` | `{cb.get('sha256_single_mb_s', 'N/A')} MB/s` | `{mb.get('mem_write_gb_s', 'N/A')} GB/s` | `{mnt_w}` |")
    lines.append("")

    lines.append(f"## 📋 5. Complete 100-Shard Fleet Roster\n")
    lines.append(f"<details><summary><b>Click to expand full 100-shard inspection table</b></summary>\n")
    lines.append(f"| Shard | CPU Model | Cores | Max MHz | AVX-512 | RAM Total | RAM Free | Root Avail | `/mnt` Avail | Score |")
    lines.append(f"| :---: | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |")
    for s in shards:
        cpu = s.get("cpu", {})
        mem = s.get("memory", {})
        st = s.get("storage", {})
        root_avail = "N/A"
        mnt_avail = "N/A"
        for m in st.get("mounts", []):
            if m.get("mount") == "/":
                root_avail = m.get("avail", "N/A")
            elif m.get("mount") == "/mnt":
                mnt_avail = m.get("avail", "N/A")

        lines.append(f"| `{s.get('shard_id')}` | {cpu.get('model', 'unknown')[:30]} | {cpu.get('logical_cores')} | {cpu.get('max_mhz', 'N/A')} | {'YES' if cpu.get('avx512') else 'NO'} | {mem.get('mem_total_gb', 'N/A')}G | {mem.get('mem_available_gb', 'N/A')}G | {root_avail} | {mnt_avail} | `{cpu.get('score')}` |")
    lines.append(f"\n</details>\n")

    md_content = "\n".join(lines)
    Path(output_md).write_text(md_content, encoding="utf-8")
    print(f"[OK] Wrote Markdown report to {output_md}")

    # If GITHUB_STEP_SUMMARY environment variable exists, append markdown
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        try:
            with open(summary_path, "a", encoding="utf-8") as f:
                f.write(md_content)
            print(f"[OK] Appended report to GITHUB_STEP_SUMMARY")
        except Exception as e:
            print(f"[!] Failed to write to GITHUB_STEP_SUMMARY: {e}")

    return census_summary


if __name__ == "__main__":
    p_dir = sys.argv[1] if len(sys.argv) > 1 else "."
    out_md = sys.argv[2] if len(sys.argv) > 2 else "fleet_report.md"
    out_js = sys.argv[3] if len(sys.argv) > 3 else "fleet_census.json"
    aggregate(p_dir, output_md=out_md, output_json=out_js)
