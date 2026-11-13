"""The laptop end of Shield's USB-C cable: a USB host for the guest's network gadget.

With ``network.usbc = "gadget"`` the guest runs Shield's real USB device side:
the Linux gadget stack with the CDC Ethernet (ECM) function, which creates
``usb0``, on the USB/IP virtual device controller (``usbip-vudc``), exported by
``usbipd --device``. This module is the other end of that cable. It imports the
gadget over USB/IP the way ``vhci_hcd`` would, enumerates it (descriptors,
SET_CONFIGURATION, data interface alt setting 1), and then relays Ethernet
frames between the gadget's bulk endpoints and the simulated laptop's tap
(through the same UDP framing QEMU's ``-netdev dgram`` uses, so the laptop
side is an ordinary :class:`shield_sil.wire.Wire`).

Unplugging the cable closes the USB/IP connection; the guest's gadget sees a
disconnect and ``usb0`` loses carrier, as on Shield. Plugging it back in
reconnects and enumerates again.

Protocol reference: Documentation/usb/usbip_protocol.rst; CDC ECM 1.2.
"""

from __future__ import annotations

import socket
import struct
import threading
from dataclasses import dataclass, field

from .server import (CMD_SUBMIT, DIR_IN, DIR_OUT, HEADER, OP_REP_IMPORT, OP_REQ_IMPORT, RET,
                     RET_SUBMIT, RET_UNLINK, SUBMIT, USBIP_VERSION, _recv_exact)

VUDC_BUSID = "usbip-vudc.0"
DEVICE_BLOCK = 312  # struct usbip_usb_device
_RX_URBS = 4  # bulk IN transfers kept queued, as usbnet does
_RX_LEN = 2048  # larger than any Ethernet frame (1514 + VLAN)
CDC_DATA_CLASS = 0x0A


class UsbipError(ConnectionError):
    pass


@dataclass
class EcmEndpoints:
    config: int
    data_interface: int
    data_alt: int
    bulk_in: int
    bulk_out: int
    max_packet_out: int


def parse_ecm_config(desc: bytes) -> EcmEndpoints:
    """Find the CDC data interface's active alt setting and its bulk endpoints."""
    config = desc[5]
    itf = alt = None
    found: dict[int, tuple[int, int, int, dict]] = {}
    i = 0
    while i + 2 <= len(desc):
        length, kind = desc[i], desc[i + 1]
        if length == 0:
            break
        if kind == 4:  # interface
            itf, alt, cls = desc[i + 2], desc[i + 3], desc[i + 5]
            if cls == CDC_DATA_CLASS:
                found[(itf << 8) | alt] = (itf, alt, cls, {})
            else:
                itf = None
        elif kind == 5 and itf is not None:  # endpoint of a CDC data interface
            address, attrs = desc[i + 2], desc[i + 3]
            mps = struct.unpack_from("<H", desc, i + 4)[0] & 0x7FF
            if attrs & 0x03 == 2:  # bulk
                found[(itf << 8) | alt][3]["in" if address & 0x80 else "out"] = (address & 0x0F, mps)
        i += length
    for itf_, alt_, _cls, eps in found.values():
        if "in" in eps and "out" in eps:
            return EcmEndpoints(config, itf_, alt_, eps["in"][0], eps["out"][0], eps["out"][1])
    raise UsbipError("gadget has no CDC data interface with bulk endpoints")


@dataclass
class _Pending:
    direction: int
    callback: object = None  # callable(status, data) for asynchronous transfers
    done: threading.Event = field(default_factory=threading.Event)
    status: int = 0
    data: bytes = b""


