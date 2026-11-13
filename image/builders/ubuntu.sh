#!/usr/bin/env bash
# Builder for Ubuntu Server (arm64) guest images.
#
# Called by image/build.sh with the image's catalog entry in the environment:
#   SIL_IMAGE   catalog name, e.g. ubuntu-26.04
#   OUT         output directory (absolute)
#   CACHE       download cache directory (absolute)
#   EMMC_GB     disk size in GB
#   STORAGE     "emmc": also build U-Boot and Shield's eMMC image  ([build] storage)
#   MACHINE     virt (default) or imx8mp: Shield's SoC on the SIL's QEMU, which
#               also needs TF-A and the SIL device tree for this kernel ([build] machine)
#   RELEASE     Ubuntu version, e.g. 26.04              ([build] release)
#   CODENAME    Ubuntu codename, e.g. resolute          ([build] codename)
#   SERIAL      cloud image serial, e.g. 20261016       ([build] serial; pins the build)
#   MIRROR      Ubuntu ports archive (default https://ports.ubuntu.com/ubuntu-ports)
#
# Sources, all official and checksum-verified:
#   cloud-images.ubuntu.com/releases/<codename>/release-<serial>/
#     ubuntu-<release>-server-cloudimg-arm64-root.tar.xz   root filesystem
#     unpacked/...-arm64-vmlinuz-generic                   the image's kernel
#     unpacked/...-arm64-initrd-generic                    its initrd (eMMC boot only:
#       the upstream kernel has the SDHCI/eMMC drivers as modules)
#   <mirror>/pool/.../linux-modules-<kernel release>_*.deb  that kernel's modules
#     (the cloud root tarball ships none; netfilter/NAT needs them). Its sha256
#     comes from the archive's Packages index.
#   <mirror> packages Shield's Ubuntu runs that the cloud image lacks
#     (NetworkManager, hostapd, wpa_supplicant, iw, i2c-tools, gpiod, usbip from
#     linux-tools-<kernel release>). The host's apt resolves them against the
#     image's own dpkg status and verifies them with the image's Ubuntu archive
#     keyring; they are installed by a provisioning boot of the image (dpkg runs
#     inside the arm64 guest under QEMU, offline), so no binfmt/qemu-user is
#     needed. The installed versions are recorded in image.json.
#
# The image is set up like the Shield: NetworkManager with br-lan (LAN gateway
# 192.168.100.1, DHCP and NAT) over eth0, USB-C usb0 and the Wi-Fi AP, wired WAN
# preferred over cellular (ModemManager on the emulated EM7595, wwan0), root shell on the serial console, the SIL
# hardware agent as a systemd service, and cloud-init, snapd and timers that only
# slow an emulated boot turned off. Boot is direct (no initrd): the Ubuntu kernel
# has virtio, ext4 and the PL011 console built in.
#
# Outputs in $OUT: kernel.bin (plain arm64 Image), rootfs.ext4 (sparse, EMMC_GB), image.json,
# provision.log (console of the provisioning boot)
set -euo pipefail

: "${SIL_IMAGE:?}" "${OUT:?}" "${CACHE:?}" "${EMMC_GB:?}"
: "${RELEASE:?set [build] release}" "${CODENAME:?set [build] codename}" "${SERIAL:?set [build] serial}"
MIRROR="${MIRROR:-https://ports.ubuntu.com/ubuntu-ports}"
MACHINE="${MACHINE:-virt}"
case "$MACHINE" in
	virt) ;;
	imx8mp) [ "${STORAGE:-}" = emmc ] || { echo "machine imx8mp needs [build] storage = \"emmc\"" >&2; exit 1; } ;;
	*) echo "unknown machine $MACHINE" >&2; exit 1 ;;
esac
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
BASE="https://cloud-images.ubuntu.com/releases/$CODENAME/release-$SERIAL"
STEM="ubuntu-$RELEASE-server-cloudimg-arm64"

for t in curl mke2fs e2fsck sha256sum tar xz zstd dpkg-deb depmod apt-get qemu-system-aarch64 \
	$([ "${STORAGE:-}" = emmc ] && echo resize2fs tune2fs mkfs.vfat mcopy python3); do
	command -v "$t" >/dev/null || { echo "missing tool: $t (apt-get install curl e2fsprogs xz-utils zstd dpkg kmod apt qemu-system-arm)" >&2; exit 1; }
