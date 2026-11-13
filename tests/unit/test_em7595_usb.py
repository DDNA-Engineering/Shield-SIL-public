"""The EM7595 over USB/IP: enumeration, AT on ttyUSB2 and QMI on cdc-wdm0.

The QMI client below does what libqmi does for ModemManager: allocate a client
ID over CTL, send requests with SEND_ENCAPSULATED_COMMAND, wait for the
RESPONSE_AVAILABLE notification and read with GET_ENCAPSULATED_RESPONSE.
"""

import struct
import time

import pytest
from usbip_client import Attached, devlist

from shield_sil.peripherals import Board, Sim
from shield_sil.profile import Profile
from shield_sil.usbip.qmi import (
    CTL,
    DMS,
    NAS,
    UIM,
    WDA,
    WDS,
    QmiMessage,
    ef_iccid,
    ef_imsi,
)
from shield_sil.usbip.shield import shield_server

EP_AT_OUT, EP_AT_IN, EP_NOTIFY = 3, 4, 7


def boot(board, seconds=30):
    for _ in range(seconds):
        with board.lock:
            board.step(1.0)


@pytest.fixture
def board():
    b = Board(Profile.load())
    boot(b)
    return b


@pytest.fixture
def server(board):
    with shield_server(board) as s:
        yield s


@pytest.fixture
def dev(server):
    d = Attached(server.port, "1-1")
    assert d.control(0x00, 9, 1)[0] == 0  # SET_CONFIGURATION 1
    yield d
    d.close()


class Qmi:
    def __init__(self, dev):
        self.dev = dev
        self.txn = 0
        self.notify = dev.submit(EP_NOTIFY, True, 8)

    def _read(self, timeout=5.0):
        status, note = self.dev.wait(self.notify, timeout)
        assert status == 0 and note[:2] == b"\xa1\x01"
        self.notify = self.dev.submit(EP_NOTIFY, True, 8)
        status, frame = self.dev.control(0xA1, 1, 0, 8, 4096)
        assert status == 0 and frame
        return QmiMessage.parse(frame)

    def request(self, service, client, msg_id, tlvs=None):
        self.txn += 1
        flags = 0
        frame = QmiMessage(service, client, flags, self.txn & (0xFF if service == CTL else 0xFFFF),
                           msg_id, tlvs or {}).encode(from_device=False)
        assert self.dev.control(0x21, 0, 0, 8, data=frame)[0] == 0
        while True:
            msg = self._read()
            if msg.msg_id == msg_id and msg.service == service and msg.flags in (1, 2):
                _result, error = struct.unpack("<HH", msg.tlvs[0x02])
                return error, msg.tlvs

    def indication(self, timeout=5.0):
        msg = self._read(timeout)
        assert msg.flags in (2, 4)
        return msg

    def client(self, service):
        err, tlvs = self.request(CTL, 0, 0x0022, {0x01: bytes([service])})
        assert err == 0
        return tlvs[0x01][1]


def at(dev, line):
    assert dev.wait(dev.submit(EP_AT_OUT, False, data=(line + "\r").encode()))[0] == 0
    out = b""
    deadline = time.monotonic() + 5
    while not (out.rstrip().endswith((b"OK", b"ERROR")) or b"+CME ERROR" in out):
        assert time.monotonic() < deadline, out
        _status, chunk = dev.wait(dev.submit(EP_AT_IN, True, 512))
        out += chunk
    return out.decode()


def test_enumerates_as_sierra_composite(board, server, dev):
    assert [d for d in devlist(server.port) if d["busid"] == "1-1"] == []  # already attached
    _status, desc = dev.control(0x80, 6, 0x0100, 0, 18)
    assert struct.unpack("<HH", desc[8:12]) == (0x1199, 0xC081)
    _status, cfg = dev.control(0x80, 6, 0x0200, 0, 512)
    numbers, i = [], 9
    while i < len(cfg):
        if cfg[i + 1] == 4:
            numbers.append(cfg[i + 2])
        i += cfg[i]
    assert numbers == [0, 2, 3, 8]


