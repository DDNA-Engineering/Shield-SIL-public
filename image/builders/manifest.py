#!/usr/bin/env python3
"""Write <out>/image.json: what was built, from what, with checksums of small files.

Usage: manifest.py OUT key=value ... [kernel=FILE] [initrd=FILE]
Files named by kernel=/initrd=/firmware=/tfa=/dtb=/sysupgrade= (relative to OUT) get a sha256 entry.
"""

import hashlib
import json
import pathlib
import sys


def sha(p: pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    out = pathlib.Path(sys.argv[1])
    meta: dict[str, str] = {}
    for arg in sys.argv[2:]:
        k, _, v = arg.partition("=")
        if k in ("kernel", "initrd", "firmware", "tfa", "dtb", "sysupgrade"):
            meta[f"{k}_sha256"] = sha(out / v)
        else:
            meta[k] = v
    (out / "image.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(json.dumps(meta, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
