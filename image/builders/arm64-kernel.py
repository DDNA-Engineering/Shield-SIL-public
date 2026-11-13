#!/usr/bin/env python3
"""Turn a distribution arm64 kernel into a plain Image that QEMU can boot with -kernel.

Distribution kernels are often wrapped for UEFI boot, which QEMU's direct kernel
boot does not understand:

* a UKI/systemd-stub style PE file whose ``.linux`` section holds the kernel
  (Ubuntu 26.04 ships this, with device trees for real boards alongside), and/or
* an EFI zboot image (``MZ`` + ``zimg``): a small EFI decompressor followed by a
  gzip or zstd compressed Image.

This unwraps both and checks the result is an arm64 Image (magic ``ARM\\x64`` at
offset 0x38). A plain Image or gzip file passes through.

Usage: arm64-kernel.py IN OUT      write the plain Image to OUT
       arm64-kernel.py --uname IN    print the kernel release (e.g. 7.0.0-38-generic)
"""

from __future__ import annotations

import gzip
import struct
import subprocess
import sys

ARM64_MAGIC = b"ARM\x64"


def pe_section(data: bytes, wanted: bytes) -> bytes | None:
    if data[:2] != b"MZ" or len(data) < 0x40:
        return None
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe : pe + 4] != b"PE\0\0":
        return None
    nsec = struct.unpack_from("<H", data, pe + 6)[0]
    opt = struct.unpack_from("<H", data, pe + 20)[0]
    off = pe + 24 + opt
    for i in range(nsec):
        s = data[off + 40 * i : off + 40 * i + 40]
        if s[:8].rstrip(b"\0") == wanted:
            size, ptr = struct.unpack_from("<II", s, 16)
            return data[ptr : ptr + size]
    return None


def decompress(payload: bytes, kind: str) -> bytes:
    if kind == "gzip":
        return gzip.decompress(payload)
    if kind == "zstd":
        # zstd is not in the Python standard library before 3.14: use the CLI.
        return subprocess.run(["zstd", "-dc"], input=payload, capture_output=True, check=True).stdout
    raise SystemExit(f"unsupported zboot compression {kind!r}")


def unwrap(data: bytes) -> bytes:
    for _ in range(4):  # a few layers at most
        if data[0x38:0x3C] == ARM64_MAGIC:
            return data
        if data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
            continue
        if data[:2] == b"MZ" and data[4:8] == b"zimg":
            off, size = struct.unpack_from("<II", data, 8)
            kind = data[24:32].rstrip(b"\0").decode()
            data = decompress(data[off : off + size], kind)
            continue
        inner = pe_section(data, b".linux")
        if inner is not None:
            data = inner
            continue
        break
    raise SystemExit("not an arm64 kernel Image (no ARM\\x64 magic after unwrapping)")


def kernel_release(data: bytes) -> str:
    """Release string from a UKI's .uname section, else from the Image's banner."""
    uname = pe_section(data, b".uname")
    if uname:
        return uname.split(b"\0", 1)[0].decode()
    image = unwrap(data)
    i = image.find(b"Linux version ")
    if i < 0:
        raise SystemExit("kernel release not found")
    return image[i + 14 :].split(b" ", 1)[0].decode()


def main() -> int:
    if sys.argv[1] == "--uname":
        with open(sys.argv[2], "rb") as f:
            print(kernel_release(f.read()))
        return 0
    src, dst = sys.argv[1], sys.argv[2]
    with open(src, "rb") as f:
        image = unwrap(f.read())
    with open(dst, "wb") as f:
        f.write(image)
    print(f"arm64 Image: {len(image)} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
