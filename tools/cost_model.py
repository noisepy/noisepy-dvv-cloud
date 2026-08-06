"""Cloud cost model: how small can the bill go for science-scale jobs?

Three deployment scenarios for the obspy-free seisfetch+NoisePy chain:

  S1  Lambda daily dv/v service (EventBridge cron, reference stack on S3)
  S2a Fargate Spot 25-year backfill, single-station dv/v (no response removal)
  S2b Fargate Spot moving-subarray cross-correlation (150 km radius tiles,
      30 km step, all pairs, multichannel CCF stacks sorted by distance)

Every number is a parameter with provenance: measured on real data (seisfetch
benchmarks, 2026-08-06), taken from an AWS price page (dated), or derived from
the real station inventory (QuakeScope networks/*.zip). Regenerate the report:

    python tools/cost_model.py [--networks ~/GitHub/QuakeScope/networks]

writes docs/cost-model.md and docs/cost-model-figs/*.png deterministically
(no timestamps in the body; the as-of dates are explicit parameters).
"""

from __future__ import annotations

import argparse
import io
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent
DOC = REPO / "docs" / "cost-model.md"
FIG_DIR = REPO / "docs" / "cost-model-figs"

# --------------------------------------------------------------------------- #
#  Prices (all overridable; provenance in comments)
# --------------------------------------------------------------------------- #


@dataclass
class Prices:
    asof: str = "2026-08-06"
    # Fargate Linux/x86, US East (Ohio) / US West (Oregon) list rates
    # (aws.amazon.com/fargate/pricing; cross-checked against the QuakeScope
    # runbook anchor: 8 vCPU + 16 GB = $0.395/h on-demand, ~$0.12/h Spot)
    fargate_od_vcpu_h: float = 0.04048
    fargate_od_gb_h: float = 0.004445
    spot_discount: float = 0.70  # Fargate Spot ~70% off; floats with demand
    # Lambda (aws.amazon.com/lambda/pricing, fetched 2026-08-06)
    lambda_gb_s: float = 0.0000166667
    lambda_per_million_req: float = 0.20
    # S3 standard (stable list rates)
    s3_storage_gb_mo: float = 0.023
    s3_get_per_1k: float = 0.0004
    s3_put_per_1k: float = 0.005
    # Reading the open-data buckets in-region is free; cross-region transfer
    # is $0.02/GB and is treated as a design error, not a line item.

    @property
    def spot_vcpu_h(self) -> float:
        return self.fargate_od_vcpu_h * (1 - self.spot_discount)

    @property
    def spot_gb_h(self) -> float:
        return self.fargate_od_gb_h * (1 - self.spot_discount)

    def fargate_task_h(self, vcpu: float, gb: float, spot: bool = True) -> float:
        if spot:
            return vcpu * self.spot_vcpu_h + gb * self.spot_gb_h
        return vcpu * self.fargate_od_vcpu_h + gb * self.fargate_od_gb_h


# --------------------------------------------------------------------------- #
#  Timings (measured anchors; single vCPU unless noted)
# --------------------------------------------------------------------------- #


@dataclass
class Timings:
    """Seconds per unit of work, single core.

    Anchors measured 2026-08-06 on the seisfetch three-archive validation
    (M1, npy93 env, n=124 station-days): parse + response removal +
    preprocess + FFT at 20 sps target from 40 sps BHZ = 2.1 s median.
    Container (Fargate-class cgroup) penalty 1.0-1.3x with the seisfetch
    recordlist parse path (benchmarks/RESULTS.md); we carry 1.3x.
    """

    asof: str = "2026-08-06"
    # per station-day-channel, 40 sps input, 20 sps target
    parse_s: float = 0.02  # seisfetch recordlist path, flat across envs
    response_s: float = 0.45  # remove_response_np, day trace rfft ~4M
    preprocess_s: float = 1.15  # demean/detrend/taper/bandpass at native fs
    fft_s: float = 0.45  # windowing + rfft at target sps (scales ~ sps)
    # per pair-day, substack=False: measured 2026-08-06 through noisepy's
    # own correlate (188 windows of 1800 s, 20 sps, window selection + one
    # irfft): 60.6 ms/pair-day. Scales ~linearly with target sps.
    correlate_pair_s: float = 0.0606  # at 20 sps
    # codameter stretching (measured 2026-08-06, codavenv, 2561-sample CCFs)
    codameter_fixed_ms_day_cfg: float = 0.18
    codameter_moving_ms_day_cfg: float = 5.7
    dvv_configs: int = 60  # 5 members x 3 pairs x 4 bands (dvv.py:59-61)
    container_penalty: float = 1.3
    hh_factor: float = 2.5  # 100 sps native vs 40 sps preprocessing cost

    def station_day_channel_s(
        self, response: bool, target_sps: float = 20.0, hh: bool = False
    ) -> float:
        t = self.parse_s + self.preprocess_s + self.fft_s * (target_sps / 20.0)
        if response:
            t += self.response_s
        if hh:
            t = self.parse_s + (t - self.parse_s) * self.hh_factor
        return t * self.container_penalty

    def pair_day_s(self, target_sps: float = 20.0) -> float:
        return self.correlate_pair_s * (target_sps / 20.0) * self.container_penalty


