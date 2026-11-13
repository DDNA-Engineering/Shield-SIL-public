"""The whole Shield board: ties the peripheral models together."""

from __future__ import annotations

import threading
from typing import Any

from ..profile import Profile
from .battery import Battery
from .charger import (
    TPS25751_ADDR,
    Bq25792,
    ChargerFaults,
    ChargerInputs,
    I2cBus,
    Tps25751,
)
from .gpio import Gpios
from .leds import Leds
from .modem import Modem
from .pd import PowerDelivery
from .thermal import Thermal


class Board:
    """Thread-safe aggregate of all peripheral models.

    ``step(dt)`` advances physics. ``guest_command`` applies a command coming
    from the software under test. Harness controls (plug PD, wedge modem, ...)
    are plain methods on the sub-models, called under ``board.lock``.
    """

    def __init__(self, profile: Profile) -> None:
        self.profile = profile
        self.lock = threading.RLock()
        # Where the guest reaches the USB/IP server (set by the SIL, kept across reset()).
        self.usbip_endpoint: tuple[str, int] | None = None
        # True on the i.MX 8M Plus machine: the I2C buses, LEDs, GPIOs and the
        # button are the SoC's own (imx8mp.py), so only the modem is on USB/IP.
        self.soc_peripherals = False
        self.reset()

    def reset(self) -> None:
        """Return every peripheral to its power-on default (bench: PD at 20 V, SIM in slot 1)."""
        with self.lock:
            self._build()

    def _build(self) -> None:
        p = self.profile
        self.t = 0.0
        self.pd = PowerDelivery(
            sink_voltages=list(p["power"]["pd_voltages"]),
            max_current_a=p["power"]["pd_max_current_a"],
        )
        b = p["battery"]
        self.battery = Battery(
            capacity_mah=b["capacity_mah"],
            charge_current_a=b["charge_current_a"],
            cells_series=b["cells_series"],
        )
        self.brownout_v = float(b["brownout_v"])
        th = p["thermal"]
        self.thermal = Thermal(
            ambient_c=th["ambient_c"],
            board_c_per_w=th["board_c_per_w"],
            board_time_constant_s=th["board_time_constant_s"],
        )
        m = p["modem"]
        self.modem = Modem(
            model=m["model"],
            firmware=m["firmware"],
            slots=list(m["sim_slots"]),
            active_slot=m["active_slot"],
            registration_time_s=m["registration_time_s"],
            boot_time_s=m.get("boot_time_s", 20.0),
        )
        self.leds = Leds(names=list(p["leds"]["names"]))
        self.gpio = Gpios()
        self.gpio.on_output["shield:cell:sim-select"] = self.modem.set_sim_select_gpio
        self.gpio.on_output["shield:cell:full-card-power"] = lambda v: self.modem.power(bool(v))
        # Guest I2C bus 1 (DT i2c2): the TPS25751 with the BQ25792 behind its proxy.
        self.charger_faults = ChargerFaults()
        self.bq25792 = Bq25792(self._charger_inputs, self.charger_faults)
        self.tps25751 = Tps25751(self.bq25792, self.charger_faults)
        self.i2c = {1: I2cBus(1)}
        self.i2c[1].attach(TPS25751_ADDR, self.tps25751)
        self.input_power_w = 0.0
        self.system_load_w = 0.0
        self.brownout = False
        # Default: on USB-PD at 20 V, the normal bench condition.
        self.pd.attach([(5.0, 3.0), (9.0, 3.0), (15.0, 3.0), (20.0, 3.0)])

    # ------------------------------------------------------------------
    def _load_w(self) -> float:
        pw = self.profile["power"]
        base = pw["idle_w_with_som"]
        cell_extra = pw["idle_w_with_cell"] - pw["idle_w_with_som"]
        modem_w = cell_extra if self.modem.power_w > 0 else 0.0
        return base + modem_w

    def _charger_inputs(self) -> ChargerInputs:
        return ChargerInputs(
            vbus_present=self.pd.attached,
            battery_present=self.battery.present,
            vbat_mv=round(self.battery.voltage * 1000),
            ibat_ma=round(self.battery.current_a * 1000),
            chg_stat=self.battery.chg_stat,
        )

    def i2c_transfer(self, bus: int, addr: int, write: bytes, read_len: int) -> bytes:
        """One guest i2c-dev transfer: an optional write, then an optional read."""
        with self.lock:
            b = self.i2c.get(bus)
            if b is None:
                raise ValueError(f"no I2C bus {bus}")
            if write or not read_len:
                b.write(addr, write)  # a zero-length write is an address probe
            return b.read(addr, read_len) if read_len else b""

    def step(self, dt: float) -> None:
        with self.lock:
            eff = self.profile["power"]["regulator_efficiency"]
            load = self._load_w()
            self.system_load_w = load

            pd_w = self.pd.available_w * eff
            if pd_w >= load:
                surplus = pd_w - load
                self.battery.step(dt, charge_power_w=surplus, load_w=0.0)
                charge_w = max(self.battery.current_a, 0.0) * max(self.battery.voltage, 0.0)
                self.input_power_w = (load + charge_w) / eff
                self.brownout = False
                heat = self.input_power_w - charge_w
            else:
                # PD absent or too weak: battery carries the rest through the regulators.
                from_batt = (load - pd_w) / eff
                unmet = self.battery.step(dt, charge_power_w=0.0, load_w=from_batt)
                self.input_power_w = self.pd.available_w
                # The board browns out when the cell sags below power.brownout_v.
                self.brownout = unmet > 0 or self.battery.voltage < self.brownout_v
                heat = load / eff
            self.thermal.step(dt, heat_w=heat)
            self.modem.step(dt)
            self.gpio.set_input("shield:pgood:dc-efuse", self.pd.attached)
            self.gpio.set_input("shield:pgood:batt-efuse", self.battery.present)
            self.t += dt

    # ------------------------------------------------------------------
    def guest_command(self, dev: str, data: dict[str, Any]) -> None:
        """Apply a command written by the guest to the hardware command port."""
        with self.lock:
            if dev == "led":
                if "state" in data:
                    self.leds.set(str(data["name"]), str(data["state"]), self.t)
                else:  # /dev/uleds brightness from the guest LED class
                    self.leds.set_brightness(str(data["name"]), int(data["brightness"]),
                                             str(data.get("trigger", "")), self.t)
            elif dev == "gpio":
                self.gpio.set_output(str(data["name"]), int(data["value"]), self.t)
            elif dev == "modem":
                action = data.get("action")
                if action == "reset":
                    self.modem.reset()
                elif action == "select_slot":
                    self.modem.select_slot(str(data["slot"]))
                elif action == "power":
                    self.modem.power(bool(data["on"]))
                else:
                    raise ValueError(f"unknown modem action {action!r}")
            else:
                raise ValueError(f"unknown device {dev!r}")

    def snapshot(self) -> dict[str, dict]:
        with self.lock:
            bms = self.battery.snapshot()
            bms["input_power_w"] = round(self.input_power_w, 3)
            bms["system_load_w"] = round(self.system_load_w, 3)
            bms["brownout"] = self.brownout
            return {
                "bms": bms,
                "pd": self.pd.snapshot(),
                "thermal": self.thermal.snapshot(),
                "modem": self.modem.snapshot(),
                "leds": self.leds.snapshot(),
                "gpio_in": self.gpio.inputs(),
                **({"usbip": self.usbip_record()} if self.usbip_endpoint else {}),
            }

    # USB/IP bus IDs, in the order the guest must attach them: the SoM's i2c-0
    # (no devices), the battery bus i2c-1, then the modem while it is on USB.
    BUSID_I2C0, BUSID_I2C1, BUSID_MODEM, BUSID_BUTTON = "1-3", "1-2", "1-1", "1-4"

    def usbip_busids(self) -> list[str]:
        # The button is attached before the modem, so the modem (which leaves and
        # comes back when it restarts) never takes another device's port.
        ids = [] if self.soc_peripherals else [self.BUSID_I2C0, self.BUSID_I2C1, self.BUSID_BUTTON]
        if self.modem.usb_id is not None:
            ids.append(self.BUSID_MODEM)
        return ids

    def usbip_record(self) -> dict:
        host, port = self.usbip_endpoint  # type: ignore[misc]
        return {"host": host, "port": port, "busids": self.usbip_busids()}
