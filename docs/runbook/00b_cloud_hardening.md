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
| 1 | **done** — [`scripts/scope_iam.py`](../../scripts/scope_iam.py) | `scripts/scope_batch_role_s3.py` | Creates the `DvvCloudExecutionRole` / `DvvCloudBatchRole` split and the scoped one-bucket policy, simulates both roles against a negative control. Departs from the original in two ways: it creates new roles rather than narrowing a shared one, and it grants **no delete at all** |
| 2 | **done** — [`scripts/create_bucket.py`](../../scripts/create_bucket.py) + [`bucket.py`](../../src/noisepy_dvv_cloud/bucket.py) | `preflight.py::check_bucket` | Versioning, public-access blocks, SSE-S3, lifecycle — set at creation, because versioning only covers objects written after it is enabled. The spec and the check live in one module so they cannot drift |
| 3 | **done** — [`scripts/preflight.py`](../../scripts/preflight.py) | `scripts/preflight.py` | Roles, images, bucket versioning, Batch objects, and whether the tracked configs still hold account detail. Exit 0 only if nothing FAILs; reports `UNKNOWN` when AWS is unreachable, because absence of evidence must not read as evidence of safety |
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

## 4b. Credential leak audit — 2026-09-11

Run again after any change to CI, Docker, or the ignore files. Method matters:
history was scanned by materialising **every blob on every ref**, not by reading
the current tree.

| Surface | Method | Result |
|---|---|---|
| Tracked files | `detect-secrets` 1.5.0, all tracked paths | **0 findings** |
| Full history | `detect-secrets` over all 21 commits on every ref (`git archive` per commit) | **0 findings** |
| Full history | regex sweep for `AKIA`/`ASIA`/`A3T*`, PEM headers, `aws_secret_access_key`, `xox*`, `ghp_`, `github_pat_`, `sk-`, JWTs | **0 findings** |
| Sensitive filenames | every path ever added on any branch, filtered for `.env`/`.pem`/`.key`/`credentials`/`id_rsa`/`.tfstate` | **none ever committed** |
| Account identifiers | this account's and the legacy account's 12-digit ids, across all history | **never committed** — docs use `ACCOUNT_ID`, and this row deliberately does not spell them out either |
| GitHub Actions | `.github/workflows/docker.yml` | only `secrets.GITHUB_TOKEN`; `permissions:` least-privilege (`contents: read`, `packages: write`); **no AWS credentials, no OIDC role** |
| Repo settings | `actions/secrets`, `actions/variables` | **0 secrets, 0 variables** — nothing to leak |
| Container images | both Dockerfiles | no `ENV`/`ARG` secrets; explicit `COPY pyproject.toml README.md` + `COPY src`, never `COPY . .` |
| Application code | `os.environ` / logging of env | only `DVV_JOB_ROLE_ARN` / `DVV_EXECUTION_ROLE_ARN` reads; nothing dumps the environment |
| `pixi.lock` | embedded `user:pass@` URLs, token/secret strings | **clean** — public conda-forge and PyPI only |
| Repo visibility | — | PRIVATE |

### The gap that was found, and fixed

Everything above was already clean. The actual risk was **forward-looking**:
`.gitignore` was 17 lines and covered build artefacts only. `.env`, `*.pem`,
`*.key`, `credentials*`, `id_rsa*`, `.aws/`, `.netrc`, `*.tfstate` and `*.log`
were **all stageable**. Nothing stopped a dropped key from being committed,
and `git add -A` is used routinely here.

For comparison, QuakeScope's `.gitignore` is 188 lines and its security audit
specifically credits it for excluding `*.pem`, `.env` and `*.log`.

Fixed in this PR:

- **`.gitignore`** — credential patterns added, with `!.env.example` kept
  stageable. Verified: all of `.env`, `.env.local`, `credentials.json`,
  `aws_credentials`, `id_rsa`, `key.pem`, `.aws/credentials`, `secrets.yaml`,
  `run.log`, `.netrc`, `terraform.tfstate` are now ignored, and
  `git ls-files -i -c` confirms **no tracked file was shadowed**.
- **`.dockerignore`** — added. Nothing sensitive reaches an image today because
  the Dockerfiles COPY explicit paths, but a later switch to `COPY . .` would
  have swept in local AWS config and campaign outputs. The three COPY paths
  (`pyproject.toml`, `README.md`, `src`) are confirmed still included.

### Automated, 2026-09-12