def test_at_port_answers_with_echo_until_ate0(dev):
    assert at(dev, "AT").startswith("AT\r")
    assert "OK" in at(dev, "ATE0")
    r = at(dev, "AT+CGMM")
    assert r.startswith("\r\nEM7595") and r.rstrip().endswith("OK")
    assert "!UIMS: 0" in at(dev, "AT!UIMS?")


def test_qmi_probe_like_modemmanager(board, dev):
    q = Qmi(dev)
    err, tlvs = q.request(CTL, 0, 0x0021)
    assert err == 0
    services = {tlvs[0x01][1 + 5 * i] for i in range(tlvs[0x01][0])}
    assert {DMS, NAS, WDS, UIM, WDA} <= services
    dms, nas, uim = q.client(DMS), q.client(NAS), q.client(UIM)
    assert q.request(DMS, dms, 0x0022)[1][0x01] == b"EM7595"
    assert q.request(DMS, dms, 0x0025)[1][0x11] == board.modem.imei.encode()
    # Legacy DMS UIM is gone on this firmware, which sends MM to the UIM service.
    assert q.request(DMS, dms, 0x002B)[0] == 71
    err, tlvs = q.request(UIM, uim, 0x002F)
    assert err == 0 and struct.unpack_from("<H", tlvs[0x10])[0] == 0x0000
    sim = board.modem.active_sim
    file_tlv = struct.pack("<HB", 0x2FE2, 2) + b"\x00\x3f"
    err, tlvs = q.request(UIM, uim, 0x0020, {0x01: b"\x06\x00", 0x02: file_tlv, 0x03: b"\0\0\0\0"})
    assert err == 0 and tlvs[0x11][2:] == ef_iccid(sim.iccid)
    file_tlv = struct.pack("<HB", 0x6F07, 4) + b"\x00\x3f\xff\x7f"
    err, tlvs = q.request(UIM, uim, 0x0020, {0x01: b"\x00\x00", 0x02: file_tlv, 0x03: b"\0\0\0\0"})
    assert tlvs[0x11][2:] == ef_imsi(sim.imsi)
    err, tlvs = q.request(NAS, nas, 0x0024)
    assert err == 0 and tlvs[0x01][0] == 1  # registered
    assert tlvs[0x12][5:].decode() == "SIL-Carrier"
    assert q.request(NAS, nas, 0x004F)[0] == 71  # Get Signal Info -> fallback
    err, tlvs = q.request(NAS, nas, 0x0020)
    assert err == 0 and tlvs[0x01][1] == 8  # LTE


def test_wda_raw_ip_and_data_call(board, dev):
    q = Qmi(dev)
    wda, wds = q.client(WDA), q.client(WDS)
    err, tlvs = q.request(WDA, wda, 0x0020, {0x11: struct.pack("<I", 2)})
    assert err == 0 and struct.unpack("<I", tlvs[0x11])[0] == 2
    assert q.request(WDS, wds, 0x002D)[0] == 15  # out of call
    err, tlvs = q.request(WDS, wds, 0x0020, {0x14: b"internet"})
    assert err == 0 and 0x01 in tlvs
    err, tlvs = q.request(WDS, wds, 0x002D)
    assert err == 0
    addr = struct.unpack("<I", tlvs[0x1E])[0]
    assert addr >> 24 == 100 and (addr >> 16) & 0xFF == 64
    assert q.request(WDS, wds, 0x0022)[1][0x01] == b"\x02"
    # Losing service drops the call with an indication.
    with board.lock:
        board.modem.set_service(False)
        board.step(1.0)
    ind = None
    for _ in range(4):
        ind = q.indication()
        if ind.service == WDS:
            break
    assert ind.service == WDS and ind.msg_id == 0x0022 and ind.tlvs[0x01][0] == 1


