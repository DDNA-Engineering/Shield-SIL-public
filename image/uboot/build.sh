#!/usr/bin/env bash
# Build U-Boot for the SIL's emulated Shield, booting the eMMC as Shield does.
#
# Upstream U-Boot at a pinned tag and commit, for one of two QEMU machines:
#   MACHINE=imx8mp  Shield's SoC (patched QEMU imx8mp-evk): the shield-sil board
#                   from patches/ with the Shield SIL device tree
#                   (image/dts/shield-sil.dts); QEMU loads it next to TF-A.
#   MACHINE=virt    QEMU virt (the default): qemu_arm64_defconfig plus
#                   shield-sil.config; QEMU -bios.
# Either way with the machine's environment (virt.env, or the board's base.env),
# the guest family's boot environment ($FLAVOR.env) and boot script
# ($FLAVOR-boot.cmd). Outputs in $OUT:
#   u-boot.bin       U-Boot proper
#   u-boot-env.txt   the default environment (written to the factory eMMC)
#   boot.scr         the boot script, for the eMMC's boot partition(s)
#
#   FLAVOR openwrt | ubuntu          OUT    output directory (absolute)
#   MACHINE virt | imx8mp            CACHE  build cache (absolute)
#   CROSS_COMPILE (default aarch64-linux-gnu-)
set -euo pipefail

TAG=v2026.10
COMMIT=5508406582f6f7120dce0c4b3059819a41788db2
REPO=https://github.com/u-boot/u-boot.git

: "${OUT:?}" "${CACHE:?}" "${FLAVOR:?openwrt or ubuntu}"
MACHINE=${MACHINE:-virt}
case "$MACHINE" in virt|imx8mp) ;; *) echo "unknown machine $MACHINE (virt or imx8mp)" >&2; exit 1 ;; esac
CROSS_COMPILE=${CROSS_COMPILE:-aarch64-linux-gnu-}
HERE="$(cd "$(dirname "$0")" && pwd)"
[ -f "$HERE/$FLAVOR.env" ] && [ -f "$HERE/$FLAVOR-boot.cmd" ] || { echo "unknown U-Boot flavor $FLAVOR" >&2; exit 1; }
for t in git make gcc bison flex bc "${CROSS_COMPILE}gcc"; do
	command -v "$t" >/dev/null || { echo "missing tool: $t (apt-get install git make gcc bison flex bc libssl-dev gcc-aarch64-linux-gnu)" >&2; exit 1; }
done

# The i.MX 8M Plus board packs u-boot.bin with binman, which needs U-Boot's
# Python libfdt binding: built with swig against a Python that has setuptools
# and its headers. Use the system Python (a venv first on PATH lacks them).
MAKE_PY=()
if [ "$MACHINE" = imx8mp ]; then
	PY=""
	for p in /usr/bin/python3 python3; do
		"$p" -c 'import setuptools, sysconfig, os; os.stat(sysconfig.get_paths()["include"] + "/Python.h")' 2>/dev/null &&
			{ PY=$(command -v "$p"); break; }
	done
	[ -n "$PY" ] && command -v swig >/dev/null ||
		{ echo "machine imx8mp needs swig and a python3 with setuptools and headers (apt-get install swig python3-setuptools python3-dev)" >&2; exit 1; }
	MAKE_PY=(PYTHON3="$PY")
fi

SRC="$CACHE/u-boot-$TAG"
if [ ! -d "$SRC/.git" ]; then
	rm -rf "$SRC"
	git clone -q --depth 1 --branch "$TAG" "$REPO" "$SRC"
fi
got=$(git -C "$SRC" rev-parse HEAD)
[ "$got" = "$COMMIT" ] || { echo "U-Boot $TAG is $got, expected $COMMIT" >&2; exit 1; }
git -C "$SRC" diff --quiet || { echo "U-Boot checkout in $SRC has local changes" >&2; exit 1; }

B="$CACHE/u-boot-build-$MACHINE-$FLAVOR"
rm -rf "$B"
mkdir -p "$B" "$OUT"
if [ "$MACHINE" = virt ]; then
	# The default environment: QEMU's own plus Shield's boot, as one board .env
	# file (generated next to qemu-arm.env for the build, removed afterwards).
	BOARD_ENV="$SRC/board/emulation/qemu-arm/shield-sil.env"
	cat "$SRC/board/emulation/qemu-arm/qemu-arm.env" "$HERE/virt.env" "$HERE/$FLAVOR.env" > "$BOARD_ENV"
	trap 'rm -f "$BOARD_ENV"' EXIT
	make -s -C "$SRC" O="$B" CROSS_COMPILE="$CROSS_COMPILE" "${MAKE_PY[@]}" qemu_arm64_defconfig
	"$SRC/scripts/kconfig/merge_config.sh" -m -O "$B" "$B/.config" "$HERE/shield-sil.config" >/dev/null
	make -s -C "$SRC" O="$B" CROSS_COMPILE="$CROSS_COMPILE" "${MAKE_PY[@]}" olddefconfig
	# Every option we asked for must have survived olddefconfig.
	while IFS= read -r line; do
		case "$line" in ''|'#'*) case "$line" in '# CONFIG_'*' is not set') ;; *) continue ;; esac ;; esac
		grep -qxF "$line" "$B/.config" || { echo "U-Boot config did not take: $line" >&2; exit 1; }
	done < "$HERE/shield-sil.config"
else
	# The shield-sil board (patches/), its device tree next to the upstream
	# i.MX 8M Plus ones, and its environment: the board's plus Shield's boot.
	# The checkout is put back to the pinned commit afterwards.
	trap 'git -C "$SRC" checkout -q -- . && git -C "$SRC" clean -fdq' EXIT
	for p in "$HERE"/patches/*.patch; do git -C "$SRC" apply "$p"; done
	cp "$HERE/../dts/shield-sil.dts" "$SRC/dts/upstream/src/arm64/freescale/shield-sil.dts"
	cat "$SRC/board/ddna/shield-sil/base.env" "$HERE/$FLAVOR.env" > "$SRC/board/ddna/shield-sil/shield-sil.env"
	make -s -C "$SRC" O="$B" CROSS_COMPILE="$CROSS_COMPILE" "${MAKE_PY[@]}" shield-sil_defconfig
fi
make -s -C "$SRC" O="$B" CROSS_COMPILE="$CROSS_COMPILE" "${MAKE_PY[@]}" -j"$(nproc)" u-boot.bin
# The default environment as text, for the factory eMMC (image/builders/emmc.py
# writes it to both environment copies, as a board's factory programming does).
make -s -C "$SRC" O="$B" CROSS_COMPILE="$CROSS_COMPILE" "${MAKE_PY[@]}" u-boot-initial-env
cp "$B/u-boot.bin" "$OUT/u-boot.bin"
cp "$B/u-boot-initial-env" "$OUT/u-boot-env.txt"
"$B/tools/mkimage" -q -A arm64 -T script -C none -n "Shield SIL $FLAVOR" -d "$HERE/$FLAVOR-boot.cmd" "$OUT/boot.scr"
echo "u-boot $TAG ($COMMIT), $MACHINE $FLAVOR -> $OUT/u-boot.bin, boot.scr"
