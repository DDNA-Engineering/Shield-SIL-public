"""Wi-Fi: Shield's AP and a Wi-Fi client device that joins it.

The SIL presents simulated radios (mac80211_hwsim): radio 0 is the Shield AP
(hostapd, WPA2, bridged into br-lan as on Shield), radio 1 is a separate Wi-Fi
client in its own network namespace inside the guest. On OpenWrt the AP is
configured through UCI (/etc/config/wireless); on Ubuntu hostapd runs it
(shield-wifi-ap.service). The radio driver is the
standard Linux Wi-Fi stack, not Shield's NXP driver; see docs/guest-interfaces.md.
"""

import json

import pytest

from shield_sil.netlab import INTERNET_HOST
from shield_sil.sil import wait_until

pytestmark = pytest.mark.board_interfaces

AP = "mwiphy0-ap0"


def client(shield, cmd, **kw):
    return shield.sh(f"ip netns exec wificlient {cmd}", check=False, **kw)


def client_ip(shield):
    out = client(shield, "ip -color=never -4 -o addr show dev wlan0")
    parts = out.split()
    return parts[3].split("/")[0] if len(parts) > 3 and parts[3].startswith("192.168.100.") else None


def test_ap_is_up_and_bridged_into_lan(shield):
    info = wait_until(lambda: (o := shield.sh(f"iw dev {AP} info", check=False)) and "type AP" in o and o,
                      120, interval=3, what="Shield AP")
    assert "ssid Shield" in info
    assert AP in shield.sh("ls /sys/class/net/br-lan/brif")


def test_client_joins_with_wpa2_and_gets_a_lan_lease(shield):
    ip = wait_until(lambda: client_ip(shield), 180, interval=3, what="Wi-Fi client lease")
    link = client(shield, "iw dev wlan0 link")
    assert "Connected to" in link and "SSID: Shield" in link
    assert "Station" in shield.sh(f"iw dev {AP} station dump")
    if shield.guest.kind == "openwrt":
        assert shield.sh("uci get wireless.ap0.encryption") == "psk2"
    else:
        conf = shield.sh("cat /etc/hostapd/shield-ap.conf")
        assert "wpa=2" in conf.splitlines() and "wpa_key_mgmt=WPA-PSK" in conf.splitlines()
    assert ip.startswith("192.168.100.")


def test_client_reaches_internet_through_shield(shield):
    wait_until(lambda: client_ip(shield), 180, interval=3, what="Wi-Fi client lease")

    def fetch():
        out = client(shield, f"wget -q -T 10 -O - http://{INTERNET_HOST}/", timeout=30)
        try:
            return json.loads(out)
        except ValueError:
            return None

    r = wait_until(fetch, 60, interval=3, what="internet from the Wi-Fi client")
    assert r["client_ip"].startswith("203.0.113.")  # NAT on the Shield, out of the wired WAN


def wifi(shield, action):
    """OpenWrt's `wifi down|up`; on Ubuntu, stop or start the AP service."""
    if shield.guest.kind == "openwrt":
        shield.sh(f"wifi {action}", timeout=120)
    else:
        shield.sh(f"systemctl {'stop' if action == 'down' else 'start'} shield-wifi-ap", timeout=120)


def test_wifi_down_disconnects_client_and_up_reconnects(shield):
    wait_until(lambda: client_ip(shield), 180, interval=3, what="Wi-Fi client lease")
    wifi(shield, "down")
    try:
        wait_until(lambda: "Not connected" in client(shield, "iw dev wlan0 link"), 60, interval=2,
                   what="client disconnected")
    finally:
        wifi(shield, "up")
    wait_until(lambda: "Connected to" in client(shield, "iw dev wlan0 link"), 180, interval=3,
               what="client reconnected")
