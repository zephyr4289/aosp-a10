#!/usr/bin/env bash
# ==============================================================================
#  05_restore_cache.sh — bring back source + ccache from Releases (or signal
#  that a fresh sync is needed).
#
#  Outputs:
#    source = cache  (tree restored from source-cache release)
#           = sync   (no cache found / FORCE_SYNC -> run 01_sync_source.sh)
#    ccache = hit|miss
# ==============================================================================
source "$(dirname "$0")/lib.sh"

FORCE_SYNC="${FORCE_SYNC:-false}"
FRESH_CCACHE="${FRESH_CCACHE:-false}"

# ------------------------------- source ------------------------------------------
if [ "${FORCE_SYNC}" = "true" ]; then
  _log "FORCE_SYNC set — skipping source cache, will re-sync from manifests"
  out source sync
elif release_exists "source-cache"; then
  DL="$HOME/.dl-src"; rm -rf "$DL"
  _log "Downloading source cache (~12-16 GB, usually 5-15 min)..."
  release_fetch "source-cache" "src.part.*" "$DL" \
    || _die "release 'source-cache' exists but has no parts — delete it in the GitHub UI and re-run"
  release_fetch "source-cache" "SHA256SUMS" "$DL" || true
  TMP="$HOME/.aosp_incoming"; rm -rf "$TMP"; mkdir -p "$TMP"
  unpack_split "$DL" "src" "$TMP" --strip
  rm -rf "$DL"
  rm -rf "${AOSP_ROOT}"
  mv "$TMP" "${AOSP_ROOT}"
  [ -f "${AOSP_ROOT}/build/envsetup.sh" ] || _die "restored source incomplete — delete the source-cache release and rerun"
  _ok "source restored from cache"
  out source cache
else
  _log "no source-cache release — bootstrap run, will sync from manifests"
  out source sync
fi

# ------------------------------- ccache -------------------------------------------
if [ "${FRESH_CCACHE}" = "true" ]; then
  _log "FRESH_CCACHE set — starting with a cold compiler cache"
  rm -rf "${CCACHE_DIR}"
  out ccache miss
elif release_exists "ccache-cache"; then
  DL="$HOME/.dl-ccache"; rm -rf "$DL"
  _log "Downloading ccache (~6-10 GB)..."
  release_fetch "ccache-cache" "ccache.part.*" "$DL" \
    || _die "release 'ccache-cache' exists but has no parts — delete it in the GitHub UI and re-run"
  release_fetch "ccache-cache" "SHA256SUMS" "$DL" || true
  TMP="$HOME/.ccache_incoming"; rm -rf "$TMP"; mkdir -p "$TMP"
  unpack_split "$DL" "ccache" "$TMP" --strip
  rm -rf "$DL"
  rm -rf "${CCACHE_DIR}"
  mv "$TMP" "${CCACHE_DIR}"
  _ok "ccache restored: $(du -sh "${CCACHE_DIR}" | cut -f1)"
  out ccache hit
else
  _log "no ccache-cache release — first slice runs cold (expected on bootstrap)"
  out ccache miss
fi

check_disk "after cache restore"
disk_report
