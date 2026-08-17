# 03 — Batch objects

| Object | What it is | Config file |
|---|---|---|
| Compute environment | the Fargate Spot pool | [configs/compute_environment.yaml](../../configs/compute_environment.yaml) |
| Job queue | where jobs wait for the pool | [configs/job_queue.yaml](../../configs/job_queue.yaml) |
| Correlate job def | container + argv template, 4 vCPU / 30 GB | [configs/job_definition_correlate.yaml](../../configs/job_definition_correlate.yaml) |
| dv/v job def | container + argv template, 2 vCPU / 8 GB | [configs/job_definition_dvv.yaml](../../configs/job_definition_dvv.yaml) |

## Output bucket

```bash
aws s3 mb s3://YOUR_BUCKET --region us-west-2
```

Put the name in `src/noisepy_dvv_cloud/parameters.py` (`OUTPUT_BUCKET`).

## IAM roles

**Already done — reuse `NoisePyBatchRole`.** Account ACCOUNT_ID has
`arn:aws:iam::ACCOUNT_ID:role/NoisePyBatchRole`, which already carries everything
both roles need:

- trust policy: `ecs-tasks.amazonaws.com`
- `AmazonECSTaskExecutionRolePolicy` — pull the image, write logs (execution role)
- `AmazonS3FullAccess` — read/write the output bucket (job role)

Paste that one ARN into **both** `jobRoleArn` and `executionRoleArn` in the two job
definition YAMLs, and into `parameters.py`. QuakeScope used a single role the same way.
Reads from `scedc-pds`/`ncedc-pds` are anonymous — no policy needed.

## Bucket policy

**Not required.** Every IAM user in the account belongs to the `scoped` group, which
carries `AmazonS3FullAccess` — niyiyu and the other group members can read and write
the new bucket as soon as it exists, and an Allow statement in a bucket policy would
grant nothing on top of that.

The one thing a bucket policy buys here is a *delete guard*: 30 users hold
`AmazonS3FullAccess`, and an explicit `Deny` is the only thing that outranks it. See
[configs/bucket_policy.json](../../configs/bucket_policy.json) — optional, apply it if
you want campaign products protected from accidental deletion.

A bucket policy becomes *mandatory* only for principals outside account
ACCOUNT_ID.

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

Next: [04_submitting_jobs.md](04_submitting_jobs.md)
