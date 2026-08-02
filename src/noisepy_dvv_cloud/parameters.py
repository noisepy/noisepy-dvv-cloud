"""Deployment names and account-specific values.

Fill these in per campaign (QuakeScope convention: keep a filled copy on the
controller machine only; commit only empty strings).
"""

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

# IAM role ARNs pasted into configs/job_definition_*.yaml
JOB_ROLE_ARN = ""
EXECUTION_ROLE_ARN = ""
