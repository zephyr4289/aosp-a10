#!/usr/bin/env bash
# ==============================================================================
#  lib.sh — shared helpers for the aosp-a10 build harness.
#  Sourced by scripts/00-06. Never executed directly.
# ==============================================================================
set -euo pipefail

# Locate the harness root (repo that contains config/, patches/, scripts/)
HARNESS_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# shellcheck source=../config/build.env
source "${HARNESS_ROOT}/config/build.env"

# ---- pretty logging -----------------------------------------------------------
_log()  { printf '\033[1;34m[%s]\033[0m %s\n' "$(date -u +%H:%M:%S)" "$*"; }
_ok()   { printf '\033[1;32m[%s] ✔ %s\033[0m\n' "$(date -u +%H:%M:%S)" "$*"; }
_warn() { printf '\033[1;33m[%s] ⚠ %s\033[0m\n' "$(date -u +%H:%M:%S)" "$*" >&2; }
_die()  { printf '\033[1;31m[%s] ✖ %s\033[0m\n' "$(date -u +%H:%M:%S)" "$*" >&2; exit 1; }
_hr()   { printf '%s\n' "--------------------------------------------------------------------------------"; }

# ---- GITHUB_OUTPUT writer (no-op outside GitHub Actions) -----------------------
out() {  # out <name> <value>
  if [ -n "${GITHUB_OUTPUT:-}" ]; then
    printf '%s=%s\n' "$1" "$2" >> "$GITHUB_OUTPUT"
  fi
  printf 'OUT: %s=%s\n' "$1" "$2"
}

# ---- step summary writer (no-op outside GitHub Actions) ------------------------
summary() {  # summary <markdown-line>
  if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    printf '%s\n' "$1" >> "$GITHUB_STEP_SUMMARY"
  fi
}

# ---- disk utilities ------------------------------------------------------------
disk_free_gb() { df -BG --output=avail / | tail -1 | tr -dc '0-9'; }

disk_report() {
  _hr; df -h / ; _hr
}

check_disk() {  # check_disk <context-label>
  local free
  free="$(disk_free_gb)"
  if [ "$free" -le "$MIN_FREE_GB_ABORT" ]; then
    disk_report
    _die "DISK CRITICAL: only ${free} GB free (${1}). Aborting before corrupting the build."
  elif [ "$free" -le "$MIN_FREE_GB_WARN" ]; then
    _warn "Disk low: ${free} GB free (${1}). Consider lowering CCACHE_SIZE in config/build.env."
  fi
}

# ---- gh release helpers ---------------------------------------------------------
# Recreate a cache release atomically: delete old release+tag, then create new
# with fresh assets. Prevents stale split-part leftovers from corrupting a
# restore when the number of parts changes between runs.
release_reset() {  # release_reset <tag> <title> <notes>
  gh release delete "$1" --yes --cleanup-tag >/dev/null 2>&1 || true
  gh release create "$1" --target "${GITHUB_SHA:-HEAD}" \
    --title "$2" --notes "$3" >/dev/null
}

release_upload() {  # release_upload <tag> <files...>
  gh release upload "$@" --clobber
}

# Download every asset of a tag that matches a glob into a directory.
release_fetch() {  # release_fetch <tag> <pattern> <destdir>
  mkdir -p "$3"
  gh release download "$1" --pattern "$2" --dir "$3" --clobber
}

release_exists() {  # release_exists <tag>
  gh release view "$1" >/dev/null 2>&1
}

# ---- split-archive streaming -----------------------------------------------------
# tar+zstd -> 1.9 GB parts (GitHub release assets are capped at 2 GiB).
# pack_split <tar-dir> <tar-member> <outdir> <prefix> [exclude-pattern]
pack_split() {
  local dir="$1" member="$2" outdir="$3" prefix="$4" exclude="${5:-}"
  mkdir -p "$outdir"
  _log "Packing ${member} -> ${prefix} parts (this can take 10-25 min)..."
  if [ -n "$exclude" ]; then
    tar -C "$dir" -cf - --exclude="${exclude}" "${member}" \
      | zstd -T0 -3 -c \
      | split -b 1900M - "${outdir}/${prefix}.part."
  else
    tar -C "$dir" -cf - "${member}" \
      | zstd -T0 -3 -c \
      | split -b 1900M - "${outdir}/${prefix}.part."
  fi
  ( cd "$outdir" && cat "${prefix}".part.* | sha256sum > SHA256SUMS )
  _ok "Packed: $(du -sh "$outdir" | cut -f1) in $(ls "${outdir}" | wc -l) files"
}

# Verify + unpack a split archive previously written by pack_split.
unpack_split() {  # unpack_split <dl-dir> <prefix> <dest-dir> [--strip]
  local dldir="$1" prefix="$2" dest="$3" strip="${4:-}"
  [ -f "${dldir}/${prefix}.part.aa" ] || _die "No ${prefix} parts found in ${dldir}"
  mkdir -p "$dest"
  if [ -f "${dldir}/SHA256SUMS" ]; then
    _log "Verifying ${prefix} integrity (sha256)..."
    ( cd "$dldir" && cat "${prefix}".part.* | sha256sum -c SHA256SUMS ) \
      || _die "sha256 mismatch for ${prefix} — cache corrupted in transfer, delete the release and rerun the job"
  else
    _warn "no SHA256SUMS asset — proceeding unverified (zstd frame checksums still apply)"
  fi
  _log "Unpacking ${prefix} -> ${dest} ..."
  if [ "$strip" = "--strip" ]; then
    cat "${dldir}/${prefix}".part.* | zstd -d -T0 -c \
      | tar -C "$dest" --strip-components=1 -xf -
  else
    cat "${dldir}/${prefix}".part.* | zstd -d -T0 -c \
      | tar -C "$dest" -xf -
  fi
}
