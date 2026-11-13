"""Emulated USB devices: descriptors, endpoint-0 handling and endpoint queues.

A device answers URBs that the USB/IP server hands it. Control transfers on
endpoint 0 complete at once. OUT transfers on other endpoints complete at once.
IN transfers wait in a per-endpoint queue until the device has data for them,
like a real device NAKing until it has something to send; the guest may cancel
them with an unlink.
"""

from __future__ import annotations

import struct
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

# USB/IP and Linux URB status codes.
EPIPE = -32  # endpoint stalled
ECONNRESET = -104  # URB unlinked
ESHUTDOWN = -108  # device gone

SPEED_HIGH = 3

# bmRequestType / bRequest values used below.
DIR_IN = 0x80
REQ_GET_STATUS = 0x00
REQ_CLEAR_FEATURE = 0x01
REQ_SET_FEATURE = 0x03
REQ_SET_ADDRESS = 0x05
REQ_GET_DESCRIPTOR = 0x06
REQ_GET_CONFIGURATION = 0x08
REQ_SET_CONFIGURATION = 0x09
REQ_GET_INTERFACE = 0x0A
REQ_SET_INTERFACE = 0x0B

DT_DEVICE, DT_CONFIG, DT_STRING, DT_INTERFACE, DT_ENDPOINT = 1, 2, 3, 4, 5

EP_BULK, EP_INTERRUPT = 2, 3


class Stall(Exception):
    """Answer the request with a STALL (-EPIPE)."""


@dataclass
class Endpoint:
    address: int  # bit 7 set = IN
    kind: int  # EP_BULK or EP_INTERRUPT
    max_packet: int = 512
    interval: int = 0


@dataclass
class Interface:
    number: int
    cls: int
    subclass: int
    protocol: int
    endpoints: list[Endpoint] = field(default_factory=list)
    name: str = ""
    extra: bytes = b""  # class-specific descriptors placed before the endpoints


@dataclass
class Setup:
    request_type: int
    request: int
    value: int
    index: int
    length: int

    @classmethod
    def parse(cls, raw: bytes) -> Setup:
        return cls(*struct.unpack("<BBHHH", raw))

    @property
    def is_in(self) -> bool:
        return bool(self.request_type & DIR_IN)

    @property
    def kind(self) -> int:
        """0 standard, 1 class, 2 vendor."""
        return (self.request_type >> 5) & 3


Completion = Callable[[int, bytes], None]  # (status, data for IN)


