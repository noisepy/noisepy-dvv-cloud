"""Parquet writers/readers for CCF and dv/v products.

Authoritative schema documentation: docs/parquet-schemas.md. Keep that file and
this module in sync — the schemas are the public contract of this pipeline.
"""

from __future__ import annotations

import datetime

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq

CCF_SCHEMA = pa.schema(
    [
        ("network", pa.string()),
        ("station", pa.string()),
        ("location", pa.string()),
        ("pair", pa.string()),  # EN, EZ, NZ, EE, NN, ZZ
        ("date", pa.date32()),
        ("ccf", pa.list_(pa.float32())),  # two-sided, length 2*maxlag*fs+1
        ("fs", pa.float32()),
        ("maxlag_s", pa.float32()),
        ("nwindows", pa.int32()),  # windows stacked into this day
        ("config_hash", pa.string()),  # provenance: hash of the effective config
    ]
)

DVV_SCHEMA = pa.schema(
    [
        # Clements-Denolle column convention (docs/parquet-schemas.md)
        ("DATE", pa.date32()),
        ("DVV", pa.float64()),  # percent
        ("DVV_ERR", pa.float64()),  # percent, 1-sigma
        ("CC", pa.float64()),
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
    fs = float(df["fs"].iloc[0])
    ccfs = np.vstack(df["ccf"].to_numpy()).astype(np.float64)
    nlag = ccfs.shape[1]
    # two-sided symmetric lag axis, codameter convention
    half = (nlag - 1) // 2
    t = np.arange(-half, half + 1) / fs
    days = pd.to_datetime(df["date"]).to_numpy()
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
    pq.write_table(table, path)
    return path


def _content_hash(rows: list[dict]) -> str:
    import hashlib

    key = "|".join(
        f"{r['network']}.{r['station']}.{r['pair']}.{r['date']}" for r in rows[:100]
    )
    return hashlib.sha1(key.encode()).hexdigest()[:12]
