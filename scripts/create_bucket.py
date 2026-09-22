#!/usr/bin/env python
"""Create the products bucket with its protections already on, and check them.

Versioning only protects objects written *after* it is enabled, so this has to
happen before the first correlate job, not after the campaign has products
worth protecting. That is the whole reason it is a script rather than a line in
the runbook saying `aws s3 mb`.

    python scripts/create_bucket.py --check     # report, change nothing
    python scripts/create_bucket.py --apply     # create if absent, then set

`--apply` is idempotent and safe on an existing bucket: it sets properties and
never writes, deletes or lists objects.

What it sets, and why, is documented in `noisepy_dvv_cloud.bucket` -- the same
module `scripts/preflight.py` reads, so the check cannot drift from the thing
being checked.

Exit 0 only if nothing is FAIL.
"""

from __future__ import annotations

import argparse
import sys

ICON = {"PASS": "ok  ", "FAIL": "FAIL", "WARN": "warn"}


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="report only")
    group.add_argument("--apply", action="store_true", help="create and configure")
    ap.add_argument("--bucket", default=None,
                    help="products bucket (default: $DVV_OUTPUT_BUCKET)")
    args = ap.parse_args(argv)

    from noisepy_dvv_cloud import bucket as spec
    from noisepy_dvv_cloud import parameters

    name = args.bucket or parameters.OUTPUT_BUCKET
    if not name:
        print("no bucket: pass --bucket or set DVV_OUTPUT_BUCKET", file=sys.stderr)
        return 2

    import boto3

    s3 = boto3.client("s3", region_name=parameters.AWS_REGION)

    if args.apply:
        for line in spec.apply(s3, name, parameters.AWS_REGION):
            print(f"  {line}")
        print()

    rows = spec.audit(s3, name, parameters.AWS_REGION)
    for check, state, detail in rows:
        print(f"  [{ICON[state]}] {check}")
        if detail:
            print(f"           {detail}")

    failed = [r for r in rows if r[1] == "FAIL"]
    print("\n  " + ("protected" if not failed
                    else f"{len(failed)} problem(s) -- run --apply"))
    if not failed:
        print(f"\n  export DVV_OUTPUT_BUCKET={name}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
