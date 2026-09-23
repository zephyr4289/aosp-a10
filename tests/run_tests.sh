#!/usr/bin/env bash
# ROMForge offline test suite — zero GitHub credentials required.
# Exercises: chunker roundtrip (gzip fallback), FsStore, relay forensics,
# and the 14-point gate against synthetic GOOD and POISONED product trees.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE/.."
python3 tests/test_all.py "$@"
