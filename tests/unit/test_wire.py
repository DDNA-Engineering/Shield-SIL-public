"""Wire relay tests. A unix datagram socketpair stands in for the tap device."""

import socket
import time

import pytest

from shield_sil.wire import Wire


@pytest.fixture
def rig(unused_udp_port_pair):
    qemu_port, wire_port = unused_udp_port_pair
    tap_host, tap_wire = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    qemu = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    qemu.bind(("127.0.0.1", qemu_port))
    qemu.settimeout(1.0)
    tap_host.settimeout(1.0)
    w = Wire("t", tap_wire.detach(), qemu_port=qemu_port, local_port=wire_port, seed=1)
    w.start()
    yield w, qemu, tap_host, ("127.0.0.1", wire_port)
    w.stop()
    qemu.close()
    tap_host.close()


@pytest.fixture
def unused_udp_port_pair():
    ports = []
    socks = []
    for _ in range(2):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", 0))
        ports.append(s.getsockname()[1])
        socks.append(s)
    for s in socks:
        s.close()
    return ports


def test_frames_pass_both_ways(rig):
    w, qemu, tap, wire_addr = rig
    qemu.sendto(b"from-guest", wire_addr)
    assert tap.recv(100) == b"from-guest"
    tap.send(b"to-guest")
    assert qemu.recv(100) == b"to-guest"


def test_cut_drops_everything_and_restore_recovers(rig):
    w, qemu, tap, wire_addr = rig
    w.cut()
    qemu.sendto(b"x", wire_addr)
    with pytest.raises(socket.timeout):
        tap.recv(100)
    assert w.stats()["to_host"]["dropped"] == 1
    w.restore()
    qemu.sendto(b"y", wire_addr)
    assert tap.recv(100) == b"y"


def test_delay_is_applied(rig):
    w, qemu, tap, wire_addr = rig
    w.set(delay_ms=200)
    t0 = time.monotonic()
    tap.send(b"late")
    assert qemu.recv(100) == b"late"
    assert time.monotonic() - t0 >= 0.19


def test_loss_rate_is_roughly_honoured(rig):
    w, qemu, tap, wire_addr = rig
    w.set(loss_pct=50)
    for i in range(400):
        qemu.sendto(b"%d" % i, wire_addr)
        if i % 50 == 0:
            time.sleep(0.01)  # stay under the kernel socket buffer
    time.sleep(0.5)
    s = w.stats()["to_host"]
    seen = s["dropped"] + s["frames"]
    assert seen > 300
    assert 0.35 < s["dropped"] / seen < 0.65


def test_invalid_impairment_rejected(rig):
    w, *_ = rig
    with pytest.raises(ValueError):
        w.set(loss_pct=101)
