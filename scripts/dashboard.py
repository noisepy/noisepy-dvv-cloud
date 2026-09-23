#!/usr/bin/env python
"""Build a self-contained HTML dashboard of the CCF and dv/v products.

    pixi run -e dvv python scripts/dashboard.py -o reports/dashboard.html
    pixi run -e dvv python scripts/dashboard.py --station CI.LJR --fragment -o page.html

Follows the Clements-Denolle presentation of single-station monitoring: a
waveform gather (lag time across, date down, signed amplitude in colour), the
reference stack with the coda measurement window shaded, and dv/v per octave
band with its uncertainty ribbon.

Like QuakeScope's campaign_dashboard.py this writes one file with no CDN, no
build step and no JavaScript charting library, so the page cannot silently fail
to load a script. Rasters are PNG data URIs written with `zlib` and `struct`;
the charts are hand-built SVG. The only dependencies are the ones the pipeline
already has (numpy, pandas, pyarrow).

Band-passing for display is a zero-phase FFT brick wall rather than a
Butterworth: scipy is not a declared dependency of this project, and for a
display gather the difference is not visible. The dv/v measurement itself is
filtered inside codameter and is untouched by anything here.

MEASURED (read from the Parquet products):
  daily CCFs, lag axis, sampling rate, windows stacked   ccf/v1
  dv/v, its three error columns, cc, members per epoch   dvv/v1

DERIVED (computed here, for display only, and labelled as such on the page):
  per-day normalisation -- each day scaled by its own CODA amplitude, not its
    global peak, which sits at zero lag and would render the coda blank; the
    zero-lag arrival is clipped as a result
  the reference stack -- mean of the band-passed daily traces

--fragment omits the <!doctype>/<html>/<head>/<body> wrapper, for a host that
supplies its own document skeleton.
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import pathlib
import struct
import sys
import zlib
from datetime import datetime, timezone

import numpy as np
import pandas as pd

PAIRS = ("EN", "EZ", "NZ", "EE", "NN", "ZZ")
# the three dv/v actually uses; the autocorrelations are opt-in
CROSS = ("EN", "EZ", "NZ")
BANDS = ((1.0, 2.0), (2.0, 4.0), (4.0, 8.0), (8.0, 16.0))
BAND_KEY = {f"{a}-{b}": f"b{i + 1}" for i, (a, b) in enumerate(BANDS)}

GATHER_MAX_PX = 430   # tallest the raster is stretched to, whatever the span
GATHER_ROW_PX = 15    # per-day height for a short series


# --------------------------------------------------------------------------- #
#  PNG, in the standard library
# --------------------------------------------------------------------------- #
def _png(rgb: np.ndarray) -> bytes:
    """Encode an (h, w, 3) uint8 array. Filter type 0 on every scanline."""
    h, w, _ = rgb.shape
    raw = b"".join(b"\x00" + rgb[y].tobytes() for y in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def _data_uri(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


# Diverging ramp for signed correlation amplitude. This is the convention these
# gathers are read in, so it is kept rather than restyled; the ends match the
# --neg / --pos tokens in the stylesheet.
_NEG = np.array([0x2B, 0x5F, 0x9E], dtype=float)
_MID = np.array([0xF7, 0xF7, 0xF4], dtype=float)
_POS = np.array([0xA8, 0x35, 0x2A], dtype=float)


def _colorize(v: np.ndarray) -> np.ndarray:
    """v in [-1, 1] -> (h, w, 3) uint8. NaN takes the mid tone."""
    x = np.clip(np.nan_to_num(v, nan=0.0), -1.0, 1.0)[..., None]
    lo = _MID + (_NEG - _MID) * (-x)
    hi = _MID + (_POS - _MID) * x
    return np.where(x < 0, lo, hi).round().astype(np.uint8)


# --------------------------------------------------------------------------- #
#  signal, for display only
# --------------------------------------------------------------------------- #
def bandpass(a: np.ndarray, fs: float, f1: float, f2: float) -> np.ndarray:
    n = a.shape[-1]
    freq = np.fft.rfftfreq(n, d=1.0 / fs)
    spec = np.fft.rfft(a, axis=-1)
    spec[..., ~((freq >= f1) & (freq <= f2))] = 0.0
    return np.fft.irfft(spec, n=n, axis=-1)


def normalize_rows(a: np.ndarray, t: np.ndarray, window) -> np.ndarray:
    """Scale each day by its CODA amplitude, then clip.

    Normalising by the global peak renders the coda blank, which is the part
    being measured: on a single-station correlation that peak sits at zero lag
    and is far larger than everything after it. Scaling by the coda instead
    saturates the zero-lag arrival -- it draws solid, which is correct and
    expected -- and spends the colour range where the measurement happens.

    Per day rather than per gather, so a quiet day does not vanish beside a
    noisy one.
    """
    t1, t2 = window
    inside = (np.abs(t) >= t1) & (np.abs(t) <= t2)
    scale = np.nanmax(np.abs(a[..., inside]), axis=-1, keepdims=True)
    with np.errstate(invalid="ignore", divide="ignore"):
        out = np.where(scale > 0, a / scale, np.nan)
    return np.clip(out, -1.0, 1.0)


# --------------------------------------------------------------------------- #
#  small SVG helpers
# --------------------------------------------------------------------------- #
def _esc(s) -> str:
    return html.escape(str(s), quote=True)


def _polyline(xs, ys) -> str:
    """A NaN breaks the line rather than drawing a segment across missing days."""
    out, pen = [], False
    for x, y in zip(xs, ys):
        if not np.isfinite(y):
            pen = False
            continue
        out.append(f"{'L' if pen else 'M'}{x:.2f} {y:.2f}")
        pen = True
    return " ".join(out)


def _ticks(lo: float, hi: float, target: int = 5) -> list[float]:
    if not (np.isfinite(lo) and np.isfinite(hi)) or hi <= lo:
        return [0.0]
    raw = (hi - lo) / target
    mag = 10.0 ** np.floor(np.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    v, out = np.ceil(lo / step) * step, []
    while v <= hi + step * 1e-9:
        out.append(round(float(v), 10))
        v += step
    return out


def _fmt(v: float) -> str:
    if not np.isfinite(v):
        return "--"
    a = abs(v)
    if a >= 100 or (a >= 1 and a == int(a)):
        return f"{v:.0f}"
    return f"{v:.3g}" if a < 1 else f"{v:.2f}"


# --------------------------------------------------------------------------- #
#  loading
# --------------------------------------------------------------------------- #
def load_ccfs(ccf_root, network, station):
    """{pair: (ccfs, days, t, fs)} for whichever pairs exist."""
    from noisepy_dvv_cloud import parquet_io
    out = {}
    for pair in PAIRS:
        try:
            out[pair] = parquet_io.read_ccf_matrix(ccf_root, network, station, pair)
        except (FileNotFoundError, OSError):
            continue
    return out


def load_nwindows(ccf_root, network, station):
    """nwindows per day, straight from the shard -- the QC trail for a gather
    row that looks thin."""
    import pyarrow.dataset as ds
    try:
        dataset = ds.dataset(ccf_root.rstrip("/"), format="parquet", partitioning="hive")
        tbl = dataset.to_table(
            columns=["date", "pair", "nwindows"],
            filter=(ds.field("network") == network) & (ds.field("station") == station),
        )
    except Exception:
        return pd.DataFrame(columns=["date", "nwindows"])
    df = tbl.to_pandas()
    if df.empty:
        return df
    return df.groupby("date", as_index=False)["nwindows"].median().sort_values("date")


def load_dvv(dvv_root, network, station):
    """{band_str: DataFrame} for whichever bands were written."""
    out = {}
    for f1, f2 in BANDS:
        band = f"{f1}-{f2}"
        path = f"{dvv_root.rstrip('/')}/band={band}/{network}.{station}.parquet"
        try:
            df = pd.read_parquet(path)
        except Exception:
            continue
        out[band] = df.sort_values("date").reset_index(drop=True)
    return out


def coda_window(band: tuple[float, float]) -> tuple[float, float]:
    """The window dv/v is measured in, taken from the pipeline rather than
    restated, so the shading on the page cannot drift from the measurement."""
    from noisepy_dvv_cloud.dvv import dvv_config
    cfg, _ = dvv_config(None, band)
    return tuple(cfg["window"])


# --------------------------------------------------------------------------- #
#  panels
# --------------------------------------------------------------------------- #
def display_lag(t: np.ndarray, fs: float, band, window):
    """Which samples of the lag axis to draw, and how much to decimate them.

    Two reductions, both of which improve the figure as well as its weight:

    * clip to a little past the coda window. The measurement never looks
      outside it, and on the first render most of the +/-32 s frame was blank.
    * decimate by the largest factor that still satisfies Nyquist for the
      band's upper corner, so the picture cannot alias.

    Returns (mask, k). 1-2 Hz decimates 9x, 8-16 Hz not at all.
    """
    _, t2 = window
    mask = np.abs(t) <= min(abs(float(t[0])), 1.25 * t2)
    k = max(1, int(fs // (2.2 * band[1])))
    return mask, k


def _decimate(a: np.ndarray, k: int) -> np.ndarray:
    """Block mean along the last axis. Safe because `display_lag` picked k."""
    if k == 1:
        return a
    n = (a.shape[-1] // k) * k
    return a[..., :n].reshape(*a.shape[:-1], n // k, k).mean(axis=-1)


def _bin_rows(a: np.ndarray, k: int) -> np.ndarray:
    """Average k adjacent days into one raster row.

    The frame is at most GATHER_MAX_PX tall, so encoding one row per day over a
    two-year span ships several times more rows than can ever be displayed --
    the browser would just resample them, and a 16 MB page cannot afford that.
    Binning is done AFTER per-day normalisation so every day carries equal
    weight in its row, which makes a bin a short stack rather than an
    amplitude-weighted average.
    """
    if k == 1:
        return a
    n = (a.shape[0] // k) * k
    out = a[:n].reshape(n // k, k, a.shape[1]).mean(axis=1)
    if n < a.shape[0]:
        out = np.vstack([out, a[n:].mean(axis=0, keepdims=True)])
    return out


def row_bin(ndays: int) -> int:
    return max(1, int(np.ceil(ndays / GATHER_MAX_PX)))


def build_gathers(ccfs: dict, pairs, kd: int = 1):
    """{(pair, band): png}, {(pair, band): trace}, {band: lag axis}."""
    gathers, stacks, axes = {}, {}, {}
    for pair in pairs:
        a, _days, t, fs = ccfs[pair]
        for f1, f2 in BANDS:
            band = f"{f1}-{f2}"
            win = coda_window((f1, f2))
            mask, k = display_lag(t, fs, (f1, f2), win)
            bp = bandpass(a, fs, f1, f2)[:, mask]
            td = _decimate(t[mask], k)
            axes[band] = td
            gathers[(pair, band)] = _data_uri(_png(_colorize(_bin_rows(
                normalize_rows(_decimate(bp, k), td, win), kd))))
            # the stack is the mean of the unclipped traces, scaled the same
            # way afterwards, so both panels share one scale
            ref = _decimate(np.nanmean(bp, axis=0), k)
            stacks[(pair, band)] = normalize_rows(ref[None, :], td, win)[0]
    return gathers, stacks, axes


def svg_stack(ref: np.ndarray, t: np.ndarray, window, band_key: str) -> str:
    """Reference stack with the coda measurement window shaded on both sides."""
    W, H, PL, PR, PT, PB = 960, 150, 8, 8, 10, 26
    iw, ih = W - PL - PR, H - PT - PB
    tmin, tmax = float(t[0]), float(t[-1])

    def sx(v):
        return PL + (v - tmin) / (tmax - tmin) * iw

    def sy(v):
        return PT + ih / 2 - v * (ih / 2) * 0.94

    t1, t2 = window
    shade = "".join(
        f'<rect x="{sx(max(lo, tmin)):.1f}" y="{PT}" '
        f'width="{sx(min(hi, tmax)) - sx(max(lo, tmin)):.1f}" '
        f'height="{ih}" class="coda"/>'
        for lo, hi in ((-t2, -t1), (t1, t2))
    )
    grid = "".join(
        f'<line x1="{sx(v):.1f}" y1="{PT}" x2="{sx(v):.1f}" y2="{PT + ih}" '
        f'class="grid"/><text x="{sx(v):.1f}" y="{H - 8}" class="ax" '
        f'text-anchor="middle">{_fmt(v)}</text>'
        for v in _ticks(tmin, tmax, 8)
    )
    d = _polyline([sx(v) for v in t], [sy(v) for v in ref])
    return (
        f'<svg viewBox="0 0 {W} {H}" role="img" '
        f'aria-label="Reference stack, coda window {t1:.1f} to {t2:.1f} s">'
        f'{shade}{grid}'
        f'<line x1="{PL}" y1="{sy(0):.1f}" x2="{W - PR}" y2="{sy(0):.1f}" class="zero"/>'
        f'<path d="{d}" class="wave v-{band_key}"/>'
        f'</svg>'
    )


def svg_lag_axis(t: np.ndarray, window) -> str:
    W, H = 960, 34
    tmin, tmax = float(t[0]), float(t[-1])

    def sx(v):
        return (v - tmin) / (tmax - tmin) * W

    t1, t2 = window
    out = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Lag axis, seconds">']
    for v in _ticks(tmin, tmax, 8):
        out.append(f'<line x1="{sx(v):.1f}" y1="0" x2="{sx(v):.1f}" y2="4" class="grid"/>')
        out.append(f'<text x="{sx(v):.1f}" y="14" class="ax" '
                   f'text-anchor="middle">{_fmt(v)}</text>')
    out.append(f'<text x="{W}" y="31" class="ax lbl" text-anchor="end">'
               f'lag time (s) &middot; coda {t1:.1f}-{t2:.1f} s &middot; '
               f'zero lag clipped</text>')
    out.append("</svg>")
    return "".join(out)


def svg_dvv(dvv: dict, uid: str = "d") -> tuple[str, str]:
    """dv/v per band with its total-error ribbon, and the cc series below."""
    frames = [d for d in dvv.values() if not d.empty]
    if not frames:
        return "", "No dv/v products for this station yet."

    dates = pd.DatetimeIndex(sorted(pd.to_datetime(
        pd.concat([d["date"] for d in frames]).unique())))
    x0, x1 = dates[0], dates[-1]
    span = max((x1 - x0).total_seconds(), 1.0)

    W, H, PL, PR, PT = 960, 326, 54, 12, 12
    dv_h, cc_h, gap = 186, 58, 34
    iw = W - PL - PR

    def sx(d):
        return PL + (pd.Timestamp(d) - x0).total_seconds() / span * iw

    finite = np.concatenate([
        np.asarray(d["dvv"], float)[np.isfinite(np.asarray(d["dvv"], float))]
        for d in frames
    ])
    has_dvv = finite.size > 0
    if has_dvv:
        # A robust spread, not the maximum. The first months of a moving
        # reference carry an enormous error ribbon while the reference settles,
        # and scaling to it squashed two years of real signal into the middle
        # third of the frame. The 98th percentile of |dv/v| + sigma keeps the
        # burn-in on the page without letting it set the axis; the drawing is
        # clipped so what overflows is cut rather than drawn over the caption.
        env = np.concatenate([
            (np.abs(np.asarray(d["dvv"], float))
             + np.nan_to_num(np.asarray(d["dvv_err"], float)))
            for d in frames])
        lim = max(float(np.nanpercentile(env, 98)) * 1.1, 1e-3)
    else:
        lim = 0.5

    def sy(v):
        return PT + dv_h / 2 - (v / lim) * (dv_h / 2)

    parts = [f'<clipPath id="clip-{uid}"><rect x="{PL}" y="{PT}" '
             f'width="{iw}" height="{dv_h}"/></clipPath>']
    for v in _ticks(-lim, lim, 4):
        y = sy(v)
        parts.append(f'<line x1="{PL}" y1="{y:.1f}" x2="{W - PR}" y2="{y:.1f}" '
                     f'class="{"zero" if abs(v) < 1e-12 else "grid"}"/>')
        parts.append(f'<text x="{PL - 8}" y="{y + 3.5:.1f}" class="ax" '
                     f'text-anchor="end">{_fmt(v)}</text>')

    parts.append(f'<g clip-path="url(#clip-{uid})">')
    for band, d in dvv.items():
        if d.empty:
            continue
        key = BAND_KEY[band]
        xs = [sx(v) for v in d["date"]]
        dv = np.asarray(d["dvv"], float)
        er = np.asarray(d["dvv_err"], float)
        ok = np.isfinite(dv) & np.isfinite(er)
        if ok.any():
            up = [(xs[i], sy(dv[i] + er[i])) for i in range(len(xs)) if ok[i]]
            dn = [(xs[i], sy(dv[i] - er[i])) for i in range(len(xs)) if ok[i]][::-1]
            parts.append('<polygon points="'
                         + " ".join(f"{x:.2f},{y:.2f}" for x, y in up + dn)
                         + f'" class="ribbon f-{key}"/>')
        parts.append(f'<path d="{_polyline(xs, [sy(v) for v in dv])}" '
                     f'class="trace v-{key}"/>')

    parts.append("</g>")

    cc_top = PT + dv_h + gap
    parts.append(f'<text x="{PL - 8}" y="{cc_top + 4}" class="ax" text-anchor="end">1.0</text>')
    parts.append(f'<text x="{PL - 8}" y="{cc_top + cc_h}" class="ax" text-anchor="end">0.8</text>')
    parts.append(f'<rect x="{PL}" y="{cc_top}" width="{iw}" height="{cc_h}" class="ccbox"/>')
    for band, d in dvv.items():
        if d.empty:
            continue
        cc = np.asarray(d["cc"], float)
        ys = [cc_top + cc_h - (min(max(v, 0.8), 1.0) - 0.8) / 0.2 * cc_h for v in cc]
        parts.append(f'<path d="{_polyline([sx(v) for v in d["date"]], ys)}" '
                     f'class="trace thin v-{BAND_KEY[band]}"/>')

    idx = np.linspace(0, len(dates) - 1, min(5, len(dates))).round().astype(int)
    for j, i in enumerate(idx):
        d = dates[i]
        anchor = "start" if j == 0 else "end" if j == len(idx) - 1 else "middle"
        parts.append(f'<text x="{sx(d):.1f}" y="{H - 8}" class="ax" '
                     f'text-anchor="{anchor}">{d:%Y-%m-%d}</text>')
    parts.append(f'<text x="{PL}" y="{PT - 2}" class="ax lbl">dv/v %</text>')
    parts.append(f'<text x="{PL}" y="{cc_top - 6}" class="ax lbl">cc</text>')

    note = "" if has_dvv else (
        "Every dv/v value here is NaN while cc is finite. One ensemble member "
        "returned nothing over this span and the aggregation discarded the "
        "rest; the cc panel below is real."
    )
    return ('<svg viewBox="0 0 %d %d" role="img" aria-label="dv/v per band">%s</svg>'
            % (W, H, "".join(parts))), note


# --------------------------------------------------------------------------- #
#  page
# --------------------------------------------------------------------------- #
FONTS = ('<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
         'family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600'
         '&display=swap">')

CSS = """
:root{
  --ground:#f5f6f8; --surface:#ffffff; --sunken:#eceef2; --rule:#d8dce4;
  --ink:#151a22; --ink2:#586074; --ink3:#8a91a1;
  --neg:#2b5f9e; --pos:#a8352a;
  --b1:#0f555d; --b2:#2f7f6b; --b3:#9a7420; --b4:#98452a;
  --coda:rgba(168,53,42,.07);
  --warn-bg:#fdf6e6; --warn-ink:#6f5510; --warn-rule:#e3cf94;
  --sans:"IBM Plex Sans",-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
  --mono:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,monospace;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --ground:#0e1118; --surface:#161a23; --sunken:#1c212b; --rule:#2a313f;
  --ink:#e8ebf1; --ink2:#a4adbf; --ink3:#6d7686;
  --neg:#5b91cf; --pos:#d9635a;
  --b1:#3ea7b1; --b2:#5cbf9d; --b3:#d4b055; --b4:#d8784f;
  --coda:rgba(217,99,90,.10);
  --warn-bg:#241f12; --warn-ink:#e0c684; --warn-rule:#4d421f;
}}
:root[data-theme="dark"]{
  --ground:#0e1118; --surface:#161a23; --sunken:#1c212b; --rule:#2a313f;
  --ink:#e8ebf1; --ink2:#a4adbf; --ink3:#6d7686;
  --neg:#5b91cf; --pos:#d9635a;
  --b1:#3ea7b1; --b2:#5cbf9d; --b3:#d4b055; --b4:#d8784f;
  --coda:rgba(217,99,90,.10);
  --warn-bg:#241f12; --warn-ink:#e0c684; --warn-rule:#4d421f;
}
*{box-sizing:border-box}
/* Every `display:` rule below is an author rule and so outranks the UA
   stylesheet's [hidden] -- without this, toggling el.hidden does nothing and
   every station, pair and band renders at once. The Artifact host adds this
   same reset; declaring it here keeps the standalone file identical. */
