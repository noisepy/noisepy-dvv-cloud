# 02 — Container images

There are **two images**, one per stage, because `noisepy-seis` pins `pandas<2` while
`codameter` needs `pandas>=2` — they cannot coexist in one environment. The GitHub
Action ([.github/workflows/docker.yml](../../.github/workflows/docker.yml)) builds
both on every push to `main`:

- `ghcr.io/noisepy/noisepy-dvv-cloud:correlate-latest` and `:correlate-<sha>`
- `ghcr.io/noisepy/noisepy-dvv-cloud:dvv-latest` and `:dvv-<sha>`

**For a campaign, pin the `<stage>-<sha>` tags in the job definition YAMLs** —
`-latest` moves under you mid-campaign.

Local smoke test of the images before trusting them to Batch:

```bash
docker pull ghcr.io/noisepy/noisepy-dvv-cloud:correlate-latest
docker run --rm ghcr.io/noisepy/noisepy-dvv-cloud:correlate-latest correlate --help
docker run --rm ghcr.io/noisepy/noisepy-dvv-cloud:dvv-latest dvv --help
```

Then a real 2-day correlation writing to a local mount:

```bash
mkdir -p /tmp/dvvtest
docker run --rm -v /tmp/dvvtest:/out \
  -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY \
  ghcr.io/noisepy/noisepy-dvv-cloud:correlate-latest \
  correlate --stations CI.LJR. --start 2023.001 --end 2023.003 --output /out/ccf
python -c "import pyarrow.dataset as ds; print(ds.dataset('/tmp/dvvtest/ccf', partitioning='hive').to_table().num_rows)"
```

(Reading `scedc-pds` is anonymous; the AWS env vars are only needed once you write to
your own bucket.)

Next: [03_batch_setup.md](03_batch_setup.md)
