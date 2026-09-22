# 00 — Unblocking Gates 1 and 2

Written 2026-08-17, against a live inspection of account ACCOUNT_ID. Supersedes the
env instructions in the other runbook pages where they disagree.

## What was actually blocking

| Blocker | Status |
|---|---|
| No environment on the laptop had `codameter`; `noisepy`/`noisepy1` broken | **Fixed** — two pixi environments, below |
| `codameter` floating `>=0.3.0` while the sign convention is version-dependent | **Fixed** — pinned `==0.4.0` |
| CD2022 archive "missing" | **Found** — the runbook path had a stale `Clements-Denolle-2022/` level |
| `CI.PASC` has no 2022 counterpart | **Fixed** — station list now uses `CI.ADO` |
| Gate 1 attempted on 10 days of correlations | **Diagnosed** — needs ~2 years, so it is a Batch run |
| IAM roles for Batch | **Already exist** — reuse `NoisePyBatchRole` |
| Bucket policy for collaborators | **Not needed** — the `scoped` group already grants it |
| Batch compute env / queue / job defs | **Still to do** — nothing named `dvvcloud2026_*` exists |
| seisfetch migration | **Deferred** — see the last section |

## Phase 0 — Environments (done; re-run to reproduce)

```bash
cd ~/GitHub/noisepy-dvv-cloud
pixi install -e correlate
pixi install -e dvv
pixi run verify-correlate    # correlate ok | pandas 1.5.3
pixi run verify-dvv          # dvv ok | codameter 0.4.0 | pandas 2.3.3
```

Both stages are defined as pixi environments over the `correlate` and `dvv` extras in
`pyproject.toml`; `pixi.lock` is committed, so these resolve identically elsewhere.
Prefix every command below with `pixi run -e correlate` or `pixi run -e dvv`.

`awscli` sits in a third environment (`ops`) and must stay there — conda-forge's
`awscli` needs `ruamel-yaml 0.19`, `noisepy-seis` pins `pydantic-yaml==1.0` which caps
it below 0.18, and the correlate solve fails outright if they share an environment. If
you already have `aws` on `PATH`, ignore `ops` entirely.

## Phase 1 — AWS objects

Four steps, in this order. The three scripts each take `--check` (read-only)
as well as `--apply`, and each is idempotent, so a re-run after changing a
constant is the supported way to change the account rather than a console
click.

**1. The products bucket, with its protections on from the first object.**
Versioning only covers objects written *after* it is enabled, so this cannot be
retrofitted once there are products worth protecting. Pick the name once — S3
names are global and every product path carries it forever.

```bash
export DVV_OUTPUT_BUCKET=denolle-dvv-cloud-2026
pixi run -e ops python scripts/create_bucket.py --check    # nothing yet
pixi run -e ops python scripts/create_bucket.py --apply
```

Sets versioning, all four public-access blocks, SSE-S3 default encryption, and
a lifecycle rule that expires noncurrent versions at 30 days, aborts incomplete
multipart uploads at 7, and clears orphaned delete markers. Current versions
never expire — they are the deliverable. What and why:
[`src/noisepy_dvv_cloud/bucket.py`](../../src/noisepy_dvv_cloud/bucket.py).

**2. The two IAM roles.** Not `NoisePyBatchRole`: that role is the live
`jobRoleArn` on both revisions of `niyiyu-noisepy-scedc-2022`, so narrowing it
would change what somebody else's jobs can do without telling them.

```bash
pixi run -e ops python scripts/scope_iam.py --check    # roles do not exist yet
pixi run -e ops python scripts/scope_iam.py --apply
```

Creates `DvvCloudBatchRole` (the job role: one bucket, read and write, **no
delete**) and `DvvCloudExecutionRole` (the platform role: pull the image, open
the log stream, no S3 at all), then simulates both against a negative control
and prints the two `export` lines for `parameters.py`:

```bash
export DVV_JOB_ROLE_ARN=arn:aws:iam::ACCOUNT_ID:role/DvvCloudBatchRole
export DVV_EXECUTION_ROLE_ARN=arn:aws:iam::ACCOUNT_ID:role/DvvCloudExecutionRole
```

**3. Batch objects.** Networking first — the compute environment needs subnets
and a security group:

```bash
aws ec2 describe-subnets --query 'Subnets[].SubnetId' --output text
aws ec2 describe-security-groups --filters Name=group-name,Values=default \
  --query 'SecurityGroups[].GroupId' --output text
```

Those subnets are in the **default VPC**, whose only route table sends
`0.0.0.0/0` to an internet gateway, so they are public — which is what
`assignPublicIp: ENABLED` in both job definitions expects. There is no NAT
gateway on this account, so the usual argument for S3 VPC endpoints (NAT
data-processing charges at fan-out scale) does not apply here. An S3 gateway
endpoint is still free and worth adding later; it is not a blocker.

Copy the templates before filling them — the tracked YAMLs stay templates, and
`.gitignore` covers the `*.local.yaml` suffix:

