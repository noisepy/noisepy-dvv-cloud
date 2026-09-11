# 00b — Cloud hardening: what QuakeScope already learned

Written 2026-09-10/11, after reading
[SeisSCOPED/QuakeScope](https://github.com/SeisSCOPED/QuakeScope) —
`docs/aws_least_privilege.md`, `SECURITY_AUDIT.md`, and its `scripts/` suite —
against a live inspection of this AWS account.

**Read this before [00_unblock_plan.md](00_unblock_plan.md) Phase 1.** It
corrects three instructions in the runbook that are verified wrong, and it is
the reason Phase 1 should not be executed as currently written.

QuakeScope runs on the **same AWS account** as this project, with the same
funding, the same archives, and the same Fargate Spot pattern. Anything it
learned the hard way applies here directly.

---

## 1. Three corrections to the existing runbook

### 1.1 IAM: do NOT just reuse `NoisePyBatchRole`

[03_batch_setup.md](03_batch_setup.md) currently says "Already done — reuse
`NoisePyBatchRole`", because it carries `AmazonECSTaskExecutionRolePolicy` +
`AmazonS3FullAccess` and therefore needs no setup.

That is the exact finding QuakeScope's least-privilege doc was written after:

> Written after the campaign role was found holding `AmazonS3FullAccess` when
> it needed exactly one bucket.

Two problems, both of which QuakeScope already fixed on this account:

- **`AmazonS3FullAccess` reaches every bucket in the account** — nine of them,
  four being another person's SkyPilot file mounts. Versioning is not enabled
  by default, so a wrong prefix is unrecoverable.
- **`NoisePyBatchRole` is a single role used as both the Batch job role and the
  ECS execution role.** QuakeScope split these. The direction that catches
  people: **on Fargate the EXECUTION role resolves `secrets:`, not the job
  role**, and getting it backwards fails at task start with
  `ResourceInitializationError: unable to pull secrets`, which reads like a
  Secrets Manager problem rather than a role problem.

### 1.2 Gate 2 cannot be run as written — the cost APIs are denied

[00_unblock_plan.md](00_unblock_plan.md) Phase 4 says to run
`aws ce get-cost-and-usage`. Verified 2026-09-10, it cannot work:

```
ce:GetCostAndUsage   -> AccessDeniedException ... explicit deny in a service
                        control policy: .../p-q1ngvul9
budgets:ViewBudget   -> AccessDeniedException ... same SCP
```

This account is **CloudBank-funded, billed through Strategic Blue**, and the
organisation denies Cost Explorer, Budgets, Cost and Usage Reports and the Free
Tier API alike. Per QuakeScope's `costs_actual.json`, the denial is deliberate
rather than a misconfiguration to work around: CloudBank bills at its own rate,
so AWS-side figures would not be what the grant is charged anyway.

**Also affected:** [01_aws_setup.md](01_aws_setup.md) step 3 tells the operator
to create a Budget in the console as the "billing guardrail". That is denied by
the same SCP. There is currently **no billing guardrail on this project**, and
the runbook implies there is one.

The replacement pattern is QuakeScope's: a hand-edited `costs_actual.json`
holding real invoiced figures from the CloudBank portal, plus a separate
estimator derived from Batch job start/stop times. The file's own comment is
worth copying — it records *why* there is no API, so the next person does not
spend an afternoon rediscovering the SCP.

### 1.3 "No bucket policy needed" was the wrong conclusion

[03_batch_setup.md](03_batch_setup.md) argues no bucket policy is needed
because every IAM user is in the `scoped` group, which carries
`AmazonS3FullAccess`, and bucket policies are additive for Allow.

The reasoning is correct and the conclusion is backwards. That 30 users hold
`AmazonS3FullAccess` is the hazard, not a reason to skip protection. The
protection that actually works is not a bucket policy at all:

- **Bucket versioning**, enabled at creation. QuakeScope's `preflight.py` has a
  dedicated check whose failure message is "a wrong delete is PERMANENT".
- **Lifecycle rules**: expire noncurrent versions (30 days), abort incomplete
  multipart uploads (7 days), clear expired delete markers.
- **Grant `s3:DeleteObject` but never `s3:DeleteObjectVersion`**, so a worker
  can create a delete marker and cannot destroy history.

The optional delete-guard in [configs/bucket_policy.json](../../configs/bucket_policy.json)
is still worth applying, but it is the third line of defence, not the first.

---

## 2. Where this project is *easier* than QuakeScope

Both checked against the code on 2026-09-10, and both make our policy tighter
than the one QuakeScope settled on:

- **Archive reads need no IAM grant.** `correlate.py:68` sets
  `cfg.storage_options["s3"] = {"anon": True}`. SCEDC and NCEDC are public
  buckets read anonymously. QuakeScope's doc makes the same point: "the workers
  read five archives" does not imply "the role needs five buckets".
- **There are no delete calls anywhere in `src/`.** Verified by grep for
  `.rm(`, `delete_object`, `DeleteObject`, `.remove(` — zero hits. QuakeScope
  needed `s3:DeleteObject` because `parquet_compact.py` calls `fs.rm()` and
  `s3_state.py` calls `delete_object()`. We have no compaction stage and
  content-hash-named shards, so re-runs are idempotent by construction.

**Consequence: the job role needs no delete permission of any kind.**

Minimum viable policy for the dv/v campaign, one bucket:

| scope | actions | why |
|---|---|---|
| bucket | `s3:ListBucket`, `s3:GetBucketLocation`, `s3:ListBucketMultipartUploads` | dataset discovery; s3fs/botocore region resolution; Parquet writes above the multipart threshold |
| objects | `s3:GetObject`, `s3:PutObject`, `s3:AbortMultipartUpload`, `s3:ListMultipartUploadParts` | write CCF/dv/v shards, read CCFs back in stage 2, clean up a failed large put |
| — | **no `s3:DeleteObject*`** | nothing in `src/` deletes |

---

## 3. The rule to work by

From QuakeScope's doc, and the reason its scoping is trustworthy:

> **Simulate, do not read.** `iam:SimulatePrincipalPolicy` evaluates every
> attached and inline policy the way a real request would. Reading a policy
> document tells you what someone intended; simulation tells you what the role
> can do.

> **A scoping change that cannot be shown to deny something has not been shown
> to do anything.** Always simulate a *negative control*: an action on a
> resource the role must not reach. If your "after" run is all green and
> contains no denials, you have proved nothing.

Use `scoped-noise` as the negative control bucket, as QuakeScope does.

The six-step recipe, quoted because step 6 is the one that gets skipped:

1. List what the code actually touches. Check whether reads are anonymous
   before assuming they need a grant — that single check removed four buckets
   for QuakeScope, and all of the archive buckets for us.
2. Write the policy as a script, not a console click. It becomes the record,
   it is reviewable in a diff, and the next person can re-run it.
3. Simulate before. Capture the "wrong" rows; they are the justification.
4. Attach the narrow policy first, detach the broad one second.
5. Simulate after, with a negative control, and retry — IAM is eventually
   consistent and an immediate check can report a stale answer.
6. **Then run something real.** Simulation is a model of the evaluator. A job
   that starts, reads, writes an object and exits is the evidence that the
   model matched reality. A tiny job is enough.

The role-split order matters too, because the middle state is the dangerous
one: create the execution role, register **every** job definition revision
pointing at it, run something real, and only then strip the old role. Between
steps 2 and 4 both roles work, so no job can fail to start.

---

## 4. What to port, in dependency order

Items 1–3 are the foundation and should land **before any AWS object is
created**. 4–5 can follow while the correlate stage drains.

| # | Port | From QuakeScope | Why it matters here |
|---|---|---|---|
| 1 | `scripts/scope_iam.py` | `scripts/scope_batch_role_s3.py` | Creates the `DvvCloudExecutionRole` / `DvvCloudBatchRole` split, attaches the scoped policy before detaching `AmazonS3FullAccess`, simulates before/after with a negative control |
| 2 | Bucket versioning + lifecycle at creation | `preflight.py::check_bucket` | Cheaper at `aws s3 mb` time than retrofitted; the only real protection against a bad delete |
| 3 | `scripts/preflight.py` | `scripts/preflight.py` | Roles, images, bucket versioning, queue state. Exit 0 only if nothing FAILs; reports `UNKNOWN` when AWS is unreachable, because absence of evidence must not read as evidence of safety |
| 4 | `costs_actual.json` + Batch-runtime estimator | `costs_actual.json`, `scripts/campaign_spend.py` | **Replaces** Gate 2 rather than deferring it. Split spend into work that produced products and work that did not — a single total hides the thing worth knowing |
| 5 | `scripts/spot_governor.py` | `scripts/spot_governor.py` | See below — this one is not optional for the 2-year run |

### Why the Spot governor is not optional

QuakeScope measured **37–53% of Spot attempts reclaimed** on this account,
*rising* across days. Batch's `attempts` is capped at 10 and cannot be raised,
so a job's expected lifetime is roughly ten reclaims, after which it fails
permanently and **is never replaced** — the fleet decays to nothing while the
queue still has work.

[runbook/README.md](README.md) currently states as a design rule:
"**Everything retries.** Fargate Spot kills tasks; the retry strategy resubmits
them." That is true and insufficient. Batch retries an *attempt*; nothing
resubmits a *job* that exhausted its attempts. Something outside Batch has to
top the fleet back up. That is what `spot_governor.py` is: a poller, not an
EventBridge rule plus a Lambda, and it fails safe — if the governor dies the
fleet stops growing rather than stampeding.

### Other QuakeScope scripts worth knowing about

- `scripts/aws_watch.py` — what is running right now, hourly burn at current
  spot price, and what looks wrong (on-demand where Spot was intended, an idle
  controller). Read-only; prints emergency-stop commands rather than running
  them. Useful because Cost Explorer lags a day *and* is denied here.
- `scripts/register_jobdef.py` — job definitions as code, which matters given
  the role-split step "register **all** of them".
- `scripts/export_incident_logs.py` — CloudWatch retention on this account is
  five days; export before it ages out.
- `infra/inventory.py`, `scripts/campaign_status.py`,
  `scripts/campaign_dashboard.py` — fleet/campaign observability.

QuakeScope also uses **pixi** (`pixi.toml`, `pixi.lock`, `PIXI_SETUP.md`),
which independently matches the environment work already merged here.

### The agent for this work

[`.claude/agents/aws-cloud-architect.md`](../../.claude/agents/aws-cloud-architect.md)
is a project-level agent scoped to exactly these decisions — Batch role splits,
S3 prefix/lifecycle design, container leanness, and cost-per-throughput review.
Its Batch/IAM section already encodes most of §2 and §3 above independently:
never attach broad managed policies to the execution role because it is on every
task, no wildcard `Resource: "*"` in job roles, `iam simulate-principal-policy`
before a wide rollout, and an explicit instruction to refuse "just give the job
role admin so it works". One of its trigger examples is verbatim the situation
this project is in:

> "I just gave the Batch job role AmazonS3FullAccess so I could stop debugging
> permissions errors."

Two of its rules are **not** yet reflected anywhere in this runbook and should be
checked during the port:

- **VPC endpoints** (Gateway for S3; Interface for ECR/CloudWatch/STS) for Batch
  compute environments in private subnets. NAT Gateway data-processing charges at
  fan-out scale "can silently exceed the compute cost itself". Both job
  definitions set `assignPublicIp: ENABLED`, and `compute_environment.yaml` still
  has `subnets: ['']` unfilled — so whether tasks land in public or private
  subnets is an open decision, and it should be made deliberately rather than by
  pasting in whatever `describe-subnets` returns first.
- **Permission boundaries** on job roles where several pipelines share a compute
  environment — which is the case on this account.

---

## 5. Open decisions

Neither should be guessed; both were put to the user and are unanswered.

1. **Scope now** — port all five items, or just 1–3 (credentials/safety
   foundation) and leave cost tracking + governor until after the correlate run
   is submitted?
2. **Role naming** — create new `DvvCloudBatchRole` / `DvvCloudExecutionRole`,
   or scope `NoisePyBatchRole` in place? New roles are preferable because
   `NoisePyBatchRole` may have consumers outside this project that cannot be
   seen from here; scoping it in place would break them silently.

---

## 6. State as of 2026-09-11

**Repo.** `main` = `57eed51`, with PR #3 (pixi environments, Gate 1 comparison
fixes, cross-component masking, credential hygiene) and PR #4 (fallback coda
window) both merged. Merged local branches pruned.

