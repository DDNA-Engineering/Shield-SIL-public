"""Shield's board interfaces as Linux driver interfaces inside the guest.

Shield firmware talks to LEDs, GPIOs, the modem and the battery bus through
standard kernel interfaces, so the SIL presents the same ones. On Shield's SoC
(the i.MX 8M Plus machine) they are the real drivers: gpio-leds, the SoC's
GPIO lines named by the device tree, gpio-keys and i2c-imx. On QEMU virt they
are stand-ins: uleds for /sys/class/leds/<name>, gpio-sim for the named GPIO
lines, and USB/IP I2C adapters. The modem is on USB/IP either way, over a
management link outside every network role.
"""

import pytest

from shield_sil.sil import wait_until

pytestmark = pytest.mark.board_interfaces

LEDS = ["Heartbeat", "System", "User", "WAN", "WiFi", "ETH", "Battery"]
GPIO_OUT = {
    "shield:cell:sim-select": 0,
    "shield:cell:full-card-power": 1,
    "shield:wifi:wake": 1,
    "shield:wifi:enabled": 1,
}
GPIO_IN = {
    "shield:pgood:5v-out-efuse": 1,
    "shield:pgood:dc-efuse": 1,
    "shield:pgood:batt-efuse": 1,
    "shield:pgood:3v3-out-efuse": 1,
}


def test_shield_kernel_modules_load(shield):
    mods = set(shield.sh("cat /proc/modules | cut -d' ' -f1").split())
    # Built into some kernels (Ubuntu builds i2c-dev in): listed in modules.builtin.
    builtin = {ln.rsplit("/", 1)[-1].removesuffix(".ko").replace("-", "_") for ln in
               shield.sh("cat /lib/modules/$(uname -r)/modules.builtin 2>/dev/null", check=False).split()}
    if shield.imx:
        drivers = ("leds_gpio", "gpio_keys", "e1000e", "vhci_hcd", "i2c_dev", "qcserial", "qmi_wwan", "cdc_wdm")
    else:
        drivers = ("gpio_sim", "uleds", "vhci_hcd", "i2c_dev", "i2c_tiny_usb", "qcserial", "qmi_wwan", "cdc_wdm")
    for m in drivers:
        assert m in mods or m in builtin, f"{m} not loaded"


def test_front_panel_leds_have_shield_names(shield):
    wait_until(lambda: all(shield.sh(f"test -e /sys/class/leds/{n} && echo y") == "y" for n in LEDS),
               30, what="all LEDs registered")


def test_led_triggers_work(shield):
    shield.sh("echo heartbeat > /sys/class/leds/Heartbeat/trigger")
    assert "[heartbeat]" in shield.sh("cat /sys/class/leds/Heartbeat/trigger")
    # gpio-leds (i.MX 8M Plus machine) has max_brightness 1, uleds 255.
    full = shield.sh("cat /sys/class/leds/User/max_brightness")
    shield.sh("echo none > /sys/class/leds/User/trigger; echo 255 > /sys/class/leds/User/brightness")
    assert shield.sh("cat /sys/class/leds/User/brightness") == full
    wait_until(lambda: shield.board.leds.state["User"] == "on", 10, what="LED on host")
    shield.sh("echo 0 > /sys/class/leds/User/brightness; echo none > /sys/class/leds/Heartbeat/trigger")


def _sysfs_gpios(shield) -> bool:
    """OpenWrt on Shield's SoC exports the named lines as /sys/class/gpio/<name>
    (gpio-export, as on Shield); they are then held by the kernel, not libgpiod."""
    return shield.imx and shield.guest.kind == "openwrt"


def _read_gpio(shield, name: str) -> str:
    if _sysfs_gpios(shield):
        return shield.sh(f"cat '/sys/class/gpio/{name}/value'", check=False)
    return shield.sh(f"gpioget --numeric '{name}'", check=False)


def test_gpio_lines_have_shield_names_and_levels(shield):
    for name, level in {**GPIO_OUT, **GPIO_IN}.items():
        out = wait_until(lambda: _read_gpio(shield, name) or None, 30, what=f"GPIO {name}")
        assert out.strip() == str(level), f"{name} reads {out!r}, expected {level}"


def test_guest_can_drive_gpio_outputs(shield):
    if _sysfs_gpios(shield):
        # Shield's interface: write the exported line's value file.
        shield.sh("echo 0 > /sys/class/gpio/shield:wifi:wake/value")
        wait_until(lambda: shield.board.gpio.values["shield:wifi:wake"] == 0, 10, what="wifi:wake low")
        shield.sh("echo 1 > /sys/class/gpio/shield:wifi:wake/value")
        wait_until(lambda: shield.board.gpio.values["shield:wifi:wake"] == 1, 10, what="wifi:wake high")
        return
    if shield.imx:
        # A real SoC GPIO: the level the guest drives reaches the board model.
        shield.sh("gpioset --toggle 0 shield:wifi:wake=0")
        wait_until(lambda: shield.board.gpio.values["shield:wifi:wake"] == 0, 10, what="wifi:wake low")
        shield.sh("gpioset --toggle 0 shield:wifi:wake=1")
        wait_until(lambda: shield.board.gpio.values["shield:wifi:wake"] == 1, 10, what="wifi:wake high")
        return
    if shield.guest.kind == "openwrt":
        # The SIL's gpio-sim keeps a released output at its last level, as a SoC does.
        shield.sh("gpioset --toggle 0 shield:wifi:wake=0")
        assert shield.sh("gpioget --as-is --numeric shield:wifi:wake") == "0"
        shield.sh("gpioset --toggle 0 shield:wifi:wake=1")
        return
    # Ubuntu's stock gpio-sim returns a released line to its pull, so hold the
    # line (as a service driving it would) and read the level the chip outputs.
    shield.sh("gpioset -z shield:wifi:wake=0")
    try:
        # gpiod 2: "gpiochip1 2 "shield:wifi:wake" output"
        chip, offset = shield.sh("gpioinfo shield:wifi:wake").split()[:2]
        value = shield.sh(f"cat /sys/devices/platform/gpio-sim.*/{chip}/sim_gpio{offset}/value")
        assert value == "0"
    finally:
        shield.sh("pkill -f 'gpioset -z shield:wifi:wake'", check=False)


MGMT_MAC = "52:54:00:5e:00:1f"


def test_management_link_is_outside_every_network_role(shield):
    mgmt = shield.sh(f"grep -l {MGMT_MAC} /sys/class/net/*/address | cut -d/ -f5 | head -n1")
    assert mgmt, "no management NIC"
    assert "10.0.2.15/24" in shield.sh(f"ip -color=never -4 addr show dev {mgmt}")
    assert mgmt not in shield.sh("ip -color=never route show default")
    assert mgmt not in shield.sh("ls /sys/class/net/br-lan/brif", check=False)
    if shield.guest.kind == "openwrt":
        assert shield.sh("uci get network.silmgmt.device") == mgmt
        assert "silmgmt" not in shield.sh("uci show firewall | grep '\\.network='", check=False)
        assert "silmgmt" not in shield.sh("uci show mwan3 2>/dev/null", check=False)
    else:
        # NetworkManager: static, never the default route, not the LAN.
        assert shield.sh("nmcli -c no -g ipv4.never-default connection show silmgmt") == "yes"


def test_usbip_agent_is_running(shield):
    # busybox ps (OpenWrt) lists every process; procps (Ubuntu) needs ax.
    ps = "ps w" if shield.guest.kind == "openwrt" else "ps axww"
    assert "shield-usbip" in shield.sh(f"{ps} | grep [s]hield-usbip")
