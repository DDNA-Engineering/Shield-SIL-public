import pytest

from shield_sil.peripherals import Board, ModemState
from shield_sil.peripherals.battery import CHARGE_VOLTAGE_V, GENERIC_1S_OCV, ocv
from shield_sil.peripherals.charger import CHG_DONE, CHG_FAST_CC, CHG_TAPER_CV
from shield_sil.profile import Profile


@pytest.fixture
def board():
    return Board(Profile.load())


def run(board, seconds, dt=1.0):
    for _ in range(int(seconds / dt)):
        board.step(dt)


def test_ocv_is_monotonic_and_follows_the_table():
    vals = [ocv(x / 100) for x in range(101)]
    assert vals == sorted(vals)
    for mv, pct in GENERIC_1S_OCV:
        assert ocv(pct / 100) == pytest.approx(mv / 1000)


def test_pd_negotiates_highest_common_voltage(board):
    board.pd.attach([(5.0, 3.0), (9.0, 3.0), (20.0, 3.25)])
    assert board.pd.contract_v == 20.0 and board.pd.contract_a == 3.25


def test_pd_falls_back_to_vsafe5v_without_common_pdo(board):
    board.pd.attach([(28.0, 5.0)])
    assert (board.pd.contract_v, board.pd.contract_a) == (5.0, 0.5)


def test_charges_on_pd_at_charge_current(board):
    board.battery.soc = 0.5
    run(board, 60)
    s = board.snapshot()["bms"]
    assert s["charging"] is True
    assert s["current_a"] == pytest.approx(1.0)
    assert s["soc_pct"] > 50.0


def test_discharges_on_battery_and_reports_input_power(board):
    board.pd.detach()
    board.battery.soc = 0.5
    run(board, 60)
    s = board.snapshot()["bms"]
    assert s["charging"] is False
    assert s["current_a"] < 0
    assert s["input_power_w"] == 0
    assert s["soc_pct"] < 50.0


def test_battery_runtime_matches_energy_budget(board):
    """Runtime to brownout should match the energy budget: capacity x ~3.8 V / (load / efficiency)."""
    board.pd.detach()
    board.battery.soc = 1.0
    hours = 0
    while not board.snapshot()["bms"]["brownout"] and hours < 10:
        run(board, 600, dt=10)
        hours += 600 / 3600
    p = board.profile
    budget_h = (p["battery"]["capacity_mah"] / 1000 * 3.8) / (
        p["power"]["idle_w_with_cell"] / p["power"]["regulator_efficiency"])
    assert 0.75 * budget_h < hours < 1.1 * budget_h
    # Brownout comes in the last few percent of charge.
    assert board.battery.soc < 0.08


def test_charger_runs_cc_then_cv_then_terminates(board):
    board.battery.soc = 0.5
    board.step(1.0)
    assert board.battery.chg_stat == CHG_FAST_CC
    board.battery.soc = 0.95
    board.step(1.0)
    assert board.battery.chg_stat == CHG_TAPER_CV
    assert board.battery.voltage <= CHARGE_VOLTAGE_V + 1e-9
    run(board, 4 * 3600, dt=10)
    assert board.battery.chg_stat == CHG_DONE
    assert board.snapshot()["bms"]["charging"] is False


def test_no_fan_and_no_battery_temperature(board):
    snap = board.snapshot()
    assert "fan" not in snap
    assert set(snap["thermal"]) == {"ambient_c", "board_c"}
    with pytest.raises(ValueError):
        board.guest_command("fan", {"duty_pct": 50})


def test_modem_boots_registers_then_connects(board):
    assert board.modem.state is ModemState.BOOTING and board.modem.usb_id is None
    run(board, 30)
    assert board.modem.state is ModemState.CONNECTED and board.modem.data_up


def test_modem_wedge_survives_soft_reset_but_not_power_cycle(board):
    run(board, 30)
    board.modem.wedge()
    board.guest_command("modem", {"action": "reset"})
    run(board, 30)
    assert board.modem.state is ModemState.UNRESPONSIVE
    assert board.snapshot()["modem"]["responsive"] is False
    board.modem.power(False)
    board.modem.power(True)
    run(board, 30)
    assert board.modem.state is ModemState.CONNECTED


def test_modem_switch_to_empty_slot_drops_data(board):
    run(board, 30)
    board.guest_command("modem", {"action": "select_slot", "slot": "nano2"})
    run(board, 30)
    assert board.modem.state is ModemState.NO_SIM and not board.modem.data_up


def test_modem_loses_service_on_weak_signal(board):
    run(board, 30)
    board.modem.set_signal(-125)
    board.step(1)
    assert board.modem.state is ModemState.SEARCHING


def test_leds_record_history(board):
    board.guest_command("led", {"name": "Battery", "state": "blink_slow"})
    board.guest_command("led", {"name": "Battery", "state": "blink_slow"})
    assert board.snapshot()["leds"]["Battery"] == "blink_slow"
    assert len(board.leds.history) == 1
    with pytest.raises(ValueError):
        board.guest_command("led", {"name": "NOPE", "state": "on"})


def test_reset_restores_defaults(board):
    board.pd.detach()
    board.modem.wedge()
    board.reset()
    assert board.pd.attached and board.modem.state is ModemState.BOOTING


def test_unknown_device_rejected(board):
    with pytest.raises(ValueError):
        board.guest_command("toaster", {})
