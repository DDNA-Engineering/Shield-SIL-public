"""Host-side network around the emulated Shield.

Topology (all addresses are documentation/test ranges)::

    [lan ns]  client <LAN .50> ---------wire--- guest LAN port (gateway from profile, 192.168.100.1 for Shield)
    [isp1 ns] 203.0.113.1 (DHCP) -------wire--- Shield eth1 (WAN, wired)
    [isp2 ns] 100.64.1.1  TUN ----------------- EM7595 wwan0 over USB/IP (raw IP, bearer 100.64.1.50)
    [isp2 ns] 100.64.0.1  (DHCP) -------wire--- Shield eth2 (only with cellular = "nic")
    isp1 ns --veth-- [inet ns] --veth-- isp2 ns
    [inet ns] 198.51.100.10: HTTP echo server that reports the caller's address

Because the ISPs do not NAT, the echo server sees the Shield's WAN-side
address, which tells a test which uplink a request actually used.

By default the lab is closed: nothing leaves it, and the inet namespace answers
the addresses mwan3 tracks. With ``internet=True`` (profile ``network.internet =
"host"``) the inet namespace also gets a veth to the host's network namespace,
and the host masquerades the Shield's uplink addresses out of its own default
route, so the guest reaches the real internet through every uplink. The ISPs
then also serve DNS (wired DHCP option 6, and the EM7595 bearer's DNS
100.64.1.1), forwarded to the host's resolvers, and the tracked addresses are
the real ones::

    [inet ns] 10.255.0.2 --veth-- 10.255.0.1 [host ns] --NAT--> host's default route
"""

from __future__ import annotations

import ipaddress
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .wire import Wire, open_tap, open_tun

INTERNET_HOST = "198.51.100.10"
ISP1_GW = "203.0.113.1"
ISP2_GW = "100.64.0.1"
# Addresses Shield's mwan3 tracks for uplink health; the simulated internet answers them.
TRACK_IPS = ("1.1.1.1", "1.0.0.1", "9.9.9.9", "149.112.112.112")
CELL_DATA_GW = "100.64.1.1"  # EM7595 bearer network (see usbip/qmi.py)

# Host internet (network.internet = "host"): the link from the inet namespace to
# the host, and every address range behind it that the host must route and NAT.
UPLINK_HOST_IP = "10.255.0.1"
UPLINK_INET_IP = "10.255.0.2"
UPLINK_SUBNETS = (
    "203.0.113.0/24",  # wired WAN (isp1)
    "100.64.0.0/24",   # cellular stand-in NIC (isp2)
    "100.64.1.0/24",   # EM7595 bearer (isp2)
    "10.255.1.0/30",   # isp1 <-> inet (the ISP's DNS forwarder)
    "10.255.2.0/30",   # isp2 <-> inet
)
INTERNET_MODES = ("off", "host")


def _run(*args: str, check: bool = True, ns: str | None = None, **kw) -> subprocess.CompletedProcess:
    cmd = list(args)
    if ns:
        cmd = ["ip", "netns", "exec", ns, *cmd]
    return subprocess.run(cmd, check=check, capture_output=True, text=True, **kw)


def require_host_tools(internet: bool = False) -> None:
    missing = [t for t in ("ip", "dnsmasq") if shutil.which(t) is None]
    if internet and not any(shutil.which(t) for t in ("nft", "iptables-legacy", "iptables")):
        missing.append("nft or iptables")
    if missing:
        raise RuntimeError(f"missing host tools: {', '.join(missing)}")
    if os.geteuid() != 0:
        raise RuntimeError("the network lab needs root (network namespaces and taps)")
    if not Path("/dev/net/tun").exists():
        raise RuntimeError("/dev/net/tun is not available")


@dataclass
class PortPlan:
    """One Shield NIC: QEMU <-UDP-> Wire <-> tap in a namespace."""

    role: str  # lan | wan | wwan
    ns: str
    tap: str
    qemu_port: int
    wire_port: int
    mac: str


