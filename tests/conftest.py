"""Shared pytest options: choose the profile and the image per run.

    python -m pytest tests/system --sil-image ubuntu-26.04
    python -m pytest tests/system --sil-disk out/other.img --sil-guest linux
    python -m pytest tests/system --sil-set image.append="console=ttyAMA0 root=/dev/vda2"

The same can be done with SIL_PROFILE / SIL_IMAGE / SIL_DISK / SIL_KERNEL / SIL_APPEND /
SIL_GUEST / SIL_SET environment variables (see shield_sil.profile).
"""

import os


def pytest_addoption(parser):
    g = parser.getgroup("shield-sil")
    g.addoption("--sil-profile", help="hardware profile TOML")
    g.addoption("--sil-image", help="image from the catalog in config/images/ (e.g. ubuntu-26.04)")
    g.addoption("--sil-disk", help="disk image to boot (overrides image.disk)")
    g.addoption("--sil-kernel", help="kernel to boot (overrides image.kernel; '' boots the disk)")
    g.addoption("--sil-guest", help="guest adapter: openwrt | linux")
    g.addoption("--sil-set", action="append", default=[], help="section.key=value override (repeatable)")


def pytest_configure(config):
    # Options become environment overrides so every fixture and helper sees one profile.
    for opt, var in (
        ("--sil-profile", "SIL_PROFILE"),
        ("--sil-image", "SIL_IMAGE"),
        ("--sil-disk", "SIL_DISK"),
        ("--sil-kernel", "SIL_KERNEL"),
        ("--sil-guest", "SIL_GUEST"),
    ):
        val = config.getoption(opt)
        if val is not None:
            os.environ[var] = val
    if config.getoption("--sil-set"):
        prior = [p for p in os.environ.get("SIL_SET", "").split(";") if p]
        os.environ["SIL_SET"] = ";".join(prior + config.getoption("--sil-set"))
    config.addinivalue_line("markers", "guest(kind): test needs this guest adapter (e.g. openwrt)")
    config.addinivalue_line("markers", "hw_agent: test needs the SIL hardware agent in the image")
    config.addinivalue_line(
        "markers", "board_interfaces: test needs Shield's LEDs, GPIOs and USB devices as Linux driver interfaces"
    )


def pytest_report_header(config):
    try:
        from shield_sil.profile import Profile

        p = Profile.from_env()
        return f"shield-sil image: {p.image_name or '(custom)'}  disk: {p['image']['disk']}  guest: {p['guest']['adapter']}"
    except Exception as e:  # noqa: BLE001 - a header must never break collection
        return f"shield-sil image: unresolved ({e})"
