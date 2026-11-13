# Compute limits and calibration

## What is fixed

| Resource | Shield | SIL |
| --- | --- | --- |
| CPU | 4x Cortex-A53 @ 1.8 GHz | `-cpu cortex-a53 -smp 4`; `/proc/cpuinfo` reports CPU part `0xd03` |
| RAM | 4 GB LPDDR4 | `-m 4096M` |
| Storage | 32 GB eMMC | 32 GB eMMC (QEMU `emmc`, 32 GiB user area) on OpenWrt 24.10.3 / 25.12.3 and Ubuntu 26.04; 32 GB virtio disk on 23.05.6 |

`tests/system/test_compute.py` checks all three on every run.

## CPU speed

QEMU runs the real A53 instruction set, but it does not model timing: guest speed
depends on the host. On a fast host one emulated core would outrun a Shield core.
Three mechanisms bound it:

1. **Overall cap.** QEMU runs under a CPU quota of `compute.cpu_cap.host_cores`
   (default 4.0 host cores), set with cgroups, or with a SIGSTOP/SIGCONT duty
   cycle when cgroups are not writable.
2. **Per-core speed limit.** After boot the SIL runs Dhrystone 2.1 in the guest
   (`dhrystone`, built with GCC `-O3` from a pinned public copy of the original
   sources; see `image/openwrt/packages/dhrystone`). If one core scores more than
   `compute.cpu_cap.device_dmips_per_core`, each vCPU thread is put in its own
   cgroup with a quota, and the quota is adjusted until a core is no faster than
   that target. `./sil up` prints the result (`speed limit: ...`), and it is in
   the run's `run.json`. Turn it off with `compute.cpu_cap.limit_speed = false`.
3. **Check.** `test_sil_is_not_faster_than_device` measures a core again and
   fails if it is more than 5 % over the target.

The SIL only slows a fast host down. A host that is slower than a Shield (QEMU
without KVM on a CI runner, for example: about 1,500 DMIPS per core) stays slower;
nothing can make emulation faster.

### Where the target comes from

| | |
| --- | --- |
| Clock | i.MX8M Plus: 4x Cortex-A53 at up to 1.8 GHz (NXP; 1.6 GHz on industrial-grade parts) |
| Per-MHz score | Cortex-A53, Dhrystone 2.1, GCC 9.2 `-O3`: 3557 DMIPS at 1.2 GHz, about 2.96 DMIPS/MHz per core (TI, *AM62x Benchmarks*, SPRAD45B, sect. 2.1). Arm publishes no official A53 figure. |
| Target | 3557 / 1200 x 1800 = **5336 DMIPS per core** |

This is from the datasheet clock and a published A53 measurement, not a
measurement of a Shield. Memory, cache and eMMC speed differences are not
included. With a Shield unit, replace it with the unit's own number:

```sh
echo 20000000 | dhrystone     # on the Shield, built the same way
```

DMIPS = "Dhrystones per Second" / 1757. Put it in
`compute.cpu_cap.device_dmips_per_core` in `config/shield-v1.5.toml`.
`sudo -E shield-sil calibrate` boots the SIL, applies the limit and prints the
measured score next to the target.

## Known gaps

* The NPU is not emulated.
* Memory bandwidth, cache sizes and eMMC throughput are not modelled.
* Dhrystone runs from the L1 cache: it bounds core speed, not memory-bound
  workloads.
* The quota is applied in 100 ms periods, so a throttled core runs in bursts.
* Only the OpenWrt 24.10.3 and 25.12.3 images ship `dhrystone`; the others are
  not speed-limited yet and their speed test is skipped.
