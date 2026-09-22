"""What protects the products bucket, written once so it can be asserted twice.

`scripts/create_bucket.py` sets these properties; `scripts/preflight.py` calls
`audit()` to check them before a campaign. Keeping the spec in one module means
the check cannot drift away from the thing it is checking.

Why versioning is the first line of defence and a bucket policy is the third:
30 IAM users on this account are in the `scoped` group, which carries
`AmazonS3FullAccess`. Policies are additive for Allow, so nothing we write
takes that away. Versioning is what makes a wrong delete recoverable at all --
without it, a deleted campaign product is gone, and a campaign product is
thousands of dollars of Fargate time.

The lifecycle rule exists to keep that protection from turning into a bill:
noncurrent versions and abandoned multipart parts both accrue storage charges
indefinitely and neither is ever read.

Deliberately NOT here: any transition to Infrequent Access or Intelligent
Tiering. Both have per-object floors (128 KB minimum billable size and a 30-day
minimum duration for IA; a per-object monitoring charge for Intelligent
Tiering) and this campaign writes many small Parquet shards, so the transition
could cost more than it saves. Decide it against real object-size figures --
docs/runbook/00b_cloud_hardening.md §4 item 4 -- not in advance.
"""

from __future__ import annotations

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"

LIFECYCLE_ID = "dvvcloud-version-hygiene"
NONCURRENT_DAYS = 30
ABORT_MPU_DAYS = 7


def lifecycle_configuration() -> dict:
    """One rule, three jobs: expire superseded versions, drop abandoned
    multipart parts, and clear delete markers left with nothing under them."""
    return {
        "Rules": [
            {
                "ID": LIFECYCLE_ID,
                "Status": "Enabled",
                "Filter": {"Prefix": ""},
                # current versions are the deliverable and never expire
                "NoncurrentVersionExpiration": {"NoncurrentDays": NONCURRENT_DAYS},
                "AbortIncompleteMultipartUpload": {
                    "DaysAfterInitiation": ABORT_MPU_DAYS
                },
                "Expiration": {"ExpiredObjectDeleteMarker": True},
            }
        ]
    }


def _code(exc) -> str:
    return exc.response.get("Error", {}).get("Code", "")


def audit(s3, bucket: str, region: str) -> list[tuple[str, str, str]]:
    """(check, state, detail) rows. State is PASS, FAIL or WARN.

    Every row is read back from AWS rather than inferred from what we would
    have set: the bucket may predate this script, or somebody may have changed
    it in the console.
    """
    from botocore.exceptions import ClientError

    rows: list[tuple[str, str, str]] = []

    try:
        s3.head_bucket(Bucket=bucket)
    except ClientError as exc:
        code = _code(exc)
        detail = {
            "404": "does not exist -- run scripts/create_bucket.py --apply",
            "403": "exists but belongs to another account, or is blocked",
        }.get(code, code)
        return [(f"bucket {bucket}", FAIL, detail)]
    rows.append((f"bucket {bucket}", PASS, "exists"))

    loc = s3.get_bucket_location(Bucket=bucket).get("LocationConstraint") or "us-east-1"
    rows.append((
        "region",
        PASS if loc == region else FAIL,
        f"{loc}"
        + ("" if loc == region
           else f" -- compute runs in {region}; cross-region reads of every "
                "CCF shard cost both transfer and latency"),
    ))

    status = s3.get_bucket_versioning(Bucket=bucket).get("Status")
    rows.append((
        "versioning",
        PASS if status == "Enabled" else FAIL,
        f"{status or 'not enabled'} -- a wrong delete "
        + ("is recoverable" if status == "Enabled" else "is PERMANENT"),
    ))

    try:
        blk = s3.get_public_access_block(Bucket=bucket)["PublicAccessBlockConfiguration"]
        off = [k for k, v in blk.items() if not v]
        rows.append((
            "public access blocked",
            PASS if not off else FAIL,
            "all four settings on" if not off else f"not set: {', '.join(off)}",
        ))
    except ClientError as exc:
        rows.append(("public access blocked", FAIL,
                     "no block configuration" if _code(exc).startswith("NoSuch")
                     else _code(exc)))

    try:
        enc = s3.get_bucket_encryption(Bucket=bucket)
        alg = (enc["ServerSideEncryptionConfiguration"]["Rules"][0]
               ["ApplyServerSideEncryptionByDefault"]["SSEAlgorithm"])
        rows.append(("default encryption", PASS, alg))
    except ClientError:
        # S3 has applied SSE-S3 to new objects by default since January 2023,
        # so an absent configuration is untidy rather than unencrypted.
        rows.append(("default encryption", WARN,
                     "none set explicitly; S3 still applies SSE-S3 by default"))

    try:
        rules = s3.get_bucket_lifecycle_configuration(Bucket=bucket)["Rules"]
    except ClientError:
        rules = []
    rule = next((r for r in rules if r.get("ID") == LIFECYCLE_ID), None)
    if rule is None:
        rows.append((
            "lifecycle", FAIL,
            f"no rule {LIFECYCLE_ID} -- noncurrent versions and abandoned "
            "multipart parts would accrue storage charges forever",
        ))
    else:
        missing = []
        if rule.get("Status") != "Enabled":
            missing.append("rule disabled")
        if (rule.get("NoncurrentVersionExpiration", {}).get("NoncurrentDays")
                != NONCURRENT_DAYS):
            missing.append(f"noncurrent != {NONCURRENT_DAYS}d")
        if (rule.get("AbortIncompleteMultipartUpload", {})
                .get("DaysAfterInitiation") != ABORT_MPU_DAYS):
            missing.append(f"abort MPU != {ABORT_MPU_DAYS}d")
        if not rule.get("Expiration", {}).get("ExpiredObjectDeleteMarker"):
            missing.append("delete markers not cleared")
        rows.append((
            "lifecycle", PASS if not missing else FAIL,
            f"{LIFECYCLE_ID}: noncurrent {NONCURRENT_DAYS}d, abort MPU "
            f"{ABORT_MPU_DAYS}d, delete markers cleared"
            if not missing else "; ".join(missing),
        ))

    return rows


def apply(s3, bucket: str, region: str) -> list[str]:
    """Create the bucket if absent, then set every property `audit` checks.

    Idempotent: safe to re-run after changing the lifecycle constants. The
    order matters -- versioning before anything writes, because versioning only
    protects objects written after it is enabled.
    """
    from botocore.exceptions import ClientError

    done = []
    try:
        s3.head_bucket(Bucket=bucket)
        done.append(f"bucket {bucket} already exists")
    except ClientError as exc:
        if _code(exc) != "404":
            raise
        kwargs = {"Bucket": bucket}
        if region != "us-east-1":
            kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
        s3.create_bucket(**kwargs)
        done.append(f"created {bucket} in {region}")

    s3.put_public_access_block(
        Bucket=bucket,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )
    done.append("blocked public access")

    s3.put_bucket_versioning(
        Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}
    )
    done.append("enabled versioning")

    s3.put_bucket_encryption(
        Bucket=bucket,
        ServerSideEncryptionConfiguration={
            "Rules": [
                {
                    "ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"},
                    "BucketKeyEnabled": True,
                }
            ]
        },
    )
    done.append("set default encryption (SSE-S3)")

    s3.put_bucket_lifecycle_configuration(
        Bucket=bucket, LifecycleConfiguration=lifecycle_configuration()
    )
    done.append(f"set lifecycle {LIFECYCLE_ID}")
    return done
