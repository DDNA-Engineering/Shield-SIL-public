"""The emulated disk: Shield's eMMC (U-Boot A/B) or a plain virtio disk."""

import importlib.util
import tarfile
import struct
import uuid
import zlib

from shield_sil.profile import REPO_ROOT, Profile
from shield_sil.sil import ShieldSIL


def _args(tmp_path, **overrides):
    sil = ShieldSIL(profile=Profile.load(overrides=overrides), run_dir=tmp_path)
    return sil._storage_args()


def test_emmc_is_an_emmc_on_sdhci_with_a_watchdog(tmp_path):
    a = _args(tmp_path)
    assert "sdhci-pci" in a
    assert f"emmc,drive=emmc,boot-partition-size={4 << 20}" in a
    assert not any("if=virtio" in x for x in a)
    assert a[-4:] == ["-device", "i6300esb", "-action", "watchdog=reset"]


def test_virtio_storage_for_a_kernel_and_plain_disk(tmp_path):
    a = _args(tmp_path, **{"image.storage": "virtio", "image.kernel": "/k", "image.firmware": "",
                           "image.watchdog": False})
    assert a == ["-drive", f"file={tmp_path / 'disk.qcow2'},if=virtio,format=qcow2"]


def _emmc_module():
    spec = importlib.util.spec_from_file_location("emmc", REPO_ROOT / "image" / "builders" / "emmc.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _gpt_entries(primary: bytes) -> list[tuple[str, int, int, str]]:
    out = []
    for i in range(128):
        e = primary[1024 + 128 * i: 1024 + 128 * (i + 1)]
        if e[:16] == bytes(16):
            break
        first, last = struct.unpack_from("<QQ", e, 32)
        guid = str(uuid.UUID(bytes_le=e[16:32]))
        out.append((e[56:].decode("utf-16-le").rstrip("\0"), first * 512, (last + 1 - first) * 512, guid))
    return out


def test_openwrt_emmc_is_shields_layout():
    m = _emmc_module()
    user = 32 << 30
    primary, backup = m.gpt("openwrt", user)
    assert primary[510:512] == b"\x55\xaa" and primary[512:520] == b"EFI PART"
    hdr = primary[512:512 + 92]
    assert zlib.crc32(hdr[:16] + b"\0\0\0\0" + hdr[20:]) == struct.unpack_from("<I", hdr, 16)[0]
    parts = _gpt_entries(primary)
    mib = 1 << 20
    assert [(n, size // mib) for n, _, size, _ in parts] == [
        ("config", 2), ("boot_rescu", 32), ("rootfs_rescu", 224), ("boot_a", 256), ("rootfs_a", 512),
        ("boot_b", 256), ("rootfs_b", 512), ("data", 25600)]
    # Contiguous, from 1 MiB.
    assert parts[0][1] == mib
    assert all(a[1] + a[2] == b[1] for a, b in zip(parts, parts[1:]))
    # OpenWrt's PARTUUID convention: the disk GUID with the partition number last.
    disk = str(uuid.UUID(bytes_le=hdr[56:72]))
    assert disk.endswith("00")
    assert [g for *_, g in parts] == [disk[:-2] + f"{i:02x}" for i in range(1, 9)]
    assert backup[-512:].startswith(b"EFI PART")
    assert struct.unpack_from("<Q", backup[-512:], 24)[0] == user // 512 - 1


def test_ubuntu_emmc_is_shields_ubuntu_layout():
    m = _emmc_module()
    parts = _gpt_entries(m.gpt("ubuntu", 32 << 30)[0])
    mib = 1 << 20
    assert [n for n, *_ in parts] == ["boot", "rootfs", "cidata", "data"]
    assert [size // mib for _, _, size, _ in parts[:3]] == [256, 4096, 32]
    # data takes the rest of the 32 GiB user area, short of the backup GPT.
    assert parts[3][1] + parts[3][2] > (32 << 30) - 2 * mib


def test_uboot_environment_is_one_copy_in_boot1():
    m = _emmc_module()
    blk = m.env_block("altbootcmd=\nbootlimit=3\naltbootcmd=run x\n# comment\n")
    assert len(blk) == m.ENV_SIZE == 0x4000 and m.ENV_OFFSET == 0x3F0000
    assert struct.unpack("<I", blk[:4])[0] == zlib.crc32(blk[4:])
    assert blk[4:].rstrip(b"\0") == b"altbootcmd=run x\0bootlimit=3"
    cfg = (REPO_ROOT / "image" / "uboot" / "shield-sil.config").read_text()
    for line in ("CONFIG_ENV_MMC_EMMC_HW_PARTITION=2", "CONFIG_ENV_OFFSET=0x3F0000", "CONFIG_ENV_SIZE=0x4000"):
        assert line in cfg.splitlines()
    assert "REDUND" not in cfg and "BOOTCOUNT" not in cfg
    fw_env = (REPO_ROOT / "image" / "overlay" / "openwrt-emmc" / "etc" / "fw_env.config").read_text()
    assert "/dev/mmcblk0boot1     0x3F0000  0x4000" in fw_env


def test_a_direct_kernel_boot_leaves_out_the_firmware(tmp_path):
    sil = ShieldSIL(profile=Profile.load(overrides={"image.storage": "virtio", "image.kernel": "/k"}),
                    run_dir=tmp_path)
    cmd = sil._qemu_cmd()
    assert "-kernel" in cmd and "-bios" not in cmd
    emmc = ShieldSIL(profile=Profile.load(), run_dir=tmp_path)._qemu_cmd()
    assert "-bios" in emmc and "-kernel" not in emmc


def test_sysupgrade_file_carries_the_dtb_only_when_given(tmp_path):


    m = _emmc_module()
    kernel, root, dtb = (tmp_path / n for n in ("Image", "root.squashfs", "shield-sil.dtb"))
    for f in (kernel, root, dtb):
        f.write_bytes(f.name.encode())
    m.sysupgrade_tar(kernel, root, tmp_path / "virt.tar")
    m.sysupgrade_tar(kernel, root, tmp_path / "imx.tar", dtb)
    with tarfile.open(tmp_path / "virt.tar") as t:
        assert t.getnames() == ["sysupgrade-shield/kernel", "sysupgrade-shield/root"]
    with tarfile.open(tmp_path / "imx.tar") as t:
        assert t.getnames()[-1] == "sysupgrade-shield/dtb"
        assert t.extractfile("sysupgrade-shield/dtb").read() == b"shield-sil.dtb"
