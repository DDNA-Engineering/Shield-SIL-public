"""Front-panel button HID device: descriptors and input reports."""

import struct
import time

from shield_sil.usbip.button import EP_IN, REPORT_DESCRIPTOR, ButtonHid
from shield_sil.usbip.device import Setup


def test_descriptors_describe_one_hid_button():
    dev = ButtonHid()
    cfg = dev.config_descriptor()
    assert b"\x09\x04\x00\x00\x01\x03\x00\x00" in cfg  # interface: 1 endpoint, class HID
    hid = dev.control(Setup(0x81, 0x06, 0x2100, 0, 9), b"")
    assert struct.unpack("<BBHBBBH", hid)[-1] == len(REPORT_DESCRIPTOR)
    assert dev.control(Setup(0x81, 0x06, 0x2200, 0, 255), b"") == REPORT_DESCRIPTOR
    # Button page, usage 1, under a Keypad application: hid-input maps it to BTN_0.
    assert bytes([0x05, 0x09, 0x19, 0x01, 0x29, 0x01]) in REPORT_DESCRIPTOR
    assert bytes([0x09, 0x07, 0xA1, 0x01]) in REPORT_DESCRIPTOR


def test_press_sends_pressed_then_released_reports():
    dev = ButtonHid()
    dev.control(Setup(0x00, 0x09, 1, 0, 0), b"")  # SET_CONFIGURATION 1
    got = []
    for seq in range(2):
        dev.submit_in(EP_IN, seq, 8, lambda st, data: got.append((st, data)))
    dev.press(0.2)
    time.sleep(0.4)
    assert got == [(0, b"\x01"), (0, b"\x00")]
    assert dev.events == 2
    assert dev.control(Setup(0xA1, 0x01, 0x0100, 0, 1), b"") == b"\x00"  # GET_REPORT
