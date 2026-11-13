# Validation status

What the SIL has been checked against, and what it has not.

**No result from this SIL has been compared with a real Shield unit.** Every
behaviour below is checked against the SIL's own models and against upstream
software, not against hardware. Treat results as "the software does X when the
modelled hardware does Y", not as a measurement of a Shield.

## Where tests run

- **GitHub CI** (`.github/workflows/ci.yml`, every push and pull request): quick
  checks only. Lint, shell syntax, the image catalog, unit tests (`tests/unit`,
  no VM) and a collect-only pass over `tests/system`.
- **Full runs** on a developer machine with `./sil build <image>` and
  `./sil test <image>`: each image built from official sources, then
  `tests/system` against it. Under emulation (no KVM) one image takes about
  1 to 1.5 hours.

## System tests per image

"yes": runs and passes on that image. "skip": the test skips itself on that
image, for the reason given.

| Behaviour | Test | OpenWrt 24.10.3 / 25.12.3 | Ubuntu 26.04 |
| --- | --- | --- | --- |
| Shield's eMMC: boot partitions and GPT layout per OS | `test_emmc_has_shields_partitions` | yes | yes |
| `data` mounted at `/root/data`; hardware watchdog present | `test_data_partition_is_mounted_at_root_data`, `test_board_has_a_watchdog` | yes | yes |
| U-Boot boots slot A from squashfs with an f2fs overlay | `test_factory_image_boots_slot_a` | yes | skip (one slot) |
| Ubuntu boots its `rootfs` partition through U-Boot | `test_ubuntu_boots_from_its_rootfs_partition` | skip | yes |
| Watchdog resets a hung board (procd stops feeding it) | `test_watchdog_resets_a_hung_board` | yes | skip (no feeder, as on Shield) |
| `sysupgrade` rewrites the running slot; config and data kept | `test_sysupgrade_rewrites_the_running_slot_and_keeps_config_and_data` | yes | skip (apt) |
| U-Boot environment read-only until unlocked; manual A/B switch and back | `test_environment_is_read_only_until_unlocked`, `test_manual_slot_switch_boots_slot_b_and_back` | yes | skip |
| Shield's LEDs, named GPIOs, kernel modules, USB/IP agent | `test_board_interfaces.py` | yes | yes |
| Management link outside every network role | `test_management_link_is_outside_every_network_role` | yes | yes |
| Guest sees 4x Cortex-A53, RAM within profile, 32 GB storage | `test_compute.py` | yes | yes |
| Host CPU use capped to 4 cores | `test_host_cpu_is_capped` | yes | yes |
| One core no faster than a Shield core (Dhrystone, datasheet target) | `test_sil_is_not_faster_than_device` | yes | skip (no dhrystone) |
| LAN gateway answers; LuCI login page | `test_network.py` | yes | ping only (no LuCI) |
| Wired WAN lease; cellular address | `test_both_uplinks_get_addresses` | yes | yes |
| Cellular through ModemManager on the EM7595's `wwan0` | `test_cellular_runs_through_the_modem` | yes | yes |
| Internet prefers wired WAN; failover to cellular on cable pull and back; nothing with both down | `test_network.py` | yes | yes |
| Failover on an upstream outage with the link up (mwan3 tracking) | `test_failover_on_upstream_outage_without_carrier_loss` | yes | skip (no mwan3) |
| Battery discharges on PD unplug, charges on replug at 9 V; SIM switch to an empty slot | `test_hardware.py` | yes | yes |
| Guest LED command reaches the host; bad command rejected; modem hang until power cycle | `test_hardware.py` | yes | skip (ubus) |
| Wi-Fi AP in `br-lan`; WPA2 client gets a lease, reaches the internet; `wifi down`/`up` | `test_wifi.py` | yes | yes |
| Button reaches `/etc/rc.button/BTN_0` with the hold time | `test_press_runs_rc_button_with_hold_time` | yes | skip (no rc.button) |
| Button press and hold reach evdev as `BTN_0` | `test_button.py` | yes | yes |
| USB-C `usb0` is a USB gadget in `br-lan`; laptop reaches Shield and internet; unplug/replug | `test_usbc.py` | yes | yes |

