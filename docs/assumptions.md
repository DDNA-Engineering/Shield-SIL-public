# Assumptions and open items

Everything here is a placeholder or a known difference from the real Shield.
Each item names what would close it.

## Software under test

* **Upstream OpenWrt is the target.** Shield runs upstream OpenWrt, so the SIL
  boots upstream OpenWrt 24.10.3 (default), 25.12.3 or 23.05.6, or Ubuntu 26.04
  arm64, built from official sources with Shield's defaults. The default
  images run the generic `armsr` (or Ubuntu arm64) kernel on QEMU `virt`; the
  `-imx8mp` images run the i.MX 8M Plus kernel with the SIL's own device tree
  ([imx8mp.md](imx8mp.md)).
* **Upstream packages only.** The images carry upstream packages (`mwan3`,
  ModemManager and the rest of `image/openwrt/shield-packages.list`). Shield's
  own scripts and packages are not included.
  *Close by:* the package list Shield units ship with.

## Hardware interfaces

* **Driver interfaces on stand-in buses.** The guest sees Shield's interfaces
  (I2C bus 1, LEDs, named GPIOs, the EM7595, `usb0`, `BTN_0`;
  [guest-interfaces.md](guest-interfaces.md)). On QEMU `virt` they are carried
  by USB/IP, `gpio-sim` and `uleds` instead of SoC controllers; the `-imx8mp`
  images use the SoC's own I2C, GPIO and watchdog drivers. The hardware bus in
  [hardware-bus.md](hardware-bus.md) is the SIL's own side channel.
  *Close by:* comparing register and sysfs dumps with a Shield unit.
* **GPIO directions.** Closed: Shield's device tree exports `shield:wifi:wake`
  as an output at 1 and the cell lines as outputs; the SIL follows it. Shield's
  current tree drives the Wi-Fi enable pin with `rfkill-gpio`, while its
  documentation names `shield:wifi:enabled`; the SIL keeps that name.
* **Cellular data path.** The EM7595 carries raw-IP on wwan0: QMI interface 8's
  bulk endpoints are joined to a TUN in the cellular ISP's namespace
  (100.64.1.1/24). The bearer is fixed at 100.64.1.50/24, gateway and DNS
  100.64.1.1. Packets flow only in raw-IP link mode with a call up and the
  modem in service. On OpenWrt 24.10.3 and 25.12.3 this is the only cellular
  path: ModemManager connects `wanb` on wwan0 (APN `internet`, IPv4). Delay,
  jitter and loss by signal strength (`sil.cell_link_for`, placeholders) are
  applied to the wwan0 packets. QMAP multiplexing and IPv6 bearers are not
  modelled. Images without Shield's driver interfaces (`network.cellular =
  "nic"`) still use the stand-in NIC `eth2`.
  *Close by:* drive-test link figures from the target carriers.
* **QMI coverage.** Only the requests ModemManager 1.22 sends are answered;
  others get `InvalidQmiCommand`. Service versions reported by CTL are
  chosen, not read from a unit.
  *Close by:* a `qmicli --get-service-version-info` capture from a Shield.
* **EM7595 timings and AT text.** Boot/re-enumeration takes 20 s and
  registration 8 s (placeholders). `ATI`, `AT!GSTATUS?` and the
  `+CSQ` mapping follow the Sierra AT reference layout, not captures from a
  Shield unit. Whether a `sim-select` GPIO change makes the module re-enumerate
  or only re-register is not documented; the model re-registers.
  *Close by:* a console log of a SIM switch and `AT!GSTATUS?` from a unit.

## Model parameters marked TBD in config/shield-v1.5.toml

| Item | Placeholder | Close by |
| --- | --- | --- |
| RAM | 4 GB | confirm SoM part number |
| CPU speed reference | 5336 DMIPS per core (A53 datasheet figure at 1.8 GHz) | real-unit benchmark (docs/calibration.md) |
| Battery capacity | 3000 mAh | battery part number |
| Thermal constants | 6.5 °C/W, 120 s | thermocouple log of an idle and loaded unit |
| Battery internal resistance | 80 mΩ | pulse-load measurement on a Shield pack |
| Battery open-circuit voltage curve | generic 1S Li-ion (`battery.py`) | full discharge log from a Shield pack |
| Charge voltage | 4.20 V | measured float voltage on a unit |
| Brownout cell voltage | 3.30 V | discharge-to-shutdown log from a unit |
| Idle power, without and with cellular | 3.0 W / 4.0 W | input power log from an idle unit |
| Regulator efficiency | 85 % | input vs battery power at a known load |
| Cellular link quality vs RSRP | table in `sil.py` | drive-test data |

## Not modelled

Shield has no fan, no battery thermistor and no fuel gauge, so the SIL has none.
Software estimates battery charge from the voltage the BQ25792 reports.
