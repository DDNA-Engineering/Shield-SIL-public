"""Front-panel LEDs driven by the guest."""

from __future__ import annotations

from dataclasses import dataclass, field

VALID_STATES = {"off", "on", "blink_slow", "blink_fast"}


@dataclass
class Leds:
    names: list[str]
    state: dict[str, str] = field(default_factory=dict)
    history: list[tuple[float, str, str]] = field(default_factory=list)
    # Last brightness/trigger seen from the guest LED class (uleds), per LED.
    detail: dict[str, dict] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.state = {n: "off" for n in self.names}

    def set(self, name: str, state: str, t: float) -> None:
        if name not in self.state:
            raise ValueError(f"unknown LED {name!r}; known: {sorted(self.state)}")
        if state not in VALID_STATES:
            raise ValueError(f"LED state must be one of {sorted(VALID_STATES)}")
        if self.state[name] != state:
            self.state[name] = state
            self.history.append((t, name, state))

    def set_brightness(self, name: str, brightness: int, trigger: str, t: float) -> None:
        """Brightness from the guest's LED class device; any non-zero value is on."""
        if not 0 <= brightness <= 255:
            raise ValueError("LED brightness must be 0..255")
        self.set(name, "on" if brightness else "off", t)
        self.detail[name] = {"brightness": brightness, "trigger": trigger}

    def snapshot(self) -> dict:
        return dict(self.state)
