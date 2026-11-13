# Guest image: Shield's driver interfaces

The OpenWrt 24.10.3 and 25.12.3 guests and the Ubuntu 26.04 guest present
Shield's hardware through the same Linux interfaces the Shield firmware uses,
so its scripts can run unmodified. The table names the OpenWrt pieces; the
Ubuntu differences are in [Ubuntu 26.04](#ubuntu-2604) below. Host-side models and the hardware bus are described in
[hardware-bus.md](hardware-bus.md).

| On Shield | In the SIL guest | Driver / agent |
|---|---|---|
| `/dev/i2c-1`: TPS25751 at 0x20, BQ25792 behind it | i2c-tiny-usb adapter over USB/IP, wired to `Board.i2c_transfer` | upstream `i2c-tiny-usb`, `i2c-dev`; `shield-usbip` |
| `/dev/i2c-0` (SoM bus) | second i2c-tiny-usb adapter, attached first so the numbering matches | same |
| EM7595 on USB (1199:c081): ttyUSB0-2, AT on ttyUSB2, cdc-wdm0, wwan0 | USB/IP device from the host modem model; ModemManager (plugin sierra) | upstream `qcserial`, `qmi_wwan`, `cdc-wdm`, `vhci-hcd` |
| `/sys/class/leds/{Heartbeat,System,User,WAN,WiFi,ETH,Battery}` | one uleds LED per name; every brightness change goes to the host as `led` | upstream `uleds`; `shield-board` |
| GPIO lines `shield:cell:sim-select`, `shield:cell:full-card-power`, `shield:wifi:*`, `shield:pgood:*` | gpio-sim chip with those line names (libgpiod and `/sys/class/gpio`); outputs go to the host as `gpio`, inputs follow the host's `gpio_in` | `kmod-gpio-sim` (below); `shield-board` |
| Front-panel button: `BTN_0` (gpio-keys) to `/etc/rc.button/BTN_0` | one-button USB HID device (busid 1-4) that `hid-input` maps to `BTN_0`; `button-hotplug` runs `/etc/rc.button/BTN_0` with `ACTION` and `SEEN`. The SIL's default handler only logs to `/var/run/shield/button.log`; install Shield's handler over it | upstream `usbhid`, `button-hotplug` |
| Wi-Fi AP `mwiphy0-ap0` in `br-lan` | `mac80211_hwsim` radio 0 with hostapd (WPA2), interface named `mwiphy0-ap0`. Radio 1 is a Wi-Fi client in network namespace `wificlient` (`ip netns exec wificlient ...`) that joins the AP and takes a LAN lease. Settings in `/usr/share/shield-sil/wifi.conf` | upstream `mac80211_hwsim`, `wpad-basic-mbedtls` |
| USB-C `usb0` (USB Ethernet gadget) in `br-lan` | default (`network.usbc = "gadget"`): the Linux gadget stack (configfs gadget `shield`, CDC Ethernet function `ecm.usb0`) on the USB/IP virtual device controller `usbip-vudc`, exported on the management link (port 3240) by `shield-vudc-exportd`. The host's simulated laptop (192.168.100.60, namespace `usbhost`) imports it as a USB host, enumerates it and carries Ethernet frames over the bulk endpoints; unplugging the cable ends the USB/IP connection and `usb0` loses carrier. Fallback `network.usbc = "nic"`: a plain NIC (MAC 52:54:00:5e:00:13) renamed `usb0` at boot (`/etc/init.d/shield-usb0`) | upstream `libcomposite`, `usb_f_ecm`; `kmod-usb-gadget-vudc` and `shield-vudc-exportd` (below); `shield-usb-gadget` |
| eMMC `/dev/mmcblk0` (GPT: `config`, `boot_rescu`, `rootfs_rescu`, `boot_a`, `rootfs_a`, `boot_b`, `rootfs_b`, `data`), boot partitions `mmcblk0boot0/1` | QEMU's JEDEC eMMC on an SDHCI controller; U-Boot (`image/uboot/`) boots slot A (p4/p5) or B (p6/p7), see [eMMC and A/B boot](#emmc-and-ab-boot) | upstream `sdhci-pci`, `uboot-envtools`, `kmod-fs-f2fs` |
| Hardware watchdog `/dev/watchdog`, fed by procd | Intel 6300ESB watchdog: the board resets if the system stops feeding it (the i.MX8M Plus WDOG on Shield) | upstream `kmod-i6300esb-wdt` |
| Network roles `lan`, `wan` (eth), `wanb` (cellular), mwan3 policy `wan_wanb` | same names; `wanb` is `proto modemmanager` on the EM7595 (wwan0, APN `internet`), as on Shield | upstream `mwan3` |

Line names, directions and initial levels are in
`image/overlay/common/usr/share/shield-sil/board.conf`.

On the i.MX 8M Plus machine (`-imx8mp` images) most of this column is the
SoC's own hardware instead, on Shield's pins: I2C on `i2c-imx`, LEDs on
`gpio-leds`, named lines on SoC GPIOs (on OpenWrt also `/sys/class/gpio/<name>`
through `gpio-export`, as on Shield), the button on `gpio-keys`, the watchdog on
WDOG1, and the eMMC
is `/dev/mmcblk2` with `mmcblk2boot0/1`, as on Shield. Only the modem and the
USB-C gadget still use USB/IP. See [imx8mp.md](imx8mp.md).

## Management link

USB/IP runs over an extra guest NIC (QEMU user networking, MAC
52:54:00:5e:00:1f, 10.0.2.15/24, no gateway). It is `eth2` on OpenWrt 24.10.3,
25.12.3 and Ubuntu 26.04, which have no stand-in cellular NIC, and `eth3` on
23.05.6; the logical interface (OpenWrt) or NetworkManager profile (Ubuntu) is
`silmgmt` either way. Guest 10.0.2.2:<port> reaches
the host's 127.0.0.1:<port>. The host publishes the server as the `usbip`
record; `shield-usbip` attaches the listed busids in order and re-attaches a
device (the modem after a restart) as soon as the host offers it again.
`silmgmt` is in no firewall zone, no mwan3 interface and no Shield role, so
network and failover tests never see its traffic. It is the one interface a
real Shield does not have.

## Ubuntu 26.04

Same kernel interfaces and SIL agents (`shield-board`, `shield-usbip`,
`shield-wifi-client`, as systemd services), set up the Ubuntu way:

| Piece | Ubuntu 26.04 image |
|---|---|
| Network configuration | NetworkManager keyfiles in `/etc/NetworkManager/system-connections/`: `lan` (bridge `br-lan`, 192.168.100.1/24, `ipv4.method=shared`: DHCP .100-.249 and NAT), ports `lan-eth0` and `lan-usb0`; `wan` (eth1, DHCP, route metric 10); `wanb` (type gsm, APN `internet`, route metric 20); `silmgmt`. No netplan, no systemd-networkd |
| Cellular | ModemManager drives the EM7595; NetworkManager's `wanb` profile brings up data on `wwan0` |
| Uplink failover | route metrics: wired WAN while it has carrier, cellular otherwise. No mwan3, so no failover on an upstream outage with the cable still in |
| Wi-Fi AP | hostapd (`shield-wifi-ap.service`, `/etc/hostapd/shield-ap.conf` generated from `wifi.conf`) on radio 0, named `mwiphy0-ap0` by `/etc/systemd/network/10-shield-wifi-ap.link`, bridged into `br-lan`. NetworkManager leaves the AP and the client radio alone |
| Button | the same `BTN_0` input event; Ubuntu has no `/etc/rc.button`, so whatever Shield's Ubuntu runs on the button reads the input device (`/dev/input/event*`, name "Front-panel button") |
| USB-C `usb0` | gadget mode as on OpenWrt, exported by `usbipd --device` (Ubuntu's usbip tools use the full libudev); NetworkManager manages the gadget netdev (`/etc/udev/rules.d/90-shield-usb0.rules`). The `nic` fallback is renamed by `/etc/systemd/network/10-shield-usb0.link` |
| Storage | Shield's Ubuntu eMMC layout: `boot` (FAT: `Image`, `initrd.img`, `boot.scr`), `rootfs` (ext4, 4 GiB), `cidata` (FAT), `data` (ext4, the rest, at `/root/data` by `mount-data.service`). U-Boot boots it; no A/B slots, updates go through apt. The upstream kernel has the eMMC drivers as modules, so the boot loads the cloud image's generic initrd (Shield's own kernel boots without one) |
| GPIO inputs | stock `gpio-sim` returns a line to its pull when the last consumer releases it (the OpenWrt package keeps the level), so a value written with `gpioset` holds only while `gpioset` runs (`gpioset -z`) |
| Tools | `nmcli`, `mmcli`, `iw`, `hostapd`, `wpa_supplicant`, `gpiod`, `i2c-tools`, `usbip` (from `linux-tools-<kernel>`) |

The packages the cloud image lacks are installed at build time by a
provisioning boot: the host's apt resolves them against the image's own dpkg
status (verified with the image's Ubuntu archive keyring), and dpkg installs
them inside the arm64 guest under QEMU, offline. `image.json` records each
package version.

## Known issue: mwan3 hotplug ordering

mwan3 2.12 (OpenWrt 24.10 and 25.12) runs each hotplug event as its own
process. If a WAN cable goes back in shortly after mwan3track declared the link
offline, the queued `disconnected` event can run after `ifup`. The tracker then
reconnects without a hotplug event, so `mwan3 interfaces` shows
`wan is online ... (offline ...)` and WAN carries no traffic until mwan3
restarts. The SIL hit this on slow CI runners (reproduce by hand with
`env -i ACTION=disconnected INTERFACE=wan DEVICE=eth1 /sbin/hotplug-call iface`).
Between tests the harness restores the baseline by restarting mwan3 after 60 s
in that state and records a warning (`tests/system/conftest.py`). The tests
themselves do not work around it, and a real Shield can hit it as well.

## How the image is built

`image/builders/openwrt.sh` builds the way OpenWrt builds release images:

1. Kernel modules that the release kernel ships disabled are compiled with the
   release's official SDK (`openwrt/sdk:<tag>`), so their vermagic matches.
   Today that is only `kmod-gpio-sim` (`image/openwrt/packages/`): upstream
   `drivers/gpio/gpio-sim.c` with `kernel/irq/irq_sim.c` linked in, because
   armsr has `CONFIG_GPIO_SIM` and `CONFIG_IRQ_SIM` off. Sources are vendored
   from linux-stable per kernel series with two small patches; see its
   `src/README.md`. Results are cached in `build/cache/<image>/local-packages/`.
2. The official ImageBuilder (`openwrt/imagebuilder:<tag>`) installs the
   release's packages plus `image/openwrt/shield-packages.list` and the SIL
   overlay (`FILES=`), and checks every requested package is in the image.
3. The root filesystem is written to a sparse ext4 image (`rootfs.ext4`).
4. With `[build] storage = "emmc"` (24.10.3, 25.12.3): U-Boot is built from
   pinned upstream sources (`image/uboot/build.sh`, cached in
   `build/cache/`), and `image/builders/emmc.py` assembles `emmc.img`
   (Shield's eMMC: boot0 + boot1 + 32 GiB user area, sparse) from the
   squashfs root, and `sysupgrade.tar`. See [eMMC and A/B boot](#emmc-and-ab-boot).

Only upstream OpenWrt packages and upstream kernel sources go into the image.
Shield's own scripts and packages are not included.

Behind an HTTPS proxy the build containers get the host's `HTTPS_PROXY` and
`SSL_CERT_FILE`. Docker Hub rate-limits anonymous pulls, so images already
present locally are not pulled again.

## Adding a kernel series

When an OpenWrt release moves to a new kernel series, add
`src/<series>/` and `patches/<series>/` to `kmod-gpio-sim` as described in its
README. The SDK build fails with a clear error if the series is missing.

## Boot order of the USB devices

On Shield the modem and I2C controllers exist when the kernel boots. In the
SIL they arrive over USB/IP a few seconds into boot, so
`/etc/init.d/shield-usbip-coldplug` (START=69) holds boot until they are
attached, before ModemManager starts (START=70). Without it the modem can
enumerate while ModemManager is still starting; ModemManager's hotplug
reporting then loses the modem until ModemManager is restarted.

## eMMC and A/B boot

`openwrt-24.10.3`, `openwrt-25.12.3` and `ubuntu-26.04` boot like Shield:
upstream U-Boot (`image/uboot/`) runs `boot.scr` from the eMMC, which loads the
kernel. `openwrt-23.05.6` still boots a kernel directly from a virtio disk.

U-Boot keeps one copy of its environment (16 KiB at `0x3F0000`) in the eMMC's
second hardware boot partition, `/dev/mmcblk0boot1` (`/etc/fw_env.config` on
OpenWrt). The factory image holds the default environment.

### OpenWrt

| Partition | Name | Size | Contents |
|---|---|---|---|
| p1 | `config` | 2 MiB | provisioning data (blank in the SIL) |
| p2, p3 | `boot_rescu`, `rootfs_rescu` | 32 MiB, 224 MiB | rescue system (blank, as shipped) |
| p4, p5 | `boot_a`, `rootfs_a` | 256 MiB, 512 MiB | slot A (`distro_bootpart=4`) |
| p6, p7 | `boot_b`, `rootfs_b` | 256 MiB, 512 MiB | slot B (`distro_bootpart=6`) |
| p8 | `data` | 25 GiB | ext4, mounted at `/root/data` by `/etc/init.d/mount-data` (made if blank) |

A boot partition is ext4 (label `kernel`) holding `Image` and `boot.scr`; a
root partition holds the squashfs root, with OpenWrt's f2fs overlay after it.
The factory image has the same system in slots A and B.

- **Boot.** U-Boot loads `boot.scr` from partition `${distro_bootpart}`; the
  script boots `Image` with `root=PARTUUID=<partition N+1>
  rootfstype=squashfs,f2fs`.
- **Update.** `sysupgrade sysupgrade.tar` (`/lib/upgrade/platform.sh`)
  rewrites the running slot in place: the kernel into its boot partition and
  the squashfs over its root partition. The saved configuration goes to the
  boot partition and `/lib/preinit/79_move_config` restores it at the next
  boot. The environment is not changed.
- **Slot switch.** Manual, as on Shield: the kernel keeps `mmcblk0boot1`
  read-only, so unlock it (`echo 0 > /sys/block/mmcblk0boot1/force_ro`), run
  `fw_setenv distro_bootpart 6` (or 4), lock it again and reboot. Each slot has its own overlay; `data` is shared. There is no boot
  counter or automatic rollback.
- **Watchdog.** procd feeds `/dev/watchdog` (30 s). `ubus call system watchdog
  '{"stop": true}'` stops feeding it, as a hung system would, and the board
  resets.

### Ubuntu

| Partition | Name | Size | Contents |
|---|---|---|---|
| p1 | `boot` | 256 MiB | FAT: `Image`, `initrd.img`, `boot.scr` |
| p2 | `rootfs` | 4 GiB | ext4 root filesystem |
| p3 | `cidata` | 32 MiB | FAT cloud-init seed (empty in the SIL) |
| p4 | `data` | the rest | ext4, mounted at `/root/data` by `mount-data.service` |

One system, no slots; updates go through apt. Ubuntu has no watchdog feeder;
`shield-watchdog.service` loads the watchdog driver, which Ubuntu deny-lists
from autoloading. The upstream kernel needs the generic initrd for its eMMC
modules, and that initrd takes DHCP leases on every NIC;
`shield-sil-initrd-net.service` flushes them before NetworkManager starts, so
NetworkManager brings up its own profiles as on Shield.

The profile chooses this with `[image] storage = "emmc"`, `firmware` (U-Boot)
and `watchdog = true`; QEMU 9.2 or newer is needed for its eMMC device (the
`./sil` container has QEMU 10.2). `--set image.storage=virtio` with a kernel
and a plain disk boots the old way. `tests/system/test_emmc.py` covers the
layout, boot, data partition, watchdog, in-place sysupgrade and the manual slot
switch.

### `shield-vudc-exportd`

OpenWrt's `usbip` tools link `libudev-zero`, which only enumerates devices
with a device node, so `usbipd --device` cannot find the `usbip-vudc` gadget
controller and refuses every import. `image/openwrt/packages/shield-vudc-exportd`
(built with the SDK) does the same import handshake reading sysfs directly:
it hands the connection to the kernel through
`/sys/devices/platform/usbip-vudc.0/usbip_sockfd`.
