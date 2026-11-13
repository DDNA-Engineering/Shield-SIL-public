#!/usr/bin/env bash
# Build TF-A (BL31) for the emulated Shield (i.MX 8M Plus).
#
# Upstream Trusted Firmware-A at a pinned LTS tag and commit, PLAT=imx8mp,
# unmodified. On a Shield, the SPL loads BL31 into on-chip RAM (0x970000)
# and BL31 starts U-Boot; in the SIL, QEMU loads both and starts BL31.
# Output: $OUT/bl31.bin
#
#   OUT    output directory (absolute)   CACHE  build cache (absolute)
#   CROSS_COMPILE (default aarch64-linux-gnu-)
set -euo pipefail

TAG=lts-v2.12.4
COMMIT=a9fcaf3ccf3c0091c33ba6ef03b8ed629394c240
REPO=https://github.com/ARM-software/arm-trusted-firmware.git

: "${OUT:?}" "${CACHE:?}"
CROSS_COMPILE=${CROSS_COMPILE:-aarch64-linux-gnu-}
for t in git make gcc "${CROSS_COMPILE}gcc"; do
	command -v "$t" >/dev/null || { echo "missing tool: $t" >&2; exit 1; }
done

SRC="$CACHE/tf-a-$TAG"
if [ ! -d "$SRC/.git" ]; then
	rm -rf "$SRC"
	git clone -q --depth 1 --branch "$TAG" "$REPO" "$SRC"
fi
got=$(git -C "$SRC" rev-parse HEAD)
[ "$got" = "$COMMIT" ] || { echo "TF-A $TAG is $got, expected $COMMIT" >&2; exit 1; }
git -C "$SRC" diff --quiet || { echo "TF-A checkout in $SRC has local changes" >&2; exit 1; }

B="$CACHE/tf-a-build"
rm -rf "$B"
# Console on UART2 (0x30890000), as U-Boot and Linux use it.
make -s -C "$SRC" BUILD_BASE="$B" CROSS_COMPILE="$CROSS_COMPILE" PLAT=imx8mp \
	IMX_BOOT_UART_BASE=0x30890000 -j"$(nproc)" bl31
mkdir -p "$OUT"
cp "$B/imx8mp/release/bl31.bin" "$OUT/bl31.bin"
echo "tf-a $TAG ($COMMIT) -> $OUT/bl31.bin"