# --------------------------------------------------------------------------- #
#  Station geometry from the real inventory (QuakeScope networks/*.zip)
# --------------------------------------------------------------------------- #

NCEDC_NETS = {"BG", "BK", "BP", "NC", "PG", "UL", "WR"}
SCEDC_NETS = {"CI"}


def _archive(net: str) -> str:
    if net in SCEDC_NETS:
        return "scedc"
    if net in NCEDC_NETS:
        return "ncedc"
    return "earthscope"


def load_stations(networks_dir: Path, start: float, end: float) -> pd.DataFrame:
    """One row per unique net.sta with a broadband channel (BH*/HH*) whose
    operating window overlaps [start, end) (year.doy floats as in the CSVs)."""
    frames = []
    for z in sorted(networks_dir.glob("*.zip")):
        try:
            with zipfile.ZipFile(z) as zf:
                name = zf.namelist()[0]
                df = pd.read_csv(io.BytesIO(zf.read(name)))
        except Exception:
            continue
        frames.append(df)
    allst = pd.concat(frames, ignore_index=True)
    ch = allst["channels"].fillna("")
    bb = ch.str.contains("BH") | ch.str.contains("HH")
    live = (allst["start_date"] < end) & (allst["end_date"] > start)
    df = allst[bb & live].copy()
    df["hh_only"] = ~ch[bb & live].str.contains("BH")
    df["key"] = df["network_code"].astype(str) + "." + df["station_code"].astype(str)
    df = (
        df.sort_values("hh_only")  # prefer the BH-bearing entry per station
        .drop_duplicates("key")
        .reset_index(drop=True)
    )
    df["archive"] = df["network_code"].astype(str).map(_archive)
    # active days inside the window
    span_start = np.maximum(df["start_date"], start)
    span_end = np.minimum(df["end_date"], end)
    df["active_days"] = np.maximum(
        0.0, (span_end - span_start) * 365.25
    )  # year.doy floats: fractional years x 365
    return df[
        ["key", "network_code", "latitude", "longitude", "archive", "hh_only",
         "active_days", "start_date", "end_date"]
    ]


def _ecef(lat, lon):
    la, lo = np.radians(lat), np.radians(lon)
    return np.column_stack(
        [np.cos(la) * np.cos(lo), np.cos(la) * np.sin(lo), np.sin(la)]
    ) * 6371.0


def neighbor_stats(df: pd.DataFrame, d_max_km: float):
    """Pair count and per-station neighbor counts within d_max_km (chord
    distance on the sphere; exact enough at <=300 km)."""
    from scipy.spatial import cKDTree

    xyz = _ecef(df["latitude"].values, df["longitude"].values)
    tree = cKDTree(xyz)
    pairs = tree.query_pairs(r=d_max_km, output_type="ndarray")
    counts = np.zeros(len(df), dtype=int)
    for a, b in pairs:
        counts[a] += 1
        counts[b] += 1
    return pairs, counts


def pair_overlap_days(df: pd.DataFrame, pairs, start: float, end: float):
    """Joint active days for each pair inside [start, end): a pair only
    produces CCFs on days both stations were recording."""
    s0 = np.maximum(df["start_date"].values, start)
    e0 = np.minimum(df["end_date"].values, end)
    lo = np.maximum(s0[pairs[:, 0]], s0[pairs[:, 1]])
    hi = np.minimum(e0[pairs[:, 0]], e0[pairs[:, 1]])
    return np.maximum(0.0, (hi - lo)) * 365.25


