#!/usr/bin/env python
"""Is this account ready to run a campaign? One command, one answer.

Everything the hardening work put in place is checked here, so "is it safe to
submit" is a thing you run rather than a thing you remember. Ported from
QuakeScope's `scripts/preflight.py`, whose checks exist because an unnoticed
fleet sent 351,735 rejected requests to an archive operator over four hours.

    python scripts/preflight.py                # human output
    python scripts/preflight.py --markdown     # for a workflow step summary
    python scripts/preflight.py --skip-image   # no network to ghcr.io

Exit code is 0 only if nothing is FAIL. A check that cannot reach AWS reports
UNKNOWN, not FAIL: absence of evidence must not read as evidence of safety, and
must not read as an emergency either.

WHAT IT DOES NOT DO. It is a snapshot, not a monitor, and simulation is a model
of the IAM evaluator rather than the evaluator itself. The evidence that the
model matched reality is one real job that starts, reads, writes an object and
exits -- see docs/runbook/00b_cloud_hardening.md §3 step 6.
"""

from __future__ import annotations

import argparse
import datetime
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import check_image_public  # noqa: E402
import scope_iam  # noqa: E402

PASS, FAIL, WARN, UNKNOWN = "PASS", "FAIL", "WARN", "UNKNOWN"
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


class Report:
    def __init__(self):
        self.rows = []

    def add(self, area, check, state, detail=""):
        self.rows.append((area, check, state, detail))

    @property
    def failed(self):
        return [r for r in self.rows if r[2] == FAIL]

    def text(self):
        icon = {PASS: "ok  ", FAIL: "FAIL", WARN: "warn", UNKNOWN: "??  "}
        out, last = [], None
        for area, check, state, detail in self.rows:
            if area != last:
                out.append(f"\n{area}")
                last = area
            out.append(f"  [{icon[state]}] {check}"
                       + (f"\n           {detail}" if detail else ""))
        return "\n".join(out)

    def markdown(self):
        icon = {PASS: "✅", FAIL: "🛑", WARN: "⚠️", UNKNOWN: "❔"}
        out, last = [], None
        for area, check, state, detail in self.rows:
            if area != last:
                out += ["", f"### {area}", "", "| | check | detail |", "|---|---|---|"]
                last = area
            out.append(f"| {icon[state]} | {check} | {detail} |")
        return "\n".join(out)


def check_roles(r, iam, bucket, account, region):
    """The container reaches one bucket and cannot delete; the platform role
    cannot reach S3 at all."""
    if iam is None:
        r.add("Permissions", "role split", UNKNOWN, "no AWS access")
        return
    try:
        job = iam.get_role(RoleName=scope_iam.JOB_ROLE)["Role"]["Arn"]
        ex = iam.get_role(RoleName=scope_iam.EXEC_ROLE)["Role"]["Arn"]
    except Exception as exc:
        r.add("Permissions", "roles exist", FAIL,
              f"{type(exc).__name__}: run scripts/scope_iam.py --apply")
        return

    r.add("Permissions", "job and execution roles are distinct",
          PASS if job != ex else FAIL,
          "split" if job != ex
          else "one role does both -- the platform holds the container's S3 "
               "access and the container holds the platform's")

    ok, rows = scope_iam.simulate(
        iam, job, scope_iam.job_role_checks(bucket, account, region))
    wrong = [f"{a} on {res.rsplit(':', 1)[-1]} is {d}"
             for a, res, d, v in rows if v != "expected"]
    r.add("Permissions", "worker reaches one bucket, and cannot delete",
          PASS if ok else FAIL,
          f"{len(rows)} simulated rows all as intended" if ok
          else "; ".join(wrong[:3]))

    ok, rows = scope_iam.simulate(
        iam, ex, scope_iam.exec_role_checks(bucket, account, region))
    wrong = [f"{a} is {d}" for a, res, d, v in rows if v != "expected"]
    r.add("Permissions", "platform role cannot touch S3",
          PASS if ok else FAIL,
          "logs only" if ok else "; ".join(wrong[:3]))


def check_bucket(r, s3, bucket, region):
    """A bad delete must be recoverable, and protection must not become a bill."""
    if s3 is None:
        r.add("Products bucket", "protections", UNKNOWN, "no AWS access")
        return
    from noisepy_dvv_cloud import bucket as spec
    try:
        for check, state, detail in spec.audit(s3, bucket, region):
            r.add("Products bucket", check, state, detail)
    except Exception as exc:
        r.add("Products bucket", "audit", UNKNOWN, f"{type(exc).__name__}: {exc}"[:90])