[hidden]{display:none!important}
body{background:var(--ground);color:var(--ink);font-family:var(--sans);
  font-size:14px;line-height:1.55;margin:0;-webkit-font-smoothing:antialiased}
.wrap{max-width:1040px;margin:0 auto;padding-inline:16px;padding-block:28px 56px}
.eyebrow{font-family:var(--mono);font-size:11px;letter-spacing:.14em;
  text-transform:uppercase;color:var(--ink3);margin:0 0 6px}
h1{font-size:clamp(24px,4.4vw,34px);font-weight:600;letter-spacing:-.02em;
  margin:0 0 6px;text-wrap:balance}
.sub{color:var(--ink2);margin:0 0 22px;max-width:64ch}
.readout{display:flex;flex-wrap:wrap;gap:1px;background:var(--rule);
  border:1px solid var(--rule);border-radius:3px;overflow:hidden;margin-bottom:34px}
.cell{background:var(--surface);padding:9px 14px;flex:1 1 130px;min-width:0}
.cell dt{font-family:var(--mono);font-size:10px;letter-spacing:.1em;
  text-transform:uppercase;color:var(--ink3);margin:0}
.cell dd{font-family:var(--mono);font-size:14px;margin:2px 0 0;
  font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
section{margin-bottom:40px}
h2{font-size:17px;font-weight:600;letter-spacing:-.01em;margin:0 0 3px;
  display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
h2 .n{font-family:var(--mono);font-size:11px;color:var(--ink3);letter-spacing:.1em}
.cap{color:var(--ink2);margin:0 0 16px;max-width:70ch}

/* network: map beside the station list */
.network{display:grid;grid-template-columns:minmax(0,1.3fr) minmax(0,1fr);
  gap:18px;align-items:start}
.mapbox{background:var(--surface);border:1px solid var(--rule);border-radius:3px;
  padding:10px}
/* one class deeper than `.mapbox img` on purpose: at equal specificity the
   generic rule would win and both maps would show */
.mapbox img.map-light,.mapbox img.map-dark{display:block;width:100%;height:auto}
.mapbox img.map-dark{display:none}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]) .mapbox img.map-light{display:none}
  :root:not([data-theme="light"]) .mapbox img.map-dark{display:block}}