# SIL management link: the guest's path to host-side simulator services (the
# USB/IP server for the modem and I2C adapters). QEMU user networking, so guest
# 10.0.2.2:<port> reaches host 127.0.0.1:<port>. It is the guest's last NIC
# (PCI slot 0x1f), has no gateway, and belongs to no Shield network role, so
# LAN/WAN/failover tests never see its traffic.
MGMT_MAC = "52:54:00:5e:00:1f"
# USB-C usb0 when presented as a plain NIC (network.usbc = "nic"); the guest
# renames the NIC with this MAC to usb0.
USB_NIC_MAC = "52:54:00:5e:00:13"
MGMT_GUEST_IP = "10.0.2.15"
MGMT_HOST_IP = "10.0.2.2"


# Guest port of usbipd --device (the USB-C gadget, network.usbc = "gadget").
GADGET_USBIP_PORT = 3240


def mgmt_netdev_args(nic_model: str, gadget_port: int | None = None, pci_bus: str | None = None) -> list[str]:
    """``gadget_port``: host 127.0.0.1 port forwarded to the guest's usbipd --device.
    ``pci_bus``: the PCI bus to put the NIC on (default: the machine's root bus)."""
    fwd = f",hostfwd=tcp:127.0.0.1:{gadget_port}-{MGMT_GUEST_IP}:{GADGET_USBIP_PORT}" if gadget_port else ""
    bus = f",bus={pci_bus}" if pci_bus else ""
    return [
        "-netdev", f"user,id=silmgmt,net=10.0.2.0/24,host=10.0.2.2,ipv6=off{fwd}",
        "-device", f"{nic_model},netdev=silmgmt,mac={MGMT_MAC}{bus},addr=0x1f",
    ]


