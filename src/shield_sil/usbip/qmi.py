"""The EM7595's QMI firmware, as much of it as ModemManager uses.

Messages and TLV layouts follow libqmi's service definitions
(data/qmi-service-*.json); the request set follows what ModemManager 1.22
sends to a QMI modem (src/mm-broadband-modem-qmi.c, mm-shared-qmi.c,
mm-sim-qmi.c, mm-bearer-qmi.c). Requests this model does not implement are
answered with ``InvalidQmiCommand``, which is what ModemManager expects from
firmware that lacks a command and the cue for its fallbacks (for example
Get Signal Info -> Get Signal Strength, Get System Info -> Get Serving System,
DMS UIM -> UIM service).

State comes from the ``Modem`` model on the board, read under the board lock.
"""

from __future__ import annotations

import struct
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from ipaddress import IPv4Address

from ..peripherals.modem import Modem, ModemState, QmiError, Sim

# Services
CTL, WDS, DMS, NAS, UIM, WDA = 0x00, 0x01, 0x02, 0x03, 0x0B, 0x1A
VERSIONS = {CTL: (1, 5), WDS: (1, 12), DMS: (1, 7), NAS: (1, 7), UIM: (1, 26), WDA: (1, 1)}

# Protocol errors (libqmi qmi-errors.h)
ERR_INTERNAL = 3
ERR_INVALID_CLIENT_ID = 7
ERR_INVALID_PROFILE = 10
ERR_NO_NETWORK_FOUND = 13
ERR_CALL_FAILED = 14
ERR_OUT_OF_CALL = 15
ERR_NOT_PROVISIONED = 16
ERR_MISSING_ARGUMENT = 17
ERR_NO_EFFECT = 26
ERR_INVALID_ARGUMENT = 48
ERR_DEVICE_NOT_READY = 52
ERR_INVALID_QMI_COMMAND = 71
ERR_SIM_FILE_NOT_FOUND = 80
ERR_NO_SIM = 102
_ERR_BY_NAME = {"Internal": ERR_INTERNAL, "NoEffect": ERR_NO_EFFECT, "InvalidArgument": ERR_INVALID_ARGUMENT}