:root[data-theme="dark"] .mapbox img.map-light{display:none}
:root[data-theme="dark"] .mapbox img.map-dark{display:block}
.stalist{display:flex;flex-direction:column;gap:1px;background:var(--rule);
  border:1px solid var(--rule);border-radius:3px;overflow:hidden}
.sta{display:grid;grid-template-columns:14px 1fr auto;gap:10px;align-items:center;
  background:var(--surface);border:0;cursor:pointer;text-align:left;
  padding:10px 13px;font-family:var(--sans);font-size:13px;color:var(--ink)}
.sta:hover{background:var(--sunken)}
.sta[aria-pressed="true"]{background:var(--sunken);
  box-shadow:inset 3px 0 0 var(--pos)}
.sta:focus-visible{outline:2px solid var(--b2);outline-offset:-2px}
.sta .tri{width:0;height:0;border-left:7px solid transparent;
  border-right:7px solid transparent;border-bottom:12px solid var(--ink3)}
.sta[aria-pressed="true"] .tri{border-bottom-color:var(--pos)}
.sta b{display:block;font-family:var(--mono);font-weight:500;font-size:13px}
.sta em{display:block;font-style:normal;color:var(--ink3);font-size:11.5px}
.sta .co{font-family:var(--mono);font-size:10.5px;color:var(--ink3);
  text-align:right;font-variant-numeric:tabular-nums;line-height:1.4}

