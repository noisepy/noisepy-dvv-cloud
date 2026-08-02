# 01 — AWS setup

Region: **us-west-2** (Oregon) for everything — unlike QuakeScope (us-east-2), this
pipeline is dominated by S3 reads from `scedc-pds`/`ncedc-pds`, which live in
us-west-2. Co-locating compute with the archives cuts read latency and removes any
cross-region transfer risk. Nothing in this repo hardcodes the region except
`parameters.AWS_REGION`.

1. Sign in, create an access key: **Console → IAM → Users → your user → Security
   credentials → Create access key** (CLI use case).
2. `aws configure` with that key; set default region `us-west-2`.
3. Billing guardrail: **Console → Billing → Budgets → Create budget**, monthly cost
   budget with an alert at your comfort level. Fargate Spot for this workload runs
   roughly $0.01–0.05 per station-day of correlation (verify on the smoke test —
   record the number here).
4. Verify: `aws sts get-caller-identity` returns your account.

Next: [02_container.md](02_container.md)
