# 03 — Batch objects

| Object | What it is | Config file |
|---|---|---|
| Compute environment | the Fargate Spot pool | [configs/compute_environment.yaml](../../configs/compute_environment.yaml) |
| Job queue | where jobs wait for the pool | [configs/job_queue.yaml](../../configs/job_queue.yaml) |
| Correlate job def | container + argv template, 2 vCPU / 16 GB | [configs/job_definition_correlate.yaml](../../configs/job_definition_correlate.yaml) |
| dv/v job def | container + argv template, 2 vCPU / 8 GB | [configs/job_definition_dvv.yaml](../../configs/job_definition_dvv.yaml) |

## Output bucket

```bash
export DVV_OUTPUT_BUCKET=denolle-dvv-cloud-2026
pixi run -e ops python scripts/create_bucket.py --apply
```

Not `aws s3 mb`. The bucket needs versioning, public-access blocks, default
encryption and a lifecycle rule, and **versioning only protects objects written
after it is enabled** — so it has to be on before the first correlate job, not
after there are products worth protecting. The script sets all of it and is
idempotent; `--check` reports without changing anything.

`parameters.py` reads `OUTPUT_BUCKET` from `$DVV_OUTPUT_BUCKET`, so the name
lives on the controller machine and never in a tracked file.

What is set, and the reasoning including why there is no Infrequent Access
transition, is in [`src/noisepy_dvv_cloud/bucket.py`](../../src/noisepy_dvv_cloud/bucket.py).

## IAM roles

```bash
pixi run -e ops python scripts/scope_iam.py --apply
```

Two roles, because Fargate uses them for different things:

| role | used by | grants |
|---|---|---|
| `DvvCloudBatchRole` | the container, as `jobRoleArn` | one bucket: list, get, put, multipart. **No delete of any kind.** |
| `DvvCloudExecutionRole` | the ECS agent, as `executionRoleArn` | `AmazonECSTaskExecutionRolePolicy` — image pull and log stream. No S3. |

The script simulates both afterwards against a negative control (`scoped-noise`)
and prints the two `export` lines for `DVV_JOB_ROLE_ARN` and
`DVV_EXECUTION_ROLE_ARN`.

**Why not reuse `NoisePyBatchRole`,** which the earlier version of this page
recommended: it is the live `jobRoleArn` on both revisions of
`niyiyu-noisepy-scedc-2022`, so scoping it down would change what another
person's jobs can do, silently. Simulated on 2026-09-22, it also allows
`s3:DeleteObject` and `s3:DeleteObjectVersion` on our products and full access
to every other bucket in the account — six denials this campaign needs and that
role cannot provide. Roles are free; new ones leave the legacy jobs alone.

Reads from `scedc-pds`/`ncedc-pds` are anonymous (`correlate.py:68` sets
`{"anon": True}`), so no archive needs a grant.

**No delete permission** because nothing in `src/` deletes — verified by grep
for `.rm(`, `delete_object`, `DeleteObject`, `.remove(`, zero hits — and shard
files are content-hash-named, so a re-run overwrites rather than cleaning up.
`scripts/scope_iam.py` asserts the denial rather than assuming it, and
`tests/test_cloud_spec.py` fails if a later edit adds a `Delete*` action.

## Bucket policy

**Optional, and the third line of defence rather than the first.** Every IAM
user in the account is in the `scoped` group, which carries
`AmazonS3FullAccess`; policies are additive for Allow, so an Allow statement
here grants nothing. What the policy buys is a *delete guard*, because an
explicit `Deny` outranks any Allow. Versioning (line one) and the lifecycle
rule (line two) do the real work.

See [configs/bucket_policy.json](../../configs/bucket_policy.json) — apply it if
you want campaign products protected from the other 29 group members. A bucket
policy becomes mandatory only for principals outside this account.

## Create the objects (in order)

**The files in `configs/` are templates and stay that way.** Filling the `''  #
[REQUIRED]` placeholders (subnets, security group, role ARNs) in the tracked copies is
the one easy way to commit account detail to this repo. Copy to `*.local.yaml` instead
— `.gitignore` covers that suffix:

```bash
for f in compute_environment job_queue job_definition_correlate job_definition_dvv; do
  cp configs/$f.yaml configs/$f.local.yaml
done
# edit the .local.yaml copies, then:
aws batch create-compute-environment --no-cli-pager --cli-input-yaml file://configs/compute_environment.local.yaml
aws batch create-job-queue          --no-cli-pager --cli-input-yaml file://configs/job_queue.local.yaml
aws batch register-job-definition   --no-cli-pager --cli-input-yaml file://configs/job_definition_correlate.local.yaml
aws batch register-job-definition   --no-cli-pager --cli-input-yaml file://configs/job_definition_dvv.local.yaml
```

Same rule for `parameters.py`: `JOB_ROLE_ARN` and `EXECUTION_ROLE_ARN` read from
`DVV_JOB_ROLE_ARN` / `DVV_EXECUTION_ROLE_ARN` in the environment, so adding a role
later never means editing a tracked file.

Record the names you used in `parameters.py`. Convention: `dvvcloud<year>_env`,
`_queue`, `_correlate`, `_dvv`.

## Check it before submitting anything

```bash
pixi run -e ops python scripts/preflight.py
```

Exit 0 only if nothing FAILs. It reads the roles, the bucket, the Batch objects
and the image back from AWS rather than from `configs/`, so it catches a job
definition registered against the wrong role — which otherwise surfaces as
`ResourceInitializationError` at task start and reads like a Secrets Manager
problem. A check that cannot reach AWS reports `UNKNOWN`, never `PASS`.

Simulation is still a model of the IAM evaluator. The evidence that the model
matched reality is one real job that starts, reads, writes an object and exits
— which is Phase 2, the micro-smoke.

Next: [04_submitting_jobs.md](04_submitting_jobs.md)