.controls{display:flex;flex-wrap:wrap;gap:18px;margin-bottom:14px}
.group{display:flex;flex-direction:column;gap:5px}
.group>span{font-family:var(--mono);font-size:10px;letter-spacing:.1em;
  text-transform:uppercase;color:var(--ink3)}
.seg{display:flex;flex-wrap:wrap;gap:1px;background:var(--rule);
  border:1px solid var(--rule);border-radius:3px;overflow:hidden}
.seg button{font-family:var(--mono);font-size:12px;border:0;cursor:pointer;
  padding:5px 11px;background:var(--surface);color:var(--ink2)}
.seg button:hover{color:var(--ink);background:var(--sunken)}
.seg button[aria-pressed="true"]{background:var(--ink);color:var(--ground)}
.seg button:focus-visible{outline:2px solid var(--b2);outline-offset:-2px}

.plot{background:var(--surface);border:1px solid var(--rule);border-radius:3px;
  padding:14px 16px 10px}
.gather{display:grid;grid-template-columns:78px minmax(0,1fr);gap:10px}
.gutter{position:relative;font-family:var(--mono);font-size:10.5px;
  color:var(--ink3);font-variant-numeric:tabular-nums}
.gutter span{position:absolute;right:0;transform:translateY(-50%);white-space:nowrap}
.raster{position:relative}
.raster img{display:block;width:100%;height:100%;border:1px solid var(--rule)}
svg{display:block;width:100%;height:auto;overflow:visible}
.ax{font-family:var(--mono);font-size:10.5px;fill:var(--ink3)}
.ax.lbl{fill:var(--ink2);font-size:11px}
.grid{stroke:var(--rule);stroke-width:1}
.zero{stroke:var(--ink3);stroke-width:1;stroke-dasharray:2 3}
.coda{fill:var(--coda)}
.ccbox{fill:none;stroke:var(--rule)}
.wave{fill:none;stroke-width:1.2}
.trace{fill:none;stroke-width:1.9;stroke-linejoin:round}
.trace.thin{stroke-width:1.2;opacity:.8}
.ribbon{stroke:none;opacity:.17}
.v-b1{stroke:var(--b1)} .v-b2{stroke:var(--b2)}
.v-b3{stroke:var(--b3)} .v-b4{stroke:var(--b4)}
.f-b1{fill:var(--b1)} .f-b2{fill:var(--b2)}
.f-b3{fill:var(--b3)} .f-b4{fill:var(--b4)}
.legend{display:flex;flex-wrap:wrap;gap:14px;margin-top:12px;
  font-family:var(--mono);font-size:11px;color:var(--ink2)}
