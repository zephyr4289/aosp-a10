#!/usr/bin/env bash
# ==============================================================================
#  tests/test_archive_roundtrip.sh — offline proof that the cache banking
#  pipeline (pack_split -> split parts -> sha256 -> unpack_split) is lossless.
#
#  Builds a miniature fake tree (aosp/ + .ccache/, incl. an out/ dir that must
#  be excluded), packs it exactly the way scripts/04 does, then restores it
#  exactly the way scripts/05 does, and asserts the results.
#
#  Run:  bash tests/test_archive_roundtrip.sh
#  Requires: zstd, split, tar, sha256sum (no gh, no network, no GitHub).
# ==============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
source "${HERE}/../scripts/lib.sh"

# Use the real zstd when present; otherwise fall back to the xz-backed shim.
if ! command -v zstd >/dev/null 2>&1; then
  export PATH="${HERE}/bin:${PATH}"
  echo "(no system zstd — using tests/bin/zstd xz shim for this offline test)"
fi

WORK="$(mktemp -d)/home"
trap 'rm -rf "$(dirname "$WORK")"' EXIT

# ---- fabricate a miniature "HOME" -------------------------------------------------
mkdir -p "${WORK}/aosp/build" "${WORK}/aosp/out/target/product/PL2" "${WORK}/.ccache/zz"
echo "envsetup-marker" > "${WORK}/aosp/build/envsetup.sh"
echo "device-mk"        > "${WORK}/aosp/device.mk"
echo "must-not-survive" > "${WORK}/aosp/out/target/product/PL2/system.img"
echo "cache-object"     > "${WORK}/.ccache/zz/object.hit"
head -c 3000000 /dev/urandom > "${WORK}/aosp/bigblob.bin"     # 3 MB random
echo "source_ready"     > "${WORK}/aosp/.source_ready"

# ---- pack (same calls scripts/04 makes) --------------------------------------------
STAGE="${WORK}/.stage"
echo "== packing source =="
pack_split "${WORK}" "aosp" "${STAGE}/src" "src" "aosp/out"
echo "== packing ccache =="
pack_split "${WORK}" ".ccache" "${STAGE}/cc" "ccache" ""

# ---- corrupt one part to prove the sha256 gate works --------------------------------
cp -r "${STAGE}/src" "${STAGE}/src-bad"
printf 'CORRUPTION' >> "${STAGE}/src-bad/src.part.aa"
# NOTE: run in a subshell — unpack_split _die()s on corruption, which must not
# take the test process with it.
if ( unpack_split "${STAGE}/src-bad" "src" "${WORK}/.tmp_bad" --strip ) >/dev/null 2>&1; then
  _die "TEST FAILURE: corrupted archive passed sha256 gate"
else
  _ok "corruption correctly rejected by sha256 gate"
fi
rm -rf "${WORK}/.tmp_bad" "${STAGE}/src-bad"

# ---- restore (same calls scripts/05 makes) -------------------------------------------
unpack_split "${STAGE}/src" "src" "${WORK}/.aosp_incoming" --strip
rm -rf "${WORK}/aosp-restored"; mv "${WORK}/.aosp_incoming" "${WORK}/aosp-restored"

unpack_split "${STAGE}/cc" "ccache" "${WORK}/.ccache_incoming" --strip
rm -rf "${WORK}/.ccache-restored"; mv "${WORK}/.ccache_incoming" "${WORK}/.ccache-restored"

# ---- assertions ----------------------------------------------------------------------
fail() { _die "TEST FAILURE: $1"; }

[ "$(cat "${WORK}/aosp-restored/build/envsetup.sh")" = "envsetup-marker" ] || fail "envsetup content lost"
[ "$(cat "${WORK}/aosp-restored/device.mk")"          = "device-mk" ]        || fail "device.mk content lost"
[ "$(cat "${WORK}/aosp-restored/.source_ready")"      = "source_ready" ]     || fail "marker lost"
cmp -s "${WORK}/aosp/bigblob.bin" "${WORK}/aosp-restored/bigblob.bin"       || fail "binary blob altered"
[ ! -e "${WORK}/aosp-restored/out" ]                                        || fail "out/ leaked into source cache"
[ "$(cat "${WORK}/.ccache-restored/zz/object.hit")"  = "cache-object" ]      || fail "ccache content lost"

_hr
_ok "ALL ASSERTIONS PASSED — archive pipeline is lossless and out/ is excluded"
