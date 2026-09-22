"""Submit correlation / dv/v shards to AWS Batch (QuakeScope pattern).

Sharding: stations x date ranges. Single-station work is embarrassingly
parallel per station, so unlike cross-station correlation there is no pair
bookkeeping — a shard is simply (station block, date block). Stations are
grouped by archive (scedc/ncedc) first so each shard reads one archive.

Every submission writes an audit CSV to submissions/. Re-running the identical
command after Spot interruptions is safe: Parquet shard files are
content-hash-named and overwrite themselves.
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timedelta
from pathlib import Path

import boto3
from botocore.config import Config

from . import parameters
from .constants import DATE_FMT, NETWORK_MAPPING


def read_station_file(path: str) -> list[str]:
    """One NET.STA.LOC per line, '#' comments (QuakeScope format)."""
    out = []
    for line in Path(path).read_text().splitlines():
        line = line.split("#")[0].strip()
        if line:
            out.append(line)
    return out


def shard(stations: list[str], start: datetime, end: datetime,
          station_group_size: int, day_group_size: int):
    """Yield (station_block, day0, day1) shards, grouped by archive."""
    by_archive: dict[str, list[str]] = {}
    for s in stations:
        net = s.split(".")[0]
        by_archive.setdefault(NETWORK_MAPPING[net], []).append(s)

    for archive, sta in sorted(by_archive.items()):
        for i in range(0, len(sta), station_group_size):
            block = sta[i : i + station_group_size]
            day = start
            while day < end:
                day1 = min(day + timedelta(days=day_group_size), end)
                yield block, day, day1
                day = day1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dvv-cloud-submit")
    parser.add_argument("stage", choices=["correlate", "dvv"])
    parser.add_argument("--station_file", required=True)
    parser.add_argument("--start", required=True, help="YYYY.DDD")
    parser.add_argument("--end", required=True, help="YYYY.DDD")
    parser.add_argument("--station_group_size", type=int, default=4)
    parser.add_argument("--day_group_size", type=int, default=30)
    parser.add_argument("--region", default=parameters.AWS_REGION)
    parser.add_argument("--dry_run", action="store_true")
    args = parser.parse_args(argv)

    stations = read_station_file(args.station_file)
    start = datetime.strptime(args.start, DATE_FMT)
    end = datetime.strptime(args.end, DATE_FMT)

    bucket = parameters.OUTPUT_BUCKET
    if not bucket:
        raise SystemExit(
            "no output bucket: export DVV_OUTPUT_BUCKET=<name> "
            "(scripts/create_bucket.py --apply creates it; runbook 03)"
        )
    ccf_root = f"s3://{bucket}/{parameters.CCF_PREFIX}"
    dvv_root = f"s3://{bucket}/{parameters.DVV_PREFIX}"

    client = boto3.client("batch", config=Config(region_name=args.region))
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    audit_rows = []

    if args.stage == "correlate":
        shards = list(shard(stations, start, end,
                            args.station_group_size, args.day_group_size))
        job_def = parameters.JOB_DEFINITION_CORRELATE
        for k, (block, d0, d1) in enumerate(shards):
            params = {
                "stations": ",".join(block),
                "start": d0.strftime(DATE_FMT),
                "end": d1.strftime(DATE_FMT),
                "output": ccf_root,
            }
            _submit(client, f"correlate_{ts}_{k}", job_def, params,
                    args.dry_run, audit_rows)
    else:
        # dv/v: one job per station block over the whole period (needs the
        # full history in one process for stacking/reference)
        job_def = parameters.JOB_DEFINITION_DVV
        for k in range(0, len(stations), args.station_group_size):
            block = stations[k : k + args.station_group_size]
            params = {"stations": ",".join(block), "ccf": ccf_root, "output": dvv_root}
            _submit(client, f"dvv_{ts}_{k}", job_def, params, args.dry_run, audit_rows)

    audit = Path("submissions") / f"{args.stage}_{ts}.csv"
    audit.parent.mkdir(exist_ok=True)
    with audit.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["jobName", "jobId", "parameters"])
        w.writeheader()
        w.writerows(audit_rows)
    print(f"{len(audit_rows)} jobs -> {audit}")
    return 0


def _submit(client, name, job_def, params, dry_run, audit_rows):
    if dry_run:
        audit_rows.append({"jobName": name, "jobId": "DRY_RUN", "parameters": params})
        return
    resp = client.submit_job(
        jobName=name,
        jobQueue=parameters.JOB_QUEUE,
        jobDefinition=job_def,
        parameters=params,
    )
    audit_rows.append({"jobName": name, "jobId": resp["jobId"], "parameters": params})


if __name__ == "__main__":
    raise SystemExit(main())
