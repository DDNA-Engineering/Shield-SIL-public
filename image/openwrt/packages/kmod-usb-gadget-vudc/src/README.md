# Vendored upstream sources

Unmodified files from linux-stable
(https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git), one
directory per kernel series the SIL's OpenWrt releases use:

| Dir | Tag | OpenWrt release |
|---|---|---|
| 6.6 | v6.6.104 | 24.10.3 |
| 6.12 | v6.12.85 | 25.12.3 |

Files, by upstream path:

- drivers/usb/gadget/: composite.c, config.c, configfs.c, configfs.h,
  epautoconf.c, functions.c, u_f.c, (6.6) u_f.h, u_os_desc.h, usbstring.c
  (libcomposite)
- drivers/usb/gadget/function/: f_ecm.c, u_ecm.h, u_ether.c, u_ether.h,
  u_ether_configfs.h (CDC Ethernet function, usb0)
- drivers/usb/usbip/: usbip_common.h, vudc.h, vudc_dev.c, vudc_main.c,
  vudc_rx.c, vudc_sysfs.c, vudc_transfer.c, vudc_tx.c (USB/IP virtual
  device controller)

They are built out of tree against the release's kernel with its SDK (the
armsr kernel has `USB_GADGET=y` but leaves these as unset). Licence:
GPL-2.0 (SPDX headers in each file).

To add a series: fetch the same files at the tag matching the release's
kernel, add the row here and rebuild.

SHA-256:

```
557abca0ea8f2151f5578e23d8109d4fd591726137f72132ca2e967b51a16acb  6.6/composite.c
985b5ddae034ffcc4db863aac1818c254eadf11a5e11284038084d30f8313f79  6.6/config.c
8228adfafbe1181bcdccbe479fc23467fc2e847310ab055c9df433aa42f99911  6.6/configfs.c
a680515bf7a3f2a897283ffd104d8b8482eed6ecbdaf694f3fdef71b5abecb52  6.6/configfs.h
a9136c72e145f6fa405b5db8518dd8b2964f6b713492857f2d1b0f535627d0dc  6.6/epautoconf.c
4a2b4156be48686ea10e633a8c90949e34c70954303432d380b82be0bfe04b07  6.6/f_ecm.c
502b7437f8a477473e445624b36d828f56a2e16bd3a9a6008c3c8aec93ab62d7  6.6/functions.c
25d9f987af0251ebd47942801ae96add8166ac790705886c8cce9409452d89f9  6.6/u_ecm.h
50673d148a5638a63f7e5177f66752620213c291b8b32e57626c924181ae8dfe  6.6/u_ether.c
6964486dba46aa5ca1db9b72fedefede8be073ed15a42649154605dbc29e69e4  6.6/u_ether.h
bbd1a651edf2147f006a3acf5c5931c9ee71a974b70ba419c93ef1fedb62f24c  6.6/u_ether_configfs.h
bdee7b59e208c201271193d52fc4b244dd75e53e7fb8b8634b564378b6c5853e  6.6/u_f.c
6f2fb5284f86480111ae447af522e923946782bbc114f5f47614f5c6c9b72137  6.6/u_f.h
f3f6260fe3883aa9f90d377c9aa6948c2f8a6fa0812a750f5a8cca98f20f28ff  6.6/u_os_desc.h
19d22e2f8642c2fdbdd913a7e523ca487e40f04d6cc920e35de69f9f232d5b20  6.6/usbip_common.h
44ffb9a0e06af49e967658e1ff91129ae0b4561855571d5386551467b173e1fa  6.6/usbstring.c
7edb64aa579b1f29ccd7433dee9d4fa9d3df1c604d3bbd3ade64c799f449a303  6.6/vudc.h
33025d0028265248840365e6477e9edc4603e576e946657274b3fac60af86087  6.6/vudc_dev.c
49c59e99dc346d7dc64d8b4adb3a2df1ca0cff1ad8db82d9f047d382313d75fa  6.6/vudc_main.c
29025089da237844c6df30eb10076206231efe3af3141d4888bf5690e19062e4  6.6/vudc_rx.c
fb7a2add97dd1b0523e0201db0e01967f17e85acd925bef0b967b991b0541f74  6.6/vudc_sysfs.c
f9c5ba690a16556539f5cd99b1207661e86d132a4148733346ba4000445428e5  6.6/vudc_transfer.c
2edb66b03230a2ee0d676c9067081f4b0559a473b76b82d80fd383f23c357d01  6.6/vudc_tx.c
9bdb416a533a8e4be06b196ac873c40c2c5d114714053b03834738621d124e22  6.12/composite.c
985b5ddae034ffcc4db863aac1818c254eadf11a5e11284038084d30f8313f79  6.12/config.c
9f41fe5e78060865d161c364b74ce0608367fa2c77fd3f89e3cc92cad1952f7f  6.12/configfs.c
a680515bf7a3f2a897283ffd104d8b8482eed6ecbdaf694f3fdef71b5abecb52  6.12/configfs.h
a9136c72e145f6fa405b5db8518dd8b2964f6b713492857f2d1b0f535627d0dc  6.12/epautoconf.c
168236b805d8c42f9f7ac20735109adcaf5e39058110b4d33c239feb26268158  6.12/f_ecm.c
502b7437f8a477473e445624b36d828f56a2e16bd3a9a6008c3c8aec93ab62d7  6.12/functions.c
29bc94a4ad60ea37402ec68f299e36270e9d880d9670e31228ae54406917ebf5  6.12/u_ecm.h
5f6cbeaa36549b244705290203dcc124a673ee6c38f1325b415e6fc08cdef2c0  6.12/u_ether.c
00f3d36b1fa39debc86284aaf83e72f94e2c89e37374e0d2a1c3ba39eb44befd  6.12/u_ether.h
bbd1a651edf2147f006a3acf5c5931c9ee71a974b70ba419c93ef1fedb62f24c  6.12/u_ether_configfs.h
aeb2416e5d31aa6dc811e9a7a21e8d493bbf3ae5ea23280bf67aca69132eec5f  6.12/u_f.c
770e15abdf7903fb0ed61b4d7bc1a2b1c9482b87433a3999b8bf73ad5dc5fe39  6.12/u_os_desc.h
19d22e2f8642c2fdbdd913a7e523ca487e40f04d6cc920e35de69f9f232d5b20  6.12/usbip_common.h
44ffb9a0e06af49e967658e1ff91129ae0b4561855571d5386551467b173e1fa  6.12/usbstring.c
8ac3bbd08233bf25b17b6633197e74915b024f3c15a64ece8a8dedc9cb716115  6.12/vudc.h
78edeb5a3ec546133cd132d65e4b3a5de2c0f3ceb9985c299180598194926dd5  6.12/vudc_dev.c
343c32e1b09f386cc0445232dfb38cbf18ad1ac967338ca9893812eb4dd38d8d  6.12/vudc_main.c
29025089da237844c6df30eb10076206231efe3af3141d4888bf5690e19062e4  6.12/vudc_rx.c
fb7a2add97dd1b0523e0201db0e01967f17e85acd925bef0b967b991b0541f74  6.12/vudc_sysfs.c
f9c5ba690a16556539f5cd99b1207661e86d132a4148733346ba4000445428e5  6.12/vudc_transfer.c
2edb66b03230a2ee0d676c9067081f4b0559a473b76b82d80fd383f23c357d01  6.12/vudc_tx.c
```