done
if [ "$(id -u)" != 0 ]; then
	# File owners, setuid bits and capabilities in the root filesystem need root.
	echo "the Ubuntu image must be built as root: sudo -E image/build.sh $SIL_IMAGE" >&2
	exit 1
fi
mkdir -p "$OUT" "$CACHE"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

sha() { sha256sum < "$1" | cut -d' ' -f1; }

# get <url> <file> <sha256>: download into the cache once, verify every time.
get() {
	local url="$1" file="$2" want="$3"
	if [ ! -f "$CACHE/$file" ] || [ "$(sha "$CACHE/$file")" != "$want" ]; then
		echo ">> downloading $file"
		curl -fsSL --retry 4 -o "$CACHE/$file.part" "$url"
		mv "$CACHE/$file.part" "$CACHE/$file"
	fi
	[ "$(sha "$CACHE/$file")" = "$want" ] || { echo "checksum mismatch: $file" >&2; rm -f "$CACHE/$file"; exit 1; }
}

# fetch <url-dir> <file>: a file listed in that directory's SHA256SUMS.
fetch() {
	local dir="$1" file="$2" sums="$CACHE/SHA256SUMS.$(echo "$1" | sha256sum | cut -c1-8)"
	[ -s "$sums" ] || curl -fsSL --retry 4 -o "$sums" "$dir/SHA256SUMS"
	local want
	want=$(awk -v f="$file" '{ n=$2; sub(/^\*/, "", n); if (n == f) print $1 }' "$sums")
	[ -n "$want" ] || { echo "$file is not listed in $dir/SHA256SUMS" >&2; exit 1; }
	get "$dir/$file" "$file" "$want"
}

fetch "$BASE" "$STEM-root.tar.xz"
fetch "$BASE/unpacked" "$STEM-vmlinuz-generic"
[ "${STORAGE:-}" != emmc ] || fetch "$BASE/unpacked" "$STEM-initrd-generic"
KVER=$("$ROOT/image/builders/arm64-kernel.py" --uname "$CACHE/$STEM-vmlinuz-generic")
echo ">> kernel $KVER"

