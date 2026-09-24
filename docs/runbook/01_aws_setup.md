# 01 — AWS setup

Region: **us-west-2** (Oregon) for everything — unlike QuakeScope (us-east-2), this
pipeline is dominated by S3 reads from `scedc-pds`/`ncedc-pds`, which live in
us-west-2. Co-locating compute with the archives cuts read latency and removes any
cross-region transfer risk. Nothing in this repo hardcodes the region except
`parameters.AWS_REGION`.

1. Sign in, create an access key: **Console → IAM → Users → your user → Security
   credentials → Create access key** (CLI use case).
2. `aws configure` with that key; set default region `us-west-2`.
3. Billing guardrail: **NOT POSSIBLE ON THIS ACCOUNT.** `budgets:ViewBudget` is
   explicitly denied by service control policy `p-q1ngvul9` (verified 2026-09-10),
   as is Cost Explorer — this is a CloudBank account billed through Strategic
   Blue. There is currently no billing guardrail; see
   [00b_cloud_hardening.md](00b_cloud_hardening.md) §1.2 for the replacement.
   The original instruction, which fails: **Console → Billing → Budgets → Create budget**,
   monthly cost budget with an alert at your comfort level.

   **Measured rate, 2026-09-23:** `$9.0e-05` per station-day of correlation on
   Fargate Spot at 2 vCPU / 16 GB — 75 jobs covering 3 stations x 2 years, from
   Batch `startedAt`/`stoppedAt` with the 1-minute minimum applied, priced at
   the Fargate Spot list rate. That is **$0.20 for the whole 3-station, 2-year
   campaign**. Derivation and what it is sensitive to:
   [docs/cost-model.md](../cost-model.md).

   Two earlier figures in this file were wrong by orders of magnitude in
   opposite directions — `$0.01–0.05` (a pre-model guess) and `$0.0002–0.001`
   (the model's own conservative prose). Both are replaced by the measurement.
4. Verify: `aws sts get-caller-identity` returns your account.

Next: [02_container.md](02_container.md)
