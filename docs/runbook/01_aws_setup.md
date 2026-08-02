# 01 — AWS setup

Region: **us-east-2** (Ohio) for everything, matching QuakeScope. The public archives
(`scedc-pds`, `ncedc-pds`) are in us-west-2; cross-region reads work and Fargate
capacity in us-east-2 has been reliable, but if egress cost dominates a large campaign,
consider running in us-west-2 instead — nothing in this repo hardcodes the region
except `parameters.AWS_REGION`.

1. Sign in, create an access key: **Console → IAM → Users → your user → Security
   credentials → Create access key** (CLI use case).
2. `aws configure` with that key; set default region `us-east-2`.
3. Billing guardrail: **Console → Billing → Budgets → Create budget**, monthly cost
   budget with an alert at your comfort level. Fargate Spot for this workload runs
   roughly $0.01–0.05 per station-day of correlation (verify on the smoke test —
   record the number here).
4. Verify: `aws sts get-caller-identity` returns your account.

Next: [02_container.md](02_container.md)
