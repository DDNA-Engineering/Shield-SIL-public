"""TPS25751 + BQ25792 model, read the way Linux software reaches it on Shield.

``ChargerHostClient`` talks to the TPS25751 host interface on /dev/i2c-1 with
plain i2c-dev writes and reads, and reaches the BQ25792 through the TPS25751
``I2Cr``/``I2Cw`` 4CC proxy tasks, as the TI TPS25751 technical reference manual
and BQ25792 datasheet describe. Any guest program that uses the same interface
sees the same register values.
"""

import struct

import pytest

from shield_sil.peripherals import Board, I2cError
from shield_sil.peripherals.charger import TPS_MODE_BOOT
from shield_sil.profile import Profile

TPS, BQ = 0x20, 0x6B
REG_MODE, REG_CMD1, REG_DATA1 = 0x03, 0x08, 0x09
CHG_STAT_DONE = 7


class Timeout(Exception):
    pass


class ChargerHostClient:
    """One-shot i2c-dev transactions to the TPS25751, and through it to the BQ25792."""

    def __init__(self, board, bus=1, poll_limit=10):
        self.board, self.bus, self.poll_limit = board, bus, poll_limit

    def _xfer(self, write, read_len=0):
        return self.board.i2c_transfer(self.bus, TPS, bytes(write), read_len)

    def tps_read(self, reg, n):  # write [reg], read n+1, byte 0 is the length
        self._xfer([reg])
        return self._xfer(b"", n + 1)[1:]

    def tps_write(self, reg, data):  # [reg, len, data...]
        self._xfer(bytes([reg, len(data)]) + bytes(data))

    def wait_cmd(self):  # CMD1 reads 0 when the 4CC task is done
        for _ in range(self.poll_limit):
            if struct.unpack("<I", self.tps_read(REG_CMD1, 4))[0] == 0:
                return
        raise Timeout("CMD1 polling timed out")

    def bq_read(self, reg, n):  # I2Cr: DATA1 = [slave, reg, len] -> [result, data...]
        self.tps_write(REG_DATA1, [BQ, reg, n])
        self.tps_write(REG_CMD1, b"I2Cr")
        self.wait_cmd()
        rx = self.tps_read(REG_DATA1, n + 1)
        if rx[0]:
            raise I2cError("TPS task failed")
        return rx[1 : 1 + n]

    def bq_write(self, reg, data):  # I2Cw: DATA1 = [slave, len + 1, reg, data...]
        self.tps_write(REG_DATA1, bytes([BQ, len(data) + 1, reg]) + bytes(data))
        self.tps_write(REG_CMD1, b"I2Cw")
        self.wait_cmd()

    def check_ready(self):
        mode = self.tps_read(REG_MODE, 4)
        if mode != b"APP ":
            raise I2cError(f"TPS25751 not in application mode (Mode: {mode!r})")
        if self.bq_read(0x48, 1)[0] & 0x3F != 0x08:
            raise I2cError("BQ25792 ID mismatch")

    def vbat_once(self):  # one-shot ADC conversion, then VBAT_ADC
        self.bq_write(0x2E, [0xC0])
        for _ in range(self.poll_limit):
            if not self.bq_read(0x2E, 1)[0] & 0x80:
                break
        else:
            raise Timeout("ADC one-shot timed out")
        return struct.unpack(">H", self.bq_read(0x3B, 2))[0]

    def status(self):
        self.check_ready()
        present = bool(self.bq_read(0x1D, 1)[0] & 0x01)
        out = {
            "battery_present": present,
            "chg_stat": (self.bq_read(0x1C, 1)[0] & 0xE0) >> 5,
            "vbus_present": bool(self.bq_read(0x1B, 1)[0] & 0x01),
            "power_good": bool(self.bq_read(0x1B, 1)[0] & 0x08),
        }
        if present:
            out["vbat_mv"] = self.vbat_once()
        return out


@pytest.fixture
def board():
    return Board(Profile.load())


@pytest.fixture
def bm(board):
    return ChargerHostClient(board)


def test_status_on_pd_power_while_charging(board, bm):
    board.battery.soc = 0.5
    board.step(1.0)
    out = bm.status()
    assert out["battery_present"] and out["vbus_present"] and out["power_good"]
    assert out["chg_stat"] not in (0, CHG_STAT_DONE)
    assert out["vbat_mv"] == round(board.battery.voltage * 1000)


def test_status_on_battery(board, bm):
    board.pd.detach()
    board.battery.soc = 0.5
    board.step(1.0)
    out = bm.status()
    assert not out["vbus_present"] and not out["power_good"]
    assert out["chg_stat"] == 0
    assert out["vbat_mv"] == round(board.battery.voltage * 1000)


def test_charge_done_after_termination(board, bm):
    board.battery.soc = 0.999
    for _ in range(600):
        board.step(10.0)
    assert bm.status()["chg_stat"] == CHG_STAT_DONE


def test_no_battery_has_no_voltage(board, bm):
    board.battery.present = False
    board.step(1.0)
    out = bm.status()
    assert out["battery_present"] is False and "vbat_mv" not in out


def test_injected_vbat_glitches_come_first_then_clear(board, bm):
    board.pd.detach()
    board.step(1.0)
    board.charger_faults.vbat_glitches = [0x0800, 0]
    assert [bm.vbat_once() for _ in range(2)] == [0x0800, 0]
    assert bm.vbat_once() == round(board.battery.voltage * 1000)
    assert board.bq25792.conversions == 3


def test_pd_firmware_not_running(board, bm):
    board.charger_faults.tps_mode = TPS_MODE_BOOT
    with pytest.raises(I2cError, match="Mode"):
        bm.status()


def test_stuck_4cc_task_times_out(board, bm):
    board.charger_faults.cmd_stuck = True
    with pytest.raises(Timeout):
        bm.status()


def test_stuck_adc_times_out(board, bm):
    board.charger_faults.adc_stuck = True
    with pytest.raises(Timeout):
        bm.vbat_once()


def test_proxy_failure_and_absent_parts(board, bm):
    board.charger_faults.proxy_fail = True
    with pytest.raises(I2cError, match="task failed"):
        bm.status()
    board.charger_faults.proxy_fail = False
    board.charger_faults.tps_absent = True
    with pytest.raises(I2cError, match="no ACK|does not ACK"):
        bm.status()


def test_unknown_4cc_reads_back_bang_cmd(board, bm):
    bm.tps_write(REG_CMD1, b"XXXX")
    assert bm.tps_read(REG_CMD1, 4) == b"!CMD"


def test_nothing_else_on_the_bus(board):
    with pytest.raises(I2cError):
        board.i2c_transfer(1, 0x50, b"\x00", 1)
    with pytest.raises(ValueError):
        board.i2c_transfer(7, TPS, b"\x03", 0)
