# 04 — Submitting jobs

## How work is split

- **correlate**: shards of (station block × day block), default 4 stations × 30 days
  per job. Single-station processing is embarrassingly parallel — no pair bookkeeping.
  Stations are grouped by archive first, so a shard never mixes SCEDC and NCEDC.
- **dvv**: one job per station block over the *whole* campaign period (the moving
  stack and reference need the full history in one process). Submit only after the
  correlate campaign is done.

Station lists: one `NET.STA.LOC` per line, `#` comments (see
[station_lists/example_ci.txt](../../station_lists/example_ci.txt)). Trailing dot when
the location code is empty: `CI.LJR.`.

## Smoke test (do not skip)

**Passed 2026-09-22 on CI.LJR** — both stages, exit 0, products verified. See
[00b_cloud_hardening.md](00b_cloud_hardening.md) §6, which also records the one
defect it exposed (a single all-NaN ensemble member NaNs the whole dv/v mean).

```bash
export DVV_OUTPUT_BUCKET=denolle-dvv-cloud-2026
pixi run -e ops python -m noisepy_dvv_cloud.submit_helper correlate \
  --station_file station_lists/smoke_ljr.txt \
  --start 2023.001 --end 2023.011 \
  --station_group_size 1 --day_group_size 10
```

One station, ten days, one shard. CI.LJR because it is the Clements-Denolle
2022 reference station, so the same correlations feed Gate 1 later without
recomputing anything. Use `station_lists/example_ci.txt` for the three-station
version. Watch one to completion (**Console →
Batch → Jobs**), then check the products:

```bash
pixi run -e ops aws s3 ls --recursive s3://$DVV_OUTPUT_BUCKET/ccf/v1/ | head
```

Expect six Parquet shards, one per `acorr_only` component pair
(`EE EN EZ NN NZ ZZ`). The system `aws` on the controller is CLI 2.0.34 and
rejects `--no-cli-pager`; run AWS commands through `pixi run -e ops aws`.

Then the dv/v stage on the same stations, and read the Parquet back with the
DuckDB query from [docs/parquet-schemas.md](../parquet-schemas.md).

**Gate 1 — legacy cross-check.** For at least 3 stations that were in the 2022
California run, compare the new dv/v against the archived Arrow products (readable
directly from Python — no Julia needed):

```bash
pixi run -e dvv python scripts/compare_cd2022.py \
  --new s3://YOUR_BUCKET/dvv/v1/band=2.0-4.0/CI.LJR.parquet \
  --legacy ~/Dropbox/RESEARCH_GROUP/TIM_MARINE_PROJEcTS/data/DVV-90-DAY-COMP/2.0-4.0/CI.LJR.arrow
```

Pass: **correlation > 0.9**. The mean offset is printed but not gated — the two
products use different reference epochs, so a constant offset is bookkeeping, not
error. Differences are expected (NoisePy vs SeisNoise conventions, ensemble mean vs
single config) but must be small and explainable.

Two constraints on the comparison run:

- **Band.** Only `2.0-4.0` is archived, so Gate 1 is a single-band check.
- **Span.** `compare_cd2022.py` drops a 150-day reference burn-in and compares a 90-day
  trailing stack, so ten days of correlations produce nothing to compare. Give it
  **two years**. That is a Batch run, not a laptop run — do the AWS setup first.

**Gate 2 — cost.** Write the observed per-station-day cost into 01_aws_setup.md.
The `aws ce` route does not work here: Cost Explorer and Budgets are both denied
by SCP `p-q1ngvul9`. Real figures come from the CloudBank portal, recorded by
hand — see [00b_cloud_hardening.md](00b_cloud_hardening.md) §1.2.

Also run the dvv smoke test twice, `--combine hobiger` and `--combine
inverse_variance`, and record which you're using for the campaign — the choice is part
of the config hash.

**Do not submit a full campaign until both gates pass.**

## Real campaign

```bash
python -m noisepy_dvv_cloud.submit_helper correlate \
  --station_file station_lists/YOUR_LIST.txt --start 2016.001 --end 2026.001
# ... wait for the queue to drain ...
python -m noisepy_dvv_cloud.submit_helper dvv \
  --station_file station_lists/YOUR_LIST.txt --start 2016.001 --end 2026.001
```

Every submission writes an audit CSV under `submissions/`. Spot interruptions are
retried automatically; if jobs died for other reasons, re-run the identical command —
outputs are idempotent.

## Emergency stop

**Console → Batch → Job queues → dvvcloud2026_queue → Disable**, then cancel runnable
jobs:

```bash
aws batch list-jobs --job-queue dvvcloud2026_queue --job-status RUNNABLE \
  --query 'jobSummaryList[].jobId' --output text | \
  xargs -n1 -I{} aws batch cancel-job --job-id {} --reason "emergency stop"
```

Next: [05_monitoring.md](05_monitoring.md)

(Gate 0 passed 2026-08-08 — see 02_container.md)
