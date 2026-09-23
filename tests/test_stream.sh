#!/usr/bin/env bash
# Extra integration proof: stream_pack -> fake gh sink (verifies the
# zero-staging path + env-var injection + SHA256SUMS protocol end to end).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE/.."
python3 tests/test_stream.py
