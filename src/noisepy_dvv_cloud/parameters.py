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

# Output bucket (this account's, not a public archive)
OUTPUT_BUCKET = ""  # e.g. "noisepy-dvv-cloud-products"
CCF_PREFIX = "ccf/v1"
DVV_PREFIX = "dvv/v1"

# IAM role ARNs pasted into configs/job_definition_*.yaml.
#
# Keep these empty in git. Real ARNs come from the environment on the
# controller machine, so adding a role later never means editing a tracked
# file:
#
#     export DVV_JOB_ROLE_ARN=arn:aws:iam::<account>:role/<role>
#     export DVV_EXECUTION_ROLE_ARN=$DVV_JOB_ROLE_ARN
#
# The account currently has one role that serves as both (see
# docs/runbook/03_batch_setup.md).
JOB_ROLE_ARN = os.environ.get("DVV_JOB_ROLE_ARN", "")
EXECUTION_ROLE_ARN = os.environ.get("DVV_EXECUTION_ROLE_ARN", "")
