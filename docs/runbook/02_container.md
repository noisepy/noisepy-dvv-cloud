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

## Measured

Dated per line — the first figures came from the local build of 2026-08-08,
the rest from the move to pixi-built multi-arch images on 2026-09-24.

- correlate image: **1.1 GB** (linux/amd64), *2026-08-08, pip build*.
- *2026-09-24:* **images are now built from the pixi lock, for amd64 and arm64.** The note
  below is kept because it is the reason. Measured locally: `correlate-image`
  (the same features without `dev`) is 857 MB against 976 MB for the dev
  environment, so keeping pytest/ruff/pre-commit out of the container saves
  ~120 MB. Total size still lands near the old pip image's 1.1 GB — this change
  buys the architecture, not the size.
- The previous images were **x86-only**, and the reason is narrower than it
  first looked.
  obspy ships no linux/aarch64 **wheels on PyPI** — still true through 1.5.1,
  re-checked 2026-09-24 — so an arm64 build on `python:3.10-slim` tries to
  compile obspy/numcodecs/psutil from source and fails (no gcc).
  **conda-forge does ship obspy for linux-aarch64** (1.4.2 through 1.5.1), so
  Graviton is blocked by this image being pip-based, not by obspy itself, and
  not by the seisfetch migration. Building from the pixi lock unblocks arm64
  without removing obspy, which is what `docker/Dockerfile.*` now does.

  Two follow-ups before Batch can use it, in order: an arm64 manifest has to be
  published from `main` (the workflow now builds both arches on every PR and
  pushes both from `main`), then both job definitions need
  `runtimePlatform.cpuArchitecture: ARM64` and a smoke shard confirming the
  products are unchanged. **Neither is done, so Batch is still X86_64.** AWS
  lists Fargate ARM at 20% below x86 for the same shape — a list price, not a
  measurement; nothing here has been measured on Graviton.
- On an Apple Silicon laptop, `--platform linux/amd64` builds the x86 image
  under emulation; arm64 now builds natively.