@dataclass
class NetLab:
    prefix: str
    base_port: int
    workdir: Path
    lan_gateway: str = "192.168.100.1"
    lan_prefix: int = 24
    nic_model: str = "virtio-net-pci"
    cellular_nic: bool = True  # False: cellular data only through the modem's wwan0
    usbc_nic: bool = False  # True: USB-C usb0 as a plain NIC to a simulated laptop
    # True: USB-C usb0 is the guest's USB network gadget; the laptop's tap is
    # fed by the host's USB/IP ECM host (usbip/ecm_host.py), not by a QEMU NIC.
    usbc_gadget: bool = False
    gadget_port: int | None = None  # host port forwarded to the guest's usbipd --device
    internet: bool = False  # True: uplinks reach the real internet through the host (NAT)
    # The machine's on-board NIC model for LAN (e.g. "imx.enet", the i.MX FEC),
    # or "" for a PCI NIC like the other ports; and the PCI bus for those.
    lan_onboard_nic: str = ""
    pci_bus: str | None = None
    ports: dict[str, PortPlan] = field(default_factory=dict)
    wires: dict[str, Wire] = field(default_factory=dict)
    _procs: list[subprocess.Popen] = field(default_factory=list)
    _namespaces: list[str] = field(default_factory=list)
    _uplink: "HostUplink | None" = None

    def __post_init__(self) -> None:
        lan = ipaddress.ip_interface(f"{self.lan_gateway}/{self.lan_prefix}")
        # The LAN client takes the 50th host address of the guest's LAN (192.168.100.50 for Shield).
        self.lan_client_ip = str(lan.network.network_address + 50)
        p, b = self.prefix, self.base_port
        # A laptop on Shield's USB-C port takes the 60th host address (192.168.100.60).
        self.usb_host_ip = str(lan.network.network_address + 60)
        self.ns = {k: f"{p}-{k}" for k in ("lan", "isp1", "isp2", "inet")}
        if self.usbc_nic or self.usbc_gadget:
            self.ns["usbhost"] = f"{p}-usbhost"
        self.ports = {
            "lan": PortPlan("lan", self.ns["lan"], f"{p}lan", b, b + 1, "52:54:00:5e:00:10"),
            "wan": PortPlan("wan", self.ns["isp1"], f"{p}wan", b + 2, b + 3, "52:54:00:5e:00:11"),
            "wwan": PortPlan("wwan", self.ns["isp2"], f"{p}cel", b + 4, b + 5, "52:54:00:5e:00:12"),
        }
        if not self.cellular_nic:
            del self.ports["wwan"]
        if self.usbc_nic or self.usbc_gadget:
            self.ports["usb"] = PortPlan("usb", self.ns["usbhost"], f"{p}usb", b + 6, b + 7, USB_NIC_MAC)

    # ------------------------------------------------------------------
    def qemu_netdev_args(self) -> list[str]:
        return self._role_netdev_args() + mgmt_netdev_args(
            self.nic_model, self.gadget_port if self.usbc_gadget else None, self.pci_bus)

    def _role_netdev_args(self) -> list[str]:
        args: list[str] = []
        for i, role in enumerate(("lan", "wan", "wwan", "usb")):
            pp = self.ports.get(role)
            if pp is None or (role == "usb" and self.usbc_gadget):
                continue
            args += [
                "-netdev",
                f"dgram,id={role},local.type=inet,local.host=127.0.0.1,local.port={pp.qemu_port},"
                f"remote.type=inet,remote.host=127.0.0.1,remote.port={pp.wire_port}",
            ]
            if role == "lan" and self.lan_onboard_nic:
                args += ["-net", f"nic,netdev={role},macaddr={pp.mac},model={self.lan_onboard_nic}"]
            else:
                bus = f",bus={self.pci_bus}" if self.pci_bus else ""
                args += ["-device", f"{self.nic_model},netdev={role},mac={pp.mac}{bus},addr=0x{0x10 + i:x}"]
        return args

    def up(self) -> None:
        require_host_tools(self.internet)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.down(quiet=True)
        for ns in self.ns.values():
            _run("ip", "netns", "add", ns)
            self._namespaces.append(ns)
            _run("ip", "link", "set", "lo", "up", ns=ns)

        for role, pp in self.ports.items():
            fd = open_tap(pp.tap)
            _run("ip", "link", "set", pp.tap, "netns", pp.ns)
            _run("ip", "link", "set", pp.tap, "up", ns=pp.ns)
            w = Wire(role, fd, qemu_port=pp.qemu_port, local_port=pp.wire_port, seed=hash(role) & 0xFFFF)
            w.start()
            self.wires[role] = w

        lan, isp1, isp2, inet = (self.ns[k] for k in ("lan", "isp1", "isp2", "inet"))
        # LAN client
        _run("ip", "addr", "add", f"{self.lan_client_ip}/{self.lan_prefix}", "dev", self.ports["lan"].tap, ns=lan)
        _run("ip", "route", "add", "default", "via", self.lan_gateway, ns=lan)
        if "usb" in self.ports:  # laptop on USB-C
            usb = self.ports["usb"]
            _run("ip", "addr", "add", f"{self.usb_host_ip}/{self.lan_prefix}", "dev", usb.tap, ns=usb.ns)
            _run("ip", "route", "add", "default", "via", self.lan_gateway, ns=usb.ns)
        # ISPs
        _run("ip", "addr", "add", f"{ISP1_GW}/24", "dev", self.ports["wan"].tap, ns=isp1)
        if "wwan" in self.ports:
            _run("ip", "addr", "add", f"{ISP2_GW}/24", "dev", self.ports["wwan"].tap, ns=isp2)
        for i, isp in ((1, isp1), (2, isp2)):
            a, b = f"{self.prefix}i{i}a", f"{self.prefix}i{i}b"
            _run("ip", "link", "add", a, "netns", isp, "type", "veth", "peer", "name", b, "netns", inet)
            _run("ip", "addr", "add", f"10.255.{i}.1/30", "dev", a, ns=isp)
            _run("ip", "addr", "add", f"10.255.{i}.2/30", "dev", b, ns=inet)
            _run("ip", "link", "set", a, "up", ns=isp)
            _run("ip", "link", "set", b, "up", ns=inet)
            _run("ip", "route", "add", "default", "via", f"10.255.{i}.2", ns=isp)
            _run("sysctl", "-qw", "net.ipv4.ip_forward=1", ns=isp)
        _run("ip", "route", "add", "203.0.113.0/24", "via", "10.255.1.1", ns=inet)
        if "wwan" in self.ports:
            _run("ip", "route", "add", "100.64.0.0/24", "via", "10.255.2.1", ns=inet)
        _run("ip", "addr", "add", f"{INTERNET_HOST}/32", "dev", "lo", ns=inet)
        if not self.internet:
            # Closed lab: the simulated internet answers mwan3's health checks.
            # With the host's internet the real addresses answer them.
            for ip in TRACK_IPS:
                _run("ip", "addr", "add", f"{ip}/32", "dev", "lo", ns=inet)

        self._spawn_dhcp(isp1, self.ports["wan"].tap, ISP1_GW, "203.0.113.100", "203.0.113.200")
        if "wwan" in self.ports:
            self._spawn_dhcp(isp2, self.ports["wwan"].tap, ISP2_GW, "100.64.0.100", "100.64.0.200")
        if self.internet:
            self._uplink = HostUplink(self.prefix, inet, self.workdir)
            self._uplink.up()
            for isp in (isp1, isp2):
                self._spawn_dns(isp)
            # `ip netns exec` uses /etc/netns/<ns>/resolv.conf: the LAN client and
            # the USB-C laptop resolve through the Shield, like real LAN devices.
            for role in ("lan", "usbhost"):
                if role in self.ns:
                    _write_netns_resolv(self.ns[role], self.lan_gateway)
        self._spawn(
            ["ip", "netns", "exec", inet, sys.executable, "-m", "shield_sil.echo_server", INTERNET_HOST, "80"],
            "echo",
        )

    def open_cell_data(self) -> int:
        """TUN for the EM7595's raw-IP data, placed in the cellular ISP's namespace.

        Returns the fd; the device keeps working after it moves namespace.
        """
        name = f"{self.prefix}cdat"
        fd = open_tun(name)
        isp2 = self.ns["isp2"]
        _run("ip", "link", "set", name, "netns", isp2)
        _run("ip", "addr", "add", f"{CELL_DATA_GW}/24", "dev", name, ns=isp2)
        _run("ip", "link", "set", name, "up", ns=isp2)
        _run("ip", "route", "replace", "100.64.1.0/24", "via", "10.255.2.1", ns=self.ns["inet"])
        return fd

    def _spawn_dhcp(self, ns: str, ifname: str, gw: str, start: str, end: str) -> None:
        lease = self.workdir / f"{ns}.leases"
        self._spawn(
            [
                "ip", "netns", "exec", ns, "dnsmasq", "--keep-in-foreground", "--conf-file=/dev/null",
                "--port=0", f"--interface={ifname}", "--bind-interfaces", "--no-hosts",
                f"--dhcp-range={start},{end},255.255.255.0,1h", f"--dhcp-option=3,{gw}",
                # The closed lab has no DNS; with the host's internet the ISP's
                # gateway is its resolver (see _spawn_dns).
                *([f"--dhcp-option=6,{gw}"] if self.internet else []),
                f"--dhcp-leasefile={lease}", f"--pid-file={self.workdir / (ns + '.pid')}",
            ],
            f"dnsmasq-{ns}",
        )

    def _spawn_dns(self, ns: str) -> None:
        """The ISP's resolver, on every address in the ISP namespace (including the
        EM7595 bearer gateway 100.64.1.1, which appears later), forwarding to the host."""
        self._spawn(
            [
                "ip", "netns", "exec", ns, "dnsmasq", "--keep-in-foreground", "--conf-file=/dev/null",
                "--bind-dynamic", "--except-interface=lo", "--no-hosts", "--no-resolv",
                f"--server={UPLINK_HOST_IP}", f"--pid-file={self.workdir / (ns + '-dns.pid')}",
            ],
            f"dns-{ns}",
        )

    def _spawn(self, cmd: list[str], name: str) -> None:
        log = open(self.workdir / f"{name}.log", "w")
        env = dict(os.environ)
        src = str(Path(__file__).resolve().parents[1])
        env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
        self._procs.append(
            subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True, env=env)
        )

    # ------------------------------------------------------------------
    def exec(self, where: str, *cmd: str, timeout: float = 30) -> subprocess.CompletedProcess:
        """Run a command in one of the lab namespaces (lan, isp1, isp2, inet, usbhost)."""
        return _run(*cmd, ns=self.ns[where], check=False, timeout=timeout)

    def down(self, quiet: bool = False) -> None:
        for w in self.wires.values():
            w.stop()
        self.wires.clear()
        for p in self._procs:
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        for p in self._procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
        self._procs.clear()
        for role in ("lan", "usbhost"):
            _remove_netns_resolv(f"{self.prefix}-{role}")
        # Also cleans up after a run that crashed before down().
        (self._uplink or HostUplink(self.prefix, self.ns["inet"], self.workdir)).down()
        self._uplink = None
        for ns in list(self.ns.values()):
            r = _run("ip", "netns", "del", ns, check=False)
            if r.returncode and not quiet and "No such file" not in r.stderr:
                print(f"warning: could not delete netns {ns}: {r.stderr.strip()}", file=sys.stderr)
        self._namespaces.clear()
        time.sleep(0.1)