# The modules package for exactly this kernel, from the archive's Packages indexes.
PKG="linux-modules-$KVER"
DEB_PATH="" DEB_SHA=""
for pocket in "$CODENAME-updates" "$CODENAME-security" "$CODENAME"; do
	idx="$CACHE/Packages.$pocket.xz"
	[ -s "$idx" ] || curl -fsSL --retry 4 -o "$idx" "$MIRROR/dists/$pocket/main/binary-arm64/Packages.xz"
	found=$(xz -dc "$idx" | awk -v p="$PKG" '
		/^Package: / { hit = ($2 == p); f = ""; s = "" }
		hit && /^Filename: / { f = $2 }
		hit && /^SHA256: / { s = $2 }
		hit && f && s { print f, s; exit }') || true  # awk exits early: xz gets SIGPIPE
	if [ -n "$found" ]; then
		DEB_PATH=${found% *}
		DEB_SHA=${found#* }
		break
	fi
done
[ -n "$DEB_PATH" ] || { echo "$PKG not found in $MIRROR for $CODENAME" >&2; exit 1; }
DEB="$(basename "$DEB_PATH")"
get "$MIRROR/$DEB_PATH" "$DEB" "$DEB_SHA"

echo ">> unpacking root filesystem and kernel modules"
R="$WORK/rootfs"
mkdir "$R"
tar -xJ --numeric-owner --xattrs --xattrs-include='*' -f "$CACHE/$STEM-root.tar.xz" -C "$R"
dpkg-deb -x "$CACHE/$DEB" "$R"
depmod -b "$R" "$KVER"

# Packages Shield's Ubuntu uses that the cloud image doesn't ship. usbip comes
# from linux-tools for exactly this kernel (Ubuntu has no separate usbip package).
EXTRA_PKGS="network-manager dnsmasq-base wpasupplicant hostapd iw wireless-regdb i2c-tools gpiod linux-tools-$KVER"
echo ">> resolving $EXTRA_PKGS"
A="$WORK/apt"
mkdir -p "$A/lists/partial" "$A/archives/partial" "$A/conf.d" "$A/prefs.d" "$CACHE/debs"
KEYRING="$R/usr/share/keyrings/ubuntu-archive-keyring.gpg"
for pocket in "$CODENAME" "$CODENAME-updates" "$CODENAME-security"; do
	echo "deb [arch=arm64 signed-by=$KEYRING] $MIRROR $pocket main universe"
done > "$A/sources.list"
APT=(apt-get -q -o APT::Architecture=arm64 -o APT::Architectures::=arm64
	-o Dir::Etc::SourceList="$A/sources.list" -o Dir::Etc::SourceParts=/nonexistent
	-o Dir::Etc::Parts="$A/conf.d" -o Dir::Etc::PreferencesParts="$A/prefs.d"
	-o Dir::State::Lists="$A/lists" -o Dir::State::status="$R/var/lib/dpkg/status"
	-o Dir::Cache="$A" -o Dir::Cache::archives="$CACHE/debs"
	-o APT::Install-Recommends=false -o APT::Sandbox::User=root -o Debug::NoLocking=1)
"${APT[@]}" update
"${APT[@]}" install --download-only -y $EXTRA_PKGS
# Copy exactly the set apt chose (new packages and any upgrades they need) into
# the image for the provisioning boot. "Inst <name> [<old>] (<version> ...".
mkdir -p "$R/var/cache/shield-sil/debs"
PKG_VERSIONS=""
while read -r name ver; do
	f=$(ls "$CACHE/debs/${name}_${ver/:/%3a}_"*.deb 2>/dev/null | head -n1)  # apt escapes the epoch's ':' 
	[ -n "$f" ] || { echo "downloaded package not found: $name $ver" >&2; exit 1; }
	cp "$f" "$R/var/cache/shield-sil/debs/"
	PKG_VERSIONS="$PKG_VERSIONS${PKG_VERSIONS:+ }$name=$ver"
done < <("${APT[@]}" -s install -y $EXTRA_PKGS |
	awk '$1 == "Inst" { for (i = 3; i <= NF; i++) if ($i ~ /^\(/) { v = substr($i, 2); break }; print $2, v }')
[ -n "$PKG_VERSIONS" ] || { echo "apt selected no packages" >&2; exit 1; }
echo ">> packages: $PKG_VERSIONS"

echo ">> applying Shield SIL overlay"
# Owned by root in the image whoever owns the checkout (a user's uid 1000 makes
# NetworkManager refuse its keyfiles as insecure).
cp -a --no-preserve=ownership "$ROOT/image/overlay/common/." "$ROOT/image/overlay/ubuntu/." "$R/"
chmod 0755 "$R/usr/sbin/shield-hwd" "$R/usr/sbin/shield-hw"

# Direct kernel boot from a single ext4 disk: no EFI partition to mount.
cat > "$R/etc/fstab" <<'EOF'
LABEL=rootfs  /  ext4  defaults,discard  0 1
EOF
printf '127.0.0.1 localhost\n127.0.1.1 Shield\n' > "$R/etc/hosts"

# The harness reads the serial console as plain text: no bracketed-paste
# escapes from bash, and no systemd shell-context (OSC 3008) escapes.
printf '\nset enable-bracketed-paste off\n' >> "$R/etc/inputrc"
rm -f "$R/etc/profile.d/80-systemd-osc-context.sh"
# ...and no colour: iproute2 and nmcli colour their output on a terminal.
echo 'export NO_COLOR=1' > "$R/etc/profile.d/00-shield-sil-nocolor.sh"

# Network: NetworkManager with the keyfiles from the overlay (as on Shield's
# Ubuntu), not netplan/cloud-init/systemd-networkd. NetworkManager refuses
# keyfiles that others can read.
rm -f "$R"/etc/netplan/*.yaml
mkdir -p "$R/etc/cloud"
touch "$R/etc/cloud/cloud-init.disabled"
chmod 0600 "$R"/etc/NetworkManager/system-connections/*.nmconnection

# Wi-Fi AP config from the same settings the OpenWrt images use.
. "$R/usr/share/shield-sil/wifi.conf"
mkdir -p "$R/etc/hostapd"
cat > "$R/etc/hostapd/shield-ap.conf" <<EOF
# Shield AP on the first simulated radio (generated from /usr/share/shield-sil/wifi.conf).
interface=$WIFI_AP_IFNAME
bridge=br-lan
driver=nl80211
ctrl_interface=/run/hostapd
ssid=$WIFI_SSID
country_code=$WIFI_COUNTRY
ieee80211d=1
hw_mode=g
channel=$WIFI_CHANNEL
ieee80211n=1
ieee80211ax=1
wmm_enabled=1
wpa=2
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
wpa_passphrase=$WIFI_KEY
EOF
chmod 0600 "$R/etc/hostapd/shield-ap.conf"

wants() {  # wants <target> <unit> [<unit path>]
	mkdir -p "$R/etc/systemd/system/$1.wants"
	ln -sf "${3:-/usr/lib/systemd/system/$2}" "$R/etc/systemd/system/$1.wants/$2"
}
for u in shield-hwd shield-board shield-usbip shield-usb-gadget shield-wifi-client shield-sil-boot-done mount-data; do
	wants multi-user.target "$u.service" "/etc/systemd/system/$u.service"
done
wants multi-user.target ModemManager.service
# The root console on whichever UART the machine has (virt: ttyAMA0, i.MX 8M Plus: ttymxc1).
for t in ttyAMA0 ttymxc1; do
	wants sysinit.target "shield-sil-console@$t.service" /etc/systemd/system/shield-sil-console@.service
done
for u in shield-sil-initrd-net shield-watchdog; do
	wants sysinit.target "$u.service" "/etc/systemd/system/$u.service"
done
# The AP starts when its radio's interface appears (renamed by 10-shield-wifi-ap.link).
wants 'sys-subsystem-net-devices-mwiphy0\x2dap0.device' shield-wifi-ap.service /etc/systemd/system/shield-wifi-ap.service

# Units that only slow a boot under emulation, or wait on things the SIL doesn't have.
# Every SIL run boots a fresh copy of the image, so one-time work (AppArmor policy
# compile ~45 s, ldconfig, dpkg backups) would repeat on every boot. AppArmor
# policy is therefore not loaded in the SIL image; see docs/swapping-images.md.
for u in snapd.service snapd.socket snapd.seeded.service snapd.apparmor.service \
	apt-daily.timer apt-daily-upgrade.timer unattended-upgrades.service motd-news.timer \
	man-db.timer fwupd-refresh.timer update-notifier-download.timer update-notifier-motd.timer \
	e2scrub_all.timer e2scrub_reap.service multipathd.service multipathd.socket \
	systemd-networkd-wait-online.service lxd-agent.service \
	systemd-networkd.service systemd-networkd.socket systemd-networkd-varlink.socket \
	NetworkManager-wait-online.service hostapd.service \
	apport.service sysstat.service sysstat-collect.timer sysstat-summary.timer udisks2.service \
	plymouth-quit-wait.service plymouth-quit.service plymouth-read-write.service \
	plymouth-start.service dpkg-db-backup.service dpkg-db-backup.timer netplan-configure.service \
	xfs_scrub_all.service xfs_scrub_all.timer networkd-dispatcher.service ldconfig.service \
	apparmor.service serial-getty@ttyAMA0.service serial-getty@ttymxc1.service getty@tty1.service keyboard-setup.service \
	console-setup.service setvtrgb.service lvm2-monitor.service rsyslog.service \
	fstrim.timer logrotate.timer; do
	ln -sf /dev/null "$R/etc/systemd/system/$u"
done

GIT_REV=$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)
cat > "$R/etc/shield-sil-release" <<EOF
SHIELD_SIL_IMAGE='$SIL_IMAGE'
SHIELD_SIL_IMAGE_SOURCE='$BASE/$STEM-root.tar.xz'
SHIELD_SIL_KERNEL='$KVER'
SHIELD_SIL_GIT_REV='$GIT_REV'
EOF

echo ">> kernel"
# Ubuntu wraps the kernel for UEFI boot (PE stub + EFI zboot, zstd); QEMU's direct
# kernel boot needs the plain arm64 Image inside.
"$ROOT/image/builders/arm64-kernel.py" "$CACHE/$STEM-vmlinuz-generic" "$OUT/kernel.bin"
rm -f "$OUT/initrd.img"

# Provisioning boot: installs the packages above with dpkg inside the guest
# (offline, as PID 1), then powers off. Runs once per image build.
cat > "$R/usr/lib/shield-sil/provision" <<'EOF'
#!/bin/sh
export PATH=/usr/sbin:/usr/bin:/sbin:/bin DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8
mount -t proc proc /proc; mount -t sysfs sys /sys
mountpoint -q /dev || mount -t devtmpfs dev /dev
mount -t tmpfs tmpfs /run; mount -t tmpfs tmpfs /tmp
printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d && chmod 0755 /usr/sbin/policy-rc.d
rm -f /var/lib/man-db/auto-update
if dpkg -i --force-confold /var/cache/shield-sil/debs/*.deb && dpkg --audit; then
	result=OK
else
	result=FAILED
fi
rm -rf /usr/sbin/policy-rc.d /var/cache/shield-sil/debs /usr/lib/shield-sil/provision
sync
echo "SHIELD-SIL-PROVISION-$result"
mount -o remount,ro / ; sync
echo o > /proc/sysrq-trigger
sleep 60
EOF
chmod 0755 "$R/usr/lib/shield-sil/provision"

echo ">> creating ${EMMC_GB} GB ext4 image (sparse)"
rm -f "$OUT/rootfs.ext4"
mke2fs -q -F -t ext4 -L rootfs -d "$R" "$OUT/rootfs.ext4" "${EMMC_GB}G"

echo ">> provisioning boot (dpkg inside the guest; a few minutes under emulation)"
LOG="$OUT/provision.log"
timeout 1800 qemu-system-aarch64 -M virt -cpu max -smp 4 -m 2048 -accel tcg,thread=multi \
	-nographic -no-reboot -nic none \
	-kernel "$OUT/kernel.bin" \
	-append "root=/dev/vda rw rootwait console=ttyAMA0 init=/usr/lib/shield-sil/provision" \
	-drive file="$OUT/rootfs.ext4",format=raw,if=virtio,discard=unmap \
	< /dev/null > "$LOG" 2>&1 || true
if ! grep -q 'SHIELD-SIL-PROVISION-OK' "$LOG"; then
	tail -n 60 "$LOG" >&2
	echo "provisioning boot failed; full log: $LOG" >&2
	exit 1
fi
rc=0; e2fsck -fy "$OUT/rootfs.ext4" >/dev/null || rc=$?
[ "$rc" -le 1 ] || { echo "e2fsck found errors after provisioning ($rc)" >&2; exit 1; }

EXTRA=()
if [ "${STORAGE:-}" = emmc ]; then
	echo ">> building U-Boot (Shield eMMC boot, machine $MACHINE)"
	OUT="$OUT" CACHE="$ROOT/build/cache/u-boot" FLAVOR=ubuntu MACHINE="$MACHINE" bash "$ROOT/image/uboot/build.sh"
	DTB=()
	if [ "$MACHINE" = imx8mp ]; then
		# The device tree is built against this kernel's sources: linux-stable at
		# its upstream version (7.0.0-38-generic -> 7.0).
		KERNEL_VERSION=$(echo "$KVER" | sed -E 's/^([0-9]+\.[0-9]+)\.0-.*/\1/; s/^([0-9]+\.[0-9]+\.[0-9]+)-.*/\1/')
		echo ">> building TF-A and the SIL device tree (linux $KERNEL_VERSION)"
		OUT="$OUT" CACHE="$ROOT/build/cache/tf-a" bash "$ROOT/image/tfa/build.sh"
		OUT="$OUT" CACHE="$ROOT/build/cache/linux-dts" KERNEL_VERSION="$KERNEL_VERSION" bash "$ROOT/image/dts/build.sh"
		DTB=(--dtb "$OUT/shield-sil.dtb")
	fi
	cp "$CACHE/$STEM-initrd-generic" "$OUT/initrd.img"
	echo ">> assembling Shield's eMMC (GPT: boot, rootfs, cidata, data)"
	python3 "$ROOT/image/builders/emmc.py" --layout ubuntu --kernel "$OUT/kernel.bin" --initrd "$OUT/initrd.img" \
		--rootfs "$OUT/rootfs.ext4" --boot-script "$OUT/boot.scr" \
		--uboot-env "$OUT/u-boot-env.txt" --out "$OUT" --user-gib "$EMMC_GB" "${DTB[@]}"
	EXTRA=(storage=emmc firmware=u-boot.bin disk=emmc.img initrd=initrd.img machine="$MACHINE")
	[ "$MACHINE" = virt ] || EXTRA+=(tfa=bl31.bin dtb=shield-sil.dtb kernel_version="$KERNEL_VERSION")
fi

"$ROOT/image/builders/manifest.py" "$OUT" image="$SIL_IMAGE" source="$BASE/$STEM-root.tar.xz" \
	kernel_release="$KVER" modules="$DEB" packages="$PKG_VERSIONS" overlay_git_rev="$GIT_REV" kernel=kernel.bin \
	"${EXTRA[@]}"
