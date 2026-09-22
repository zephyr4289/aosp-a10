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

TARGET="${1:-qassa}"
# QASSA ROM zip target is 'qassa' (or otapackage); map 'bacon' to 'qassa'
if [ "$TARGET" = "bacon" ]; then
  TARGET="qassa"
fi
out attempted true

[ -f "${AOSP_ROOT}/.source_ready" ] || _die "source tree not ready — run 01/02 first"
command -v setsid >/dev/null || _die "setsid missing (util-linux) — cannot guarantee group kill"

# ---- ccache wiring -----------------------------------------------------------------
export USE_CCACHE=1
export CCACHE_EXEC="$(command -v ccache)"
export CCACHE_DIR="${CCACHE_DIR}"
ccache -M "${CCACHE_SIZE}" >/dev/null
ccache -o compression=true >/dev/null 2>&1 || _warn "ccache compression unavailable (pre-4.x?) — cache will be larger"
ccache -z >/dev/null 2>&1 || true   # zero stats so BEFORE/AFTER in this slice is exact
check_disk "before build slice"

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
# Log hygiene: full output goes to $BUILD_LOG (for forensics), console gets a
# throttled filter (every 5% + errors) so the GitHub live view never truncates.
# RC is taken from the real soong PID (no pipes), so slice/error classification is unchanged.
T0=$(date +%s)
BUILD_LOG="$HOME/build-slice.log"
rm -f "$BUILD_LOG"

_log "soong bootstrap/Kati is single-threaded and quiet for ~5-15 min — [alive] heartbeats prove breathing; first 5% notice starts ninja main."
if command -v stdbuf >/dev/null 2>&1; then
  setsid stdbuf -oL -eL "${SOONG_UI}" --make-mode "${TARGET}" >"$BUILD_LOG" 2>&1 &
else
  setsid "${SOONG_UI}" --make-mode "${TARGET}" >"$BUILD_LOG" 2>&1 &
fi
SOONG_PID=$!

# Filtered console tail (background, killed after build; never affects RC).
# Zero-overhead live %: every 5% jump also emits a ::notice:: annotation
# (max ~20 notices, under GitHub's 50-annotation cap). monitor.py reads the
# latest annotation via the Checks API — no artifacts, no extra pushes.
echo "::group::Build output (throttled, full log in artifact on failure)"
(
  tail -F -n +1 "$BUILD_LOG" 2>/dev/null | awk '
    /FAILED|error:|No space left|ninja: .* stopped/ { print; fflush(); next }
    /\[[ ]*[0-9]+%[ ]*[0-9]+\/[0-9]+/ {
      tmp = $0; sub(/.*\[[ ]*/, "", tmp)
      pct = tmp + 0
      dt = tmp; sub(/^[^0-9]*[0-9]+%[ ]*/, "", dt); sub(/\].*/, "", dt)
      n = split(dt, a, "/"); done = (n >= 1 ? a[1] : "?"); total = (n >= 2 ? a[2] : "?")
      if (NR == 1 || pct - last >= 5) {
        print; fflush(); last = pct
        printf "::notice title=Build-Progress::PROGRESS:%d:%s/%s\n", pct, done, total; fflush()
      }
      next
    }
  ' || true
) &
TAIL_PID=$!

# Heartbeat + stuck alarm: breathing console lines every 2 min (elapsed, disk,
# out/ size, top CPU hog) so the log never looks dead; loud warning — never
# auto-kill — if BUILD_LOG sees zero writes for 45 min while soong is alive.
(
  while true; do
    sleep 120
    now=$(date +%s); elapsemin=$(( (now - T0) / 60 ))
    dfline=$(df -h / 2>/dev/null | tail -1 | awk '{print $3"/"$2" used "$5" free "$4}')
    outsz=$(du -sh "${AOSP_ROOT}/out" 2>/dev/null | cut -f1)
    topproc=$(ps -eo pcpu,comm --sort=-pcpu 2>/dev/null | head -2 | tail -1 | tr -s ' ')
    echo "[alive ${elapsemin}m] disk ${dfline:-?} out ${outsz:-?} top:${topproc:-?}"
    if [ -f "$BUILD_LOG" ] && kill -0 "$SOONG_PID" 2>/dev/null; then
      if [ -n "$(find "$BUILD_LOG" -mmin +45 2>/dev/null)" ]; then
        echo "[STUCK-ALARM] no build output for 45+ min (elapsed ${elapsemin}m) — dumping diagnostics (warn only, build continues)"
        ps -eo pid,pcpu,pmem,etime,comm --sort=-pcpu 2>/dev/null | head -6 || true
        free -h 2>/dev/null || true
        df -h / 2>/dev/null | tail -2 || true
        dmesg 2>/dev/null | tail -5 || true
        tail -n 5 "$BUILD_LOG" 2>/dev/null || true
      fi
    fi
  done
) &
DISKMON=$!

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
kill "${WATCHDOG}" "${TAIL_PID}" "${DISKMON}" 2>/dev/null || true
wait "${WATCHDOG}" 2>/dev/null || true
wait "${TAIL_PID}" 2>/dev/null || true
wait "${DISKMON}" 2>/dev/null || true
# Stop tail following the log file.
pkill -P $$ tail 2>/dev/null || true

T1=$(date +%s)
ELAPSED=$(( T1 - T0 ))
echo "::endgroup::"

# Always show the actionable tail (errors + last progress) even with filtering.
_hr
_log "build log tail (last 30 lines):"
tail -n 30 "$BUILD_LOG" 2>/dev/null | sed "s/^/  /" || true
if grep -E -m 5 "FAILED|No space left|error:" "$BUILD_LOG" 2>/dev/null | sed "s/^/  >> /"; then
  summary "| build errors | see log tail |"
  true
fi
# Keep a compressed full log for forensics ONLY on real errors (not slices);
# success/slice paths delete it to return ~100MB before the ccache-pack step.
if [ "$RC" -ne 0 ] && [ "$ELAPSED" -lt $((BUILD_SLICE_SECONDS - 5)) ]; then
  zstd -3 -c "$BUILD_LOG" >"$HOME/build-slice.log.zst" 2>/dev/null || true
  summary "| full log | \`~/build-slice.log.zst\` (artifact on failure) |"
fi
rm -f "$BUILD_LOG"
check_disk "after build slice"

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

  if [ "$TARGET" = "qassa" ] || [ "$TARGET" = "bacon" ] || [ "$TARGET" = "otapackage" ]; then
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
    [ -n "$ROM_ZIP" ] || _die "${TARGET} succeeded but no ROM zip found in ${ROM_DIR}"
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
