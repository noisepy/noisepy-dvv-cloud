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
## Graviton, measured 2026-09-24

Both architectures were run on Batch against the same station-days, from the
same published image, into separate S3 prefixes, as
`dvvcloud2026_correlate:1` (X86_64) and `dvvcloud2026_correlate_arm64:1`
(ARM64). Eight matched 10-day shards across CI.ADO, CI.RXH and CI.LJR.

**Products are equivalent.** All six pairs on all ten days:

| metric | worst across all pairs |
|---|---|
| max absolute difference | 1.2e-07 |
| relative difference | 1.3e-06 (~10 float32 ULPs) |
| **1 - CC, per day** | **8.7e-13** |

The last row is the one that matters: stretching compares waveform shape, so a
1-CC of 8.7e-13 is about 1e-9 of the dv/v measurement floor (~1e-3). The
estimator cannot see it. Not bit-identical, and it never will be -- different
FFT kernels on different architectures.

**Throughput is the same, within noise.** This is the part worth reading
carefully, because the first shard alone suggested ARM was 18% faster and that
was noise:

| shard | x86 s | arm64 s | ratio |
|---|---|---|---|
| ADO 2023.001 | 94.0 | 77.1 | 0.820 |
| RXH 2023.032 | 75.1 | 76.1 | 1.013 |
| LJR 2023.060 | 76.5 | 87.5 | 1.143 |
| ADO 2023.100 | 77.2 | 92.8 | 1.203 |
| RXH 2023.140 | 95.4 | 99.6 | 1.044 |
| LJR 2023.180 | 71.2 | 72.4 | 1.017 |
| ADO 2023.220 | 79.8 | 92.5 | 1.160 |
| RXH 2023.260 | 81.6 | 74.6 | 0.913 |

Mean ratio **1.039 +/- 0.046** (SE, n=8), 95% interval **0.950-1.129**. Parity
sits inside the interval, so there is **no measurable throughput difference**;
the point estimate is 4% slower on ARM. One shard would have supported the
opposite conclusion, which is why there are eight.

**Cost: 17% cheaper per station-day**, and essentially all of it is the price
list rather than performance.

| | $/station-day |
|---|---|
| x86, these 8 shards | 1.03e-04 |
| arm64, these 8 shards | 8.52e-05 |
| *campaign baseline, 75 jobs, 30-day shards* | *9.00e-05* |

The probe's x86 figure is above the campaign baseline because these are 10-day
shards: the ~21.6 s fixed cost per job amortises over a third as many days.
Compare the two probe rows to each other, not either one to the baseline.

AWS's 20% ARM discount, minus the 4% slower point estimate, gives the 17%.

**Batch is still X86_64.** `dvvcloud2026_correlate_arm64:1` exists as a
separate job definition name, so switching is a one-line change to
`submit_helper` or a `--job-definition` override, and reverting is the same.
Worth taking for a long campaign -- 17% of the 25-year California figure is
about $50 -- and not worth any risk for a smoke test.

- On an Apple Silicon laptop, `--platform linux/amd64` builds the x86 image
  under emulation; arm64 now builds natively.
