# 06 — Troubleshooting

## Jobs stuck in RUNNABLE

Almost always networking or capacity:
- Compute environment INVALID (**Console → Batch → Compute environments** — bad
  subnet/SG IDs are the usual cause).
- `maxvCpus` exhausted — jobs queue until slots free; this is normal, not stuck.
- Fargate Spot capacity crunch — wait, or add a second compute environment with
  `type: FARGATE` (on-demand) at `order: 2` in the queue.

## Job fails immediately (< 2 min)

Read the CloudWatch log first. Common causes:
- `CannotPullContainerError` — execution role missing, or the image tag doesn't exist
  (did the GH Action finish?).
- `AccessDenied` on S3 write — job role missing the bucket policy (runbook 03).
- Python `ImportError` — dependency drift in the image; check the pins in the
  Dockerfile (`noisepy-seis-io==0.3.5` is the critical one).

## Docker image build fails on pip install

Two known traps, both already encoded in `docker/`:
- `noisepy-seis` (pandas<2) and `codameter` (pandas>=2) can never share an
  environment — that's why there are two images. Don't try to merge them.
- In the correlate image, `boto3` must stay pinned to match noisepy's
  `aiobotocore==2.5.2`; unpinned, pip backtracks through years of noisepy releases
  and dies with `ResolutionImpossible`.

## Job OOM-killed (exit 137)

Correlate jobs: lower `--station_group_size` or `day_group_size` (less data per shard),
or raise MEMORY in the job def (Fargate max at 4 vCPU is 30 GB; 8 vCPU allows 60 GB).

## dv/v looks wrong

- All-NaN series: check `nwindows` in the CCF Parquet — gappy days produce weak CCFs
  that the CC gate (0.6) removes. Lower the gate only with eyes on the waveforms.
- Saturated at ±eps_max: the stretching grid is too narrow for the true dv/v; raise
  `eps_max` (codameter `use_cases.eps_max` per use case).
- Sign confusion: codameter convention is dv/v < 0 dilates the coda; the Parquet DVV
  column is in percent with that sign.

## Spot interruption storm

Retries are automatic (10 attempts). If a shard exhausts retries, just re-run the
identical submit command — completed shards overwrite themselves idempotently.

## Emergency stop

See [04_submitting_jobs.md](04_submitting_jobs.md#emergency-stop).
