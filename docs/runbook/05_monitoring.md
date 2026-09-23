# 05 — Monitoring and teardown

## Jobs

**Console → Batch → Jobs**, filter by queue. Healthy campaign: most jobs SUCCEEDED, a
steady trickle of RUNNING, occasional RETRY from Spot interruptions (that's normal).

## Logs

Each job's detail page links its CloudWatch log stream. Or from the CLI:

```bash
pixi run -e ops aws logs tail /aws/batch/job --since 1h --filter-pattern ERROR
```

## Data

Track product growth (row counts beat object counts for spotting half-written shards):

```bash
python - <<'EOF'
import duckdb
print(duckdb.sql("""
  SELECT pair, count(*) AS days, count(DISTINCT station) AS stations
  FROM 's3://denolle-dvv-cloud-2026/ccf/v1/**/*.parquet' GROUP BY pair
"""))
EOF
```

## Dashboard

```bash
export DVV_OUTPUT_BUCKET=denolle-dvv-cloud-2026
pixi run -e map python scripts/station_map.py --stations CI.LJR,CI.RXH,CI.ADO
pixi run -e dvv python scripts/dashboard.py --stations CI.LJR,CI.RXH,CI.ADO
```

One self-contained HTML file: a shaded-relief station map, the daily waveform
gather (lag across, date down), the reference stack with its coda measurement
window, and dv/v per octave band with uncertainty ribbons — the
Clements-Denolle layout. Clicking a station switches every panel. No CDN and no
JS charting library, so it cannot silently fail to load a script; rasters are
PNG data URIs and the charts are inline SVG.

`station_map.py` is a separate step in a separate pixi environment because GMT
is a C library with its own data stack, the same reason `awscli` sits in `ops`.
It writes `map-light.png`, `map-dark.png` and `stations.json` into `reports/`;
the dashboard embeds whichever it finds and renders fine without them.
Coordinates come from the archive's own StationXML, so they cannot drift from
the metadata the correlations were computed against.

Keep the page under **16 MB** if you want to publish it. Three stations x three
cross-components x four bands is about 9 MB. `--all-pairs` adds the
autocorrelations and roughly doubles it.

Read it as QC, not decoration. A gather row that breaks up is a day the
measurement cannot use, and `nwindows` in the readout strip is the trail back to
why. `reports/` is gitignored — the files are megabytes of embedded PNG.

`--fragment` drops the document skeleton for embedding in a host page.

## Money

**Not Cost Explorer.** `ce:GetCostAndUsage`, Budgets, Cost and Usage Reports and
the Free Tier API are all denied on this account by an explicit Deny in SCP
`p-q1ngvul9` — see [00b_cloud_hardening.md](00b_cloud_hardening.md) §1.2. Real
invoiced figures come from the CloudBank portal, recorded by hand.

What you can see from here is runtime, which is the input to a cost estimate:

```bash
pixi run -e ops aws batch list-jobs --job-queue dvvcloud2026_queue \
  --job-status SUCCEEDED --query 'jobSummaryList[].[jobName,startedAt,stoppedAt]' \
  --output text
```

If the burn rate surprises you, halve `maxvCpus` on the compute environment
(**Console → Batch → Compute environments → Edit**) — jobs queue up harmlessly.
The Batch-runtime estimator that turns those timestamps into spend is
[00b_cloud_hardening.md](00b_cloud_hardening.md) §4 item 4, still unported.

## Teardown after the campaign

```bash
aws batch update-job-queue --job-queue dvvcloud2026_queue --state DISABLED
aws batch delete-job-queue --job-queue dvvcloud2026_queue
aws batch update-compute-environment --compute-environment dvvcloud2026_env --state DISABLED
aws batch delete-compute-environment --compute-environment dvvcloud2026_env
```

Job definitions and the S3 products cost (almost) nothing at rest; keep them.

Next: [06_troubleshooting.md](06_troubleshooting.md)