class UsbDevice:
    """Base class. Subclasses set the identity and interfaces and handle class requests."""

    vendor_id = 0
    product_id = 0
    bcd_device = 0x0100
    device_class = 0
    device_subclass = 0
    device_protocol = 0
    manufacturer = ""
    product = ""
    serial = ""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.interfaces: list[Interface] = []
        self.configuration = 0
        self._pending: dict[int, deque[tuple[int, int, Completion]]] = {}
        self._data: dict[int, deque[bytes]] = {}

    # --- descriptors ---------------------------------------------------
    def _strings(self) -> list[str]:
        out = [self.manufacturer, self.product, self.serial]
        out += [i.name for i in self.interfaces if i.name]
        return out

    def _string_index(self, s: str) -> int:
        return self._strings().index(s) + 1 if s else 0

    def device_descriptor(self) -> bytes:
        return struct.pack(
            "<BBHBBBBHHHBBBB", 18, DT_DEVICE, 0x0200, self.device_class, self.device_subclass,
            self.device_protocol, 64, self.vendor_id, self.product_id, self.bcd_device,
            self._string_index(self.manufacturer), self._string_index(self.product),
            self._string_index(self.serial), 1,
        )

    def config_descriptor(self) -> bytes:
        body = b""
        for itf in self.interfaces:
            body += struct.pack("<BBBBBBBBB", 9, DT_INTERFACE, itf.number, 0, len(itf.endpoints),
                                itf.cls, itf.subclass, itf.protocol, self._string_index(itf.name))
            body += itf.extra
            for ep in itf.endpoints:
                body += struct.pack("<BBBBHB", 7, DT_ENDPOINT, ep.address, ep.kind,
                                    ep.max_packet, ep.interval)
        head = struct.pack("<BBHBBBBB", 9, DT_CONFIG, 9 + len(body), len(self.interfaces), 1, 0,
                           0x80, 250)
        return head + body

    def string_descriptor(self, index: int) -> bytes:
        if index == 0:
            return struct.pack("<BBH", 4, DT_STRING, 0x0409)
        strings = self._strings()
        if index > len(strings):
            raise Stall
        raw = strings[index - 1].encode("utf-16-le")
        return bytes([2 + len(raw), DT_STRING]) + raw

    def endpoint(self, address: int) -> Endpoint | None:
        for itf in self.interfaces:
            for ep in itf.endpoints:
                if ep.address == address:
                    return ep
        return None

    # --- endpoint 0 ----------------------------------------------------
    def control(self, setup: Setup, data: bytes) -> bytes:
        """Handle a control transfer. Returns IN data (trimmed to wLength by the caller)."""
        if setup.kind == 0:
            return self._standard(setup)
        return self.class_request(setup, data)

    def _standard(self, s: Setup) -> bytes:
        if s.request == REQ_GET_DESCRIPTOR and s.is_in:
            dtype, index = s.value >> 8, s.value & 0xFF
            if dtype == DT_DEVICE:
                return self.device_descriptor()
            if dtype == DT_CONFIG:
                return self.config_descriptor()
            if dtype == DT_STRING:
                return self.string_descriptor(index)
            raise Stall  # device qualifier, BOS, ...: not a dual-speed or USB 3 device
        if s.request == REQ_GET_STATUS:
            return b"\x00\x00"
        if s.request == REQ_GET_CONFIGURATION:
            return bytes([self.configuration])
        if s.request == REQ_SET_CONFIGURATION:
            self.configuration = s.value & 0xFF
            return b""
        if s.request == REQ_GET_INTERFACE:
            return b"\x00"
        if s.request in (REQ_SET_INTERFACE, REQ_CLEAR_FEATURE, REQ_SET_FEATURE, REQ_SET_ADDRESS):
            return b""
        raise Stall

    def class_request(self, setup: Setup, data: bytes) -> bytes:
        raise Stall

    # --- other endpoints -----------------------------------------------
    def out_transfer(self, ep: int, data: bytes) -> None:
        """Data the host sent on an OUT endpoint."""

    def submit_in(self, ep: int, seqnum: int, length: int, done: Completion) -> None:
        with self.lock:
            self._pending.setdefault(ep, deque()).append((seqnum, length, done))
            self._pump(ep)

    def unlink(self, seqnum: int) -> bool:
        """Cancel a waiting IN transfer. False if it already completed."""
        with self.lock:
            for q in self._pending.values():
                for item in list(q):
                    if item[0] == seqnum:
                        q.remove(item)
                        return True
        return False

    def send_in(self, ep: int, data: bytes) -> None:
        """Queue data for the host on an IN endpoint."""
        with self.lock:
            self._data.setdefault(ep, deque()).append(data)
            self._pump(ep)

    def _pump(self, ep: int) -> None:
        q, d = self._pending.get(ep), self._data.get(ep)
        while q and d:
            _seqnum, length, done = q.popleft()
            chunk = d.popleft()
            if len(chunk) > length:
                d.appendleft(chunk[length:])
                chunk = chunk[:length]
            done(0, chunk)

    def disconnect(self) -> None:
        """Fail every waiting IN transfer (device unplugged or connection closed)."""
        with self.lock:
            for q in self._pending.values():
                while q:
                    _, _, done = q.popleft()
                    done(ESHUTDOWN, b"")
            self._data.clear()
            self.configuration = 0

    def close(self) -> None:
        """The server is shutting down; stop any background work."""

    # --- presence --------------------------------------------------------
    def present(self) -> bool:
        return True

    def generation(self) -> int:
        """Changes whenever the device leaves and comes back (re-enumeration)."""
        return 0
