# SAFETY — read before flashing anything

ROMForge's contract: **no unverified artifact ever reaches the flash
path**, and **the flash path re-verifies everything at flash time**. This
document explains what is guaranteed, what is not, and exactly what to
do when something goes wrong anyway.

## 1. What the 14-point gate guarantees

Every published ROM passed (see `SAFETY_REPORT.json` in the release):

1. build.prop identity matches this device's allowlist (anti-crossflash)
2. the OTA updater asserts the same device codenames
3. every partition image fits its size budget
4. dynamic-partition metadata is internally consistent (or static, as declared)
5. AVB/vbmeta flags and descriptors are coherent
6. boot.img/dtbo.img anatomy is well-formed for this device
7. VINTF compatibility matrix verified (when tooling is available)
8. security patch level is inside the device's window
9. Treble API/VNDK levels are sane
10. SELinux policies ship and the build is enforcing
11. OTA payload hashes/sizes are self-consistent
12. zip structure matches the device's A/B-ness
13. the zip is signed; test-keys are loudly warned about
14. the flash plan was simulated: every referenced artifact exists and fits

## 2. What no build system can guarantee

Unknown hardware states exist: failing eMMC, dying batteries, flaky
cables, a bootloader state nobody dumped, user error (wrong slot,
interrupted flash, 1 % battery at fastboot). ROMForge controls every
software-controllable cause; the residual risk is yours to accept by
flashing. The mitigations below are the difference between a bad evening
and a paperweight.

## 3. Before you flash (every time)

1. **Unlocked bootloader** — this is a custom ROM; the BL must be
   unlocked. Nokia 6.1 (PL2): unlock via the HMD dev program flow before
   anything here.
2. **Battery > 50 %.**
3. **Read `SAFETY_REPORT.json`** — especially any WARN entries.
4. **Verify checksums**: `sha256sum -c SHA256SUMS` (the flash script
   also does this, but look yourself).
5. **Have the rescue images ready** — they ship in the same release
   (`boot.img`, `dtbo.img`, `vbmeta.img`).

## 4. Flashing (A/B device)

```bash
# The guarded script refuses on: wrong device, checksum mismatch, missing files
./flash-guarded.sh
```

The script checks `fastboot getvar product` against the allowlist before
touching anything, flashes the images it shipped with (each verified),
and sets the other slot active before rebooting. For the ROM itself,
recovery sideload is the gentlest path:

```
fastboot reboot recovery   # or boot to recovery via key combo
# Apply update → Apply from ADB
adb sideload <rom>.zip
```

The updater re-asserts device identity and payload hashes before writing
a single block.

## 5. If it bootloops

1. **Recovery first** — A/B slots: boot the *other* slot
   (`fastboot set_active a|b`). The previous slot is intact by design.
2. **Wipe and retry**: `fastboot -w` then re-sideload.
3. **Re-flash the rescue images** from the release (boot/dtbo/vbmeta).

## 6. If it is truly wedged

For PL2 with an unlocked BL, `fastboot` remains available even with a
broken system (fastboot lives in the bootloader path, not the OS):

```bash
fastboot flash boot boot.img      # stock or release rescue image
fastboot flash dtbo dtbo.img
fastboot flash vbmeta vbmeta.img  # restore coherent vbmeta state
fastboot -w
fastboot reboot
```

Last resort: flash the full stock firmware package for your model/region
(HMD's official images; keep a copy on your PC **before** you start —
see the device profile's fingerprint tokens for the exact model family,
e.g. `Plate2_00WW`). If fastboot itself is gone, that is a hardware-level
situation (EDL/service mode) — outside any software system's remit.

## 7. Maintainer safety checklist (before you publish for others)

- [ ] `budgets_authority: stock` with measured partition sizes from a
      real device dump (`docs/ADD_DEVICE.md` shows the extraction).
- [ ] `spl_window` reflects the device's real shipped range.
- [ ] `anti_crossflash` lists every codename/variant your ROM supports.
- [ ] You personally flashed one build of this campaign on real hardware
      and booted it fully — the gate verifies artifacts, not experience.
- [ ] `SAFETY_REPORT.json` WARNs are understood, not ignored.
