# Shield SIL: sysupgrade on Shield's eMMC, as Shield's firmware does it: the
# RUNNING slot is upgraded in place. The new kernel goes into the slot's boot
# partition and the new squashfs root over its root partition (which also
# clears the old f2fs overlay after it); the saved configuration goes to the
# boot partition and /lib/preinit/79_move_config restores it at the next boot.
# The U-Boot environment is not touched: switching between slots A and B is a
# separate, manual step (fw_setenv distro_bootpart 4|6, then reboot).
#
# Upgrade file: a tar with sysupgrade-shield/kernel (arm64 Image),
# sysupgrade-shield/root (squashfs) and, on the i.MX 8M Plus machine,
# sysupgrade-shield/dtb (the kernel's device tree, saved under the name
# the slot's DTB has, which is U-Boot's fdtfile), as image/builders/emmc.py writes it.

. /lib/upgrade/shield.sh

RAMFS_COPY_DATA="/lib/upgrade/shield.sh"
REQUIRE_IMAGE_METADATA=0

platform_check_image() {
	[ "$#" -gt 1 ] && return 1
	local f
	for f in kernel root; do
		tar tf "$1" "sysupgrade-shield/$f" >/dev/null 2>&1 || {
			v "Not a Shield upgrade file: sysupgrade-shield/$f missing"
			return 1
		}
	done
	shield_slot_devices || {
		v "Cannot tell the running slot (no root=PARTUUID= on the kernel command line)"
		return 1
	}
}

platform_do_upgrade() {
	shield_slot_devices || return 1
	v "Shield: upgrading the running slot: boot /dev/$SHIELD_BOOTDEV, root /dev/$SHIELD_ROOTDEV"
	mount -t ext4 -o rw,noatime "/dev/$SHIELD_BOOTDEV" /mnt || return 1
	tar xOf "$1" sysupgrade-shield/kernel > /mnt/Image.new && mv -f /mnt/Image.new /mnt/Image
	local rc=$?
	if [ "$rc" = 0 ] && tar tf "$1" sysupgrade-shield/dtb >/dev/null 2>&1; then
		# Keep the name the slot's DTB already has (U-Boot's fdtfile).
		# Shell only: the upgrade ramdisk has no head.
		local dtb=shield-sil.dtb f
		for f in /mnt/*.dtb; do
			[ -e "$f" ] && { dtb=${f##*/}; break; }
		done
		tar xOf "$1" sysupgrade-shield/dtb > "/mnt/$dtb.new" && mv -f "/mnt/$dtb.new" "/mnt/$dtb"
		rc=$?
	fi
	umount /mnt
	[ "$rc" = 0 ] || return 1
	tar xOf "$1" sysupgrade-shield/root | dd of="/dev/$SHIELD_ROOTDEV" bs=1M conv=fsync 2>/dev/null || return 1
	# Blank the start of the old overlay. fstools looks for it right after the
	# squashfs, rounded up to 64 KiB; with no file system there it makes a new
	# f2fs overlay at the next boot.
	local size=$(tar xOf "$1" sysupgrade-shield/root | wc -c)
	dd if=/dev/zero of="/dev/$SHIELD_ROOTDEV" bs=64k seek=$(( (size + 65535) / 65536 )) count=1 conv=fsync 2>/dev/null || return 1
	sync
}

platform_copy_config() {
	shield_slot_devices || return 1
	mount -t ext4 -o rw,noatime "/dev/$SHIELD_BOOTDEV" /mnt && {
		cp -af "$UPGRADE_BACKUP" "/mnt/$BACKUP_FILE"
		umount /mnt
	}
}
