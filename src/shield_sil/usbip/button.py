"""Shield's front-panel button as a USB HID device (upstream ``usbhid``).

On Shield the button is a GPIO line handled by the ``gpio-keys`` driver, which
reports ``BTN_0`` on an input device; OpenWrt's ``button-hotplug`` turns that
into ``/etc/rc.button/BTN_0`` calls with ``ACTION=pressed|released`` and
``SEEN`` (seconds held). A QEMU ``virt`` guest has no way to add a gpio-keys
device without a device tree, so the SIL presents the same input event from a
one-button HID device: a Button-page usage 1 under a non-pointer application
collection, which the kernel's ``hid-input`` maps to ``BTN_0``
(``BTN_MISC`` + 0). Everything above the input layer is the same as on Shield.
"""

from __future__ import annotations

import struct
import threading

from .device import EP_INTERRUPT, Endpoint, Interface, Setup, Stall, UsbDevice

EP_IN = 0x81
DT_HID, DT_REPORT = 0x21, 0x22
REQ_GET_REPORT, REQ_GET_IDLE, REQ_GET_PROTOCOL = 0x01, 0x02, 0x03
REQ_SET_REPORT, REQ_SET_IDLE, REQ_SET_PROTOCOL = 0x09, 0x0A, 0x0B

REPORT_DESCRIPTOR = bytes([
    0x05, 0x01,        # Usage Page (Generic Desktop)
    0x09, 0x07,        # Usage (Keypad): not mouse/joystick/gamepad -> BTN_MISC range
    0xA1, 0x01,        # Collection (Application)
    0x05, 0x09,        #   Usage Page (Button)
    0x19, 0x01,        #   Usage Minimum (1)
    0x29, 0x01,        #   Usage Maximum (1)
    0x15, 0x00,        #   Logical Minimum (0)
    0x25, 0x01,        #   Logical Maximum (1)
    0x75, 0x01,        #   Report Size (1)
    0x95, 0x01,        #   Report Count (1)
    0x81, 0x02,        #   Input (Data, Variable, Absolute)
    0x75, 0x07,        #   Report Size (7)
    0x95, 0x01,        #   Report Count (1)
    0x81, 0x03,        #   Input (Constant): padding to a byte
    0xC0,              # End Collection
])


class ButtonHid(UsbDevice):
    vendor_id = 0x1209   # pid.codes test VID: not a real product's identity
    product_id = 0x0001
    bcd_device = 0x0100
    manufacturer = "Shield SIL"
    product = "Front-panel button"
    serial = "shield-button"

    def __init__(self) -> None:
        super().__init__()
        hid = struct.pack("<BBHBBBH", 9, DT_HID, 0x0111, 0, 1, DT_REPORT, len(REPORT_DESCRIPTOR))
        self.interfaces = [Interface(0, 0x03, 0, 0, [Endpoint(EP_IN, EP_INTERRUPT, 8, 4)],
                                     extra=hid)]
        self.pressed = False
        self.events = 0
        self._idle = 0
        self._release_timer: threading.Timer | None = None

    def report(self) -> bytes:
        return bytes([1 if self.pressed else 0])

    # --- harness side ---------------------------------------------------
    def set_pressed(self, pressed: bool) -> None:
        with self.lock:
            if pressed == self.pressed:
                return
            self.pressed = pressed
            self.events += 1
            if self.configuration:
                self.send_in(EP_IN, self.report())

    def press(self, seconds: float) -> None:
        """Press now and release after ``seconds`` (non-blocking)."""
        with self.lock:
            if self._release_timer:
                self._release_timer.cancel()
            self.set_pressed(True)
            self._release_timer = threading.Timer(seconds, self.set_pressed, args=(False,))
            self._release_timer.daemon = True
            self._release_timer.start()

    # --- USB ------------------------------------------------------------
    def control(self, setup: Setup, data: bytes) -> bytes:
        # Standard GET_DESCRIPTOR addressed to the interface: HID and report descriptors.
        if setup.kind == 0 and setup.request == 0x06 and setup.is_in and (setup.request_type & 0x1F) == 1:
            dtype = setup.value >> 8
            if dtype == DT_REPORT:
                return REPORT_DESCRIPTOR
            if dtype == DT_HID:
                return self.interfaces[0].extra
            raise Stall
        return super().control(setup, data)

    def class_request(self, setup: Setup, data: bytes) -> bytes:
        if setup.kind != 1:
            raise Stall
        if setup.request == REQ_GET_REPORT:
            return self.report()
        if setup.request == REQ_GET_IDLE:
            return bytes([self._idle])
        if setup.request == REQ_GET_PROTOCOL:
            return b"\x01"
        if setup.request == REQ_SET_IDLE:
            self._idle = setup.value >> 8
            return b""
        if setup.request in (REQ_SET_REPORT, REQ_SET_PROTOCOL):
            return b""
        raise Stall

    def close(self) -> None:
        if self._release_timer:
            self._release_timer.cancel()

