"""Command line: shield-sil up | images | build-image | calibrate | profile."""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time

import os

from . import images
from .profile import Profile, env_overrides, parse_set
from .sil import ShieldSIL


def _load(args: argparse.Namespace) -> Profile:
    """Profile from --profile, then env overrides (SIL_*), then command-line overrides."""
    overrides = env_overrides()
    for flag, key in (("disk", "image.disk"), ("kernel", "image.kernel"), ("guest", "guest.adapter")):
        val = getattr(args, flag)
        if val is not None:
            overrides[key] = val
    if getattr(args, "internet", False):
        overrides["network.internet"] = "host"
    overrides.update(parse_set(args.set))
    image = args.image if args.image is not None else (os.environ.get("SIL_IMAGE") or None)
    return Profile.load(args.profile or os.environ.get("SIL_PROFILE") or None, overrides, image=image)


def _up(args: argparse.Namespace) -> int:
    sil = ShieldSIL(profile=_load(args), instance=args.instance)
    print("booting the emulated Shield ...", flush=True)
    t0 = time.monotonic()
    sil.start()
    print(f"ready in {time.monotonic() - t0:.0f} s (CPU cap: {sil.cpucap.method if sil.cpucap else 'none'})")
    if sil.speed:
        print(f"speed limit: {sil.speed}")
    if sil.internet_mode == "host":
        print("internet: REAL, through this machine's connection (NAT) on every uplink")
    else:
        print("internet: simulated (closed lab; ./sil up --internet for the real one)")
    print(f"  run dir:  {sil.run_dir}")
    print(f"  console:  socat -,raw,echo=0 UNIX-CONNECT:{sil.run_dir / 'console.sock'}")
    print(f"  image:    {sil.profile.image_path('disk')}  (guest: {sil.guest.kind})")
    print(f"  LAN shell: sudo ip netns exec {sil.net.ns['lan']} bash   "
          f"(device LAN is {sil.profile['network']['lan_ip']})")
    from .control import ControlServer, socket_path
    ctl = ControlServer(sil, socket_path(args.instance))
    print("Ctrl-C to stop.")
    stop = False

    def _sig(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)
    try:
        while not stop and sil.proc and sil.proc.poll() is None:
            time.sleep(0.5)
    finally:
        ctl.close()
        sil.stop()
    return 0


def _calibrate(args: argparse.Namespace) -> int:
    prof = _load(args)
    with ShieldSIL(profile=prof, instance=args.instance) as sil:
        if not sil.has_dhrystone():
            print("this image has no dhrystone (speed probe)", file=sys.stderr)
            return 1
        dmips = sil.dmips()
        applied = sil.speed
    ref = prof["compute"]["cpu_cap"].get("device_dmips_per_core", 0)
    out = {"sil_dmips_per_core": round(dmips), "device_dmips_per_core": ref, "speed_limit": applied}
    if ref:
        out["ratio_sil_to_device"] = round(dmips / ref, 3)
        out["verdict"] = ("ok: a SIL core is not faster than a Shield core" if dmips <= ref * 1.05
                          else "too fast: is compute.cpu_cap.limit_speed on?")
    else:
        out["verdict"] = "no device_dmips_per_core in the profile: see docs/calibration.md"
    print(json.dumps(out, indent=2))
    return 0


def _ctl(args: argparse.Namespace) -> int:
    from .control import request

    if args.action == "button":
        req = {"cmd": "button", "seconds": float(args.values[0]) if args.values else 0.3}
    else:
        if len(args.values) != 2 or args.values[1] not in ("up", "down"):
            print("usage: shield-sil ctl link PORT up|down", file=sys.stderr)
            return 2
        req = {"cmd": "link", "port": args.values[0], "up": args.values[1] == "up"}
    try:
        reply = request(req, args.instance)
    except OSError as e:
        print(f"no running SIL (shield-sil up): {e}", file=sys.stderr)
        return 1
    print(json.dumps(reply))
    return 0 if reply.get("ok") else 1


def _images(args: argparse.Namespace) -> int:
    default = Profile.load(args.profile).image_name
    for name in images.names():
        e = images.get(name)
        mark = "*" if name == default else " "
        state = "built" if e.built else "not built"
        print(f"{mark} {name:<18} {state:<10} {e.description}")
    print("\n* default. Build: image/build.sh <name>   Use: shield-sil --image <name> up")
    return 0


def _build_image(args: argparse.Namespace) -> int:
    prof = Profile.load(args.profile)
    names = args.names or [prof.image_name]
    if args.all:
        names = images.names()
    for name in names:
        images.get(name)  # fail fast on a typo, before any download
    for name in names:
        print(f"== building {name}", flush=True)
        r = images.build(name, emmc_gb=int(prof["compute"]["emmc_gb"]), check=False)
        if r.returncode != 0:
            print(f"!! building {name} failed (exit {r.returncode})", file=sys.stderr)
            return r.returncode
        print(f"== {name} ready in {images.get(name).out_dir}", flush=True)
    return 0


def _profile(args: argparse.Namespace) -> int:
    print(json.dumps(_load(args).data, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="shield-sil")
    ap.add_argument("--profile", help="hardware profile TOML (default: config/shield-v1.5.toml)")
    ap.add_argument("--instance", type=int, default=0, help="instance number, for parallel runs")
    ap.add_argument("--image", help="image from the catalog to boot (see `shield-sil images`)")
    ap.add_argument("--disk", help="disk image to boot instead of the profile's image.disk")
    ap.add_argument("--kernel", help="kernel to boot instead of image.kernel ('' boots the disk)")
    ap.add_argument("--guest", help="guest adapter: openwrt | linux")
    ap.add_argument("--internet", action="store_true",
                    help="route the Shield's uplinks to the real internet through this machine "
                         "(network.internet=host)")
    ap.add_argument("--set", action="append", default=[], metavar="SECTION.KEY=VALUE",
                    help="override any profile value for this run (repeatable)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("up", help="boot the emulated Shield and keep it running").set_defaults(fn=_up)
    c = sub.add_parser("calibrate", help="measure guest CPU speed against the device reference")
    c.set_defaults(fn=_calibrate)
    sub.add_parser("images", help="list the images in the catalog").set_defaults(fn=_images)
    k = sub.add_parser("ctl", help="act on a running SIL (from `up`): button SECONDS | link PORT up|down")
    k.add_argument("action", choices=["button", "link"])
    k.add_argument("values", nargs="*")
    k.set_defaults(fn=_ctl)
    b = sub.add_parser("build-image", help="build images from the catalog (default: the profile's image)")
    b.add_argument("names", nargs="*", help="image names")
    b.add_argument("--all", action="store_true", help="build every image in the catalog")
    b.set_defaults(fn=_build_image)
    sub.add_parser("profile", help="print the resolved hardware profile").set_defaults(fn=_profile)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
