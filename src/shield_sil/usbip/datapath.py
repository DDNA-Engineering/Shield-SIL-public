"""Where the EM7595's wwan0 packets go on the host.

A data path carries raw IP packets between the modem's QMI data endpoints and
the simulated cellular network. In a SIL run it is a TUN device in the
cellular ISP's namespace (``NetLab.open_cell_data``); tests use ``PacketQueue``.
"""

from __future__ import annotations

import heapq
import os
import queue
import random
import select
import threading
import time
from collections.abc import Callable

from ..wire import Impairment

Receiver = Callable[[bytes], None]


class TunDataPath:
    """Raw IP over a TUN file descriptor (IFF_TUN | IFF_NO_PI)."""

    def __init__(self, fd: int, mtu: int = 1500) -> None:
        self.fd = fd
        self.mtu = mtu
        self._receiver: Receiver | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="cell-data", daemon=True)
        self._thread.start()

    def set_receiver(self, receiver: Receiver | None) -> None:
        self._receiver = receiver

    def send(self, packet: bytes) -> None:
        try:
            os.write(self.fd, packet)
        except OSError:
            pass  # interface down or packet rejected: dropped, like on air

    def _loop(self) -> None:
        while not self._stop.is_set():
            r, _, _ = select.select([self.fd], [], [], 0.2)
            if not r:
                continue
            try:
                pkt = os.read(self.fd, 65536)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError:
                return
            rx = self._receiver
            if rx is not None:
                rx(pkt)

    def close(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        self._thread.join(timeout=1)
        os.close(self.fd)


class ImpairedDataPath:
    """Radio-link impairment (delay, jitter, loss, rate) in both directions of a data path.

    Wraps a data path the way ``Wire`` impairs an Ethernet port, so the cellular
    link quality the modem model reports (signal strength) shapes ``wwan0``
    traffic. ``up=False`` drops everything, like a radio link with no coverage.
    """

    def __init__(self, inner, seed: int = 0) -> None:
        self.inner = inner
        self.imp = Impairment()
        self._rng = random.Random(seed)
        self._lock = threading.Condition()
        self._queue: list[tuple[float, int, str, bytes]] = []
        self._seq = 0
        self._next_free = {"uplink": 0.0, "downlink": 0.0}
        self.frames = {"uplink": 0, "downlink": 0}
        self.dropped = {"uplink": 0, "downlink": 0}
        self._receiver: Receiver | None = None
        self._stop = False
        inner.set_receiver(lambda pkt: self._admit("downlink", pkt))
        self._thread = threading.Thread(target=self._run, name="cell-impair", daemon=True)
        self._thread.start()

    def set(self, **kw) -> Impairment:
        with self._lock:
            new = Impairment(**{**self.imp.__dict__, **kw})
            new.validate()
            self.imp = new
            if not new.up:
                self._queue.clear()
            self._lock.notify()
        return new

    def restore(self) -> None:
        self.set(up=True, loss_pct=0.0, delay_ms=0.0, jitter_ms=0.0, rate_mbps=0.0)

    def set_receiver(self, receiver: Receiver | None) -> None:
        self._receiver = receiver

    def send(self, packet: bytes) -> None:
        self._admit("uplink", packet)

    def _admit(self, direction: str, packet: bytes) -> None:
        now = time.monotonic()
        with self._lock:
            imp = self.imp
            if not imp.up or (imp.loss_pct and self._rng.random() * 100 < imp.loss_pct):
                self.dropped[direction] += 1
                return
            due = now
            if imp.rate_mbps:
                start = max(now, self._next_free[direction])
                self._next_free[direction] = start + len(packet) * 8 / (imp.rate_mbps * 1e6)
                due = self._next_free[direction]
            if imp.delay_ms or imp.jitter_ms:
                jitter = self._rng.uniform(-imp.jitter_ms, imp.jitter_ms) if imp.jitter_ms else 0.0
                due += max(imp.delay_ms + jitter, 0.0) / 1000.0
            self.frames[direction] += 1
            if due > now:
                self._seq += 1
                heapq.heappush(self._queue, (due, self._seq, direction, packet))
                self._lock.notify()
                return
        self._emit(direction, packet)

    def _emit(self, direction: str, packet: bytes) -> None:
        if direction == "uplink":
            self.inner.send(packet)
        elif (rx := self._receiver) is not None:
            rx(packet)

    def _run(self) -> None:
        while True:
            with self._lock:
                while not self._stop and (not self._queue or self._queue[0][0] > time.monotonic()):
                    wait = self._queue[0][0] - time.monotonic() if self._queue else None
                    self._lock.wait(timeout=wait)
                if self._stop:
                    return
                _, _, direction, packet = heapq.heappop(self._queue)
            self._emit(direction, packet)

    def close(self) -> None:
        with self._lock:
            if self._stop:
                return  # already closed (the USB/IP server closes its data path too)
            self._stop = True
            self._lock.notify()
        self._thread.join(timeout=1)
        self.inner.close()


class PacketQueue:
    """In-memory data path for tests: ``sent`` holds uplink, ``inject`` sends downlink."""

    def __init__(self) -> None:
        self.sent: queue.Queue[bytes] = queue.Queue()
        self._receiver: Receiver | None = None

    def set_receiver(self, receiver: Receiver | None) -> None:
        self._receiver = receiver

    def send(self, packet: bytes) -> None:
        self.sent.put(packet)

    def inject(self, packet: bytes) -> None:
        if self._receiver is not None:
            self._receiver(packet)

    def close(self) -> None:
        pass