def tile_census(df: pd.DataFrame, radius_km: float, step_km: float, pairs):
    """Tile centers on a step_km lat/lon grid (cos-lat corrected), keeping
    tiles with >=2 stations within radius_km. Pairs are assigned to the tile
    nearest their midpoint (each pair computed exactly once)."""
    from scipy.spatial import cKDTree

    step_deg = step_km / 111.32
    lat = df["latitude"].values
    lon = df["longitude"].values
    glat = np.round(lat / step_deg) * step_deg
    # longitude step widens with latitude so tiles stay ~step_km wide
    coslat = np.clip(np.cos(np.radians(glat)), 0.2, None)
    glon = np.round(lon / (step_deg / coslat)) * (step_deg / coslat)
    centers = np.unique(np.column_stack([glat, glon]), axis=0)
    # keep tiles with >=2 stations in radius
    st_xyz = _ecef(lat, lon)
    tree = cKDTree(st_xyz)
    c_xyz = _ecef(centers[:, 0], centers[:, 1])
    n_in = np.array([len(tree.query_ball_point(c, r=radius_km)) for c in c_xyz])
    keep = n_in >= 2
    centers, c_xyz, n_in = centers[keep], c_xyz[keep], n_in[keep]
    # midpoint assignment
    mid = 0.5 * (st_xyz[pairs[:, 0]] + st_xyz[pairs[:, 1]])
    mid /= np.linalg.norm(mid, axis=1, keepdims=True) / 6371.0
    ctree = cKDTree(c_xyz)
    _, owner = ctree.query(mid)
    pairs_per_tile = np.bincount(owner, minlength=len(centers))
    # stations a tile job must FFT under midpoint assignment: stations of its
    # owned pairs
    st_sets = [set() for _ in range(len(centers))]
    for (a, b), o in zip(pairs, owner):
        st_sets[o].add(a)
        st_sets[o].add(b)
    st_per_tile = np.array([len(s) for s in st_sets])
    # naive-architecture duplication: each station FFT'd once per tile whose
    # radius covers it
    dup = n_in.sum() / max(len(df), 1)
    # ... and each PAIR correlated once per tile covering BOTH stations
    # (sampled; this is the dominant naive waste)
    cover = ctree.query_ball_point(st_xyz, r=radius_km)
    cover = [set(c) for c in cover]
    rng = np.random.default_rng(0)
    sample = pairs[rng.choice(len(pairs), size=min(20000, len(pairs)),
                              replace=False)]
    dup_corr = float(
        np.mean([len(cover[a] & cover[b]) for a, b in sample])
    )
    return {
        "naive_corr_dup_factor": max(dup_corr, 1.0),
        "n_tiles": len(centers),
        "stations_in_radius": n_in,
        "pairs_per_tile": pairs_per_tile,
        "stations_per_tile_job": st_per_tile,
        "naive_dup_factor": dup,
    }


# --------------------------------------------------------------------------- #
#  Scenarios
# --------------------------------------------------------------------------- #


