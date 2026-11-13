"""The EM7595 as a USB device, backed by the board's Modem model.

Normal mode (``1199:c081``), Sierra Wireless composition as the upstream
drivers expect it (qcserial QCSERIAL_SWI layout, qmi_wwan
``QMI_QUIRK_SET_DTR(0x1199, 0xc081, 8)``):

* interface 0: DM/DIAG  -> ttyUSB0 (silent here)
* interface 2: NMEA     -> ttyUSB1 (silent here)
* interface 3: AT       -> ttyUSB2, answered by ``Modem.at``
* interface 8: QMI/net  -> cdc-wdm0 + wwan0, answered by ``QmiFirmware``

Fastboot mode (``1199:c082``): one vendor interface (ff/42/03) and nothing
else, so no driver binds, as on a unit stuck after an interrupted update.

The device is on the bus whenever ``Modem.usb_id`` is set; every completed
boot is a new enumeration, which closes the USB/IP connection so the guest
sees an unplug and re-attaches.
"""

from __future__ import annotations

import re
import threading

from ..peripherals import Board
from ..peripherals.modem import ModemGone, ModemTimeout
from .datapath import PacketQueue, TunDataPath
from .device import EP_BULK, EP_INTERRUPT, Endpoint, Interface, Setup, Stall, UsbDevice
from .qmi import QmiFirmware

EP_AT_IN, EP_AT_OUT = 0x84, 0x03
EP_QMI_NOTIFY, EP_QMI_IN, EP_QMI_OUT = 0x87, 0x86, 0x04
QMI_INTERFACE = 8

CDC_SEND_ENCAPSULATED_COMMAND = 0x00
CDC_GET_ENCAPSULATED_RESPONSE = 0x01
CDC_SET_LINE_CODING = 0x20
CDC_SET_CONTROL_LINE_STATE = 0x22
RESPONSE_AVAILABLE = bytes([0xA1, 0x01, 0, 0, QMI_INTERFACE, 0, 0, 0])

_NORMAL = [
    Interface(0, 0xFF, 0xFF, 0xFF, [Endpoint(0x81, EP_BULK), Endpoint(0x01, EP_BULK)]),
    Interface(2, 0xFF, 0xFF, 0xFF, [Endpoint(0x83, EP_INTERRUPT, 10, 9),
                                    Endpoint(0x82, EP_BULK), Endpoint(0x02, EP_BULK)]),
    Interface(3, 0xFF, 0xFF, 0xFF, [Endpoint(0x85, EP_INTERRUPT, 10, 9),
                                    Endpoint(EP_AT_IN, EP_BULK), Endpoint(EP_AT_OUT, EP_BULK)]),
    Interface(QMI_INTERFACE, 0xFF, 0xFF, 0xFF, [Endpoint(EP_QMI_NOTIFY, EP_INTERRUPT, 8, 9),
                                                Endpoint(EP_QMI_IN, EP_BULK),
                                                Endpoint(EP_QMI_OUT, EP_BULK)]),
]
_FASTBOOT = [Interface(0, 0xFF, 0x42, 0x03, [Endpoint(0x81, EP_BULK), Endpoint(0x01, EP_BULK)],
                       name="fastboot")]

_ECHO = re.compile(r"^AT(?:[ZQV][01]?|&F0?)*E([01])")


