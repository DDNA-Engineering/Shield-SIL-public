"""EM7595 model: USB identity, AT port, Shield's SIM routing and injected faults.

The slot-switch tests drive the modem the way host software can: set GPIO
``shield:cell:sim-select``, ask for the primary slot over QMI (as
``mmcli --set-primary-sim-slot`` does), and fall back to ``AT!UIMS`` on ttyUSB2
when the firmware answers ``Internal``.
"""

import pytest

from shield_sil.peripherals.modem import (
    USB_ID_FASTBOOT,
    USB_ID_NORMAL,
    Modem,
    ModemGone,
    ModemState,
    ModemTimeout,
    QmiError,
    Sim,
)


def make(**kw):
    args = {"model": "Sierra Wireless EM7595", "firmware": "SWIX55C_00.00.00.00",
            "slots": ["nano1", "nano2", "esim"], "active_slot": "nano1",
            "registration_time_s": 8.0, "boot_time_s": 20.0}
    args.update(kw)
    return Modem(**args)


def run(m, seconds):
    for _ in range(int(seconds)):
        m.step(1.0)


@pytest.fixture
def modem():
    m = make()
    m.sims["esim"] = Sim(present=True, carrier="eSIM-Carrier", msin="0000000003")
    run(m, 30)
    assert m.state is ModemState.CONNECTED
    return m


def switch_sim(m, target):
    """Host-side SIM switch: sim-select GPIO, QMI primary slot, AT!UIMS fallback."""
    mm_slot, gpio = {"sim1": (1, 0), "sim2": (2, None), "esim": (1, 1)}[target]
    if gpio is not None:
        m.set_sim_select_gpio(gpio)
    try:
        m.qmi_switch_slot(mm_slot)
    except QmiError as e:
        if e.name == "NoEffect":
            return "already"
        if e.name != "Internal" or "EM7595" not in m.model:
            raise
        assert m.at(f"AT!UIMS={mm_slot - 1}").endswith("OK")
        run(m, 30)
        try:
            m.qmi_switch_slot(mm_slot)
        except QmiError as e2:
            if not (e2.name == "Internal" and m.qmi_get_slot_status()["active_slot"] == mm_slot):
                raise
        return "fallback"
    return "qmi"


def test_usb_identity_follows_boot():
    m = make()
    assert m.usb_id is None and m.interfaces == {}
    run(m, 19)
    assert m.usb_id is None
    run(m, 1)
    assert m.usb_id == USB_ID_NORMAL
    assert m.interfaces["at"] == "ttyUSB2" and m.interfaces["net"] == "wwan0"
    assert m.enumerations == 1


def test_at_identity_and_registration(modem):
    assert modem.at("AT") == "OK"
    assert "Model: EM7595" in modem.at("ATI")
    assert modem.at("AT+CGMR").startswith("SWIX55C_00.00.00.00")
    imei = modem.at("AT+CGSN").split()[0]
    assert len(imei) == 15 and imei.isdigit()
    assert modem.at("AT+CPIN?").startswith("+CPIN: READY")
    assert modem.at("AT+CIMI").startswith("00101")
    assert modem.at("AT+CEREG?").startswith("+CEREG: 0,1")
    assert '"SIL-Carrier"' in modem.at("AT+COPS?")
    assert "REGISTERED" in modem.at("AT!GSTATUS?")
    assert modem.at("AT!UIMS?").startswith("!UIMS: 0")
    assert modem.at("AT+BOGUS") == "ERROR"


def test_iccid_is_luhn_valid(modem):
    iccid = modem.at("AT!ICCID?").split()[1]
    digits = [int(d) for d in iccid]
    total = sum(d if i % 2 == 0 else (d * 2 - 9 if d * 2 > 9 else d * 2)
                for i, d in enumerate(reversed(digits)))
    assert total % 10 == 0 and iccid.startswith("89")


