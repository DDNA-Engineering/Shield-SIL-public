# Shield SIL: the running slot on Shield's eMMC (image/builders/emmc.py).
#   p4 boot_a / p5 rootfs_a   p6 boot_b / p7 rootfs_b
# U-Boot boots root=PARTUUID=<disk GUID with the partition number as last byte>;
# the slot's boot partition is the one before its root partition.

# The running root's partition UUID and number.
shield_rootuuid() {
	local arg
	for arg in $(cat /proc/cmdline); do
		case "$arg" in
			root=PARTUUID=????????-????-????-????-????????????) echo "${arg#root=PARTUUID=}"; return 0 ;;
		esac
	done
	return 1
}

shield_rootpart() {
	local uuid
	uuid=$(shield_rootuuid) || return 1
	printf '%d\n' "0x${uuid#${uuid%??}}"
}

# shield_slot_devices: SHIELD_ROOTDEV and SHIELD_BOOTDEV (names under /dev) of
# the running slot. The disk is the one whose GPT disk GUID is the root's
# PARTUUID with 00 as the last byte (read from the GPT header, LBA 1).
shield_slot_devices() {
	local uuid part want disk d p
	uuid=$(shield_rootuuid) || return 1
	part=$(shield_rootpart) || return 1
	want="${uuid%??}00"
	for d in /sys/class/block/*; do
		[ -e "$d/partition" ] && continue
		[ -b "/dev/${d##*/}" ] || continue
		set -- $(dd if="/dev/${d##*/}" bs=1 skip=568 count=16 2>/dev/null |
			hexdump -v -e '8/1 "%02x "" "2/1 "%02x""-"6/1 "%02x"')
		[ "$4$3$2$1-$6$5-$8$7-$9" = "$want" ] && disk=${d##*/} && break
	done
	[ -n "$disk" ] || return 1
	case "$disk" in *[0-9]) p="${disk}p" ;; *) p="$disk" ;; esac
	[ -b "/dev/$p$part" ] && [ -b "/dev/$p$((part - 1))" ] || return 1
	export SHIELD_ROOTDEV="$p$part" SHIELD_BOOTDEV="$p$((part - 1))"
}
