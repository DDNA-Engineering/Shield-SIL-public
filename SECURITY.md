# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub: open the repository's
**Security** tab and choose **Report a vulnerability**. Do not open a public
issue for a security problem.

We aim to acknowledge a report within 5 business days.

## Scope

This repository is a test environment. It runs QEMU and network namespaces as
root on the host, and the guest images use their upstream default credentials
(OpenWrt's root login has no password). Run it only on a machine you control,
and do not expose the SIL networks to untrusted networks.

In scope: the harness in `src/`, the image builders in `image/`, and the
workflows in `.github/`. Vulnerabilities in OpenWrt, Ubuntu or QEMU themselves
belong to those projects.
