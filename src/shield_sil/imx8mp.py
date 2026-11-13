"""The i.MX 8M Plus machine: Shield's SoC on the SIL's patched QEMU (qemu/).

QEMU's ``imx8mp-evk`` machine with the SIL patches runs TF-A, U-Boot and the
guest kernel unmodified, and wires the SoC's own I2C and GPIO controllers to
the board models here, so the guest uses Shield's real drivers:

* ``/dev/i2c-1`` (I2C2) is the i2c-imx controller; each modelled device on it
  is a ``sil-i2c-bridge`` whose bus events :class:`I2cBridge` answers from
  ``Board.i2c``.
* Front-panel LEDs (gpio-leds), the button (gpio-keys) and the named GPIO lines
  are SoC pins, carried by one ``sil-gpio-bridge`` to :class:`GpioBridge`.

The pins are Shield's, from its device tree, set in
``image/dts/shield-sil.dts`` (the tables here must match it; a unit test
checks that).

Everything else follows the SoC: the eMMC is on uSDHC3 (``mmcblk2``), LAN is
the FEC, the console is UART2, the watchdog is WDOG1. Shield's second GbE port
(the EQoS, which QEMU does not model) is an e1000e on PCIe, behind a PCI bridge
together with the SIL's management NIC. The hardware bus runs on UART1 (state)
and UART3 (commands).
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .peripherals import Board
from .peripherals.charger import I2cError

MACHINE = "imx8mp-evk"
EMMC_SLOT = 3  # uSDHC3, /dev/mmcblk2
TFA_ADDR = 0x970000
UBOOT_ADDR = 0x40200000
NIC_MODEL = "e1000e"
PCI_BRIDGE = "silpcib"

# Pins (GPIO controller, pin), as in image/dts/shield-sil.dts: Shield's own.
LED_PINS: dict[str, tuple[int, int]] = {
    "Heartbeat": (1, 12), "System": (5, 3), "User": (4, 30), "WAN": (5, 0),
    "WiFi": (4, 31), "ETH": (1, 1), "Battery": (4, 22),
}
LED_ACTIVE_LOW = {"Heartbeat", "System"}
BUTTON_PIN = (5, 4)  # BTN_0, active low
LINE_PINS: dict[str, tuple[int, int]] = {
    "shield:cell:sim-select": (3, 27), "shield:cell:full-card-power": (4, 12),
    "shield:wifi:wake": (5, 10), "shield:wifi:enabled": (5, 12),
    "shield:pgood:5v-out-efuse": (5, 1), "shield:pgood:dc-efuse": (3, 26),
    "shield:pgood:batt-efuse": (4, 25), "shield:pgood:3v3-out-efuse": (5, 11),
}
BUTTON = "BTN_0"
RELEASED = 0xFF  # 'O' level: the guest no longer drives the pin


def is_imx8mp(image: dict) -> bool:
    return str(image.get("machine", "")).split(",")[0] == MACHINE


@dataclass(frozen=True)
class Pin:
    line: int  # bridge line number
    name: str
    kind: str  # "led" | "button" | "gpio"
    ctrl: int
    pin: int


def pins() -> list[Pin]:
    """The bridge's lines, in order."""
    out: list[Pin] = []
    for name, (c, p) in LED_PINS.items():
        out.append(Pin(len(out), name, "led", c, p))
    out.append(Pin(len(out), BUTTON, "button", *BUTTON_PIN))
    for name, (c, p) in LINE_PINS.items():
        out.append(Pin(len(out), name, "gpio", c, p))
    return out


def gpio_map() -> str:
    return ";".join(f"{p.ctrl}:{p.pin}" for p in pins())


def qemu_machine_args(image: dict, tfa: Path, firmware: Path, disk: Path) -> list[str]:
    """Machine, boot chain and eMMC. QEMU starts TF-A at EL3, as the boot ROM
    and SPL would hand over; TF-A runs U-Boot, U-Boot boots the eMMC."""
    boot_mib = int(image.get("emmc_boot_mib", 4))
    return [
        "-M", f"{MACHINE},emmc-slot={EMMC_SLOT},emmc-boot-partition-size={boot_mib}M",
        "-device", f"loader,file={tfa},addr={TFA_ADDR:#x}",
        "-device", f"loader,file={firmware},addr={UBOOT_ADDR:#x}",
        "-device", f"loader,addr={TFA_ADDR:#x},cpu-num=0",
        "-drive", f"file={disk},if=sd,bus={EMMC_SLOT - 1},format=qcow2,id=emmc",
        # WDOG1 resets the board when the guest stops feeding it.
        "-action", "watchdog=reset",
    ]