def scenario_lambda_daily(p: Prices, t: Timings, n_stations_list=(100, 700, 3000)):
    """S1: daily per-station dv/v update. One Lambda invocation per station-day:
    fetch day file + StationXML-cached response, seisfetch chain (with
    response removal so amplitudes are physical), 6 cross-component pair-days,
    append CCF rows, run the 60-config codameter ensemble incrementally
    (fixed-ref update on the new day only), write dv/v rows."""
    # single-invocation work (seconds, 1 vCPU-equivalent = 1769 MB Lambda)
    chan_s = 3 * t.station_day_channel_s(response=True)
    corr_s = 6 * t.pair_day_s()
    dvv_s = t.dvv_configs * t.codameter_fixed_ms_day_cfg / 1000 * 30  # 30-day
    # trailing re-measure so gating/moving stacks stay consistent
    io_s = 4.0  # S3 day-file GET + parquet read/append (measured fetches ~2-7 s)
    total_s = chan_s + corr_s + dvv_s + io_s
    mem_gb = 1.769  # 1 vCPU-equivalent; chain fits in <400 MB RSS
    rows = []
    for n in n_stations_list:
        invocations_mo = n * 30.4
        compute = invocations_mo * total_s * mem_gb * p.lambda_gb_s
        requests = invocations_mo / 1e6 * p.lambda_per_million_req
        s3 = invocations_mo * (3 * p.s3_get_per_1k + 2 * p.s3_put_per_1k) / 1000
        storage = n * 61e-6 * 365 * p.s3_storage_gb_mo  # CCF ~61 kB/station-day
        total = compute + requests + s3 + storage
        rows.append(
            {
                "stations": n,
                "sec/invocation": round(total_s, 1),
                "$ compute/mo": round(compute, 2),
                "$ requests/mo": round(requests, 4),
                "$ S3 req/mo": round(s3, 2),
                "$ storage/mo (yr1)": round(storage, 2),
                "$ total/mo": round(total, 2),
                "$/station-day": round(total / (n * 30.4), 5),
            }
        )
    # Fargate alternative: one nightly 2 vCPU Spot task sweeps all stations
    alt = []
    for n in n_stations_list:
        wall_h = n * (chan_s + corr_s + dvv_s + io_s / 4) / 2 / 3600  # 2 vCPU
        cost_mo = wall_h * p.fargate_task_h(2, 4) * 30.4
        alt.append(
            {"stations": n, "nightly wall (h)": round(wall_h, 2),
             "$ total/mo": round(cost_mo, 2),
             "$/station-day": round(cost_mo / (n * 30.4), 5)}
        )
    return pd.DataFrame(rows), pd.DataFrame(alt), total_s


def scenario_fargate_25y(p: Prices, t: Timings, df: pd.DataFrame, years=25):
    """S2a: one Spot container per station, whole-history single-station dv/v,
    no response removal (self-normalized dv/v does not need it)."""
    days = years * 365.25
    out = {}
    for label, sub in (
        ("CA-broadband (SCEDC+NCEDC)", df[df["archive"] != "earthscope"]),
        ("all three archives", df),
    ):
        n_bh = int((~sub["hh_only"]).sum())
        n_hh = int(sub["hh_only"].sum())
        per_day = {
            False: 3 * t.station_day_channel_s(response=False)
            + 6 * t.pair_day_s(),
            True: 3 * t.station_day_channel_s(response=False, hh=True)
            + 6 * t.pair_day_s(),
        }
        act = sub["active_days"].clip(upper=days)
        active_frac = (act / days).mean()
        bh_days = float(act[~sub["hh_only"]].sum())
        hh_days = float(act[sub["hh_only"]].sum())
        cpu_s = bh_days * per_day[False] + hh_days * per_day[True]
        dvv_s = (
            (bh_days + hh_days) * t.dvv_configs
            * t.codameter_fixed_ms_day_cfg / 1000
        )
        task_h = (cpu_s / 2 + dvv_s / 2) / 3600  # 2 vCPU tasks
        cost = task_h * p.fargate_task_h(2, 8)
        wall_days_256 = task_h / (256 / 2) / 24
        out[label] = {
            "stations": len(sub),
            "(of which HH-only)": n_hh,
            "mean active fraction": round(active_frac, 2),
            "task-hours (2 vCPU)": int(task_h),
            "$ compute (Spot)": int(cost),
            "$/station": round(cost / max(len(sub), 1), 2),
            "wall @ maxvCpus=256": f"{wall_days_256:.1f} d",
            "wall @ maxvCpus=2048": f"{wall_days_256 / 8:.1f} d",
        }
    return pd.DataFrame(out).T


