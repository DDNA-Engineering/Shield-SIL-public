"""network.internet: the opt-in route from the Shield's uplinks to the host's real internet.

The namespace and NAT setup needs root and runs in the system tests' environment;
these checks cover the switch and the NAT rules without touching the host.
"""

import argparse

import pytest

from shield_sil import cli
from shield_sil.netlab import UPLINK_SUBNETS, NetLab, nat_commands
from shield_sil.profile import Profile, env_overrides


def test_closed_lab_is_the_default():
    assert Profile.load()["network"]["internet"] == "off"
    for image in ("openwrt-24.10.3", "openwrt-25.12.3", "ubuntu-26.04"):
        assert Profile.load(image=image)["network"]["internet"] == "off"


def test_host_internet_can_be_chosen_per_run():
    assert Profile.load(overrides={"network.internet": "host"})["network"]["internet"] == "host"
    assert env_overrides({"SIL_INTERNET": "host"}) == {"network.internet": "host"}


def test_cli_flag_selects_host_internet():
    args = argparse.Namespace(disk=None, kernel=None, guest=None, set=[], image=None, profile=None,
                              internet=True)
    assert cli._load(args)["network"]["internet"] == "host"


def test_unknown_internet_mode_rejected():
    with pytest.raises(ValueError, match="network.internet"):
        Profile.load(overrides={"network.internet": "yes"})


def test_uplink_ranges_cover_every_uplink_address():
    # Wired WAN DHCP pool, cellular stand-in NIC pool and the EM7595 bearer.
    import ipaddress
    nets = [ipaddress.ip_network(n) for n in UPLINK_SUBNETS]
    for addr in ("203.0.113.150", "100.64.0.150", "100.64.1.50", "10.255.1.1", "10.255.2.1"):
        assert any(ipaddress.ip_address(addr) in n for n in nets), addr


def test_nft_rules_masquerade_uplinks_except_back_into_the_lab():
    add, remove = nat_commands("nft", "shield_sil_sil0", "sil0up")
    rule = " ".join(add[-1])
    assert "masquerade" in rule and 'oifname != sil0up' in rule
    for net in UPLINK_SUBNETS:
        assert net in rule
    assert remove == [["nft", "delete", "table", "ip", "shield_sil_sil0"]]


def test_iptables_rules_are_removed_one_for_one():
    add, remove = nat_commands("iptables", "shield_sil_sil0", "sil0up")
    assert len(add) == len(remove) == len(UPLINK_SUBNETS)
    for a, r in zip(add, remove):
        assert a[a.index("-A")] == "-A" and r[r.index("-D")] == "-D"
        assert [x for x in a if x != "-A"] == [x for x in r if x != "-D"]
        assert "MASQUERADE" in a and "!" in a


def test_closed_lab_spawns_no_dns(tmp_path, monkeypatch):
    calls = []
    lab = NetLab(prefix="t0", base_port=1, workdir=tmp_path)
    monkeypatch.setattr(lab, "_spawn", lambda cmd, name: calls.append(cmd))
    lab._spawn_dhcp("ns", "tap", "203.0.113.1", "203.0.113.100", "203.0.113.200")
    assert not any(a.startswith("--dhcp-option=6") for a in calls[0])
    lab.internet = True
    lab._spawn_dhcp("ns", "tap", "203.0.113.1", "203.0.113.100", "203.0.113.200")
    assert "--dhcp-option=6,203.0.113.1" in calls[1]
