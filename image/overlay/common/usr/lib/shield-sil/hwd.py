#!/usr/bin/env python3
"""shield-hwd, Python edition: guest end of the SIL hardware bus (state direction).

Used instead of the shell loop when the image has python3 (e.g. Ubuntu), where
forking mv(1) for every record is too slow to keep up under emulation.

Same contract as the shell agent: each "<device>\\t<json>" record from the
virtio-serial port ends up atomically in /var/run/shield/<device>.json, and the
"bus" record of a frame is written only after every record before it. When
several frames are waiting, only the newest reading of each device is written,
so a busy guest catches up instead of replaying stale state.

Usage: hwd.py /dev/vportNpM
"""

import os
import select
import sys
import time

STATE_DIR = os.environ.get("SHIELD_STATE_DIR", "/var/run/shield")
BUS = "bus"


def valid_name(dev: str) -> bool:
    return bool(dev) and all(c.islower() or c.isdigit() or c == "_" for c in dev)


def write(dev: str, payload: str) -> None:
    tmp = os.path.join(STATE_DIR, f".{dev}.tmp")
    with open(tmp, "w") as f:
        f.write(payload + "\n")
    os.replace(tmp, os.path.join(STATE_DIR, f"{dev}.json"))


def main() -> int:
    port = sys.argv[1]
    os.makedirs(STATE_DIR, exist_ok=True)
    fd = os.open(port, os.O_RDONLY)
    buf = b""
    latest: dict[str, str] = {}  # insertion order = arrival order; bus kept last
    last_flush = time.monotonic()
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            # Host side not connected: the port reads EOF until it is.
            select.select([], [], [], 0.2)
            continue
        buf += chunk
        *lines, buf = buf.split(b"\n")
        for raw in lines:
            dev, sep, payload = raw.decode(errors="replace").partition("\t")
            if not sep or not valid_name(dev):
                continue
            latest.pop(dev, None)
            latest[dev] = payload
        # More already waiting? Read it first, so stale frames are skipped
        # (but publish at least every half second even under a steady stream).
        if select.select([fd], [], [], 0)[0] and time.monotonic() - last_flush < 0.5:
            continue
        last_flush = time.monotonic()
        bus = latest.pop(BUS, None)
        for dev, payload in latest.items():
            write(dev, payload)
        if bus is not None:
            write(BUS, bus)
        latest.clear()


if __name__ == "__main__":
    sys.exit(main())
