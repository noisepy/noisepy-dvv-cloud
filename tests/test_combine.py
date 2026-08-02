import numpy as np

from noisepy_dvv_cloud.combine import hobiger_combine


def _pair(dvv, cc, valid=None):
    dvv = np.asarray(dvv, float)
    cc = np.asarray(cc, float)
    if valid is None:
        valid = np.isfinite(dvv)
    return {"dvv": dvv, "cc": cc, "sigma": np.full_like(dvv, 0.1), "valid": valid}


def test_equal_cc_is_plain_mean():
    per_pair = {
        "EN": _pair([1.0, 2.0], [0.9, 0.9]),
        "EZ": _pair([3.0, 4.0], [0.9, 0.9]),
    }
    dvv, cc, sig = hobiger_combine(per_pair)
    np.testing.assert_allclose(dvv, [2.0, 3.0])
    np.testing.assert_allclose(cc, [0.9, 0.9])


def test_cc_squared_weighting():
    per_pair = {
        "EN": _pair([1.0], [1.0]),
        "EZ": _pair([0.0], [0.5]),
    }
    dvv, _, _ = hobiger_combine(per_pair)
    # weights 1.0 and 0.25 -> (1*1 + 0.25*0) / 1.25
    np.testing.assert_allclose(dvv, [0.8])


def test_all_invalid_epoch_is_nan():
    per_pair = {
        "EN": _pair([1.0, np.nan], [0.9, np.nan], valid=np.array([True, False])),
        "EZ": _pair([1.0, np.nan], [0.9, np.nan], valid=np.array([True, False])),
    }
    dvv, cc, sig = hobiger_combine(per_pair)
    assert np.isfinite(dvv[0]) and np.isnan(dvv[1])
