"""Hardware bus framing. A unix stream socketpair stands in for the virtio-serial port."""

import json
import socket

import pytest

from shield_sil.hwbus import BUS_RECORD, HardwareBus
from shield_sil.peripherals import Board
from shield_sil.profile import Profile


@pytest.fixture
def rig(tmp_path):
    board = Board(Profile.load())
    bus = HardwareBus(board, tmp_path / "s", tmp_path / "c", full_frame_every=4)
    host, guest = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    bus.attach_state_socket(host)
    guest.settimeout(1.0)
    yield board, bus, guest
    host.close()
    guest.close()


def read_frame(guest, last: int | None = None) -> list[tuple[str, dict]]:
    """Read up to the end of the next frame. ``last`` keeps only the final N records
    (after a drop the stream may start with the tail of an older frame)."""
    buf = b""
    while not buf.endswith(b"\n") or not buf.rsplit(b"\n", 2)[-2].startswith(f"{BUS_RECORD}\t".encode()):
        buf += guest.recv(65536)
    lines = buf.decode().splitlines()[-last:] if last else buf.decode().splitlines()
    return [(d, json.loads(j)) for d, j in (line.split("\t", 1) for line in lines)]


def test_first_frame_is_full_and_ends_with_seq(rig):
    _board, bus, guest = rig
    assert bus.publish_now()
    frame = read_frame(guest)
    assert [d for d, _ in frame] == ["bms", "pd", "thermal", "modem", "leds", "gpio_in", BUS_RECORD]
    assert frame[-1][1]["seq"] == 1 == bus.seq


def test_unchanged_devices_are_not_resent(rig):
    board, bus, guest = rig
    bus.publish_now()
    read_frame(guest)
    with board.lock:
        board.pd.detach()
    bus.publish_now()
    devs = [d for d, _ in read_frame(guest)]
    assert devs == ["pd", BUS_RECORD]


def test_periodic_full_frame(rig):
    _board, bus, guest = rig
    for _ in range(4):
        bus.publish_now()
        devs = [d for d, _ in read_frame(guest)]
    assert len(devs) == 7  # frame 4 is a full frame


def test_slow_guest_drops_frames_instead_of_queueing_stale_state(rig):
    _board, bus, guest = rig
    sent = sum(bus.publish_now() for _ in range(2000))
    assert bus.dropped_frames > 0 and sent < 2000
    # The guest drains the backlog, after which the next frame goes through in full.
    guest.setblocking(False)
    try:
        while guest.recv(1 << 20):
            pass
    except BlockingIOError:
        pass
    guest.setblocking(True)
    assert bus.publish_now()
    frame = read_frame(guest, last=7)
    assert [d for d, _ in frame] == ["bms", "pd", "thermal", "modem", "leds", "gpio_in", BUS_RECORD]
    assert frame[-1][1]["seq"] == bus.seq
