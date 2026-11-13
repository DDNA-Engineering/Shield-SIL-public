# Swapping the image

Any image can be booted in the SIL without code changes. Ready-made images are
picked by name from the catalog. Anything else can be pointed at directly.

## Picking an image from the catalog

| Name | What it is |
| --- | --- |
| `openwrt-24.10.3` | upstream OpenWrt 24.10.3 `armsr/armv8` + Shield defaults (the default) |
| `openwrt-25.12.3` | upstream OpenWrt 25.12.3 `armsr/armv8` + Shield defaults |
| `ubuntu-26.04` | Ubuntu Server 26.04 LTS arm64 cloud image set up as a Shield router |
| `openwrt-23.05.6` | upstream OpenWrt 23.05.6 `armsr/armv8` + Shield defaults |

```sh
image/build.sh --list                          # names, descriptions, built or not
sudo -E image/build.sh openwrt-25.12.3 ubuntu-26.04   # or --all
sudo -E shield-sil --image ubuntu-26.04 up
sudo -E python -m pytest tests/system --sil-image ubuntu-26.04
SIL_IMAGE=ubuntu-26.04 sudo -E python -m pytest tests/system
```

The default comes from `[image] name` in the profile. Each image builds into
`build/images/<name>/`, so they live side by side. Downloads are cached in
`build/cache/<name>/` and checked against the publisher's checksums.

| Image family | Built from | Shield setup |
| --- | --- | --- |
| OpenWrt | Docker Hub `openwrt/imagebuilder` (kernel) and `openwrt/rootfs` | `image/overlay/openwrt/`: UCI defaults, procd service, ubus plugin |
| Ubuntu | cloud-images.ubuntu.com root tarball + kernel, kernel modules from ports.ubuntu.com, pinned by `serial` | `image/overlay/ubuntu/`: NetworkManager LAN/WAN/cellular (ModemManager) with DHCP and NAT, hostapd, serial autologin, systemd services |

Both families get the hardware agent from `image/overlay/common/`.

The Ubuntu image turns off services that only cost boot time under emulation
(each run boots a fresh copy, so one-time work repeats every boot): cloud-init,
snapd, apt/update timers, plymouth, and AppArmor policy loading (compiling the
profiles takes ~45 s per boot under TCG). Tests that depend on AppArmor need it
re-enabled in `image/builders/ubuntu.sh`.

### Adding an image to the catalog

Add `config/images/<name>.toml`:

```toml
description = "One line for listings"

[build]
builder = "openwrt"            # image/builders/<builder>.sh
tag = "armsr-armv8-v24.10.4"   # every other key reaches the builder as an env var (TAG=...)

[image]                        # merged over the profile's [image]
kernel = "build/images/<name>/kernel.bin"
disk = "build/images/<name>/rootfs.ext4"
append = "root=/dev/vda rootwait console=ttyAMA0 noinitrd"

[guest]                        # merged over the profile's [guest]
adapter = "openwrt"
hw_agent = true
```

A new OpenWrt or Ubuntu release is usually only a new file. Another
distribution needs a builder script that writes the files named in `[image]`.

## Per run, no file changes

The default image boots from Shield's eMMC through U-Boot (`image.storage =
"emmc"`). To boot your own kernel and disk instead, also pass
`--set image.storage=virtio` (a kernel given directly is booted without U-Boot;
add `--set image.watchdog=false` if your kernel has no `i6300esb` driver).

```sh
# Interactive
sudo -E shield-sil --set image.storage=virtio --disk out/my-build.img --kernel out/Image up
sudo -E shield-sil --disk out/disk.qcow2 --set image.disk_format=qcow2 up

# Test suite
sudo -E python -m pytest tests/system --sil-set image.storage=virtio --sil-disk out/my-build.img --sil-kernel out/Image
sudo -E SIL_SET=image.storage=virtio SIL_DISK=out/my-build.img SIL_KERNEL=out/Image python -m pytest tests/system
```

| Option | Environment | Profile key |
| --- | --- | --- |
| `--disk` / `--sil-disk` | `SIL_DISK` | `image.disk` |
| `--kernel` / `--sil-kernel` | `SIL_KERNEL` | `image.kernel` (`""` boots the disk's bootloader) |
| `--guest` / `--sil-guest` | `SIL_GUEST` | `guest.adapter` |
| `--set k=v` / `--sil-set k=v` | `SIL_SET="k=v;k=v"` | any `section.key` |
| `--image` / `--sil-image` | `SIL_IMAGE` | `image.name` (catalog entry) |
| `--profile` / `--sil-profile` | `SIL_PROFILE` | the whole profile file |

Each run boots a copy-on-write overlay, so the source image is never modified.

These override the catalog image's settings. To boot only your own settings,
select no catalog image: `--set image.name=` (or `name = ""` in the profile).

## A disk image with its own bootloader

Set no kernel and give QEMU UEFI firmware:

```sh
sudo apt-get install -y qemu-efi-aarch64
sudo -E shield-sil --disk vendor.img --kernel "" \
  --set image.firmware=/usr/share/qemu-efi-aarch64/QEMU_EFI.fd up
```

## A non-OpenWRT Linux

Use the `linux` guest adapter. Its defaults (log in as root with no password,
`#`/`$` prompt, `ip` for addresses) can each be changed in `[guest]`:

```toml
[guest]
adapter = "linux"
login_user = "root"
login_password = "secret"
shell_prompt = "root@.*# $"
ready_command = "systemctl is-system-running --wait >/dev/null; true"
hw_agent = false
```

## A different board

Copy `config/shield-v1.5.toml` and change `[image]` (`qemu`, `machine`),
`[compute]` (`cpu_model`, `cpu_cores`, `ram_mb`, `emmc_gb`, CPU cap) and
`[network]` (LAN address, interface names). Then pass it with `--profile`.

## What gets skipped

Tests declare what they need, and the suite skips what an image can't do:

| Marker | Needs | Tests |
| --- | --- | --- |
| none | any image with a shell on the serial console | compute limits, LAN gateway, DHCP uplinks, internet, failover |
| `guest("openwrt")` | the `openwrt` adapter | LuCI, `uci`, `ubus` |
| `hw_agent` | `shield-hwd`/`shield-hw` in the image (`guest.hw_agent = true`) | battery, PD, thermal, LEDs, modem |

The hardware agent (`image/overlay/common/`) is POSIX shell with a Python
variant: `shield-hwd` uses the Python agent when the image has `python3` (it
coalesces backlogged readings and doesn't fork per record), and the shell loop
otherwise. `shield-hw` uses `flock` or OpenWrt's `lock`, and `jsonfilter`, `jq`
or `python3`, whichever the image has. The ubus object is OpenWrt only.
