# Changelog

All notable changes to Shield SIL are listed here. Versions follow
[Semantic Versioning](https://semver.org/).

## v1.0.0 (2026-11-12)

First release. Shield SIL boots a real OpenWrt or Ubuntu image on an emulated
Kform Shield, wires it to a simulated LAN, wired ISP and cellular carrier, and
lets `pytest` tests break the hardware and the network from the outside.

### Highlights

- **One command.** `./sil` runs everything in a Docker container on Linux or
  Windows (WSL2): `./sil up`, `./sil ssh`, `./sil test`, `./sil button`.
- **Selectable images.** `openwrt-24.10.3` (default), `openwrt-25.12.3` and
  `ubuntu-26.04`, built from official upstream sources, plus `openwrt-23.05.6`
  as a baseline. Add your own through the image catalog.
- **Emulated i.MX 8M Plus.** The `-imx8mp` images run TF-A, U-Boot and the
  SoC's own kernel drivers (`i2c-imx`, `gpio-leds`, `gpio-keys`, `imx2-wdt`,
  `sdhci-esdhc-imx`, FEC) on upstream QEMU v10.2.1 with the SIL's patches.
- **Held to Shield's compute.** 4x Cortex-A53, 4 GB RAM, 32 GB eMMC, with a
  per-core speed cap from the A53 datasheet Dhrystone figure.
- **Shield's driver interfaces.** TPS25751 PD controller and BQ25792 charger
  on I2C bus 1, the Sierra EM7595 over USB driven by ModemManager on `wwan0`,
  front-panel LEDs and button, named GPIO lines, a USB-C network gadget on
  `usb0`, and a Wi-Fi AP on a simulated radio.
- **Real boot and update path.** Shield's eMMC layout, upstream U-Boot, A/B
  slots with a manual switch, `sysupgrade` and a hardware watchdog.
- **Network lab.** Wires that can be cut, delayed, dropped or rate-limited,
  cellular signal that shapes the link, and mwan3 failover on OpenWrt.
  `./sil up --internet` gives the emulated Shield your real internet.
- **Light CI.** GitHub CI runs lint, shell syntax, the image catalog, unit
  tests and a collect-only pass over the system tests. Full runs happen on a
  developer machine.

### Validation

Full system suites, every image rebuilt from official sources, no failures:

| Image | Passed | Skipped | Failed |
| --- | :---: | :---: | :---: |
| `openwrt-24.10.3` | 47 | 3 | 0 |
| `openwrt-25.12.3` | 47 | 3 | 0 |
| `ubuntu-26.04` | 34 | 16 | 0 |
| `openwrt-24.10.3-imx8mp` | 47 | 2 | 0 |
| `openwrt-25.12.3-imx8mp` | 47 | 2 | 0 |
| `ubuntu-26.04-imx8mp` | 34 | 16 | 0 |

Per-test results are in [docs/validation-status.md](docs/validation-status.md).

### Known issues

- **Not validated on hardware.** No result has been compared with a real
  Shield. Battery, power, thermal and CPU speed values are placeholders
  ([docs/assumptions.md](docs/assumptions.md)).
- **Rare cellular miss after a reset.** Seen once, on a 4-core cloud VM: the guest's
  first `simple-connect` after a board reset returned
  `MobileEquipment.Unknown`, netifd did not retry, and `wanb` stayed down for
  the rest of the run. Root cause open.
- **Modem re-attach miss.** After a board reset the guest's USB/IP helper
  sometimes misses the modem coming back (about twice per full 25.12.3 run).
  A 10 s re-check recovers it. Root cause open.
- **mwan3 stale hotplug.** mwan3 2.12 can leave `wan` out of the
  `wan_wanb` policy after a quick WAN unplug and replug. The harness detects
  this, restarts mwan3 and records a warning. Not yet reported upstream.
- **Slow full runs.** One image's full suite takes about 1 to 1.5 hours under
  emulation without KVM. Work to shorten it is not finished.
- **`openwrt-23.05.6`** keeps the older plain-disk setup and has not had a
  full run since the eMMC and driver-interface work.
