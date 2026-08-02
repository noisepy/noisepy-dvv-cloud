# Seed project: Julia (SeisNoise.jl) vs Python (NoisePy) for cloud correlation

Status: **seed — not started.** This file is the instruction set for whoever picks this
up as a standalone project (side repo, student project, or hackweek). It lives here
because noisepy-dvv-cloud is the Python side of the comparison.

## Why this project exists

Clements & Denolle (2022/2023) computed single-station cross-component correlations for
~700 California stations with a Julia stack: SeisNoise.jl v0.5.2, SeisIO.jl v1.2.1, and
three unregistered personal packages (SeisDvv.jl, SCEDC.jl, SCEDCCorr.jl from
github.com/tclements). As of mid-2026:

- SeisNoise.jl has had no substantive maintenance in ~2 years; the unregistered
  dependencies are effectively frozen and pinned to Julia 1.6-era manifests.
- The 2022 correlation outputs were stored as Julia `Serialization.serialize` blobs on
  S3 (`s3://bh-auto-corr/CORR/<pair-name>`), which are unreadable from any other
  language and even from future Julia versions if struct layouts change. This is the
  single biggest reproducibility liability of that project.
- NoisePy is actively maintained, has native S3 stores, and an AWS Batch scheduler.

So noisepy-dvv-cloud rebuilds the pipeline in Python. **But**: SeisNoise.jl's FFT
kernels were fast, and nobody has measured the actual cost difference at scale. Julia
may still win on raw compute; if the gap is large, a maintained Julia rewrite could be
worth it. This benchmark settles that with numbers instead of folklore.

## The question

For the fixed scientific task below, what is the wall-clock time, peak memory, and
AWS dollar cost per station-year of (a) the Julia stack and (b) the NoisePy stack,
and do they produce the same correlation functions?

## Fixed task definition (do not vary this)

Reproduce the Clements-Denolle single-station recipe on **10 CI stations × 1 year**
(2019, includes Ridgecrest) reading directly from `s3://scedc-pds`:

- fs = 40 Hz, cc_len = 1800 s, step = 450 s, maxlag = 32 s
- broadband pre-filter 0.5–19 Hz, instrument response removed (responsefreq 0.4 Hz)
- component pairs EN, EZ, NZ; daily linear stacks
- output: daily CCFs in the Parquet schema of this repo (`docs/parquet-schemas.md`) —
  port the writer to Julia (Parquet2.jl) so outputs are directly diffable.

## Protocol

1. **Correctness first.** Run both stacks on ONE station-month. Align conventions
   (normalization, whitening, taper) until daily CCFs match with waveform correlation
   > 0.99 in 0.5–16 Hz. Document every convention difference found — this list is a
   publishable appendix on its own.
2. **Single-node benchmark.** Same EC2 instance type (e.g. c7i.4xlarge), same S3
   region, 10 stations × 12 months. Record wall time, peak RSS, bytes read.
   Three repeats; report medians. Separate I/O time from compute time (both stacks
   stream from S3; the download share may dominate and equalize everything).
3. **Fargate benchmark.** Containerize both, run the same shards on the Batch
   FARGATE_SPOT queue from this repo, compare cost per station-year from the Cost
   Explorer numbers, including Spot-interruption retry overhead.
4. **Verdict.** A factor < 2 in cost: Python wins on maintenance grounds, close the
   question. A factor > 5: write it up and reconsider the kernel language (e.g. Julia
   kernel behind a Python orchestration layer, or a Rust/C kernel for NoisePy).

## Starting points

- Julia reference implementation: `Clements-Denolle-2022/src/01-stream-auto-corr.jl`
  (Dropbox: `RESEARCH_GROUP/TIM_MARINE_PROJEcTS/Clements-Denolle-2022`), pinned
  Manifest.toml in the same repo. Budget real time for resurrecting the three
  unregistered packages on a modern Julia — that pain is itself a data point.
- Python implementation: this repo's `correlate` stage.
- Prior art on the recipe: Clements & Denolle (2023, JGR), doi:10.1029/2022JB025553.

## Deliverables

- A short report (or Seismica Fast Report) with the cost/correctness tables.
- The convention-difference appendix from step 1.
- A recommendation in this repo: either "stay pure Python" or a concrete plan for a
  mixed-language kernel.