def _write_netns_resolv(ns: str, nameserver: str) -> None:
    d = Path("/etc/netns") / ns
    d.mkdir(parents=True, exist_ok=True)
    (d / "resolv.conf").write_text(f"# written by the Shield SIL (network.internet)\nnameserver {nameserver}\n")


def _remove_netns_resolv(ns: str) -> None:
    d = Path("/etc/netns") / ns
    f = d / "resolv.conf"
    if f.exists() and "Shield SIL" in f.read_text():
        f.unlink()
        try:
            d.rmdir()
        except OSError:
            pass


def nat_commands(tool: str, table: str, out_exclude: str, subnets: tuple[str, ...] = UPLINK_SUBNETS
                 ) -> tuple[list[list[str]], list[list[str]]]:
    """(add, remove) commands that masquerade ``subnets`` leaving the host by any
    interface but ``out_exclude`` (the lab's own veth). ``tool``: nft or an iptables binary."""
    if tool == "nft":
        add = [
            ["nft", "add", "table", "ip", table],
            ["nft", "add", "chain", "ip", table, "postrouting",
             "{ type nat hook postrouting priority srcnat ; }"],
            ["nft", "add", "rule", "ip", table, "postrouting",
             "ip", "saddr", "{ " + ", ".join(subnets) + " }", "oifname", "!=", out_exclude, "masquerade"],
        ]
        return add, [["nft", "delete", "table", "ip", table]]
    def rule(op: str, net: str) -> list[str]:
        return [tool, "-t", "nat", op, "POSTROUTING", "-s", net, "!", "-o", out_exclude,
                "-m", "comment", "--comment", table, "-j", "MASQUERADE"]

    return [rule("-A", n) for n in subnets], [rule("-D", n) for n in subnets]


