# Shield SIL boot script (OpenWrt), on each slot's boot partition. Built with
# mkimage -T script by image/uboot/build.sh. Works on both SIL machines: the
# environment names the eMMC (shield_mmc) and console, and on the i.MX 8M Plus
# the kernel's device tree (fdtfile) comes from the same boot partition.
setexpr rootpart ${distro_bootpart} + 1
part uuid mmc ${shield_mmc}:${rootpart} rootuuid
echo "Shield: booting partition ${distro_bootpart} (root partition ${rootpart})"
load mmc ${shield_mmc}:${distro_bootpart} ${kernel_addr_r} Image
setenv shield_fdt ${fdtcontroladdr}
if test -n "${fdtfile}"; then
	load mmc ${shield_mmc}:${distro_bootpart} ${fdt_addr_r} ${fdtfile} && setenv shield_fdt ${fdt_addr_r}
fi
setenv bootargs "root=PARTUUID=${rootuuid} rootwait rootfstype=squashfs,f2fs console=${console}"
booti ${kernel_addr_r} - ${shield_fdt}
