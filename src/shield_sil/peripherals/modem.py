"""Sierra Wireless EM7595 cellular module, as Shield wires it.

SIM routing on Shield:

* The EM7595 has two UIM slots. ModemManager numbers them 1 and 2; Sierra's
  ``AT!UIMS`` numbers them 0 and 1.
* Modem slot 1 is muxed by GPIO ``shield:cell:sim-select``: 0 routes physical
  SIM1 (``nano1``), 1 routes the onboard eSIM (``esim``).
* Modem slot 2 is physical SIM2 (``nano2``).
* Changing the slot, or the card behind slot 1, restarts the modem and it
  re-enumerates on USB (ModemManager gives it a new index).

USB identity: ``1199:c081`` with QMI (``cdc-wdm0``/``wwan0``) and serial ports,
AT on ``ttyUSB2``. In fastboot the module shows as ``1199:c082`` with one vendor
interface and nothing else; only a cold boot (cell rfkill or card power)
recovers it.

Faults, settable through ``ModemFaults``:

* ``qmi_slot_stuck``: the firmware's persistent slot setting is stuck, so a QMI
  UIM slot switch (``mmcli --set-primary-sim-slot``) fails with ``Internal``.
  ``AT!UIMS=<n>`` (Sierra AT command reference) still works as a fallback.
* ``fastboot_on_next_boot``: the next boot lands in fastboot, as after an
  interrupted firmware update.
* ``wedge()``: the firmware stops answering on every port (still enumerated)
  until the module is power-cycled.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field

USB_ID_NORMAL = "1199:c081"
USB_ID_FASTBOOT = "1199:c082"
AT_PORT = "ttyUSB2"
MANUFACTURER = "Sierra Wireless, Incorporated"

# Shield SIM holders -> (modem slot 1-based, sim-select GPIO value or None).
SIM_ROUTES: dict[str, tuple[int, int | None]] = {
    "nano1": (1, 0),
    "esim": (1, 1),
    "nano2": (2, None),
}

# 3GPP 27.007 +CME ERROR codes used below.
CME_SIM_NOT_INSERTED = 10
CME_SIM_FAILURE = 13


class ModemState(str, enum.Enum):
    OFF = "off"
    BOOTING = "booting"
    FASTBOOT = "fastboot"
    RADIO_OFF = "radio_off"
    NO_SIM = "no_sim"
    SEARCHING = "searching"
    REGISTERED = "registered"
    CONNECTED = "connected"
    UNRESPONSIVE = "unresponsive"


class ModemGone(OSError):
    """The port does not exist right now (module off, booting or in fastboot)."""


class ModemTimeout(TimeoutError):
    """The port exists but the firmware does not answer (wedged)."""


class QmiError(Exception):
    """A QMI request failed with a protocol error (libqmi's name, e.g. ``Internal``)."""

    def __init__(self, name: str) -> None:
        super().__init__(f"QMI protocol error: {name}")
        self.name = name


def _luhn_complete(digits: str) -> str:
    total = 0
    for i, d in enumerate(reversed(digits)):
        n = int(d)
        if i % 2 == 0:
            n *= 2
            n -= 9 if n > 9 else 0
        total += n
    return digits + str((10 - total % 10) % 10)


@dataclass
class Sim:
    """A SIM card (or eSIM profile) in one of Shield's SIM holders.

    Defaults are test identities: MCC/MNC 001/01 is the 3GPP test network.
    """

    present: bool = True
    carrier: str = "SIL-Carrier"
    signal_dbm: float = -85.0  # RSRP
    service: bool = True
    mcc_mnc: str = "00101"
    msin: str = "0000000001"
    iccid: str = ""

    def __post_init__(self) -> None:
        if not self.iccid:
            self.iccid = _luhn_complete("89" + "001" + "01" + self.msin.rjust(12, "0")[-12:])

    @property
    def imsi(self) -> str:
        return self.mcc_mnc + self.msin


@dataclass
class ModemFaults:
    qmi_slot_stuck: bool = False
    fastboot_on_next_boot: bool = False


@dataclass
class Modem:
    """EM7595 state machine, AT port and the QMI requests Shield software depends on.

    The data path is "up" only in CONNECTED. The SIL ties the cellular wire
    and the guest's cellular NIC carrier to ``data_up``.
    """

    model: str
    firmware: str
    slots: list[str]
    active_slot: str
    registration_time_s: float
    boot_time_s: float = 20.0  # TBD: boot until USB re-enumeration, not measured
    imei: str = "35907010000001"
    powered: bool = True
    state: ModemState = ModemState.BOOTING
    sims: dict[str, Sim] = field(default_factory=dict)
    faults: ModemFaults = field(default_factory=ModemFaults)
    wedged: bool = False
    fastboot: bool = False
    # Persistent in modem NV (survives power cycles): selected UIM slot, 1-based.
    nv_slot: int = 1
    # Host side: GPIO shield:cell:sim-select (0 SIM1, 1 eSIM).
    sim_select_gpio: int = 0
    cfun: int = 1
    enumerations: int = 0
    _boot_timer: float = 0.0
    _reg_timer: float = 0.0
    _pending_enumeration: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        unknown = set(self.slots) - set(SIM_ROUTES)
        if unknown:
            raise ValueError(f"unknown SIM holders {sorted(unknown)}; Shield has {sorted(SIM_ROUTES)}")
        if self.active_slot not in self.slots:
            raise ValueError(f"active_slot {self.active_slot!r} not in {self.slots}")
        if len(self.imei) == 14:
            self.imei = _luhn_complete(self.imei)
        if not self.sims:
            self.sims = {
                s: Sim(present=(s == self.active_slot), msin=f"{i + 1:010d}")
                for i, s in enumerate(self.slots)
            }
        for s in self.slots:
            self.sims.setdefault(s, Sim(present=False))
        self.nv_slot, gpio = SIM_ROUTES[self.active_slot]
        if gpio is not None:
            self.sim_select_gpio = gpio
        self._begin_boot(count=True)

    # --- routing --------------------------------------------------------
    def _holder_for(self, modem_slot: int, gpio: int) -> str:
        if modem_slot == 2:
            return "nano2"
        return "esim" if gpio else "nano1"

    def _sync_active(self) -> None:
        holder = self._holder_for(self.nv_slot, self.sim_select_gpio)
        self.active_slot = holder

    @property
    def active_sim(self) -> Sim:
        return self.sims.get(self.active_slot) or Sim(present=False)

    @property
    def data_up(self) -> bool:
        return self.state is ModemState.CONNECTED

    @property
    def usb_id(self) -> str | None:
        """What ``lsusb`` shows, or None when the module is not on the bus."""
        if not self.powered or self.state is ModemState.BOOTING:
            return None
        return USB_ID_FASTBOOT if self.fastboot else USB_ID_NORMAL

    @property
    def interfaces(self) -> dict[str, str]:
        """Host-side nodes the module exposes right now."""
        uid = self.usb_id
        if uid == USB_ID_NORMAL:
            return {"qmi": "cdc-wdm0", "net": "wwan0", "dm": "ttyUSB0", "nmea": "ttyUSB1", "at": AT_PORT}
        if uid == USB_ID_FASTBOOT:
            return {"fastboot": "1199:c082"}
        return {}

    # --- controls (test harness) -----------------------------------------
    def power(self, on: bool) -> None:
        """Card power / cell rfkill. Off then on is a cold boot."""
        if on and not self.powered:
            self.powered = True
            self.wedged = False
            self.fastboot = False
            self.cfun = 1
            self._begin_boot(count=True)
        elif not on:
            self.powered = False
            self.state = ModemState.OFF

    def wedge(self) -> None:
        self.wedged = True
        self.state = ModemState.UNRESPONSIVE

    def set_signal(self, dbm: float, slot: str | None = None) -> None:
        self.sims[slot or self.active_slot].signal_dbm = dbm

    def set_service(self, available: bool, slot: str | None = None) -> None:
        self.sims[slot or self.active_slot].service = available

    def insert_sim(self, slot: str, carrier: str = "SIL-Carrier", **kw) -> None:
        self.sims[slot] = Sim(present=True, carrier=carrier, **kw)
        if slot == self.active_slot:
            self._restart()

    def remove_sim(self, slot: str) -> None:
        self.sims[slot] = Sim(present=False)

    # --- controls (guest) -------------------------------------------------
    def set_sim_select_gpio(self, value: int) -> None:
        """GPIO ``shield:cell:sim-select``. Swaps the card behind modem slot 1.

        The module sees a card change and registers again; it stays on USB.
        """
        value = 1 if value else 0
        if value == self.sim_select_gpio:
            return
        self.sim_select_gpio = value
        if self.nv_slot == 1:
            self._sync_active()
            self._reregister()

    def _reregister(self) -> None:
        if self.state in (ModemState.REGISTERED, ModemState.CONNECTED, ModemState.NO_SIM):
            self.state = ModemState.SEARCHING
        self._reg_timer = 0.0

    def select_slot(self, slot: str) -> None:
        """Route ``slot`` to the modem: sim-select GPIO first, then the UIM slot."""
        if slot not in self.slots:
            raise ValueError(f"unknown SIM slot {slot!r}; known: {self.slots}")
        if self.wedged or self.fastboot or not self.powered:
            return
        modem_slot, gpio = SIM_ROUTES[slot]
        if gpio is not None:
            self.set_sim_select_gpio(gpio)
        if modem_slot != self.nv_slot:
            self.nv_slot = modem_slot
            self._sync_active()
            self._restart()

    def reset(self) -> None:
        """Soft reset from the guest. Does not clear a wedge or fastboot."""
        if not self.wedged and not self.fastboot and self.powered:
            self._restart()

    def _restart(self) -> None:
        if not self.powered or self.wedged or self.fastboot:
            return
        self._begin_boot(count=True)

    def _begin_boot(self, count: bool) -> None:
        self._boot_timer = 0.0
        self._reg_timer = 0.0
        self.state = ModemState.BOOTING
        self._pending_enumeration = count

    # --- QMI (what ModemManager does over cdc-wdm0) -----------------------
    def _qmi_ready(self) -> None:
        if self.interfaces.get("qmi") is None:
            raise ModemGone("cdc-wdm0 not present")
        if self.wedged:
            raise ModemTimeout("QMI request timed out")

    def qmi_get_slot_status(self) -> dict:
        """UIM Get Slot Status: the active slot (1-based) and card presence per slot."""
        self._qmi_ready()
        return {
            "active_slot": self.nv_slot,
            "cards": {1: self.sims.get(self._holder_for(1, self.sim_select_gpio), Sim(present=False)).present,
                      2: self.sims.get("nano2", Sim(present=False)).present},
        }

    def qmi_switch_slot(self, modem_slot: int) -> None:
        """UIM Switch Slot (``mmcli --set-primary-sim-slot``)."""
        self._qmi_ready()
        if modem_slot not in (1, 2):
            raise QmiError("InvalidArgument")
        if self.faults.qmi_slot_stuck:
            raise QmiError("Internal")
        if modem_slot == self.nv_slot:
            raise QmiError("NoEffect")
        self.nv_slot = modem_slot
        self._sync_active()
        self._restart()

    # --- AT port (ttyUSB2) ------------------------------------------------
    def at(self, line: str) -> str:
        """Send one AT command line, return the response (final result included)."""
        if self.interfaces.get("at") is None:
            raise ModemGone(f"/dev/{AT_PORT} not present")
        if self.wedged:
            raise ModemTimeout("no response on AT port")
        cmd = line.strip().upper()
        if not cmd.startswith("AT"):
            return "ERROR"
        try:
            body = self._at(cmd[2:])
        except _AtCme as e:
            return f"+CME ERROR: {e.code}"
        except _AtError:
            return "ERROR"
        return (body + "\r\n\r\nOK") if body else "OK"

    def _sim_ready(self) -> Sim:
        sim = self.active_sim
        if not sim.present:
            raise _AtCme(CME_SIM_NOT_INSERTED)
        return sim

    def _reg_stat(self) -> int:
        if self.state in (ModemState.REGISTERED, ModemState.CONNECTED):
            return 1
        if self.state is ModemState.SEARCHING:
            return 2
        return 0

    def _at(self, c: str) -> str:
        if re.fullmatch(r"(?:[EVQ][01]?|Z|&F0?| )*", c) or c in ("+CMEE=1", "+CMEE=2"):
            return ""
        if c == "+GCAP":
            return "+GCAP: +CGSM"
        if c == "I":
            return (f"Manufacturer: {MANUFACTURER}\r\nModel: EM7595\r\n"
                    f"Revision: {self.firmware}\r\nIMEI: {self.imei}\r\n+GCAP: +CGSM")
        if c in ("+CGMI", "+GMI"):
            return MANUFACTURER
        if c in ("+CGMM", "+GMM"):
            return "EM7595"
        if c in ("+CGMR", "+GMR"):
            return self.firmware
        if c in ("+CGSN", "+GSN"):
            return self.imei
        if c == "+CIMI":
            return self._sim_ready().imsi
        if c in ("+ICCID", "!ICCID?"):
            prefix = "ICCID: " if c == "+ICCID" else "!ICCID: "
            return prefix + self._sim_ready().iccid
        if c == "+CPIN?":
            self._sim_ready()
            return "+CPIN: READY"
        if c == "+CSQ":
            return f"+CSQ: {self._csq()},99"
        m = re.fullmatch(r"\+(CREG|CGREG|CEREG)\?", c)
        if m:
            return f"+{m.group(1)}: 0,{self._reg_stat()}"
        if c == "+COPS?":
            if self._reg_stat() == 1:
                return f'+COPS: 0,0,"{self.active_sim.carrier}",7'
            return "+COPS: 0"
        if c == "+CFUN?":
            return f"+CFUN: {self.cfun}"
        m = re.fullmatch(r"\+CFUN=(\d)(?:,(\d))?", c)
        if m:
            fun, rst = int(m.group(1)), int(m.group(2) or 0)
            if fun not in (0, 1, 4) or rst not in (0, 1):
                raise _AtError
            self.cfun = fun
            if rst:
                self._restart()
            return ""
        if c == "!UIMS?":
            return f"!UIMS: {self.nv_slot - 1}"
        m = re.fullmatch(r"!UIMS=(\d+)", c)
        if m:
            n = int(m.group(1))
            if n not in (0, 1):
                raise _AtError
            changed = n + 1 != self.nv_slot
            self.nv_slot = n + 1
            self._sync_active()
            if changed:
                self._restart()
            return ""
        if c == "!GSTATUS?":
            return self._gstatus()
        if c == "!RESET":
            self._restart()
            return ""
        if c == "!BOOTHOLD":
            self.faults.fastboot_on_next_boot = True
            self._restart()
            return ""
        raise _AtError

    def _csq(self) -> int:
        sim = self.active_sim
        if self._reg_stat() == 0 or not sim.present:
            return 99
        # RSRP -> an RSSI-like 0..31 index; good enough for bars, not for RF work.
        return max(0, min(31, round((sim.signal_dbm + 140) / 2)))

    def _gstatus(self) -> str:
        sim = self.active_sim
        reg = {1: "REGISTERED", 2: "SEARCHING"}.get(self._reg_stat(), "NOT REGISTERED")
        mode = "ONLINE" if self.cfun == 1 else "LOW POWER MODE"
        rsrp = f"{sim.signal_dbm:.0f}" if sim.present and self._reg_stat() == 1 else "---"
        return (f"!GSTATUS: \r\nCurrent Time:  0\t\tTemperature: 38\r\n"
                f"Modem Mitigate Level: 0\t\tISIM Mitigate Level: 0\r\n"
                f"Mode:        {mode}\t\tSystem mode: LTE\r\n"
                f"EMM state:     {reg}\t\tNormal Service\r\n"
                f"PCC RxM RSRP:  {rsrp}\t\tPCC RxD RSRP:  {rsrp}")

    # --- simulation -------------------------------------------------------
    def step(self, dt: float) -> None:
        if not self.powered:
            self.state = ModemState.OFF
            return
        if self.wedged:
            self.state = ModemState.UNRESPONSIVE
            return
        if self.fastboot:
            self.state = ModemState.FASTBOOT
            return
        if self.state is ModemState.BOOTING:
            self._boot_timer += dt
            if self._boot_timer < self.boot_time_s:
                return
            if self._pending_enumeration:
                self.enumerations += 1
                self._pending_enumeration = False
            self.cfun = 1
            if self.faults.fastboot_on_next_boot:
                self.faults.fastboot_on_next_boot = False
                self.fastboot = True
                self.state = ModemState.FASTBOOT
                return
            self.state = ModemState.SEARCHING
        if self.cfun != 1:
            self._reg_timer = 0.0
            self.state = ModemState.RADIO_OFF
            return
        sim = self.active_sim
        if not sim.present:
            self._reg_timer = 0.0
            self.state = ModemState.NO_SIM
            return
        usable = sim.service and sim.signal_dbm > -120.0
        if not usable:
            self._reg_timer = 0.0
            self.state = ModemState.SEARCHING
            return
        if self.state in (ModemState.SEARCHING, ModemState.NO_SIM, ModemState.RADIO_OFF):
            self.state = ModemState.SEARCHING
            self._reg_timer += dt
            if self._reg_timer >= self.registration_time_s:
                self.state = ModemState.REGISTERED
        if self.state is ModemState.REGISTERED:
            self.state = ModemState.CONNECTED

    @property
    def power_w(self) -> float:
        return 0.0 if self.state is ModemState.OFF else 1.1

    def snapshot(self) -> dict:
        sim = self.active_sim
        responsive = self.state is not ModemState.UNRESPONSIVE
        live = sim.present and responsive and self._reg_stat() == 1
        return {
            "model": self.model,
            "firmware": self.firmware,
            "responsive": responsive,
            "state": self.state.value,
            "usb_id": self.usb_id,
            "enumerations": self.enumerations,
            "active_slot": self.active_slot,
            "modem_slot": self.nv_slot,
            "sim_select_gpio": self.sim_select_gpio,
            "sim_present": {k: v.present for k, v in self.sims.items()},
            "carrier": sim.carrier if live else "",
            "signal_dbm": sim.signal_dbm if (sim.present and responsive) else None,
            "data_up": self.data_up,
        }


class _AtError(Exception):
    pass


class _AtCme(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code