class UsbipClient:
    """Minimal USB/IP importer: one device, control and bulk transfers."""

    def __init__(self, host: str, port: int, busid: str = VUDC_BUSID, timeout: float = 5.0) -> None:
        self.sock = socket.create_connection((host, port), timeout=timeout)
        try:
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.sock.sendall(struct.pack(">HHI32s", USBIP_VERSION, OP_REQ_IMPORT, 0, busid.encode()))
            _ver, code, status = struct.unpack(">HHI", _recv_exact(self.sock, 8))
            if code != OP_REP_IMPORT or status != 0:
                raise UsbipError(f"import of {busid} refused (status {status})")
            block = _recv_exact(self.sock, DEVICE_BLOCK)
        except BaseException:
            self.sock.close()
            raise
        busnum, devnum = struct.unpack_from(">II", block, 288)
        self.devid = (busnum << 16) | devnum
        self.sock.settimeout(None)
        self._seq = 0
        self._lock = threading.Lock()  # seqnum, pending table and socket writes
        self._pending: dict[int, _Pending] = {}
        self.closed = threading.Event()
        self._reader = threading.Thread(target=self._read_loop, name="usbip-client", daemon=True)
        self._reader.start()

    def close(self) -> None:
        with self._lock:
            if self.closed.is_set():
                return
            self.closed.set()
            pending, self._pending = self._pending, {}
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()
        for p in pending.values():
            p.status = -1
            p.done.set()

    # -- transfers --------------------------------------------------------
    def submit(self, ep: int, direction: int, length: int, setup: bytes = bytes(8), data: bytes = b"",
               callback=None) -> _Pending:
        """Queue one transfer. ``callback(status, data)`` runs on completion if given."""
        p = _Pending(direction, callback)
        with self._lock:
            if self.closed.is_set():
                raise UsbipError("disconnected")
            self._seq += 1
            seq = self._seq
            self._pending[seq] = p
            msg = HEADER.pack(CMD_SUBMIT, seq, self.devid, direction, ep)
            msg += SUBMIT.pack(0, length, 0, 0, 0, setup)  # no flags, not isochronous
            if direction == DIR_OUT:
                msg += data
            try:
                self.sock.sendall(msg)
            except OSError as e:
                self._pending.pop(seq, None)
                err = e
            else:
                return p
        self.close()
        raise UsbipError(str(err))

    def control(self, request_type: int, request: int, value: int, index: int, length: int = 0,
                data: bytes = b"", timeout: float = 5.0) -> bytes:
        direction = DIR_IN if request_type & 0x80 else DIR_OUT
        n = length if direction == DIR_IN else len(data)
        setup = struct.pack("<BBHHH", request_type, request, value, index, n)
        p = self.submit(0, direction, n, setup, data)
        if not p.done.wait(timeout):
            raise UsbipError(f"control request {request:#x} timed out")
        if p.status != 0:
            raise UsbipError(f"control request {request:#x} failed ({p.status})")
        return p.data

    def _read_loop(self) -> None:
        try:
            while not self.closed.is_set():
                cmd, seq, _devid, _dir, _ep = HEADER.unpack(_recv_exact(self.sock, HEADER.size))
                if cmd == RET_SUBMIT:
                    status, actual, _sf, _np, _err = RET.unpack(_recv_exact(self.sock, RET.size))
                    with self._lock:
                        p = self._pending.pop(seq, None)
                    # IN data follows the reply; the reply's own direction field is 0,
                    # so the direction comes from our record of the request.
                    data = _recv_exact(self.sock, actual) if p and p.direction == DIR_IN and actual > 0 else b""
                    if p is None:
                        continue
                    p.status, p.data = status, data
                    p.done.set()
                    if p.callback is not None:
                        p.callback(status, data)
                elif cmd == RET_UNLINK:
                    _recv_exact(self.sock, 28)
                else:
                    raise UsbipError(f"unexpected USB/IP reply {cmd}")
        except (ConnectionError, OSError):
            pass
        finally:
            self.close()


