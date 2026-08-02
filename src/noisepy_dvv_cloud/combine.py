"""Cross-component combination of per-pair dv/v series.

Two combiners, selectable per run (compare them on the smoke test):

- "hobiger": Hobiger et al. (2014) / Clements & Denolle (2023) convention.
  DVV = sum(CC^2 * dvv) / sum(CC^2); CC = sum(CC^3) / sum(CC^2).
  Deterministic and exactly reproduces the 2022 pipeline, but the CC^2 weight
  is a heuristic — CC measures waveform similarity, not dv/v variance.
- "inverse_variance": weights each pair by 1/sigma^2 (sigma from the
  Weaver/Clarke coherence floor), the statistically efficient estimator when
  the per-pair sigmas are trustworthy, with the standard 1/sqrt(sum w) error.

Both are pure numpy on identical inputs -> bit-reproducible given the same
CCF Parquet, codameter version, and config hash.
"""

from __future__ import annotations

import numpy as np


def hobiger_combine(per_pair: dict[str, dict]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """CC^2-weighted combination. Returns (dvv, cc, sigma); all-invalid epochs NaN."""
    dvv, cc, sig, valid = _stack(per_pair)
    w = np.where(valid & np.isfinite(cc), cc**2, 0.0)
    return _weighted(dvv, cc, sig, w, sigma_mode="weighted_mean")


def inverse_variance_combine(
    per_pair: dict[str, dict],
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """1/sigma^2-weighted combination with propagated error 1/sqrt(sum w)."""
    dvv, cc, sig, valid = _stack(per_pair)
    with np.errstate(divide="ignore", invalid="ignore"):
        w = np.where(valid & np.isfinite(sig) & (sig > 0), 1.0 / sig**2, 0.0)
    return _weighted(dvv, cc, sig, w, sigma_mode="propagated")


COMBINERS = {"hobiger": hobiger_combine, "inverse_variance": inverse_variance_combine}


def combine(per_pair: dict[str, dict], method: str = "hobiger"):
    return COMBINERS[method](per_pair)


def _stack(per_pair):
    pairs = list(per_pair)
    return (
        np.vstack([per_pair[p]["dvv"] for p in pairs]),
        np.vstack([per_pair[p]["cc"] for p in pairs]),
        np.vstack([per_pair[p]["sigma"] for p in pairs]),
        np.vstack([per_pair[p]["valid"] for p in pairs]),
    )


def _weighted(dvv, cc, sig, w, sigma_mode):
    wsum = w.sum(axis=0)
    ok = wsum > 0
    n = dvv.shape[1]
    out_dvv = np.full(n, np.nan)
    out_cc = np.full(n, np.nan)
    out_sig = np.full(n, np.nan)
    with np.errstate(invalid="ignore"):
        out_dvv[ok] = np.nansum(w * dvv, axis=0)[ok] / wsum[ok]
        # CC reported with the published sum(CC^3)/sum(CC^2) convention when
        # weights are CC^2; otherwise a weighted mean of CC.
        if sigma_mode == "weighted_mean":
            out_cc[ok] = np.nansum(np.where(w > 0, cc**3, 0.0), axis=0)[ok] / wsum[ok]
            out_sig[ok] = np.nansum(w * sig, axis=0)[ok] / wsum[ok]
        else:
            out_cc[ok] = np.nansum(w * cc, axis=0)[ok] / wsum[ok]
            out_sig[ok] = 1.0 / np.sqrt(wsum[ok])
    return out_dvv, out_cc, out_sig
