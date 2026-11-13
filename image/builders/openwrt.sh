#!/usr/bin/env bash
# Builder for upstream OpenWrt (armsr/armv8) guest images.
#
# Built the way OpenWrt builds release images: the official ImageBuilder for
# the release assembles the root filesystem from the release's own package
# feeds, plus the SIL overlay as FILES. Kernel modules the release kernel
# ships disabled (image/openwrt/packages/) are compiled against that exact
# kernel with the release's official SDK, so their vermagic matches.
#
# Called by image/build.sh with the image's catalog entry in the environment:
#   SIL_IMAGE   catalog name, e.g. openwrt-24.10.3
#   OUT         output directory (absolute)
#   CACHE       cache directory (absolute)
#   EMMC_GB     disk size in GB
#   TAG         Docker Hub tag, e.g. armsr-armv8-v24.10.3        ([build] tag)
#   PACKAGES_FILE  package list, relative to the repo, optional  ([build] packages_file)
#   STORAGE     "emmc": also build U-Boot and Shield's eMMC image  ([build] storage)
#   MACHINE     "virt" (default) or "imx8mp": Shield's SoC; also builds TF-A and
#               the SIL device tree for KERNEL_VERSION   ([build] machine)
#   PROFILE     ImageBuilder profile (default generic; imx8m for imx8mp)
#   KERNEL_VERSION  the release kernel's version, for the device tree (imx8mp)
#
# Sources (Docker Hub, official OpenWrt images):
#   openwrt/imagebuilder:<tag>  ImageBuilder: kernel + package installation
#   openwrt/sdk:<tag>           SDK: only when the package list names a local package
#
# Outputs in $OUT: kernel.bin, rootfs.ext4 (sparse, EMMC_GB), packages.manifest, image.json;
# with STORAGE=emmc also u-boot.bin, boot.scr, root.squashfs, emmc.img (sparse) and
# sysupgrade.tar; with MACHINE=imx8mp also bl31.bin and shield-sil.dtb.
set -euo pipefail

: "${SIL_IMAGE:?}" "${OUT:?}" "${EMMC_GB:?}" "${TAG:?set [build] tag in the catalog entry}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
CACHE="${CACHE:-$ROOT/build/cache/$SIL_IMAGE}"
MACHINE="${MACHINE:-virt}"
PROFILE="${PROFILE:-generic}"
case "$MACHINE" in
	virt) ;;
	imx8mp) [ "${STORAGE:-}" = emmc ] && [ -n "${KERNEL_VERSION:-}" ] ||
		{ echo "machine imx8mp needs [build] storage = \"emmc\" and kernel_version" >&2; exit 1; } ;;
	*) echo "unknown machine $MACHINE" >&2; exit 1 ;;
esac
LOCAL_SRC="$ROOT/image/openwrt/packages"
WORK="$(mktemp -d)"
IB_CT="shield-sil-ib-$$"
SDK_CT="shield-sil-sdk-$$"
trap 'rm -rf "$WORK"; docker rm -f "$IB_CT" "$SDK_CT" >/dev/null 2>&1 || true' EXIT

for t in docker mke2fs sha256sum tar $([ "${STORAGE:-}" = emmc ] && echo python3 gzip); do
	command -v "$t" >/dev/null || { echo "missing tool: $t" >&2; exit 1; }
done
mkdir -p "$OUT" "$CACHE"

# Docker Hub rate-limits anonymous pulls (HTTP 429): retry with backoff.
# An image already present locally is used as is (tags are fixed releases).
pull() {
	docker image inspect "${@: -1}" >/dev/null 2>&1 && return 0
	local delay=15
	for attempt in 1 2 3 4 5; do
		docker pull -q "$@" >/dev/null && return 0
		[ "$attempt" = 5 ] && break
		echo "   docker pull failed (attempt $attempt), retrying in ${delay}s" >&2
		sleep "$delay"; delay=$((delay * 2))
	done
	echo "could not pull $*" >&2
	return 1
}

