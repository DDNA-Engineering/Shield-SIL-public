"""Hardware profile loading (config/*.toml), with per-run overrides.

A profile describes the board (compute, network, power, peripherals) and the
image booted on it. The image is picked by name from the catalog in
config/images/ (see shield_sil.images), and its [image]/[guest]/[network]
settings are merged over the profile:

    Profile.load(image="ubuntu-26.04")

Any value can also be overridden for one run without editing a file:

    Profile.load(overrides={"image.disk": "out/my.img", "guest.adapter": "linux"})

Environment variables do the same for test runs:

    SIL_PROFILE   profile file        SIL_IMAGE    image name from the catalog
    SIL_DISK      image.disk          SIL_KERNEL   image.kernel
    SIL_APPEND    image.append        SIL_GUEST    guest.adapter
    SIL_INTERNET  network.internet ("host": uplinks reach the real internet)
    SIL_SET       extra "section.key=value" pairs, separated by ';'
"""

from __future__ import annotations

import copy
import json
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROFILE = REPO_ROOT / "config" / "shield-v1.5.toml"

_ENV_KEYS = {
    "SIL_DISK": "image.disk",
    "SIL_KERNEL": "image.kernel",
    "SIL_APPEND": "image.append",
    "SIL_GUEST": "guest.adapter",
    "SIL_INTERNET": "network.internet",
}


def parse_value(raw: str) -> Any:
    """Parse an override value: JSON if it parses (numbers, booleans, lists), else a string."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def parse_set(pairs: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for pair in pairs:
        if "=" not in pair or "." not in pair.split("=", 1)[0]:
            raise ValueError(f"override must look like section.key=value, got {pair!r}")
        k, v = pair.split("=", 1)
        out[k.strip()] = parse_value(v)
    return out


def env_overrides(environ: dict[str, str] | None = None) -> dict[str, Any]:
    env = os.environ if environ is None else environ
    out = {key: env[var] for var, key in _ENV_KEYS.items() if env.get(var)}
    if env.get("SIL_SET"):
        out.update(parse_set([p for p in env["SIL_SET"].split(";") if p.strip()]))
    return out


@dataclass(frozen=True)
class Profile:
    path: Path
    data: dict[str, Any] = field(repr=False)

    @classmethod
    def load(
        cls,
        path: str | Path | None = None,
        overrides: dict[str, Any] | None = None,
        image: str | None = None,
    ) -> "Profile":
        """Load a profile, merge the selected catalog image over it, then apply overrides.

        The image is ``image`` if given, else ``image.name`` in ``overrides``, else
        ``[image] name`` in the profile. An empty name boots the profile's own
        [image] settings unchanged.
        """
        p = Path(path) if path else DEFAULT_PROFILE
        with p.open("rb") as f:
            data = tomllib.load(f)
        data = copy.deepcopy(data)
        overrides = dict(overrides or {})
        if image is not None:
            name = image
            overrides.pop("image.name", None)
        elif "image.name" in overrides:
            name = str(overrides.pop("image.name") or "")
        else:
            name = data.get("image", {}).get("name", "")
        if name:
            from . import images  # local import: images imports REPO_ROOT from here

            entry = images.get(name)
            for section, values in entry.sections.items():
                data.setdefault(section, {}).update(copy.deepcopy(values))
        data.setdefault("image", {})["name"] = name
        for dotted, value in overrides.items():
            section, _, key = dotted.partition(".")
            if not key:
                raise ValueError(f"override key must be section.key, got {dotted!r}")
            data.setdefault(section, {})[key] = value
        prof = cls(path=p, data=data)
        prof.validate()
        return prof

    @classmethod
    def from_env(cls, extra: dict[str, Any] | None = None) -> "Profile":
        overrides = env_overrides()
        overrides.update(extra or {})
        return cls.load(os.environ.get("SIL_PROFILE") or None, overrides, image=os.environ.get("SIL_IMAGE") or None)

    @property
    def image_name(self) -> str:
        """Catalog name of the selected image ("" for a custom [image])."""
        return self.data.get("image", {}).get("name", "")

    def __getitem__(self, section: str) -> dict[str, Any]:
        return self.data[section]

    @property
    def name(self) -> str:
        return self.data["profile"]["name"]

    def image_path(self, key: str) -> Path | None:
        """Resolve an [image] file path ("" means not used). Relative paths are from the repo root."""
        raw = self.data["image"].get(key, "")
        if not raw:
            return None
        p = Path(os.path.expanduser(str(raw)))
        return p if p.is_absolute() else REPO_ROOT / p

    def validate(self) -> None:
        img = self.data.get("image")
        if not img:
            raise ValueError("profile needs an [image] section")
        for k in ("qemu", "machine", "disk"):
            if not img.get(k):
                raise ValueError(f"image.{k} is required")
        if img.get("disk_format", "raw") not in {"raw", "qcow2"}:
            raise ValueError("image.disk_format must be raw or qcow2")
        if img.get("storage", "virtio") not in {"virtio", "emmc"}:
            raise ValueError("image.storage must be virtio or emmc")
        if img.get("storage") == "emmc":
            if img.get("kernel") or not img.get("firmware"):
                raise ValueError(
                    "image.storage = emmc boots the eMMC through firmware (U-Boot): set image.firmware, "
                    "not image.kernel. To boot a kernel and a plain disk instead, also set "
                    "image.storage=virtio (--set image.storage=virtio)")
            if img.get("emmc_boot_mib", 4) <= 0:
                raise ValueError("image.emmc_boot_mib must be > 0")
        if not isinstance(img.get("extra_args", []), list):
            raise ValueError("image.extra_args must be a list")
        if not img.get("kernel") and not img.get("firmware"):
            # Booting from disk on 'virt' needs firmware; other machines may have it built in.
            if img["machine"] == "virt":
                raise ValueError("image: set a kernel, or firmware to boot the disk's bootloader")
        if "guest" not in self.data:
            raise ValueError("profile needs a [guest] section")
        c = self.data["compute"]
        if c["cpu_cores"] < 1:
            raise ValueError("compute.cpu_cores must be >= 1")
        if c["ram_mb"] < 128:
            raise ValueError("compute.ram_mb must be >= 128")
        cap = c["cpu_cap"]
        if not 0 < cap["host_cores"] <= c["cpu_cores"]:
            raise ValueError(
                "compute.cpu_cap.host_cores must be > 0 and <= compute.cpu_cores; "
                "the SIL may never get more compute than the real board"
            )
        if cap["method"] not in {"auto", "cgroup2", "cgroup1", "duty", "none"}:
            raise ValueError(f"unknown compute.cpu_cap.method {cap['method']!r}")
        internet = self.data.get("network", {}).get("internet", "off")
        if internet not in {"off", "host"}:
            raise ValueError(f"network.internet must be 'off' (simulated) or 'host' (real), not {internet!r}")
        b = self.data["battery"]
        if b["capacity_mah"] <= 0:
            raise ValueError("battery.capacity_mah must be > 0")
