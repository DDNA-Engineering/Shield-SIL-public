"""Host side of the hardware bus between the peripheral models and the guest.

Two virtio-serial ports (on the i.MX 8M Plus machine: UART1 and UART3, see
imx8mp.py) carry newline-delimited records of the form
``<device>\\t<json>``:

* ``org.kform.shield.hw.state`` (host -> guest): one frame every ``period_s``
  and after each guest command. A frame holds a record for every peripheral
  whose reading changed since the last frame (every peripheral on a full
  frame), then a ``bus`` record ``{"seq": N, "t": <board seconds>}``. The guest
  agent ``shield-hwd`` writes each record to ``/var/run/shield/<device>.json``,
  so once ``bus.json`` shows seq N every device file is at least as new as
  frame N.
* ``org.kform.shield.hw.cmd`` (guest -> host): commands from the software
  under test, written with ``shield-hw set <device> '<json>'``.

This is the SIL's hardware abstraction boundary. See docs/hardware-bus.md.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path
from typing import Callable

from .peripherals import Board

STATE_PORT = "org.kform.shield.hw.state"
CMD_PORT = "org.kform.shield.hw.cmd"
BUS_RECORD = "bus"
# Host-side backlog limit. Frames that do not fit are dropped (and the next frame
# is sent in full), so a guest that falls behind never reads minutes-old state.
SEND_BUFFER_BYTES = 16 * 1024


def qemu_args(state_sock: Path, cmd_sock: Path) -> list[str]:
    return [
        "-device", "virtio-serial-pci,id=vser0",
        "-chardev", f"socket,id=hwstate,path={state_sock},server=on,wait=off",
        "-device", f"virtserialport,bus=vser0.0,chardev=hwstate,name={STATE_PORT}",
        "-chardev", f"socket,id=hwcmd,path={cmd_sock},server=on,wait=off",
        "-device", f"virtserialport,bus=vser0.0,chardev=hwcmd,name={CMD_PORT}",
    ]


def _connect(path: Path, timeout: float) -> socket.socket:
    deadline = time.monotonic() + timeout
    while True:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.connect(str(path))
            return s
        except (FileNotFoundError, ConnectionRefusedError):
            s.close()
            if time.monotonic() > deadline:
                raise
            time.sleep(0.1)


class HardwareBus:
    """Runs the physics clock and moves records between the board and the guest."""

    def __init__(
        self,
        board: Board,
        state_sock: Path,
        cmd_sock: Path,
        period_s: float = 0.5,
        time_scale: float = 1.0,
        on_tick: Callable[[Board], None] | None = None,
        full_frame_every: int = 10,
    ) -> None:
        self.board = board
        self.state_sock = state_sock
        self.cmd_sock = cmd_sock
        self.period_s = period_s
        self.time_scale = time_scale
        self.on_tick = on_tick
        self.full_frame_every = max(1, full_frame_every)
        self.seq = 0  # seq of the last frame handed to the transport
        self.dropped_frames = 0
        self.errors: list[str] = []
        self.commands: list[tuple[float, str, dict]] = []
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._send_lock = threading.Lock()
        self._pending = b""
        self._last_sent: dict[str, dict] = {}
        self._force_full = True

    def start(self, connect_timeout: float = 30.0) -> None:
        self.attach_state_socket(_connect(self.state_sock, connect_timeout))
        self._cmd = _connect(self.cmd_sock, connect_timeout)
        self._cmd.settimeout(0.5)
        for target, name in ((self._tick_loop, "hw-tick"), (self._cmd_loop, "hw-cmd")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=5)
        for s in (getattr(self, "_state", None), getattr(self, "_cmd", None)):
            if s:
                s.close()

    def attach_state_socket(self, sock: socket.socket) -> None:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SEND_BUFFER_BYTES)
        sock.setblocking(False)
        self._state = sock

    def publish_now(self) -> bool:
        """Send one frame to the guest. Returns False if it was dropped (guest behind)."""
        with self._send_lock:
            if self._pending and not self._flush():
                self.dropped_frames += 1
                self._force_full = True
                return False
            snap = self.board.snapshot()
            full = self._force_full or (self.seq + 1) % self.full_frame_every == 0
            changed = {d: v for d, v in snap.items() if full or self._last_sent.get(d) != v}
            self.seq += 1
            changed[BUS_RECORD] = {"seq": self.seq, "t": round(self.board.t, 3)}
            self._pending = "".join(
                f"{dev}\t{json.dumps(data, separators=(',', ':'))}\n" for dev, data in changed.items()
            ).encode()
            self._last_sent = snap
            self._force_full = False
            self._flush()
            return True

    def _flush(self) -> bool:
        """Push pending bytes without blocking. True when nothing is left pending."""
        self._drain_state()
        while self._pending:
            try:
                n = self._state.send(self._pending)
            except (BlockingIOError, InterruptedError):
                return False
            except OSError as e:
                self.errors.append(f"state port: {e}")
                self._pending = b""
                self._force_full = True
                return True
            self._pending = self._pending[n:]
        return True

    def _drain_state(self) -> None:
        """Discard anything the guest sent back on the state port. On a UART
        (the i.MX 8M Plus machine) the tty may echo until the agent has set it
        to raw mode; unread, the echo would fill the socket and stall QEMU."""
        while True:
            try:
                if not self._state.recv(4096):
                    return
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                return

    def _tick_loop(self) -> None:
        last = time.monotonic()
        while not self._stop.wait(self.period_s):
            now = time.monotonic()
            self.board.step((now - last) * self.time_scale)
            last = now
            if self.on_tick:
                self.on_tick(self.board)
            self.publish_now()

    def _cmd_loop(self) -> None:
        buf = b""
        while not self._stop.is_set():
            try:
                chunk = self._cmd.recv(4096)
            except socket.timeout:
                continue
            except OSError as e:
                self.errors.append(f"cmd port: {e}")
                return
            if not chunk:
                time.sleep(0.1)
                continue
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                self._handle(line.decode(errors="replace").strip())

    def _handle(self, line: str) -> None:
        if not line:
            return
        try:
            dev, raw = line.split("\t", 1)
            data = json.loads(raw)
            self.board.guest_command(dev, data)
            self.commands.append((self.board.t, dev, data))
        except Exception as e:  # noqa: BLE001 - a bad guest command must never kill the bus
            self.errors.append(f"bad command {line!r}: {e}")
        self.publish_now()
