"""Front-panel button: a press reaches the guest as the BTN_0 input event.

On Shield the button is gpio-keys BTN_0, and so it is on the i.MX 8M Plus
machine (GPIO1_IO01, src/shield_sil/imx8mp.py). On QEMU virt it is a one-button
USB HID device that the kernel maps to the same BTN_0 input event (see
src/shield_sil/usbip/button.py). On OpenWrt, /etc/rc.button/BTN_0 runs the same
way as on Shield; on Ubuntu the test reads the input device directly.
"""

import base64

import pytest

from shield_sil.sil import wait_until

pytestmark = pytest.mark.board_interfaces


def events(shield):
    out = shield.sh("cat /var/run/shield/button.log 2>/dev/null", check=False)
    return [ln.split() for ln in out.splitlines() if ln.strip()]


def test_button_is_an_input_device_with_btn_0(shield):
    out = wait_until(lambda: (o := shield.sh("cat /proc/bus/input/devices", check=False))
                     and "Front-panel button" in o and o, 120, interval=3, what="button input device")
    block = next(b for b in out.split("\n\n") if "Front-panel button" in b)
    # B: KEY bitmap includes BTN_0 (0x100): bit 0 of the 5th 64-bit word from the right.
    key = next(ln for ln in block.splitlines() if ln.startswith("B: KEY="))
    words = key.split("=", 1)[1].split()
    assert int(words[-5], 16) & 1 if len(words) >= 5 else False, key


@pytest.mark.guest("openwrt")
@pytest.mark.parametrize("seconds,seen_min,seen_max", [(0.4, 0, 0), (3.0, 2, 4)])
def test_press_runs_rc_button_with_hold_time(shield, seconds, seen_min, seen_max):
    shield.sh("rm -f /var/run/shield/button.log")
    shield.press_button(seconds)
    ev = wait_until(lambda: (e := events(shield)) and any(x[0] == "released" for x in e) and e,
                    seconds + 30, interval=1, what="button press and release")
    assert ev[0][0] == "pressed"
    seen = int(next(x for x in ev if x[0] == "released")[1])
    assert seen_min <= seen <= seen_max


# Reads BTN_0 (type EV_KEY=1, code 0x100) events from the button's evdev node:
# prints "<value> <timestamp>" per event (1 pressed, 0 released).
EVDEV_READER = r"""
import struct, sys
fmt = "llHHi"
size = struct.calcsize(fmt)
with open(sys.argv[1], "rb", buffering=0) as f:
    while True:
        sec, usec, typ, code, value = struct.unpack(fmt, f.read(size))
        if typ == 1 and code == 0x100:
            print(value, sec + usec / 1e6, flush=True)
"""


@pytest.mark.guest("linux")
def test_press_and_hold_reach_evdev_as_btn_0(shield):
    devices = wait_until(lambda: (o := shield.sh("cat /proc/bus/input/devices", check=False))
                         and "Front-panel button" in o and o, 120, interval=3, what="button input device")
    block = next(b for b in devices.split("\n\n") if "Front-panel button" in b)
    handlers = next(ln for ln in block.splitlines() if ln.startswith("H: Handlers="))
    event = next(h for h in handlers.split("=", 1)[1].split() if h.startswith("event"))
    b64 = base64.b64encode(EVDEV_READER.encode()).decode()
    shield.sh(f"echo {b64} | base64 -d > /tmp/btn-reader.py")
    shield.sh(f"rm -f /tmp/btn.log; setsid python3 /tmp/btn-reader.py /dev/input/{event} "
              "> /tmp/btn.log 2>&1 < /dev/null &")
    wait_until(lambda: shield.sh("pgrep -f btn-reader.py", check=False), 10, what="evdev reader running")
    try:
        shield.press_button(2.0)
        ev = wait_until(lambda: (e := [ln.split() for ln in shield.sh("cat /tmp/btn.log").splitlines()])
                        and any(x[0] == "0" for x in e) and e, 30, interval=1, what="press and release")
    finally:
        shield.sh("pkill -f btn-reader.py", check=False)
    assert ev[0][0] == "1"
    held = float(next(x for x in ev if x[0] == "0")[1]) - float(ev[0][1])
    assert 1.5 <= held <= 3.0, ev
