"""Shield's eMMC: GPT layout, U-Boot boot, data partition, watchdog, and on
OpenWrt the in-place sysupgrade and the manual A/B slot switch.

These tests reboot and reflash the board, so they run on their own emulated
Shield (instance 1), not the shared one. Order matters on OpenWrt: each test
starts from the state the previous one left.
"""

import functools
import http.server
import re
import threading

import pytest

from shield_sil.profile import Profile
from shield_sil.sil import ShieldSIL

LAYOUT = {
    "openwrt": ["config", "boot_rescu", "rootfs_rescu", "boot_a", "rootfs_a", "boot_b", "rootfs_b", "data"],
    "linux": ["boot", "rootfs", "cidata", "data"],
}
BOOT_S = 900  # one boot under TCG with the CPU cap, generous


@pytest.fixture(scope="module")
def board():
    profile = Profile.from_env()
    if profile["image"].get("storage") != "emmc":
        pytest.skip("image does not boot from Shield's eMMC (image.storage != emmc)")
    sil = ShieldSIL(profile=profile, instance=1)
    sil.start()
    yield sil
    sil.stop()


@pytest.fixture(scope="module")
def upgrade_url(board):
    """sysupgrade.tar served to the guest (QEMU user-net: host 127.0.0.1 is 10.0.2.2)."""
    src = board.profile.image_path("disk").parent
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(src))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://10.0.2.2:{httpd.server_address[1]}/sysupgrade.tar"
    httpd.shutdown()


def rootpart(board) -> int:
    """The root partition number, from root=PARTUUID=<disk GUID ...NN> (U-Boot's boot.scr)."""
    m = re.search(r"root=PARTUUID=[0-9a-f-]+([0-9a-f]{2})\b", board.sh("cat /proc/cmdline"))
    assert m, "kernel command line has no root=PARTUUID="
    return int(m.group(1), 16)


def set_slot(board, bootpart: int) -> None:
    """Switch slots as Shield's advanced reboot does: boot1 is read-only until unlocked."""
    dev = board.emmc_dev
    board.sh(f"echo 0 > /sys/block/{dev}boot1/force_ro && "
             f"fw_setenv distro_bootpart {bootpart}; rc=$?; "
             f"echo 1 > /sys/block/{dev}boot1/force_ro; exit $rc")


def reboot(board) -> None:
    mark = board.console.mark()
    board.console.send("reboot\n")
    board.wait_rebooted(mark, timeout=BOOT_S)


def test_emmc_has_shields_partitions(board):
    dev = board.emmc_dev
    assert board.sh(f"test -b /dev/{dev}boot0 && test -b /dev/{dev}boot1 && echo ok") == "ok"
    want = LAYOUT[board.guest.kind]
    names = board.sh(f"for i in {' '.join(str(i) for i in range(1, len(want) + 1))}; do "
                     f"sed -n 's/^PARTNAME=//p' /sys/class/block/{dev}p$i/uevent; done").split()
    assert names == want


def test_data_partition_is_mounted_at_root_data(board):
    data = len(LAYOUT[board.guest.kind])
    assert re.search(rf"^/dev/{board.emmc_dev}p{data} on /root/data type ext4", board.sh("mount"), re.M)


def test_board_has_a_watchdog(board):
    assert board.sh("test -c /dev/watchdog && echo ok") == "ok"


# --- Ubuntu -------------------------------------------------------------------

@pytest.mark.guest("linux")
def test_ubuntu_boots_from_its_rootfs_partition(board):
    assert rootpart(board) == 2
    assert re.search(rf"^/dev/{board.emmc_dev}p2 on / type ext4", board.sh("mount"), re.M)
    assert "Shield: booting Ubuntu" in board.console.text()


# --- OpenWrt ------------------------------------------------------------------

@pytest.mark.guest("openwrt")
def test_factory_image_boots_slot_a(board):
    assert rootpart(board) == 5
    assert board.sh("fw_printenv -n distro_bootpart") == "4"
    mounts = board.sh("mount")
    assert re.search(r"^/dev/root on /rom type squashfs", mounts, re.M)
    assert re.search(r"^overlayfs:/overlay on / type overlay", mounts, re.M)
    assert re.search(r" on /overlay type f2fs", mounts, re.M)


@pytest.mark.guest("openwrt")
def test_watchdog_resets_a_hung_board(board):
    assert '"status": "running"' in board.sh("ubus call system watchdog")
    mark = board.console.mark()
    # procd stops feeding the watchdog but keeps it open: a hung system.
    board.console.send("ubus call system watchdog '{\"stop\": true}'\n")
    board.wait_rebooted(mark, timeout=BOOT_S)
    assert "Shield: booting partition 4" in board.console.text_since(mark)
    assert rootpart(board) == 5


@pytest.mark.guest("openwrt")
def test_sysupgrade_rewrites_the_running_slot_and_keeps_config_and_data(board, upgrade_url):
    board.sh("uci set system.@system[0].description='kept across sysupgrade' && uci commit system")
    board.sh("echo kept > /root/data/marker && echo gone > /root/not-kept && sync")
    board.sh(f"wget -q -O /tmp/sysupgrade.tar {upgrade_url}", timeout=180)
    mark = board.console.mark()
    board.console.send("sysupgrade /tmp/sysupgrade.tar\n")
    board.wait_rebooted(mark, timeout=BOOT_S)
    # The same slot, rewritten: no slot switch, the environment untouched.
    assert rootpart(board) == 5
    assert board.sh("fw_printenv -n distro_bootpart") == "4"
    assert board.sh("uci get system.@system[0].description") == "kept across sysupgrade"
    assert board.sh("cat /root/data/marker") == "kept"
    # The overlay was replaced (only the saved configuration came back), and
    # the saved configuration is restored once.
    assert board.sh("test -e /root/not-kept || echo gone") == "gone"
    assert board.sh("test -e /sysupgrade.tgz || echo gone") == "gone"


@pytest.mark.guest("openwrt")
def test_environment_is_read_only_until_unlocked(board):
    assert board.sh(f"cat /sys/block/{board.emmc_dev}boot1/force_ro") == "1"
    assert "Operation not permitted" in board.sh("fw_setenv distro_bootpart 6", check=False)
    assert board.sh("fw_printenv -n distro_bootpart") == "4"


@pytest.mark.guest("openwrt")
def test_manual_slot_switch_boots_slot_b_and_back(board):
    set_slot(board, 6)
    reboot(board)
    assert rootpart(board) == 7
    assert board.sh("fw_printenv -n distro_bootpart") == "6"
    # Slot B has its own overlay: the configuration made on slot A is not there.
    assert board.sh("uci -q get system.@system[0].description || echo none") == "none"
    assert board.sh("cat /root/data/marker") == "kept"  # data is shared
    set_slot(board, 4)
    reboot(board)
    assert rootpart(board) == 5
    assert board.sh("uci get system.@system[0].description") == "kept across sysupgrade"
