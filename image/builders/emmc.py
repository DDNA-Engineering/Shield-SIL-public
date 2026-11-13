#!/usr/bin/env python3
"""Assemble Shield's eMMC as a QEMU ``emmc`` device image (and, for OpenWrt, a sysupgrade file).

The layouts are Shield's, per guest family (GPT on the eMMC user area):

OpenWrt (``--layout openwrt``)::

    p1 config         2 MiB   provisioning data (left blank in the SIL)
    p2 boot_rescu    32 MiB   rescue (flashed blank at the factory)
    p3 rootfs_rescu 224 MiB
    p4 boot_a       256 MiB   slot A: ext4 "kernel" with Image, boot.scr (and the DTB) (distro_bootpart=4)
    p5 rootfs_a     512 MiB   slot A: squashfs root, f2fs overlay after it (OpenWrt fstools)
    p6 boot_b       256 MiB   slot B (distro_bootpart=6)
    p7 rootfs_b     512 MiB
    p8 data       25600 MiB   ext4 "data", mounted at /root/data

Ubuntu (``--layout ubuntu``)::

    p1 boot         256 MiB   FAT "boot": Image, initrd.img, boot.scr (and the DTB)
    p2 rootfs      4096 MiB   ext4 "rootfs"
    p3 cidata        32 MiB   FAT "cidata" (cloud-init seed; empty)
    p4 data        the rest   ext4 "data", mounted at /root/data

The factory image has the same system in slot A and slot B, and an empty ext4
file system on "data" (Shield's mount-data makes one on first use if there is
none; doing it at build time saves every SIL run that minute under emulation). U-Boot's
environment (one copy, 16 KiB at 0x3F0000 in hardware boot partition boot1) is
written with the default environment, as a factory programming would.

Partition GUIDs follow OpenWrt's convention (the disk GUID with the partition
number as the last byte), so ``root=PARTUUID=...`` names the partition and
OpenWrt's upgrade scripts find the disk.

With ``--dtb`` (the i.MX 8M Plus machine) the kernel's device tree goes into the
boot partition(s) under its own name, where U-Boot's ``fdtfile`` points, and
into the OpenWrt upgrade file as sysupgrade-shield/dtb. On QEMU virt the kernel
uses U-Boot's device tree, so there is none.

QEMU's emmc image is boot0, boot1, then the user area. The user area must be a
power of two, so the file is ``2 * boot + user`` bytes (sparse).

Usage:
  emmc.py --layout openwrt --kernel Image --rootfs root.squashfs --boot-script boot.scr \\
          --uboot-env u-boot-env.txt --out DIR [--dtb shield-sil.dtb] [--user-gib 32] [--boot-mib 4]
  emmc.py --layout ubuntu --kernel Image --initrd initrd.img --rootfs rootfs.ext4 \\
          --boot-script boot.scr --uboot-env u-boot-env.txt --out DIR [--dtb shield-sil.dtb]

Writes DIR/emmc.img, and for OpenWrt DIR/sysupgrade.tar (sysupgrade-shield/kernel,
sysupgrade-shield/root and, with --dtb, sysupgrade-shield/dtb; see /lib/upgrade/platform.sh in the image).
"""

from __future__ import annotations

import argparse
import os
import struct
import subprocess
import tarfile
import tempfile
import uuid
import zlib
from pathlib import Path

MiB = 1 << 20
GiB = 1 << 30
SECTOR = 512
ENTRY_SIZE = 128
N_ENTRIES = 128
LAST_USABLE_PAD = 34 * SECTOR  # backup GPT

LINUX_FS = uuid.UUID("0fc63daf-8483-4772-8e79-3d69d8477de4")
EFI_BASIC_DATA = uuid.UUID("ebd0a0a2-b9e5-4433-87c0-68b6b72699c7")

