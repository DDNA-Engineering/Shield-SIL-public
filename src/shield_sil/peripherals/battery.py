"""1S Li-ion pack and the BQ25792 charge loop.

Software on the board has no fuel gauge or battery thermistor to read. What it
sees is the BQ25792's VBAT ADC (terminal voltage, under load). So this model's
job is to produce a realistic terminal voltage: open-circuit voltage for the
state of charge, minus the drop across the pack's internal resistance at the
current being drawn, plus the charger's own state (CHG_STAT).

The open-circuit voltage curve is a generic 1S Li-ion (NMC/graphite) shape,
not a measurement of the Shield pack. Replace it with a measured curve
(docs/assumptions.md) before treating state-of-charge results as authoritative.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass

from .charger import CHG_DONE, CHG_FAST_CC, CHG_NOT_CHARGING, CHG_TAPER_CV

# Generic 1S Li-ion open-circuit voltage (mV) vs state of charge (%). TBD: measure.
GENERIC_1S_OCV = [
    (4200, 100),
    (4060, 90),
    (3980, 80),
    (3920, 70),
    (3870, 60),
    (3820, 50),
    (3790, 40),
    (3770, 30),
    (3740, 20),
    (3680, 10),
    (3450, 5),
    (3000, 0),
]
INTERNAL_RESISTANCE_OHM = 0.08  # TBD: generic small-pack value, not measured.
CHARGE_VOLTAGE_V = 4.20  # BQ25792 charge voltage for one Li-ion cell
TERMINATION_CURRENT_A = 0.05

_OCV_TABLE = sorted((pct / 100.0, mv / 1000.0) for mv, pct in GENERIC_1S_OCV)


def ocv(soc: float) -> float:
    """Open-circuit voltage of one cell at state of charge ``soc`` (0..1)."""
    soc = min(max(soc, 0.0), 1.0)
    xs = [x for x, _ in _OCV_TABLE]
    i = bisect_left(xs, soc)
    if i == 0:
        return _OCV_TABLE[0][1]
    x0, y0 = _OCV_TABLE[i - 1]
    x1, y1 = _OCV_TABLE[i]
    return y0 + (y1 - y0) * (soc - x0) / (x1 - x0)


@dataclass
class Battery:
    capacity_mah: float
    charge_current_a: float
    cells_series: int = 1
    present: bool = True
    soc: float = 0.8
    current_a: float = 0.0  # + charging, - discharging
    chg_stat: int = CHG_NOT_CHARGING
    charging_enabled: bool = True

    @property
    def voltage(self) -> float:
        """Terminal voltage, what the BQ25792 VBAT ADC reads."""
        if not self.present:
            return 0.0
        v = self.cells_series * ocv(self.soc) + self.current_a * INTERNAL_RESISTANCE_OHM
        if self.chg_stat == CHG_TAPER_CV:
            v = min(v, self.cells_series * CHARGE_VOLTAGE_V)
        return v

    @property
    def charging(self) -> bool:
        return self.chg_stat not in (CHG_NOT_CHARGING, CHG_DONE)

    def step(self, dt: float, charge_power_w: float, load_w: float) -> float:
        """Advance ``dt`` seconds.

        ``charge_power_w`` is input power left over for charging (0 on battery).
        ``load_w`` is power the system draws from the battery (0 on input power).
        Returns the load power the battery could not supply (W).
        """
        if not self.present:
            self.current_a = 0.0
            self.chg_stat = CHG_NOT_CHARGING
            return load_w

        v = max(self.cells_series * ocv(self.soc), 0.1)
        if load_w > 0:
            self.chg_stat = CHG_NOT_CHARGING
            if self.soc <= 0.0:
                self.current_a = 0.0
                return load_w
            self.current_a = -load_w / v
        elif charge_power_w > 0 and self.charging_enabled:
            if self.chg_stat == CHG_DONE and self.soc > 0.95:
                # Terminated: the charger waits for VBAT to sag below recharge.
                self.current_a = 0.0
            else:
                i = min(self.charge_current_a, charge_power_w / v)
                if self.soc >= 0.9:
                    i *= max((1.0 - self.soc) / 0.1, 0.0)
                    self.chg_stat = CHG_TAPER_CV
                else:
                    self.chg_stat = CHG_FAST_CC
                if self.soc >= 1.0 or (self.chg_stat == CHG_TAPER_CV and i < TERMINATION_CURRENT_A):
                    i = 0.0
                    self.chg_stat = CHG_DONE
                self.current_a = i
        else:
            self.current_a = 0.0
            if self.chg_stat != CHG_DONE or charge_power_w <= 0:
                self.chg_stat = CHG_NOT_CHARGING

        delta_ah = self.current_a * dt / 3600.0
        self.soc = min(max(self.soc + delta_ah / (self.capacity_mah / 1000.0), 0.0), 1.0)
        return 0.0

    def snapshot(self) -> dict:
        return {
            "present": self.present,
            "soc_pct": round(self.soc * 100.0, 2),
            "voltage_v": round(self.voltage, 3),
            "current_a": round(self.current_a, 3),
            "charging": self.charging,
            "chg_stat": self.chg_stat,
        }
