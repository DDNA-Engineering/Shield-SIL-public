"""The emulated Shield must not have more compute than the real board."""

import re

import pytest


def test_cpu_is_four_cortex_a53(shield):
    info = shield.sh("cat /proc/cpuinfo")
    parts = re.findall(r"CPU part\s*:\s*(0x[0-9a-f]+)", info)
    assert parts == ["0xd03"] * shield.profile["compute"]["cpu_cores"]  # 0xd03 = Cortex-A53


def test_ram_does_not_exceed_profile(shield):
    kb = int(re.search(r"MemTotal:\s+(\d+)", shield.sh("cat /proc/meminfo")).group(1))
    ram_mb = shield.profile["compute"]["ram_mb"]
    assert ram_mb * 1024 * 0.9 < kb <= ram_mb * 1024


def test_storage_matches_emmc_size(shield):
    dev = shield.emmc_dev if shield.profile["image"].get("storage") == "emmc" else "vda"
    blocks = int(shield.sh(f"cat /sys/block/{dev}/size"))  # 512-byte sectors (eMMC: user area)
    assert blocks * 512 == shield.profile["compute"]["emmc_gb"] * 1024**3


def test_host_cpu_is_capped(shield):
    assert shield.cpucap is not None and shield.cpucap.method != "none"


def test_sil_is_not_faster_than_device(shield):
    """One emulated core is no faster than one Shield core (Dhrystone, docs/calibration.md)."""
    ref = shield.profile["compute"]["cpu_cap"].get("device_dmips_per_core", 0)
    if not ref:
        pytest.skip("no device_dmips_per_core in the profile (docs/calibration.md)")
    if not shield.has_dhrystone():
        pytest.skip("image has no dhrystone (speed probe)")
    dmips = shield.dmips()
    assert dmips <= ref * 1.05, (
        f"a SIL core runs {dmips:.0f} DMIPS, a Shield core {ref}: speed limit not applied "
        f"({shield.speed})")
