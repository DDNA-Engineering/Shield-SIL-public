#!/usr/bin/env bash
# Build the Shield SIL device tree (shield-sil.dts) for one guest kernel.
#
# The DT includes the SoC's imx8mp.dtsi, so it is compiled against the device
# tree sources of the kernel that boots it: linux-stable at that version
# (sparse checkout of the DT directories only, cached). Output: $OUT/shield-sil.dtb
#
#   KERNEL_VERSION  e.g. 6.6.104 (OpenWrt 24.10.3), 6.12.85 (25.12.3), 7.0 (Ubuntu 26.04)
#   OUT             output directory (absolute)
#   CACHE           cache directory (absolute)
set -euo pipefail

: "${KERNEL_VERSION:?}" "${OUT:?}" "${CACHE:?}"
REPO=https://github.com/gregkh/linux.git
HERE="$(cd "$(dirname "$0")" && pwd)"
for t in git cpp dtc; do
	command -v "$t" >/dev/null || { echo "missing tool: $t (apt-get install git cpp device-tree-compiler)" >&2; exit 1; }
done

SRC="$CACHE/linux-dts-v$KERNEL_VERSION"
if [ ! -d "$SRC/.git" ]; then
	rm -rf "$SRC"
	git clone -q --depth 1 --filter=blob:none --sparse --branch "v$KERNEL_VERSION" "$REPO" "$SRC"
	git -C "$SRC" sparse-checkout set arch/arm64/boot/dts/freescale include/dt-bindings \
		include/uapi/linux scripts/dtc/include-prefixes
fi
# The tag is the version; a checkout with local changes is not used.
git -C "$SRC" diff --quiet || { echo "linux checkout in $SRC has local changes" >&2; exit 1; }

DTS="$SRC/arch/arm64/boot/dts/freescale"
mkdir -p "$OUT"
tmp="$OUT/.shield-sil.dts.pre"
trap 'rm -f "$tmp"' EXIT
cpp -nostdinc -undef -D__DTS__ -x assembler-with-cpp \
	-I "$DTS" -I "$SRC/include" -I "$SRC/scripts/dtc/include-prefixes" \
	-o "$tmp" "$HERE/shield-sil.dts"
# -q: the warnings dtc prints come from the SoC dtsi, not from shield-sil.dts.
dtc -q -I dts -O dtb -i "$DTS" -o "$OUT/shield-sil.dtb" "$tmp"
echo "shield-sil.dtb for linux $KERNEL_VERSION -> $OUT/shield-sil.dtb"
