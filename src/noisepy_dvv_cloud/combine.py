"""Cross-component combination, Hobiger et al. (2014) / Clements & Denolle (2023).

DVV = sum(CC^2 * dvv) / sum(CC^2)  across component pairs, per epoch;
CC   = sum(CC^3) / sum(CC^2)       (the published convention);
error propagates as a CC^2-weighted mean of per-pair sigmas.
"""

from __future__ import annotations

import numpy as np


def hobiger_combine(per_pair: dict[str, dict]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Combine per-component dv/v series (same date axis) into one series.

    per_pair: {pair: {"dvv", "cc", "sigma", "valid"}} with equal-length arrays.
    Returns (dvv, cc, sigma); epochs with no valid component are NaN.
    """
    pairs = list(per_pair)
    dvv = np.vstack([per_pair[p]["dvv"] for p in pairs])
    cc = np.vstack([per_pair[p]["cc"] for p in pairs])
    sig = np.vstack([per_pair[p]["sigma"] for p in pairs])
    valid = np.vstack([per_pair[p]["valid"] for p in pairs])

    w = np.where(valid & np.isfinite(cc), cc**2, 0.0)
    wsum = w.sum(axis=0)
    ok = wsum > 0

    out_dvv = np.full(dvv.shape[1], np.nan)
    out_cc = np.full(dvv.shape[1], np.nan)
    out_sig = np.full(dvv.shape[1], np.nan)
    with np.errstate(invalid="ignore"):
        out_dvv[ok] = np.nansum(w * dvv, axis=0)[ok] / wsum[ok]
        out_cc[ok] = np.nansum(np.where(w > 0, cc**3, 0.0), axis=0)[ok] / wsum[ok]
        out_sig[ok] = np.nansum(w * sig, axis=0)[ok] / wsum[ok]
    return out_dvv, out_cc, out_sig
