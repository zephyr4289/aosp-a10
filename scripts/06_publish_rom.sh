#!/usr/bin/env bash
# ==============================================================================
#  06_publish_rom.sh <rom-zip-path> — publish the finished ROM
#  * sha256 + release 'rom' (this release's existence also gates future
#    scheduled runs)
#  * the workflow additionally uploads an artifact backup
# ==============================================================================
source "$(dirname "$0")/lib.sh"

ROM_ZIP="${1:?usage: 06_publish_rom.sh <rom-zip-path>}"
[ -f "$ROM_ZIP" ] || _die "ROM zip not found: ${ROM_ZIP}"

LOG_DIR="$HOME/rom-publish"
mkdir -p "$LOG_DIR"
cp "$ROM_ZIP" "$LOG_DIR/"
( cd "$LOG_DIR" && sha256sum "$(basename "$ROM_ZIP")" > SHA256SUMS )

NOTES="$(basename "$ROM_ZIP")

- ROM: ${ROM_NAME} (Android 10 / Q)
- Device: Nokia 6.1 (PL2 / PL2_sprout / Plate2)
- Lunch: \`${LUNCH_COMBO}\`
- Built by the aosp-a10 chained-CI harness on GitHub Actions — total cost: ₹0

Flash at your own risk. Verify SHA256SUMS before sideloading."

_log "Publishing ${ROM_ZIP} to release 'rom'..."
if release_exists "rom"; then
  release_upload "rom" "$LOG_DIR"/*
else
  gh release create "rom" "$LOG_DIR"/* \
    --target "${GITHUB_SHA:-HEAD}" \
    --title "${ROM_NAME} · Nokia 6.1 (PL2)" \
    --notes "$NOTES" >/dev/null
fi

_hr
_ok "ROM PUBLISHED"
[ -n "${GITHUB_REPOSITORY:-}" ] && _ok "https://github.com/${GITHUB_REPOSITORY}/releases/tag/rom"
_ok "sha256: $(cat "$LOG_DIR/SHA256SUMS" | cut -d' ' -f1)"
