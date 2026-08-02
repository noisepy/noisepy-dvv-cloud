"""Stage 2: robust dv/v with codameter, per station.

Reads the Parquet CCF dataset, runs codameter's pipeline per cross-component
pair and band, combines components with Hobiger CC^2 weighting, writes the
dv/v Parquet product (Clements-Denolle column convention + DVV_ERR).

codameter contract (pure numpy, in memory): ccfs [ndays, nlag] float64,
two-sided symmetric lag axis t in seconds, UNFILTERED input (band-passing is
internal to the estimators).
"""

from __future__ import annotations

import hashlib
import json
import logging

import numpy as np
import pandas as pd

from . import constants, parquet_io
from .combine import hobiger_combine

logger = logging.getLogger(__name__)


def dvv_config(use_case: str | None, band: tuple[float, float]) -> tuple[dict, float]:
    """Processing config: literature-grounded defaults from codameter.use_cases,
    with the band forced to the requested octave."""
    from codameter.use_cases import eps_max, recommend

    if use_case:
        cfg = recommend(use_case, band=band)
        eps = eps_max(use_case)
    else:
        # Clements-Denolle-like fallback: stretching, fixed reference,
        # 90-day trailing stack, coda 0..20/fmin seconds
        cfg = {
            "estimator": "stretching (TS)",
            "band": band,
            "window": (0.0, 20.0 / band[0]),
            "stack": 90,
            "reference": "fixed",
            "gate": True,
        }
        eps = 0.05
    return cfg, eps


def run(
    stations: list[str],
    ccf_root: str,
    output: str,
    use_case: str | None = None,
    bands: tuple = constants.FREQ_BANDS,
) -> None:
    for sta_id in stations:
        network, station = sta_id.split(".")[:2]
        for band in bands:
            try:
                df = station_dvv(ccf_root, network, station, band, use_case)
            except FileNotFoundError:
                logger.warning("no CCFs for %s.%s, skipping", network, station)
                break
            cfg, _ = dvv_config(use_case, band)
            chash = hashlib.sha1(json.dumps(cfg, default=str).encode()).hexdigest()[:12]
            path = parquet_io.write_dvv(output, df, network, station, band, chash)
            logger.info("wrote %s", path)


def station_dvv(
    ccf_root: str,
    network: str,
    station: str,
    band: tuple[float, float],
    use_case: str | None,
) -> pd.DataFrame:
    """dv/v time series for one station and band, combined across EN, EZ, NZ."""
    from codameter.deviations import run_pipeline
    from codameter.uq_measurement import weaver_stretching_error

    cfg, eps = dvv_config(use_case, band)

    per_pair: dict[str, dict] = {}
    days_ref = None
    for pair in constants.CROSS_COMPONENTS:
        ccfs, days, t, fs = parquet_io.read_ccf_matrix(ccf_root, network, station, pair)
        if days_ref is None:
            days_ref = days
        elif len(days) != len(days_ref) or (days != days_ref).any():
            # align on the intersection of dates, Clements-Denolle style
            common = np.intersect1d(days, days_ref)
            keep = np.isin(days, common)
            ccfs, days = ccfs[keep], days[keep]
            for p in per_pair.values():
                k = np.isin(p["days"], common)
                p["dvv"], p["valid"], p["days"] = p["dvv"][k], p["valid"][k], p["days"][k]
            days_ref = common

        dvv, valid = run_pipeline(ccfs, t, fs, cfg, eps_max=eps)

        # per-epoch coherence sigma (Weaver/Clarke 2011): needs the stretching CC;
        # run_pipeline gates on CC internally but does not return it, so recompute
        # a cheap proxy from the gated validity for now.
        # TODO(first smoke test): surface CC from run_pipeline (or call
        # measure_stretching directly) so DVV_ERR uses weaver_stretching_error.
        cc = np.where(valid, 0.8, np.nan)
        f_center = float(np.sqrt(band[0] * band[1]))
        sigma = np.array(
            [weaver_stretching_error(c, f_center, cfg["window"][0], cfg["window"][1])
             if np.isfinite(c) else np.nan for c in cc]
        )
        per_pair[pair] = {"dvv": dvv, "cc": cc, "sigma": sigma, "valid": valid, "days": days}

    dvv, cc, err = hobiger_combine(per_pair)
    return pd.DataFrame(
        {
            "DATE": pd.to_datetime(days_ref).date,
            "DVV": dvv * 100.0,  # fraction -> percent, Clements-Denolle convention
            "DVV_ERR": err * 100.0,
            "CC": cc,
        }
    )