def test_cfun_radio_off_and_back(modem):
    assert modem.at("AT+CFUN=4") == "OK"
    run(modem, 1)
    assert modem.state is ModemState.RADIO_OFF and not modem.data_up
    assert modem.at("AT+CEREG?").startswith("+CEREG: 0,0")
    modem.at("AT+CFUN=1")
    run(modem, 10)
    assert modem.state is ModemState.CONNECTED


def test_empty_slot_reports_sim_not_inserted(modem):
    switch_sim(modem, "sim2")
    run(modem, 30)
    assert modem.state is ModemState.NO_SIM
    assert modem.active_slot == "nano2"
    assert modem.at("AT+CPIN?") == "+CME ERROR: 10"


def test_sim2_switch_reenumerates(modem):
    modem.insert_sim("nano2", carrier="Other")
    run(modem, 30)
    before = modem.enumerations
    assert switch_sim(modem, "sim2") == "qmi"
    assert modem.usb_id is None  # gone from USB while it restarts
    run(modem, 30)
    assert modem.enumerations == before + 1
    assert modem.state is ModemState.CONNECTED and modem.active_sim.carrier == "Other"
    assert modem.at("AT!UIMS?").startswith("!UIMS: 1")


def test_gpio_swaps_card_behind_slot_1(modem):
    n = modem.enumerations
    assert switch_sim(modem, "esim") == "already"
    assert modem.active_slot == "esim" and modem.state is ModemState.SEARCHING
    assert modem.usb_id == USB_ID_NORMAL  # a card change, not a restart
    run(modem, 10)
    assert modem.enumerations == n
    assert modem.active_sim.carrier == "eSIM-Carrier" and modem.data_up
    # On slot 2 the GPIO does nothing visible.
    switch_sim(modem, "sim2")
    run(modem, 30)
    n = modem.enumerations
    modem.set_sim_select_gpio(0)
    run(modem, 30)
    assert modem.enumerations == n and modem.active_slot == "nano2"


def test_stuck_nv_slot_needs_at_fallback(modem):
    modem.faults.qmi_slot_stuck = True
    modem.insert_sim("nano2")
    run(modem, 30)
    with pytest.raises(QmiError, match="Internal"):
        modem.qmi_switch_slot(2)
    assert switch_sim(modem, "sim2") == "fallback"
    assert modem.qmi_get_slot_status()["active_slot"] == 2
    run(modem, 30)
    assert modem.active_slot == "nano2" and modem.data_up


def test_slot_setting_survives_power_cycle(modem):
    modem.at("AT!UIMS=1")
    modem.power(False)
    modem.power(True)
    run(modem, 30)
    assert modem.at("AT!UIMS?").startswith("!UIMS: 1")


def test_fastboot_shows_c082_until_cold_boot(modem):
    assert modem.at("AT!BOOTHOLD") == "OK"
    run(modem, 30)
    assert modem.state is ModemState.FASTBOOT
    assert modem.usb_id == USB_ID_FASTBOOT
    assert "at" not in modem.interfaces and "qmi" not in modem.interfaces
    with pytest.raises(ModemGone):
        modem.at("AT")
    modem.reset()  # dctrl restart / soft reset: no effect
    run(modem, 30)
    assert modem.state is ModemState.FASTBOOT
    modem.power(False)  # cell rfkill soft=1, then 0
    modem.power(True)
    run(modem, 30)
    assert modem.usb_id == USB_ID_NORMAL and modem.data_up


def test_fastboot_fault_on_next_boot(modem):
    modem.faults.fastboot_on_next_boot = True
    modem.at("AT+CFUN=1,1")
    run(modem, 30)
    assert modem.state is ModemState.FASTBOOT
    assert modem.faults.fastboot_on_next_boot is False


def test_wedge_times_out_on_every_port(modem):
    modem.wedge()
    assert modem.usb_id == USB_ID_NORMAL  # still enumerated
    with pytest.raises(ModemTimeout):
        modem.at("AT")
    with pytest.raises(ModemTimeout):
        modem.qmi_get_slot_status()


def test_rejects_unknown_sim_holder():
    with pytest.raises(ValueError):
        make(slots=["nano1", "sim3"])