class QmiFail(Exception):
    def __init__(self, code: int, tlvs: dict[int, bytes] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.tlvs = tlvs or {}


# ---------------------------------------------------------------- framing
def parse_tlvs(data: bytes) -> dict[int, bytes]:
    out, i = {}, 0
    while i + 3 <= len(data):
        t, n = data[i], struct.unpack_from("<H", data, i + 1)[0]
        out[t] = data[i + 3 : i + 3 + n]
        i += 3 + n
    return out


def build_tlvs(tlvs: dict[int, bytes]) -> bytes:
    return b"".join(struct.pack("<BH", t, len(v)) + v for t, v in sorted(tlvs.items()))


def result_tlv(error: int = 0) -> bytes:
    return struct.pack("<HH", 1 if error else 0, error)


@dataclass
class QmiMessage:
    service: int
    client: int
    flags: int  # SDU flags: CTL 0 req/1 resp/2 ind; others 0 req/2 resp/4 ind
    txn: int
    msg_id: int
    tlvs: dict[int, bytes] = field(default_factory=dict)

    @classmethod
    def parse(cls, frame: bytes) -> QmiMessage:
        if len(frame) < 6 or frame[0] != 0x01:
            raise ValueError("not a QMUX frame")
        _length, _qflags, service, client = struct.unpack_from("<HBBB", frame, 1)
        sdu = frame[6:]
        if service == CTL:
            flags, txn, msg_id, n = struct.unpack_from("<BBHH", sdu)
            body = sdu[6 : 6 + n]
        else:
            flags, txn, msg_id, n = struct.unpack_from("<BHHH", sdu)
            body = sdu[7 : 7 + n]
        return cls(service, client, flags, txn, msg_id, parse_tlvs(body))

    def encode(self, from_device: bool = True) -> bytes:
        body = build_tlvs(self.tlvs)
        if self.service == CTL:
            sdu = struct.pack("<BBHH", self.flags, self.txn, self.msg_id, len(body)) + body
        else:
            sdu = struct.pack("<BHHH", self.flags, self.txn, self.msg_id, len(body)) + body
        qmux = struct.pack("<BBB", 0x80 if from_device else 0x00, self.service, self.client) + sdu
        return b"\x01" + struct.pack("<H", len(qmux) + 2) + qmux


def _resp_flags(service: int) -> int:
    return 0x01 if service == CTL else 0x02


def _ind_flags(service: int) -> int:
    return 0x02 if service == CTL else 0x04


# ---------------------------------------------------------------- SIM files
def _bcd_swapped(digits: str, length: int) -> bytes:
    digits = digits.ljust(length * 2, "F")
    return bytes(int(digits[i + 1], 16) << 4 | int(digits[i], 16) for i in range(0, length * 2, 2))


def ef_iccid(iccid: str) -> bytes:
    return _bcd_swapped(iccid, 10)


def ef_imsi(imsi: str) -> bytes:
    # 3GPP TS 31.102 4.2.2: length byte, then parity nibble + digits, nibble-swapped.
    parity = 0x9 if len(imsi) % 2 else 0x1
    nibbles = [parity] + [int(c) for c in imsi]
    if len(nibbles) % 2:
        nibbles.append(0xF)
    body = bytes(nibbles[i + 1] << 4 | nibbles[i] for i in range(0, len(nibbles), 2))
    return bytes([len(body)]) + body


def ef_spn(name: str) -> bytes:
    return b"\x01" + name.encode()[:16].ljust(16, b"\xff")


def ef_ad(mnc_digits: int) -> bytes:
    return bytes([0x00, 0x00, 0x00, mnc_digits])


# ---------------------------------------------------------------- the firmware
@dataclass
class _WdsCall:
    handle: int
    ip_family: int = 4


class QmiFirmware:
    """Answers QMUX requests from the host and produces indications.

    ``modem`` returns the board's Modem; ``lock`` is the board lock.
    ``bearer`` gives the IPv4 settings handed out on Start Network.
    """

    def __init__(self, modem: Callable[[], Modem], lock: threading.RLock,
                 bearer: dict[str, str] | None = None) -> None:
        self._modem = modem
        self._lock = lock
        self.bearer = bearer or {
            # The cellular ISP's data network in the net lab (NetLab.open_cell_data).
            "address": "100.64.1.50", "gateway": "100.64.1.1", "netmask": "255.255.255.0",
            "dns1": "100.64.1.1", "dns2": "100.64.1.1", "mtu": "1500",
        }
        self.clients: dict[int, set[int]] = {s: set() for s in VERSIONS if s != CTL}
        self._next_cid = dict.fromkeys(VERSIONS, 1)
        self.link_layer = 1  # WDA: 802.3 until the host asks for raw-ip
        self.calls: dict[int, _WdsCall] = {}  # WDS client -> active call
        self.attach_pdns: list[int] = [1]  # LTE attach PDN list: profile 1 ("internet")
        # 3GPP profile 1, as stored in the module (Modify Profile writes it, Get
        # Profile Settings reads it back): name, PDP type (0 = IPv4), APN, auth.
        self.profile1: dict[int, bytes] = {0x10: b"internet", 0x11: _u8(0), 0x14: b"internet", 0x1D: _u8(0)}
        self._next_handle = 0x1000
        self.requests: list[tuple[int, int]] = []  # (service, msg_id) log
        self._last_reg: tuple | None = None  # serving system last reported to NAS clients

    # --- entry points ---------------------------------------------------
    def handle(self, frame: bytes) -> list[bytes]:
        try:
            req = QmiMessage.parse(frame)
        except (ValueError, struct.error):
            return []
        self.requests.append((req.service, req.msg_id))
        with self._lock:
            if req.service != CTL and req.client not in self.clients.get(req.service, set()):
                return [self._response(req, {}, ERR_INVALID_CLIENT_ID)]
            handler = _HANDLERS.get((req.service, req.msg_id))
            if handler is None:
                return [self._response(req, {}, ERR_INVALID_QMI_COMMAND)]
            try:
                tlvs = handler(self, req)
                return [self._response(req, tlvs, 0)]
            except QmiFail as e:
                return [self._response(req, e.tlvs, e.code)]

    def poll_indications(self) -> list[bytes]:
        """Indications for state that changed since the last poll."""
        out: list[bytes] = []
        with self._lock:
            m = self._modem()
            reg = self._serving_system(m)
            key = tuple(sorted(reg.items()))
            if self._last_reg is not None and key != self._last_reg:
                for cid in self.clients[NAS]:
                    out.append(QmiMessage(NAS, cid, _ind_flags(NAS), 0, 0x0024, reg).encode())
            self._last_reg = key
            if not m.data_up:
                # Calls do not survive losing the data path: tell each WDS client.
                for cid in list(self.calls):
                    del self.calls[cid]
                    ind = {0x01: bytes([1, 0]), 0x10: struct.pack("<H", 2)}  # disconnected, generic
                    out.append(QmiMessage(WDS, cid, _ind_flags(WDS), 0, 0x0022, ind).encode())
        return out

    def _response(self, req: QmiMessage, tlvs: dict[int, bytes], error: int) -> bytes:
        tlvs = {**tlvs, 0x02: result_tlv(error)}
        return QmiMessage(req.service, req.client, _resp_flags(req.service), req.txn, req.msg_id,
                          tlvs).encode()

    # --- helpers --------------------------------------------------------
    @property
    def modem(self) -> Modem:
        return self._modem()

    def _sim(self) -> Sim:
        sim = self.modem.active_sim
        if not sim.present:
            raise QmiFail(ERR_NO_SIM)
        return sim

    def _serving_system(self, m: Modem) -> dict[int, bytes]:
        state = m.state
        if state in (ModemState.REGISTERED, ModemState.CONNECTED):
            reg, ps, net, radios = 1, 1, 2, [8]
        elif state is ModemState.SEARCHING:
            reg, ps, net, radios = 2, 2, 0, []
        else:
            reg, ps, net, radios = 0, 2, 0, []
        tlvs = {0x01: struct.pack("<BBBBB", reg, ps, ps, net, len(radios)) + bytes(radios)}
        if reg == 1:
            sim = m.active_sim
            mcc, mnc = int(sim.mcc_mnc[:3]), int(sim.mcc_mnc[3:])
            desc = sim.carrier.encode()
            tlvs[0x10] = b"\x01"  # roaming off
            tlvs[0x12] = struct.pack("<HHB", mcc, mnc, len(desc)) + desc
            tlvs[0x27] = struct.pack("<HHB", mcc, mnc, 1 if len(sim.mcc_mnc) == 6 else 0)
        return tlvs


# ---------------------------------------------------------------- handlers
def _u8(v: int) -> bytes:
    return struct.pack("<B", v)


def _u32(v: int) -> bytes:
    return struct.pack("<I", v)


def _ip(s: str) -> bytes:
    return struct.pack("<I", int(IPv4Address(s)))


def _ok(_fw: QmiFirmware, _req: QmiMessage) -> dict[int, bytes]:
    return {}


# CTL
def ctl_version_info(_fw, _req):
    body = bytes([len(VERSIONS)]) + b"".join(
        struct.pack("<BHH", s, maj, mnr) for s, (maj, mnr) in VERSIONS.items())
    return {0x01: body}


def ctl_allocate_cid(fw, req):
    if 0x01 not in req.tlvs:
        raise QmiFail(ERR_MISSING_ARGUMENT)
    service = req.tlvs[0x01][0]
    if service not in fw.clients:
        raise QmiFail(ERR_INVALID_ARGUMENT)
    cid = fw._next_cid[service]
    fw._next_cid[service] = cid % 254 + 1
    fw.clients[service].add(cid)
    return {0x01: bytes([service, cid])}


def ctl_release_cid(fw, req):
    service, cid = req.tlvs.get(0x01, b"\0\0")[:2]
    fw.clients.get(service, set()).discard(cid)
    if service == WDS:
        fw.calls.pop(cid, None)
    return {0x01: bytes([service, cid])}


# DMS
def dms_capabilities(_fw, _req):
    radios = [5, 8]  # UMTS, LTE
    return {0x01: struct.pack("<IIBBB", 150_000_000, 300_000_000, 3, 2, len(radios)) + bytes(radios)}


def dms_manufacturer(fw, _req):
    return {0x01: b"Sierra Wireless, Incorporated"}


def dms_model(fw, _req):
    return {0x01: b"EM7595"}


def dms_revision(fw, _req):
    return {0x01: fw.modem.firmware.encode()}


def dms_ids(fw, _req):
    return {0x11: fw.modem.imei.encode(), 0x13: b"01"}


def dms_msisdn(_fw, _req):
    raise QmiFail(ERR_NOT_PROVISIONED)


def dms_power_state(_fw, _req):
    return {0x01: bytes([1, 100])}  # external power, 100 %


def dms_get_operating_mode(fw, _req):
    m = fw.modem
    return {0x01: _u8(0 if m.cfun == 1 else 1), 0x11: _u8(0)}


def dms_set_operating_mode(fw, req):
    mode = req.tlvs.get(0x01, b"\0")[0]
    m = fw.modem
    if mode == 0:
        m.at("AT+CFUN=1")
    elif mode in (1, 6, 7):
        m.at("AT+CFUN=4")
    elif mode == 4:
        m.at("AT+CFUN=1,1")
    else:
        raise QmiFail(ERR_INVALID_ARGUMENT)
    return {}


def dms_iccid(fw, _req):
    return {0x01: fw._sim().iccid.encode()}


def dms_imsi(fw, _req):
    return {0x01: fw._sim().imsi.encode()}


def dms_band_caps(_fw, _req):
    return {0x01: struct.pack("<Q", 0), 0x10: struct.pack("<Q", 0x00000000_080800DF)}


def dms_software_version(fw, _req):
    return {0x01: fw.modem.firmware.encode()}


# NAS
def nas_serving_system(fw, _req):
    tlvs = fw._serving_system(fw.modem)
    fw._last_reg = tuple(sorted(tlvs.items()))
    return tlvs


def nas_signal_strength(fw, _req):
    m = fw.modem
    if m.state not in (ModemState.REGISTERED, ModemState.CONNECTED):
        raise QmiFail(ERR_NO_NETWORK_FOUND)
    rsrp = m.active_sim.signal_dbm
    rssi = max(-113, min(-51, round(rsrp) + 25))
    return {0x01: struct.pack("<bb", rssi, 8)}


def nas_home_network(fw, _req):
    sim = fw._sim()
    desc = sim.carrier.encode()
    return {0x01: struct.pack("<HHB", int(sim.mcc_mnc[:3]), int(sim.mcc_mnc[3:]), len(desc)) + desc}


def nas_ssp(_fw, _req):
    return {0x11: struct.pack("<H", 0x18), 0x10: _u8(0)}  # UMTS | LTE


# UIM
def uim_card_status(fw, _req):
    sim = fw.modem.active_sim
    if not sim.present:
        card = bytes([0, 0, 0, 0, 0, 0])  # absent, no applications
        return {0x10: struct.pack("<HHHHB", 0xFFFF, 0xFFFF, 0xFFFF, 0xFFFF, 1) + card}
    aid = bytes.fromhex("a0000000871002ff86ff0389ffffffff")
    app = bytes([2, 7, 2, 11, 0, 0, len(aid)]) + aid + bytes([0, 3, 3, 10, 3, 3, 10])
    card = bytes([1, 0, 0, 0, 0, 1]) + app
    return {0x10: struct.pack("<HHHHB", 0x0000, 0xFFFF, 0xFFFF, 0xFFFF, 1) + card}


def uim_slot_status(fw, _req):
    m = fw.modem
    slots = b""
    for phys in (1, 2):
        holder = "nano2" if phys == 2 else ("esim" if m.sim_select_gpio else "nano1")
        sim = m.sims.get(holder) or Sim(present=False)
        iccid = ef_iccid(sim.iccid) if sim.present else b""
        active = phys == m.nv_slot
        slots += struct.pack("<IIBB", 2 if sim.present else 1, 1 if active else 0,
                             1 if active else 0, len(iccid)) + iccid
    return {0x10: bytes([2]) + slots}


def uim_switch_slot(fw, req):
    if 0x02 not in req.tlvs:
        raise QmiFail(ERR_MISSING_ARGUMENT)
    phys = struct.unpack("<I", req.tlvs[0x02])[0]
    try:
        fw.modem.qmi_switch_slot(phys)
    except QmiError as e:
        raise QmiFail(_ERR_BY_NAME.get(e.name, ERR_INTERNAL)) from None
    return {}


def uim_read_transparent(fw, req):
    f = req.tlvs.get(0x02)
    if not f or len(f) < 3:
        raise QmiFail(ERR_MISSING_ARGUMENT)
    file_id = struct.unpack_from("<H", f)[0]
    sim = fw._sim()
    data = {
        0x2FE2: lambda: ef_iccid(sim.iccid),
        0x6F07: lambda: ef_imsi(sim.imsi),
        0x6FAD: lambda: ef_ad(len(sim.mcc_mnc) - 3),
        0x6F46: lambda: ef_spn(sim.carrier),
    }.get(file_id)
    if data is None:
        raise QmiFail(ERR_SIM_FILE_NOT_FOUND, {0x10: bytes([0x6A, 0x82])})
    content = data()
    off, n = struct.unpack("<HH", req.tlvs.get(0x03, b"\0\0\0\0"))
    content = content[off : off + n] if n else content[off:]
    return {0x10: bytes([0x90, 0x00]), 0x11: struct.pack("<H", len(content)) + content}


# WDS
def wds_packet_service_status(fw, req):
    return {0x01: _u8(2 if req.client in fw.calls and fw.modem.data_up else 1)}


def wds_start_network(fw, req):
    m = fw.modem
    if not m.data_up:
        raise QmiFail(ERR_CALL_FAILED, {0x10: struct.pack("<H", 1)})  # unspecified
    if req.client in fw.calls:
        raise QmiFail(ERR_NO_EFFECT, {0x01: _u32(fw.calls[req.client].handle)})
    fw._next_handle += 1
    fw.calls[req.client] = _WdsCall(fw._next_handle)
    return {0x01: _u32(fw._next_handle)}


def wds_stop_network(fw, req):
    if fw.calls.pop(req.client, None) is None:
        raise QmiFail(ERR_OUT_OF_CALL)
    return {}


def wds_current_settings(fw, req):
    if req.client not in fw.calls:
        raise QmiFail(ERR_OUT_OF_CALL)
    b = fw.bearer
    return {
        0x1E: _ip(b["address"]), 0x20: _ip(b["gateway"]), 0x21: _ip(b["netmask"]),
        0x15: _ip(b["dns1"]), 0x16: _ip(b["dns2"]), 0x29: _u32(int(b["mtu"])), 0x2B: _u8(4),
    }


def wds_profile_list(_fw, _req):
    name = b"internet"
    return {0x01: bytes([1, 0, 1, len(name)]) + name}


def wds_profile_settings(fw, req):
    if req.tlvs.get(0x01, b"\0\0")[1] != 1:
        raise QmiFail(ERR_INVALID_PROFILE)
    return dict(fw.profile1)


def wds_modify_profile(fw, req):
    """Store what the host writes to profile 1 (ModemManager reads it back to check)."""
    if req.tlvs.get(0x01, b"\0\0")[1] != 1:
        raise QmiFail(ERR_INVALID_PROFILE)
    fw.profile1.update({t: v for t, v in req.tlvs.items() if t != 0x01})
    return {}


def wds_default_profile(_fw, _req):
    return {0x01: _u8(1)}


def wds_max_attach_pdns(_fw, _req):
    return {0x10: _u8(1)}


def wds_get_attach_pdns(fw, _req):
    """LTE attach PDN list: ModemManager reads it to find the initial EPS bearer's profile."""
    return {0x10: bytes([len(fw.attach_pdns)]) + b"".join(struct.pack("<H", p) for p in fw.attach_pdns),
            0x11: bytes([0])}


def wds_set_attach_pdns(fw, req):
    raw = req.tlvs.get(0x01, b"")
    if not raw or len(raw) != 1 + 2 * raw[0] or not raw[0]:
        raise QmiFail(ERR_INVALID_ARGUMENT)
    ids = [struct.unpack_from("<H", raw, 1 + 2 * i)[0] for i in range(raw[0])]
    if any(i != 1 for i in ids):  # only profile 1 exists
        raise QmiFail(ERR_INVALID_PROFILE)
    fw.attach_pdns = ids
    return {}


def wds_autoconnect(_fw, _req):
    return {0x01: _u8(0)}


# WDA
def wda_get_format(fw, _req):
    return {0x10: _u8(0), 0x11: _u32(fw.link_layer), 0x12: _u32(0), 0x13: _u32(0)}


def wda_set_format(fw, req):
    if 0x11 in req.tlvs:
        llp = struct.unpack("<I", req.tlvs[0x11])[0]
        if llp not in (1, 2):
            raise QmiFail(ERR_INVALID_ARGUMENT)
        fw.link_layer = llp
    return wda_get_format(fw, req)


_HANDLERS: dict[tuple[int, int], Callable[[QmiFirmware, QmiMessage], dict[int, bytes]]] = {
    (CTL, 0x0021): ctl_version_info,
    (CTL, 0x0022): ctl_allocate_cid,
    (CTL, 0x0023): ctl_release_cid,
    (CTL, 0x0027): _ok,  # Sync
    (DMS, 0x0001): _ok,  # Set Event Report
    (DMS, 0x0020): dms_capabilities,
    (DMS, 0x0021): dms_manufacturer,
    (DMS, 0x0022): dms_model,
    (DMS, 0x0023): dms_revision,
    (DMS, 0x0024): dms_msisdn,
    (DMS, 0x0025): dms_ids,
    (DMS, 0x0026): dms_power_state,
    (DMS, 0x002C): dms_revision,  # Get Hardware Revision
    (DMS, 0x002D): dms_get_operating_mode,
    (DMS, 0x002E): dms_set_operating_mode,
    (DMS, 0x003C): dms_iccid,
    (DMS, 0x0043): dms_imsi,
    (DMS, 0x0045): dms_band_caps,
    (DMS, 0x0051): dms_software_version,
    # DMS UIM Get PIN Status (0x002B) is left out on purpose: like current
    # firmware, InvalidQmiCommand moves ModemManager to the UIM service.
    (NAS, 0x0002): _ok,  # Set Event Report
    (NAS, 0x0003): _ok,  # Register Indications
    (NAS, 0x0020): nas_signal_strength,
    (NAS, 0x0022): _ok,  # Initiate Network Register
    (NAS, 0x0024): nas_serving_system,
    (NAS, 0x0025): nas_home_network,
    (NAS, 0x0033): _ok,  # Set System Selection Preference
    (NAS, 0x0034): nas_ssp,
    (UIM, 0x0020): uim_read_transparent,
    (UIM, 0x002A): _ok,  # Refresh Register
    (UIM, 0x002C): _ok,  # Refresh Complete
    (UIM, 0x002E): _ok,  # Register Events
    (UIM, 0x002F): uim_card_status,
    (UIM, 0x0044): _ok,  # Refresh Register All
    (UIM, 0x0046): uim_switch_slot,
    (UIM, 0x0047): uim_slot_status,
    (WDS, 0x0001): _ok,  # Set Event Report
    (WDS, 0x0003): _ok,  # Indication Register
    (WDS, 0x0020): wds_start_network,
    (WDS, 0x0021): wds_stop_network,
    (WDS, 0x0022): wds_packet_service_status,
    (WDS, 0x002A): wds_profile_list,
    (WDS, 0x002B): wds_profile_settings,
    (WDS, 0x002D): wds_current_settings,
    (WDS, 0x0028): wds_modify_profile,
    (WDS, 0x0034): wds_autoconnect,
    (WDS, 0x0049): wds_default_profile,
    (WDS, 0x004D): _ok,  # Set IP Family
    (WDS, 0x0051): _ok,  # Set Autoconnect Settings
    (WDS, 0x0092): wds_max_attach_pdns,
    (WDS, 0x0093): wds_set_attach_pdns,
    (WDS, 0x0094): wds_get_attach_pdns,
    (WDS, 0x00A2): _ok,  # Bind Mux Data Port
    (WDA, 0x0020): wda_set_format,
    (WDA, 0x0021): wda_get_format,
}
