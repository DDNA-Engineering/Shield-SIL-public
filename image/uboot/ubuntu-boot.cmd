# Shield SIL boot script (Ubuntu), on p1 "boot". Built with mkimage -T script.
# The upstream Ubuntu kernel has its eMMC drivers as modules, so the initrd is
# loaded too (Shield's own kernel boots without one). The network the initrd sets
# up is undone by shield-sil-initrd-net.service before NetworkManager starts.
# Works on both SIL machines: the environment names the eMMC (shield_mmc) and
# console, and on the i.MX 8M Plus the kernel's device tree (fdtfile) is on p1.
part uuid mmc ${shield_mmc}:2 rootuuid
echo "Shield: booting Ubuntu (root partition 2)"
load mmc ${shield_mmc}:1 ${kernel_addr_r} Image
setenv shield_initrd_addr 0x50000000
load mmc ${shield_mmc}:1 ${shield_initrd_addr} initrd.img
setenv shield_initrd_size ${filesize}
setenv shield_fdt ${fdtcontroladdr}
# The Ubuntu Image (~70 MiB) runs past the board's fdt_addr_r, so the device
# tree goes between the kernel and the initrd.
setenv shield_fdt_addr 0x4f000000
if test -n "${fdtfile}"; then
	load mmc ${shield_mmc}:1 ${shield_fdt_addr} ${fdtfile} && setenv shield_fdt ${shield_fdt_addr}
fi
setenv bootargs "root=PARTUUID=${rootuuid} rootwait ro console=${console} net.ifnames=0 biosdevname=0"
booti ${kernel_addr_r} ${shield_initrd_addr}:${shield_initrd_size} ${shield_fdt}
