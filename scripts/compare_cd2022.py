"""Pre-launch validation: new dv/v Parquet vs archived Clements-Denolle-2022 Arrow.

The 2022 dv/v products are Arrow IPC files readable directly from Python — no
Julia needed. Local copies live in the Dropbox project under
Clements-Denolle-2022/data/DVV-90-DAY-COMP/<band>/<NET.STA>.arrow with columns
DATE, DVV (percent), CC.

Usage:
  python scripts/compare_cd2022.py \
      --new s3://BUCKET/dvv/v1/band=2.0-4.0/CI.LJR.parquet \
      --legacy ~/Dropbox/RESEARCH_GROUP/TIM_MARINE_PROJEcTS/Clements-Denolle-2022/data/DVV-90-DAY-COMP/2.0-4.0/CI.LJR.arrow

Gate (runbook 04): correlation > 0.9 and |mean offset| < 0.05 %% on at least
3 stations before submitting a full campaign. Differences are expected —
NoisePy vs SeisNoise conventions, ensemble mean vs single config — but they
must be small and explainable, not structural.
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
    args = ap.parse_args()

    new = pq.read_table(args.new).to_pandas()[["date", "dvv", "dvv_err", "cc"]]
    old = read_legacy_arrow(args.legacy)
    both = pd.merge(new, old, on="date", suffixes=("_new", "_2022")).dropna(
        subset=["dvv_new", "dvv_2022"]
    )
    if len(both) < 30:
        print(f"only {len(both)} overlapping days — not enough to judge")
        return 1

    r = np.corrcoef(both["dvv_new"], both["dvv_2022"])[0, 1]
    offset = (both["dvv_new"] - both["dvv_2022"]).mean()
    rms = np.sqrt(((both["dvv_new"] - both["dvv_2022"]) ** 2).mean())
    within_err = (
        np.abs(both["dvv_new"] - both["dvv_2022"]) < 2 * both["dvv_err"]
    ).mean()

    print(f"overlap:            {len(both)} days")
    print(f"correlation:        {r:.3f}   (gate: > 0.9)")
    print(f"mean offset:        {offset:+.4f} %  (gate: |x| < 0.05)")
    print(f"rms difference:     {rms:.4f} %")
    print(f"within 2*dvv_err:   {100 * within_err:.0f} %  (sanity check on error bars)")
    return 0 if (r > 0.9 and abs(offset) < 0.05) else 1


if __name__ == "__main__":
    raise SystemExit(main())