.legend i{display:inline-block;width:16px;height:2.5px;margin-right:6px;
  vertical-align:2px;border-radius:2px}
.ramp{display:flex;align-items:center;gap:8px;margin-top:12px;flex-wrap:wrap;
  font-family:var(--mono);font-size:10.5px;color:var(--ink3)}
.ramp .bar{height:8px;flex:0 1 190px;border:1px solid var(--rule);border-radius:2px;
  background:linear-gradient(90deg,var(--neg),var(--sunken),var(--pos))}
.note{background:var(--warn-bg);border:1px solid var(--warn-rule);
  border-left:3px solid var(--warn-rule);color:var(--warn-ink);
  border-radius:3px;padding:12px 15px;margin:0 0 16px;max-width:76ch}
.tablewrap{overflow-x:auto;border:1px solid var(--rule);border-radius:3px;
  background:var(--surface);max-height:460px}
table{border-collapse:collapse;width:100%;font-family:var(--mono);font-size:12px;
  font-variant-numeric:tabular-nums}
th,td{text-align:right;padding:6px 12px;white-space:nowrap;
  border-bottom:1px solid var(--rule)}
th{color:var(--ink3);font-weight:500;font-size:10px;letter-spacing:.09em;
  text-transform:uppercase;position:sticky;top:0;background:var(--sunken)}
td:first-child,th:first-child{text-align:left}
tbody tr:last-child td{border-bottom:0}
.nan{color:var(--ink3)}
footer{border-top:1px solid var(--rule);padding-top:20px;color:var(--ink2);
  font-size:12.5px}
footer h3{font-family:var(--mono);font-size:10px;letter-spacing:.12em;
  text-transform:uppercase;color:var(--ink3);margin:0 0 6px;font-weight:500}
footer ul{margin:0 0 16px;padding-left:18px}
footer code{font-family:var(--mono);font-size:11.5px;background:var(--sunken);
  padding:1px 5px;border-radius:2px}
