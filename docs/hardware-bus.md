# Hardware bus

The hardware bus is the boundary between the software under test and the
simulated hardware. On a real Shield this boundary is a set of drivers (I2C
charger and PD controller, LEDs and GPIOs, QMI/AT for the modem). Those driver
interfaces are not presented to the guest yet, so the SIL uses one explicit, documented
interface instead. When the real interfaces are known, add a guest shim that
presents them (for example sysfs/hwmon paths) on top of this bus.

## Transport

Two virtio-serial ports carry newline-delimited `<device>\t<json>` records.
On the i.MX 8M Plus machine they are UARTs instead: UART1 (`/dev/ttymxc0`)
for state and UART3 (`/dev/ttymxc2`) for commands, set to raw mode by the
agents. The device tree leaves DMA off on both, since QEMU does not model the
SoC's SDMA engine.

| Port name | Direction | Guest side |
| --- | --- | --- |
| `org.kform.shield.hw.state` | host → guest, every 0.5 s and after each command | `shield-hwd` writes `/var/run/shield/<device>.json` atomically |
| `org.kform.shield.hw.cmd` | guest → host | `shield-hw set <device> '<json>'` |

### Frames and freshness

The host publishes a frame every 0.5 s and after each guest command. A frame
carries a record for each device whose reading changed since the previous frame
(every device on a full frame, sent every 10 frames and after any drop), then a
`bus` record `{"seq": N, "t": <simulated seconds>}`. `shield-hwd` writes records
in order, so when `/var/run/shield/bus.json` shows seq N, every device file is at
least as new as frame N.

`ShieldSIL.hw(device)` uses this: it waits until the guest has applied the last
frame published before the call, so a test never acts on a reading from before
its own harness change. On a slow host the guest agent can fall behind. The host
then drops frames rather than queueing them (backlog capped at 16 KB), sends the
next frame in full, and counts drops in `HardwareBus.dropped_frames`. If the
guest still cannot catch up, `hw()` raises with the frame it has and the one it
needs.

## Guest interfaces

```sh
shield-hw get bms                       # latest reading as JSON
shield-hw set led '{"name":"User","state":"on"}'
ubus call shield status                 # every device
ubus call shield bms                    # bms | pd | thermal | modem | leds
ubus call shield set_led '{"name":"Battery","state":"blink_slow"}'
ubus call shield modem_reset
```

## Readings

| Device | Fields |
| --- | --- |
| `bms` | `present soc_pct voltage_v current_a charging chg_stat input_power_w system_load_w brownout` |
| `pd` | `attached contract_v contract_a available_w source_pdos` |
| `thermal` | `ambient_c board_c` |
| `modem` | `model firmware responsive state usb_id enumerations active_slot modem_slot sim_select_gpio sim_present carrier signal_dbm data_up` |
| `leds` | `{<name>: off|on|blink_slow|blink_fast}` |
| `gpio_in` | `{"shield:pgood:<rail>": 0|1, ...}`: input lines the guest's gpio-sim chip presents |
| `usbip` | `{"host": "10.0.2.2", "port": N, "busids": [...]}` (only while the SIL runs a USB/IP server) |
| `bus` | `seq t` (frame number and simulated time of the last frame applied) |

`bms.chg_stat` is the BQ25792 `CHG_STAT` code: 0 not charging, 3 fast charge (CC), 4 taper (CV), 7 done.
`modem.state` is one of `off booting fastboot radio_off no_sim searching registered connected unresponsive`.
`modem.usb_id` is `1199:c081` normally, `1199:c082` in fastboot, `null` while off or booting.

## Commands

| Device | JSON |
| --- | --- |
| `led` | `{"name": "<LED>", "state": "off|on|blink_slow|blink_fast"}` |
| `led` (LED class) | `{"name": "<LED>", "brightness": 0..255, "trigger": "<trigger>"}` from `/dev/uleds`; non-zero is on |
| `gpio` | `{"name": "shield:cell:sim-select", "value": 0|1}` for an output line the guest drives |
| `modem` | `{"action":"reset"}`, `{"action":"select_slot","slot":"nano1|nano2|esim"}`, `{"action":"power","on":true}` |

Invalid commands are rejected on the host and recorded in `HardwareBus.errors`;
they never stop the bus.

## Real Shield interfaces (in progress)

Shield firmware does not use this bus; it uses Linux driver interfaces. The SIL
is moving to present those, with this bus kept as the transport between guest
shims and host models. Done so far:

