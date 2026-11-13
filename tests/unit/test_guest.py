import pytest

from shield_sil.guest import Guest
from shield_sil.profile import Profile


def test_openwrt_adapter_defaults():
    g = Guest.from_profile(Profile.load())
    assert g.kind == "openwrt" and g.has_hw_agent
    assert "ifstatus wan" in g.cfg["ipv4_command"].format(ifname="eth1", logical="wan")


def test_linux_adapter_with_profile_overrides():
    p = Profile.load(overrides={"guest.adapter": "linux", "guest.login_password": "pw",
                                "guest.hw_agent": False, "guest.ready_command": "systemctl is-system-running"})
    g = Guest.from_profile(p)
    assert g.kind == "linux" and not g.has_hw_agent
    assert g.cfg["login_password"] == "pw"
    assert g.cfg["ready_command"] == "systemctl is-system-running"
    assert g.cfg["ipv4_command"].format(ifname="eth1", logical="wan").startswith("ip -4 -o addr show dev eth1")


def test_unknown_adapter_rejected():
    with pytest.raises(ValueError):
        Guest.from_profile(Profile.load(overrides={"guest.adapter": "windows"}))
