"""USB-PD controller (TI TPS25751) and battery charger (TI BQ25792) register models.

On Shield the guest never talks to the BQ25792 directly. The charger sits on
the TPS25751's private I2C bus, and Linux reaches it through the TPS25751 host
interface on ``/dev/i2c-1`` at 0x20, using the ``I2Cr``/``I2Cw`` 4CC proxy tasks
(TI TPS25751 technical reference manual). Register behaviour follows the TI
TPS25751 and BQ25792 datasheets:

TPS25751 host interface (``i2c-dev`` plain write/read, no repeated start):

* register read: write ``[reg]``, then read ``n + 1`` bytes. Byte 0 is the
  register's byte count, the rest is the register contents (multi-byte values
  little-endian).
* register write: write ``[reg, n, data...]``.
* ``MODE`` (0x03) reads ``"APP "`` when the PD firmware is running.
* ``CMD1`` (0x08) takes a 4CC task; it reads back 0 when the task is done, or
  ``"!CMD"`` when the task is unknown.
* ``DATA1`` (0x09) carries task input and output. ``I2Cr`` input is
  ``[slave, reg, len]`` and output is ``[result, data...]`` (result 0 = OK).
  ``I2Cw`` input is ``[slave, len + 1, reg, data...]``.

BQ25792 registers modelled:

* 0x1B ``CHARGER_STATUS_0``: bit 0 VBUS present, bit 1 AC1, bit 2 AC2,
  bit 3 power good.
* 0x1C ``CHARGER_STATUS_1``: bits 7:5 ``CHG_STAT``.
* 0x1D ``CHARGER_STATUS_2``: bit 0 ``VBAT_PRESENT_STAT``.
* 0x2E ``ADC_CONTROL``: writing bit 7 (``ADC_EN``) with bit 6 (one-shot) starts
  a conversion; the chip clears ``ADC_EN`` when it is done.
* 0x33 ``IBAT_ADC`` (signed mA) and 0x3B ``VBAT_ADC`` (mV), both MSB first.
* 0x48 ``PART_INFORMATION``: 0x08 for a BQ25792.

Faults can be injected (see ``ChargerFaults``).
"""

from __future__ import annotations

import struct
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

TPS25751_ADDR = 0x20
BQ25792_ADDR = 0x6B

TPS_REG_MODE = 0x03
TPS_REG_CMD1 = 0x08
TPS_REG_DATA1 = 0x09
TPS_MODE_APP = b"APP "
TPS_MODE_BOOT = b"BOOT"
TPS_CMD_UNKNOWN = b"!CMD"
TPS_4CC_I2CR = b"I2Cr"
TPS_4CC_I2CW = b"I2Cw"
TPS_DATA1_LEN = 64

BQ_REG_STATUS0 = 0x1B
BQ_REG_STATUS1 = 0x1C
BQ_REG_STATUS2 = 0x1D
BQ_REG_ADC_CTRL = 0x2E
BQ_REG_IBAT = 0x33
BQ_REG_VBAT = 0x3B
BQ_REG_PART_INFO = 0x48
BQ_PART_INFO_BQ25792 = 0x08
BQ_ADC_EN = 0x80

# CHG_STAT values (REG1C bits 7:5).
CHG_NOT_CHARGING = 0
CHG_TRICKLE = 1
CHG_PRECHARGE = 2
CHG_FAST_CC = 3
CHG_TAPER_CV = 4
CHG_TOPOFF = 6
CHG_DONE = 7


class I2cError(OSError):
    """A transfer the real bus would fail (NACK, timeout)."""


class I2cDevice(Protocol):
    def i2c_write(self, data: bytes) -> None: ...

    def i2c_read(self, n: int) -> bytes: ...


