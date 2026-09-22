#!/usr/bin/env bash
# ==============================================================================
#  01_sync_source.sh — materialize the QASSA (Android 10) source tree
#  * shallow repo init + sync of the ROM manifest (with retries)
#  * lands the 4 PL2 device repos (direct clone = proven Colab flow, or
#    via local_manifest)
#  * strips .git/.repo metadata afterwards (saves 4-8 GB; force_sync re-inits)
#  * writes the .source_ready marker that gates every later stage
# ==============================================================================
source "$(dirname "$0")/lib.sh"

rm -rf "${AOSP_ROOT}"            # force_sync / retry safety: always start clean
mkdir -p "${AOSP_ROOT}"

_log "Installing repo launcher..."
mkdir -p "${HOME}/bin"
curl -sSL https://storage.googleapis.com/git-repo-downloads/repo > "${HOME}/bin/repo"
chmod a+x "${HOME}/bin/repo"
export PATH="${HOME}/bin:${PATH}"

cd "${AOSP_ROOT}"

# ---- local manifest mode: register device repos before sync --------------------
if [ "${DEVICE_CLONE_MODE}" = "manifest" ]; then
  _log "Copying local manifest (mode: manifest)..."
  mkdir -p .repo/local_manifests
  cp "${HARNESS_ROOT}/${LOCAL_MANIFEST_SRC}" .repo/local_manifests/device_pl2.xml
fi

# ---- repo init (retry x2) --------------------------------------------------------
attempt=0
until repo init --depth=1 -u "${MANIFEST_URL}" -b "${MANIFEST_BRANCH}" --git-lfs; do
  attempt=$((attempt + 1))
  [ "$attempt" -ge 2 ] && _die "repo init failed after retries"
  _warn "repo init failed — retrying in 60s..."
  sleep 60
done
_ok "repo init: ${MANIFEST_URL} @ ${MANIFEST_BRANCH}"

# ---- repo sync (retry x3, resumes where it left off) ------------------------------
attempt=0
sync_ok=0
while [ "$attempt" -lt 3 ]; do
  attempt=$((attempt + 1))
  _log "repo sync attempt ${attempt}/3 (-j${SYNC_JOBS})..."
  force=""
  [ "$attempt" -gt 1 ] && force="--force-sync"
  if repo sync -c -j"${SYNC_JOBS}" $force \
       --no-clone-bundle --no-tags --optimized-fetch --prune; then
    sync_ok=1
    break
  fi
  _warn "sync attempt ${attempt} incomplete — completed projects persist, resuming in $((attempt * 60))s..."
  sleep $((attempt * 60))
done
[ "$sync_ok" -eq 1 ] || _die "repo sync failed after 3 attempts"
_ok "repo sync complete"

check_disk "after repo sync"

# ---- land the device repos --------------------------------------------------------
# Direct mode reproduces the exact flow that worked on Colab: wipe whatever the
# ROM manifest put at those paths and shallow-clone the verified Zoro-15 repos.
while IFS='|' read -r path url branch; do
  [ -z "$path" ] && continue
  case "$path" in \#*) continue ;; esac
  if [ "${DEVICE_CLONE_MODE}" = "direct" ]; then
    _log "direct clone: ${url} (${branch}) -> ${path}"
    rm -rf "${AOSP_ROOT}/${path}"
    git clone --depth=1 -b "${branch}" "$url" "${AOSP_ROOT}/${path}"
  else
    # manifest mode: only rescue a path if the sync left it empty/broken
    if [ ! -e "${AOSP_ROOT}/${path}/.git" ] && [ ! -f "${AOSP_ROOT}/${path}/Android.mk" ] \
       && [ ! -f "${AOSP_ROOT}/${path}/AndroidProducts.mk" ] \
       && [ ! -f "${AOSP_ROOT}/${path}/device.mk" ]; then
      _warn "manifest mode left ${path} empty — falling back to direct clone"
      rm -rf "${AOSP_ROOT}/${path}"
      git clone --depth=1 -b "${branch}" "$url" "${AOSP_ROOT}/${path}"
    fi
  fi
done < "${HARNESS_ROOT}/${DEVICE_REPOS_FILE}"

[ -f "${AOSP_ROOT}/${DEVICE_PATH}/device.mk" ] \
  || _die "device tree incomplete: ${DEVICE_PATH}/device.mk missing after sync+clone"

# ---- strip git metadata + non-Linux prebuilts -------------------------------------
if [ "${STRIP_GIT}" = "true" ]; then
  _log "Stripping .git/.repo metadata (saves 4-8 GB; never needed post-sync)..."
  find "${AOSP_ROOT}" -name ".git" -type d -prune -exec rm -rf {} + 2>/dev/null || true
  rm -rf "${AOSP_ROOT}/.repo"
fi
rm -rf "${AOSP_ROOT}/prebuilts/gcc/darwin-x86" \
       "${AOSP_ROOT}/prebuilts/gcc/windows-x86" \
       "${AOSP_ROOT}/prebuilts/gcc/windows-x86_64" 2>/dev/null || true

touch "${AOSP_ROOT}/.source_ready"

_hr
_ok "Source tree ready: ${AOSP_ROOT}"
du -sh "${AOSP_ROOT}" | sed 's/^/  size: /'
disk_report
