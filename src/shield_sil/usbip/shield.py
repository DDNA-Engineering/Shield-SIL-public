"""The Shield's USB/IP exports, in the order the guest attaches them.

* ``1-3``: i2c-tiny-usb standing in for the SoM's i2c-0 (nothing on it), so the
  next adapter the guest attaches is numbered i2c-1.
* ``1-2``: i2c-tiny-usb for i2c-1, the battery bus (TPS25751 at 0x20).
* ``1-4``: the front-panel button (HID, reports ``BTN_0``).
* ``1-1``: the EM7595, while it is on USB. Attached last: guest vhci port 4.
"""

from __future__ import annotations

from ..peripherals import Board
from .datapath import PacketQueue, TunDataPath
from .button import ButtonHid
from .em7595 import Em7595Usb
from .i2c_tiny import I2cTinyUsb
from .server import UsbipServer


def shield_server(board: Board, host: str = "127.0.0.1", port: int = 0,
                  data: TunDataPath | PacketQueue | None = None) -> UsbipServer:
    server = UsbipServer(host, port)
    server.export(Board.BUSID_I2C0, I2cTinyUsb(None, serial="shield-i2c0"))
    server.export(Board.BUSID_I2C1, I2cTinyUsb(
        lambda addr, write, n: board.i2c_transfer(1, addr, write, n), serial="shield-i2c1"))
    server.export(Board.BUSID_BUTTON, ButtonHid())
    server.export(Board.BUSID_MODEM, Em7595Usb(board, data))
    return server