| Interface on Shield | Host model | Guest side |
| --- | --- | --- |
| `/dev/i2c-1`: TPS25751 at 0x20, BQ25792 at 0x6B behind its `I2Cr`/`I2Cw` 4CC proxy | `peripherals/charger.py`, `Board.i2c_transfer()`; fault hooks in `Board.charger_faults` | not yet wired |
| EM7595 on USB (`1199:c081`, AT on `ttyUSB2`, QMI on `cdc-wdm0`), SIM routing through GPIO `shield:cell:sim-select` and UIM slots 1/2 (used by ModemManager) | `peripherals/modem.py`: AT command set, QMI UIM slot status/switch, boot and re-enumeration, fastboot (`1199:c082`), NV slot; fault hooks in `Board.modem.faults` | not yet wired |

The register-level contract is in the `charger.py` docstring (from the TI
TPS25751 and BQ25792 datasheets) and is checked by an i2c-dev client in
`tests/unit/test_charger.py`. The modem's SIM-switch behaviour (GPIO, QMI
switch, `AT!UIMS` fallback) is checked in `tests/unit/test_modem.py`.

## USB/IP devices

The SIL runs a USB/IP server (`src/shield_sil/usbip/`) on host 127.0.0.1 with
a per-run port. The guest reaches it at 10.0.2.2 over a management NIC that is
in no network role, and attaches the bus IDs from the `usbip` record in list
order, so adapter numbering is stable:

| Bus ID | Device | Guest sees |
| --- | --- | --- |
| `1-3` | i2c-tiny-usb `0403:c631`, serial `shield-i2c0`, nothing on the bus | `/dev/i2c-0` |
| `1-2` | i2c-tiny-usb `0403:c631`, serial `shield-i2c1`, forwards to `Board.i2c_transfer(1, ...)` | `/dev/i2c-1` (TPS25751 at 0x20) |
| `1-1` | EM7595 `1199:c081` (listed only while the modem is on USB) | ttyUSB0-2, cdc-wdm0, wwan0 |

A device that leaves USB or re-enumerates has its connection closed, which
the guest sees as an unplug; it re-attaches when the bus ID is listed again.

### EM7595 (`1-1`)

`src/shield_sil/usbip/em7595.py`. Interfaces 0 (DM), 2 (NMEA), 3 (AT) and 8
(QMI) in the Sierra layout that upstream `qcserial` and `qmi_wwan` bind, so
the guest gets ttyUSB0-2, cdc-wdm0 and wwan0 with no custom drivers.

* AT on ttyUSB2 goes to `Modem.at` (echo until `ATE0`). DM and NMEA are silent.
* QMI (`src/shield_sil/usbip/qmi.py`) covers CTL, DMS, NAS, UIM, WDS and WDA
  as ModemManager 1.22 uses them, with layouts from libqmi's service
  definitions. Commands it does not implement answer `InvalidQmiCommand`,
  which is ModemManager's cue for its fallbacks. Like current firmware, the
  legacy DMS UIM commands are absent, so SIM state comes from the UIM service.
* Indications: NAS Serving System when registration changes, WDS Packet
  Service Status when a call drops.
* Slot switch (UIM Switch Slot) and the stuck-NV fault (`Internal`) behave as
  in `Modem.qmi_switch_slot`; `AT!UIMS` is the fallback.
* Fastboot: the device shows as `1199:c082` with one vendor interface.
* A wedged module stays enumerated and never answers AT or QMI.
* wwan0 data: raw-IP on interface 8 (bulk OUT 0x04, IN 0x86) goes to a TUN
  at 100.64.1.1 in the isp2 namespace, routed to the internet host. It flows
  only after WDA Set Data Format selects raw-IP and WDS Start Network
  succeeds, and stops when the modem loses service or wedges. The bearer is
  100.64.1.50/24, gateway and DNS 100.64.1.1.

## Named GPIO lines

| Line | Direction | Initial | Drives / driven by |
| --- | --- | --- | --- |
| `shield:cell:sim-select` | out | 0 | `Modem.set_sim_select_gpio` (0 SIM1, 1 eSIM) |
| `shield:cell:full-card-power` | out | 1 | `Modem.power` |
| `shield:wifi:enabled` | out | 1 | recorded |
| `shield:wifi:wake` | out | 1 | recorded |
| `shield:pgood:5v-out-efuse`, `shield:pgood:3v3-out-efuse` | in | 1 | constant |
| `shield:pgood:dc-efuse` | in | 1 | PD attached |
| `shield:pgood:batt-efuse` | in | 1 | battery present |
