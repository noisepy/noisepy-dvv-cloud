import pytest

from noisepy_dvv_cloud import constants
from noisepy_dvv_cloud.dvv import dvv_config, ensemble_configs


@pytest.mark.parametrize("band", constants.FREQ_BANDS)
def test_fallback_window_is_usable_for_every_ensemble_member(band):
    """Every member must satisfy the Weaver floor's 0 < t1 < t2.

    The fallback used to start the coda at 0.0, so four of the five members
    raised inside the per-epoch sigma loop -- after the correlate stage had
    already been paid for.
    """
    cfg, _ = dvv_config(None, band)
    for label, member in ensemble_configs(cfg).items():
        t1, t2 = member["window"]
        assert 0 < t1 < t2, f"{label} window {(t1, t2)} unusable for band {band}"


def test_fallback_window_scales_with_the_band():
    lo, _ = dvv_config(None, (1.0, 2.0))
    hi, _ = dvv_config(None, (8.0, 16.0))
    assert lo["window"] == (2.0, 20.0)
    assert hi["window"] == (0.25, 2.5)


def test_bad_use_case_window_fails_at_config_time(monkeypatch):
    """A bad window from codameter must raise here, naming the use case."""
    import codameter.use_cases as uc

    monkeypatch.setattr(uc, "recommend", lambda *a, **k: {"window": (0.0, 5.0)})
    monkeypatch.setattr(uc, "eps_max", lambda *a, **k: 0.05)
    with pytest.raises(ValueError, match="groundwater"):
        dvv_config("groundwater", (2.0, 4.0))