def check_batch(r, batch, params):
    """What would actually launch, read back from Batch rather than from configs."""
    if batch is None:
        r.add("What would launch", "Batch objects", UNKNOWN, "no AWS access")
        return
    try:
        ces = batch.describe_compute_environments(
            computeEnvironments=[params.COMPUTE_ENVIRONMENT])["computeEnvironments"]
    except Exception as exc:
        r.add("What would launch", "compute environment", UNKNOWN, str(exc)[:80])
        return
    if not ces:
        r.add("What would launch", "compute environment", FAIL,
              f"{params.COMPUTE_ENVIRONMENT} does not exist")
    else:
        ce = ces[0]
        kind = ce.get("computeResources", {}).get("type", "?")
        good = (ce["state"] == "ENABLED" and ce["status"] == "VALID")
        r.add("What would launch", "compute environment",
              PASS if good else FAIL,
              f"{kind}, {ce['state']}/{ce['status']}"
              + ("" if kind == "FARGATE_SPOT"
                 else " -- not FARGATE_SPOT; on-demand Fargate is ~3x the price"))

    qs = batch.describe_job_queues(jobQueues=[params.JOB_QUEUE])["jobQueues"]
    if not qs:
        r.add("What would launch", "job queue", FAIL,
              f"{params.JOB_QUEUE} does not exist")
    else:
        q = qs[0]
        good = q["state"] == "ENABLED" and q["status"] == "VALID"
        live = 0
        for status in ("SUBMITTED", "PENDING", "RUNNABLE", "STARTING", "RUNNING"):
            live += len(batch.list_jobs(jobQueue=params.JOB_QUEUE,
                                        jobStatus=status)["jobSummaryList"])
        r.add("What would launch", "job queue", PASS if good else FAIL,
              f"{q['state']}/{q['status']}, {live} job(s) alive")

    for name in (params.JOB_DEFINITION_CORRELATE, params.JOB_DEFINITION_DVV):
        jds = batch.describe_job_definitions(
            jobDefinitionName=name, status="ACTIVE")["jobDefinitions"]
        if not jds:
            r.add("What would launch", f"job definition {name}", FAIL,
                  "not registered")
            continue
        jd = max(jds, key=lambda d: d["revision"])
        props = jd["containerProperties"]
        job_arn = props.get("jobRoleArn", "")
        exec_arn = props.get("executionRoleArn", "")
        problems = []
        if not job_arn.endswith(f"/{scope_iam.JOB_ROLE}"):
            problems.append(f"jobRoleArn is {job_arn.rsplit('/', 1)[-1] or 'unset'}")
        if not exec_arn.endswith(f"/{scope_iam.EXEC_ROLE}"):
            problems.append(
                f"executionRoleArn is {exec_arn.rsplit('/', 1)[-1] or 'unset'}")
        r.add("What would launch", f"job definition {name}",
              PASS if not problems else FAIL,
              f"rev {jd['revision']}, {props['image'].rsplit('/', 1)[-1]}"
              + ("" if not problems else " -- " + "; ".join(problems)))


def check_image(r):
    """Fargate pulls with the execution role, which has no ghcr identity."""
    try:
        token, why = check_image_public.anon_token(check_image_public.DEFAULT_REPO)
    except Exception as exc:
        r.add("Image", "anonymously pullable", UNKNOWN, str(exc)[:80])
        return
    if token is None:
        r.add("Image", "anonymously pullable",
              FAIL if why == "private" else UNKNOWN,
              "package is private -- Batch fails at task start with "
              "CannotPullContainerError, before any of our code runs"
              if why == "private" else why)
        return
    bad = [t for t in check_image_public.DEFAULT_TAGS
           if check_image_public.manifest_status(
               check_image_public.DEFAULT_REPO, t, token) != 200]
    r.add("Image", "anonymously pullable", PASS if not bad else FAIL,
          f"{', '.join(check_image_public.DEFAULT_TAGS)} all HTTP 200"
          if not bad else f"missing or unreadable: {', '.join(bad)}")


def check_configs(r):
    """Account detail must stay out of git. The tracked YAMLs are templates;
    the filled copies are `*.local.yaml`, which .gitignore covers."""
    filled = []
    for path in sorted((REPO_ROOT / "configs").glob("*.yaml")):
        text = path.read_text()
        if "[REQUIRED]" in text and "''" not in text:
            filled.append(path.name)
    r.add("Repo hygiene", "tracked configs are still templates",
          PASS if not filled else WARN,
          "placeholders unfilled" if not filled
          else f"{', '.join(filled)} look filled in -- copy to *.local.yaml "
               "instead, so subnets and role ARNs are not committed")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--markdown", action="store_true")
    ap.add_argument("--skip-image", action="store_true",
                    help="skip the ghcr.io check (no outbound network)")
    ap.add_argument("--bucket", default=None,
                    help="products bucket (default: $DVV_OUTPUT_BUCKET)")
    args = ap.parse_args(argv)

    from noisepy_dvv_cloud import parameters

    bucket = args.bucket or parameters.OUTPUT_BUCKET
    region = parameters.AWS_REGION

    r = Report()
    batch = iam = s3 = None
    account = ""
    try:
        import boto3
        sts = boto3.client("sts", region_name=region)
        ident = sts.get_caller_identity()
        account = ident["Account"]
        batch = boto3.client("batch", region_name=region)
        iam = boto3.client("iam")
        s3 = boto3.client("s3", region_name=region)
        r.add("Account", "credentials", PASS,
              f"{ident['Arn'].rsplit('/', 1)[-1]} in {account}, {region}")
    except Exception as exc:
        r.add("Account", "credentials", UNKNOWN,
              f"{type(exc).__name__} -- every AWS check below reports UNKNOWN")

    if not bucket:
        r.add("Account", "products bucket named", FAIL,
              "DVV_OUTPUT_BUCKET is unset; nothing knows where products go")
        s3 = None

    check_batch(r, batch, parameters)
    if not args.skip_image:
        check_image(r)
    if bucket:
        check_roles(r, iam, bucket, account, region)
        check_bucket(r, s3, bucket, region)
    check_configs(r)

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if args.markdown:
        print(("## 🛑 Something needs attention" if r.failed
               else "## ✅ Clear — safe to submit") + f"\n\n_Checked {stamp}._")
        print(r.markdown())
        if r.failed:
            print("\n**Do not submit a campaign until these are resolved.** "
                  "Background: [00b_cloud_hardening.md]"
                  "(docs/runbook/00b_cloud_hardening.md).")
    else:
        print(f"noisepy-dvv-cloud preflight · {stamp}")
        print(r.text())
        print("\n  " + ("SOMETHING NEEDS ATTENTION" if r.failed
                        else "clear — safe to submit"))
    return 1 if r.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
