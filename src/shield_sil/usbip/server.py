"""USB/IP server (the "host" side of Linux usbip) exporting emulated devices.

The guest runs ``usbip attach -r <host> -b <busid>`` with vhci_hcd and sees
the device as if it were plugged into a USB port. Protocol reference:
Documentation/usb/usbip_protocol.rst in the Linux tree. All fields are
big-endian.

A device that leaves the bus (``present()`` false) or re-enumerates
(``generation()`` changes) has its connection closed, which vhci_hcd reports
as a disconnect. The guest re-attaches it when it is listed again.
"""

from __future__ import annotations

import socket
import struct
import threading
from dataclasses import dataclass

from .device import ECONNRESET, EPIPE, SPEED_HIGH, Setup, Stall, UsbDevice

USBIP_VERSION = 0x0111
OP_REQ_DEVLIST, OP_REP_DEVLIST = 0x8005, 0x0005
OP_REQ_IMPORT, OP_REP_IMPORT = 0x8003, 0x0003
CMD_SUBMIT, CMD_UNLINK, RET_SUBMIT, RET_UNLINK = 1, 2, 3, 4
DIR_OUT, DIR_IN = 0, 1
HEADER = struct.Struct(">IIIII")  # command, seqnum, devid, direction, ep
SUBMIT = struct.Struct(">IiiiI8s")  # flags, length, start_frame, npackets, interval, setup
RET = struct.Struct(">iiiii8x")  # status, actual_length, start_frame, npackets, error_count


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("peer closed")
        buf += chunk
    return buf


@dataclass
class Export:
    busid: str
    device: UsbDevice

    @property
    def busnum(self) -> int:
        return int(self.busid.split("-")[0])

    @property
    def devnum(self) -> int:
        return int(self.busid.split("-")[1]) + 1

    def block(self) -> bytes:
        d = self.device
        return struct.pack(
            ">256s32sIIIHHHBBBBBB",
            f"/sys/devices/platform/shield-sil/usb{self.busnum}/{self.busid}".encode(),
            self.busid.encode(), self.busnum, self.devnum, SPEED_HIGH,
            d.vendor_id, d.product_id, d.bcd_device, d.device_class, d.device_subclass,
            d.device_protocol, 1, 1, len(d.interfaces),
        )


class _Session:
    """One imported device on one TCP connection."""

    def __init__(self, export: Export, sock: socket.socket) -> None:
        self.export = export
        self.sock = sock
        self.generation = export.device.generation()
        self.devid = (export.busnum << 16) | export.devnum
        self._wlock = threading.Lock()
        self.closed = threading.Event()

    def _send(self, data: bytes) -> None:
        with self._wlock:
            if self.closed.is_set():
                return
            try:
                self.sock.sendall(data)
                return
            except OSError:
                pass
        # Close outside the write lock: closing fails the device's other waiting
        # transfers, whose completions come back through _send.
        self.close()

    def ret_submit(self, seqnum: int, status: int, data: bytes = b"", out_len: int = 0) -> None:
        actual = len(data) if data else out_len
        self._send(HEADER.pack(RET_SUBMIT, seqnum, 0, 0, 0) + RET.pack(status, actual, 0, 0, 0) + data)

    def close(self) -> None:
        if self.closed.is_set():
            return
        self.closed.set()
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()
        self.export.device.disconnect()

    def run(self) -> None:
        dev = self.export.device
        try:
            while not self.closed.is_set():
                cmd, seqnum, _devid, direction, ep = HEADER.unpack(_recv_exact(self.sock, HEADER.size))
                if cmd == CMD_SUBMIT:
                    _flags, length, _sf, npackets, _iv, setup = SUBMIT.unpack(_recv_exact(self.sock, SUBMIT.size))
                    data = _recv_exact(self.sock, length) if direction == DIR_OUT and length > 0 else b""
                    if npackets not in (0, -1):
                        self.ret_submit(seqnum, EPIPE)  # no isochronous endpoints here
                        continue
                    self._submit(dev, seqnum, direction, ep, length, setup, data)
                elif cmd == CMD_UNLINK:
                    victim = struct.unpack(">I24x", _recv_exact(self.sock, 28))[0]
                    status = ECONNRESET if dev.unlink(victim) else 0
                    self._send(HEADER.pack(RET_UNLINK, seqnum, 0, 0, 0) + struct.pack(">i24x", status))
                else:
                    raise ConnectionError(f"unknown USB/IP command {cmd}")
        except (ConnectionError, OSError):
            pass
        finally:
            self.close()

    def _submit(self, dev: UsbDevice, seqnum: int, direction: int, ep: int, length: int,
                setup_raw: bytes, data: bytes) -> None:
        if ep == 0:
            setup = Setup.parse(setup_raw)
            try:
                with dev.lock:
                    reply = dev.control(setup, data)
            except Stall:
                self.ret_submit(seqnum, EPIPE)
                return
            if direction == DIR_IN:
                self.ret_submit(seqnum, 0, reply[: min(length, setup.length)])
            else:
                self.ret_submit(seqnum, 0, out_len=len(data))
            return
        address = ep | (0x80 if direction == DIR_IN else 0)
        if dev.endpoint(address) is None:
            self.ret_submit(seqnum, EPIPE)
        elif direction == DIR_OUT:
            with dev.lock:
                dev.out_transfer(address, data)
            self.ret_submit(seqnum, 0, out_len=len(data))
        else:
            dev.submit_in(address, seqnum, length,
                          lambda status, d: self.ret_submit(seqnum, status, d if status == 0 else b""))