class I2cBus:
    """One Linux I2C adapter (``/dev/i2c-N``) with devices by 7-bit address."""

    def __init__(self, number: int) -> None:
        self.number = number
        self.devices: dict[int, I2cDevice] = {}
        self.log: list[tuple[str, int, bytes]] = []
        self.log_limit = 1000

    def attach(self, addr: int, dev: I2cDevice) -> None:
        self.devices[addr] = dev

    def _dev(self, addr: int) -> I2cDevice:
        dev = self.devices.get(addr)
        if dev is None:
            raise I2cError(f"i2c-{self.number}: no ACK from 0x{addr:02x}")
        return dev

    def _record(self, op: str, addr: int, data: bytes) -> None:
        self.log.append((op, addr, data))
        if len(self.log) > self.log_limit:
            del self.log[: len(self.log) - self.log_limit]

    def write(self, addr: int, data: bytes) -> None:
        self._dev(addr).i2c_write(bytes(data))
        self._record("w", addr, bytes(data))

    def read(self, addr: int, n: int) -> bytes:
        out = self._dev(addr).i2c_read(n)
        self._record("r", addr, out)
        return out

    # Byte-level access, for a bus whose master reports each event (QEMU's
    # sil-i2c-bridge on the i.MX 8M Plus machine).
    def ack(self, addr: int) -> bool:
        """Address phase: does a device answer at ``addr``?"""
        try:
            self._dev(addr).i2c_write(b"")
        except I2cError:
            return False
        return True

    def read_ahead(self, addr: int, n: int) -> bytes:
        """Up to ``n`` bytes of the next read, not logged (see ``record_read``).

        Device reads depend only on their register pointer, so the guest's
        actual (shorter) read is a prefix of this."""
        return self._dev(addr).i2c_read(n)

    def record_read(self, addr: int, data: bytes) -> None:
        self._record("r", addr, bytes(data))


@dataclass
class ChargerFaults:
    """Faults to inject from tests.

    ``vbat_glitches``: VBAT values (mV) returned by the next conversions instead
    of the true reading, oldest first (for example 0 or 0x0800 from a bad
    transfer).
    ``adc_stuck``: one-shot conversions never finish (ADC_EN stays set).
    ``cmd_stuck``: TPS25751 4CC tasks never finish (CMD1 keeps the task).
    ``proxy_fail``: TPS25751 reports a failed proxy task (result byte != 0).
    ``tps_mode``: what MODE reads; ``"BOOT"`` models PD firmware not running.
    ``tps_absent`` / ``bq_absent``: the device does not ACK.
    """

    vbat_glitches: list[int] = field(default_factory=list)
    adc_stuck: bool = False
    cmd_stuck: bool = False
    proxy_fail: bool = False
    tps_mode: bytes = TPS_MODE_APP
    tps_absent: bool = False
    bq_absent: bool = False


@dataclass
class ChargerInputs:
    """Analog state the BQ25792 measures, refreshed from the board model."""

    vbus_present: bool = False
    battery_present: bool = True
    vbat_mv: int = 0
    ibat_ma: int = 0
    chg_stat: int = CHG_NOT_CHARGING


class Bq25792:
    """Register model of the BQ25792 buck-boost charger."""

    def __init__(self, inputs: Callable[[], ChargerInputs], faults: ChargerFaults) -> None:
        self._inputs = inputs
        self.faults = faults
        self.regs = bytearray(0x49)
        self.regs[BQ_REG_PART_INFO] = BQ_PART_INFO_BQ25792
        self.conversions = 0
        self._adc_vbat = 0
        self._adc_ibat = 0

    def _refresh_status(self) -> None:
        s = self._inputs()
        self.regs[BQ_REG_STATUS0] = 0x0F if s.vbus_present else 0x00
        self.regs[BQ_REG_STATUS1] = (s.chg_stat & 0x7) << 5
        self.regs[BQ_REG_STATUS2] = 0x01 if s.battery_present else 0x00

    def _convert(self) -> None:
        s = self._inputs()
        self.conversions += 1
        if self.faults.vbat_glitches:
            self._adc_vbat = self.faults.vbat_glitches.pop(0) & 0xFFFF
        else:
            self._adc_vbat = max(s.vbat_mv, 0) if s.battery_present else 0
        self._adc_ibat = s.ibat_ma

    def read_regs(self, reg: int, n: int) -> bytes:
        if self.faults.bq_absent:
            raise I2cError("BQ25792 does not ACK")
        self._refresh_status()
        regs = bytearray(self.regs)
        struct.pack_into(">H", regs, BQ_REG_VBAT, self._adc_vbat)
        struct.pack_into(">h", regs, BQ_REG_IBAT, max(min(self._adc_ibat, 32767), -32768))
        out = bytes(regs[reg : reg + n])
        if len(out) != n:
            raise I2cError(f"BQ25792 read past register map (0x{reg:02x}+{n})")
        if reg <= BQ_REG_ADC_CTRL < reg + n and not self.faults.adc_stuck:
            # A one-shot conversion finishes well inside the firmware's 5 ms poll.
            self.regs[BQ_REG_ADC_CTRL] &= ~BQ_ADC_EN & 0xFF
        return out

    def write_regs(self, reg: int, data: bytes) -> None:
        if self.faults.bq_absent:
            raise I2cError("BQ25792 does not ACK")
        for i, b in enumerate(data):
            r = reg + i
            if r >= len(self.regs) or r == BQ_REG_PART_INFO:
                continue
            self.regs[r] = b
            if r == BQ_REG_ADC_CTRL and b & BQ_ADC_EN:
                self._convert()


