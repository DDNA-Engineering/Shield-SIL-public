"""Orchestrates one emulated system: QEMU, network lab, hardware bus, CPU cap.

The image and the way to talk to it come from the profile (``[image]`` and
``[guest]``), so any image can be swapped in without code changes.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Callable, TypeVar

from . import hwbus, imx8mp
from .console import Console
from .cpucap import CpuCap
from .guest import Guest
from .netlab import INTERNET_HOST, INTERNET_MODES, NetLab
from .peripherals import Board
from .profile import REPO_ROOT, Profile
from .qmp import QMP
from .usbip.datapath import ImpairedDataPath, TunDataPath
from .usbip.ecm_host import EcmLaptop
from .usbip.shield import shield_server

T = TypeVar("T")

# Cellular link characteristics by signal strength (RSRP dBm): (delay ms, jitter ms, loss %).
# TBD: replace with drive-test data from the target carriers.
_CELL_LINK = [(-90, (35, 5, 0.0)), (-100, (45, 10, 0.5)), (-110, (70, 20, 2.0)), (-120, (120, 40, 8.0))]


def cell_link_for(signal_dbm: float) -> tuple[float, float, float]:
    for threshold, params in _CELL_LINK:
        if signal_dbm >= threshold:
            return params
    return _CELL_LINK[-1][1]


class SILError(RuntimeError):
    pass


class ShieldSIL:
    def __init__(
        self,
        profile: Profile | None = None,
        run_dir: Path | None = None,
        instance: int = 0,
        time_scale: float = 1.0,
    ) -> None:
        self.profile = profile or Profile.from_env()
        self.instance = instance
        self.run_dir = run_dir or REPO_ROOT / "build" / "runs" / f"i{instance}-{int(time.time())}"
        self.time_scale = time_scale
        self.guest = Guest.from_profile(self.profile)
        self.cellular_mode = self.profile["network"].get("cellular", "nic")
        if self.cellular_mode not in ("nic", "modem"):
            raise SILError(f"network.cellular must be 'nic' or 'modem', not {self.cellular_mode!r}")
        self.cell_data: ImpairedDataPath | None = None
        # ifindex of the guest's cellular netdev from before the last reset, until
        # the guest has dropped it (see reset_environment and uplinks_ready).
        self._stale_wwan: str | None = None
        self.usbc_mode = self.profile["network"].get("usbc", "off")
        if self.usbc_mode not in ("off", "nic", "gadget"):
            raise SILError(f"network.usbc must be 'off', 'nic' or 'gadget', not {self.usbc_mode!r}")
        self.usbc_laptop: EcmLaptop | None = None  # USB host for the gadget while running
        self.internet_mode = self.profile["network"].get("internet", "off")
        if self.internet_mode not in INTERNET_MODES:
            raise SILError(f"network.internet must be 'off' or 'host', not {self.internet_mode!r}")
        # Shield's SoC (QEMU imx8mp-evk, patched): real I2C, GPIO, LED and button
        # drivers in the guest, bridged to the board models (imx8mp.py).
        self.imx = imx8mp.is_imx8mp(self.profile["image"])
        self.board = Board(self.profile)
        self.board.soc_peripherals = self.imx
        self.i2c_bridge: imx8mp.I2cBridge | None = None
        self.gpio_bridge: imx8mp.GpioBridge | None = None
        net = self.profile["network"]
        self.net = NetLab(
            prefix=f"sil{instance}",
            base_port=47000 + 10 * instance,
            workdir=self.run_dir / "net",
            lan_gateway=net["lan_ip"],
            lan_prefix=net["lan_prefix"],
            nic_model=imx8mp.NIC_MODEL if self.imx else self.profile["image"].get("nic_model", "virtio-net-pci"),
            lan_onboard_nic="imx.enet" if self.imx else "",
            pci_bus=imx8mp.PCI_BRIDGE if self.imx else None,
            cellular_nic=self.cellular_mode == "nic",
            usbc_nic=self.usbc_mode == "nic",
            usbc_gadget=self.usbc_mode == "gadget",
            gadget_port=48000 + instance,
            internet=self.internet_mode == "host",
        )
        self.proc: subprocess.Popen | None = None
        self.qmp: QMP | None = None
        self.console: Console | None = None
        self.bus: hwbus.HardwareBus | None = None
        self.usbip = None  # UsbipServer while running
        self.cpucap: CpuCap | None = None
        self.speed: dict | None = None  # result of limit_speed()
        self._qmp_lock = threading.Lock()
        self._links = {"lan": True, "wan": True}
        if self.cellular_mode == "nic":
            self._links["wwan"] = True
        if self.usbc_mode in ("nic", "gadget"):
            self._links["usb"] = True
        self._cell_applied: tuple | None = None

    # ------------------------------------------------------------------
    def _qemu_cmd(self) -> list[str]:
        if self.imx:
            return self._qemu_cmd_imx8mp()
        c = self.profile["compute"]
        img = self.profile["image"]
        rd = self.run_dir
        boot: list[str] = []
        kernel = self.profile.image_path("kernel")
        for key, flag in (("kernel", "-kernel"), ("initrd", "-initrd"), ("firmware", "-bios")):
            path = self.profile.image_path(key)
            # A kernel given directly is booted directly: the catalog's U-Boot is
            # left out, so --set image.storage=virtio --kernel ... just works.
            if path and not (key == "firmware" and kernel):
                boot += [flag, str(path)]
        if self.profile.image_path("kernel") and img.get("append"):
            boot += ["-append", img["append"]]
        return [
            shutil.which(img["qemu"]) or img["qemu"],
            "-M", img["machine"],
            # Named threads ("CPU n/TCG") let the CPU cap find and limit each vCPU.
            "-name", "shield,debug-threads=on",
            "-cpu", c["cpu_model"],
            "-smp", str(c["cpu_cores"]),
            "-m", f"{c['ram_mb']}M",
            "-accel", "tcg,thread=multi",
            "-display", "none",
            "-monitor", "none",
            "-serial", f"unix:{rd / 'console.sock'},server=on,wait=off",
            "-qmp", f"unix:{rd / 'qmp.sock'},server=on,wait=off",
            *boot,
            *self._storage_args(),
            "-device", "virtio-rng-pci",
            "-rtc", "base=utc",
            *self.net.qemu_netdev_args(),
            *hwbus.qemu_args(rd / "hwstate.sock", rd / "hwcmd.sock"),
            *[str(a) for a in img.get("extra_args", [])],
        ]

    def _qemu_cmd_imx8mp(self) -> list[str]:
        """Shield's SoC: TF-A, U-Boot and the eMMC on uSDHC3; SoC I2C and GPIO
        bridged to the board models; hardware bus on UART1/UART3."""
        c = self.profile["compute"]
        img = self.profile["image"]
        rd = self.run_dir
        return [
            shutil.which(img["qemu"]) or img["qemu"],
            *imx8mp.qemu_machine_args(img, self.profile.image_path("tfa"),
                                      self.profile.image_path("firmware"), rd / "disk.qcow2"),
            "-name", "shield,debug-threads=on",
            "-smp", str(c["cpu_cores"]),
            "-m", f"{c['ram_mb']}M",
            "-accel", "tcg,thread=multi",
            "-display", "none",
            "-monitor", "none",
            *imx8mp.qemu_hwbus_serial_args(rd / "hwstate.sock", rd / "hwcmd.sock", rd / "console.sock"),
            "-qmp", f"unix:{rd / 'qmp.sock'},server=on,wait=off",
            "-rtc", "base=utc",
            *imx8mp.qemu_pci_bridge_args(),
            *self.net.qemu_netdev_args(),
            *imx8mp.qemu_bridge_args(self.board, rd / "i2c-bridge.sock", rd / "gpio-bridge.sock"),
            *[str(a) for a in img.get("extra_args", [])],
        ]

    def _storage_args(self) -> list[str]:
        """The disk, as a virtio block device or as Shield's eMMC.

        ``storage = "emmc"``: QEMU's eMMC (JEDEC, with boot0/boot1 hardware
        partitions) on an SDHCI controller, so the guest sees /dev/mmcblk0 with
        Shield's GPT, and U-Boot (image.firmware) boots the active A/B slot. The
        disk file is boot0 + boot1 + user area (image/builders/emmc.py).
        ``watchdog = true`` adds a hardware watchdog (i6300esb, /dev/watchdog)
        that resets the board when the guest stops feeding it.
        """
        img = self.profile["image"]
        disk = self.run_dir / "disk.qcow2"
        if img.get("storage", "virtio") == "emmc":
            boot = int(img.get("emmc_boot_mib", 4)) << 20
            args = ["-device", "sdhci-pci",
                    "-drive", f"file={disk},if=none,id=emmc,format=qcow2",
                    "-device", f"emmc,drive=emmc,boot-partition-size={boot}"]
        else:
            args = ["-drive", f"file={disk},if=virtio,format=qcow2"]
        if img.get("watchdog", False):
            args += ["-device", "i6300esb", "-action", "watchdog=reset"]
        return args

    def check_image(self) -> None:
        for key in ("disk", "kernel", "initrd", "firmware", "tfa"):
            path = self.profile.image_path(key)
            if path and not path.exists():
                name = self.profile.image_name
                hint = f" (run: image/build.sh {name})" if name and "build" in path.parts else ""
                raise SILError(f"image.{key} not found: {path}{hint}")
        qemu = self.profile["image"]["qemu"]
        if shutil.which(qemu) is None:
            raise SILError(f"{qemu} not found")
        if self.imx:
            img = self.profile["image"]
            if img.get("storage") != "emmc" or not img.get("firmware") or not img.get("tfa"):
                raise SILError(f"machine {imx8mp.MACHINE} boots TF-A and U-Boot from the eMMC: "
                               "set image.storage = emmc, image.firmware and image.tfa")
            devices = subprocess.run([qemu, "-device", "help"], capture_output=True, text=True).stdout
            if '"sil-gpio-bridge"' not in devices:
                raise SILError(f"{qemu} is not the SIL's patched QEMU (qemu/build.sh; "
                               "the ./sil container has it at /opt/qemu-sil)")
        elif self.profile["image"].get("storage") == "emmc":
            devices = subprocess.run([qemu, "-device", "help"], capture_output=True, text=True).stdout
            if '"emmc"' not in devices:
                raise SILError(f"{qemu} has no eMMC device (needs QEMU 9.2 or newer; the ./sil container has it)")

    def start(self, boot_timeout: float | None = None) -> "ShieldSIL":
        self.check_image()
        boot_timeout = boot_timeout or float(self.guest.cfg.get("boot_timeout_s", 300))
        self.run_dir.mkdir(parents=True, exist_ok=True)
        # Fresh copy-on-write disk per run: the source image is never modified.
        disk = self.profile.image_path("disk")
        subprocess.run(
            ["qemu-img", "create", "-q", "-f", "qcow2",
             "-F", self.profile["image"].get("disk_format", "raw"),
             "-b", str(disk.resolve()), str(self.run_dir / "disk.qcow2")],
            check=True,
        )
        self.net.up()
        try:
            # USB devices (i2c adapters, modem) for the guest's usbip client. QEMU
            # user-net maps guest 10.0.2.2:<port> to host 127.0.0.1:<port>.
            self.cell_data = ImpairedDataPath(TunDataPath(self.net.open_cell_data()))
            self.usbip = shield_server(self.board, data=self.cell_data).start()
            self.board.usbip_endpoint = ("10.0.2.2", self.usbip.port)
            if self.usbc_mode == "gadget":
                usb = self.net.ports["usb"]
                self.usbc_laptop = EcmLaptop(("127.0.0.1", self.net.gadget_port),
                                             wire_port=usb.wire_port, local_port=usb.qemu_port).start()
            if self.imx:
                self.i2c_bridge = imx8mp.I2cBridge(self.board, self.run_dir / "i2c-bridge.sock")
                self.gpio_bridge = imx8mp.GpioBridge(self.board, self.run_dir / "gpio-bridge.sock",
                                                     now=lambda: self.board.t)
            log = open(self.run_dir / "qemu.log", "w")
            self.proc = subprocess.Popen(self._qemu_cmd(), stdout=log, stderr=subprocess.STDOUT)
            cap = self.profile["compute"]["cpu_cap"]
            self.cpucap = CpuCap(self.proc.pid, cap["host_cores"], cap["method"])
            self.qmp = QMP(self.run_dir / "qmp.sock")
            self.console = Console(self.run_dir / "console.sock", self.run_dir / "console.log")
            self.bus = hwbus.HardwareBus(
                self.board,
                self.run_dir / "hwstate.sock",
                self.run_dir / "hwcmd.sock",
                time_scale=self.time_scale,
                on_tick=self._on_tick,
            )
            self.bus.start()
            self._sync_cellular(self.board)
            timeout = boot_timeout or float(self.guest.cfg.get("boot_timeout_s", 300))
            deadline = time.monotonic() + timeout
            start = self._wait_kernel_started(0, timeout)
            self.guest.wait_ready(self.console, timeout=max(1.0, deadline - time.monotonic()), start=start)
            if self.profile["compute"]["cpu_cap"].get("limit_speed", False):
                self.speed = self.limit_speed()
        except BaseException:
            self.stop()
            raise
        self._write_manifest()
        return self

    def _write_manifest(self) -> None:
        meta = {
            "profile": self.profile.name,
            "image": self.profile["image"],
            "guest": self.guest.kind,
            "qemu_cmd": self._qemu_cmd(),
            "usbip_port": self.usbip.port if self.usbip else None,
            "cpu_cap_method": self.cpucap.method if self.cpucap else None,
            "cpu_cap_host_cores": self.profile["compute"]["cpu_cap"]["host_cores"],
            "speed_limit": self.speed,
        }
        (self.run_dir / "run.json").write_text(json.dumps(meta, indent=2) + "\n")

    def stop(self) -> None:
        if self.bus:
            self.bus.stop()
        for bridge in (self.gpio_bridge, self.i2c_bridge):
            if bridge:
                bridge.close()
        self.gpio_bridge = self.i2c_bridge = None
        if self.usbc_laptop:
            self.usbc_laptop.stop()
            self.usbc_laptop = None
        if self.usbip:
            self.usbip.stop()
            self.usbip = None
        if self.cell_data:
            self.cell_data.close()
            self.cell_data = None
        if self.console:
            self.console.close()
        if self.qmp:
            try:
                with self._qmp_lock:
                    self.qmp.cmd("quit")
            except Exception:  # noqa: BLE001
                pass
            self.qmp.close()
        if self.proc:
            if self.cpucap:
                self.cpucap.release()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
            if self.cpucap:
                self.cpucap.release()
        self.net.down(quiet=True)

    def __enter__(self) -> "ShieldSIL":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # ------------------------------------------------------------------
    # Physical-layer controls
    def set_link(self, role: str, up: bool) -> None:
        """Plug (up=True) or pull (up=False) the cable of a port. The guest sees carrier change."""
        if role not in self._links:
            raise ValueError(f"unknown port {role!r}")
        if role == "usb" and self.usbc_laptop is not None:
            # USB-C gadget: pulling the cable ends the USB connection itself.
            self.usbc_laptop.set_plugged(up)
        else:
            with self._qmp_lock:
                assert self.qmp
                self.qmp.cmd("set_link", name=role, up=up)
        self._links[role] = up

    def press_button(self, seconds: float) -> None:
        """Press Shield's front-panel button for ``seconds`` (returns at once).

        The guest sees BTN_0 pressed, then released; OpenWrt runs
        /etc/rc.button/BTN_0 with ACTION=pressed and ACTION=released (SEEN = seconds held).
        """
        if self.imx:
            assert self.gpio_bridge
            self.gpio_bridge.press(seconds)  # gpio-keys on GPIO1_IO01
            return
        from .peripherals import Board
        assert self.usbip
        self.usbip.exports[Board.BUSID_BUTTON].device.press(seconds)

    def unplug(self, role: str) -> None:
        self.set_link(role, False)

    def plug(self, role: str) -> None:
        self.set_link(role, True)

    def _on_tick(self, board: Board) -> None:
        self._sync_cellular(board)
        if self.gpio_bridge:
            self.gpio_bridge.sync_inputs()

    def _sync_cellular(self, board: Board) -> None:
        """Shape the cellular data path from the modem model's signal and data state.

        With cellular = "modem" the EM7595 emulator already stops carrying data
        when the modem has no data service (the guest's ModemManager sees the
        bearer drop over QMI); here only the radio link quality is applied. With
        cellular = "nic" the stand-in NIC's carrier follows the modem as well.
        """
        with board.lock:
            up = board.modem.data_up
            dbm = board.modem.active_sim.signal_dbm
        wanted = (up, cell_link_for(dbm) if up else None)
        if wanted == self._cell_applied or not self.qmp:
            return
        links = [p for p in (self.cell_data, self.net.wires.get("wwan")) if p is not None]
        if not links:
            return
        for link in links:
            if up:
                delay, jitter, loss = wanted[1]
                link.set(up=True, delay_ms=delay, jitter_ms=jitter, loss_pct=loss)
            else:
                link.set(up=False)
        if "wwan" in self._links and self._links["wwan"] != up:
            self.set_link("wwan", up)
        self._cell_applied = wanted

    @property
    def emmc_dev(self) -> str:
        """The eMMC's block device in the guest: mmcblk2 on Shield's SoC (uSDHC3,
        as on Shield), mmcblk0 on QEMU virt."""
        return f"mmcblk{imx8mp.EMMC_SLOT - 1}" if self.imx else "mmcblk0"

    @property
    def cell_prefix(self) -> str:
        """Address prefix of the Shield's cellular uplink, as the internet sees it."""
        return "100.64.1." if self.cellular_mode == "modem" else "100.64.0."

    def reset_environment(self) -> None:
        """Undo harness-side changes: all cables in, wires clean, peripherals at defaults.

        The guest itself is not rebooted. With the EM7595 model the reset powers the
        modem up from scratch, so it leaves the USB bus and the guest drops its
        cellular session; ``uplinks_ready`` stays false until that has happened.
        """
        if self.cellular_mode == "modem":
            self._stale_wwan = self._wwan_ifindex() or None
        self.board.reset()
        cabled = [r for r in ("lan", "wan", "usb") if r in self._links]
        for role in cabled:
            self.net.wires[role].restore()
        for role in cabled:
            if not self._links[role]:
                self.set_link(role, True)
        self._cell_applied = None
        self._sync_cellular(self.board)

    def has_dhrystone(self) -> bool:
        return self.sh("command -v dhrystone >/dev/null && echo y", check=False).strip() == "y"

    def dmips(self, seconds: float = 3.0) -> float:
        """One guest core's Dhrystone 2.1 score in DMIPS (Dhrystones/s / 1757).

        Dhrystone needs at least 2 s of measured time, so the run count is
        scaled from a short first run.
        """
        runs = 200_000
        for _ in range(4):
            out = self.sh(f"echo {runs} | dhrystone", timeout=600)
            m = re.search(r"Dhrystones per Second:\s*([0-9.]+)", out)
            if m:
                dps = float(m.group(1))
                if runs / dps >= seconds * 0.8:
                    return dps / 1757.0
                runs = int(dps * seconds)
            else:  # "Measured time too small": try ten times as many runs
                runs *= 10
        raise SILError("dhrystone did not produce a score")

    def limit_speed(self, tolerance: float = 0.03) -> dict | None:
        """Throttle each vCPU until one guest core is no faster than one Shield core.

        QEMU runs the A53 instruction set without A53 timing, so on a fast host a
        guest core outruns the real one. The target is
        ``compute.cpu_cap.device_dmips_per_core``; see docs/calibration.md for
        where it comes from. Returns what was measured and applied, or None when
        there is no target or the image has no ``dhrystone``.
        """
        target = float(self.profile["compute"]["cpu_cap"].get("device_dmips_per_core", 0) or 0)
        if not target or not self.cpucap or not self.has_dhrystone():
            return None
        first = measured = self.dmips()
        how = "none"
        for _ in range(6):
            too_fast = measured > target * (1 + tolerance)
            too_slow = measured < target * (1 - 3 * tolerance) and self.cpucap.core_fraction < 1.0
            if not (too_fast or too_slow):
                break
            # Proportional step, aiming a little under the target (quota jitter).
            how = self.cpucap.set_core_fraction(
                self.cpucap.core_fraction * target / measured * (1 - tolerance))
            measured = self.dmips()
        return {"target_dmips_per_core": target, "unthrottled_dmips": round(first),
                "dmips": round(measured), "core_fraction": round(self.cpucap.core_fraction, 3),
                "method": how}

    # ------------------------------------------------------------------
    # Convenience for tests
    def wait_rebooted(self, since: int, timeout: float | None = None, firmware: bool = True) -> None:
        """Wait for the guest to come back up after a reboot or a board reset.

        ``since`` is a console mark (``console.mark()``) taken before the reboot.
        With ``firmware`` (and eMMC storage), first wait for U-Boot's banner, so
        output of the system going down is never taken for the new boot.
        """
        timeout = timeout or float(self.guest.cfg.get("boot_timeout_s", 300))
        deadline = time.monotonic() + timeout
        if firmware and self._boots_firmware():
            # The firmware's banner: a new boot really started (not the old system's output).
            since = self.console.wait_for(r"U-Boot \d{4}\.\d{2}", timeout, start=since).end()
        since = self._wait_kernel_started(since, max(1.0, deadline - time.monotonic()))
        self.guest.wait_ready(self.console, max(1.0, deadline - time.monotonic()), start=since)

    def _boots_firmware(self) -> bool:
        """U-Boot loads the kernel (eMMC images), rather than QEMU directly."""
        return self.profile["image"].get("storage") == "emmc"

    def _wait_kernel_started(self, since: int, timeout: float) -> int:
        """With U-Boot, wait until it hands over to the kernel. Until then a key
        on the console (the guest adapter's Enter to get a prompt) would stop
        U-Boot's autoboot, as on a real board."""
        if not self._boots_firmware():
            return since
        return self.console.wait_for(r"Starting kernel \.\.\.", timeout, start=since).end()

    def sh(self, cmd: str, timeout: float = 60.0, check: bool = True) -> str:
        assert self.console
        return self.console.run(cmd, timeout=timeout, check=check)[1]

    def ipv4(self, role: str) -> str:
        """IPv4 address of the guest's lan/wan/wwan interface, or "" if none."""
        assert self.console
        return self.guest.ipv4(self.console, role)

    def _mwan3_status(self) -> dict[str, str] | None:
        """``mwan3 interfaces`` lines by interface name, or None without mwan3."""
        if self.guest.kind != "openwrt":
            return None
        out = self.sh("if [ -x /etc/init.d/mwan3 ]; then mwan3 interfaces; else echo no-mwan3; fi",
                      check=False)
        if out.strip() == "no-mwan3":
            return None
        return {ln.split()[1]: ln for ln in out.splitlines() if ln.strip().startswith("interface ")}

    def uplinks_ready(self) -> bool:
        """Both uplinks have a lease and, where mwan3 runs, mwan3 reports both online.

        With mwan3 a leased uplink carries traffic only once its tracking marks it
        online, which lags the lease (longer on a slow host), so a failover test
        must start from this state, not from the leases alone.
        """
        if self._stale_wwan is not None:
            # The guest notices the modem leaving only after a few seconds (netifd
            # and mwan3 later still); until then it shows the old session as up.
            if self._wwan_ifindex() == self._stale_wwan:
                return False
            self._stale_wwan = None
        if not (self.ipv4("wan") and self.ipv4("wwan")):
            return False
        lines = self._mwan3_status()
        if lines is None:
            return True
        # "interface wan is online and tracking is active (online 00h:00m:49s, ...)": the
        # tracker's status, then in brackets the hotplug state (whether mwan3 has
        # installed the interface's routes and rules). Both must be online.
        return all(
            (ln := lines.get(self.guest.logical[r], "")) and " is online " in ln and "(online " in ln
            for r in ("wan", "wwan"))

    def diagnostics(self) -> Path:
        """Write the host- and guest-side state of the hardware path to the run dir.

        For a stalled wait: whether the model's locks are free, the modem model,
        the hardware bus and USB/IP sessions, every host thread's stack, and the
        guest's view (bus files, USB/IP agent, vhci ports). Returns the file.
        """
        out: list[str] = [f"# diagnostics at {time.strftime('%Y-%m-%d %H:%M:%S')}"]

        def lock_free(lock) -> str:
            if lock.acquire(timeout=2):
                lock.release()
                return "free"
            return "HELD (not free within 2 s)"

        out.append(f"board.lock: {lock_free(self.board.lock)}")
        if self.board.lock.acquire(timeout=2):
            try:
                out.append(f"board.t: {self.board.t:.1f}")
                out.append(f"modem: {json.dumps(self.board.modem.snapshot(), default=str)}")
                out.append(f"modem.state={self.board.modem.state} powered={self.board.modem.powered} "
                           f"enumerations={self.board.modem.enumerations} usb_id={self.board.modem.usb_id}")
                out.append(f"usbip busids offered: {self.board.usbip_busids()}")
            finally:
                self.board.lock.release()
        if self.bus:
            out.append(f"hw bus: seq={self.bus.seq} dropped={self.bus.dropped_frames} "
                       f"errors={self.bus.errors[-5:]}")
        if self.usbip:
            srv = self.usbip
            out.append(f"usbip server lock: {lock_free(srv._lock)}")
            for busid, e in srv.exports.items():
                s = srv.sessions.get(busid)
                sess = (f"session gen={s.generation} closed={s.closed.is_set()} "
                        f"wlock={lock_free(s._wlock)}") if s else "no session"
                out.append(f"usbip {busid} {type(e.device).__name__}: device lock {lock_free(e.device.lock)}; "
                           f"{sess}")
        frames = sys._current_frames()
        for t in threading.enumerate():
            f = frames.get(t.ident) if t.ident is not None else None
            out.append(f"--- thread {t.name} ---")
            out.append("".join(traceback.format_stack(f)) if f else "(no frame)")
        if self.console:
            out.append("--- guest ---")
            try:
                guest = self.sh(
                "for f in bus usbip modem; do echo \"$f: $(cat /var/run/shield/$f.json 2>&1)\"; done; "
                "ps w 2>/dev/null | grep -E '[u]sbip|[h]wd' || ps axww | grep -E '[u]sbip|[h]wd'; "
                "cat /sys/devices/platform/vhci_hcd.0/status 2>&1 | head -6; "
                "(logread 2>/dev/null || journalctl -b --no-pager 2>/dev/null) "
                "| grep -E 'shield-usbip|vhci|usb 1-|hwd' | tail -40",
                timeout=60, check=False)
            except Exception as e:  # noqa: BLE001 - keep the host-side state
                guest = f"(guest state unavailable: {e!r})"
            out.append(guest)
        path = self.run_dir / f"diag-{int(time.time())}.txt"
        path.write_text("\n".join(out) + "\n")
        return path

    def _wwan_ifindex(self) -> str:
        """Kernel ifindex of the guest's cellular netdev, or "" if it is absent.

        A re-enumerated modem gets a new netdev and so a new ifindex.
        """
        dev = self.guest.ifnames["wwan"]
        return self.sh(f"cat /sys/class/net/{dev}/ifindex 2>/dev/null", check=False).strip()

    def mwan3_desynced(self) -> list[str]:
        """Uplinks mwan3's tracker calls online while its hotplug state is offline.

        mwan3 2.12 runs each hotplug event as its own process. When a cable goes back
        in just after the tracker declared the link offline, the queued
        ``disconnected`` event can run after ``ifup``; the tracker then reconnects
        without a hotplug event (FIRSTCONNECT), so the interface keeps no routes
        until mwan3 restarts. Seen on slow CI runners; see docs/guest-interfaces.md.
        """
        lines = self._mwan3_status() or {}
        return [n for n, ln in lines.items() if " is online " in ln and "(offline " in ln]

    def hw(self, device: str, timeout: float = 30.0) -> dict:
        """Read a peripheral the way the software under test sees it.

        The reading is at least as new as the last hardware-bus frame published
        before this call, so a test never acts on state from before its own
        harness change. Raises SILError if the guest agent cannot catch up.
        """
        if not self.guest.has_hw_agent:
            raise SILError("this image has no hardware agent (guest.hw_agent = false)")
        assert self.bus
        wanted = self.bus.seq
        deadline = time.monotonic() + timeout
        seq = -1
        while True:
            # bus.json is written last in each frame: read it first, then the device.
            out = self.sh(f"shield-hw get bus && shield-hw get {device}", check=False).splitlines()
            if len(out) >= 2:
                try:
                    seq = int(json.loads(out[0])["seq"])
                    if seq >= wanted:
                        return json.loads(out[1])
                except (ValueError, KeyError):
                    pass
            if time.monotonic() > deadline:
                raise SILError(
                    f"guest hardware agent is behind: has frame {seq}, need {wanted} "
                    f"({self.bus.dropped_frames} frames dropped so far); output: {out!r}"
                )
            time.sleep(0.2)

    def lan(self, *cmd: str, timeout: float = 30) -> subprocess.CompletedProcess:
        return self.net.exec("lan", *cmd, timeout=timeout)

    def internet_get(self, timeout: float = 10) -> dict | None:
        """HTTP GET to the simulated internet from the LAN client. None on failure."""
        r = self.lan("curl", "-sS", "--max-time", str(timeout), f"http://{INTERNET_HOST}/", timeout=timeout + 5)
        if r.returncode != 0:
            return None
        return json.loads(r.stdout)


def wait_until(fn: Callable[[], T], timeout: float, interval: float = 1.0, what: str = "condition") -> T:
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while True:
        try:
            v = fn()
            if v:
                return v
        except Exception as e:  # noqa: BLE001
            last_exc = e
        if time.monotonic() > deadline:
            raise TimeoutError(f"timed out after {timeout}s waiting for {what}" + (f" (last error: {last_exc})" if last_exc else ""))
        time.sleep(interval)

