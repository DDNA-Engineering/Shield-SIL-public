"""USB/IP server and the i2c-tiny-usb adapters, driven the way the guest drives them."""

import struct

import pytest
from test_charger import ChargerHostClient
from usbip_client import Attached, devlist

from shield_sil.peripherals import Board
from shield_sil.profile import Profile
from shield_sil.usbip.shield import shield_server

ENXIO = "ENXIO"


@pytest.fixture
def board():
    return Board(Profile.load())


@pytest.fixture
def server(board):
    with shield_server(board) as s:
        yield s


class TinyUsbClient(ChargerHostClient):
    """ChargerHostClient with each i2c-dev transfer going over i2c-tiny-usb, as i2c_tiny_usb.ko does."""

    def __init__(self, dev, board):
        super().__init__(board)
        self.dev = dev

    def _msg(self, addr, read, data=b"", n=0):
        cmd = 4 | 1 | 2  # one message: BEGIN | END
        if read:
            status, out = self.dev.control(0xC1, cmd, 1, addr, n)
        else:
            status, out = self.dev.control(0x41, cmd, 0, addr, data=bytes(data))
        assert status == 0
        _st, s = self.dev.control(0xC1, 3, 0, 0, 1)
        if s[0] == 2:
            from shield_sil.peripherals import I2cError
            raise I2cError(ENXIO)
        return out

    def _xfer(self, write, read_len=0):
        if write:
            self._msg(0x20, False, write)
        return self._msg(0x20, True, n=read_len) if read_len else b""


def test_devlist_in_attach_order(server):
    devs = devlist(server.port)
    assert [d["busid"] for d in devs] == ["1-3", "1-2", "1-4"]
    assert all(d["id"] == "0403:c631" for d in devs[:2])
    assert devs[2]["id"] == "1209:0001"  # front-panel button (HID)


def test_descriptors_and_distinct_serials(server):
    serials = []
    for busid in ("1-3", "1-2"):
        dev = Attached(server.port, busid)
        st, desc = dev.control(0x80, 6, 0x0100, 0, 18)
        assert st == 0 and desc[:2] == b"\x12\x01"
        vid, pid = struct.unpack("<HH", desc[8:12])
        assert (vid, pid) == (0x0403, 0xC631)
        st, cfg = dev.control(0x80, 6, 0x0200, 0, 255)
        assert st == 0 and cfg[1] == 2 and cfg[4] == 1  # one interface
        st, s = dev.control(0x80, 6, 0x0300 | desc[16], 0x0409, 255)
        serials.append(s[2:].decode("utf-16-le"))
        dev.close()
    assert serials == ["shield-i2c0", "shield-i2c1"]


def test_functionality_and_unknown_request(server):
    dev = Attached(server.port, "1-2")
    st, f = dev.control(0xC1, 1, 0, 0, 4)
    assert st == 0 and struct.unpack("<I", f)[0] & 1
    st, _ = dev.control(0xC1, 99, 0, 0, 1)
    assert st == -32  # stall
    dev.close()


def test_battery_status_over_usbip(board, server):
    board.battery.soc = 0.5
    board.step(1.0)
    dev = Attached(server.port, "1-2")
    out = TinyUsbClient(dev, board).status()
    assert out["battery_present"] and out["vbus_present"]
    assert out["vbat_mv"] == round(board.battery.voltage * 1000)
    dev.close()


def test_i2c0_has_nothing_and_absent_tps_naks(board, server):
    empty = Attached(server.port, "1-3")
    with pytest.raises(OSError, match=ENXIO):
        TinyUsbClient(empty, board).tps_read(0x03, 4)
    empty.close()
    board.charger_faults.tps_absent = True
    dev = Attached(server.port, "1-2")
    with pytest.raises(OSError, match=ENXIO):
        TinyUsbClient(dev, board).check_ready()
    dev.close()


def test_device_imported_once(server):
    a = Attached(server.port, "1-2")
    with pytest.raises(ConnectionRefusedError):
        Attached(server.port, "1-2")
    assert [d["busid"] for d in devlist(server.port)] == ["1-3", "1-4"]
    a.close()


def test_usbip_record_on_the_hw_bus(board, server):
    assert "usbip" not in board.snapshot()
    board.usbip_endpoint = ("10.0.2.2", server.port)
    rec = board.snapshot()["usbip"]
    assert rec == {"host": "10.0.2.2", "port": server.port, "busids": ["1-3", "1-2", "1-4"]}
    for _ in range(30):
        board.step(1.0)
    assert board.snapshot()["usbip"]["busids"] == ["1-3", "1-2", "1-4", "1-1"]
    board.reset()
    assert board.snapshot()["usbip"]["port"] == server.port


def test_gpio_outputs_drive_the_modem_and_inputs_follow_power(board):
    for _ in range(30):
        board.step(1.0)
    board.guest_command("gpio", {"name": "shield:cell:full-card-power", "value": 0})
    board.step(1.0)
    assert board.modem.state.value == "off"
    board.pd.detach()
    board.step(1.0)
    assert board.snapshot()["gpio_in"]["shield:pgood:dc-efuse"] == 0
    with pytest.raises(ValueError):
        board.guest_command("gpio", {"name": "shield:pgood:dc-efuse", "value": 1})


def test_led_brightness_from_uleds(board):
    board.guest_command("led", {"name": "WAN", "brightness": 255, "trigger": "netdev"})
    assert board.leds.state["WAN"] == "on" and board.leds.detail["WAN"]["trigger"] == "netdev"
    board.guest_command("led", {"name": "WAN", "brightness": 0, "trigger": "netdev"})
    assert board.leds.state["WAN"] == "off"


def test_send_failure_with_waiting_transfers_does_not_deadlock():
    """A send that fails because the guest closed the connection closes the session,
    which fails the device's other waiting IN transfers; that must not re-enter the
    session's write lock (it froze the device and every import after it)."""
    import socket
    import threading

    from shield_sil.usbip.device import UsbDevice
    from shield_sil.usbip.server import Export, _Session

    dev = UsbDevice()
    ours, guest = socket.socketpair()
    s = _Session(Export("1-4", dev), ours)
    for seqnum in (1, 2):
        dev.submit_in(0x81, seqnum, 64, lambda st, d, n=seqnum: s.ret_submit(n, st, d))
    guest.close()
    t = threading.Thread(target=dev.send_in, args=(0x81, b"x"), daemon=True)
    t.start()
    t.join(timeout=5)
    assert not t.is_alive(), "send_in deadlocked after the connection closed"
    assert s.closed.is_set()
    assert dev.lock.acquire(timeout=1)
    dev.lock.release()