The hand-run audit above is a snapshot, and a snapshot is not a control. It is
now continuous:

- **`.github/workflows/security.yml`** on every push to `main`, every PR, and
  weekly. Two scanners, because they cover different things: `detect-secrets`
  (27 rule plugins + entropy) over tracked files, baseline-diffed so a
  known-safe string does not fail every future PR; and `gitleaks` over **full
  history** on the pushed ref, which catches a secret that was committed and
  later removed — something a working-tree scan structurally cannot see.
  `fetch-depth: 0` is required for the second one to mean anything.
  Both are free: gitleaks is fetched as a pinned release binary rather than via
  `gitleaks-action`, which wants a `GITLEAKS_LICENSE` for organisation repos.
- **`.pre-commit-config.yaml`** — `pre-commit` had been a declared dev
  dependency since the scaffold with no config file, so `pre-commit install`
  did nothing. Now `detect-secrets`, `detect-private-key`,
  `detect-aws-credentials`, large-file and merge-conflict guards, and `ruff`.
  Run `pixi run -e dvv pre-commit install` once per clone.
- **`.secrets.baseline`** — 27 plugins, 0 findings at creation.

Verified rather than assumed: the hook exits 0 on the repo as it stands, and a
planted `aws_secret_access_key` is caught by three separate detectors and
blocked with a nonzero exit.

**Remaining tradeoff:** `*.log` is gitignored, so an intentionally committed
log needs `git add -f`. That is the right default — tracebacks carry presigned
URLs and account ids.

### The image must be public — verified launch blocker

`ghcr.io/noisepy/noisepy-dvv-cloud` is **private**, and both tags exist
(`correlate-latest`, `dvv-latest`, built 2026-09-10). AWS Batch on Fargate
pulls with the ECS execution role, which has no ghcr identity, so a campaign
would fail at task start with `CannotPullContainerError` before any of our code
runs.

ghcr is unambiguous about this, which is what makes it checkable without any
credentials:

| package | `GET ghcr.io/token?scope=repository:<repo>:pull` |
|---|---|
| public | `200` + `{"token": ...}` |
| private | `401` + `{"errors":[{"code":"UNAUTHORIZED"}]}` |

[`scripts/check_image_public.py`](../../scripts/check_image_public.py) does
exactly that, stdlib only, and is wired into the `security` workflow as the
`image-pullable` job — informational on PRs, a real failure on `main`, where
the published image is what a campaign would launch. Confirmed against a
**positive control**: `seisscoped/quakescope` returns `HTTP 200 PUBLIC`, so the
check can distinguish, not just fail.

Two ways forward. This repo takes the first, as QuakeScope does — its
`register_jobdef.py` resolves manifests through the same anonymous token
endpoint, which only succeeds for a public package:

1. **Make the ghcr package public.** No credentials anywhere, nothing to
   rotate. **This publishes `src/`, `pyproject.toml` and `README.md` to anyone**
   — the repo is currently private, so this is a disclosure decision, not just
   a config toggle. The project is MIT-licensed and framed as a successor to a
   published paper, so it is very likely the intended end state; it should still
   be made deliberately.
2. **Keep it private** and add `repositoryCredentials` to both job definitions
   pointing at a Secrets Manager secret holding a ghcr PAT, granting
   `secretsmanager:GetSecretValue` to the **execution** role — not the job role.
   More moving parts, a credential to rotate, and it walks straight into the
   trap in §1.1.

Requires package admin. A `gh` token with only `repo` scope cannot change it
through the API; the local token's scopes are
`admin:public_key, gist, read:org, repo`, so this is a web-UI action:
**GitHub → Packages → noisepy-dvv-cloud → Package settings → Change visibility.**

## 5. Decisions, and the evidence that settled them — 2026-09-22

Both were open; neither was guessed.

**1. Scope — items 1–3 now, 4–5 after the correlate stage is submitted.**
Items 1–3 gate the first job: nothing can be launched safely without the roles,
the bucket and a check. Cost tracking (item 4) has nothing to measure until
jobs run, and the Spot governor (item 5) matters over a two-year campaign, not
over a ten-day smoke test.

**2. Role naming — new `DvvCloud*` roles, not `NoisePyBatchRole` scoped in
place.** The concern was that the legacy role might have consumers invisible
from here. It does: it is the `jobRoleArn` on **both revisions of
`niyiyu-noisepy-scedc-2022`**, and on nothing else on the account. Narrowing it
would change what those jobs can do without telling their owner. Roles cost
nothing.

