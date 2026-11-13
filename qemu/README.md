# The SIL's QEMU

The `imx8mp` machine (Shield's i.MX 8M Plus) runs on upstream QEMU
`v10.2.1` with the patches in `patches/`. `build.sh` builds it from the
pinned upstream commit; the `./sil` container builds it into
`/opt/qemu-sil` (`docker/Dockerfile`), so nothing has to be installed by hand.

| Patch | What it adds | Why the SIL needs it |
| --- | --- | --- |
| 0001 | i.MX 8M Plus GPC and SRC models | TF-A's power-domain code and PSCI `CPU_ON` (secondary cores) run as on hardware instead of hanging on unimplemented registers |
| 0002 | GPC, SRC and a boot ROM mapped in the SoC | U-Boot asks the boot ROM which device it booted from (ROM API at `0x980`) |
| 0003 | i.MX 8M Plus `DIGPROG` value | U-Boot and Linux identify the SoC as an 8M Plus, not an 8MQ |
| 0004 | `sil-i2c-bridge`, `sil-gpio-bridge` | Shield's TPS25751/BQ25792, LEDs, button and named GPIOs sit on the SoC's own I2C and GPIO controllers, answered by the SIL's Python models |
| 0005 | `imx8mp-evk` options `emmc-slot`, `emmc-boot-partition-size` | the eMMC (with boot0/boot1) on uSDHC3, as `mmcblk2`, and reported as the boot device |
| 0006 | Auto CMD23 in the SDHCI core, mapped from the uSDHC `MIX_CTRL` bit | Linux writes to the eMMC with Auto CMD23; without it every multi-block write left the card busy and the root filesystem hung |
| 0007 | DesignWare PCIe passes inbound requests no iATU region claims | the PCIe NIC's DMA reaches RAM as on hardware (Linux programs no inbound regions on the 8M Plus), so the second GbE port carries traffic |
| 0008 | uSDHC `SYS_CTRL` clock bits read as reserved ones | U-Boot's i.MX SD driver leaves them 0, which QEMU took as the card clock being off; U-Boot found no eMMC |
| 0009-0011 | i.MX watchdog: write-once `WCR` bits kept from `WCR`, not `WICR`; reset clears the `WDT` lock. Backports of upstream QEMU c278e6dc, 328d4c56 and 98d21eef (after v10.2.1) | Linux's `imx2-wdt` lost `WDE` when it set the timeout, so a hung board never reset, and TF-A's system reset (`reboot`) did nothing |
| 0012 | No SDMA buffer boundary on the uSDHC | the 512 KiB boundary QEMU imposed stopped U-Boot's large reads (the kernel) with a DMA interrupt it never handles, so the board hung until the watchdog reset it; it showed once Linux had rewritten the kernel file (sysupgrade, slot switch) |
| 0013 | DesignWare PCIe decodes only the iATU `TYPE` field; I/O regions stay unmapped | Linux 7.0 (Ubuntu 26.04) sets bit 8 (TD) on its outbound regions, which QEMU took for a configuration window, so every register access to the PCIe NICs went to config space and `e1000e` failed to probe; Ubuntu had no WAN or management port |

What stays different from a Shield:

- **No boot ROM loader or SPL.** QEMU loads TF-A (`bl31.bin`) and U-Boot
  into memory and starts TF-A, which is where a Shield is after its SPL
  has trained the DRAM. DRAM training cannot run without a DDR controller.
- **Power switching is instant.** Power domains and cores power up as soon as
  they are asked to; there are no power-up delays.
- **One Ethernet controller.** QEMU models the FEC, not the EQoS, so Shield's
  second GbE port is a PCIe NIC.

The patches are written to upstream QEMU's coding style, so they can be sent
upstream later.