@media (max-width:760px){.network{grid-template-columns:minmax(0,1fr)}}
@media (max-width:560px){.gather{grid-template-columns:56px minmax(0,1fr)}
  .cell{flex:1 1 100%}}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
"""


def station_rows(coords: dict, stations, days_by_station) -> str:
    out = []
    for i, sid in enumerate(stations):
        c = coords.get(sid, {})
        site = c.get("site") or ""
        lat, lon = c.get("lat"), c.get("lon")
        co = (f'{lat:.3f}&deg;, {lon:.3f}&deg;<br>{c.get("elevation_m", 0):.0f} m'
              if lat is not None else "&mdash;")
        nd = len(days_by_station.get(sid, []))
        out.append(
            f'<button type="button" class="sta" data-station="{sid}" '
            f'aria-pressed="{str(i == 0).lower()}">'
            f'<span class="tri" aria-hidden="true"></span>'
            f'<span><b>{_esc(sid)}</b><em>{_esc(site)} &middot; {nd} days</em></span>'
            f'<span class="co">{co}</span></button>'
        )
    return "".join(out)


def page(built, stations, coords, maps, first_pair, first_band, pairs,
         bucket="") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    bucket = _esc(bucket or "local products")
    band_strs = [f"{a}-{b}" for a, b in BANDS]
    s0 = stations[0]

    readouts = "".join(
        f'<dl class="readout" id="ro-{sid}" {"" if sid == s0 else "hidden"}>'
        + "".join(f'<div class="cell"><dt>{_esc(k)}</dt><dd>{_esc(v)}</dd></div>'
                  for k, v in built[sid]["meta"])
        + "</dl>"
        for sid in stations
    )

    pair_btns = "".join(
        f'<button type="button" data-pair="{p}" '
        f'aria-pressed="{str(p == first_pair).lower()}">{p}</button>' for p in pairs)
    band_btns = "".join(
        f'<button type="button" data-band="{b}" '
        f'aria-pressed="{str(b == first_band).lower()}">{b} Hz</button>'
        for b in band_strs)

    imgs = rasters = stacks = axes = tables = dvvs = ""
    for sid in stations:
        b = built[sid]
        on0 = sid == s0
        rasters += (
            f'<div class="raster" id="r-{sid}" style="{b["height_css"]}" '
            f'{"" if on0 else "hidden"}>'
            + "".join(
                f'<img id="g-{sid}-{p}-{bd}" src="{b["gathers"][(p, bd)]}" '
                f'alt="Gather {sid} {p} {bd} Hz" '
                f'{"" if (on0 and p == first_pair and bd == first_band) else "hidden"}>'
                for p in pairs for bd in band_strs if (p, bd) in b["gathers"])
            + "</div>")
        imgs += (f'<div class="gutter" id="gut-{sid}" {"" if on0 else "hidden"}>'
                 f'{b["gutter"]}</div>')
        stacks += "".join(
            f'<div id="s-{sid}-{p}-{bd}" '
            f'{"" if (on0 and p == first_pair and bd == first_band) else "hidden"}>'
            f'{b["stacks"][(p, bd)]}</div>'
            for p in pairs for bd in band_strs if (p, bd) in b["stacks"])
        axes += "".join(
            f'<div id="a-{sid}-{bd}" class="axis" '
            f'{"" if (on0 and bd == first_band) else "hidden"}>{sv}</div>'
            for bd, sv in b["axes"].items())
        dvvs += (f'<div id="dv-{sid}" {"" if on0 else "hidden"}>'
                 + (f'<p class="note">{_esc(b["dvv_note"])}</p>' if b["dvv_note"] else "")
                 + (f'<div class="plot">{b["dvv_svg"]}<div class="legend">'
                    + "".join(f'<span><i style="background:var(--{BAND_KEY[bd]})">'
                              f'</i>{bd} Hz</span>' for bd in band_strs)
                    + "</div></div>" if b["dvv_svg"] else
                    '<p class="note">No dv/v products for this station yet.</p>')
                 + "</div>")
        tables += (f'<div id="tb-{sid}" {"" if on0 else "hidden"}>{b["table"]}</div>')

    mapimgs = ""
    if maps.get("light"):
        mapimgs += f'<img class="map-light" src="{maps["light"]}" alt="Station map">'
    if maps.get("dark"):
        mapimgs += f'<img class="map-dark" src="{maps["dark"]}" alt="Station map">'
    mapbox = (f'<div class="mapbox">{mapimgs}</div>' if mapimgs else
              '<div class="mapbox"><p class="cap" style="margin:0">No map yet. '
              'Build one with <code>pixi run -e map python '
              'scripts/station_map.py</code>.</p></div>')

    rows = station_rows(coords, stations,
                        {sid: built[sid]["days"] for sid in stations})

    return f"""<div class="wrap">
<p class="eyebrow">noisepy-dvv-cloud &middot; single-station coda monitoring</p>
<h1>Coda Monitor</h1>
<p class="sub">Cross-component correlations and relative velocity change, laid
out the way Clements &amp; Denolle present them: the daily gather, the reference
stack with its measurement window, then dv/v per octave band. Pick a station on
the map to change every panel below.</p>

<section>
<h2>Network <span class="n">southern California, SCEDC</span></h2>
<p class="cap">Triangles are the stations. Coordinates come from the archive&rsquo;s
own StationXML, so they cannot drift from the instrument metadata the
correlations were computed against. Relief is shaded SRTM at 15 arc-seconds,
kept light because it is context, not the subject.</p>
<div class="network">
  {mapbox}
  <div class="stalist" id="stations">{rows}</div>
</div>
</section>

{readouts}

