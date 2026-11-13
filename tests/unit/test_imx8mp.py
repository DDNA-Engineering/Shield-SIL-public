"""The i.MX 8M Plus machine: pin map, QEMU command, and the I2C/GPIO bridge peers."""

import re
import socket
import time

from shield_sil import imx8mp
from shield_sil.peripherals import Board
from shield_sil.peripherals.charger import TPS25751_ADDR
from shield_sil.peripherals.gpio import SHIELD_LINES
from shield_sil.profile import REPO_ROOT, Profile
from shield_sil.sil import ShieldSIL

DTS = (REPO_ROOT / "image" / "dts" / "shield-sil.dts").read_text()


def _imx_profile(tmp_path) -> Profile:
    for n in ("bl31.bin", "u-boot.bin", "emmc.img"):
        (tmp_path / n).write_bytes(b"x")
    return Profile.load(overrides={
        "image.name": "", "image.machine": imx8mp.MACHINE, "image.storage": "emmc",
        "image.qemu": "/opt/qemu-sil/bin/qemu-system-aarch64",
        "image.tfa": str(tmp_path / "bl31.bin"), "image.firmware": str(tmp_path / "u-boot.bin"),
        "image.disk": str(tmp_path / "emmc.img"), "image.kernel": "",
    })


def test_pin_map_matches_the_device_tree():
    for name, (ctrl, pin) in imx8mp.LED_PINS.items():
        pol = "LOW" if name in imx8mp.LED_ACTIVE_LOW else "HIGH"
        assert re.search(rf'label = "{name}";\s*gpios = <&gpio{ctrl} {pin} GPIO_ACTIVE_{pol}>', DTS), name
    ctrl, pin = imx8mp.BUTTON_PIN
    assert re.search(rf"linux,code = <BTN_0>;\s*gpios = <&gpio{ctrl} {pin} GPIO_ACTIVE_LOW>", DTS)
    for name, (ctrl, pin) in imx8mp.LINE_PINS.items():
        names = re.search(rf"&gpio{ctrl} \{{\s*gpio-line-names =(.*?);", DTS, re.S).group(1)
        lines = re.findall(r'"([^"]*)"', names)
        assert len(lines) == 32 and lines[pin] == name, name
        # OpenWrt's gpio-export: /sys/class/gpio/<name>, with the line's direction and level.
        direction, level = SHIELD_LINES[name]
        prop = f"output = <{level}>" if direction == "out" else "input = <0>"
        assert re.search(rf'gpio-export,name = "{name}";\s*gpio-export,{prop};\s*'
                         rf"gpios = <&gpio{ctrl} {pin} GPIO_ACTIVE_HIGH>", DTS), name
    assert set(imx8mp.LINE_PINS) == set(SHIELD_LINES)
    pins = list(imx8mp.LED_PINS.values()) + [imx8mp.BUTTON_PIN] + list(imx8mp.LINE_PINS.values())
    assert len(set(pins)) == len(pins), "two functions on one pin"


def test_bridge_lines_are_numbered_in_map_order():
    pins = imx8mp.pins()
    assert [p.line for p in pins] == list(range(len(pins)))
    assert imx8mp.gpio_map().split(";")[0] == "1:12"
    assert pins[7].name == "BTN_0" and imx8mp.gpio_map().split(";")[7] == "5:4"


def test_qemu_command_for_the_soc(tmp_path):
    sil = ShieldSIL(profile=_imx_profile(tmp_path), run_dir=tmp_path)
    cmd = sil._qemu_cmd()
    s = " ".join(cmd)
    assert "imx8mp-evk,emmc-slot=3,emmc-boot-partition-size=4M" in s
    assert f"loader,file={tmp_path / 'bl31.bin'},addr=0x970000" in s
    assert f"loader,file={tmp_path / 'u-boot.bin'},addr=0x40200000" in s
    assert "loader,addr=0x970000,cpu-num=0" in s
    assert f"file={tmp_path / 'disk.qcow2'},if=sd,bus=2,format=qcow2" in s
    assert "-kernel" not in cmd and "-bios" not in cmd and "virtio-serial-pci,id=vser0" not in s
    # UART1 hardware state, UART2 console, UART3 commands
    serials = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-serial"]
    assert serials[0] == "chardev:hwstate" and serials[1].startswith("unix:") and serials[2] == "chardev:hwcmd"
    # LAN on the FEC, the other NICs e1000e behind the PCI bridge
    assert "nic,netdev=lan,macaddr=52:54:00:5e:00:10,model=imx.enet" in s
    assert "e1000e,netdev=wan,mac=52:54:00:5e:00:11,bus=silpcib,addr=0x11" in s
    assert "e1000e,netdev=silmgmt,mac=52:54:00:5e:00:1f,bus=silpcib,addr=0x1f" in s
    assert "pci-bridge,id=silpcib,chassis_nr=1,bus=dw-pcie,addr=0" in s
    assert f"sil-i2c-bridge,bus=i2c-bus.1,address={TPS25751_ADDR:#x}" in s
    assert f"sil-gpio-bridge,chardev=silgpio,map={imx8mp.gpio_map()}" in s


