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
    # cc > 0, not just isfinite(cc): squaring an anticorrelated pair's CC
    # produces a positive, plausible-looking weight, so a cc = -0.3 epoch
    # would otherwise contribute 0.09 of the weight with the wrong sign of
    # coda similarity behind it.
    w = np.where(valid & np.isfinite(cc) & (cc > 0), cc**2, 0.0)
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


def _wmean(values, w):
    """Weighted mean, renormalised over the entries that actually contribute.

    np.nansum drops a non-finite value from the numerator but nothing removes
    its weight from the denominator, which silently biases the result toward
    zero. Zero the weight per quantity instead, so each output is normalised by
    exactly the weight that went into it.
    """
    wv = np.where(np.isfinite(values), w, 0.0)
    wsum = wv.sum(axis=0)
    ok = wsum > 0
    out = np.full(values.shape[1], np.nan)
    with np.errstate(invalid="ignore"):
        out[ok] = np.nansum(wv * values, axis=0)[ok] / wsum[ok]
    return out


def _weighted(dvv, cc, sig, w, sigma_mode):
    ok = w.sum(axis=0) > 0
    n = dvv.shape[1]
    out_dvv = _wmean(dvv, w)
    out_cc = np.full(n, np.nan)
    out_sig = np.full(n, np.nan)
    with np.errstate(invalid="ignore"):
        # CC reported with the published sum(CC^3)/sum(CC^2) convention when
        # weights are CC^2; otherwise a weighted mean of CC.
        if sigma_mode == "weighted_mean":
            wc = np.where(np.isfinite(cc), w, 0.0)
            wcsum = wc.sum(axis=0)
            okc = wcsum > 0
            out_cc[okc] = np.nansum(np.where(wc > 0, cc**3, 0.0), axis=0)[okc] / wcsum[okc]
            # sigma renormalised over the pairs with a finite sigma: epochs
            # masked by the Weaver CC-domain guard carry NaN, and counting
            # their weight in the denominator biased dvv_err_within low.
            out_sig = _wmean(sig, w)
        else:
            out_cc = _wmean(cc, w)
            out_sig[ok] = 1.0 / np.sqrt(w.sum(axis=0)[ok])
    return out_dvv, out_cc, out_sig
