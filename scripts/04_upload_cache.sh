#!/usr/bin/env bash
# ==============================================================================
#  04_upload_cache.sh <src|ccache> — persist state into GitHub Releases
#
#  Why Releases and not actions/cache?
#    * actions/cache caps at 10 GB/repo with LRU eviction — a source tree or a
#      hot ccache does not fit and can silently vanish mid-campaign.
#    * Release assets on public repos are free, 2 GiB each; we split archives
#      into 1.9 GB parts and reset the release each time (no stale parts).
# ==============================================================================
source "$(dirname "$0")/lib.sh"

MODE="${1:?usage: 04_upload_cache.sh <src|ccache>}"
STAGE="$HOME/.upload-${MODE}"

if [ "$MODE" = "src" ]; then
  [ -f "${AOSP_ROOT}/.source_ready" ] || { _warn "source not ready — nothing to upload"; exit 0; }
  TAG="source-cache"
  TITLE="Source cache — QASSA ${MANIFEST_BRANCH} (shallow, git-stripped)"
  NOTES="Auto-generated source snapshot. Restored by scripts/05_restore_cache.sh. Do not delete unless you want the next run to re-sync from scratch."
  MEMBER="aosp"
elif [ "$MODE" = "ccache" ]; then
  [ -d "${CCACHE_DIR}" ] || { _warn "no ccache dir — nothing to upload"; exit 0; }
  CC_SIZE="$(du -sm "${CCACHE_DIR}" | cut -f1)"
  [ "$CC_SIZE" -gt 50 ] || { _ok "ccache nearly empty (${CC_SIZE} MB) — skipping upload"; exit 0; }
  TAG="ccache-cache"
  TITLE="ccache — ${LUNCH_COMBO}"
  NOTES="Compiler cache carried between build slices. Deleting this tag resets to a cold build."
  MEMBER=".ccache"
else
  _die "unknown mode '${MODE}' (expected src|ccache)"
fi

check_disk "before packing ${MODE}"
rm -rf "$STAGE"
if [ "$MODE" = "src" ]; then
  pack_split "$HOME" "$MEMBER" "$STAGE" "$MODE" "aosp/out"   # never cache intermediates
else
  pack_split "$HOME" "$MEMBER" "$STAGE" "$MODE"
fi

_log "Publishing to release '${TAG}' (delete + recreate for atomicity)..."
release_reset "$TAG" "$TITLE" "$NOTES"
release_upload "$TAG" "$STAGE"/*
_ok "release '${TAG}' now carries $(ls "$STAGE" | wc -l) assets ($(du -sh "$STAGE" | cut -f1))"

rm -rf "$STAGE"
disk_report