Simulated read-only against that role on 2026-09-22, using the same check list
`scope_iam.py` applies to the new one — this is the "before" row set that §3
step 3 asks for:

| action | resource | decision |
|---|---|---|
| `s3:ListBucket`, `s3:GetBucketLocation`, `s3:ListBucketMultipartUploads` | the products bucket | allowed (wanted) |
| `s3:GetObject`, `s3:PutObject`, `s3:AbortMultipartUpload`, `s3:ListMultipartUploadParts` | a product object | allowed (wanted) |
| `s3:DeleteObject`, `s3:DeleteObjectVersion` | a product object | **allowed — not wanted** |
| `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject` | `scoped-noise/anything` | **allowed — not wanted** |
| `s3:ListBucket` | `scoped-noise` | **allowed — not wanted** |
| `secretsmanager:GetSecretValue` | a secret ARN | implicitDeny (wanted) |

Six of fourteen rows wrong, all of them `AmazonS3FullAccess` reaching past the
one bucket this campaign uses.

**3. Public vs private subnets — public, decided rather than inherited.** §4
flagged VPC endpoints as an unchecked rule. Checked 2026-09-22: QuakeScope's
`niyiyu-noisepy-scedc` compute environment sits in the account's **default
VPC**, whose single main route table sends `0.0.0.0/0` to an internet gateway,
so all four of its subnets are public. **The account has no NAT gateways and no
VPC endpoints at all.** The NAT data-processing charge that motivates interface
endpoints therefore cannot be incurred, and `assignPublicIp: ENABLED` in both
job definitions matches the pattern already proven on this account. An S3
gateway endpoint is free and still worth adding; it is a refinement, not a
blocker.

**4. Permission boundaries — not used, deliberately.** §4 flagged them as the
second unchecked agent rule. The rule is about job roles that share a compute
environment; `dvvcloud2026_env` is this project's alone, and the roles it uses
are new and reach one bucket. What a boundary would actually buy is a cap on
future careless grants — and `scripts/preflight.py` already catches that, by
simulating the denials on every run rather than trusting that nobody attached
`AmazonS3FullAccess` later. A control that runs beats a control that is
configured. Revisit if a second pipeline is ever pointed at these roles.

---

## 6. State as of 2026-09-22

**Repo.** `main` = `986b668`. PR #3 (pixi environments, Gate 1 comparison
fixes, cross-component masking, credential hygiene), PR #4 (fallback coda
window), PR #5 (this document) and PR #6 (automated secret scanning, the
`image-pullable` check) all merged. Merged local branches pruned.

**Launch blocker cleared.** `ghcr.io/noisepy/noisepy-dvv-cloud` was made public
on 2026-09-22. Both tags return HTTP 200 to an anonymous pull token, and the
`image-pullable` job in the `security` workflow passes as a hard check on
`main` — so a Fargate task can pull what a campaign would launch.

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

**AWS — created 2026-09-22, `preflight.py` exit 0.** Everything Phase 1 calls
for now exists:

| object | name | note |
|---|---|---|
| products bucket | `denolle-dvv-cloud-2026` | us-west-2; versioning, all four public-access blocks, SSE-S3, lifecycle `dvvcloud-version-hygiene` |
| job role | `DvvCloudBatchRole` | inline `DvvCloudProductsS3`, one bucket, no delete |
| execution role | `DvvCloudExecutionRole` | `AmazonECSTaskExecutionRolePolicy` only |
| compute environment | `dvvcloud2026_env` | FARGATE_SPOT, ENABLED/VALID, maxvCpus 256 |
| job queue | `dvvcloud2026_queue` | ENABLED/VALID |
| job definitions | `dvvcloud2026_correlate:1`, `dvvcloud2026_dvv:1` | both on the `DvvCloud*` role pair |

Networking: the four public subnets of the default VPC (`us-west-2a`–`d`,
`MapPublicIpOnLaunch: true`) and the default security group, which allows all
egress and no ingress from outside itself. The filled values live in
`configs/*.local.yaml`, which `.gitignore` covers; the six `[REQUIRED]`
placeholders in the tracked YAMLs are untouched, and `preflight.py` checks that
they stay that way.

