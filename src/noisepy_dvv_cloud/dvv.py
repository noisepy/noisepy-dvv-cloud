"""Stage 2: robust dv/v with codameter, per station.

Reads the Parquet CCF dataset, runs a deterministic processing ENSEMBLE per
station and band — the baseline config from codameter.use_cases plus four
perturbations (stack length halved/doubled, coda window shifted later,
reference scheme swapped) — combines cross-components per member, then feeds
the members to codameter.uq_measurement.processing_ensemble. The reported
uncertainty separates:

  dvv_err_within  — per-epoch coherence floor (Weaver/Clarke 2011)
  dvv_err_method  — spread across processing choices (law of total variance)
  dvv_err         — total = sqrt(within^2 + method^2)

codameter contract (pure numpy, in memory): ccfs [ndays, nlag] float64,
two-sided symmetric lag axis t in seconds, UNFILTERED input (band-passing is
internal to the estimators).
"""

from __future__ import annotations

import hashlib
import json
import logging
from functools import reduce

import numpy as np
import pandas as pd

from . import constants, parquet_io
from .combine import combine

logger = logging.getLogger(__name__)


def dvv_config(use_case: str | None, band: tuple[float, float]) -> tuple[dict, float]:
    """Baseline processing config: literature-grounded defaults from
    codameter.use_cases, with the band forced to the requested octave."""
    from codameter.use_cases import eps_max, recommend

    if use_case:
        cfg = recommend(use_case, band=band)
        eps = eps_max(use_case)
    else:
        # Clements-Denolle-like fallback: stretching, fixed reference,
        # 90-day trailing stack, coda 2/fmin..20/fmin seconds.
        #
        # The lower bound is a GUARD, not a tuned choice. It used to be 0.0,
        # which put the window on the zero-lag peak of a single-station
        # correlation rather than on its coda, and made the Weaver coherence
        # floor undefined -- codameter requires 0 < t1 < t2, so four of the
        # five ensemble members raised before producing anything. Two periods
        # at the low corner clears the zero-lag artifact and scales with the
        # band. Pass --use-case for a window that is actually calibrated for
        # the target process.
        cfg = {
            "estimator": "stretching (TS)",
            "band": band,
            "window": (2.0 / band[0], 20.0 / band[0]),
            "stack": 90,
            "reference": "fixed",
            "gate": True,
        }
        eps = 0.05
    _check_window(cfg, use_case)
    return cfg, eps


def _check_window(cfg: dict, use_case: str | None) -> None:
    """Fail at config time, not deep inside the per-epoch sigma loop.

    The Weaver floor requires 0 < t1 < t2. Reaching it with a bad window
    raises once per epoch per pair per member, after the correlate stage has
    already been paid for, and the message says nothing about where the window
    came from.
    """
    t1, t2 = cfg["window"]
    if not 0 < t1 < t2:
        source = f"use case {use_case!r}" if use_case else "the no-use-case fallback"
        raise ValueError(
            f"coda window {(t1, t2)} from {source} is not 0 < t1 < t2; "
            "the Weaver coherence floor is undefined there"
        )


def ensemble_configs(cfg: dict) -> dict[str, dict]:
    """Baseline + 4 deterministic perturbations. Small on purpose: each member
    costs a full run_pipeline pass per component pair. Widen only with a
    budget check (5 members x 3 pairs x 4 bands per station)."""
    t1, t2 = cfg["window"]
    shift = 0.25 * (t2 - t1)
    return {
        "baseline": dict(cfg),
        "stack_half": {**cfg, "stack": max(1, round(cfg["stack"] / 2))},
        "stack_double": {**cfg, "stack": cfg["stack"] * 2},
        "window_late": {**cfg, "window": (t1 + shift, t2 + shift)},
        "ref_swap": {**cfg, "reference": "moving" if cfg["reference"] == "fixed" else "fixed"},
    }


def run(
    stations: list[str],
    ccf_root: str,
    output: str,
    use_case: str | None = None,
    bands: tuple = constants.FREQ_BANDS,
    combine_method: str = "hobiger",
) -> None:
    for sta_id in stations:
        network, station = sta_id.split(".")[:2]
        try:
            data, days = load_aligned(ccf_root, network, station)
        except FileNotFoundError:
            logger.warning("no CCFs for %s.%s, skipping", network, station)
            continue
        for band in bands:
            cfg, _ = dvv_config(use_case, band)
            df = station_dvv(data, days, band, use_case, combine_method)
            chash = hashlib.sha1(
                json.dumps({**cfg, "combine": combine_method}, default=str).encode()
            ).hexdigest()[:12]
            path = parquet_io.write_dvv(output, df, network, station, band, chash)
            logger.info("wrote %s", path)