class Em7595Usb(UsbDevice):
    vendor_id = 0x1199
    bcd_device = 0x0006
    manufacturer = "Sierra Wireless, Incorporated"

    def __init__(self, board: Board, data: TunDataPath | PacketQueue | None = None,
                 indication_period_s: float = 0.5) -> None:
        super().__init__()
        self.board = board
        self.data = data
        self.data_in_packets = 0
        self.data_dropped = 0
        if data is not None:
            data.set_receiver(self._downlink)
        self.serial = "SIL" + board.modem.imei
        self.qmi = self._new_firmware()
        self._responses: list[bytes] = []
        self._at_buf = b""
        self._echo = True
        self.at_log: list[tuple[str, str]] = []
        self.data_out_packets = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._indications, args=(indication_period_s,),
                                        name="em7595-ind", daemon=True)
        self._thread.start()

    # --- identity follows the model ---------------------------------------
    @property
    def fastboot(self) -> bool:
        return self.board.modem.fastboot

    @property
    def product_id(self) -> int:  # type: ignore[override]
        return 0xC082 if self.fastboot else 0xC081

    @property
    def product(self) -> str:  # type: ignore[override]
        return "Android" if self.fastboot else "Sierra Wireless EM7595"

    @property
    def interfaces(self) -> list[Interface]:  # type: ignore[override]
        return _FASTBOOT if self.fastboot else _NORMAL

    @interfaces.setter
    def interfaces(self, _value) -> None:
        pass  # fixed by the mode

    def present(self) -> bool:
        with self.board.lock:
            return self.board.modem.usb_id is not None

    def generation(self) -> int:
        with self.board.lock:
            m = self.board.modem
            return m.enumerations * 2 + (1 if m.fastboot else 0)

    def _new_firmware(self) -> QmiFirmware:
        return QmiFirmware(lambda: self.board.modem, self.board.lock)

    def disconnect(self) -> None:
        super().disconnect()
        with self.lock:
            self._responses.clear()
            self._at_buf = b""
            self._echo = True
            self.qmi = self._new_firmware()  # QMI clients do not survive an unplug

    def close(self) -> None:
        self._stop.set()
        if self.data is not None:
            self.data.set_receiver(None)
            self.data.close()

    # --- endpoint 0 class requests -----------------------------------------
    def class_request(self, setup: Setup, data: bytes) -> bytes:
        if self.fastboot or setup.kind != 1:
            raise Stall
        if setup.request in (CDC_SET_CONTROL_LINE_STATE, CDC_SET_LINE_CODING) and not setup.is_in:
            return b""
        if (setup.index & 0xFF) != QMI_INTERFACE:
            raise Stall
        if setup.request == CDC_SEND_ENCAPSULATED_COMMAND and not setup.is_in:
            if self._wedged():
                return b""  # accepted by the USB core, never answered
            for frame in self.qmi.handle(data):
                self._queue_response(frame)
            return b""
        if setup.request == CDC_GET_ENCAPSULATED_RESPONSE and setup.is_in:
            return self._responses.pop(0) if self._responses else b""
        raise Stall

    def _queue_response(self, frame: bytes) -> None:
        with self.lock:
            self._responses.append(frame)
            self.send_in(EP_QMI_NOTIFY, RESPONSE_AVAILABLE)

    def _wedged(self) -> bool:
        with self.board.lock:
            return self.board.modem.wedged

    # --- bulk endpoints ----------------------------------------------------
    def out_transfer(self, ep: int, data: bytes) -> None:
        if self.fastboot:
            return
        if ep == EP_AT_OUT:
            self._at_bytes(data)
        elif ep == EP_QMI_OUT:
            self._uplink(data)

    def _at_bytes(self, data: bytes) -> None:
        self._at_buf += data
        while b"\r" in self._at_buf:
            raw, self._at_buf = self._at_buf.split(b"\r", 1)
            line = raw.decode(errors="replace").strip("\n ")
            if not line:
                continue
            with self.board.lock:
                try:
                    reply = self.board.modem.at(line)
                except (ModemTimeout, ModemGone):
                    continue  # a wedged or departing module stays silent
            self.at_log.append((line, reply))
            out = (line + "\r") if self._echo else ""
            m = _ECHO.match(line.upper())
            if m and reply == "OK":
                self._echo = m.group(1) == "1"
            self.send_in(EP_AT_IN, (out + "\r\n" + reply + "\r\n").encode())

    # --- wwan0 data (raw IP) -------------------------------------------------
    def _carrying(self) -> bool:
        """Packets flow only in raw-IP mode, with a call up, while the modem has data."""
        with self.board.lock:
            return (self.qmi.link_layer == 2 and bool(self.qmi.calls)
                    and self.board.modem.data_up and not self.board.modem.wedged)

    def _uplink(self, packet: bytes) -> None:
        self.data_out_packets += 1
        if self.data is not None and packet and self._carrying():
            self.data.send(packet)
        else:
            self.data_dropped += 1

    def _downlink(self, packet: bytes) -> None:
        if self.configuration and not self.fastboot and self._carrying():
            self.data_in_packets += 1
            self.send_in(EP_QMI_IN, packet)
        else:
            self.data_dropped += 1

    # --- indications ---------------------------------------------------------
    def _indications(self, period: float) -> None:
        while not self._stop.wait(period):
            if not self.configuration or self.fastboot:
                continue
            for frame in self.qmi.poll_indications():
                self._queue_response(frame)