**The account's `aws` binary is CLI 2.0.34 (2020) and rejects
`--no-cli-pager`.** Use `pixi run -e ops aws ...`, which is 2.36.24. That is
the second reason the `ops` environment exists, alongside the ruamel-yaml pin
conflict.

**§3 step 6 — done 2026-09-22.** Both stages ran end to end on CI.LJR (Lake
Hughes, the Clements-Denolle 2022 reference station), 2023.001–2023.011, from
`station_lists/smoke_ljr.txt`:

| job | exit | queue→start | run | resources |
|---|---|---|---|---|
| `correlate_20260922115411_0` | 0 | 59 s | 81 s | 2 vCPU / 16 GB |
| `dvv_20260922115751_0` | 0 | 53 s | 26 s | 2 vCPU / 8 GB |

What each one proves, which is why both were needed:

- **correlate** reached `RUNNING` 60 s after submission, so `DvvCloudExecutionRole`
  pulled the public ghcr image with no `CannotPullContainerError`, read
  `scedc-pds` anonymously, and wrote under `DvvCloudBatchRole`.
- **dvv** read the CCFs back out of the products bucket, which is the only
  thing that exercises the job role's `s3:GetObject`. correlate never does —
  it reads the archive anonymously and only writes.

Products: six CCF Parquet shards (`EE EN EZ NN NZ ZZ`, the `acorr_only` upper
triangle) and four dv/v tables, one per octave band. The CCFs check out
physically: lag axis exactly 2561 samples = 2 x 32 s x 40 Hz + 1, 100 % finite,
all ten days present, and the ZZ autocorrelation peaks at the zero-lag sample
on nine of ten days (day two at +0.05 s). `nwindows` runs 71–121 against the
~189 a gapless day would give, so LJR has real gaps in that window — worth
watching on the campaign, not a blocker.

Object versioning is live on the products: every key carries a `VersionId`.

**Do not read a dollar figure into those runtimes.** Cost Explorer is denied by
SCP `p-q1ngvul9` (§1.2), and one 10-day shard amortises container start and the
StationXML catalogue load differently from a 30-day campaign shard. 8 s per
station-day is a first data point, not a rate. The estimator that turns Batch
start/stop times into spend is §4 item 4 and is still unported.

### The defect the smoke test found

**dv/v came back NaN on all ten days in all four bands, while `cc` was 0.91 to
1.00 and `n_members` said 4.** A product that reports four contributing members
*and* a NaN measurement is self-inconsistent, which is what makes this worth
chasing rather than filing under "ten days is too short".

Reproduced locally against the same S3 CCFs, per ensemble member, band 2–4 Hz:

| member | config change | finite dv/v |
|---|---|---|
| `baseline` | — | 10/10 |
| `stack_half` | stack 90 → 45 | 10/10 |
| `stack_double` | stack 90 → 180 | 10/10 |
| `window_late` | coda window +25 % | 10/10 |
| `ref_swap` | reference `fixed` → `moving` | **0/10** |

Four members produced a measurement on every epoch. `stack_double` asks for 180
days of history and still returns finite values from ten, so the stack-length
members degrade gracefully. **`reference: "moving"` does not** — it returns
nothing at all on a series this short.

The failure is then arithmetic: `processing_ensemble` takes `stack.mean(axis=0)`,
a plain mean, so a single all-NaN member makes the ensemble mean NaN at *every*
epoch. Measured on these ten days: plain mean 0/10 finite, `np.nanmean` 10/10.

**Why this matters beyond a short smoke test.** It is not "ten days is too
short" — it is "one failing member silently discards the other four". On the
campaign that costs the leading epochs of every station, for however long the
moving reference needs to spin up, and it would cost any isolated epoch where
one member happens to fail. `compare_cd2022.py` drops a 150-day burn-in, so
Gate 1 may well never see it, which is the bad case: a silent loss that the
gate is blind to.

**Not fixed here — it is a method decision, not a bug fix.** Three routes:

1. Drop all-NaN members before calling `processing_ensemble`. Cheapest, fixes
   exactly the `ref_swap` case, and `n_members` already records what happened.
2. Mask per epoch rather than per member. More faithful, but the methodological
   variance is then computed over a member count that varies with epoch.
3. Treat it as upstream: `processing_ensemble` arguably wants `nanmean` when it
   is handed `within_sigma`. That would ride along with the codameter 0.5 pin
   bump, which is already pending for the Weaver band-form change.

Nothing blocks the correlate campaign either way: the correlate stage never
imports codameter (§6, sequencing).
