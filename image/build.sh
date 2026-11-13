#!/usr/bin/env bash
# Build SIL guest images from the catalog in config/images/.
#
#   image/build.sh                    the profile's default image
#   image/build.sh ubuntu-26.04       one or more images by name
#   image/build.sh --all              every image in the catalog
#   image/build.sh --list             what's available, and what's built
#
# Output: build/images/<name>/ (kernel, rootfs.ext4, image.json; initrd for Ubuntu).
# Downloads are cached in build/cache/<name>/.
# OpenWrt images need Docker. Ubuntu images must be built as root (sudo -E).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
if [ "${1:-}" = "--list" ]; then
	exec python3 -m shield_sil.cli images
fi
exec python3 -m shield_sil.cli build-image "$@"