def test_lte_attach_pdn_list_names_the_default_profile(board, dev):
    # ModemManager 1.24 reads it to set or clear the initial EPS bearer
    # (OpenWrt 25.12's netifd proto does that on every connect).
    q = Qmi(dev)
    wds = q.client(WDS)
    err, tlvs = q.request(WDS, wds, 0x0094)
    assert err == 0 and tlvs[0x10] == b"\x01\x01\x00"
    assert q.request(WDS, wds, 0x0093, {0x01: b"\x01\x01\x00"})[0] == 0
    assert q.request(WDS, wds, 0x0093, {0x01: b"\x01\x07\x00"})[0] != 0
    err, tlvs = q.request(WDS, wds, 0x002B, {0x01: b"\x00\x01"})
    assert err == 0 and tlvs[0x14] == b"internet"
    # Clearing the attach APN (what ModemManager does to delete the initial EPS
    # bearer) is stored and read back.
    assert q.request(WDS, wds, 0x0028, {0x01: b"\x00\x01", 0x14: b""})[0] == 0
    assert q.request(WDS, wds, 0x002B, {0x01: b"\x00\x01"})[1][0x14] == b""
    assert q.request(WDS, wds, 0x0028, {0x01: b"\x00\x07", 0x14: b"x"})[0] != 0


def test_slot_switch_over_qmi_reenumerates(board, server, dev):
    with board.lock:
        board.modem.insert_sim("nano2", carrier="Other")
    boot(board)
    q = Qmi(dev)
    uim = q.client(UIM)
    err, tlvs = q.request(UIM, uim, 0x0047)
    assert err == 0 and tlvs[0x10][0] == 2
    err, _ = q.request(UIM, uim, 0x0046, {0x01: b"\x01", 0x02: struct.pack("<I", 2)})
    assert err == 0
    time.sleep(0.5)
    assert dev.closed  # the module restarts and leaves USB
    boot(board)
    time.sleep(0.5)
    assert "1-1" in [d["busid"] for d in devlist(server.port)]


def test_stuck_slot_returns_internal_and_at_fallback_works(board, dev):
    board.modem.faults.qmi_slot_stuck = True
    q = Qmi(dev)
    uim = q.client(UIM)
    err, _ = q.request(UIM, uim, 0x0046, {0x01: b"\x01", 0x02: struct.pack("<I", 2)})
    assert err == 3  # Internal; the AT!UIMS fallback below still works
    assert at(dev, "AT!UIMS=1").rstrip().endswith("OK")


def test_fastboot_is_c082_with_one_vendor_interface(board, server, dev):
    at(dev, "AT!BOOTHOLD")
    boot(board)
    time.sleep(0.5)
    found = [d for d in devlist(server.port) if d["busid"] == "1-1"]
    assert found and found[0]["id"] == "1199:c082" and found[0]["interfaces"] == [(0xFF, 0x42, 0x03)]


def test_wedged_modem_never_answers_qmi(board, dev):
    q = Qmi(dev)
    with board.lock:
        board.modem.wedge()
    frame = QmiMessage(CTL, 0, 0, 1, 0x0021, {}).encode(from_device=False)
    assert dev.control(0x21, 0, 0, 8, data=frame)[0] == 0
    with pytest.raises(TimeoutError):
        dev.wait(q.notify, timeout=1.0)


def test_no_sim_card_status(board, dev):
    with board.lock:
        board.modem.remove_sim("nano1")
    q = Qmi(dev)
    uim = q.client(UIM)
    err, tlvs = q.request(UIM, uim, 0x002F)
    assert err == 0 and struct.unpack_from("<H", tlvs[0x10])[0] == 0xFFFF
    assert tlvs[0x10][9] == 0  # card absent


def test_esim_via_gpio_shows_in_slot_status(board, dev):
    board.modem.sims["esim"] = Sim(present=True, carrier="eSIM", msin="0000000009")
    board.guest_command("gpio", {"name": "shield:cell:sim-select", "value": 1})
    q = Qmi(dev)
    uim = q.client(UIM)
    _, tlvs = q.request(UIM, uim, 0x0047)
    slot1_iccid_len = tlvs[0x10][1 + 9]
    assert tlvs[0x10][1 + 10 : 1 + 10 + slot1_iccid_len] == ef_iccid(board.modem.sims["esim"].iccid)


