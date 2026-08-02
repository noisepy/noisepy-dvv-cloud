# 05 — Monitoring and teardown

## Jobs

**Console → Batch → Jobs**, filter by queue. Healthy campaign: most jobs SUCCEEDED, a
steady trickle of RUNNING, occasional RETRY from Spot interruptions (that's normal).

## Logs

Each job's detail page links its CloudWatch log stream. Or from the CLI:

```bash
aws logs tail /aws/batch/job --since 1h --filter-pattern ERROR
```

## Data

Track product growth (row counts beat object counts for spotting half-written shards):

```bash
python - <<'EOF'
import duckdb
print(duckdb.sql("""
  SELECT pair, count(*) AS days, count(DISTINCT station) AS stations
  FROM 's3://YOUR_BUCKET/ccf/v1/**/*.parquet' GROUP BY pair
"""))
EOF
```

## Money

**Console → Cost Explorer**, filter service = *Fargate*. Set a daily granularity during
the campaign. If the burn rate surprises you, halve `maxvCpus` on the compute
environment (**Console → Batch → Compute environments → Edit**) — jobs queue up
harmlessly.

## Teardown after the campaign

```bash
aws batch update-job-queue --job-queue dvvcloud2026_queue --state DISABLED
aws batch delete-job-queue --job-queue dvvcloud2026_queue
aws batch update-compute-environment --compute-environment dvvcloud2026_env --state DISABLED
aws batch delete-compute-environment --compute-environment dvvcloud2026_env
```

Job definitions and the S3 products cost (almost) nothing at rest; keep them.

Next: [06_troubleshooting.md](06_troubleshooting.md)