class Tps25751:
    """Host-interface model of the TPS25751 with the BQ25792 behind its proxy."""

    def __init__(self, charger: Bq25792, faults: ChargerFaults) -> None:
        self.charger = charger
        self.faults = faults
        self._ptr = 0
        self._cmd1 = b"\x00\x00\x00\x00"
        self._data1 = bytearray(TPS_DATA1_LEN)
        self.tasks: list[str] = []

    # -- i2c-dev side ----------------------------------------------------
    def i2c_write(self, data: bytes) -> None:
        if self.faults.tps_absent:
            raise I2cError("TPS25751 does not ACK")
        if not data:
            return
        self._ptr = data[0]
        if len(data) == 1:
            return  # register pointer only, a read follows
        n = data[1]
        payload = data[2 : 2 + n]
        if len(payload) != n:
            raise I2cError(f"TPS25751 write to 0x{self._ptr:02x}: count {n}, got {len(payload)}")
        if self._ptr == TPS_REG_CMD1:
            self._cmd1 = payload[:4].ljust(4, b"\x00")
            self._run_task()
        elif self._ptr == TPS_REG_DATA1:
            self._data1 = bytearray(payload.ljust(TPS_DATA1_LEN, b"\x00")[:TPS_DATA1_LEN])
        # Other registers are accepted and ignored, like read-only ones on the part.

    def i2c_read(self, n: int) -> bytes:
        if self.faults.tps_absent:
            raise I2cError("TPS25751 does not ACK")
        if n < 1:
            return b""
        value = self._register(self._ptr)
        body = value.ljust(n - 1, b"\x00")[: n - 1]
        return bytes([len(value)]) + body

    # -- internals -------------------------------------------------------
    def _register(self, reg: int) -> bytes:
        if reg == TPS_REG_MODE:
            return self.faults.tps_mode
        if reg == TPS_REG_CMD1:
            return self._cmd1
        if reg == TPS_REG_DATA1:
            return bytes(self._data1)
        return b"\x00\x00\x00\x00"

    def _run_task(self) -> None:
        cmd = self._cmd1
        if cmd == b"\x00\x00\x00\x00":
            return
        self.tasks.append(cmd.decode("latin-1"))
        if self.faults.cmd_stuck:
            return  # CMD1 keeps reading the task: the host times out
        if cmd == TPS_4CC_I2CR:
            self._task_i2cr()
        elif cmd == TPS_4CC_I2CW:
            self._task_i2cw()
        else:
            self._cmd1 = TPS_CMD_UNKNOWN
            return
        self._cmd1 = b"\x00\x00\x00\x00"

    def _result(self, ok: bool, data: bytes = b"") -> None:
        code = 0 if ok and not self.faults.proxy_fail else 1
        self._data1 = bytearray((bytes([code]) + data).ljust(TPS_DATA1_LEN, b"\x00"))

    def _task_i2cr(self) -> None:
        slave, reg, n = self._data1[0], self._data1[1], self._data1[2]
        if slave != BQ25792_ADDR or n == 0:
            self._result(False)
            return
        try:
            self._result(True, self.charger.read_regs(reg, n))
        except I2cError:
            self._result(False)

    def _task_i2cw(self) -> None:
        slave, n = self._data1[0], self._data1[1]
        if slave != BQ25792_ADDR or n < 2:
            self._result(False)
            return
        reg = self._data1[2]
        try:
            self.charger.write_regs(reg, bytes(self._data1[3 : 2 + n]))
            self._result(True)
        except I2cError:
            self._result(False)
