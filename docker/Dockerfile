# Shield SIL host environment: everything the harness, the image builders and the
# system tests need, so the SIL runs the same on any Docker host (Linux, or
# Windows/macOS through WSL2 or Docker Desktop). See docker/README.md.
# 26.04: QEMU 10.2, which has the eMMC device (9.2+) for Shield's emulated eMMC.

# The SIL's QEMU for the i.MX 8M Plus machine: upstream v10.2.1 plus
# qemu/patches/, built from source (qemu/README.md). Installed to /opt/qemu-sil;
# Ubuntu's qemu-system-arm below stays for the virt machine.
FROM ubuntu:26.04 AS qemu-sil
ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update -q \
 && apt-get install -y -q --no-install-recommends \
      build-essential ninja-build meson pkg-config python3 python3-venv git ca-certificates \
      libglib2.0-dev libpixman-1-dev libslirp-dev libfdt-dev zlib1g-dev flex bison \
 && rm -rf /var/lib/apt/lists/*
COPY qemu /qemu
RUN PREFIX=/opt/qemu-sil /qemu/build.sh

FROM ubuntu:26.04

ARG DEBIAN_FRONTEND=noninteractive
# Same host tools as .github/workflows/ci.yml, plus the image builders' tools
# (e2fsprogs/debugfs, xz, dpkg, git, dosfstools/mtools for the FAT boot
# partition), the Docker CLI for the OpenWrt builder,
# the cross toolchain for U-Boot and TF-A (image/uboot/, image/tfa/) and the
# device tree compiler for the i.MX 8M Plus machine (image/dts/build.sh), and
# swig with the Python headers for U-Boot's binman on that machine.
RUN apt-get update -q \
 && apt-get install -y -q --no-install-recommends \
      qemu-system-arm qemu-utils ipxe-qemu dnsmasq-base iproute2 nftables curl ca-certificates \
      zstd kmod e2fsprogs xz-utils dpkg git python3 python3-venv docker-cli procps socat iputils-ping openssh-client \
      make gcc libc6-dev bison flex bc libssl-dev gcc-aarch64-linux-gnu dosfstools mtools \
      swig python3-setuptools python3-dev \
      device-tree-compiler cpp \
 && rm -rf /var/lib/apt/lists/*

# The harness has no runtime dependencies; pytest is the only test dependency.
COPY --from=qemu-sil /opt/qemu-sil /opt/qemu-sil

RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install -q --no-cache-dir "pytest>=8"
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/work/src \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# The checkout is mounted here at run time (compose.yaml), so edits need no rebuild.
WORKDIR /work
RUN git config --system --add safe.directory /work
