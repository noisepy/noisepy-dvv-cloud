"""Pin the two decisions that are easy to lose in a later edit.

Neither of these is a test of AWS. They are tests of the intent recorded in
docs/runbook/00b_cloud_hardening.md §2 and §3: the job role grants no delete of
any kind, and every simulation carries a negative control.
"""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

import scope_iam  # noqa: E402

from noisepy_dvv_cloud import bucket  # noqa: E402

BUCKET = "test-products"
ACCOUNT = "111122223333"
REGION = "us-west-2"


def _actions(doc):
    out = []
    for st in doc["Statement"]:
        out += st["Action"] if isinstance(st["Action"], list) else [st["Action"]]
    return out


def test_job_policy_grants_no_delete():
    """Nothing in src/ deletes, so the grant would be standing risk with no use."""
    assert not [a for a in _actions(scope_iam.policy_document(BUCKET))
                if "Delete" in a]


def test_job_policy_reaches_exactly_one_bucket():
    resources = {st["Resource"] for st in scope_iam.policy_document(BUCKET)["Statement"]}
    assert resources == {f"arn:aws:s3:::{BUCKET}", f"arn:aws:s3:::{BUCKET}/*"}
    assert not any(r.startswith("arn:aws:s3:::*") or r == "*" for r in resources)


def test_simulation_carries_a_negative_control():
    """An 'after' run that is all green and contains no denials proves nothing."""
    checks = scope_iam.job_role_checks(BUCKET, ACCOUNT, REGION)
    denials = [c for c in checks if c[2] is False]
    assert denials, "no denial is asserted anywhere"
    assert any(scope_iam.CONTROL_BUCKET in c[1] for c in denials)
    assert {a for a, _, allow in checks if not allow} >= set(
        scope_iam.FORBIDDEN_OBJECT_ACTIONS
    )


def test_execution_role_is_denied_s3():
    """The split is only real if the platform role cannot reach the products."""
    checks = scope_iam.exec_role_checks(BUCKET, ACCOUNT, REGION)
    s3_checks = [c for c in checks if c[0].startswith("s3:")]
    assert s3_checks and all(allow is False for _, _, allow in s3_checks)


def test_lifecycle_does_not_expire_current_versions():
    """Current versions are the campaign deliverable."""
    rule = bucket.lifecycle_configuration()["Rules"][0]
    assert rule["Status"] == "Enabled"
    assert set(rule["Expiration"]) == {"ExpiredObjectDeleteMarker"}
    assert rule["NoncurrentVersionExpiration"]["NoncurrentDays"] == bucket.NONCURRENT_DAYS
    assert (rule["AbortIncompleteMultipartUpload"]["DaysAfterInitiation"]
            == bucket.ABORT_MPU_DAYS)


class _FakeS3:
    """Just enough S3 to drive `bucket.audit` without network or credentials."""

    def __init__(self, **overrides):
        self.state = {
            "location": REGION,
            "versioning": "Enabled",
            "block": dict.fromkeys(
                ["BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy",
                 "RestrictPublicBuckets"], True),
            "lifecycle": bucket.lifecycle_configuration()["Rules"],
        }
        self.state.update(overrides)

    def head_bucket(self, Bucket):
        return {}

    def get_bucket_location(self, Bucket):
        return {"LocationConstraint": self.state["location"]}

    def get_bucket_versioning(self, Bucket):
        return {"Status": self.state["versioning"]} if self.state["versioning"] else {}

    def get_public_access_block(self, Bucket):
        return {"PublicAccessBlockConfiguration": self.state["block"]}

    def get_bucket_encryption(self, Bucket):
        return {"ServerSideEncryptionConfiguration": {"Rules": [
            {"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]}}

    def get_bucket_lifecycle_configuration(self, Bucket):
        return {"Rules": self.state["lifecycle"]}


def _states(rows):
    return {check: state for check, state, _ in rows}


def test_audit_passes_a_correctly_configured_bucket():
    rows = bucket.audit(_FakeS3(), BUCKET, REGION)
    assert bucket.FAIL not in {state for _, state, _ in rows}


@pytest.mark.parametrize(
    "override,check",
    [
        ({"versioning": None}, "versioning"),
        ({"location": "us-east-1"}, "region"),
        ({"lifecycle": []}, "lifecycle"),
        ({"block": {"BlockPublicAcls": False}}, "public access blocked"),
    ],
)
def test_audit_fails_each_missing_protection(override, check):
    """A check that cannot fail is not a check."""
    rows = bucket.audit(_FakeS3(**override), BUCKET, REGION)
    assert _states(rows)[check] == bucket.FAIL
