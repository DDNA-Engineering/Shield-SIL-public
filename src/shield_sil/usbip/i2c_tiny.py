"""i2c-tiny-usb adapter (upstream Linux driver ``i2c-tiny-usb``, 0403:c631).

The guest's battery tooling opens ``/dev/i2c-1`` and talks to the TPS25751.
In the SIL that adapter is this device: every I2C message the guest sends is
forwarded to ``transfer(addr, write, read_len)``, normally
``Board.i2c_transfer`` on the matching board bus.

Protocol (drivers/i2c/busses/i2c-tiny-usb.c): vendor control requests on
endpoint 0. ``CMD_I2C_IO`` (+BEGIN/END flags) carries one message, address in
wIndex, I2C flags in wValue; IN for reads, OUT for writes. After each message
the driver reads ``CMD_GET_STATUS``; ``STATUS_ADDRESS_NAK`` fails the transfer
with -ENXIO, as a missing or silent device would on the real bus.
"""

from __future__ import annotations

import struct
from collections.abc import Callable

from .device import Interface, Setup, Stall, UsbDevice

CMD_ECHO, CMD_GET_FUNC, CMD_SET_DELAY, CMD_GET_STATUS, CMD_I2C_IO = 0, 1, 2, 3, 4
STATUS_IDLE, STATUS_ADDRESS_ACK, STATUS_ADDRESS_NAK = 0, 1, 2
I2C_M_RD = 0x0001
# I2C_FUNC_I2C | I2C_FUNC_SMBUS_EMUL, what the adapter firmware reports.
FUNCTIONALITY = 0x00000001 | 0x0EFF0008

Transfer = Callable[[int, bytes, int], bytes]


class I2cTinyUsb(UsbDevice):
    vendor_id = 0x0403
    product_id = 0xC631
    bcd_device = 0x0104
    device_class = 0xFF
    manufacturer = "Shield SIL"
    product = "i2c-tiny-usb"

    def __init__(self, transfer: Transfer | None, serial: str) -> None:
        super().__init__()
        self.serial = serial
        self.transfer = transfer  # None: a bus with nothing on it
        self.interfaces = [Interface(0, 0xFF, 0, 0)]
        self.status = STATUS_IDLE
        self.delay_us = 0
        self.messages = 0

    def class_request(self, setup: Setup, data: bytes) -> bytes:
        if setup.kind != 2:  # vendor
            raise Stall
        cmd = setup.request
        if cmd == CMD_ECHO:
            return struct.pack("<H", setup.value)
        if cmd == CMD_GET_FUNC:
            return struct.pack("<I", FUNCTIONALITY)
        if cmd == CMD_SET_DELAY:
            self.delay_us = setup.value
            return b""
        if cmd == CMD_GET_STATUS:
            return bytes([self.status])
        if cmd & ~3 == CMD_I2C_IO:
            return self._io(setup, data)
        raise Stall

    def _io(self, setup: Setup, data: bytes) -> bytes:
        addr = setup.index & 0x7F
        read = bool(setup.value & I2C_M_RD)
        self.messages += 1
        if self.transfer is None:
            self.status = STATUS_ADDRESS_NAK
            return bytes(setup.length) if read else b""
        try:
            if read:
                out = self.transfer(addr, b"", setup.length)
            else:
                self.transfer(addr, data, 0)
                out = b""
        except OSError:  # I2cError: no ACK, or the device failed the transfer
            self.status = STATUS_ADDRESS_NAK
            return bytes(setup.length) if read else b""
        self.status = STATUS_ADDRESS_ACK
        return out
