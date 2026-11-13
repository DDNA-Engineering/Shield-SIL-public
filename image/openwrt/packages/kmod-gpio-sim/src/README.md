# Vendored upstream sources

Unmodified files from linux-stable
(https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git), one
directory per kernel series the SIL's OpenWrt releases use:

| Dir | Tag | OpenWrt release |
|---|---|---|
| 6.6 | v6.6.104 | 24.10.3 |
| 6.12 | v6.12.85 | 25.12.3 |

Files: drivers/gpio/gpio-sim.c, kernel/irq/irq_sim.c, and (6.6 only)
drivers/gpio/gpiolib.h. They are patched at build time only by
../patches/<series>/ (irq_sim linked into the module; released outputs keep
their level, as on a real SoC). Licence: GPL-2.0 (SPDX headers in each file).

To add a series: fetch the three files at the tag matching the release's
kernel (see `kmods/` under the release on downloads.openwrt.org), add the row
here, copy ../patches/<previous series>/ to ../patches/<series>/ and refresh
them, and rebuild.

SHA-256:

```
d5768effca6f54344dc6bb2ddc709a27c56bb2f1d4c6ce30575dc87a5747d6c3  6.6/gpio-sim.c
9f2ebfa07c76ab5670d07a3e8d825bfc55e888265e12584b07e4a0ce1bc6bb80  6.6/gpiolib.h
6ec020f70dbe27a0bfb5de1a32e13ca424a95b1e09dd12c20b6edf1cdf5a2ed8  6.6/irq_sim.c
e97b27cb38d9f13245eafd983bbc8d2b97837e052d4a7c65c7960eefd026b909  6.12/gpio-sim.c
808a37813c4f58a567b22f30c408b912042e137572e7e143f1f59274387de879  6.12/irq_sim.c
```
