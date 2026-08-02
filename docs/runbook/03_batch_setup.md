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

Two roles (may be the same role, QuakeScope did this):

- **Execution role** — trusted entity *Elastic Container Service Task*, managed policy
  `AmazonECSTaskExecutionRolePolicy`. Lets the ECS agent pull the image and write logs.
- **Job role** — same trust, plus an inline policy granting `s3:GetObject`,
  `s3:PutObject`, `s3:ListBucket` on `YOUR_BUCKET`. (Reads from `scedc-pds`/`ncedc-pds`
  are anonymous — no policy needed.)

**Console → IAM → Roles → Create role.** Paste both ARNs into the two job definition
YAMLs and into `parameters.py`.

## Create the objects (in order)

Fill the `''  # [REQUIRED]` placeholders (subnets, security group, role ARNs), then:

```bash
aws batch create-compute-environment --no-cli-pager --cli-input-yaml file://configs/compute_environment.yaml
aws batch create-job-queue          --no-cli-pager --cli-input-yaml file://configs/job_queue.yaml
aws batch register-job-definition   --no-cli-pager --cli-input-yaml file://configs/job_definition_correlate.yaml
aws batch register-job-definition   --no-cli-pager --cli-input-yaml file://configs/job_definition_dvv.yaml
```

Record the names you used in `parameters.py`. Convention: `dvvcloud<year>_env`,
`_queue`, `_correlate`, `_dvv`.

Next: [04_submitting_jobs.md](04_submitting_jobs.md)
