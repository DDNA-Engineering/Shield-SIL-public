"""Image catalog: the guest images the SIL can build and boot by name.

Each ``config/images/<name>.toml`` describes one image:

* ``description``: one line for listings.
* ``[build]``: ``builder`` (a script in ``image/builders/``) and its settings,
  passed to the builder as upper-case environment variables.
* ``[image]``, ``[guest]``, optionally ``[network]``: merged over the hardware
  profile when the image is selected, so the board stays the same and only the
  software changes.

Select an image with ``shield-sil --image NAME``, ``pytest --sil-image NAME``,
``SIL_IMAGE=NAME`` or ``[image] name`` in the profile. Build it with
``image/build.sh NAME``.
"""

from __future__ import annotations

import os
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .profile import REPO_ROOT

CATALOG_DIR = REPO_ROOT / "config" / "images"
BUILDERS_DIR = REPO_ROOT / "image" / "builders"
BUILD_ROOT = REPO_ROOT / "build" / "images"
CACHE_ROOT = REPO_ROOT / "build" / "cache"
MERGED_SECTIONS = ("image", "guest", "network")


@dataclass(frozen=True)
class ImageEntry:
    name: str
    path: Path
    description: str
    build: dict[str, Any] = field(repr=False)
    sections: dict[str, dict[str, Any]] = field(repr=False)

    @property
    def out_dir(self) -> Path:
        return BUILD_ROOT / self.name

    def files(self) -> list[Path]:
        """Files the image needs at boot, as absolute paths."""
        img = self.sections.get("image", {})
        out = []
        for key in ("kernel", "initrd", "firmware", "tfa", "disk"):
            raw = img.get(key, "")
            if raw:
                p = Path(os.path.expanduser(str(raw)))
                out.append(p if p.is_absolute() else REPO_ROOT / p)
        return out

    @property
    def built(self) -> bool:
        return all(p.exists() for p in self.files())


def names() -> list[str]:
    return sorted(p.stem for p in CATALOG_DIR.glob("*.toml"))


def get(name: str) -> ImageEntry:
    path = CATALOG_DIR / f"{name}.toml"
    if not path.is_file():
        raise ValueError(f"unknown image {name!r}; available: {', '.join(names()) or 'none'}")
    with path.open("rb") as f:
        data = tomllib.load(f)
    build = dict(data.get("build", {}))
    if not build.get("builder"):
        raise ValueError(f"{path}: [build] builder is required")
    if not (BUILDERS_DIR / f"{build['builder']}.sh").is_file():
        raise ValueError(f"{path}: no builder image/builders/{build['builder']}.sh")
    unknown = set(data) - {"description", "build", *MERGED_SECTIONS}
    if unknown:
        raise ValueError(f"{path}: unexpected keys {sorted(unknown)}")
    sections = {s: dict(data[s]) for s in MERGED_SECTIONS if s in data}
    if "image" not in sections or "guest" not in sections:
        raise ValueError(f"{path}: needs [image] and [guest] sections")
    return ImageEntry(name=name, path=path, description=data.get("description", ""), build=build, sections=sections)


def build(name: str, emmc_gb: int, check: bool = True) -> subprocess.CompletedProcess:
    """Run the image's builder. Output goes to build/images/<name>/."""
    entry = get(name)
    env = dict(os.environ)
    env.update({k.upper(): str(v) for k, v in entry.build.items() if k != "builder"})
    env.update(
        SIL_IMAGE=name,
        OUT=str(entry.out_dir),
        CACHE=str(CACHE_ROOT / name),
        EMMC_GB=str(emmc_gb),
    )
    script = BUILDERS_DIR / f"{entry.build['builder']}.sh"
    return subprocess.run(["bash", str(script)], env=env, check=check)