# Containers download from the release feeds. Behind an HTTPS proxy (with its
# own CA), give them the host's proxy settings and CA bundle.
#
# Files go into the build containers with `docker cp`, never bind mounts: when the
# SIL runs in its own container (./sil) Docker is the host's, through its socket,
# and a bind-mount path would be resolved on the host, not here.
NET=()
CA=""
if [ -n "${HTTPS_PROXY:-${https_proxy:-}}" ]; then
	NET+=(--network host -e "https_proxy=${HTTPS_PROXY:-$https_proxy}" -e "HTTPS_PROXY=${HTTPS_PROXY:-$https_proxy}"
		-e "no_proxy=${NO_PROXY:-${no_proxy:-}}")
	if [ -f "${SSL_CERT_FILE:-}" ]; then
		CA=$SSL_CERT_FILE
		NET+=(-e SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt)
	fi
fi

# Copy the proxy CA bundle into a created (not yet started) container.
add_ca() { [ -z "$CA" ] || docker cp "$CA" "$1:/etc/ssl/certs/ca-certificates.crt"; }

# --- package list ------------------------------------------------------------
# One package per line, '#' comments. A name with a directory under
# image/openwrt/packages/ is built locally with the SDK; the rest come from
# the release feeds.
PACKAGES=()
LOCAL=()
if [ -n "${PACKAGES_FILE:-}" ]; then
	while read -r pkg _; do
		case "$pkg" in ''|'#'*) continue ;; esac
		PACKAGES+=("$pkg")
		[ -d "$LOCAL_SRC/$pkg" ] && LOCAL+=("$pkg")
	done < "$ROOT/$PACKAGES_FILE"
fi

