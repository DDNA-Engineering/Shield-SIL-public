# The i.MX 8M Plus machine

Shield's SoC is an NXP i.MX 8M Plus. Besides QEMU `virt` (generic arm64 board,
stand-in devices), the SIL can run Shield's software on an emulated i.MX 8M
Plus, so the boot chain and the kernel's drivers are the ones a Shield uses.
Pick it with an `-imx8mp` image: `openwrt-24.10.3-imx8mp`, `openwrt-25.12.3-imx8mp` or
`ubuntu-26.04-imx8mp`, for example `./sil up openwrt-24.10.3-imx8mp`.

## What runs

| On Shield | On the i.MX 8M Plus machine | On QEMU virt |
| --- | --- | --- |
| Boot ROM, SPL (DRAM training) | QEMU loads TF-A and U-Boot into RAM and starts TF-A; a small boot ROM answers U-Boot's boot-device query | QEMU `-bios u-boot.bin` |
| TF-A BL31 (PSCI, power domains) | upstream TF-A `lts-v2.12.4`, `PLAT=imx8mp`, unmodified (`image/tfa/`) | none (QEMU's own PSCI) |
| U-Boot | upstream U-Boot `v2026.10`, `shield-sil` board (`image/uboot/patches/`): 4 GiB, eMMC on uSDHC3 as `mmc 2`, environment in boot1 | upstream U-Boot, `qemu_arm64_defconfig` |
| Device tree | `image/dts/shield-sil.dts`, built against the guest kernel's own `imx8mp.dtsi`, in the boot partition as `shield-sil.dtb` | QEMU's |
| eMMC | uSDHC3, `/dev/mmcblk2` with `mmcblk2boot0/1` | SDHCI on PCI, `/dev/mmcblk0` |
| LAN | FEC (`fec`), `eth0` | virtio-net |
| WAN (EQoS) | e1000e on PCIe, `eth1` (QEMU has no EQoS model) | virtio-net |
| TPS25751 / BQ25792 on `/dev/i2c-1` | I2C2 with `i2c-imx`; the device answers from the board model through `sil-i2c-bridge` | i2c-tiny-usb over USB/IP |
| SoM bus `/dev/i2c-0` | I2C1 (no devices modelled) | empty i2c-tiny-usb adapter |
| Front-panel LEDs | `gpio-leds` on Shield's pins (Heartbeat GPIO1_IO12 and System GPIO5_IO03 active low; User GPIO4_IO30, WAN GPIO5_IO00, WiFi GPIO4_IO31, ETH GPIO1_IO01, Battery GPIO4_IO22) | uleds |
| Named GPIO lines | Shield's pins (GPIO3_IO26/27, GPIO4_IO12/25, GPIO5_IO01/10/11/12). OpenWrt: `/sys/class/gpio/<name>` from `gpio-export`, as on Shield; Ubuntu: `gpio-line-names` (libgpiod) | gpio-sim |
| Button `BTN_0` | `gpio-keys` on GPIO5_IO04 (active low, 50 ms debounce), as on Shield | USB HID over USB/IP |
| Watchdog | WDOG1 (`imx2-wdt`) | i6300esb |
| EM7595 modem, USB-C gadget | USB/IP over the management NIC (as on virt) | same |
| SIL hardware bus | UART1 (state) and UART3 (commands), raw mode | virtio-serial |

The pins of the LEDs, the button and the named lines are Shield's, taken from
Shield's device tree (facts only; the SIL's tree is its own).
`src/shield_sil/imx8mp.py` holds the same map, and a unit test checks the two
agree.

## The SIL's QEMU

Stock QEMU's `imx8mp-evk` cannot run TF-A or U-Boot and has no way to connect
SoC pins or I2C devices to models outside QEMU. `qemu/` builds QEMU `v10.2.1`
with the SIL's patches (listed in [qemu/README.md](../qemu/README.md)); the
`./sil` container has it at `/opt/qemu-sil`. The patches add the power
controller (GPC), reset controller (SRC), boot ROM, the two bridge devices and
the eMMC slot option, fix four device models (eMMC Auto CMD23, PCIe
inbound DMA, uSDHC clock bits and SDMA boundary), and backport upstream's watchdog fixes, which
reboot and the watchdog reset depend on.

### The bridges

`sil-i2c-bridge` is an I2C target in QEMU. Every bus event (start, each byte,
stop) goes to the harness over a UNIX socket and QEMU waits for the answer, so
the board model ACKs, NACKs and returns bytes like the real part.
`sil-gpio-bridge` carries pin levels both ways: what the guest drives
(LEDs, output lines) goes to the board model; the button and the input lines
(`shield:pgood:*`) are driven from the model. Output lines read their default
level as a pull until the guest drives them. Both protocols are documented at
the top of their source files in `qemu/patches/0004-*.patch`.

## Building the images

```
image/build.sh openwrt-24.10.3-imx8mp      # or openwrt-25.12.3-imx8mp
sudo -E image/build.sh ubuntu-26.04-imx8mp
```

The OpenWrt builder uses the release's `imx/cortexa53` ImageBuilder
(`imx8m` profile) and SDK with `image/openwrt/shield-packages-imx8mp.list`,
then builds U-Boot (`MACHINE=imx8mp`), TF-A and the device tree for the
release kernel (`kernel_version` in the catalog entry) and assembles the same
eMMC layout as on virt. The imx target ships no squashfs root image, so the
builder makes one from the same root directory with the ImageBuilder's
`mksquashfs4` and OpenWrt's options.

The Ubuntu builder makes the same image as for virt (cloud image root, Ubuntu's
generic kernel and initrd, provisioned packages) and adds U-Boot, TF-A and the
device tree, built against the kernel's upstream version (7.0 for
`7.0.0-38-generic`). Ubuntu's kernel has the SoC's clock, pin, GPIO, UART and
FEC drivers built in; uSDHC, I2C, watchdog, gpio-keys, gpio-leds and e1000e are
modules, so the initrd stays.

## What stays different from a Shield

- No boot ROM loader or SPL, and no DRAM training: QEMU starts at TF-A.
- Power domains and cores switch on instantly.
- The second GbE port is a PCIe e1000e, not the EQoS.
- The modem and the USB-C gadget still arrive over USB/IP (QEMU cannot pass a
  model in as a USB device on the SoC's xHCI).
- No thermal sensor (TMU): QEMU does not model it.
- U-Boot does not start the watchdog; the OS does (OpenWrt's procd, Ubuntu's
  `shield-watchdog.service`). Under
  emulation, loading Ubuntu's kernel and initrd takes longer than the
  watchdog's 60 s.
- No CPU power-down idle state (`cpu-pd-wait`): QEMU cannot power a core off in
  WFI and wake it, so idle cores use plain WFI.
- Loading from the eMMC in U-Boot runs at a few MB/s (QEMU's SD model), so
  Ubuntu spends about 40 s in U-Boot.
- LEDs use `gpio-leds` (on/off) where Shield uses `pwm-gpio` and `pwm-leds`
  (brightness 0 to 255): the upstream OpenWrt kernels have no `pwm-gpio`.
- Shield's EEPROM, HD3SS3220 and RTC on I2C2 and its PMIC on I2C1 are not
  modelled.
