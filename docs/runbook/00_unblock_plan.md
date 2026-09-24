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
| seisfetch migration | **Re-assessed 2026-09-24** — two of its three wins do not need it; see Phase 6 |

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

## Phase 1 — AWS objects (done 2026-09-22)

All four steps below have been run; `scripts/preflight.py` exits 0. The object
names and the networking actually used are recorded in
[00b_cloud_hardening.md](00b_cloud_hardening.md) §6. Keep the instructions —
they are how the account is reproduced, and every script is idempotent.

Note: this account's system `aws` is CLI 2.0.34 and rejects `--no-cli-pager`.
Run the `aws batch` commands through `pixi run -e ops aws ...`.

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

## Phase 6 — seisfetch, re-assessed 2026-09-24

The 2026-08 deferral said seisfetch would buy three things: a smaller image,
Graviton, and a pure-numpy response removal. Re-checked against what is
published now and against the running pipeline, **two of the three turned out
not to depend on seisfetch at all**, and the largest efficiency win was
somewhere else entirely.

### What moved

`seisfetch` is now **0.5.0** and has genuinely graduated. Its core dependencies
are `numpy + boto3 + pymseed` — obspy is an *extra*, not a requirement — and it
now publishes a `noisepy` extra. That is a real change from the 0.4.1
"evaluation grade" store the deferral was written against.

### What did not move

NoisePy still imports obspy **at module level** in the files we import —
`noise_module.py`, `correlate.py`, `io/s3store.py`, `io/datatypes.py`,
`io/stores.py`, `io/channelcatalog.py`, 20+ in total. Verified 2026-09-24:
`import noisepy.seis` pulls obspy 1.5.0. So a seisfetch-fed container still
ships obspy, exactly as the original deferral said.

One detail worth knowing: **obspy is an undeclared dependency**. Neither
`noisepy-seis` nor `noisepy-seis-io` lists it; it arrives only because
`pyasdf` requires it, and pyasdf exists for the ASDF store this project never
uses. The hot path's own obspy usage is shallow — `bandpass`, `_npts2nfft` and
a taper lookup in `noise_module` — all scipy-backed. The deep usage is in the
`io/` layer (`Stream`, `Trace`, `UTCDateTime`, `read_inventory`), which is
precisely what a seisfetch raw store would replace.

### Graviton is not blocked by obspy

obspy ships no linux/aarch64 wheels **on PyPI** (checked through 1.5.1), which
is what the container note recorded. But **conda-forge ships obspy for
linux-aarch64** (1.4.2–1.5.1). The blocker is that `Dockerfile.correlate` is
pip-on-`python:3.10-slim`; an image built from the pixi lock would get arm64
today. That is a container change, not a migration — see
[02_container.md](02_container.md).

### The read path is network-bound, not obspy-bound

Profiled one channel-day: of 5.46 s, **5.14 s is `_thread.lock.acquire` under
`fsspec._fetch`** — waiting on S3. obspy's own mseed decode is 0.28 s. Swapping
the mseed reader therefore cannot be the efficiency story; the bytes are.

### The efficiency win that was actually there

`get_channels` returned **six** channels for CI.LJR (BH? and HH?) and NoisePy
read and decoded all six, then preprocessed three: its band dedup happens
*after* `read_data`. HH at 100 sps is ~2.75x the bytes of BH at 40 sps, so
**73% of the bytes downloaded were discarded**.

`correlate.PreferredBandStore` now drops HH where the same station offers BH,
before anything reads it. Measured on CI.LJR 2023-01-01 through the production
path: **7.2 s -> 3.6 s**, six channels to three, and the output is
**bit-identical** — max |diff| 0.0 on all six pairs, because BH is what NoisePy
kept either way. Per station rather than `channels=["BH?"]`, because 9,837 of
the 22,191 inventory stations are HH-only and would otherwise be silently
dropped.

### Where that leaves seisfetch

Still worth doing, but it is now the **third** priority, not the first, and its
case rests on the response removal rather than on speed or image size:

1. **Done** — band preference. Free, bit-identical, no new dependency.
2. **Next, if wanted** — pixi-built image for arm64. Needs a rebuild and a
   Graviton smoke test; no upstream dependency.
3. **Then** — seisfetch, for the pure-numpy response removal that would let us
   re-enable the correction disabled in e8cf629 for cost. Removing obspy
   entirely still needs the upstream NoisePy PR demoting it to an extra, and
   `SeisfetchS3RawStore` still has to provide the catalog and coordinates that
   `XMLStationChannelCatalog` provides today.

Track the upstream PR. Do not block the campaign on it.