def test_registration_change_is_indicated(board, dev):
    q = Qmi(dev)
    nas = q.client(NAS)
    assert q.request(NAS, nas, 0x0024)[1][0x01][0] == 1
    with board.lock:
        board.modem.set_service(False)
        board.step(1.0)
    ind = q.indication()
    assert (ind.service, ind.msg_id, ind.client) == (NAS, 0x0024, nas)
    assert ind.tlvs[0x01][0] == 2  # searching


# --- wwan0 raw-IP data path ---------------------------------------------------
EP_DATA_OUT, EP_DATA_IN = 4, 6
PACKET = bytes.fromhex("4500001c00000000400100000a0000016440013208000000")  # IPv4 header + ICMP


@pytest.fixture
def data_path():
    from shield_sil.usbip.datapath import PacketQueue

    return PacketQueue()


@pytest.fixture
def data_dev(board, data_path):
    with shield_server(board, data=data_path) as s:
        d = Attached(s.port, "1-1")
        assert d.control(0x00, 9, 1)[0] == 0
        yield d
        d.close()


def _connect(q, raw_ip=True):
    wda, wds = q.client(WDA), q.client(WDS)
    if raw_ip:
        assert q.request(WDA, wda, 0x0020, {0x11: struct.pack("<I", 2)})[0] == 0
    assert q.request(WDS, wds, 0x0020, {0x14: b"internet"})[0] == 0
    return wds


def _uplink(dev, packet=PACKET):
    assert dev.wait(dev.submit(EP_DATA_OUT, False, data=packet))[0] == 0


def test_no_uplink_before_a_call(data_dev, data_path):
    _uplink(data_dev)
    time.sleep(0.1)
    assert data_path.sent.empty()


def test_raw_ip_call_carries_both_directions(data_dev, data_path):
    _connect(Qmi(data_dev))
    _uplink(data_dev)
    assert data_path.sent.get(timeout=2) == PACKET
    urb = data_dev.submit(EP_DATA_IN, True, 2048)
    data_path.inject(PACKET[::-1])
    status, got = data_dev.wait(urb, 2)
    assert status == 0 and got == PACKET[::-1]


def test_802_3_mode_carries_nothing(data_dev, data_path):
    _connect(Qmi(data_dev), raw_ip=False)
    _uplink(data_dev)
    time.sleep(0.1)
    assert data_path.sent.empty()


def test_losing_service_stops_data(board, data_dev, data_path):
    _connect(Qmi(data_dev))
    with board.lock:
        board.modem.set_service(False)
        board.step(1.0)
    _uplink(data_dev)
    urb = data_dev.submit(EP_DATA_IN, True, 2048)
    data_path.inject(PACKET)
    with pytest.raises(TimeoutError):
        data_dev.wait(urb, 0.5)
    assert data_path.sent.empty()


# --- cellular radio-link impairment on the data path ---------------------------
def test_impaired_data_path_passes_clean_link_both_ways():
    from shield_sil.usbip.datapath import ImpairedDataPath, PacketQueue

    inner = PacketQueue()
    path = ImpairedDataPath(inner)
    got = []
    path.set_receiver(got.append)
    path.send(b"up")
    inner.inject(b"down")
    assert inner.sent.get(timeout=1) == b"up"
    assert got == [b"down"]
    path.close()


def test_impaired_data_path_delays_and_cuts():
    import time

    from shield_sil.usbip.datapath import ImpairedDataPath, PacketQueue

    inner = PacketQueue()
    path = ImpairedDataPath(inner)
    path.set(delay_ms=150)
    t0 = time.monotonic()
    path.send(b"x")
    assert inner.sent.get(timeout=2) == b"x"
    assert time.monotonic() - t0 >= 0.14
    path.set(up=False)
    path.send(b"lost")
    inner.inject(b"lost")
    assert inner.sent.empty() and path.dropped == {"uplink": 1, "downlink": 1}
    path.restore()
    path.send(b"back")
    assert inner.sent.get(timeout=1) == b"back"
    path.close()