**Open work.**

- `feature/weaver-band-form` — the `weaver_stretching_error_band` migration,
  **uncommitted** on that branch. Blocked on a codameter 0.5 release: PyPI's
  latest is 0.4.0 and the local `~/GitHub/codameter` checkout still
  self-reports `0.4.0` in `_version.py`. codameter 0.5 also made `bandwidth_hz`
  a **required** fifth argument to `weaver_stretching_error`, so the pin bump
  and the call switch must land in one atomic PR.
- `feature/cost-model` (`06f3170`) — pre-existing branch, not reviewed in this
  session.

**Sequencing insight worth keeping.** The correlate stage never imports
codameter (only two comments in `parquet_io.py` mention it). So the expensive
long pole — 3 stations x 2 years of correlations — has no dependency on the
Weaver work. Launch correlate first, land codameter 0.5 while the queue drains,
then run the dv/v stage once on 0.5 and never need
`codameter/scripts/correct_gate1_within_error.py` at all.

**Weaver correction, quantified** (measured 2026-09-10, old 0.4.0 vs local 0.5
source): a pure per-band constant, independent of `cc` — **2.16x** at 1–2 Hz,
**3.05x** at 2–4 Hz, **6.10x** at 8–16 Hz, all overestimates, spread across
`cc` ~1e-16. `processing_ensemble` takes a plain unweighted mean, so the
correction changes **only** the error columns, never dv/v. Gate 1 gates on
`r > 0.9` of the dv/v series and is unaffected either way.

**AWS.** Nothing for this project exists yet: no `dvvcloud*` compute
environment, queue or job definition, and no output bucket. The only Batch
objects on the account are the legacy `niyiyu-noisepy-scedc` ones. `configs/`
still has six `[REQUIRED]` placeholders across three files (subnets, security
group, and the role ARN four times). Fill `*.local.yaml` copies — the tracked
skeletons stay templates, and `.gitignore` covers that suffix.
