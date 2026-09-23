"""The returned lag axis must be centred on the data's real zero lag.

NoisePy labels every returned sample one lag too large (see the derivation
above `center_on_zero_lag`), so the sample it calls t = 0 is not zero lag. An
autocorrelation is symmetric about true zero lag and nothing else, which is the
only test that does not just restate the arithmetic.
"""

import numpy as np

from noisepy_dvv_cloud.parquet_io import ZERO_LAG_INDEX_OFFSET, center_on_zero_lag

FS = 40.0


def _noisepy_style(n=2561, rng=None):
    """A trace built the way noise_module.correlate returns one: symmetric
    about index n // 2 + ZERO_LAG_INDEX_OFFSET, not about the midpoint."""
    rng = rng or np.random.default_rng(0)
    zero = n // 2 + ZERO_LAG_INDEX_OFFSET
    k = min(zero, n - 1 - zero)
    half = rng.normal(size=k)
    tr = np.zeros(n)
    tr[zero] = 5.0
    tr[zero + 1 : zero + 1 + k] = half
    tr[zero - k : zero] = half[::-1]
    return tr[None, :]


def _asymmetry(tr):
    c = len(tr) // 2
    k = min(c, len(tr) - 1 - c)
    return np.sqrt(np.mean((tr[c - k : c][::-1] - tr[c + 1 : c + 1 + k]) ** 2))


def test_centering_makes_an_autocorrelation_symmetric():
    raw = _noisepy_style()
    assert _asymmetry(raw[0]) > 1e-3, "fixture is not off-centre; test is vacuous"
    out, t = center_on_zero_lag(raw, FS)
    assert _asymmetry(out[0]) < 1e-12
    assert out.shape[1] % 2 == 1
    assert t[len(t) // 2] == 0.0
    np.testing.assert_allclose(t, -t[::-1], atol=1e-12)


def test_zero_lag_sample_is_the_peak_once_centred():
    raw = _noisepy_style()
    out, t = center_on_zero_lag(raw, FS)
    assert int(np.argmax(out[0])) == len(t) // 2
    assert t[int(np.argmax(out[0]))] == 0.0


def test_axis_spacing_is_one_over_fs():
    out, t = center_on_zero_lag(_noisepy_style(), FS)
    np.testing.assert_allclose(np.diff(t), 1.0 / FS)
    assert out.shape[1] == len(t)
