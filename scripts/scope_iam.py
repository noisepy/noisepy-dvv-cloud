#!/usr/bin/env python
"""Create the Batch role split for this campaign, scoped to one bucket, and prove it.

Two roles, because Fargate uses them for different things and merging them is
the mistake this account has already made once:

  DvvCloudBatchRole      the JOB role -- what our code runs as. One bucket,
                         read and write, and no delete of any kind.
  DvvCloudExecutionRole  the EXECUTION role -- what the ECS agent uses to pull
                         the image and open the log stream, before our code
                         exists. Never touches S3.

Why new roles rather than scoping `NoisePyBatchRole` in place: that role is the
live `jobRoleArn` on both revisions of `niyiyu-noisepy-scedc-2022`. Narrowing it
would change what somebody else's jobs can do, silently. Roles are free.

Why no delete permission: nothing in `src/` deletes. Verified by grep for
`.rm(`, `delete_object`, `DeleteObject`, `.remove(` -- zero hits. Shard files
are content-hash-named, so a re-run overwrites rather than needing a cleanup
pass. QuakeScope needed `s3:DeleteObject` because `parquet_compact.py` calls
`fs.rm()`; we have no compaction stage, so the grant would be unused standing
risk. The simulation below asserts the denial rather than assuming it.

Archive reads need no grant at all: `correlate.py:68` sets
`storage_options["s3"] = {"anon": True}`, so scedc-pds and ncedc-pds are read
anonymously.

    python scripts/scope_iam.py --check     # simulate only, changes nothing
    python scripts/scope_iam.py --apply     # create/update, then simulate

`--apply` is idempotent: it upserts the inline policy and the managed
attachment, so re-running after widening `OBJECT_ACTIONS` is the supported way
to change the grant. It never detaches anything from a role it did not create.

TO REVERSE entirely:

    aws iam delete-role-policy --role-name DvvCloudBatchRole \\
        --policy-name DvvCloudProductsS3
    aws iam detach-role-policy --role-name DvvCloudExecutionRole \\
        --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
    aws iam delete-role --role-name DvvCloudBatchRole
    aws iam delete-role --role-name DvvCloudExecutionRole

LIMIT OF THE EVIDENCE. `SimulatePrincipalPolicy` evaluates identity policies;
it does not reliably reflect service control policies, and this account sits
under SCP `p-q1ngvul9`. Simulation is a model of the evaluator, so it is
necessary and not sufficient -- the campaign is only proven by a real job that
starts, reads, writes one object and exits. `scripts/preflight.py` is the
before-launch version of the same question.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

JOB_ROLE = "DvvCloudBatchRole"
EXEC_ROLE = "DvvCloudExecutionRole"
POLICY_NAME = "DvvCloudProductsS3"
ECS_EXEC_POLICY = (
    "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
)
FULL_ACCESS = "arn:aws:iam::aws:policy/AmazonS3FullAccess"
LOG_GROUP = "/aws/batch/job"

# Everything the pipeline does to the products bucket.
#
#   ListBucket                  dataset discovery for the Parquet reader
#   GetBucketLocation           s3fs/botocore region resolution
#   ListBucketMultipartUploads  Parquet writes above the multipart threshold
BUCKET_ACTIONS = [
    "s3:ListBucket",
    "s3:GetBucketLocation",
    "s3:ListBucketMultipartUploads",
]

#   PutObject                   CCF shards (stage 1), dv/v tables (stage 2)
#   GetObject                   stage 2 reads stage 1's output back
#   Abort/ListMultipartUpload*  clean up a large Parquet put that died
OBJECT_ACTIONS = [
    "s3:GetObject",
    "s3:PutObject",
    "s3:AbortMultipartUpload",
    "s3:ListMultipartUploadParts",
]

# Must stay denied on our OWN bucket. Listed explicitly because "we did not
# grant it" is not evidence -- a group policy or a later managed attachment
# could grant it without this file changing.
FORBIDDEN_OBJECT_ACTIONS = ["s3:DeleteObject", "s3:DeleteObjectVersion"]

# A bucket the campaign must never reach, for the negative control. A scoping
# change that cannot be shown to deny something has not been shown to do
# anything: an "after" run that is all green and contains no denials proves
# nothing. QuakeScope uses the same name.
CONTROL_BUCKET = "scoped-noise"


def trust_policy(account: str) -> dict:
    """Fargate tasks assume both roles. The account condition is AWS's
    documented confused-deputy guard for `ecs-tasks.amazonaws.com`; it costs
    nothing here because every task runs in this account."""
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                "Action": "sts:AssumeRole",
                "Condition": {"StringEquals": {"aws:SourceAccount": account}},
            }
        ],
    }


def policy_document(bucket: str) -> dict:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "ProductsBucket",
                "Effect": "Allow",
                "Action": BUCKET_ACTIONS,
                "Resource": f"arn:aws:s3:::{bucket}",
            },
            {
                "Sid": "ProductsObjects",
                "Effect": "Allow",
                "Action": OBJECT_ACTIONS,
                "Resource": f"arn:aws:s3:::{bucket}/*",
            },
        ],
    }


def job_role_checks(bucket: str, account: str, region: str):
    """(action, resource, should_be_allowed) for the job role."""
    obj = f"arn:aws:s3:::{bucket}/ccf/v1/probe.parquet"
    checks = [(a, f"arn:aws:s3:::{bucket}", True) for a in BUCKET_ACTIONS]
    checks += [(a, obj, True) for a in OBJECT_ACTIONS]
    # the no-delete decision, asserted rather than assumed
    checks += [(a, obj, False) for a in FORBIDDEN_OBJECT_ACTIONS]
    # negative control: another bucket in the same account
    checks += [
        (a, f"arn:aws:s3:::{CONTROL_BUCKET}/anything", False)
        for a in ("s3:GetObject", "s3:PutObject", "s3:DeleteObject")
    ]
    checks.append(("s3:ListBucket", f"arn:aws:s3:::{CONTROL_BUCKET}", False))
    # the container must not be able to resolve secrets; on Fargate that is
    # the execution role's job, and confusing the two is the documented trap.
    # The ARN need not exist -- simulation evaluates the policy, not the world.
    checks.append((
        "secretsmanager:GetSecretValue",
        f"arn:aws:secretsmanager:{region}:{account}:secret:dvvcloud/probe",
        False,
    ))
    return checks


def exec_role_checks(bucket: str, account: str, region: str):
    """(action, resource, should_be_allowed) for the execution role.

    The image lives on ghcr.io, so pulling it needs no AWS permission at all --
    only the log stream does. The S3 rows are the ones that matter: they are
    what makes the split real rather than nominal.
    """
    log_arn = f"arn:aws:logs:{region}:{account}:log-group:{LOG_GROUP}:*"
    return [
        ("logs:CreateLogStream", log_arn, True),
        ("logs:PutLogEvents", log_arn, True),
        ("s3:PutObject", f"arn:aws:s3:::{bucket}/x", False),
        ("s3:GetObject", f"arn:aws:s3:::{bucket}/x", False),
    ]


def simulate(iam, role_arn, checks):
    rows, ok = [], True
    for action, resource, want_allow in checks:
        result = iam.simulate_principal_policy(
            PolicySourceArn=role_arn,
            ActionNames=[action],
            ResourceArns=[resource],
        )
        decision = result["EvaluationResults"][0]["EvalDecision"]
        good = (decision == "allowed") == want_allow
        ok &= good
        rows.append((action, resource, decision, "expected" if good else "WRONG"))
    return ok, rows


def show(rows):
    for action, resource, decision, verdict in rows:
        mark = " " if verdict == "expected" else "!"
        print(f"  {mark} {action:34} {resource:52} {decision:12} {verdict}")


def describe(iam, role):
    """Current attachments, or None if the role does not exist yet."""
    try:
        arn = iam.get_role(RoleName=role)["Role"]["Arn"]
    except iam.exceptions.NoSuchEntityException:
        return None
    attached = [
        p["PolicyArn"]
        for p in iam.list_attached_role_policies(RoleName=role)["AttachedPolicies"]
    ]
    inline = iam.list_role_policies(RoleName=role)["PolicyNames"]
    return arn, attached, inline


def report(iam, role):
    info = describe(iam, role)
    if info is None:
        print(f"role {role}: DOES NOT EXIST")
        return None
    arn, attached, inline = info
    print(f"role {role}")
    print(f"  attached: {[p.split('/')[-1] for p in attached] or 'none'}")
    print(f"  inline  : {inline or 'none'}")
    if FULL_ACCESS in attached:
        print("  ! AmazonS3FullAccess is attached -- that is the thing this "
              "script exists to avoid")
    return arn


def ensure_roles(iam, account, bucket):
    doc = json.dumps(trust_policy(account))
    for role, description in (
        (JOB_ROLE, "noisepy-dvv-cloud Batch JOB role: one bucket, no delete"),
        (EXEC_ROLE, "noisepy-dvv-cloud ECS EXECUTION role: pull image, write logs"),
    ):
        try:
            iam.create_role(
                RoleName=role,
                AssumeRolePolicyDocument=doc,
                Description=description,
                Tags=[{"Key": "project", "Value": "noisepy-dvv-cloud"}],
            )
            print(f"  created {role}")
        except iam.exceptions.EntityAlreadyExistsException:
            iam.update_assume_role_policy(RoleName=role, PolicyDocument=doc)
            print(f"  {role} exists; trust policy refreshed")

    iam.put_role_policy(
        RoleName=JOB_ROLE,
        PolicyName=POLICY_NAME,
        PolicyDocument=json.dumps(policy_document(bucket)),
    )
    print(f"  put inline policy {POLICY_NAME} on {JOB_ROLE} (bucket {bucket})")

    iam.attach_role_policy(RoleName=EXEC_ROLE, PolicyArn=ECS_EXEC_POLICY)
    print(f"  attached AmazonECSTaskExecutionRolePolicy to {EXEC_ROLE}")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="simulate only")
    group.add_argument("--apply", action="store_true", help="create/update, then simulate")
    ap.add_argument("--bucket", default=None,
                    help="products bucket (default: $DVV_OUTPUT_BUCKET)")
    args = ap.parse_args(argv)

    from noisepy_dvv_cloud import parameters

    bucket = args.bucket or parameters.OUTPUT_BUCKET
    if not bucket:
        print("no bucket: pass --bucket or set DVV_OUTPUT_BUCKET", file=sys.stderr)
        return 2

    import boto3

    iam = boto3.client("iam")
    account = boto3.client("sts").get_caller_identity()["Account"]
    region = parameters.AWS_REGION

    if args.apply:
        ensure_roles(iam, account, bucket)
        print()

    job_arn = report(iam, JOB_ROLE)
    exec_arn = report(iam, EXEC_ROLE)
    print()
    if job_arn is None or exec_arn is None:
        print("  run --apply to create the missing role(s)")
        return 1

    if job_arn == exec_arn:
        print("  ! job role and execution role are the same role -- the split "
              "is nominal")
        return 1

    # IAM is eventually consistent, so an immediate check after --apply can
    # report a stale answer. Retry rather than mislead.
    attempts = 12 if args.apply else 1
    for attempt in range(attempts):
        ok_job, rows_job = simulate(
            iam, job_arn, job_role_checks(bucket, account, region)
        )
        ok_exec, rows_exec = simulate(
            iam, exec_arn, exec_role_checks(bucket, account, region)
        )
        if ok_job and ok_exec:
            break
        if attempt < attempts - 1:
            time.sleep(5)

    print(f"{JOB_ROLE} -- what the container can do")
    show(rows_job)
    print(f"\n{EXEC_ROLE} -- what the platform can do")
    show(rows_exec)

    ok = ok_job and ok_exec
    print("\n  " + ("scoped to one bucket, no delete, denied elsewhere"
                    if ok else "NOT as intended -- see the WRONG rows above"))
    if ok:
        print("\n  export DVV_JOB_ROLE_ARN=" + job_arn)
        print("  export DVV_EXECUTION_ROLE_ARN=" + exec_arn)
        print("\n  Simulation is not proof. Run one real job before the "
              "campaign (00b_cloud_hardening.md §3 step 6).")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
