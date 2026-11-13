import copy

import pytest

from shield_sil.profile import Profile


def test_default_profile_matches_documented_shield_compute():
    p = Profile.load()
    c = p["compute"]
    assert (c["cpu_model"], c["cpu_cores"], c["cpu_mhz"]) == ("cortex-a53", 4, 1800)
    assert c["ram_mb"] == 4096 and c["emmc_gb"] == 32


def test_cap_may_not_exceed_board_cores():
    p = Profile.load()
    data = copy.deepcopy(p.data)
    data["compute"]["cpu_cap"]["host_cores"] = 6
    with pytest.raises(ValueError, match="never get more compute"):
        Profile(path=p.path, data=data).validate()


def test_unknown_cap_method_rejected():
    p = Profile.load()
    data = copy.deepcopy(p.data)
    data["compute"]["cpu_cap"]["method"] = "magic"
    with pytest.raises(ValueError):
        Profile(path=p.path, data=data).validate()


def test_overrides_swap_the_image_without_editing_the_file(tmp_path):
    img = tmp_path / "other.img"
    p = Profile.load(overrides={"image.disk": str(img), "image.kernel": "", "image.firmware": "/fw.fd"})
    assert p.image_path("disk") == img
    assert p.image_path("kernel") is None
    # The file on disk is untouched.
    assert Profile.load()["image"]["disk"] == "build/images/openwrt-24.10.3/emmc.img"


def test_relative_image_paths_resolve_from_repo_root():
    from shield_sil.profile import REPO_ROOT

    assert Profile.load().image_path("disk") == REPO_ROOT / "build" / "images" / "openwrt-24.10.3" / "emmc.img"


def test_env_overrides(monkeypatch):
    from shield_sil.profile import env_overrides

    monkeypatch.setenv("SIL_DISK", "/img.qcow2")
    monkeypatch.setenv("SIL_SET", "image.disk_format=qcow2;guest.boot_timeout_s=90;image.extra_args=[\"-s\"]")
    o = env_overrides()
    assert o == {"image.disk": "/img.qcow2", "image.disk_format": "qcow2",
                 "guest.boot_timeout_s": 90, "image.extra_args": ["-s"]}
    p = Profile.from_env()
    assert p["image"]["disk_format"] == "qcow2" and p["guest"]["boot_timeout_s"] == 90


def test_virt_disk_boot_needs_kernel_or_firmware():
    with pytest.raises(ValueError, match="firmware"):
        Profile.load(overrides={"image.storage": "virtio", "image.kernel": "", "image.firmware": ""})


def test_emmc_boots_through_firmware_not_a_kernel():
    with pytest.raises(ValueError, match="image.storage=virtio"):
        Profile.load(overrides={"image.kernel": "/k"})
    p = Profile.load(overrides={"image.storage": "virtio", "image.kernel": "/k", "image.firmware": "",
                                "image.disk": "/d.img"})
    assert p["image"]["storage"] == "virtio"


def test_unknown_storage_rejected():
    with pytest.raises(ValueError, match="storage"):
        Profile.load(overrides={"image.storage": "nvme"})


def test_bad_override_key_rejected():
    from shield_sil.profile import parse_set

    with pytest.raises(ValueError):
        parse_set(["nodot=1"])


# --- image catalog -----------------------------------------------------------

def test_catalog_offers_the_supported_images():
    from shield_sil import images

    assert {"openwrt-24.10.3", "openwrt-25.12.3", "ubuntu-26.04"} <= set(images.names())


@pytest.mark.parametrize("name", ["openwrt-23.05.6", "openwrt-24.10.3", "openwrt-25.12.3", "ubuntu-26.04"])
def test_every_catalog_image_loads_on_the_shield_board(name):
    p = Profile.load(image=name)
    assert p.image_name == name
    emmc = name in ("openwrt-24.10.3", "openwrt-25.12.3", "ubuntu-26.04")
    assert p["image"]["disk"] == f"build/images/{name}/{'emmc.img' if emmc else 'rootfs.ext4'}"
    assert p["image"].get("storage", "virtio") == ("emmc" if emmc else "virtio")
    # The board does not change with the image.
    assert p["compute"] == Profile.load()["compute"]


def test_default_image_is_openwrt_24_10_3():
    assert Profile.load().image_name == "openwrt-24.10.3"


def test_ubuntu_uses_the_linux_adapter_with_the_hw_agent():
    p = Profile.load(image="ubuntu-26.04")
    assert p["guest"]["adapter"] == "linux" and p["guest"]["hw_agent"] is True
    assert p.image_path("firmware").name == "u-boot.bin" and p.image_path("kernel") is None


def test_image_selected_by_env_and_overrides_still_apply(monkeypatch):
    monkeypatch.setenv("SIL_IMAGE", "openwrt-25.12.3")
    monkeypatch.setenv("SIL_DISK", "/other.img")
    p = Profile.from_env()
    assert p.image_name == "openwrt-25.12.3"
    assert p["image"]["disk"] == "/other.img"
    assert p["image"]["firmware"] == "build/images/openwrt-25.12.3/u-boot.bin"


def test_image_name_override_selects_image():
    assert Profile.load(overrides={"image.name": "ubuntu-26.04"}).image_name == "ubuntu-26.04"


def test_unknown_image_is_rejected_with_the_choices():
    with pytest.raises(ValueError, match="available: .*openwrt-24.10.3"):
        Profile.load(image="windows-95")


def test_empty_image_name_boots_the_profiles_own_image_settings(tmp_path):
    p = Profile.load(overrides={"image.name": "", "image.disk": str(tmp_path / "x.img"), "image.kernel": "/k"})
    assert p.image_name == ""
    assert p.image_path("disk") == tmp_path / "x.img"