def test_only_the_modem_is_on_usbip_with_soc_peripherals(tmp_path):
    board = Board(Profile.load())
    assert Board.BUSID_I2C1 in board.usbip_busids()
    board.soc_peripherals = True
    board.reset()
    assert board.soc_peripherals
    assert not set(board.usbip_busids()) & {Board.BUSID_I2C0, Board.BUSID_I2C1, Board.BUSID_BUTTON}
    assert set(board.usbip_busids()) <= {Board.BUSID_MODEM}


def _connect(path) -> socket.socket:
    for _ in range(50):
        try:
            c = socket.socket(socket.AF_UNIX)
            c.connect(str(path))
            return c
        except (FileNotFoundError, ConnectionRefusedError):
            time.sleep(0.02)
    raise AssertionError("bridge did not listen")


def _xfer(c: socket.socket, op: str, arg: int) -> int:
    c.sendall(bytes([ord(op), arg]))
    return c.recv(1)[0]


def test_i2c_bridge_reads_a_tps25751_register_byte_by_byte(tmp_path):
    board = Board(Profile.load())
    bridge = imx8mp.I2cBridge(board, tmp_path / "i2c.sock")
    try:
        c = _connect(tmp_path / "i2c.sock")
        assert _xfer(c, "S", 0x7A) == 1  # nobody there: NACK
        assert _xfer(c, "P", 0) == 0
        # MODE register (0x03): write the pointer, repeated start, read 5 bytes
        assert _xfer(c, "S", TPS25751_ADDR) == 0
        assert _xfer(c, "W", 0x03) == 0
        assert _xfer(c, "R", TPS25751_ADDR) == 0
        got = bytes(_xfer(c, "B", 0) for _ in range(5))
        _xfer(c, "N", 0)
        _xfer(c, "P", 0)
        expected = board.i2c_transfer(1, TPS25751_ADDR, b"\x03", 5)
        assert got == expected and got[0] == 4
        log = board.i2c[1].log
        assert ("w", TPS25751_ADDR, b"\x03") in log
        assert ("r", TPS25751_ADDR, expected) in log  # what the guest read, not the read-ahead
        c.close()
    finally:
        bridge.close()


def _msgs(c: socket.socket, n: int) -> list[tuple[str, int, int]]:
    out = []
    c.settimeout(2)
    while len(out) < n:
        m = c.recv(3, socket.MSG_WAITALL)
        out.append((chr(m[0]), m[1], m[2]))
    return out


def test_gpio_bridge_leds_lines_and_button(tmp_path):
    board = Board(Profile.load())
    bridge = imx8mp.GpioBridge(board, tmp_path / "gpio.sock", now=lambda: 1.0)
    try:
        c = _connect(tmp_path / "gpio.sock")
        pins = {p.name: p.line for p in imx8mp.pins()}
        # On connect: every named line (outputs at their pull) and the released button
        first = _msgs(c, 9)
        assert ("I", pins["BTN_0"], 1) in first
        assert ("I", pins["shield:pgood:dc-efuse"], 1) in first
        assert ("I", pins["shield:cell:full-card-power"], 1) in first
        assert ("I", pins["shield:cell:sim-select"], 0) in first
        # The guest lights an LED and drives the SIM select line
        c.sendall(bytes([ord("O"), pins["WAN"], 1, ord("O"), pins["shield:cell:sim-select"], 1]))
        for _ in range(100):
            if board.leds.state["WAN"] == "on" and board.gpio.values["shield:cell:sim-select"] == 1:
                break
            time.sleep(0.01)
        assert board.leds.state["WAN"] == "on"
        assert board.gpio.values["shield:cell:sim-select"] == 1
        # System is active low, as on Shield: a low pin lights it
        c.sendall(bytes([ord("O"), pins["System"], 0]))
        for _ in range(100):
            if board.leds.state["System"] == "on":
                break
            time.sleep(0.01)
        assert board.leds.state["System"] == "on"
        c.sendall(bytes([ord("O"), pins["System"], 1]))
        for _ in range(100):
            if board.leds.state["System"] == "off":
                break
            time.sleep(0.01)
        assert board.leds.state["System"] == "off"
        # Released (reset): back to the line default
        c.sendall(bytes([ord("O"), pins["shield:cell:sim-select"], imx8mp.RELEASED]))
        for _ in range(100):
            if board.gpio.values["shield:cell:sim-select"] == 0:
                break
            time.sleep(0.01)
        assert board.gpio.values["shield:cell:sim-select"] == 0
        # Button: low while pressed, high again after
        bridge.press(0.05)
        assert _msgs(c, 2) == [("I", pins["BTN_0"], 0), ("I", pins["BTN_0"], 1)]
        # A model input change reaches the pin on the next tick
        board.pd.detach()
        board.step(0.1)
        bridge.sync_inputs()
        assert _msgs(c, 1) == [("I", pins["shield:pgood:dc-efuse"], 0)]
        c.close()
    finally:
        bridge.close()
