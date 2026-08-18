import numpy as np

from noisepy_dvv_cloud.combine import hobiger_combine, inverse_variance_combine


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


def test_inverse_variance_weighting_and_error():
    per_pair = {
        "EN": {"dvv": np.array([1.0]), "cc": np.array([0.9]),
               "sigma": np.array([0.1]), "valid": np.array([True])},
        "EZ": {"dvv": np.array([0.0]), "cc": np.array([0.9]),
               "sigma": np.array([0.2]), "valid": np.array([True])},
    }
    dvv, cc, sig = inverse_variance_combine(per_pair)
    # weights 100 and 25 -> dvv = 100/125 = 0.8; sigma = 1/sqrt(125)
    np.testing.assert_allclose(dvv, [0.8])
    np.testing.assert_allclose(sig, [1.0 / np.sqrt(125.0)])


def test_anticorrelated_pair_gets_no_weight():
    """cc <= 0 must not contribute: cc**2 makes a negative CC look plausible."""
    per_pair = {
        "EN": _pair([1.0], [1.0]),
        "EZ": _pair([0.0], [0.5]),
        # same |cc| as EZ, so a cc**2 weight would pull the mean hard
        "NZ": {"dvv": np.array([-100.0]), "cc": np.array([-0.5]),
               "sigma": np.array([np.nan]), "valid": np.array([True])},
    }
    dvv, _, _ = hobiger_combine(per_pair)
    np.testing.assert_allclose(dvv, [0.8])  # identical to the two-pair result


def test_masked_sigma_does_not_bias_error_low():
    """A NaN sigma must drop its weight from the denominator, not just the sum.

    np.nansum kept the masked pair's weight while dropping its value, which
    reported dvv_err_within well below any contributing pair's sigma.

    NZ carries valid=True on purpose: that is the state the Weaver CC-domain
    guard produced before the mask was folded into `valid`, and it is the only
    combination that exposes the bias.
    """
    per_pair = {
        "EN": {"dvv": np.array([1.0]), "cc": np.array([0.9]),
               "sigma": np.array([0.10]), "valid": np.array([True])},
        "EZ": {"dvv": np.array([1.0]), "cc": np.array([0.9]),
               "sigma": np.array([0.10]), "valid": np.array([True])},
        "NZ": {"dvv": np.array([1.0]), "cc": np.array([0.9]),
               "sigma": np.array([np.nan]), "valid": np.array([True])},
    }
    _, _, sig = hobiger_combine(per_pair)
    np.testing.assert_allclose(sig, [0.10])  # not 0.0667 (= 2/3 of it)


def test_all_invalid_epoch_is_nan():
    per_pair = {
        "EN": _pair([1.0, np.nan], [0.9, np.nan], valid=np.array([True, False])),
        "EZ": _pair([1.0, np.nan], [0.9, np.nan], valid=np.array([True, False])),
    }
    dvv, cc, sig = hobiger_combine(per_pair)
    assert np.isfinite(dvv[0]) and np.isnan(dvv[1])
