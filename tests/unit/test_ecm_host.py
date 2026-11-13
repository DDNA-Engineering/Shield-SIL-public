"""The simulated laptop's USB host for the USB-C ECM gadget (network.usbc = "gadget").

Runs EcmLaptop against the SIL's own USB/IP server exporting a minimal ECM
device, so enumeration, frame relay in both directions, the ZLP padding rule
and cable unplug/replug are checked without a guest.
"""

import socket
import time

from shield_sil.usbip.device import EP_BULK, EP_INTERRUPT, Endpoint, Interface, UsbDevice
from shield_sil.usbip.ecm_host import EcmLaptop, parse_ecm_config
from shield_sil.usbip.server import UsbipServer

BUSID = "1-9"


class FakeEcm(UsbDevice):
    vendor_id, product_id = 0x1D6B, 0x0104

    def __init__(self):
        super().__init__()
        self.interfaces = [
            Interface(0, 0x02, 0x06, 0, [Endpoint(0x83, EP_INTERRUPT, 16, 9)]),
            Interface(1, 0x0A, 0, 0, [Endpoint(0x81, EP_BULK, 512), Endpoint(0x02, EP_BULK, 512)]),
        ]
        self.received = []
        self.disconnects = 0

    def out_transfer(self, ep, data):
        self.received.append(data)

    def disconnect(self):
        self.disconnects += 1
        super().disconnect()


def wait(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def _free_udp_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_parse_ecm_config_picks_data_alt_with_bulk_endpoints():
    # Real f_ecm layout: data interface alt 0 has no endpoints, alt 1 has both.
    cfg = bytes([9, 2, 0, 0, 2, 1, 0, 0x80, 50])
    cfg += bytes([9, 4, 0, 0, 1, 2, 6, 0, 0]) + bytes([7, 5, 0x83, 3, 16, 0, 9])
    cfg += bytes([9, 4, 1, 0, 0, 0x0A, 0, 0, 0])
    cfg += bytes([9, 4, 1, 1, 2, 0x0A, 0, 0, 0])
    cfg += bytes([7, 5, 0x81, 2, 0, 2, 0]) + bytes([7, 5, 0x02, 2, 0, 2, 0])
    eps = parse_ecm_config(cfg)
    assert (eps.config, eps.data_interface, eps.data_alt) == (1, 1, 1)
    assert (eps.bulk_in, eps.bulk_out, eps.max_packet_out) == (1, 2, 512)


def test_frames_relay_both_ways_and_unplug_replug():
    dev = FakeEcm()
    wire = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    wire.bind(("127.0.0.1", 0))
    wire.settimeout(3)
    wire_port = wire.getsockname()[1]
    local = _free_udp_port()
    with UsbipServer() as srv:
        srv.export(BUSID, dev)
        laptop = EcmLaptop(("127.0.0.1", srv.port), wire_port, local, retry_s=0.1, busid=BUSID).start()
        try:
            assert laptop.connected.wait(5)
            assert dev.configuration == 1
            # Laptop -> Shield: one frame per bulk OUT transfer.
            wire.sendto(b"\x01" * 100, ("127.0.0.1", local))
            assert wait(lambda: dev.received)
            assert dev.received[0] == b"\x01" * 100
            # A frame filling whole 512-byte packets gets one pad byte (no ZLP).
            wire.sendto(b"\x02" * 1024, ("127.0.0.1", local))
            assert wait(lambda: len(dev.received) == 2)
            assert len(dev.received[1]) == 1025
            # Shield -> laptop: each bulk IN completion is one frame on the wire.
            dev.send_in(0x81, b"\x03" * 60)
            assert wire.recv(4096) == b"\x03" * 60
            # Cable out: the device sees the host go away; frames are lost.
            laptop.set_plugged(False)
            assert wait(lambda: dev.disconnects >= 1)
            assert not laptop.connected.is_set()
            # Cable back in: enumerated again, traffic flows.
            laptop.set_plugged(True)
            assert laptop.connected.wait(5)
            dev.send_in(0x81, b"\x04" * 60)
            assert wire.recv(4096) == b"\x04" * 60
        finally:
            laptop.stop()
            wire.close()
