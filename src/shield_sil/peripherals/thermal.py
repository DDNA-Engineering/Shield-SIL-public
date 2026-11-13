"""First-order SoC/board thermal model.

Shield has no fan and no thermistor that software reads (none in the device
tree or firmware). The only temperatures Linux sees are the i.MX8M Plus TMU
zones (cpu, soc; passive trip 85 C, critical 95 C). This model provides that
die temperature for a future guest thermal zone.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class Thermal:
    ambient_c: float
    board_c_per_w: float
    board_time_constant_s: float
    board_c: float = 25.0

    def __post_init__(self) -> None:
        self.board_c = self.ambient_c

    def step(self, dt: float, heat_w: float) -> None:
        target = self.ambient_c + heat_w * self.board_c_per_w
        a = 1.0 - math.exp(-dt / self.board_time_constant_s)
        self.board_c += (target - self.board_c) * a

    def snapshot(self) -> dict:
        return {"ambient_c": round(self.ambient_c, 2), "board_c": round(self.board_c, 2)}