<section>
<h2>Waveform gather <span class="n">lag &times; date</span></h2>
<p class="cap">One row per day, band-passed for display and scaled by each
day&rsquo;s own coda amplitude, so what you are reading is waveform coherence in
the window that matters rather than raw amplitude. The zero-lag arrival is far
larger than the coda and draws solid; that saturation is the price of making the
coda visible at all. The frame stops just past the coda window because the
measurement never looks further, and each band is decimated to the largest
factor that still satisfies Nyquist at its upper corner. Over a long span
adjacent days are averaged into one row so the raster never carries more rows
than the frame can show &mdash; the readout above says how many days per row. A
stable coda means the medium held still; a row that breaks up is a day the
measurement cannot use.</p>
<div class="controls">
<label class="group"><span>component pair</span><span class="seg" id="pairs">{pair_btns}</span></label>
<label class="group"><span>band</span><span class="seg" id="bands">{band_btns}</span></label>
</div>
<div class="plot">
  <div class="gather">
    <div>{imgs}</div>
    <div>{rasters}{axes}</div>
  </div>
  <div class="ramp"><span>-1</span><span class="bar"></span><span>+1</span>
    <span>normalised amplitude</span></div>
</div>
</section>

<section>
<h2>Reference stack <span class="n">mean of the band-passed days</span></h2>
<p class="cap">The waveform dv/v is measured against. Stretching this trace by a
few tenths of a percent and re-correlating against each day is the measurement;
the shaded coda window is where that comparison happens. Same coda scaling as
the gather, so the zero-lag arrival is clipped at the frame.</p>
<div class="plot">{stacks}</div>
</section>

<section>
<h2>Relative velocity change <span class="n">dv/v, four octave bands</span></h2>
<p class="cap">Ribbons are the total 1-sigma uncertainty: the per-epoch
coherence floor and the spread across five processing choices, combined by the
law of total variance. The axis is set from the 98th percentile of that
envelope rather than its maximum, so the reference burn-in at the start of each
series does not squash everything after it; whatever overflows is clipped.
Colour runs cool to warm with frequency, which is also depth &mdash; 1-2 Hz
samples deepest, 8-16 Hz shallowest. The lower panel is the
correlation coefficient against the reference, on a fixed 0.8-1.0 scale.</p>
{dvvs}
</section>

<section>
<h2>Measurements <span class="n">as written to Parquet</span></h2>
{tables}
</section>

<footer>
<h3>Measured</h3>
<ul>
<li>Daily CCFs, lag axis, sampling rate and windows stacked, from
<code>ccf/v1</code> (<code>CCF_SCHEMA</code>).</li>
<li>dv/v, its three error columns, cc and members per epoch, from
<code>dvv/v1</code> (<code>DVV_SCHEMA</code>).</li>
<li>Station coordinates and site names, from the archive's StationXML.</li>
</ul>
<h3>Derived here, for display only</h3>
<ul>
<li>Band-passing: zero-phase FFT brick wall. The measurement's own filtering
happens inside codameter and is untouched.</li>
<li>Per-day coda normalisation, which clips the zero-lag arrival, and the
reference stack shown above (the estimator builds its own reference
internally).</li>
</ul>
<p>Generated {stamp} from <code>{bucket}</code>. Regenerate with
<code>pixi run -e dvv python scripts/dashboard.py</code>; the map with
<code>pixi run -e map python scripts/station_map.py</code>.</p>
</footer>
</div>

