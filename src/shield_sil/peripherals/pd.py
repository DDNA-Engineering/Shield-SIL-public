"""USB-C Power Delivery sink (TI PD controller on the Shield)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PowerDelivery:
    """USB-PD sink that negotiates the highest voltage the source offers.

    The sink accepts the fixed USB PD voltages listed in the profile
    (``power.pd_voltages``).
    """

    sink_voltages: list[float]
    max_current_a: float
    attached: bool = False
    source_pdos: list[tuple[float, float]] = field(default_factory=list)
    contract_v: float = 0.0
    contract_a: float = 0.0

    def attach(self, pdos: list[tuple[float, float]]) -> None:
        """Plug in a source advertising ``pdos`` as (volts, max amps) pairs."""
        if not pdos:
            raise ValueError("a PD source must advertise at least one PDO")
        self.attached = True
        self.source_pdos = sorted(pdos)
        self._negotiate()

    def detach(self) -> None:
        self.attached = False
        self.source_pdos = []
        self.contract_v = 0.0
        self.contract_a = 0.0

    def _negotiate(self) -> None:
        best = (0.0, 0.0)
        for v, a in self.source_pdos:
            if any(abs(v - s) < 0.01 for s in self.sink_voltages):
                cand = (v, min(a, self.max_current_a))
                if cand[0] * cand[1] > best[0] * best[1]:
                    best = cand
        if best == (0.0, 0.0):
            # No common PDO: USB default 5 V at 0.5 A (vSafe5V).
            best = (5.0, 0.5)
        self.contract_v, self.contract_a = best

    @property
    def available_w(self) -> float:
        return self.contract_v * self.contract_a if self.attached else 0.0

    def snapshot(self) -> dict:
        return {
            "attached": self.attached,
            "contract_v": round(self.contract_v, 2),
            "contract_a": round(self.contract_a, 2),
            "available_w": round(self.available_w, 2),
            "source_pdos": [list(p) for p in self.source_pdos],
        }
