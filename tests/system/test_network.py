"""LAN service, uplinks and WAN failover."""

import pytest

from shield_sil.sil import wait_until


def via(shield, prefix):
    def check():
        r = shield.internet_get()
        return r if r and r["client_ip"].startswith(prefix) else None
    return check


def lan_gw(shield):
    return shield.profile["network"]["lan_ip"]


def test_lan_gateway_answers(shield):
    assert shield.lan("ping", "-c", "2", "-W", "2", lan_gw(shield)).returncode == 0


@pytest.mark.guest("openwrt")
def test_web_ui_login_page(shield):
    r = shield.lan("curl", "-sS", "--max-time", "30", f"http://{lan_gw(shield)}/cgi-bin/luci/", timeout=40)
    # Unauthenticated LuCI answers with its login form (HTTP 403).
    assert 'name="luci_username"' in r.stdout, r.stderr


@pytest.mark.guest("openwrt")
def test_hostname(shield):
    assert shield.sh("uci get system.@system[0].hostname") == "Shield"


def test_both_uplinks_get_addresses(shield):
    """Wired WAN by DHCP; cellular from the modem's bearer (or DHCP on the stand-in NIC)."""
    assert shield.ipv4("wan").startswith("203.0.113.")
    assert shield.ipv4("wwan").startswith(shield.cell_prefix)


def test_cellular_runs_through_the_modem(shield):
    """On images with the emulated EM7595, wanb runs through ModemManager on wwan0, not a stand-in NIC."""
    if shield.cellular_mode != "modem":
        pytest.skip("image uses the stand-in cellular NIC (network.cellular = 'nic')")
    if shield.guest.kind == "openwrt":
        status = shield.sh("ifstatus wanb")
        assert '"proto": "modemmanager"' in status and '"l3_device": "wwan0"' in status
    else:
        # NetworkManager's gsm profile, through ModemManager, with data on wwan0.
        active = shield.sh("nmcli -c no -t -f NAME,TYPE connection show --active")
        assert "wanb:gsm" in active.splitlines(), active
        assert shield.ipv4("wwan").startswith(shield.cell_prefix)
    assert "EM7595" in shield.sh("mmcli -L")
    # No stand-in cellular NIC (MAC from NetLab's "wwan" port plan).
    assert "52:54:00:5e:00:12" not in shield.sh("cat /sys/class/net/*/address")


def test_internet_prefers_wired_wan(shield):
    r = wait_until(shield.internet_get, 60, interval=2, what="internet")
    assert r["client_ip"].startswith("203.0.113.")


def test_failover_to_cellular_on_cable_pull_and_back(shield):
    wait_until(via(shield, "203.0.113."), 60, interval=2, what="wired path")
    shield.unplug("wan")
    wait_until(via(shield, shield.cell_prefix), 60, interval=2, what="failover to cellular")
    shield.plug("wan")
    wait_until(via(shield, "203.0.113."), 90, interval=2, what="fail back to wired")


def test_no_internet_when_both_uplinks_down(shield):
    shield.unplug("wan")
    with shield.board.lock:
        shield.board.modem.set_service(False)
    wait_until(lambda: shield.internet_get(timeout=3) is None, 60, interval=2, what="loss of internet")


def test_failover_on_upstream_outage_without_carrier_loss(shield):
    """mwan3 health checks move traffic to wanb when wan keeps carrier but loses upstream.

    With mwan3's upstream defaults (the SIL's mwan3 config) wan is declared down after
    5 failed rounds; a round pings the 4 tracked resolvers (4 s timeout each) and then
    waits 10 s, so detection takes up to ~2.5 min.
    """
    if shield.sh("test -x /etc/init.d/mwan3 && echo y", check=False) != "y":
        pytest.skip("image has no mwan3 (only carrier-loss failover)")
    wait_until(via(shield, "203.0.113."), 60, interval=2, what="wired path")
    shield.net.wires["wan"].cut()
    wait_until(via(shield, shield.cell_prefix), 240, interval=3, what="failover to cellular")
