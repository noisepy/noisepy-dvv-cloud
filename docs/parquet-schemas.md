# Parquet schemas and S3 layout

These schemas are the public contract of the pipeline. The 2022 Julia run stored
correlations as Julia-only `serialize` blobs (one giant object per pair, no
partitioning, no manifest) — the layout below is designed so DuckDB, Athena, pandas,
and Julia (Parquet2.jl) can all query the products directly.

Code twin: `src/noisepy_dvv_cloud/parquet_io.py`. Change them together.

## CCF dataset

```
s3://<bucket>/ccf/v1/network=CI/station=LJR/pair=EN/year=2023/shard-*.parquet
```

One row per station-day-component-pair (daily linear stack of all correlation windows).

| column | type | notes |
|---|---|---|
| `network` | string | partition key |
| `station` | string | partition key |
| `location` | string | `""` when empty (not `--`) |
| `pair` | string | partition key; `EN, EZ, NZ` (+ `EE, NN, ZZ` autocorrs) |
| `date` | date32 | UTC day |
| `ccf` | list\<float32\> | two-sided, length `2*maxlag_s*fs + 1`, lag axis symmetric about 0 |
| `fs` | float32 | Hz (40.0 for the standard recipe) |
| `maxlag_s` | float32 | seconds (32.0) |
| `nwindows` | int32 | 30-min windows stacked into this day (QC: low = gappy day) |
| `config_hash` | string | sha1[:12] of the effective NoisePy config (provenance) |

Files are content-hash-named so Spot-retried shards overwrite themselves —
re-submitting an identical campaign command is an idempotent sweep, not a duplication.

## dv/v dataset

```
s3://<bucket>/dvv/v1/band=2.0-4.0/CI.LJR.parquet
```

One file per station per octave band, one row per day. Column names keep the
Clements-Denolle-2022 Arrow convention (`DATE, DVV, CC`) so old and new products diff
directly; `DVV_ERR` is new (codameter Weaver/Clarke-style 1-sigma).

| column | type | notes |
|---|---|---|
| `DATE` | date32 | |
| `DVV` | float64 | percent; dv/v < 0 dilates the coda (codameter sign convention) |
| `DVV_ERR` | float64 | percent, 1-sigma |
| `CC` | float64 | Hobiger-combined: `sum(CC^3)/sum(CC^2)` across EN, EZ, NZ |
| `network`, `station`, `band` | string | denormalized for cross-file queries |
| `config_hash` | string | sha1[:12] of the codameter processing config |

## Example queries

```python
import duckdb
duckdb.sql("""
  SELECT station, avg(DVV) FROM 's3://BUCKET/dvv/v1/band=2.0-4.0/*.parquet'
  WHERE DATE BETWEEN '2019-07-01' AND '2019-08-01' GROUP BY station
""")
```

```python
from noisepy_dvv_cloud.parquet_io import read_ccf_matrix
ccfs, days, t, fs = read_ccf_matrix("s3://BUCKET/ccf/v1", "CI", "LJR", "EN")
```
