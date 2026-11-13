"""Layer-2 "wire" between an emulated Shield port and a host tap device.

QEMU sends and receives Ethernet frames for one NIC over a UDP socket
(``-netdev dgram``). A Wire relays those frames to a tap device that lives in
a network namespace, and can impair the link in both directions: cut it,
drop a percentage of frames, add latency and jitter, or limit the rate.

Cutting the wire models an upstream outage with the cable still plugged in
(carrier stays up in the guest). Pulling the cable is modelled separately by
QEMU's ``set_link``, which the guest sees as carrier loss.
"""

from __future__ import annotations

import fcntl
import heapq
import os
import random
import select
import socket
import struct
import threading
import time
from dataclasses import dataclass

TUNSETIFF = 0x400454CA
IFF_TUN = 0x0001
IFF_TAP = 0x0002
IFF_NO_PI = 0x1000
_MAX_FRAME = 65536


def open_tap(name: str) -> int:
    """Create (or attach to) tap ``name`` in the current namespace; return its fd."""
    if len(name) > 15:
        raise ValueError(f"interface name too long: {name!r}")
    fd = os.open("/dev/net/tun", os.O_RDWR | os.O_NONBLOCK)
    ifr = struct.pack("16sH", name.encode(), IFF_TAP | IFF_NO_PI)
    fcntl.ioctl(fd, TUNSETIFF, ifr)
    return fd


def open_tun(name: str) -> int:
    """Create TUN ``name`` (raw IP, no packet info) in the current namespace; return its fd."""
    if len(name) > 15:
        raise ValueError(f"interface name too long: {name!r}")
    fd = os.open("/dev/net/tun", os.O_RDWR | os.O_NONBLOCK)
    fcntl.ioctl(fd, TUNSETIFF, struct.pack("16sH", name.encode(), IFF_TUN | IFF_NO_PI))
    return fd


@dataclass
class Impairment:
    up: bool = True
    loss_pct: float = 0.0
    delay_ms: float = 0.0
    jitter_ms: float = 0.0
    rate_mbps: float = 0.0  # 0 = unlimited

    def validate(self) -> None:
        if not 0 <= self.loss_pct <= 100:
            raise ValueError("loss_pct must be 0..100")
        if self.delay_ms < 0 or self.jitter_ms < 0 or self.rate_mbps < 0:
            raise ValueError("delay, jitter and rate must be >= 0")


class _Direction:
    """Per-direction token bucket and delay queue."""

    def __init__(self) -> None:
        self.next_free = 0.0  # serialisation time for rate limiting
        self.frames = 0
        self.dropped = 0


class Wire:
    def __init__(self, name: str, tap_fd: int, qemu_port: int, local_port: int, seed: int = 0):
        self.name = name
        self.tap_fd = tap_fd
        os.set_blocking(tap_fd, False)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", local_port))
        self.sock.setblocking(False)
        self.qemu_addr = ("127.0.0.1", qemu_port)
        self.imp = Impairment()
        self._rng = random.Random(seed)
        self._lock = threading.Lock()
        self._queue: list[tuple[float, int, str, bytes]] = []
        self._seq = 0
        self._dirs = {"to_guest": _Direction(), "to_host": _Direction()}
        self._stop = threading.Event()
        self._wake_r, self._wake_w = os.pipe()
        os.set_blocking(self._wake_r, False)
        self._thread = threading.Thread(target=self._run, name=f"wire-{name}", daemon=True)

    # ------------------------------------------------------------------
    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        os.write(self._wake_w, b"x")
        self._thread.join(timeout=5)
        self.sock.close()
        os.close(self.tap_fd)
        os.close(self._wake_r)
        os.close(self._wake_w)

    def set(self, **kw) -> Impairment:
        with self._lock:
            new = Impairment(**{**self.imp.__dict__, **kw})
            new.validate()
            self.imp = new
            if not new.up:
                self._queue.clear()
        os.write(self._wake_w, b"x")
        return new

    def cut(self) -> None:
        self.set(up=False)

    def restore(self) -> None:
        self.set(up=True, loss_pct=0.0, delay_ms=0.0, jitter_ms=0.0, rate_mbps=0.0)

    def stats(self) -> dict:
        return {
            d: {"frames": s.frames, "dropped": s.dropped} for d, s in self._dirs.items()
        }

    # ------------------------------------------------------------------
    def _admit(self, direction: str, frame: bytes) -> None:
        now = time.monotonic()
        d = self._dirs[direction]
        with self._lock:
            imp = self.imp
            if not imp.up or (imp.loss_pct and self._rng.random() * 100 < imp.loss_pct):
                d.dropped += 1
                return
            due = now
            if imp.rate_mbps:
                start = max(now, d.next_free)
                d.next_free = start + (len(frame) * 8) / (imp.rate_mbps * 1e6)
                due = d.next_free
            if imp.delay_ms or imp.jitter_ms:
                jitter = self._rng.uniform(-imp.jitter_ms, imp.jitter_ms) if imp.jitter_ms else 0
                due += max(imp.delay_ms + jitter, 0.0) / 1000.0
            d.frames += 1
            if due <= now:
                self._emit(direction, frame)
            else:
                self._seq += 1
                heapq.heappush(self._queue, (due, self._seq, direction, frame))

    def _emit(self, direction: str, frame: bytes) -> None:
        try:
            if direction == "to_guest":
                self.sock.sendto(frame, self.qemu_addr)
            else:
                os.write(self.tap_fd, frame)
        except (BlockingIOError, OSError):
            self._dirs[direction].dropped += 1

    def _flush_due(self) -> float | None:
        now = time.monotonic()
        with self._lock:
            while self._queue and self._queue[0][0] <= now:
                _, _, direction, frame = heapq.heappop(self._queue)
                self._emit(direction, frame)
            return self._queue[0][0] - now if self._queue else None

    def _run(self) -> None:
        fds = [self.sock.fileno(), self.tap_fd, self._wake_r]
        while not self._stop.is_set():
            timeout = self._flush_due()
            r, _, _ = select.select(fds, [], [], timeout if timeout is not None else 0.5)
            if self._wake_r in r:
                try:
                    os.read(self._wake_r, 64)
                except BlockingIOError:
                    pass
            if self.sock.fileno() in r:
                while True:
                    try:
                        frame = self.sock.recv(_MAX_FRAME)
                    except BlockingIOError:
                        break
                    self._admit("to_host", frame)
            if self.tap_fd in r:
                while True:
                    try:
                        frame = os.read(self.tap_fd, _MAX_FRAME)
                    except BlockingIOError:
                        break
                    if not frame:
                        break
                    self._admit("to_guest", frame)