<script>
(function () {{
  var sta = {s0!r}, pair = {first_pair!r}, band = {first_band!r};
  function show(id, on) {{ var el = document.getElementById(id); if (el) el.hidden = !on; }}
  function paint(on) {{
    show('ro-' + sta, on); show('r-' + sta, on); show('gut-' + sta, on);
    show('dv-' + sta, on); show('tb-' + sta, on);
    show('g-' + sta + '-' + pair + '-' + band, on);
    show('s-' + sta + '-' + pair + '-' + band, on);
    show('a-' + sta + '-' + band, on);
  }}
  function apply(ns, np, nb) {{
    paint(false);
    sta = ns; pair = np; band = nb;
    paint(true);
    document.querySelectorAll('#stations button').forEach(function (b) {{
      b.setAttribute('aria-pressed', String(b.dataset.station === sta)); }});
    document.querySelectorAll('#pairs button').forEach(function (b) {{
      b.setAttribute('aria-pressed', String(b.dataset.pair === pair)); }});
    document.querySelectorAll('#bands button').forEach(function (b) {{
      b.setAttribute('aria-pressed', String(b.dataset.band === band)); }});
  }}
  document.getElementById('stations').addEventListener('click', function (e) {{
    var b = e.target.closest('button'); if (b) apply(b.dataset.station, pair, band); }});
  document.getElementById('pairs').addEventListener('click', function (e) {{
    var b = e.target.closest('button'); if (b) apply(sta, b.dataset.pair, band); }});
  document.getElementById('bands').addEventListener('click', function (e) {{
    var b = e.target.closest('button'); if (b) apply(sta, pair, b.dataset.band); }});
}})();
</script>"""


def dvv_table(dvv: dict, band: str) -> str:
    d = dvv.get(band)
    if d is None or d.empty:
        return ""
    rows = []
    for _, r in d.iterrows():
        def cell(v, fmt="{:.4f}"):
            return ('<td class="nan">--</td>' if not np.isfinite(v)
                    else f"<td>{fmt.format(v)}</td>")
        rows.append(
            f'<tr><td>{r["date"]}</td>' + cell(r["dvv"]) + cell(r["dvv_err"])
            + cell(r["dvv_err_within"]) + cell(r["dvv_err_method"])
            + cell(r["cc"]) + f'<td>{int(r["n_members"])}</td></tr>')
    return (
        '<div class="tablewrap"><table><thead><tr><th>date</th><th>dv/v %</th>'
        '<th>err total</th><th>err within</th><th>err method</th><th>cc</th>'
        '<th>members</th></tr></thead><tbody>' + "".join(rows)
        + '</tbody></table></div>'
        f'<p class="cap" style="margin-top:10px">Band {_esc(band)} Hz. '
        '<code>err within</code> is the per-epoch coherence floor '
        '(Weaver/Clarke 2011); <code>err method</code> is the spread across the '
        'five processing choices; <code>err total</code> is their quadrature '
        'sum. <code>members</code> below 5 means a processing choice produced '
        'nothing at that epoch.</p>')


def _maps(outdir: pathlib.Path) -> dict:
    out = {}
    for theme in ("light", "dark"):
        f = outdir / f"map-{theme}.png"
        if f.exists():
            out[theme] = _data_uri(f.read_bytes())
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stations", default="CI.LJR,CI.RXH,CI.ADO",
                    help="comma-separated NET.STA")
    ap.add_argument("--ccf", default=None, help="CCF root (default: the campaign bucket)")
    ap.add_argument("--dvv", default=None, help="dv/v root (default: the campaign bucket)")
    ap.add_argument("--assets", default="reports",
                    help="where station_map.py put map-*.png and stations.json")
    ap.add_argument("--all-pairs", action="store_true",
                    help="include the autocorrelations (EE NN ZZ) as well as "
                         "the three cross-components dv/v actually uses")
    ap.add_argument("-o", "--output", default="reports/dashboard.html")
    ap.add_argument("--fragment", action="store_true",
                    help="omit the document skeleton")
    args = ap.parse_args(argv)

    from noisepy_dvv_cloud import parameters

    bucket = parameters.OUTPUT_BUCKET
    ccf_root = args.ccf or (f"s3://{bucket}/{parameters.CCF_PREFIX}" if bucket else None)
    dvv_root = args.dvv or (f"s3://{bucket}/{parameters.DVV_PREFIX}" if bucket else None)
    if not ccf_root:
        print("no CCF root: pass --ccf or set DVV_OUTPUT_BUCKET", file=sys.stderr)
        return 2

    assets = pathlib.Path(args.assets)
    coords = {}
    sj = assets / "stations.json"
    if sj.exists():
        coords = {c["id"]: c for c in json.loads(sj.read_text())}

    want = [s.strip() for s in args.stations.split(",") if s.strip()]
    wanted_pairs = PAIRS if args.all_pairs else CROSS
    first_band = "2.0-4.0"

    built, stations, pairs_seen = {}, [], []
    for sid in want:
        network, station = sid.split(".")[:2]
        ccfs = load_ccfs(ccf_root, network, station)
        ccfs = {p: v for p, v in ccfs.items() if p in wanted_pairs}
        if not ccfs:
            print(f"  {sid}: no CCFs under {ccf_root}, skipping", file=sys.stderr)
            continue
        pairs = [p for p in wanted_pairs if p in ccfs]
        pairs_seen = pairs if len(pairs) > len(pairs_seen) else pairs_seen

        _, days, t, fs = ccfs[pairs[0]]
        kd = row_bin(len(days))
        gathers, stack_arrays, lag_axes = build_gathers(ccfs, pairs, kd)
        windows = {f"{a}-{b}": coda_window((a, b)) for a, b in BANDS}
        stacks = {k: svg_stack(v, lag_axes[k[1]], windows[k[1]], BAND_KEY[k[1]])
                  for k, v in stack_arrays.items()}
        axes = {b: svg_lag_axis(lag_axes[b], windows[b]) for b in lag_axes}

        ndays = len(days)
        nrows = int(np.ceil(ndays / kd))
        row_px = min(GATHER_ROW_PX, GATHER_MAX_PX / max(nrows, 1))
        every = max(1, int(np.ceil(nrows / (GATHER_MAX_PX / 18))))
        gutter = "".join(
            f'<span style="top:{(r + 0.5) * row_px:.1f}px">'
            f'{pd.Timestamp(days[min(r * kd, ndays - 1)]):%Y-%m-%d}</span>'
            for r in range(nrows) if r % every == 0 or r == nrows - 1)

        dvv = load_dvv(dvv_root, network, station) if dvv_root else {}
        dvv_svg, dvv_note = (svg_dvv(dvv, uid=sid.replace(".", ""))
                             if dvv else ("", ""))
        nw = load_nwindows(ccf_root, network, station)
        hashes = sorted({d["config_hash"].iloc[0] for d in dvv.values() if not d.empty})

        built[sid] = {
            "gathers": gathers, "stacks": stacks, "axes": axes,
            "gutter": gutter, "height_css": f"height:{nrows * row_px:.0f}px",
            "row_bin": kd,
            "days": days, "dvv_svg": dvv_svg, "dvv_note": dvv_note,
            "table": dvv_table(dvv, first_band),
            "meta": [
                ("station", sid),
                ("site", coords.get(sid, {}).get("site", "--")),
                ("span", f"{pd.Timestamp(days[0]):%Y-%m-%d} to "
                         f"{pd.Timestamp(days[-1]):%Y-%m-%d}"),
                ("days", ndays if kd == 1 else f"{ndays} ({kd}/row)"),
                ("pairs", " ".join(pairs)),
                ("fs / maxlag", f"{fs:.0f} Hz / {abs(t[0]):.0f} s"),
                ("windows per day", f"{int(nw['nwindows'].min())}-"
                                    f"{int(nw['nwindows'].max())}"
                                    if not nw.empty else "--"),
                ("dv/v config", hashes[0] if hashes else "--"),
            ],
        }
        stations.append(sid)
        print(f"  {sid}: {ndays} days, {len(gathers)} gathers, {len(dvv)} bands")

    if not stations:
        print("no station had CCFs", file=sys.stderr)
        return 1

    body = page(built, stations, coords, _maps(assets),
                pairs_seen[0], first_band, pairs_seen, bucket=ccf_root)
    head = (f"<title>Coda Monitor</title>\n{FONTS}\n<style>{CSS}</style>")
    if args.fragment:
        out = head + "\n" + body
    else:
        out = ('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
               '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
               f"{head}\n</head>\n<body>\n{body}\n</body>\n</html>\n")

    p = pathlib.Path(args.output)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(out, encoding="utf-8")
    mb = len(out.encode()) / 1e6
    print(f"{p} · {mb:.2f} MB · {len(stations)} stations"
          + ("  ** over the 16 MB artifact limit **" if mb > 16 else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
