"""Peripherals as seen by the software under test, through the hardware bus."""

import pytest

from shield_sil.sil import wait_until

pytestmark = pytest.mark.hw_agent


def reading(shield, dev, pred, timeout=30, what=None):
    return wait_until(lambda: (lambda r: r if pred(r) else None)(shield.hw(dev)), timeout,
                      interval=1, what=what or f"{dev} reading")


@pytest.mark.guest("openwrt")  # uses ubus
def test_ubus_exposes_all_devices(shield):
    import json

    status = json.loads(shield.sh("ubus call shield status"))
    assert set(status) == {"bms", "pd", "thermal", "modem", "leds"}
    assert status["pd"]["contract_v"] == 20.0


def test_battery_discharges_when_pd_unplugged(shield):
    reading(shield, "bms", lambda r: r["charging"])
    with shield.board.lock:
        shield.board.pd.detach()
    r = reading(shield, "bms", lambda r: r["current_a"] < 0 and not r["charging"])
    assert r["input_power_w"] == 0
    assert shield.hw("pd")["attached"] is False


def test_charging_resumes_on_pd_replug_at_9v(shield):
    with shield.board.lock:
        shield.board.battery.soc = 0.5
        shield.board.pd.detach()
    reading(shield, "bms", lambda r: not r["charging"])
    with shield.board.lock:
        shield.board.pd.attach([(5.0, 3.0), (9.0, 2.0)])
    reading(shield, "pd", lambda r: r["contract_v"] == 9.0)
    reading(shield, "bms", lambda r: r["charging"] and r["current_a"] > 0.9)


@pytest.mark.guest("openwrt")  # uses ubus
def test_guest_led_command_reaches_hardware(shield):
    shield.sh("ubus call shield set_led '{\"name\": \"Battery\", \"state\": \"blink_fast\"}'")
    wait_until(lambda: shield.board.leds.state["Battery"] == "blink_fast", 10, what="LED on host")


@pytest.mark.guest("openwrt")  # uses ubus
def test_bad_guest_command_is_rejected_not_fatal(shield):
    shield.sh("shield-hw set led '{\"name\": \"Toaster\", \"state\": \"on\"}'")
    wait_until(lambda: any("Toaster" in e for e in shield.bus.errors), 10, what="bus error recorded")
    shield.bus.errors.clear()
    shield.sh("ubus call shield set_led '{\"name\": \"User\", \"state\": \"on\"}'")
    wait_until(lambda: shield.board.leds.state["User"] == "on", 10, what="bus still working")


@pytest.mark.guest("openwrt")  # uses ubus
def test_modem_wedge_drops_cellular_until_power_cycle(shield):
    shield.unplug("wan")
    wait_until(lambda: (r := shield.internet_get()) and r["client_ip"].startswith(shield.cell_prefix),
               60, interval=2, what="cellular path")
    with shield.board.lock:
        shield.board.modem.wedge()
    reading(shield, "modem", lambda r: r["responsive"] is False)
    wait_until(lambda: shield.internet_get(timeout=3) is None, 60, interval=2, what="cellular loss")
    # A soft reset from the guest must not recover a wedged modem.
    shield.sh("ubus call shield modem_reset")
    reading(shield, "modem", lambda r: r["state"] == "unresponsive")
    with shield.board.lock:
        shield.board.modem.power(False)
        shield.board.modem.power(True)
    reading(shield, "modem", lambda r: r["state"] == "connected", timeout=60)
    wait_until(lambda: shield.internet_get(timeout=5), 120, interval=3, what="cellular recovery")


def test_sim_slot_switch_to_empty_slot(shield):
    shield.sh("shield-hw set modem '{\"action\":\"select_slot\",\"slot\":\"nano2\"}'")
    reading(shield, "modem", lambda r: r["state"] == "no_sim" and r["data_up"] is False,
            timeout=60)