def qemu_bridge_args(board: Board, i2c_sock: Path, gpio_sock: Path) -> list[str]:
    """One sil-i2c-bridge per modelled I2C device, and the GPIO bridge."""
    args: list[str] = []
    n = 0
    for bus, b in sorted(board.i2c.items()):
        for addr in sorted(b.devices):
            cid = f"sili2c{n}"
            args += ["-chardev", f"socket,id={cid},path={i2c_sock}",
                     "-device", f"sil-i2c-bridge,bus=i2c-bus.{bus},address={addr:#x},chardev={cid}"]
            n += 1
    args += ["-chardev", f"socket,id=silgpio,path={gpio_sock}",
             "-device", f"sil-gpio-bridge,chardev=silgpio,map={gpio_map()}"]
    return args


def qemu_pci_bridge_args() -> list[str]:
    """QEMU's DesignWare root port has one slot: NICs go behind a PCI bridge."""
    return ["-device", f"pci-bridge,id={PCI_BRIDGE},chassis_nr=1,bus=dw-pcie,addr=0"]


def qemu_hwbus_serial_args(state_sock: Path, cmd_sock: Path, console_sock: Path) -> list[str]:
    """UART1 = hardware state (host -> guest), UART2 = console, UART3 = commands.

    The hardware bus sockets are servers QEMU creates, as with virtio-serial;
    hwbus.HardwareBus connects to them.
    """
    return [
        "-chardev", f"socket,id=hwstate,path={state_sock},server=on,wait=off",
        "-chardev", f"socket,id=hwcmd,path={cmd_sock},server=on,wait=off",
        "-serial", "chardev:hwstate",
        "-serial", f"unix:{console_sock},server=on,wait=off",
        "-serial", "chardev:hwcmd",
    ]