# (name, size in bytes or None for "the rest", partition type)
LAYOUTS = {
    "openwrt": [
        ("config", 2 * MiB, LINUX_FS),
        ("boot_rescu", 32 * MiB, LINUX_FS),
        ("rootfs_rescu", 224 * MiB, LINUX_FS),
        ("boot_a", 256 * MiB, LINUX_FS),
        ("rootfs_a", 512 * MiB, LINUX_FS),
        ("boot_b", 256 * MiB, LINUX_FS),
        ("rootfs_b", 512 * MiB, LINUX_FS),
        ("data", 25600 * MiB, LINUX_FS),
    ],
    "ubuntu": [
        ("boot", 256 * MiB, EFI_BASIC_DATA),
        ("rootfs", 4096 * MiB, LINUX_FS),
        ("cidata", 32 * MiB, EFI_BASIC_DATA),
        ("data", None, LINUX_FS),
    ],
}
FIRST = 1 * MiB  # first partition, aligned
# U-Boot environment: CONFIG_ENV_MMC_EMMC_HW_PARTITION=2 (boot1), ENV_OFFSET, ENV_SIZE.
ENV_OFFSET, ENV_SIZE = 0x3F0000, 0x4000

# Stable identifiers, so every build of the same layout is byte-identical.
NS = uuid.UUID("5e1d0000-5111-4c00-8000-000000000000")


def disk_guid(layout: str) -> uuid.UUID:
    """Disk GUID ending in 00, as OpenWrt's image tools make it."""
    return uuid.UUID(uuid.uuid5(NS, "disk:" + layout).hex[:30] + "00")


def part_uuid(layout: str, number: int) -> uuid.UUID:
    """Partition GUID: the disk GUID with the partition number as the last byte."""
    return uuid.UUID(disk_guid(layout).hex[:30] + f"{number:02x}")


def partitions(layout: str, user_bytes: int) -> list[tuple[str, int, int, uuid.UUID]]:
    """[(name, start byte, size bytes, type)] within the user area."""
    out, off = [], FIRST
    end = (user_bytes - LAST_USABLE_PAD) // MiB * MiB
    for name, size, kind in LAYOUTS[layout]:
        if size is None:
            size = end - off
        out.append((name, off, size, kind))
        off += size
    if off > end:
        raise SystemExit(f"{layout} layout does not fit a {user_bytes // GiB} GiB eMMC user area")
    return out


def gpt(layout: str, user_bytes: int) -> tuple[bytes, bytes]:
    """(protective MBR + primary header + entries, backup entries + header)."""
    sectors = user_bytes // SECTOR
    entries = b""
    for i, (name, start, size, kind) in enumerate(partitions(layout, user_bytes), 1):
        entries += struct.pack("<16s16sQQQ72s", kind.bytes_le, part_uuid(layout, i).bytes_le,
                               start // SECTOR, (start + size) // SECTOR - 1, 0,
                               name.encode("utf-16-le"))
    entries = entries.ljust(ENTRY_SIZE * N_ENTRIES, b"\0")
    entries_crc = zlib.crc32(entries)
    entry_sectors = ENTRY_SIZE * N_ENTRIES // SECTOR

    def header(my_lba: int, alt_lba: int, entries_lba: int) -> bytes:
        fields = [b"EFI PART", 0x00010000, 92, 0, 0, my_lba, alt_lba, 34,
                  sectors - 34, disk_guid(layout).bytes_le, entries_lba, N_ENTRIES, ENTRY_SIZE, entries_crc]
        fmt = "<8sIIIIQQQQ16sQIII"
        raw = struct.pack(fmt, *fields)
        fields[3] = zlib.crc32(raw)
        return struct.pack(fmt, *fields).ljust(SECTOR, b"\0")

    mbr = bytearray(SECTOR)
    mbr[446:462] = struct.pack("<BBBBBBBBII", 0, 0, 2, 0, 0xEE, 0xFF, 0xFF, 0xFF, 1,
                               min(sectors - 1, 0xFFFFFFFF))
    mbr[510:512] = b"\x55\xaa"
    primary = bytes(mbr) + header(1, sectors - 1, 2) + entries
    backup = entries + header(sectors - 1, 1, sectors - 1 - entry_sectors)
    return primary, backup


