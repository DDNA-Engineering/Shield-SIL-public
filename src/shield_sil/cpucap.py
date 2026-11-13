"""Cap the host CPU the emulator may use, so the SIL never out-computes a Shield.

QEMU reproduces the Cortex-A53 instruction set but not its timing: on a fast
host the guest could run faster than the real board. The cap bounds the
emulator to ``host_cores`` worth of host CPU time. ``calibrate`` (see
docs/calibration.md) checks the result against a measurement from a real unit.
"""

from __future__ import annotations

import os
import signal
import threading
import time
from pathlib import Path

PERIOD_US = 100_000


class CpuCap:
    def __init__(self, pid: int, host_cores: float, method: str = "auto", name: str = "shield-sil") -> None:
        self.pid = pid
        self.host_cores = host_cores
        self.name = f"{name}-{pid}"
        self.method = "none"
        self._cg: Path | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.core_fraction = 1.0
        order = ["cgroup2", "cgroup1", "duty"] if method == "auto" else [method]
        errors = []
        for m in order:
            if m == "none":
                return
            try:
                getattr(self, f"_apply_{m}")()
                self.method = m
                return
            except (OSError, RuntimeError) as e:
                errors.append(f"{m}: {e}")
        raise RuntimeError("could not cap emulator CPU: " + "; ".join(errors))

    # ------------------------------------------------------------------
    def _apply_cgroup2(self) -> None:
        for root in (Path("/sys/fs/cgroup"), Path("/sys/fs/cgroup/unified")):
            if (root / "cgroup.controllers").exists():
                break
        else:
            raise RuntimeError("no cgroup v2 hierarchy")
        if "cpu" not in (root / "cgroup.controllers").read_text().split():
            raise RuntimeError("cgroup v2 cpu controller not available")
        try:
            (root / "cgroup.subtree_control").write_text("+cpu")
        except OSError:
            pass
        cg = root / self.name
        cg.mkdir(exist_ok=True)
        (cg / "cpu.max").write_text(f"{int(self.host_cores * PERIOD_US)} {PERIOD_US}")
        self._move_threads(cg / "cgroup.threads" if (cg / "cgroup.threads").exists() else cg / "cgroup.procs", procs_only=True)
        self._cg = cg

    def _apply_cgroup1(self) -> None:
        root = Path("/sys/fs/cgroup/cpu")
        if not (root / "cpu.cfs_quota_us").exists():
            raise RuntimeError("no cgroup v1 cpu controller")
        cg = root / self.name
        cg.mkdir(exist_ok=True)
        (cg / "cpu.cfs_period_us").write_text(str(PERIOD_US))
        (cg / "cpu.cfs_quota_us").write_text(str(int(self.host_cores * PERIOD_US)))
        self._move_threads(cg / "tasks", procs_only=False)
        self._cg = cg

    def _move_threads(self, target: Path, procs_only: bool) -> None:
        if procs_only:
            target.write_text(str(self.pid))
            return
        # v1 'tasks' takes thread ids; QEMU creates vCPU threads at start, so move all.
        for tid in os.listdir(f"/proc/{self.pid}/task"):
            target.write_text(tid)

    def _apply_duty(self) -> None:
        """Fallback: SIGSTOP/SIGCONT so QEMU runs a fraction of each period.

        Coarser than a cgroup quota. ``host_cores`` is converted to a duty
        cycle relative to the cores QEMU can use (one per vCPU).
        """
        os.kill(self.pid, 0)
        self._thread = threading.Thread(target=self._duty_loop, name="cpucap", daemon=True)
        self._thread.start()

    def _duty_loop(self) -> None:
        vcpus = max(len(os.listdir(f"/proc/{self.pid}/task")) - 2, 1)
        period = 0.05
        while not self._stop.is_set():
            # Re-read each period: set_core_fraction may lower host_cores later.
            duty = min(self.host_cores / vcpus, 1.0)
            if duty >= 1.0:
                time.sleep(period)
                continue
            try:
                time.sleep(period * duty)
                os.kill(self.pid, signal.SIGSTOP)
                time.sleep(period * (1 - duty))
                os.kill(self.pid, signal.SIGCONT)
            except ProcessLookupError:
                return

    # ------------------------------------------------------------------
    # Per-core speed limit
    def vcpu_threads(self) -> list[int]:
        """Thread ids of QEMU's vCPU threads ("CPU n/TCG", "CPU n/KVM")."""
        tids = []
        for tid in os.listdir(f"/proc/{self.pid}/task"):
            try:
                comm = Path(f"/proc/{self.pid}/task/{tid}/comm").read_text().strip()
            except OSError:
                continue
            if comm.startswith("CPU ") and "/" in comm:
                tids.append(int(tid))
        return sorted(tids)

    def set_core_fraction(self, fraction: float) -> str:
        """Limit every emulated core to ``fraction`` of one host core.

        The overall cap (``host_cores``) bounds the whole emulator; this bounds
        each vCPU on its own, so one busy guest core runs at most as fast as one
        Shield core even on a host with fast cores to spare. Each vCPU thread gets
        its own child cgroup (threaded on cgroup v2) with that quota. Without
        cgroups, the overall cap is lowered to ``fraction`` per vCPU instead, which
        bounds total throughput but not a single busy core. Returns how it was
        applied.
        """
        fraction = max(min(fraction, 1.0), 0.01)
        tids = self.vcpu_threads()
        if self.method in ("cgroup2", "cgroup1") and self._cg and tids:
            try:
                for i, tid in enumerate(tids):
                    child = self._cg / f"vcpu{i}"
                    child.mkdir(exist_ok=True)
                    if self.method == "cgroup2":
                        (child / "cgroup.type").write_text("threaded")
                        (child / "cpu.max").write_text(f"{int(fraction * PERIOD_US)} {PERIOD_US}")
                        (child / "cgroup.threads").write_text(str(tid))
                    else:  # v1: child groups take thread ids in 'tasks'
                        (child / "cpu.cfs_period_us").write_text(str(PERIOD_US))
                        (child / "cpu.cfs_quota_us").write_text(str(int(fraction * PERIOD_US)))
                        (child / "tasks").write_text(str(tid))
                self.core_fraction = fraction
                return f"{self.method}-per-vcpu"
            except OSError:
                pass  # per-thread groups not allowed here: fall back below
        if not tids:
            raise RuntimeError("no vCPU threads found (QEMU needs -name ...,debug-threads=on)")
        total = min(self.host_cores, fraction * len(tids))
        if self._cg and self.method == "cgroup2":
            (self._cg / "cpu.max").write_text(f"{int(total * PERIOD_US)} {PERIOD_US}")
        elif self._cg and self.method == "cgroup1":
            (self._cg / "cpu.cfs_quota_us").write_text(str(int(total * PERIOD_US)))
        else:
            self.host_cores = total  # duty loop reads it on its next start
        self.core_fraction = fraction
        return f"{self.method}-total"

    # ------------------------------------------------------------------
    def usage_seconds(self) -> float | None:
        """Total host CPU seconds consumed under the cap (cgroup methods only)."""
        if not self._cg:
            return None
        if (self._cg / "cpu.stat").exists() and self.method == "cgroup2":
            for line in (self._cg / "cpu.stat").read_text().splitlines():
                k, v = line.split()
                if k == "usage_usec":
                    return int(v) / 1e6
        acct = Path("/sys/fs/cgroup/cpuacct") / self.name / "cpuacct.usage"
        if acct.exists():
            return int(acct.read_text()) / 1e9
        return None

    def release(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
            try:
                os.kill(self.pid, signal.SIGCONT)
            except ProcessLookupError:
                pass
        if self._cg and self._cg.exists():
            for child in self._cg.glob("vcpu*"):
                try:
                    child.rmdir()
                except OSError:
                    pass
            try:
                self._cg.rmdir()
            except OSError:
                pass  # still has tasks if QEMU is alive; it will be removed with them
