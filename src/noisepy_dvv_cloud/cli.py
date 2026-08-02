"""Container entrypoint: python -m noisepy_dvv_cloud <correlate|dvv> ...

Batch job definitions pass argv via Ref:: parameter substitution; the same
entrypoint serves both job definitions (QuakeScope pattern).
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime

from . import constants


def parse_stations(value: str) -> list[str]:
    return [s.strip() for s in value.split(",") if s.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dvv-cloud")
    parser.add_argument("--loglevel", default="INFO")
    sub = parser.add_subparsers(dest="command", required=True)

    p_corr = sub.add_parser("correlate", help="NoisePy single-station correlations -> Parquet")
    p_corr.add_argument("--stations", required=True, help="comma-separated NET.STA.LOC")
    p_corr.add_argument("--start", required=True, help="YYYY.DDD")
    p_corr.add_argument("--end", required=True, help="YYYY.DDD (exclusive)")
    p_corr.add_argument("--output", required=True, help="s3:// or local CCF dataset root")
    p_corr.add_argument("--scratch", default="/tmp/noisepy_scratch",
                        help="scratch for NoisePy numpy stores (container-local)")

    p_dvv = sub.add_parser("dvv", help="codameter dv/v from Parquet CCFs")
    p_dvv.add_argument("--stations", required=True, help="comma-separated NET.STA")
    p_dvv.add_argument("--ccf", required=True, help="CCF dataset root (stage 1 output)")
    p_dvv.add_argument("--output", required=True, help="s3:// or local dv/v root")
    p_dvv.add_argument("--use-case", default=None,
                       help="codameter use case (volcano, groundwater, ...); "
                            "omit for the Clements-Denolle fallback recipe")
    p_dvv.add_argument("--combine", default="hobiger",
                       choices=["hobiger", "inverse_variance"],
                       help="cross-component combiner (compare both on the smoke test)")

    args = parser.parse_args(argv)
    logging.basicConfig(level=args.loglevel,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if args.command == "correlate":
        from . import correlate

        correlate.run(
            parse_stations(args.stations),
            datetime.strptime(args.start, constants.DATE_FMT),
            datetime.strptime(args.end, constants.DATE_FMT),
            args.output,
            args.scratch,
        )
    elif args.command == "dvv":
        from . import dvv

        dvv.run(parse_stations(args.stations), args.ccf, args.output, args.use_case,
                combine_method=args.combine)
    return 0


if __name__ == "__main__":
    sys.exit(main())