## Last full runs

On a Windows 11 workstation (WSL2, Docker, 32 cores, no KVM), 2026-11-10, every image
rebuilt from official sources:

| Image | Result |
| --- | --- |
| OpenWrt 24.10.3 | 47 passed, 3 skipped, 0 failed (1 h 14 min) |
| OpenWrt 25.12.3 | 47 passed, 3 skipped, 0 failed (1 h 33 min) |
| Ubuntu 26.04 | 34 passed, 16 skipped, 0 failed (1 h 00 min) |

OpenWrt 23.05.6 has not had a full run since the eMMC and driver-interface work;
it keeps the older plain-disk setup.

The i.MX 8M Plus images (patched QEMU `imx8mp-evk`, [imx8mp.md](imx8mp.md)),
2026-11-10:

| Image | Where | Result |
| --- | --- | --- |
| OpenWrt 24.10.3 `-imx8mp` | workstation | 47 passed, 2 skipped, 0 failed (1 h 18 min) |
| OpenWrt 25.12.3 `-imx8mp` | workstation | 47 passed, 2 skipped, 0 failed (1 h 39 min) |
| Ubuntu 26.04 `-imx8mp` | 4-core cloud VM | 34 passed, 15 skipped, 0 failed (1 h 19 min) |
| Ubuntu 26.04 `-imx8mp`, with QEMU patch 0013 | workstation | 34 passed, 16 skipped, 0 failed (1 h 03 min) |
| OpenWrt 25.12.3 `-imx8mp`, with patch 0013 and Shield's pins | 4-core cloud VM | 47 passed, 2 skipped, 0 failed (1 h 25 min) |
| All three `-imx8mp`, board and button tests with Shield's pins | 4-core cloud VM | 0 failed |

## Known gaps

| Gap | Effect | Status |
| --- | --- | --- |
| No real-unit comparison | Model parameters (battery capacity, internal resistance, thermal constants, CPU speed) are placeholders; see [assumptions.md](assumptions.md) | needs a Shield unit |
| CPU speed target from the datasheet | Each core is limited to 5336 DMIPS (A53 at 1.8 GHz, published figure); not a Shield measurement | replace with a unit's Dhrystone score ([calibration.md](calibration.md)) |
| Generic kernels on virt | The `virt` images run the generic OpenWrt `armsr` or Ubuntu arm64 kernel, not the i.MX8M Plus kernel and device tree. Ubuntu's eMMC drivers are modules, so it boots with the generic initrd (Shield's Ubuntu boots without one) | closed by the `-imx8mp` images (TF-A, U-Boot, the release's imx kernel or Ubuntu's own kernel, Shield SIL device tree; [imx8mp.md](imx8mp.md)). Ubuntu there still boots with the generic initrd |
| Wi-Fi radio is simulated | `mac80211_hwsim` and the Linux Wi-Fi stack, not the NXP IW611 and its driver; no RF range or interference. SSID and key are SIL defaults | by design (QEMU cannot emulate the IW611) |
| Button is a USB HID device | Same `BTN_0` event and `/etc/rc.button` path as gpio-keys; the input device's name and bus differ | by design on `virt`; on the `-imx8mp` images the button is `gpio-keys` on a SoC GPIO |
| mwan3 only on OpenWrt | Ubuntu fails over on carrier loss only (route metrics) | by design for now |
| OpenWrt 23.05.6 | No eMMC, driver interfaces or ModemManager: cellular data on a stand-in NIC (`eth2`) | kept as the older baseline |
| Modem re-attach miss | After a board reset the guest's USB/IP agent `shield-usbip` sometimes misses the modem coming back (about twice per full 25.12.3 run, not seen on 24.10.3 or Ubuntu in the last runs). It used to leave the modem off for about 7 minutes; the agent's re-check every 10 s now re-attaches it and logs `found by the periodic re-check`. Why the fast path misses the change is not known | bounded to 10 s; root cause open |
| Timing | QEMU emulates the instruction set, not A53 timing; the speed limit bounds a core's Dhrystone score, not cache, memory or eMMC timing | needs a Shield unit |
