#!/usr/bin/env bash
# ==============================================================================
#  03_build.sh <target> — run ONE build slice inside the job budget
#
#  The 6-hour GitHub job wall makes a single cold AOSP build impossible on
#  4 vCPUs (~15-20 h needed). This script runs the build in its own process
#  group (setsid) with a watchdog that SIGINTs the WHOLE GROUP (soong_ui +
#  ninja) when the slice budget is spent — soong/ninja then shut down
#  gracefully, and every completed compilation is already persisted in the
#  ccache directory. The next slice resumes at near-zero marginal cost.
#  Re-dispatch until the ROM target completes.
#
#  Why not plain `timeout ... m bacon`?
#    `m` is a shell function (unreachable for timeout), and `timeout` signals
#    only its direct child — a bash wrapper would orphan the ninja process
#    group and we'd tar a ccache that is still being written. Process-group
#    signaling is the only race-free way.
#
#  Outputs (GITHUB_OUTPUT + stdout):
#    attempted  = true   (build actually ran)
#    sliced     = true   (budget exhausted — dispatch another slice)
#    done       = true   (target completed successfully)
#    rom_zip    = path   (full flashable zip, when target=bacon succeeded)
# ==============================================================================
source "$(dirname "$0")/lib.sh"

TARGET="${1:-bacon}"
out attempted true

[ -f "${AOSP_ROOT}/.source_ready" ] || _die "source tree not ready — run 01/02 first"
command -v setsid >/dev/null || _die "setsid missing (util-linux) — cannot guarantee group kill"

# ---- ccache wiring -----------------------------------------------------------------
export USE_CCACHE=1
export CCACHE_EXEC="$(command -v ccache)"
export CCACHE_DIR="${CCACHE_DIR}"
ccache -M "${CCACHE_SIZE}" >/dev/null
ccache -o compression=true >/dev/null 2>&1 || _warn "ccache compression unavailable (pre-4.x?) — cache will be larger"

# ---- build env ----------------------------------------------------------------------
export LC_ALL=C                             # old AOSP perl/python scripts hate UTF-8
export WITH_DEXPREOPT="${WITH_DEXPREOPT}"   # false = huge out/ + time savings

cd "${AOSP_ROOT}"
set +eu
# shellcheck disable=SC1091
source build/envsetup.sh
lunch "${LUNCH_COMBO}"
LUNCH_RC=$?
set -euo pipefail

[ "${LUNCH_RC}" -eq 0 ] || _die "lunch ${LUNCH_COMBO} failed (rc=${LUNCH_RC})"

SOONG_UI="${AOSP_ROOT}/build/soong/soong_ui.bash"
[ -x "${SOONG_UI}" ] || _die "soong_ui not found at ${SOONG_UI}"

_hr
_log "ccache BEFORE:"
ccache -s | sed 's/^/  /'
_hr

_log "BUILD SLICE: target='${TARGET}' budget=$((BUILD_SLICE_SECONDS / 60)) min device=${DEVICE}"
summary "## Build slice — ${TARGET}"
summary ""
summary "| field | value |"
summary "|---|---|"
summary "| target | \`${TARGET}\` |"
summary "| lunch | \`${LUNCH_COMBO}\` |"
summary "| slice budget | $((BUILD_SLICE_SECONDS / 60)) min |"

# ---- run the build in its own process group + watchdog -----------------------------
T0=$(date +%s)

setsid "${SOONG_UI}" --make-mode "${TARGET}" &
SOONG_PID=$!

# Watchdog: SIGINT the whole group at budget expiry (graceful ninja stop),
# then SIGKILL five minutes later as a backstop against a stuck process.
(
  sleep "${BUILD_SLICE_SECONDS}"
  kill -INT -- -"${SOONG_PID}" 2>/dev/null || true
  sleep 300
  kill -KILL -- -"${SOONG_PID}" 2>/dev/null || true
) &
WATCHDOG=$!

RC=0
wait "${SOONG_PID}" || RC=$?
kill "${WATCHDOG}" 2>/dev/null || true
wait "${WATCHDOG}" 2>/dev/null || true

T1=$(date +%s)
ELAPSED=$(( T1 - T0 ))

_hr
_log "ccache AFTER:"
ccache -s | sed 's/^/  /'
_hr

summary "| wall time | $((ELAPSED / 60)) min |"
summary "| exit code | ${RC} |"

# ---- classify the outcome -----------------------------------------------------------
# The watchdog fires at exactly BUILD_SLICE_SECONDS; allow 5 s of scheduling
# drift. Anything non-zero BEFORE the budget is a real build error.
if [ "$RC" -eq 0 ]; then
  _ok "target '${TARGET}' completed in $((ELAPSED / 60)) min"
  out done true
  out sliced false

  if [ "$TARGET" = "bacon" ]; then
    ROM_DIR="${AOSP_ROOT}/out/target/product/${DEVICE}"
    ROM_ZIP=""
    # largest zip that is not a fastboot image package = the OTA/ROM zip
    for z in "${ROM_DIR}"/*.zip; do
      [ -e "$z" ] || continue
      case "$(basename "$z")" in
        *-img-*.zip|*fastboot*) continue ;;
      esac
      if [ -z "$ROM_ZIP" ] || [ "$(stat -c%s "$z")" -gt "$(stat -c%s "$ROM_ZIP")" ]; then
        ROM_ZIP="$z"
      fi
    done
    [ -n "$ROM_ZIP" ] || _die "bacon succeeded but no ROM zip found in ${ROM_DIR}"
    out rom_zip "$ROM_ZIP"
    out rom_name "$(basename "$ROM_ZIP")"
    out rom_dir "${ROM_DIR}/"
    summary ""
    summary "**ROM ready:** \`$(basename "$ROM_ZIP")\` ($(( $(stat -c%s "$ROM_ZIP") / 1024 / 1024 )) MB)"
    _ok "ROM: ${ROM_ZIP}"
  fi

elif [ "$ELAPSED" -ge $((BUILD_SLICE_SECONDS - 5)) ]; then
  _ok "slice budget spent after $((ELAPSED / 60)) min — progress banked in ccache"
  out sliced true
  out done false
  summary ""
  summary "**Slice complete.** All compiled objects are banked in ccache; the next"
  summary "slice (auto-scheduled every 6 h, or instant with the CHAIN_PAT secret)"
  summary "resumes from cache hits instead of recompiling."

else
  _die "build failed with rc=${RC} after $((ELAPSED / 60)) min (real error, not a timeout) — ccache progress is still preserved & uploaded"
fi