class _Server:
    """A UNIX socket server that QEMU's bridge devices connect to."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.unlink(missing_ok=True)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(path))
        self.sock.listen(16)
        self._stop = threading.Event()
        self._conns: list[socket.socket] = []
        self._lock = threading.Lock()
        threading.Thread(target=self._accept, name=f"bridge-{path.name}", daemon=True).start()

    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            with self._lock:
                self._conns.append(c)
            threading.Thread(target=self._serve_safe, args=(c,), name=f"bridge-{self.path.name}",
                             daemon=True).start()

    def _serve_safe(self, c: socket.socket) -> None:
        try:
            self.serve(c)
        except OSError:
            pass
        finally:
            with self._lock:
                if c in self._conns:
                    self._conns.remove(c)
            c.close()

    def serve(self, c: socket.socket) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def close(self) -> None:
        self._stop.set()
        try:
            self.sock.close()
        finally:
            with self._lock:
                conns, self._conns = self._conns, []
            for c in conns:
                try:
                    c.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                c.close()
            self.path.unlink(missing_ok=True)


def _recv_exact(c: socket.socket, n: int) -> bytes | None:
    buf = b""
    while len(buf) < n:
        chunk = c.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


class I2cBridge(_Server):
    """Answers QEMU's sil-i2c-bridge bus events from the board's I2C models.

    QEMU reports each event of a transfer as it happens (start, every byte,
    stop), while the models take whole writes and reads. Bytes written are
    collected until a repeated start or the stop and then written as one
    message; a read is taken from the model when the master clocks its first
    byte and handed out byte by byte. The models' reads depend only on the
    register pointer (a shorter read is a prefix of a longer one), so reading
    ahead does not change what the guest gets; the bus log records the bytes
    the guest actually read.
    """

    READ_AHEAD = 256

    def __init__(self, board: Board, path: Path) -> None:
        self.board = board
        super().__init__(path)

    def serve(self, c: socket.socket) -> None:
        bus_no: int | None = None
        addr = 0
        wbuf = bytearray()
        rbuf = b""
        rpos = 0
        reading = False

        def bus():
            return self.board.i2c.get(bus_no) if bus_no is not None else None

        def flush() -> None:
            nonlocal wbuf, rbuf, rpos, reading
            b = bus()
            with self.board.lock:
                if b is not None and not reading and wbuf:
                    try:
                        b.write(addr, bytes(wbuf))
                    except I2cError:
                        pass
                if b is not None and reading and rpos:
                    b.record_read(addr, rbuf[:rpos])
            wbuf = bytearray()
            rbuf, rpos, reading = b"", 0, False

        while True:
            m = _recv_exact(c, 2)
            if m is None:
                return
            op, arg = chr(m[0]), m[1]
            reply = 0
            if op in "SR":
                flush()
                addr = arg
                bus_no = self._bus_of(addr)
                reading = op == "R"
                reply = 0 if self._ack(bus_no, addr) else 1
            elif op == "W":
                wbuf.append(arg)
            elif op == "B":
                if not rbuf:
                    b = bus()
                    try:
                        with self.board.lock:
                            rbuf = b.read_ahead(addr, self.READ_AHEAD) if b else b""
                    except I2cError:
                        rbuf = b""
                reply = rbuf[rpos] if rpos < len(rbuf) else 0xFF
                rpos += 1
            elif op == "P":
                flush()
            # 'N': the master NACKed the last byte; the stop follows.
            c.sendall(bytes([reply]))

    def _bus_of(self, addr: int) -> int | None:
        # Addresses are unique across the modelled buses (one device each today).
        for n, b in self.board.i2c.items():
            if addr in b.devices:
                return n
        return None

    def _ack(self, bus_no: int | None, addr: int) -> bool:
        if bus_no is None:
            return False
        with self.board.lock:
            return self.board.i2c[bus_no].ack(addr)


class GpioBridge(_Server):
    """Connects the board models to the SoC pins of QEMU's sil-gpio-bridge.

    LEDs and output lines follow what the guest drives; the button and the
    input lines are driven from here. A pin the guest releases (reset, or
    switched to input) goes back to its line's default level.
    """

    def __init__(self, board: Board, path: Path, now: Callable[[], float]) -> None:
        self.board = board
        self.now = now
        self.pins = pins()
        self.by_line = {p.line: p for p in self.pins}
        self.by_name = {p.name: p for p in self.pins}
        self._conn: socket.socket | None = None
        self._send_lock = threading.Lock()
        self._driven: dict[int, int] = {}
        self._button_timer: threading.Timer | None = None
        self.button_pressed = False
        super().__init__(path)

    def serve(self, c: socket.socket) -> None:
        self._conn = c
        self._driven = {}
        self.sync_inputs(force=True)
        while True:
            m = _recv_exact(c, 3)
            if m is None:
                if self._conn is c:
                    self._conn = None
                return
            if chr(m[0]) == "O" and m[1] in self.by_line:
                self._guest_drove(self.by_line[m[1]], m[2])

    def _guest_drove(self, p: Pin, level: int) -> None:
        with self.board.lock:
            t = self.now()
            if p.kind == "led":
                on = level != RELEASED and (level == 0) == (p.name in LED_ACTIVE_LOW)
                self.board.leds.set_brightness(p.name, 255 if on else 0, "", t)
            elif p.kind == "gpio":
                direction, default = self.board.gpio.lines[p.name]
                if direction == "out":
                    self.board.gpio.set_output(p.name, default if level == RELEASED else level, t)

    def _send(self, line: int, level: int) -> None:
        c = self._conn
        if c is None:
            return
        with self._send_lock:
            try:
                c.sendall(bytes([ord("I"), line, 1 if level else 0]))
            except OSError:
                pass

    def sync_inputs(self, force: bool = False) -> None:
        """Drive the input lines (and the button) from the models; call on every tick.

        Output lines get their default level as a pull, so a line the guest
        does not drive (yet) reads like the board's resistor holds it."""
        with self.board.lock:
            inputs = self.board.gpio.inputs()
            levels = {}
            for name, (direction, default) in self.board.gpio.lines.items():
                if name in self.by_name:
                    levels[self.by_name[name].line] = inputs[name] if direction == "in" else default
        levels[self.by_name[BUTTON].line] = 0 if self.button_pressed else 1  # active low
        for line, v in levels.items():
            if force or self._driven.get(line) != v:
                self._driven[line] = v
                self._send(line, v)

    def press(self, seconds: float) -> None:
        """Press BTN_0 now and release it after ``seconds`` (non-blocking)."""
        if self._button_timer:
            self._button_timer.cancel()
        self.button_pressed = True
        self.sync_inputs()
        self._button_timer = threading.Timer(seconds, self._release)
        self._button_timer.daemon = True
        self._button_timer.start()

    def _release(self) -> None:
        self.button_pressed = False
        self.sync_inputs()

    def close(self) -> None:
        if self._button_timer:
            self._button_timer.cancel()
        super().close()