def load_aligned(ccf_root: str, network: str, station: str):
    """Load the three cross-component CCF matrices on their common date axis."""
    raw = {
        pair: parquet_io.read_ccf_matrix(ccf_root, network, station, pair)
        for pair in constants.CROSS_COMPONENTS
    }
    common = reduce(np.intersect1d, (days for _, days, _, _ in raw.values()))
    if common.size == 0:
        raise FileNotFoundError(f"no common dates across components for {network}.{station}")
    data = {}
    for pair, (ccfs, days, t, fs) in raw.items():
        keep = np.isin(days, common)
        data[pair] = {"ccfs": ccfs[keep], "t": t, "fs": fs}
    return data, common


def _codameter_is_physical(version: str) -> bool:
    """True when run_pipeline returns physical dv/v rather than the stretch factor.

    Tolerant of non-PEP-440 versions (a source checkout can report
    ``0+unknown``): an unparseable version defaults to the modern convention,
    which is what pyproject pins.
    """
    from packaging.version import InvalidVersion, Version

    try:
        v = Version(version)
    except InvalidVersion:
        logger.warning("unparseable codameter version %r; assuming >= 0.4", version)
        return True
    # setuptools_scm's fallback for a checkout with no tag is "0+unknown",
    # which IS valid PEP 440 and sorts below 0.4 -- taking it at face value
    # would silently negate dv/v. A bare 0 release carries no information.
    if v.release == (0,):
        logger.warning("uninformative codameter version %r; assuming >= 0.4", version)
        return True
    return v >= Version("0.4")


def station_dvv(
    data: dict,
    days: np.ndarray,
    band: tuple[float, float],
    use_case: str | None,
    combine_method: str,
) -> pd.DataFrame:
    """Ensemble dv/v for one station and band, combined across EN, EZ, NZ."""
    from codameter import __version__ as _codameter_version
    from codameter.deviations import run_pipeline

    _CODAMETER_PHYSICAL = _codameter_is_physical(_codameter_version)
    from codameter.uq_measurement import processing_ensemble, weaver_stretching_error

    cfg, eps = dvv_config(use_case, band)
    f_center = float(np.sqrt(band[0] * band[1]))

    members: dict[str, np.ndarray] = {}
    within: dict[str, np.ndarray] = {}
    baseline_cc = None
    for label, vcfg in ensemble_configs(cfg).items():
        per_pair = {}
        for pair, d in data.items():
            try:
                dvv, valid, cc = run_pipeline(
                    d["ccfs"], d["t"], d["fs"], vcfg, eps_max=eps, return_cc=True
                )
            except TypeError:
                # codameter <= 0.3.0 without return_cc
                # (Denolle-Lab/codameter#32): nominal CC on valid epochs
                dvv, valid = run_pipeline(d["ccfs"], d["t"], d["fs"], vcfg, eps_max=eps)
                cc = np.where(valid, 0.8, np.nan)
            # non-stretching estimators / inversion reference return NaN CC;
            # keep those epochs usable with the same nominal value
            cc = np.where(np.isfinite(cc), cc, np.where(valid, 0.8, np.nan))
            # SIGN CONVENTION (Gate 1 finding, 2026-08-08): codameter
            # run_pipeline < 0.4 returned the stretch factor
            # epsilon = -dv/v; codameter 0.4+ returns physical dv/v
            # natively (Denolle-Lab/codameter#36). Negate only on the old
            # convention so the stored column is always physical dv/v.
            if not _CODAMETER_PHYSICAL:
                dvv = -np.asarray(dvv)
            # real data produces epochs with cc <= 0 (glitch days,
            # anticorrelated coda); codameter's Weaver error correctly
            # requires cc in (0, 1] — mask those epochs instead of dying
            # (found on the Gate 1 full-year run, 2019 CI.RXH)
            sigma = np.array(
                [
                    weaver_stretching_error(
                        min(c, 1.0), f_center, vcfg["window"][0], vcfg["window"][1]
                    )
                    if np.isfinite(c) and 0.0 < c <= 1.0 + 1e-12
                    else np.nan
                    for c in cc
                ]
            )
            # Fold the sigma mask into `valid`. A NaN sigma alone does not
            # remove an epoch: both combiners weight on `valid`, so a masked
            # epoch kept its CC^2 weight, contributed its dv/v to the mean,
            # and dropped only out of the sigma numerator.
            valid = np.asarray(valid) & np.isfinite(sigma)
            per_pair[pair] = {"dvv": dvv, "cc": cc, "sigma": sigma, "valid": valid}
        m_dvv, m_cc, m_sig = combine(per_pair, method=combine_method)
        members[label] = m_dvv
        within[label] = m_sig
        if label == "baseline":
            baseline_cc = m_cc

    res = processing_ensemble(members, within_sigma=within)
    n_members = np.isfinite(np.vstack(list(members.values()))).sum(axis=0)

    return pd.DataFrame(
        {
            "date": pd.to_datetime(days).date,
            "dvv": res.mean * 100.0,  # fraction -> percent
            "dvv_err": res.total_std * 100.0,
            "dvv_err_within": res.within_std * 100.0,
            "dvv_err_method": res.methodological_std * 100.0,
            "cc": baseline_cc,
            "n_members": n_members.astype("int32"),
        }
    )