# --- local kernel modules (SDK) -----------------------------------------------
LOCAL_OUT=""
if [ "${#LOCAL[@]}" -gt 0 ]; then
	# Rebuild only when the sources or the release change.
	key=$( { echo "$TAG"; printf '%s\n' "${LOCAL[@]}";
		for p in "${LOCAL[@]}"; do (cd "$LOCAL_SRC" && find "$p" -type f -print0 | sort -z | xargs -0 sha256sum); done; } \
		| sha256sum | cut -c1-16)
	LOCAL_OUT="$CACHE/local-packages/$key"
	if ! ls "$LOCAL_OUT"/*.[ai]pk >/dev/null 2>&1; then
		echo ">> building ${LOCAL[*]} with openwrt/sdk:$TAG"
		pull "openwrt/sdk:$TAG"
		# The SDK image must be for the release's target. Docker Hub's
		# openwrt/sdk:imx-cortexa53-v24.10.3 holds the bcm27xx/bcm2710 SDK (its
		# TARGET says so), which builds userspace for the same CPU but not kernel
		# modules for the imx kernel. A mismatched image fetches the right SDK
		# from downloads.openwrt.org, checked against the release's signed
		# sha256sums with the keys already in the image.
		want_target=${TAG%-v*}; want_target=${want_target/-//}
		want_version=${TAG##*-v}
		docker create --name "$SDK_CT" "${NET[@]}" -e "SIL_TARGET=$want_target" -e "SIL_VERSION=$want_version" \
			"openwrt/sdk:$TAG" bash -ec '
			if [ "$TARGET" != "$SIL_TARGET" ]; then
				echo "openwrt/sdk image is for $TARGET, not $SIL_TARGET: fetching the $SIL_TARGET SDK" >&2
				base="https://downloads.openwrt.org/releases/$SIL_VERSION/targets/$SIL_TARGET"
				mkdir -p /tmp/sdk && cd /tmp/sdk
				wget -q "$base/sha256sums" "$base/sha256sums.asc"
				gpg -q --verify sha256sums.asc sha256sums
				f=$(grep -o "openwrt-sdk-[^ ]*Linux-x86_64.tar.zst" sha256sums | head -n 1)
				wget -q "$base/$f"
				grep " \*$f\$" sha256sums | sha256sum -c --quiet
				tar xf "$f" --strip=1 --no-same-owner && rm -f "$f"
			fi
			for p in "$@"; do cp -r "/src/$p" package/; done
			make defconfig >/dev/null
			for p in "$@"; do make "package/$p/compile" -j"$(nproc)" || make "package/$p/compile" V=s -j1; done
			mkdir -p /tmp/out
			for p in "$@"; do
				find bin -type f \( -name "${p}_*.ipk" -o -name "${p}-[0-9]*.apk" \) -exec cp {} /tmp/out/ \;
			done
			ls /tmp/out' _ "${LOCAL[@]}" >/dev/null
		add_ca "$SDK_CT"
		docker cp "$LOCAL_SRC/." "$SDK_CT:/src"
		docker start -a "$SDK_CT"
		rm -rf "$LOCAL_OUT.tmp" && mkdir -p "$LOCAL_OUT.tmp"
		docker cp "$SDK_CT:/tmp/out/." "$LOCAL_OUT.tmp/"
		docker rm -f "$SDK_CT" >/dev/null
		for p in "${LOCAL[@]}"; do
			ls "$LOCAL_OUT.tmp/${p}"[-_]* >/dev/null 2>&1 || { echo "SDK produced no package for $p" >&2; exit 1; }
		done
		rm -rf "$LOCAL_OUT" && mv "$LOCAL_OUT.tmp" "$LOCAL_OUT"
	else
		echo ">> local packages from cache ($LOCAL_OUT)"
	fi
fi

# --- overlay -----------------------------------------------------------------
echo ">> staging Shield SIL overlay"
FILES="$WORK/files"
mkdir -p "$FILES"
# Owned by root in the image, whoever owns the checkout.
cp -a --no-preserve=ownership "$ROOT/image/overlay/common/." "$ROOT/image/overlay/openwrt/." "$FILES/"
# Shield's eMMC: upgrade, configuration and data partition scripts.
[ "${STORAGE:-}" != emmc ] || cp -a --no-preserve=ownership "$ROOT/image/overlay/openwrt-emmc/." "$FILES/"
# On Shield's SoC the eMMC is uSDHC3, /dev/mmcblk2 (as on Shield); QEMU virt's is mmcblk0.
if [ "$MACHINE" = imx8mp ] && [ -f "$FILES/etc/fw_env.config" ]; then
	sed -i 's|^/dev/mmcblk0boot1 |/dev/mmcblk2boot1 |' "$FILES/etc/fw_env.config"
fi
find "$FILES/usr/sbin" "$FILES/usr/libexec" "$FILES/etc/init.d" "$FILES/etc/uci-defaults" \
	-type f -exec chmod 0755 {} + 2>/dev/null || true
GIT_REV=$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)
cat > "$FILES/etc/shield-sil-release" <<EOF
SHIELD_SIL_IMAGE='$SIL_IMAGE'
SHIELD_SIL_IMAGE_SOURCE='openwrt/imagebuilder:$TAG'
SHIELD_SIL_GIT_REV='$GIT_REV'
EOF
chmod -R a+rX "$FILES"

# --- ImageBuilder ------------------------------------------------------------
echo ">> assembling rootfs with openwrt/imagebuilder:$TAG (${#PACKAGES[@]} extra packages)"
pull "openwrt/imagebuilder:$TAG"
# Targets whose release has no squashfs root image (imx/cortexa53) get one made
# from the same root directory with the ImageBuilder's mksquashfs4 and
# OpenWrt's squashfs options (include/image.mk).
docker create --name "$IB_CT" "${NET[@]}" -e "PROFILE=$PROFILE" "openwrt/imagebuilder:$TAG" bash -ec '
	[ -d /sil-local ] && cp /sil-local/* packages/
	make image PROFILE="$PROFILE" FILES=/sil-files PACKAGES="$*" BIN_DIR=/tmp/sil-bin
	if ! ls /tmp/sil-bin/*-squashfs-rootfs.img.gz >/dev/null 2>&1; then
		root=$(ls -d build_dir/target-*/root-* | head -n 1)
		staging_dir/host/bin/mksquashfs4 "$root" /tmp/sil-bin/sil-root.squashfs -nopad -noappend -root-owned \
			-comp xz -Xpreset 9 -Xe -Xlc 0 -Xlp 2 -Xpb 2 -b 256k \
			-p "/dev d 755 0 0" -p "/dev/console c 600 0 0 5 1" -quiet
	fi
	ls /tmp/sil-bin' _ "${PACKAGES[@]}" >/dev/null
