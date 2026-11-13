"""USB-C usb0: a laptop on Shield's USB-C port is on the LAN.

On Shield, usb0 is a USB network gadget bridged into br-lan. With
network.usbc = "gadget" the guest runs that gadget (configfs, CDC Ethernet) on
the USB/IP virtual device controller and the SIL's simulated laptop
(192.168.100.60) is the USB host; with network.usbc = "nic" usb0 is a plain
NIC to the same laptop. Either way, unplugging the cable is carrier loss.
"""

import json

import pytest

from shield_sil.netlab import INTERNET_HOST
from shield_sil.sil import wait_until


@pytest.fixture(autouse=True)
def _needs_usbc(shield):
    if shield.usbc_mode == "off":
        pytest.skip("image has no USB-C usb0 (network.usbc = 'off')")


def laptop(shield, *cmd, timeout=30):
    return shield.net.exec("usbhost", *cmd, timeout=timeout)


def test_usb0_is_a_usb_gadget_in_gadget_mode(shield):
    if shield.usbc_mode != "gadget":
        pytest.skip("usb0 is a plain NIC (network.usbc = 'nic')")
    g = "/sys/kernel/config/usb_gadget/shield"
    wait_until(lambda: shield.sh(f"cat {g}/UDC", check=False).startswith("usbip-vudc."), 60,
               interval=3, what="gadget bound to the USB/IP device controller")
    assert shield.sh(f"cat {g}/functions/ecm.usb0/ifname") == "usb0"
    assert shield.usbc_laptop.connected.wait(120), "laptop did not enumerate the gadget"


def test_usb0_is_in_the_lan_bridge(shield):
    wait_until(lambda: "usb0" in shield.sh("ls /sys/class/net/br-lan/brif", check=False), 60,
               interval=3, what="usb0 in br-lan")


def test_laptop_reaches_shield_and_internet(shield):
    gw = shield.profile["network"]["lan_ip"]
    wait_until(lambda: laptop(shield, "ping", "-c1", "-W2", gw).returncode == 0, 60, interval=2,
               what="laptop reaches Shield over USB-C")
    r = laptop(shield, "curl", "-sS", "--max-time", "10", f"http://{INTERNET_HOST}/", timeout=20)
    assert r.returncode == 0 and json.loads(r.stdout)["client_ip"].startswith("203.0.113.")


def test_unplug_and_replug_usbc(shield):
    gw = shield.profile["network"]["lan_ip"]
    wait_until(lambda: laptop(shield, "ping", "-c1", "-W2", gw).returncode == 0, 60, interval=2,
               what="laptop reaches Shield")
    shield.unplug("usb")
    wait_until(lambda: "NO-CARRIER" in shield.sh("ip link show usb0") or "DOWN" in shield.sh("ip link show usb0"),
               30, interval=1, what="usb0 carrier loss")
    assert laptop(shield, "ping", "-c1", "-W2", gw).returncode != 0
    shield.plug("usb")
    wait_until(lambda: laptop(shield, "ping", "-c1", "-W2", gw).returncode == 0, 60, interval=2,
               what="laptop back after replug")
