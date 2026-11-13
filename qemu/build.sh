#!/usr/bin/env bash
# Build the SIL's QEMU: upstream QEMU at a pinned tag and commit, plus the
# patches in qemu/patches/ (see qemu/README.md), for the aarch64 system
# emulator only.
#
#   PREFIX  install prefix (default /opt/qemu-sil)
#   SRC     source checkout (default: a temporary directory, removed afterwards)
#   JOBS    parallel jobs (default: nproc)
set -euo pipefail

TAG=v10.2.1
COMMIT=2d3df8abca265c9bcc9e438d691d561592060998
REPO=https://github.com/qemu/qemu.git

PREFIX=${PREFIX:-/opt/qemu-sil}
JOBS=${JOBS:-$(nproc)}
HERE="$(cd "$(dirname "$0")" && pwd)"
for t in git ninja meson pkg-config python3 cc; do
	command -v "$t" >/dev/null || { echo "missing tool: $t" >&2; exit 1; }
done

if [ -z "${SRC:-}" ]; then
	SRC=$(mktemp -d)
	trap 'rm -rf "$SRC"' EXIT
fi
if [ ! -d "$SRC/.git" ]; then
	git clone -q --depth 1 --branch "$TAG" "$REPO" "$SRC"
fi
got=$(git -C "$SRC" rev-parse HEAD)
[ "$got" = "$COMMIT" ] || { echo "QEMU $TAG is $got, expected $COMMIT" >&2; exit 1; }

# Apply each patch once (a reused SRC may have them already).
for p in "$HERE"/patches/*.patch; do
	if git -C "$SRC" apply --check "$p" 2>/dev/null; then
		git -C "$SRC" apply "$p"
	elif ! git -C "$SRC" apply --reverse --check "$p" 2>/dev/null; then
		echo "patch does not apply: $p" >&2; exit 1
	fi
done

mkdir -p "$SRC/build"
cd "$SRC/build"
../configure --prefix="$PREFIX" --target-list=aarch64-softmmu \
	--enable-slirp --enable-fdt=system --disable-docs --disable-rust \
	--disable-werror >/dev/null
ninja -j"$JOBS" >/dev/null
ninja install >/dev/null
"$PREFIX/bin/qemu-system-aarch64" --version | head -1