class UsbipServer:
    """Listens on ``host:port`` and serves the exported devices."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0, poll_s: float = 0.2) -> None:
        self.exports: dict[str, Export] = {}
        self.sessions: dict[str, _Session] = {}
        self.poll_s = poll_s
        self._lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._lsock.bind((host, port))
        self.host, self.port = self._lsock.getsockname()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []

    def export(self, busid: str, device: UsbDevice) -> None:
        self.exports[busid] = Export(busid, device)

    def start(self) -> UsbipServer:
        self._lsock.listen(8)
        self._lsock.settimeout(0.5)
        for target, name in ((self._accept_loop, "usbip-accept"), (self._watch_loop, "usbip-watch")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)
        return self

    def stop(self) -> None:
        self._stop.set()
        self._lsock.close()
        with self._lock:
            for s in list(self.sessions.values()):
                s.close()
        for t in self._threads:
            t.join(timeout=2)
        for e in self.exports.values():
            e.device.close()

    def __enter__(self) -> UsbipServer:  # noqa: PYI034
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # ------------------------------------------------------------------
    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._lsock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            threading.Thread(target=self._handle, args=(conn,), name="usbip-conn", daemon=True).start()

    def _watch_loop(self) -> None:
        """Drop sessions whose device left the bus or re-enumerated."""
        while not self._stop.wait(self.poll_s):
            with self._lock:
                for busid, s in list(self.sessions.items()):
                    dev = s.export.device
                    if s.closed.is_set() or not dev.present() or dev.generation() != s.generation:
                        s.close()
                        del self.sessions[busid]

    def _handle(self, conn: socket.socket) -> None:
        try:
            _version, code, _status = struct.unpack(">HHI", _recv_exact(conn, 8))
            if code == OP_REQ_DEVLIST:
                conn.sendall(self._devlist())
                conn.close()
                return
            if code != OP_REQ_IMPORT:
                conn.close()
                return
            busid = _recv_exact(conn, 32).split(b"\0", 1)[0].decode()
            session = self._import(busid, conn)
            if session is None:
                conn.sendall(struct.pack(">HHI", USBIP_VERSION, OP_REP_IMPORT, 1))
                conn.close()
                return
            conn.sendall(struct.pack(">HHI", USBIP_VERSION, OP_REP_IMPORT, 0) + session.export.block())
            session.run()
        except (ConnectionError, OSError):
            conn.close()

    def _available(self) -> list[Export]:
        return [e for e in self.exports.values() if e.device.present()]

    def _devlist(self) -> bytes:
        avail = [e for e in self._available() if e.busid not in self.sessions]
        out = struct.pack(">HHII", USBIP_VERSION, OP_REP_DEVLIST, 0, len(avail))
        for e in avail:
            out += e.block()
            for itf in e.device.interfaces:
                out += struct.pack(">BBBx", itf.cls, itf.subclass, itf.protocol)
        return out

    def _import(self, busid: str, conn: socket.socket) -> _Session | None:
        with self._lock:
            e = self.exports.get(busid)
            if e is None or not e.device.present():
                return None
            old = self.sessions.get(busid)
            if old is not None and not old.closed.is_set():
                return None  # already attached
            s = _Session(e, conn)
            self.sessions[busid] = s
            return s