def env_block(text: str) -> bytes:
    """A U-Boot environment block (single copy): CRC32, then NUL-separated
    ``name=value`` strings ending in two NULs, padded to ENV_SIZE."""
    env: dict[str, str] = {}
    for ln in text.splitlines():
        if ln and not ln.startswith("#"):
            name, _, value = ln.partition("=")
            env[name] = value  # a later definition wins, as when U-Boot imports it
    data = b"".join(f"{k}={v}".encode() + b"\0" for k, v in sorted(env.items())) + b"\0"
    size = ENV_SIZE - 4
    if len(data) > size:
        raise SystemExit(f"U-Boot environment is {len(data)} bytes, more than {size}")
    data = data.ljust(size, b"\0")
    return struct.pack("<I", zlib.crc32(data)) + data


def run(*cmd: str) -> None:
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL)


def ext_boot(files: dict[str, Path], size: int, out: Path, tmp: Path) -> None:
    """ext4 boot partition labelled "kernel" holding ``files``."""
    d = tmp / "bootdir"
    d.mkdir(exist_ok=True)
    for name, src in files.items():
        (d / name).write_bytes(src.read_bytes())
    out.unlink(missing_ok=True)
    run("mke2fs", "-q", "-F", "-t", "ext4", "-L", "kernel", "-d", str(d), "-E", "root_owner=0:0",
        str(out), f"{size // 1024}k")


