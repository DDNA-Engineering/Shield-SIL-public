"""Shield's named GPIO lines (device tree ``gpio-line-names``).

Outputs are driven by the guest (``gpio`` command on the hardware bus) and
reach the model they control. Inputs are driven by the board model and sent to
the guest in the ``gpio_in`` record, where a gpio-sim chip presents them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

# name -> (direction, initial value)
SHIELD_LINES: dict[str, tuple[str, int]] = {
    "shield:cell:sim-select": ("out", 0),        # 0 SIM1, 1 eSIM (behind modem slot 1)
    "shield:cell:full-card-power": ("out", 1),   # 1 = M.2 card powered
    "shield:wifi:enabled": ("out", 1),
    "shield:wifi:wake": ("out", 1),              # output, 1 at boot (Shield device tree)
    "shield:pgood:5v-out-efuse": ("in", 1),
    "shield:pgood:dc-efuse": ("in", 1),
    "shield:pgood:batt-efuse": ("in", 1),
    "shield:pgood:3v3-out-efuse": ("in", 1),
}


@dataclass
class Gpios:
    lines: dict[str, tuple[str, int]] = field(default_factory=lambda: dict(SHIELD_LINES))
    values: dict[str, int] = field(default_factory=dict)
    on_output: dict[str, Callable[[int], None]] = field(default_factory=dict)
    history: list[tuple[float, str, int]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.values = {n: v for n, (_, v) in self.lines.items()}

    def set_output(self, name: str, value: int, t: float = 0.0) -> None:
        """A guest write to an output line."""
        if name not in self.lines:
            raise ValueError(f"unknown GPIO {name!r}; known: {sorted(self.lines)}")
        if self.lines[name][0] != "out":
            raise ValueError(f"GPIO {name!r} is an input")
        value = 1 if value else 0
        if self.values[name] == value:
            return
        self.values[name] = value
        self.history.append((t, name, value))
        cb = self.on_output.get(name)
        if cb:
            cb(value)

    def set_input(self, name: str, value: int) -> None:
        """The board drives an input line."""
        if self.lines.get(name, ("", 0))[0] != "in":
            raise ValueError(f"{name!r} is not an input line")
        self.values[name] = 1 if value else 0

    def inputs(self) -> dict[str, int]:
        return {n: self.values[n] for n, (d, _) in self.lines.items() if d == "in"}

    def snapshot(self) -> dict[str, int]:
        return dict(self.values)