def scenario_subarray(
    p: Prices,
    t: Timings,
    df: pd.DataFrame,
    pairs,
    ov_days,
    census,
    years=25,
    target_sps=5.0,
    n_comp=9,
):
    """S2b: tiles of 150 km radius / 30 km step; crustal band (0.05-1 Hz) at
    target_sps. Three architectures compared."""
    days = years * 365.25
    sd_s = 3 * t.station_day_channel_s(response=True, target_sps=target_sps)
    pd_s = n_comp * t.pair_day_s(target_sps=target_sps)
    n_pairs = len(pairs)
    pair_days = float(ov_days.sum())  # joint-overlap: both stations recording

    station_days = float(df["active_days"].clip(upper=days).sum())
    # (naive) every tile FFTs every station in radius AND correlates every
    # pair both of whose stations it covers
    naive_fft_s = census["naive_dup_factor"] * station_days * sd_s
    naive_corr_s = census["naive_corr_dup_factor"] * pair_days * pd_s
    # (a) midpoint assignment: pairs computed exactly once; stations FFT'd
    # once per tile that owns >=1 of their pairs (census-measured)
    a_dup = census["stations_per_tile_job"].sum() / max(len(df), 1)
    a_fft_s = a_dup * station_days * sd_s
    # (b) two-phase: FFT exactly once per station-day + transient FFT store
    b_fft_s = station_days * sd_s
    fft_store_gb_day = len(df) * 3 * 86400 * target_sps * 4 / 1e9  # float32
    b_store = fft_store_gb_day * 30 * p.s3_storage_gb_mo  # 30-day lifecycle

    corr_s = pair_days * pd_s
    rows = {}
    for label, fft_s, c_s, extra in (
        ("naive per-tile recompute", naive_fft_s, naive_corr_s, 0.0),
        ("(a) midpoint assignment", a_fft_s, corr_s, 0.0),
        ("(b) two-phase FFT store", b_fft_s, corr_s, b_store),
    ):
        task_h = (fft_s + c_s) / 2 / 3600
        cost = task_h * p.fargate_task_h(2, 8) + extra
        rows[label] = {
            "task-hours (2 vCPU)": int(task_h),
            "$ compute (Spot)": int(cost),
            "wall @ 2048 vCpus": f"{task_h / 1024 / 24:.1f} d",
        }
    # storage of final products, with retention options
    n_lags = int(2 * 300 * target_sps) + 1
    stack_gb = n_pairs * n_comp * n_lags * 4 / 1e9
    monthly_gb = stack_gb * years * 12
    yearly_gb = stack_gb * years
    retention = pd.DataFrame(
        {
            "GB": [round(stack_gb), round(yearly_gb), round(monthly_gb)],
            "$/yr (S3 standard)": [
                int(stack_gb * p.s3_storage_gb_mo * 12),
                int((stack_gb + yearly_gb) * p.s3_storage_gb_mo * 12),
                int((stack_gb + monthly_gb) * p.s3_storage_gb_mo * 12),
            ],
        },
        index=["final stacks only", "+ yearly substacks", "+ monthly substacks"],
    )
    geometry = {
        "stations": len(df),
        "pairs <=300 km": n_pairs,
        "pair-days (joint overlap)": f"{pair_days / 1e9:.1f} B",
        "tiles": census["n_tiles"],
        "median stations/tile-job": int(np.median(census["stations_per_tile_job"])),
        "median pairs/tile": int(np.median(census["pairs_per_tile"])),
        "naive FFT duplication": round(census["naive_dup_factor"], 1),
        "naive correlate duplication": round(census["naive_corr_dup_factor"], 1),
        "(a) FFT duplication": round(a_dup, 1),
        "final stacks (GB)": round(stack_gb, 1),
    }
    return pd.DataFrame(rows).T, geometry, retention


# --------------------------------------------------------------------------- #
#  Report
# --------------------------------------------------------------------------- #


def _md_table(df: pd.DataFrame, index_label="") -> str:
    d = df.reset_index().rename(columns={"index": index_label})
    return d.to_markdown(index=False)


