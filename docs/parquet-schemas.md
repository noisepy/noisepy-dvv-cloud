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
| `ccf` | list\<float32\> | two-sided, length `2*maxlag_s*fs + 1` **as NoisePy returns it**, whose true zero lag sits at index `n // 2 + 1`, not the midpoint. Read it with `parquet_io.read_ccf_matrix`, which trims to a genuinely symmetric window centred on zero lag (`2*maxlag_s*fs - 1` samples) and returns the matching axis. |
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

One file per station per octave band, one row per day. All column names are lowercase
snake_case. The legacy Clements-Denolle-2022 Arrow files used `DATE, DVV, CC`
(uppercase); when diffing old vs new, lowercase the legacy columns
(`scripts/compare_cd2022.py` does this).

The dv/v value is the mean of a 5-member processing ensemble (baseline config plus
stack halved/doubled, coda window shifted, reference scheme swapped — see
`dvv.ensemble_configs`), combined across EN/EZ/NZ per member.

| column | type | notes |
|---|---|---|
| `date` | date32 | |
| `dvv` | float64 | percent; dv/v < 0 dilates the coda (codameter sign convention); ensemble mean |
| `dvv_err` | float64 | percent, 1-sigma total = sqrt(within² + method²) |
| `dvv_err_within` | float64 | percent; Weaver/Clarke (2011) coherence floor |
| `dvv_err_method` | float64 | percent; spread across the processing ensemble |
| `cc` | float64 | baseline member, combined across pairs |
| `n_members` | int32 | ensemble members with a finite dv/v at this epoch |
| `network`, `station`, `band` | string | denormalized for cross-file queries |
| `config_hash` | string | sha1[:12] of baseline config + combiner choice |

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
