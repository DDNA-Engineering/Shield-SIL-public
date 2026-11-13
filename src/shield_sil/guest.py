"""How the harness talks to whatever image is booted.

The image itself is chosen in the profile's ``[image]`` section (or per run with
overrides). The ``[guest]`` section says what kind of system it is, so the
harness knows how to tell that boot finished and how to read interface
addresses. Two adapters ship:

* ``openwrt``: procd console ("Please press Enter"), ``ifstatus`` for addresses.
* ``linux``: any Linux with a shell on the serial console. Optional login,
  configurable prompt and readiness command, ``ip`` for addresses.

Fields in ``[guest]`` override the adapter's defaults, so a new image usually
needs only a profile, not code.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

from .console import Console, ConsoleError

_ADAPTER_DEFAULTS: dict[str, dict[str, Any]] = {
    "openwrt": {
        "console_banner": r"Please press Enter to activate this console",
        "shell_prompt": r"root@[^:]+:[^#]*# ",
        "login_prompt": "",
        "login_user": "",
        "login_password": "",
        "after_login": "dmesg -n 1",
        "ready_command": "test -e /var/run/shield-sil.boot-done",
        "ipv4_command": "ifstatus {logical} | jsonfilter -e '@[\"ipv4-address\"][0].address'",
    },
    "linux": {
        "console_banner": "",
        "shell_prompt": r"[#$] $",
        "login_prompt": r"login: ?$",
        "login_user": "root",
        "login_password": "",
        "after_login": "dmesg -n 1 2>/dev/null; true",
        "ready_command": "true",
        "ipv4_command": "ip -4 -o addr show dev {ifname} | awk '{{print $4}}' | cut -d/ -f1 | head -n1",
    },
}


@dataclass
class Guest:
    kind: str
    cfg: dict[str, Any]
    ifnames: dict[str, str]  # role -> guest interface name (lan, wan, wwan)
    logical: dict[str, str]  # role -> logical name (OpenWRT interface section)

    @classmethod
    def from_profile(cls, profile) -> "Guest":
        g = dict(profile["guest"])
        kind = g.get("adapter", "openwrt")
        if kind not in _ADAPTER_DEFAULTS:
            raise ValueError(f"unknown guest adapter {kind!r}; known: {sorted(_ADAPTER_DEFAULTS)}")
        cfg = {**_ADAPTER_DEFAULTS[kind], **{k: v for k, v in g.items() if k != "adapter"}}
        net = profile["network"]
        ifnames = {r: net[f"{r}_ifname"] for r in ("lan", "wan", "wwan")}
        logical = {r: net.get(f"{r}_logical", r) for r in ("lan", "wan", "wwan")}
        return cls(kind=kind, cfg=cfg, ifnames=ifnames, logical=logical)

    @property
    def has_hw_agent(self) -> bool:
        return bool(self.cfg.get("hw_agent", False))

    @property
    def has_board_interfaces(self) -> bool:
        """The image presents Shield's LEDs, named GPIOs and USB devices through
        Linux drivers (uleds, gpio-sim, USB/IP), not only the hardware bus."""
        return bool(self.cfg.get("board_interfaces", False))

    # ------------------------------------------------------------------
    def wait_ready(self, console: Console, timeout: float, start: int = 0) -> None:
        """Wait until the guest is up. ``start``: a console mark, to wait for a later boot."""
        deadline = time.monotonic() + timeout
        c = self.cfg
        if c["console_banner"]:
            console.wait_for(c["console_banner"], timeout, start=start)
        self._get_prompt(console, deadline)
        if c["after_login"]:
            console.run(c["after_login"], timeout=60, check=False)
        while time.monotonic() < deadline:
            rc, _ = console.run(c["ready_command"], timeout=60, check=False)
            if rc == 0:
                return
            time.sleep(1)
        raise ConsoleError(f"guest not ready: {c['ready_command']!r} never succeeded")

    def _get_prompt(self, console: Console, deadline: float) -> None:
        c = self.cfg
        prompt = re.compile(c["shell_prompt"].encode(), re.M)
        login = re.compile(c["login_prompt"].encode(), re.M) if c["login_prompt"] else None
        while time.monotonic() < deadline:
            mark = len(console.text().encode())
            console.send("\n")
            time.sleep(2)
            tail = console.text().encode()[mark:]
            if login and login.search(tail):
                console.send(c["login_user"] + "\n")
                if c["login_password"]:
                    console.wait_for(r"[Pp]assword: ?", 15, start=mark)
                    console.send(c["login_password"] + "\n")
                time.sleep(2)
                continue
            if prompt.search(tail):
                return
        tail = console.text()[-3000:]
        raise ConsoleError(f"no shell prompt on the console; console tail:\n{tail}")

    def ipv4(self, console: Console, role: str) -> str:
        cmd = self.cfg["ipv4_command"].format(ifname=self.ifnames[role], logical=self.logical[role])
        _, out = console.run(cmd, timeout=30, check=False)
        out = out.strip()
        return out if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", out) else ""