add_ca "$IB_CT"
docker cp "$FILES/." "$IB_CT:/sil-files"
[ -z "$LOCAL_OUT" ] || docker cp "$LOCAL_OUT/." "$IB_CT:/sil-local"
docker start -a "$IB_CT"
docker cp "$IB_CT:/tmp/sil-bin/." "$WORK/bin/"
docker rm -f "$IB_CT" >/dev/null

kernel=$(ls "$WORK"/bin/*-"$PROFILE"-kernel.bin)
rootfs_tar=$(ls "$WORK"/bin/*-rootfs.tar.gz)
manifest=$(ls "$WORK"/bin/*.manifest)
cp "$kernel" "$OUT/kernel.bin"
cp "$manifest" "$OUT/packages.manifest"
# Every requested package must be in the image (ImageBuilder only warns for some).
for p in "${PACKAGES[@]}"; do
	case "$p" in -*) continue ;; esac
	grep -q "^$p - " "$OUT/packages.manifest" || { echo "package $p missing from image" >&2; exit 1; }
done

echo ">> creating ${EMMC_GB} GB ext4 image (sparse)"
mkdir "$WORK/rootfs"
tar -xzf "$rootfs_tar" -C "$WORK/rootfs"
rm -f "$OUT/rootfs.ext4"
mke2fs -q -F -t ext4 -L rootfs -d "$WORK/rootfs" "$OUT/rootfs.ext4" "${EMMC_GB}G"

EXTRA=()
if [ "${STORAGE:-}" = emmc ]; then
	echo ">> building U-Boot (Shield eMMC boot, machine $MACHINE)"
	OUT="$OUT" CACHE="$ROOT/build/cache/u-boot" FLAVOR=openwrt MACHINE="$MACHINE" bash "$ROOT/image/uboot/build.sh"
	DTB=()
	if [ "$MACHINE" = imx8mp ]; then
		echo ">> building TF-A and the SIL device tree (linux $KERNEL_VERSION)"
		OUT="$OUT" CACHE="$ROOT/build/cache/tf-a" bash "$ROOT/image/tfa/build.sh"
		OUT="$OUT" CACHE="$ROOT/build/cache/linux-dts" KERNEL_VERSION="$KERNEL_VERSION" bash "$ROOT/image/dts/build.sh"
		DTB=(--dtb "$OUT/shield-sil.dtb")
	fi
	# The release's own squashfs root filesystem image (or one made the same
	# way, above); fstools puts the f2fs overlay after it, as on Shield.
	if ls "$WORK"/bin/*-squashfs-rootfs.img.gz >/dev/null 2>&1; then
		gzip -dc "$(ls "$WORK"/bin/*-squashfs-rootfs.img.gz)" > "$OUT/root.squashfs"
	else
		cp "$WORK/bin/sil-root.squashfs" "$OUT/root.squashfs"
	fi
	echo ">> assembling Shield's eMMC (GPT, slots A and B) and sysupgrade.tar"
	python3 "$ROOT/image/builders/emmc.py" --layout openwrt --kernel "$OUT/kernel.bin" \
		--rootfs "$OUT/root.squashfs" --boot-script "$OUT/boot.scr" \
		--uboot-env "$OUT/u-boot-env.txt" --out "$OUT" --user-gib "$EMMC_GB" "${DTB[@]}"
	EXTRA=(storage=emmc firmware=u-boot.bin disk=emmc.img sysupgrade=sysupgrade.tar machine="$MACHINE")
	[ "$MACHINE" = virt ] || EXTRA+=(tfa=bl31.bin dtb=shield-sil.dtb kernel_version="$KERNEL_VERSION")
fi

"$ROOT/image/builders/manifest.py" "$OUT" image="$SIL_IMAGE" source="openwrt/imagebuilder:$TAG" \
	packages_file="${PACKAGES_FILE:-}" overlay_git_rev="$GIT_REV" kernel=kernel.bin "${EXTRA[@]}"
