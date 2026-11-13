"""ShieldSIL.limit_speed: per-core throttle converges on the Dhrystone target."""

from shield_sil.profile import Profile
from shield_sil.sil import ShieldSIL


class FakeCap:
    def __init__(self) -> None:
        self.core_fraction = 1.0
        self.calls = []

    def set_core_fraction(self, f: float) -> str:
        self.core_fraction = min(f, 1.0)
        self.calls.append(self.core_fraction)
        return "fake-per-vcpu"


def _sil(target: float, unthrottled: float) -> ShieldSIL:
    p = Profile.load(image="openwrt-24.10.3")
    p.data["compute"]["cpu_cap"]["device_dmips_per_core"] = target
    sil = ShieldSIL(profile=p)
    sil.cpucap = FakeCap()
    sil.has_dhrystone = lambda: True
    # Guest speed scales with the quota, with some overhead, like TCG under a quota.
    sil.dmips = lambda: unthrottled * sil.cpucap.core_fraction ** 1.2
    return sil


def test_slow_host_is_not_throttled():
    sil = _sil(target=5336, unthrottled=1500)
    r = sil.limit_speed()
    assert r["core_fraction"] == 1.0 and r["method"] == "none" and sil.cpucap.calls == []


def test_fast_host_is_throttled_to_the_target():
    sil = _sil(target=5336, unthrottled=16000)
    r = sil.limit_speed()
    assert r["dmips"] <= 5336 * 1.03
    assert r["dmips"] >= 5336 * 0.85
    assert r["method"] == "fake-per-vcpu"


def test_no_target_or_no_probe_does_nothing():
    sil = _sil(target=0, unthrottled=16000)
    assert sil.limit_speed() is None
    sil = _sil(target=5336, unthrottled=16000)
    sil.has_dhrystone = lambda: False
    assert sil.limit_speed() is None
