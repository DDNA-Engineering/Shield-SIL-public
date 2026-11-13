import os
import shutil
import time
import warnings

import pytest

from shield_sil.guest import Guest
from shield_sil.profile import Profile
from shield_sil.sil import ShieldSIL, wait_until


def _profile() -> Profile:
    return Profile.from_env()


def _skip_reason(profile: Profile) -> str | None:
    if os.geteuid() != 0:
        return "system tests need root (network namespaces, taps, CPU cap)"
    qemu = profile["image"]["qemu"]
    if shutil.which(qemu) is None:
        return f"{qemu} not installed"
    for key in ("disk", "kernel", "initrd", "firmware"):
        path = profile.image_path(key)
        if path and not path.exists():
            name = profile.image_name
            build = f"image/build.sh {name}" if name else "image/build.sh <name>"
            return f"image.{key} not found: {path} (run {build}, or pass --sil-image/--sil-disk)"
    return None


def pytest_collection_modifyitems(config, items):
    profile = _profile()
    guest = Guest.from_profile(profile)
    reason = _skip_reason(profile)
    for item in items:
        if "tests/system" not in str(item.fspath):
            continue
        item.add_marker(pytest.mark.system)
        if reason:
            item.add_marker(pytest.mark.skip(reason=reason))
            continue
        for m in item.iter_markers("guest"):
            if guest.kind not in m.args:
                item.add_marker(pytest.mark.skip(reason=f"needs guest {'/'.join(m.args)}, image is {guest.kind}"))
        if item.get_closest_marker("hw_agent") and not guest.has_hw_agent:
            item.add_marker(pytest.mark.skip(reason="image has no SIL hardware agent (guest.hw_agent = false)"))
        if item.get_closest_marker("board_interfaces") and not guest.has_board_interfaces:
            item.add_marker(pytest.mark.skip(
                reason="image does not present Shield's driver interfaces (guest.board_interfaces = false)"))


# mwan3 (upstream defaults) brings an uplink back after 5 good tracking rounds. A
# round that times out takes ~26 s, so on a slow runner recovery after an outage
# takes several minutes; give it 7.
UPLINK_TIMEOUT_S = 420


# How long an uplink may stay desynchronised in mwan3 (see ShieldSIL.mwan3_desynced)
# before the harness restarts mwan3 to restore the baseline for the next test.
DESYNC_GRACE_S = 60


def _wait_uplinks(sil: ShieldSIL, what: str) -> None:
    desynced_since: float | None = None
    restarted = False

    def ready() -> bool:
        nonlocal desynced_since, restarted
        if sil.uplinks_ready():
            return True
        if restarted or not sil.mwan3_desynced():
            desynced_since = None
            return False
        now = time.monotonic()
        desynced_since = desynced_since or now
        if now - desynced_since >= DESYNC_GRACE_S:
            warnings.warn(f"mwan3 left {sil.mwan3_desynced()} without routes after a replug "
                          "(upstream hotplug ordering race); restarting mwan3 between tests")
            sil.sh("/etc/init.d/mwan3 restart", timeout=180, check=False)
            restarted = True
        return False

    try:
        wait_until(ready, timeout=UPLINK_TIMEOUT_S, interval=3, what=what)
    except TimeoutError as e:
        # Diagnostics never hide the timeout: a slow console must not stop them.
        try:
            diag = f"hardware path diagnostics: {sil.diagnostics()}"
        except Exception as d:  # noqa: BLE001
            diag = f"diagnostics failed: {d!r}"
        try:
            detail = sil.sh("mwan3 interfaces; logread | grep -E 'mwan3|netifd' | tail -40", check=False) \
                if sil.guest.kind == "openwrt" else ""
        except Exception as d:  # noqa: BLE001
            detail = f"(guest detail unavailable: {d!r})"
        raise TimeoutError(f"{e}\n{diag}\n{detail}") from None


@pytest.fixture(scope="session")
def shield():
    """One emulated system for the whole session; reset between tests."""
    sil = ShieldSIL(profile=_profile())
    sil.start()
    # Both uplinks must be usable (lease, and mwan3 online where it runs) before any test.
    _wait_uplinks(sil, "WAN and cellular uplinks")
    yield sil
    sil.stop()


@pytest.fixture(autouse=True)
def _reset(request):
    yield
    if "shield" in request.fixturenames:
        sil = request.getfixturevalue("shield")
        sil.reset_environment()
        _wait_uplinks(sil, "uplinks to recover after reset")