```bash
for f in compute_environment job_queue job_definition_correlate job_definition_dvv; do
  cp configs/$f.yaml configs/$f.local.yaml
done
# fill subnets, security group, and the two role ARNs, then:
aws batch create-compute-environment --no-cli-pager --cli-input-yaml file://configs/compute_environment.local.yaml
aws batch create-job-queue          --no-cli-pager --cli-input-yaml file://configs/job_queue.local.yaml
aws batch register-job-definition   --no-cli-pager --cli-input-yaml file://configs/job_definition_correlate.local.yaml
aws batch register-job-definition   --no-cli-pager --cli-input-yaml file://configs/job_definition_dvv.local.yaml
```

**4. Check the whole thing before submitting anything.**

```bash
pixi run -e ops python scripts/preflight.py
```

Exit 0 only if nothing FAILs. It reads roles, bucket, Batch objects and image
back from AWS rather than from `configs/`, so it catches a job definition that
points at the wrong role — the failure mode that otherwise shows up as
`ResourceInitializationError` at task start.

Images are x86-only (obspy ships no linux/aarch64 wheels) and are built by the
`docker` GitHub Action on every push to `main`. To build by hand:

```bash
docker buildx build --platform linux/amd64 -f docker/Dockerfile.correlate \
  -t ghcr.io/noisepy/noisepy-dvv-cloud:correlate-latest --push .
docker buildx build --platform linux/amd64 -f docker/Dockerfile.dvv \
  -t ghcr.io/noisepy/noisepy-dvv-cloud:dvv-latest --push .
```

## Phase 2 — Micro-smoke on Batch (1 station, 10 days)

Proves container + IAM + S3 before spending two years of compute.

```bash
pixi run -e correlate python -m noisepy_dvv_cloud.submit_helper correlate \
  --station_file station_lists/example_ci.txt \
  --start 2023.001 --end 2023.011 \
  --station_group_size 1 --day_group_size 10

aws s3 ls --recursive s3://denolle-dvv-cloud-2026/ccf/v1/ | head
```

Cross-check against the local `smoke_out/ccf` — same station, same days, so the row
counts and a ZZ waveform should match.

## Phase 3 — Gate 1

```bash
pixi run -e correlate python -m noisepy_dvv_cloud.submit_helper correlate \
  --station_file station_lists/example_ci.txt \
  --start 2018.001 --end 2020.001 \
  --station_group_size 1 --day_group_size 30

# wait for the queue to drain
pixi run -e correlate python -m noisepy_dvv_cloud.submit_helper dvv \
  --station_file station_lists/example_ci.txt \
  --start 2018.001 --end 2020.001 --combine hobiger
```

No `--use-case` here on purpose: the no-use-case fallback in `dvv.py` is the
Clements-Denolle-like config (stretching, fixed reference, 90-day trailing
stack), which is what Gate 1 is comparing against. Passing a use case would
swap in a window calibrated for a different target process and make the
comparison less like-for-like.

Then compare, per station:

```bash
LEGACY=~/Dropbox/RESEARCH_GROUP/TIM_MARINE_PROJEcTS/data/DVV-90-DAY-COMP/2.0-4.0
for S in LJR RXH ADO; do
  echo "=== CI.$S ==="
  pixi run -e dvv python scripts/compare_cd2022.py \
    --new s3://denolle-dvv-cloud-2026/dvv/v1/band=2.0-4.0/CI.$S.parquet \
    --legacy $LEGACY/CI.$S.arrow
done
```

Pass: correlation > 0.9, |mean offset| < 0.05 %. Repeat the dv/v stage with
`--combine inverse_variance` and record which one the campaign uses — it is part of the
config hash.

## Phase 4 — Gate 2 (cost)

```bash
aws ce get-cost-and-usage \
  --time-period Start=<smoke-start>,End=<smoke-end> \
  --granularity DAILY --metrics UnblendedCost \
  --filter '{"Dimensions":{"Key":"SERVICE","Values":["Amazon Elastic Container Service"]}}'
```

Divide by station-days processed; write the number into
[01_aws_setup.md](01_aws_setup.md).

## Phase 5 — Campaign

Only once both gates pass. See [04_submitting_jobs.md](04_submitting_jobs.md).

## Phase 6 — seisfetch, deferred

`seisfetch` 0.4.1 is published and its NoisePy adapter reports bit-identical
preprocessing (max abs diff `0.0` through `compute_fft` and `correlate`), plus a
pure-numpy response removal that would let us re-enable the response correction
disabled in e8cf629 for cost.

It is still **off the gate path**, for a reason the adapter's own docstring states:
NoisePy imports obspy at module level, so a seisfetch-fed correlate container still
ships obspy — the image-size and Graviton wins land only after the upstream NoisePy PR
demotes obspy to an extra. `SeisfetchS3RawStore` is also self-described "evaluation
grade": no catalog support, placeholder `0.0` coordinates, and `get_timespans` /
`get_channels` left as hooks for the harness to fill.

Track the upstream PR. Do not block the campaign on it.