@dataclass
class HostUplink:
    """The lab's way out: a veth from the inet namespace to the host namespace,
    routes back to the Shield's uplink ranges, NAT, and a DNS forwarder on the
    host side that uses the host's own resolvers (/etc/resolv.conf, which in a
    Docker container is Docker's DNS). One per host network namespace: every
    ./sil command runs in its own container, so that is one per command."""

    prefix: str
    inet_ns: str
    workdir: Path
    _tool: str | None = None
    _forward_was: str | None = None
    _dns: subprocess.Popen | None = None

    @property
    def veth(self) -> str:
        return f"{self.prefix}up"

    @property
    def table(self) -> str:
        return f"shield_sil_{self.prefix}"

    def up(self) -> None:
        if not _run("ip", "-4", "route", "show", "default", check=False).stdout.strip():
            raise RuntimeError("network.internet = host, but this machine has no default route "
                               "(no internet to share)")
        for net in UPLINK_SUBNETS:
            if _run("ip", "-4", "route", "show", net, check=False).stdout.strip():
                raise RuntimeError(f"network.internet = host: {net} is already routed on this machine "
                                   "(another SIL instance with host internet in the same network "
                                   "namespace?). Run each instance in its own container.")
        inside = f"{self.prefix}upi"
        _run("ip", "link", "add", self.veth, "type", "veth", "peer", "name", inside, "netns", self.inet_ns)
        _run("ip", "addr", "add", f"{UPLINK_HOST_IP}/30", "dev", self.veth)
        _run("ip", "link", "set", self.veth, "up")
        _run("ip", "addr", "add", f"{UPLINK_INET_IP}/30", "dev", inside, ns=self.inet_ns)
        _run("ip", "link", "set", inside, "up", ns=self.inet_ns)
        _run("ip", "route", "add", "default", "via", UPLINK_HOST_IP, ns=self.inet_ns)
        _run("sysctl", "-qw", "net.ipv4.ip_forward=1", ns=self.inet_ns)
        for net in UPLINK_SUBNETS:
            _run("ip", "route", "add", net, "via", UPLINK_INET_IP, "dev", self.veth)
        self._forward_was = _run("sysctl", "-n", "net.ipv4.ip_forward").stdout.strip()
        _run("sysctl", "-qw", "net.ipv4.ip_forward=1")
        self._forward_accept(True)
        self._nat_up()
        log = open(self.workdir / "dns-host.log", "w")
        self._dns = subprocess.Popen(
            ["dnsmasq", "--keep-in-foreground", "--conf-file=/dev/null", "--no-hosts",
             f"--listen-address={UPLINK_HOST_IP}", "--bind-interfaces",
             f"--pid-file={self.workdir / 'dns-host.pid'}"],
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)

    def _nat_up(self) -> None:
        errors = []
        for tool in ("nft", "iptables-legacy", "iptables"):
            if not shutil.which(tool):
                continue
            add, remove = nat_commands(tool, self.table, self.veth)
            r = None
            for cmd in add:
                r = _run(*cmd, check=False)
                if r.returncode:
                    break
            if r is not None and r.returncode == 0:
                self._tool = tool
                return
            errors.append(f"{tool}: {r.stderr.strip() if r else 'no commands'}")
            for cmd in remove:
                _run(*cmd, check=False)
        raise RuntimeError("network.internet = host: could not set up NAT (" + "; ".join(errors) + ")")

    def _forward_rules(self, op: str) -> list[list[str]]:
        tag = ["-m", "comment", "--comment", self.table, "-j", "ACCEPT"]
        return [["iptables", op, "FORWARD", "-i", self.veth, *tag],
                ["iptables", op, "FORWARD", "-o", self.veth, *tag]]

    def _forward_accept(self, on: bool) -> None:
        """Let the lab's traffic through a FORWARD chain that drops by default, as
        Docker sets it on a host that runs the SIL without a container. Inside the
        SIL container the chain is empty and this changes nothing."""
        if not shutil.which("iptables"):
            return
        if on:
            for cmd in self._forward_rules("-I"):
                _run(*cmd, check=False)
        else:
            for cmd in self._forward_rules("-D"):
                while _run(*cmd, check=False).returncode == 0:
                    pass

    def down(self) -> None:
        if self._dns:
            try:
                os.killpg(self._dns.pid, signal.SIGTERM)
                self._dns.wait(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass
            self._dns = None
        # Without a record of the tool (cleanup after a crash), try them all.
        for tool in [self._tool] if self._tool else ["nft", "iptables-legacy", "iptables"]:
            if shutil.which(tool):
                for cmd in nat_commands(tool, self.table, self.veth)[1]:
                    _run(*cmd, check=False)
        self._tool = None
        self._forward_accept(False)
        # Deleting our end removes the pair and the routes through it.
        _run("ip", "link", "del", self.veth, check=False)
        if self._forward_was is not None:
            _run("sysctl", "-qw", f"net.ipv4.ip_forward={self._forward_was}", check=False)
            self._forward_was = None
