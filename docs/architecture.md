# Architecture

```
                    host (root)                                     guest: emulated Shield
 ┌──────────────────────────────────────────────┐   ┌──────────────────────────────────────┐
 │ [lan ns] client 192.168.100.50 ─tap─ Wire ───┼UDP┼─ eth0 ─ br-lan 192.168.100.1         │
 │ [isp1 ns] 203.0.113.1 DHCP ─tap─ Wire ───────┼UDP┼─ eth1 ─ wan  (dhcp, metric 10)       │
 │ [isp2 ns] 100.64.1.1 TUN ─ radio impairment ─┼USB┼─ EM7595 wwan0 ─ wanb (ModemManager) │
 │   (images with cellular = "nic": 100.64.0.1  │/IP │   or eth2 ─ wanb (dhcp, metric 20)   │
 │    DHCP ─tap─ Wire ─ eth2 instead)           │   │                                      │
 │ isp1/isp2 ─veth─ [inet ns] 198.51.100.10 echo│   │                                      │
 │                                              │   │ OpenWrt 24.10.3 (default) / 25.12.3  │
 │                                              │   │ / 23.05.6, or Ubuntu 26.04           │
 │ Board models ─ HardwareBus ──────────────────┼vio┼─ shield-hwd → /var/run/shield/*.json │
 │   bms pd thermal modem leds      ◄───────────┼ser┼─ shield-hw set / ubus call shield …  │
 │ QMP (set_link) · serial console · CPU cap    │   │                                      │
 └──────────────────────────────────────────────┘   └──────────────────────────────────────┘
```

## Pieces

* **`sil.ShieldSIL`** starts everything in order: a copy-on-write disk over the
  base image, the network lab, QEMU, the CPU cap, QMP, the serial console and the
  hardware bus. It waits until `rc.local` has run, then hands control to tests.
* **`cpucap.CpuCap`** puts QEMU in a cgroup with a CPU quota of
  `compute.cpu_cap.host_cores` (cgroup v2, then v1, then a SIGSTOP/SIGCONT
  duty cycle). QEMU reproduces the A53 instruction set but not its timing, so the
  quota is what stops a fast host from out-computing the board.
* **`netlab.NetLab`** builds four network namespaces. The ISPs do not NAT, so
  the echo server reports which uplink a request used. With `network.internet =
  "host"` (`./sil up --internet`), `netlab.HostUplink` adds a veth from the inet
  namespace to the host's, routes the uplink ranges back through it, masquerades
  them out of the host's default route (nftables, else iptables), and runs a DNS
  forwarder there; each ISP namespace runs a resolver on its gateway (wired DHCP
  option 6, and the EM7595 bearer DNS 100.64.1.1). The tracked addresses
  (1.1.1.1 etc.) are then the real ones.
* **`wire.Wire`** relays Ethernet frames between QEMU (`-netdev dgram`) and a
  tap. It can cut a link (upstream outage, carrier stays up), drop, delay, add
  jitter or limit rate. Pulling a cable is QMP `set_link` (carrier loss).
* **`peripherals.Board`** holds the hardware models and steps them on a clock
  (`time_scale` can run physics faster than real time).
* **`hwbus.HardwareBus`** publishes model readings to the guest twice a second and
  applies guest commands. It also shapes the cellular data path (wwan0 packets,
  or the `eth2` wire and carrier on `cellular = "nic"` images) from the modem
  state, with link quality from signal strength.
* **`console.Console`** runs shell commands over the serial console with unique
  begin/end markers and exit codes.

## Boot and run times (4-core x86 host, no KVM)

* Boot to "init finished": about 55 s (OpenWrt) and 70 s (Ubuntu) on a 4-core
  workstation; slower on GitHub runners.
* Full system suite, one shared instance: 12 to 14 minutes per image on GitHub's
  `ubuntu-24.04` runners (CI on main, 2026-11-07).
