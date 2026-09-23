"""Parquet writers/readers for CCF and dv/v products.

Authoritative schema documentation: docs/parquet-schemas.md. Keep that file and
this module in sync — the schemas are the public contract of this pipeline.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq

from . import constants

logger = logging.getLogger(__name__)

CCF_SCHEMA = pa.schema(
    [
        ("network", pa.string()),
        ("station", pa.string()),
        ("location", pa.string()),
        ("pair", pa.string()),  # EN, EZ, NZ, EE, NN, ZZ
        ("date", pa.date32()),
        # two-sided, length 2*maxlag*fs+1, exactly as NoisePy returns it --
        # its true zero lag is at index n//2+1, NOT the midpoint. Read through
        # read_ccf_matrix, which centres it. See center_on_zero_lag below.
        ("ccf", pa.list_(pa.float32())),
        ("fs", pa.float32()),
        ("maxlag_s", pa.float32()),
        ("nwindows", pa.int32()),  # windows stacked into this day
        ("config_hash", pa.string()),  # provenance: hash of the effective config
    ]
)

DVV_SCHEMA = pa.schema(
    [
        # lowercase snake_case throughout; legacy Clements-Denolle mapping
        # (DATE/DVV/CC -> date/dvv/cc) documented in docs/parquet-schemas.md
        ("date", pa.date32()),
        ("dvv", pa.float64()),  # percent
        ("dvv_err", pa.float64()),  # percent, 1-sigma total (within + methodological)
        ("dvv_err_within", pa.float64()),  # percent, coherence/Weaver floor
        ("dvv_err_method", pa.float64()),  # percent, processing-ensemble spread
        ("cc", pa.float64()),
        ("n_members", pa.int32()),  # ensemble members contributing at this epoch
        ("network", pa.string()),
        ("station", pa.string()),
        ("band", pa.string()),  # "2.0-4.0"
        ("config_hash", pa.string()),
    ]
)


def ccf_dataset_path(root: str) -> str:
    """CCF products live at <root>/network=NET/station=STA/pair=XY/year=YYYY/*.parquet."""
    return root.rstrip("/")


def write_ccf_day_batch(
    root: str,
    rows: list[dict],
    storage_options: dict | None = None,
) -> None:
    """Write one shard's daily CCFs, Hive-partitioned.

    Each row: keys matching CCF_SCHEMA, with `ccf` a 1-D float array.
    Files are named by shard content hash so Spot-retried shards overwrite
    themselves (idempotent re-runs) instead of duplicating rows.
    """
    table = pa.Table.from_pylist(rows, schema=CCF_SCHEMA)
    year = pa.compute.year(table["date"])
    table = table.append_column("year", year)
    ds.write_dataset(
        table,
        ccf_dataset_path(root),
        format="parquet",
        partitioning=ds.partitioning(
            pa.schema(
                [("network", pa.string()), ("station", pa.string()),
                 ("pair", pa.string()), ("year", pa.int32())]
            ),
            flavor="hive",
        ),
        existing_data_behavior="overwrite_or_ignore",
        basename_template="shard-{i}-" + _content_hash(rows) + ".parquet",
    )


# NoisePy's returned lag axis is one sample wider than the trace it labels, so
# the sample it calls t = 0 is NOT the zero lag of the data.
#
# noise_module.correlate builds the two-sided function with
# `np.fft.ifftshift(ifft(crap, Nfft))`, which puts lag zero at index Nfft/2 of
# an Nfft-sample array. It then trims with
#
#     t   = np.arange(-Nfft2 + 1, Nfft2) * dt      # Nfft - 1 entries
#     ind = np.where(np.abs(t) <= maxlag)[0]
#
# and applies `ind` -- indices into a length-(Nfft-1) axis -- to the
# length-Nfft data. Element j of the result therefore holds lag
# (ind[j] - Nfft2) * dt while being labelled (ind[j] - Nfft2 + 1) * dt: every
# label is one sample too large, and true zero lag lands at index n // 2 + 1
# rather than at the midpoint.
#
# Measured on the campaign's own products (CI.LJR, 2022-2023, 2561 lags): an
# autocorrelation must be symmetric about true zero lag, and ZZ, EE and NN are
# each symmetric to 1.8e-8 about index 1281 while the midpoint 1280 gives 0.43.
# That is not a judgement call.
#
# Why it was not obvious: `correlate` subtracts the frequency-domain mean,
# which removes a delta at true zero lag, so argmax sits on the NEIGHBOURING
# sample. Reading argmax as "the autocorrelation peaks at zero lag" confirms
# the wrong index.
#
# So the offset is MEASURED from an autocorrelation at read time rather than
# assumed from the version pin: symmetry identifies zero lag exactly, and a
# NoisePy that changes this convention is then followed rather than overridden
# by a stale constant.
# Fallback only. The offset is MEASURED per station by `measure_zero_lag_index`
# below; this value is what we use when no autocorrelation is available to
# measure from, and it is what noisepy-seis 0.9.93 produces.
ZERO_LAG_INDEX_OFFSET = 1

# (root, network, station) -> measured zero-lag index. One probe per station
# per process; the answer cannot change between pairs of the same station.
_ZERO_LAG_CACHE: dict[tuple[str, str, str], int] = {}


def _asymmetry(trace: np.ndarray, centre: int) -> float:
    """RMS mismatch between the two sides of `trace` about `centre`, relative
    to the trace's own RMS. Zero for a perfectly symmetric function."""
    k = min(centre, len(trace) - 1 - centre)
    if k < 1:
        return np.inf
    scale = np.sqrt(np.mean(trace**2))
    if not np.isfinite(scale) or scale == 0:
        return np.inf
    left = trace[centre - k : centre][::-1]
    right = trace[centre + 1 : centre + 1 + k]
    return float(np.sqrt(np.mean((left - right) ** 2)) / scale)


def measure_zero_lag_index(auto: np.ndarray, max_shift: int = 3,
                           margin: float = 100.0) -> int | None:
    """Locate zero lag in an AUTOcorrelation by symmetry, or None if unclear.

    An autocorrelation is the inverse transform of a real, non-negative
    spectrum, so it is symmetric about zero lag exactly -- not statistically.
    On real products the winning candidate scores ~1e-8 against ~4e-1 for its
    neighbour, so the test is decisive rather than a best-fit.

    `margin` demands the winner be that much better than the runner-up. Being
    unsure returns None and the caller falls back to the documented constant,
    because a confidently wrong lag axis is worse than a documented assumption.
    Searching only +/-`max_shift` around the midpoint keeps a noisy trace from
    locking onto a spurious symmetry far away.
    """
    trace = np.asarray(auto, dtype=float)
    if trace.ndim == 2:
        trace = np.nanmean(trace, axis=0)
    n = trace.shape[0]
    mid = n // 2
    scored = sorted(
        (_asymmetry(trace, c), c)
        for c in range(mid - max_shift, mid + max_shift + 1)
        if 0 < c < n - 1
    )
    if len(scored) < 2:
        return None
    (best, centre), (second, _) = scored[0], scored[1]
    if not np.isfinite(best) or best * margin > second:
        return None
    return centre


def _probe_zero_lag(dataset, root: str, network: str, station: str,
                    have: np.ndarray | None, pair: str) -> int:
    """Measured zero-lag index for this station, cached, with a logged fallback."""
    key = (root, network, station)
    if key in _ZERO_LAG_CACHE:
        return _ZERO_LAG_CACHE[key]

    probe = have if pair in constants.AUTO_COMPONENTS else None
    if probe is None:
        for auto in constants.AUTO_COMPONENTS:
            try:
                tbl = dataset.scanner(
                    columns=["ccf"],
                    filter=(
                        (ds.field("network") == network)
                        & (ds.field("station") == station)
                        & (ds.field("pair") == auto)
                    ),
                ).head(1)
            except Exception:  # noqa: BLE001 - any read problem means "cannot probe"
                continue
            if tbl.num_rows:
                probe = np.asarray(tbl.column("ccf")[0].as_py(), dtype=float)
                break

    n = have.shape[1] if have is not None else None
    measured = measure_zero_lag_index(probe) if probe is not None else None
    if measured is None:
        measured = (n // 2 + ZERO_LAG_INDEX_OFFSET) if n else 0
        logger.warning(
            "%s.%s: no autocorrelation to locate zero lag from; assuming index "
            "%d (noisepy-seis 0.9.93 convention). Correlate EE, NN or ZZ to "
            "have this measured instead.", network, station, measured,
        )
    elif n and measured != n // 2 + ZERO_LAG_INDEX_OFFSET:
        logger.warning(
            "%s.%s: zero lag measured at index %d, not the %d this NoisePy "
            "version is documented to produce. Using the measurement; check "
            "the noisepy-seis pin.", network, station, measured,
            n // 2 + ZERO_LAG_INDEX_OFFSET,
        )
    _ZERO_LAG_CACHE[key] = measured
    return measured


def center_on_zero_lag(ccfs: np.ndarray, fs: float,
                       zero: int | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Trim to a genuinely symmetric two-sided window centred on zero lag.

    Returns (ccfs, t). Trimming rather than relabelling because the contract
    this module publishes -- and codameter's -- is a *symmetric* two-sided
    axis; relabelling would satisfy the arithmetic and quietly break that.
    Costs two samples at the acausal end, 50 ms out of 32 s.
    """
    n = ccfs.shape[1]
    if zero is None:
        zero = n // 2 + ZERO_LAG_INDEX_OFFSET
    k = min(zero, n - 1 - zero)
    return ccfs[:, zero - k : zero + k + 1], np.arange(-k, k + 1) / fs


def read_ccf_matrix(
    root: str,
    network: str,
    station: str,
    pair: str,
    storage_options: dict | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, float]:
    """Assemble the [ndays, nlag] matrix codameter expects.

    Returns (ccfs, days, t, fs): chronologically sorted daily CCFs, the date
    axis, the two-sided lag axis in seconds, and the sampling rate.
    """
    dataset = ds.dataset(ccf_dataset_path(root), format="parquet", partitioning="hive")
    table = dataset.to_table(
        filter=(
            (ds.field("network") == network)
            & (ds.field("station") == station)
            & (ds.field("pair") == pair)
        )
    )
    df = table.to_pandas().sort_values("date")
    if df.empty:
        raise FileNotFoundError(f"no CCFs for {network}.{station} {pair} under {root}")

    # Two shards can cover the same day. Shard files are content-hash-named, so
    # a re-run with a DIFFERENT --day_group_size writes a new file instead of
    # overwriting the old one and both land in the dataset: the documented
    # idempotence holds for an identical command, not for a different sharding.
    # Keeping both silently lengthens this matrix past its own date axis, and
    # the failure surfaces much later and unrecognisably, as "All arrays must
    # be of the same length" from a DataFrame constructor in dvv.station_dvv
    # (found 2026-09-23: a 10-day smoke shard overlapped a 30-day campaign
    # shard and added 10 rows to a 660-day series).
    #
    # Same code and config produce the same CCF for a day, so the duplicates
    # agree; where they do not, the day stacked from more windows is the better
    # product. config_hash breaks the remaining tie deterministically.
    dup = int(df["date"].duplicated().sum())
    if dup:
        df = (
            df.sort_values(["date", "nwindows", "config_hash"])
            .drop_duplicates(subset="date", keep="last")
            .sort_values("date")
        )
        logger.warning(
            "%s.%s %s: %d duplicate day(s) from overlapping shards; kept the "
            "one with the most windows stacked", network, station, pair, dup
        )
    fs = float(df["fs"].iloc[0])
    ccfs = np.vstack(df["ccf"].to_numpy()).astype(np.float64)
    days = pd.to_datetime(df["date"]).to_numpy()
    zero = _probe_zero_lag(dataset, root, network, station, ccfs, pair)
    ccfs, t = center_on_zero_lag(ccfs, fs, zero)
    return ccfs, days, t, fs


def write_dvv(
    root: str,
    df: pd.DataFrame,
    network: str,
    station: str,
    band: tuple[float, float],
    config_hash: str,
    storage_options: dict | None = None,
) -> str:
    """Write one station's dv/v series: <root>/band=fmin-fmax/NET.STA.parquet."""
    band_str = f"{band[0]}-{band[1]}"
    df = df.assign(network=network, station=station, band=band_str, config_hash=config_hash)
    table = pa.Table.from_pandas(df, schema=DVV_SCHEMA, preserve_index=False)
    path = f"{root.rstrip('/')}/band={band_str}/{network}.{station}.parquet"
    if "://" not in path:
        # S3 "directories" are implicit; local filesystems are not — without
        # this the local smoke test dies on the first band (found 2026-08-08)
        import os

        os.makedirs(os.path.dirname(path), exist_ok=True)
    pq.write_table(table, path)
    return path


def _content_hash(rows: list[dict]) -> str:
    import hashlib

    key = "|".join(
        f"{r['network']}.{r['station']}.{r['pair']}.{r['date']}" for r in rows[:100]
    )
    return hashlib.sha1(key.encode()).hexdigest()[:12]
