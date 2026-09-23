"""The ensemble aggregation, pinned against codameter itself.

The point of these tests is the pair of them: `ensemble` must agree with
codameter exactly on a complete ensemble, and must NOT agree with it when a
member is missing -- because that disagreement is the whole reason it exists.
"""

import numpy as np
import pytest

from noisepy_dvv_cloud.dvv import ensemble

codameter_uq = pytest.importorskip("codameter.uq_measurement")


def _members(rng, n_members=5, n_time=40):
    m = {f"m{i}": rng.normal(0, 1e-3, n_time) for i in range(n_members)}
    w = {k: rng.uniform(1e-4, 5e-4, n_time) for k in m}
    return m, w


def test_matches_codameter_on_a_complete_ensemble():
    """Same estimator, not a reimplementation of one."""
    m, w = _members(np.random.default_rng(0))
    mean, within, method, total, n = ensemble(m, w)
    ref = codameter_uq.processing_ensemble(m, within_sigma=w)

    np.testing.assert_allclose(mean, ref.mean)
    np.testing.assert_allclose(within, ref.within_std)
    np.testing.assert_allclose(method, ref.methodological_std)
    np.testing.assert_allclose(total, ref.total_std)
    assert (n == len(m)).all()


def test_one_dead_member_does_not_discard_the_others():
    """The CI.LJR smoke-run failure, 2026-09-22.

    reference="moving" returned nothing on a ten-day series. codameter's plain
    stack.mean then made dv/v NaN at EVERY epoch even though four members had
    produced a measurement, while n_members still reported 4.
    """
    rng = np.random.default_rng(1)
    m, w = _members(rng)
    m["m4"] = np.full_like(m["m4"], np.nan)   # the ref_swap member

    ref = codameter_uq.processing_ensemble(m, within_sigma=w)
    assert np.isnan(ref.mean).all(), "codameter changed; revisit ensemble()"

    mean, within, method, total, n = ensemble(m, w)
    assert np.isfinite(mean).all()
    assert np.isfinite(total).all()
    assert (n == 4).all()

    # and the surviving four are exactly what a complete ensemble of them gives
    alive = {k: v for k, v in m.items() if k != "m4"}
    ref4 = codameter_uq.processing_ensemble(
        alive, within_sigma={k: w[k] for k in alive})
    np.testing.assert_allclose(mean, ref4.mean)
    np.testing.assert_allclose(total, ref4.total_std)


def test_epoch_with_one_survivor_is_dropped():
    """sqrt(within^2 + method^2) with a single member would report method = 0,
    which is unknown, not zero. Better no number than a confident one."""
    rng = np.random.default_rng(2)
    m, w = _members(rng, n_members=3, n_time=4)
    for k in ("m1", "m2"):
        m[k][2] = np.nan
    mean, _, _, total, n = ensemble(m, w)
    assert n[2] == 1
    assert np.isnan(mean[2]) and np.isnan(total[2])
    assert np.isfinite(mean[[0, 1, 3]]).all()


def test_all_dead_epoch_is_nan_not_an_error():
    m, w = _members(np.random.default_rng(3), n_members=3, n_time=3)
    for k in m:
        m[k][1] = np.nan
    mean, _, _, total, n = ensemble(m, w)
    assert n[1] == 0
    assert np.isnan(mean[1]) and np.isnan(total[1])
