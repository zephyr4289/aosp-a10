#!/usr/bin/env bash
# ==============================================================================
#  02_apply_patches.sh — inject the QASSA product into the PL2 tree + smoke lunch
#  * copies patches/device/* into the device tree (same files the Colab
#    notebook generated inline)
#  * validates that `lunch qassa_PL2-userdebug` actually resolves BEFORE we
#    burn build hours
# ==============================================================================
source "$(dirname "$0")/lib.sh"

[ -f "${AOSP_ROOT}/.source_ready" ] || _die "source tree not marked ready — run 01 first"
[ -f "${AOSP_ROOT}/build/envsetup.sh" ] || _die "${AOSP_ROOT}/build/envsetup.sh missing — tree incomplete"
[ -f "${AOSP_ROOT}/${DEVICE_PATH}/device.mk" ] || _die "${DEVICE_PATH}/device.mk missing — device tree broken"

_log "Injecting QASSA product makefiles into ${DEVICE_PATH}..."
cp "${HARNESS_ROOT}/patches/device/qassa_PL2.mk"     "${AOSP_ROOT}/${DEVICE_PATH}/"
cp "${HARNESS_ROOT}/patches/device/AndroidProducts.mk" "${AOSP_ROOT}/${DEVICE_PATH}/"
_ok "patched: qassa_PL2.mk, AndroidProducts.mk"

_log "Validating lunch target (kati/soong product resolution)..."
cd "${AOSP_ROOT}"
# shellcheck disable=SC1091
source build/envsetup.sh >/dev/null 2>&1
lunch "${LUNCH_COMBO}" >/dev/null 2>&1 \
  || _die "lunch ${LUNCH_COMBO} failed — check device tree + product mk files"

_ok "lunch ${LUNCH_COMBO} resolves. Tree is buildable."
check_disk "after lunch validation"
