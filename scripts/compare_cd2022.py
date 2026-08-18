"""Pre-launch validation: new dv/v Parquet vs archived Clements-Denolle-2022 Arrow.

The 2022 dv/v products are Arrow IPC files readable directly from Python — no
Julia needed. Local copies live in the Dropbox project under
data/DVV-90-DAY-COMP/<band>/<NET.STA>.arrow with columns DATE, DVV (percent),
CC. Only band 2.0-4.0 was archived.

Usage:
  python scripts/compare_cd2022.py \
      --new s3://BUCKET/dvv/v1/band=2.0-4.0/CI.LJR.parquet \
      --legacy ~/Dropbox/RESEARCH_GROUP/TIM_MARINE_PROJEcTS/data/DVV-90-DAY-COMP/2.0-4.0/CI.LJR.arrow

Gate (runbook 04): correlation > 0.9 on at least 3 stations before submitting
a full campaign. The mean offset is reported but NOT gated — the two products
use different reference epochs, so a constant offset is bookkeeping rather
than error. Differences are expected — NoisePy vs SeisNoise conventions,
ensemble mean vs single config — but they must be small and explainable, not
structural.
"""

from __future__ import annotations

import argparse

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq


def read_legacy_arrow(path: str) -> pd.DataFrame:
    """Julia Arrow.write files are Arrow IPC; try file then stream format."""
    try:
        with pa.OSFile(path, "rb") as f:
            table = pa.ipc.open_file(f).read_all()
    except pa.ArrowInvalid:
        with pa.OSFile(path, "rb") as f:
            table = pa.ipc.open_stream(f).read_all()
    df = table.to_pandas()
    df.columns = [c.lower() for c in df.columns]
    return df[["date", "dvv", "cc"]]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--new", required=True, help="new dv/v parquet (s3:// or local)")
    ap.add_argument("--legacy", required=True, help="2022 .arrow file")
    ap.add_argument("--smooth-days", type=int, default=90,
                    help="TRAILING rolling mean on the new daily series. The "
                    "CD2022 90-DAY-COMP is a trailing stack (lag-scan "
                    "verified 2026-08-09: legacy lags a centered-smoothed "
                    "series by ~45 days; trailing-90d at zero lag gives "
                    "LJR r=0.985, ARV 0.922). 0 = off")
    ap.add_argument("--burn-in-days", type=int, default=150,
                    help="drop this many days from the start of the new "
                    "series: the fixed reference is immature early in the "
                    "campaign window and biases the comparison")
    ap.add_argument("--demean", action=argparse.BooleanOptionalAction, default=True,
                    help="compare demeaned series: the two products use "
                    "different reference epochs, so a constant offset is a "
                    "reference artifact, not an error (reported separately)")
    ap.add_argument("--min-points", type=int, default=30,
                    help="minimum comparable points AFTER smoothing and "
                    "burn-in; below this the gate is not decidable")
    args = ap.parse_args()

    new = pq.read_table(args.new).to_pandas()[["date", "dvv", "dvv_err", "cc"]]
    old = read_legacy_arrow(args.legacy)
    for df in (new, old):
        df["date"] = pd.to_datetime(df["date"])

    # Smooth on a complete daily calendar grid, BEFORE merging and before the
    # burn-in trim. Rolling over the merged frame counts ROWS, so any missing
    # day silently stretched the "90-day" window past 90 calendar days, and
    # trimming first left the earliest retained points as 40-of-90 partial
    # means being compared against full 90-day legacy stacks.
    new = new.sort_values("date").set_index("date")
    new = new[~new.index.duplicated(keep="first")]
    grid = pd.date_range(new.index.min(), new.index.max(), freq="D")
    new = new.reindex(grid)

    new_s, new_e = new["dvv"], new["dvv_err"]
    if args.smooth_days:
        # trailing, matching the legacy product's construction — centered
        # smoothing leaves a ~45-day phase shift that depresses r. On the
        # daily grid the window is now 90 calendar days by construction;
        # min_periods keeps the old 40-of-90 coverage requirement.
        min_periods = max(2, round(args.smooth_days * 40 / 90))
        roll = {"window": args.smooth_days, "center": False, "min_periods": min_periods}
        new_s = new_s.rolling(**roll).mean()
        # The error bar of a 90-day mean of positively correlated daily
        # estimates: averaging the daily sigmas is the fully-correlated
        # (conservative) bound. Using the raw daily sigma here made the
        # check compare a 90-day mean against a one-day error bar.
        new_e = new_e.rolling(**roll).mean()

    # burn-in measured from the first real observation, not the grid start
    if args.burn_in_days:
        t0 = new["dvv"].first_valid_index()
        if t0 is not None:
            keep = new_s.index >= t0 + pd.Timedelta(days=args.burn_in_days)
            new_s, new_e = new_s[keep], new_e[keep]

    both = pd.merge(
        pd.DataFrame({"date": new_s.index, "dvv_new": new_s.values, "err_new": new_e.values}),
        old.rename(columns={"dvv": "dvv_2022"}),
        on="date",
    ).dropna(subset=["dvv_new", "dvv_2022"])

    # checked AFTER smoothing and trimming: the old placement let a 200-day
    # overlap clear the guard and then reach the gate on ~11 points
    if len(both) < args.min_points:
        print(f"only {len(both)} comparable points after smoothing and "
              f"burn-in — not enough to judge (need {args.min_points})")
        return 1

    a, b = both["dvv_new"], both["dvv_2022"]
    offset = (a - b).mean()
    if args.demean:
        a, b = a - a.mean(), b - b.mean()
    r = np.corrcoef(a, b)[0, 1]
    rms = np.sqrt(((a - b) ** 2).mean())
    within_err = (np.abs(a - b) < 2 * both["err_new"]).mean()

    print(f"comparable points:  {len(both)} (after {args.smooth_days}d smoothing, "
          f"{args.burn_in_days}d burn-in)")
    print(f"correlation:        {r:.3f}   (gate: > 0.9)")
    print(f"reference offset:   {offset:+.4f} %  (informational — different "
          "reference epochs)")
    print(f"rms difference:     {rms:.4f} %")
    print(f"within 2*dvv_err:   {100 * within_err:.0f} %  (sanity check on error bars)")
    # gate: correlation on like-for-like (smoothed, demeaned) series; the
    # offset is reference-epoch bookkeeping and no longer gated
    return 0 if r > 0.9 else 1


if __name__ == "__main__":
    raise SystemExit(main())