class EcmLaptop:
    """USB host for the guest's ECM gadget, relaying frames to a Wire over UDP.

    ``guest`` is where the guest's ``usbipd --device`` is reachable from the host
    (a QEMU user-net host forward); ``wire_port`` is the laptop-side Wire's UDP
    port and ``local_port`` the port this end sends from (the Wire's peer).
    """

    def __init__(self, guest: tuple[str, int], wire_port: int, local_port: int,
                 retry_s: float = 1.0, busid: str = VUDC_BUSID) -> None:
        self.guest = guest
        self.busid = busid
        self.wire_port = wire_port
        self.retry_s = retry_s
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.bind(("127.0.0.1", local_port))
        self.udp.settimeout(0.5)
        self.plugged = True
        self.connected = threading.Event()
        self.client: UsbipClient | None = None
        self.eps: EcmEndpoints | None = None
        self.frames_in = self.frames_out = 0
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []

    def start(self) -> "EcmLaptop":
        for target, name in ((self._connect_loop, "ecm-connect"), (self._tx_loop, "ecm-tx")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)
        return self

    def stop(self) -> None:
        self._stop.set()
        self._drop()
        for t in self._threads:
            t.join(timeout=2)
        self.udp.close()

    def set_plugged(self, plugged: bool) -> None:
        """Plug or pull the USB-C cable."""
        self.plugged = plugged
        if not plugged:
            self._drop()

    # ------------------------------------------------------------------
    def _drop(self) -> None:
        with self._lock:
            c, self.client = self.client, None
            self.connected.clear()
        if c is not None:
            c.close()

    def _connect_loop(self) -> None:
        while not self._stop.is_set():
            c = self.client
            if c is not None and not c.closed.is_set():
                self._stop.wait(0.2)
                continue
            if c is not None:
                self._drop()
            if not self.plugged:
                self._stop.wait(0.2)
                continue
            try:
                self._enumerate()
            except (UsbipError, OSError):
                self._drop()
                self._stop.wait(self.retry_s)

    def _enumerate(self) -> None:
        c = UsbipClient(*self.guest, busid=self.busid)
        with self._lock:
            self.client = c
        dev = c.control(0x80, 6, 0x0100, 0, 18)  # GET_DESCRIPTOR(device)
        if len(dev) < 18:
            raise UsbipError("short device descriptor")
        head = c.control(0x80, 6, 0x0200, 0, 9)  # GET_DESCRIPTOR(config), header first
        total = struct.unpack_from("<H", head, 2)[0]
        eps = parse_ecm_config(c.control(0x80, 6, 0x0200, 0, total))
        c.control(0x00, 9, eps.config, 0)  # SET_CONFIGURATION
        c.control(0x01, 11, eps.data_alt, eps.data_interface)  # SET_INTERFACE: data on
        self.eps = eps
        for _ in range(_RX_URBS):
            self._queue_rx(c)
        self.connected.set()

    def _queue_rx(self, c: UsbipClient) -> None:
        def done(status: int, data: bytes) -> None:
            if status != 0 or c.closed.is_set():
                if status != 0 and not c.closed.is_set():
                    c.close()  # endpoint stalled or gadget gone: re-enumerate
                return
            if data:
                self.frames_in += 1
                try:
                    self.udp.sendto(data, ("127.0.0.1", self.wire_port))
                except OSError:
                    pass
            try:
                self._queue_rx(c)
            except UsbipError:
                pass
        c.submit(self.eps.bulk_in, DIR_IN, _RX_LEN, callback=done)

    def _tx_loop(self) -> None:
        while not self._stop.is_set():
            try:
                frame = self.udp.recv(65536)
            except TimeoutError:
                continue
            except OSError:
                return
            c, eps = self.client, self.eps
            if c is None or eps is None or not self.connected.is_set():
                continue  # cable out or not enumerated: the frame is lost, as on a wire
            # ECM ends a frame with a short packet. Like Linux usbnet without
            # ZLP support, pad a frame that fills whole packets by one byte
            # (IP ignores bytes past its own length).
            if len(frame) % eps.max_packet_out == 0:
                frame += b"\0"
            try:
                c.submit(eps.bulk_out, DIR_OUT, len(frame), data=frame, callback=lambda s, d: None)
                self.frames_out += 1
            except UsbipError:
                pass