def make_figures(lam, alt, census, tiles_df=None):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    FIG_DIR.mkdir(exist_ok=True)
    # Lambda vs Fargate crossover
    fig, ax = plt.subplots(figsize=(7, 4.2))
    ax.plot(lam["stations"], lam["$ total/mo"], "o-", color="#2a78d6",
            label="Lambda per-station cron")
    ax.plot(alt["stations"], alt["$ total/mo"], "s-", color="#eb6834",
            label="One nightly Fargate Spot task")
    ax.set_xlabel("stations")
    ax.set_ylabel("$/month")
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_title("S1: daily dv/v service — architecture crossover")
    ax.legend()
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(FIG_DIR / "s1_crossover.png", dpi=110)
    plt.close(fig)
    # tile census histogram
    fig, ax = plt.subplots(1, 2, figsize=(10, 4))
    ax[0].hist(census["stations_per_tile_job"], bins=40, color="#2a78d6")
    ax[0].set_xlabel("stations per tile job (midpoint assignment)")
    ax[0].set_ylabel("tiles")
    ax[1].hist(np.log10(np.clip(census["pairs_per_tile"], 1, None)), bins=40,
               color="#1baf7a")
    ax[1].set_xlabel("log10(pairs per tile)")
    fig.suptitle("S2b tile census from the real station inventory")
    fig.tight_layout()
    fig.savefig(FIG_DIR / "s2b_tiles.png", dpi=110)
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--networks",
        default=str(Path.home() / "GitHub" / "QuakeScope" / "networks"),
    )
    ap.add_argument("--start", type=float, default=2000.0)
    ap.add_argument("--end", type=float, default=2025.25)
    args = ap.parse_args()

    p, t = Prices(), Timings()
    df = load_stations(Path(args.networks), args.start, args.end)
    pairs, ncounts = neighbor_stats(df, 300.0)
    ov_days = pair_overlap_days(df, pairs, args.start, args.end)
    census = tile_census(df, 150.0, 30.0, pairs)

    lam, alt, s1_sec = scenario_lambda_daily(p, t)
    s2a = scenario_fargate_25y(p, t, df)
    s2b, geom, retention = scenario_subarray(p, t, df, pairs, ov_days, census)
    make_figures(lam, alt, census)

    doc = f"""# Cloud cost model — seisfetch + NoisePy, obspy-free

How small can the bill go for science-scale ambient-noise jobs? Three
scenarios, every number traceable to a measured anchor, a dated AWS list
price, or the real station inventory. Regenerate with
`python tools/cost_model.py`.

## Anchors

| parameter | value | provenance |
|---|---|---|
| chain s/station-day-channel (resp. removed, 20 sps) | 2.1 s | measured 2026-08-06, M1, n=124 (seisfetch three-archive validation) |
| same w/o response removal | ~1.6 s | same run, component split |
| container penalty | x{t.container_penalty} | seisfetch benchmarks/RESULTS.md (recordlist parse path) |
| correlate, per pair-day (substack=False) | {t.correlate_pair_s} s @40 sps | noisepy-dvv-cloud 2026 audit (25-30% of compute) |
| codameter stretching, fixed ref | {t.codameter_fixed_ms_day_cfg} ms/day/config | measured 2026-08-06 (codavenv, 2561-sample CCFs) |
| codameter stretching, moving ref | {t.codameter_moving_ms_day_cfg} ms/day/config | same |
| Fargate on-demand | ${p.fargate_od_vcpu_h}/vCPU-h + ${p.fargate_od_gb_h}/GB-h | aws.amazon.com/fargate/pricing, {p.asof}; QuakeScope runbook cross-check |
| Fargate Spot discount | {int(p.spot_discount * 100)}% | AWS states "up to 70%"; floats with demand |
| Lambda | ${p.lambda_gb_s}/GB-s + ${p.lambda_per_million_req}/M req | aws.amazon.com/lambda/pricing, {p.asof} |
| S3 | ${p.s3_storage_gb_mo}/GB-mo, GET ${p.s3_get_per_1k}/1k, PUT ${p.s3_put_per_1k}/1k | list rates |
| station inventory | {len(df)} broadband stations active {args.start:.0f}-{args.end:.0f} | QuakeScope networks/*.zip (58,479 raw entries) |

Regions: run SCEDC/NCEDC jobs in us-west-2, EarthScope jobs in us-east-2
(bucket co-location; EarthScope direct S3 needs credential refresh). Reading
the open buckets in-region is free; cross-region is $0.02/GB and shows up as
a design error, not a budget line.

## S1 — Lambda daily dv/v service

One EventBridge rule -> one invocation per station per day: fetch the new
day file, seisfetch chain with response removal, 6 cross-component
pair-days, append CCF rows (Parquet on S3), incremental 60-config codameter
update against the reference stack, write dv/v rows. ~{s1_sec:.0f} s per
invocation at 1 vCPU-equivalent (1769 MB).

{_md_table(lam.set_index("stations"), "stations")}

Alternative: skip Lambda entirely — one nightly 2 vCPU Fargate Spot task
sweeps every station sequentially:

{_md_table(alt.set_index("stations"), "stations")}

![S1 crossover](cost-model-figs/s1_crossover.png)

**Read**: the per-station Lambda costs ~$0.01/station-month — and the
Lambda free tier (400k GB-s/month) covers the first ~500 stations outright,
so a public "live dv/v" pilot on EarthScope runs for approximately $0. The
nightly Fargate sweep is ~10x cheaper per station-day at scale; the choice
is operational (per-station isolation, retries, event-driven latency vs one
simple nightly task), not budgetary. Both are dwarfed by any standing
database; keep the product on S3+Parquet only.

## S2a — Fargate Spot, 25-year single-station dv/v backfill

One 2 vCPU / 8 GB Spot container per station (shardable 4-up as in the
existing job definitions), whole history, no response removal (dv/v is
self-normalized). Includes the codameter 60-config ensemble.

{_md_table(s2a)}

**Read**: the full California broadband archive for 25 years of
single-station dv/v is a few hundred dollars; the entire
EarthScope+SCEDC+NCEDC broadband inventory is a few thousand — the bill is
dominated by station count x active years, and HH-only stations cost
~{t.hh_factor}x their BH peers. At maxvCpus=2048 the whole thing drains in
about a week.

## S2b — moving-subarray cross-correlation (crustal band)

Tiles of 150 km radius stepped 30 km over the real station map; all pairs
<=300 km; {9}-component CCFs at 5 sps (crustal band 0.05-1 Hz); stacks
stored sorted by interstation distance.

Geometry from the real inventory:

{pd.Series(geom).to_markdown()}

![S2b tiles](cost-model-figs/s2b_tiles.png)

Architecture is the cost story — the same science at three prices:

{_md_table(s2b)}

**Read**: with 30-km steps a pair sits inside ~{geom['naive correlate duplication']:.0f}
tiles, so naive per-tile recompute multiplies the dominant correlation bill
by that factor — never acceptable. *Midpoint assignment* (each pair computed
only by the tile owning its midpoint) keeps the one-job-per-tile shape while
killing the correlate duplication entirely; what it cannot kill is FFT
duplication (~{geom['(a) FFT duplication']}x), because a station's pairs
scatter their midpoints over many tiles. The *two-phase* design (per-station
FFT jobs write a transient whitened-FFT store with a 30-day lifecycle; tile
jobs consume it) removes that too and is the cheapest — the FFT store rents
for pennies. Retention is the other lever:

{retention.to_markdown()}

Keep final stacks + yearly substacks; monthly substacks for every pair cost
more than the entire compute. (Monthly retention restricted to pairs
< 100 km, or float16 substacks, are cheap middle grounds the store layout
should permit.)

## Sensitivity (what moves the bill)

1. **Target sampling rate** — FFT + correlate + storage all scale ~linearly.
2. **Archive span x station count** — linear; the inventory's mean active
   fraction ({s2a.loc['all three archives', 'mean active fraction']}) already
   discounts stations that were not deployed.
3. **Spot discount** — floats around 70%; on-demand is ~3.3x.
4. **Compute timing uncertainty** — anchors are single-core M1; assume
   +/-2x when porting to Fargate silicon until the smoke test writes the
   observed $/station-day into runbook/01_aws_setup.md (Gate 2).
5. **Standing services** — none in any scenario by design. A DocumentDB-class
   database would exceed the entire S2a compute bill within months.

## Cheapest credible configurations

- **S1**: nightly Fargate Spot sweep + S3 Parquet, Lambda only if per-station
  isolation/latency matters. Order $10-100/month for 100-3000 stations.
- **S2a**: 2 vCPU Spot shards, 4 stations each, no response removal —
  ~${s2a.loc['CA-broadband (SCEDC+NCEDC)', '$/station']}/station for 25 years.
- **S2b**: midpoint-assigned tiles at 5 sps, monthly substacks + final stacks
  only (never keep daily pair CCFs) — the full western-US crustal survey for
  roughly the price of a laptop.
"""
    DOC.write_text(doc)
    print(f"wrote {DOC}")
    print(f"figures in {FIG_DIR}")


if __name__ == "__main__":
    main()
