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

```bash
python -m noisepy_dvv_cloud.submit_helper correlate \
  --station_file station_lists/example_ci.txt \
  --start 2023.001 --end 2023.011 \
  --station_group_size 1 --day_group_size 10
```

This submits a handful of one-station jobs. Watch one to completion (**Console →
Batch → Jobs**), then check the products:

```bash
aws s3 ls --recursive s3://YOUR_BUCKET/ccf/v1/ | head
```

Then the dv/v stage on the same stations, and read the Parquet back with the
DuckDB query from [docs/parquet-schemas.md](../parquet-schemas.md).

**Gate: do not submit a full campaign until the smoke-test dv/v series looks sane and
you've written the observed per-station-day cost into 01_aws_setup.md.**

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
