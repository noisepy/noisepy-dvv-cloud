"""Deployment names and account-specific values.

Fill these in per campaign (QuakeScope convention: keep a filled copy on the
controller machine only; commit only empty strings).
"""

import os

# us-west-2 co-locates compute with scedc-pds/ncedc-pds — the correlate
# stage is read-heavy, so same-region S3 reads cut both wall time and risk
# of cross-region transfer cost
AWS_REGION = "us-west-2"

# AWS Batch object names, created in docs/runbook/03_batch_setup.md
COMPUTE_ENVIRONMENT = "dvvcloud2026_env"
JOB_QUEUE = "dvvcloud2026_queue"
JOB_DEFINITION_CORRELATE = "dvvcloud2026_correlate"
JOB_DEFINITION_DVV = "dvvcloud2026_dvv"

# Output bucket (this account's, not a public archive). Created with its
# protections already on by scripts/create_bucket.py -- versioning only covers
# objects written after it is enabled, so it has to exist before job one.
#
#     export DVV_OUTPUT_BUCKET=noisepy-dvv-cloud-products
#
# Read from the environment for the same reason as the role ARNs below: a
# campaign is configured on the controller machine, never by editing a tracked
# file.
OUTPUT_BUCKET = os.environ.get("DVV_OUTPUT_BUCKET", "")
CCF_PREFIX = "ccf/v1"
DVV_PREFIX = "dvv/v1"

# IAM role ARNs pasted into configs/job_definition_*.yaml.
#
# Keep these empty in git. Real ARNs come from the environment on the
# controller machine, so adding a role later never means editing a tracked
# file:
#
#     export DVV_JOB_ROLE_ARN=arn:aws:iam::<account>:role/DvvCloudBatchRole
#     export DVV_EXECUTION_ROLE_ARN=arn:aws:iam::<account>:role/DvvCloudExecutionRole
#
# The two are DIFFERENT roles on purpose. On Fargate the execution role is
# what the ECS agent uses to pull the image and open the log stream, before
# any of our code exists; the job role is what the container runs as. Merging
# them gives the platform role our S3 access and the container the platform's.
# scripts/scope_iam.py creates both and prints these two export lines.
JOB_ROLE_ARN = os.environ.get("DVV_JOB_ROLE_ARN", "")
EXECUTION_ROLE_ARN = os.environ.get("DVV_EXECUTION_ROLE_ARN", "")
