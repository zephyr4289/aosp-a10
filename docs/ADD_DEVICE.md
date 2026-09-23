# ADD_DEVICE — onboarding a new device safely

A device profile is the file that makes a universal compiler *safe* for
your phone. It tells the 14-point gate what "correct" looks like for
your hardware. Budget 30–60 minutes the first time; every later ROM for
the same device reuses the file untouched.

## 1. Create the profile

Copy the pattern from `configs/devices/nokia_pl2.yaml` into
`configs/devices/<your-key>.yaml` (the key is what ROM profiles point at
via the `device:` field).

```yaml
name: <human name>
codenames: [codename, codename_sprout, marketing_name]   # every identity
soc: <SoC>                     # e.g. SM8150
arch: arm64
kernel_version: "4.14"
ab_update: true                # false for legacy non-A/B devices
dynamic_partitions: false      # null = auto-detect from misc_info.txt
boot_header_version: 2         # match your kernel era
dtbo_required: true
avb:
  enabled: false               # custom-ROM/unlocked-BL posture
  verity_disabled: true
budgets_authority: stock       # once you complete §2
partition_budgets: { ... }      # from your dump, see §2
anti_crossflash:
  ro_product_device: [...]
  fingerprint_tokens: [...]
  fastboot_product: [...]
spl_window: { min: "...", max: "..." }
vndk: { required: false }
```

## 2. Extract the ground truth (the part that protects users)

While booted on the *stock* ROM (or your current trusted ROM), collect:

```bash
# identity tokens
adb shell getprop ro.product.device
adb shell getprop ro.build.fingerprint
fastboot getvar product          # from the bootloader
adb shell getprop ro.build.version.security_patch

# partition sizes (root; non-dynamic partitions)
adb shell su -c 'blockdev --getsize64 /dev/block/bootdevice/by-name/system'
adb shell su -c 'blockdev --getsize64 /dev/block/bootdevice/by-name/vendor'
adb shell su -c 'blockdev --getsize64 /dev/block/bootdevice/by-name/product'

# dynamic (super) devices — lpmake metadata instead
adb shell su -c 'lpdump /dev/block/bootdevice/by-name/super'
```

Paste the numbers into `partition_budgets` with
`source: "stock dump <date>"` and set `budgets_authority: stock`. Now
check 3 compares against *measured* reality — the strictest mode.

If you cannot dump a device, the tree-declared values (`misc_info.txt`,
which the gate reads automatically) are used with `budgets_authority:
tree` — safe against tree-internal inconsistencies, weaker against a
device tree that itself lies. Say so in your profile comments.

## 3. VINTF (recommended)

Save the device manifest and reference it for check 7:

```bash
adb shell su -c 'cat /vendor/etc/vintf/manifest.xml' > my-device-manifest.xml
```

```yaml
vintf_device_manifest: configs/devices/my-device-manifest.xml
```

## 4. Wire a ROM to it

In a ROM profile: `device: <your-key>`. That's the entire integration —
the gate, budgets, guards and flash script all specialize to your
hardware from this one file.

## 5. Prove it before others flash it

1. `./forge validate` — schema sanity.
2. Run a `bootimage` campaign first; gate it.
3. Flash the full build **yourself**; boot it fully; use it a day.
4. Only then publish the release for others, and keep the maintainer
   checklist in docs/SAFETY.md green.