def fat(label: str, files: dict[str, Path], size: int, out: Path) -> None:
    out.unlink(missing_ok=True)
    run("mkfs.vfat", "-F", "32", "-n", label.upper(), "-i", f"{zlib.crc32(label.encode()):08x}",
        "-C", str(out), str(size // 1024))
    for name, src in files.items():
        run("mcopy", "-i", str(out), str(src), f"::{name}")


def resized_ext4(src: Path, size: int, label: str, out: Path) -> None:
    """Copy an ext4 image, relabel it and resize it to exactly ``size`` bytes."""
    run("cp", "--sparse=always", str(src), str(out))
    subprocess.run(["e2fsck", "-fy", str(out)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    r = subprocess.run(["resize2fs", "-f", str(out), f"{size // 1024}K"], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"root filesystem does not fit the {size // MiB} MiB partition: {r.stderr.strip()}")
    os.truncate(out, size)
    run("tune2fs", "-L", label, str(out))
    run("e2fsck", "-fn", str(out))


def copy_sparse(src: Path, dst_fd: int, offset: int, limit: int) -> None:
    """Copy a (sparse) file into dst at offset, skipping all-zero blocks."""
    if src.stat().st_size > limit:
        raise SystemExit(f"{src} ({src.stat().st_size} bytes) is larger than its {limit // MiB} MiB partition")
    block = 1 << 20
    zero = bytes(block)
    with open(src, "rb") as f:
        pos = 0
        while chunk := f.read(block):
            if chunk != zero[: len(chunk)]:
                os.pwrite(dst_fd, chunk, offset + pos)
            pos += len(chunk)


def sysupgrade_tar(kernel: Path, root: Path, out: Path, dtb: Path | None = None) -> None:
    """OpenWrt sysupgrade file: a tar with sysupgrade-shield/kernel, /root (and /dtb)."""
    members = [(kernel, "sysupgrade-shield/kernel"), (root, "sysupgrade-shield/root")]
    if dtb:
        members.append((dtb, "sysupgrade-shield/dtb"))
    with tarfile.open(out, "w") as tar:
        for src, arc in members:
            info = tar.gettarinfo(str(src), arc)
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            info.mtime = 0
            with open(src, "rb") as fh:
                tar.addfile(info, fh)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--layout", required=True, choices=sorted(LAYOUTS))
    ap.add_argument("--kernel", required=True, type=Path, help="arm64 Image")
    ap.add_argument("--initrd", type=Path, help="initrd (ubuntu)")
    ap.add_argument("--rootfs", required=True, type=Path,
                    help="root filesystem image: squashfs (openwrt) or ext4 (ubuntu)")
    ap.add_argument("--boot-script", required=True, type=Path, help="boot.scr (image/uboot/build.sh)")
    ap.add_argument("--uboot-env", required=True, type=Path, help="default environment (name=value lines)")
    ap.add_argument("--dtb", type=Path,
                    help="kernel device tree for the boot partition(s) (i.MX 8M Plus machine)")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--user-gib", type=int, default=32)
    ap.add_argument("--boot-mib", type=int, default=4)
    a = ap.parse_args()

    user = a.user_gib * GiB
    if user & (user - 1):
        raise SystemExit("--user-gib must be a power of two (QEMU emmc)")
    boot = a.boot_mib * MiB
    if ENV_OFFSET + ENV_SIZE > boot:
        raise SystemExit("the U-Boot environment (boot1 at 0x3F0000) needs boot partitions of 4 MiB or more")
    if a.layout == "ubuntu" and not a.initrd:
        raise SystemExit("--layout ubuntu needs --initrd")
    a.out.mkdir(parents=True, exist_ok=True)
    img = a.out / "emmc.img"
    parts = {name: (start, size) for name, start, size, _ in partitions(a.layout, user)}

    dtb = {a.dtb.name: a.dtb} if a.dtb else {}
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        writes: list[tuple[Path, str]] = []  # (file, partition)
        if a.layout == "openwrt":
            kimg = t / "boot.img"
            ext_boot({"Image": a.kernel, "boot.scr": a.boot_script, **dtb}, parts["boot_a"][1], kimg, t)
            writes = [(kimg, "boot_a"), (a.rootfs, "rootfs_a"), (kimg, "boot_b"), (a.rootfs, "rootfs_b")]
        else:
            bimg, rimg, cimg = t / "boot.img", t / "rootfs.img", t / "cidata.img"
            fat("boot", {"Image": a.kernel, "initrd.img": a.initrd, "boot.scr": a.boot_script, **dtb},
                parts["boot"][1], bimg)
            resized_ext4(a.rootfs, parts["rootfs"][1], "rootfs", rimg)
            fat("cidata", {}, parts["cidata"][1], cimg)
            writes = [(bimg, "boot"), (rimg, "rootfs"), (cimg, "cidata")]

        dimg = t / "data.img"
        empty = t / "empty"
        empty.mkdir()
        run("mke2fs", "-q", "-F", "-t", "ext4", "-L", "data", "-d", str(empty), "-E", "root_owner=0:0",
            str(dimg), f"{parts['data'][1] // 1024}k")
        writes.append((dimg, "data"))

        img.unlink(missing_ok=True)
        fd = os.open(img, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            os.ftruncate(fd, 2 * boot + user)
            os.pwrite(fd, env_block(a.uboot_env.read_text()), boot + ENV_OFFSET)  # boot1
            primary, backup = gpt(a.layout, user)
            os.pwrite(fd, primary, 2 * boot)
            os.pwrite(fd, backup, 2 * boot + user - len(backup))
            for src, name in writes:
                start, size = parts[name]
                copy_sparse(src, fd, 2 * boot + start, size)
        finally:
            os.close(fd)

        if a.layout == "openwrt":
            sysupgrade_tar(a.kernel, a.rootfs, a.out / "sysupgrade.tar", a.dtb)
    print(f"{img}: {2 * boot + user} bytes (boot0/boot1 {boot} each, user {user}), layout {a.layout}")
    for i, (name, start, size, _) in enumerate(partitions(a.layout, user), 1):
        print(f"  p{i} {name:14s} {start:#12x} {size // MiB:6d} MiB  PARTUUID {part_uuid(a.layout, i)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
