# 02 — Container image

The GitHub Action ([.github/workflows/docker.yml](../../.github/workflows/docker.yml))
builds and pushes `ghcr.io/noisepy/noisepy-dvv-cloud` on every push to `main`, tagged
with the short commit SHA plus `latest`.

**For a campaign, pin the short-SHA tag in both job definition YAMLs** — `latest`
moves under you mid-campaign.

Local smoke test of the image before trusting it to Batch:

```bash
docker pull ghcr.io/noisepy/noisepy-dvv-cloud:latest
docker run --rm ghcr.io/noisepy/noisepy-dvv-cloud:latest correlate --help
```

Then a real 2-day correlation writing to a local mount:

```bash
mkdir -p /tmp/dvvtest
docker run --rm -v /tmp/dvvtest:/out \
  -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY \
  ghcr.io/noisepy/noisepy-dvv-cloud:latest \
  correlate --stations CI.LJR. --start 2023.001 --end 2023.003 --output /out/ccf
python -c "import pyarrow.dataset as ds; print(ds.dataset('/tmp/dvvtest/ccf', partitioning='hive').to_table().num_rows)"
```

(Reading `scedc-pds` is anonymous; the AWS env vars are only needed once you write to
your own bucket.)

Next: [03_batch_setup.md](03_batch_setup.md)
