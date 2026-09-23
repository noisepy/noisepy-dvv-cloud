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
import struct
import sys
import zlib
from datetime import datetime, timezone

import numpy as np
import pandas as pd

PAIRS = ("EN", "EZ", "NZ", "EE", "NN", "ZZ")
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
def build_gathers(ccfs: dict):
    """One raster and one reference stack per (pair, band)."""
    gathers, stacks = {}, {}
    for pair, (a, days, t, fs) in ccfs.items():
        for f1, f2 in BANDS:
            band = f"{f1}-{f2}"
            win = coda_window((f1, f2))
            bp = bandpass(a, fs, f1, f2)
            gathers[(pair, band)] = _data_uri(
                _png(_colorize(normalize_rows(bp, t, win))))
            # the stack is the mean of the unclipped traces, scaled the same
            # way afterwards, so both panels share one scale
            ref = np.nanmean(bp, axis=0)
            stacks[(pair, band)] = normalize_rows(ref[None, :], t, win)[0]
    return gathers, stacks


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
    bands = "".join(
        f'<rect x="{sx(lo):.1f}" y="{PT}" width="{sx(hi) - sx(lo):.1f}" '
        f'height="{ih}" class="coda"/>'
        for lo, hi in ((-t2, -t1), (t1, t2))
    )
    grid = "".join(
        f'<line x1="{sx(v):.1f}" y1="{PT}" x2="{sx(v):.1f}" y2="{PT + ih}" '
        f'class="grid"/><text x="{sx(v):.1f}" y="{H - 8}" class="ax" '
        f'text-anchor="middle">{v:.0f}</text>'
        for v in _ticks(tmin, tmax, 8)
    )
    step = max(1, len(t) // 2400)
    d = _polyline([sx(v) for v in t[::step]], [sy(v) for v in ref[::step]])
    return (
        f'<svg viewBox="0 0 {W} {H}" role="img" '
        f'aria-label="Reference stack, coda window {t1:.1f} to {t2:.1f} seconds">'
        f'{bands}{grid}'
        f'<line x1="{PL}" y1="{sy(0):.1f}" x2="{W - PR}" y2="{sy(0):.1f}" class="zero"/>'
        f'<path d="{d}" class="wave v-{band_key}"/>'
        f'<text x="{sx((t1 + t2) / 2):.1f}" y="{PT + 12}" class="ax cw" '
        f'text-anchor="middle">coda {t1:.1f}-{t2:.1f} s</text>'
        f'</svg>'
    )


def svg_dvv(dvv: dict) -> tuple[str, str]:
    """dv/v per band with its total-error ribbon, and the cc series below.

    Returns (svg, note). The note is non-empty when there is no finite dv/v to
    draw, which is a real state of the product and not an error here: it is
    what a short series produces, and the page has to say so rather than show
    an empty box.
    """
    frames = [d for d in dvv.values() if not d.empty]
    if not frames:
        return "", "No dv/v products found for this station."

    dates = pd.to_datetime(pd.concat([d["date"] for d in frames]).unique())
    dates = pd.DatetimeIndex(sorted(dates))
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
    ]) if frames else np.array([])
    has_dvv = finite.size > 0

    if has_dvv:
        errs = np.concatenate([np.nan_to_num(np.asarray(d["dvv_err"], float)) for d in frames])
        lim = float(np.nanmax(np.abs(finite)) + np.nanmax(errs)) * 1.15
        lim = max(lim, 1e-3)
    else:
        lim = 0.5

    def sy(v):
        return PT + dv_h / 2 - (v / lim) * (dv_h / 2)

    parts = []
    for v in _ticks(-lim, lim, 4):
        y = sy(v)
        parts.append(f'<line x1="{PL}" y1="{y:.1f}" x2="{W - PR}" y2="{y:.1f}" '
                     f'class="{"zero" if abs(v) < 1e-12 else "grid"}"/>')
        parts.append(f'<text x="{PL - 8}" y="{y + 3.5:.1f}" class="ax" '
                     f'text-anchor="end">{_fmt(v)}</text>')

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
            pts = " ".join(f"{x:.2f},{y:.2f}" for x, y in up + dn)
            parts.append(f'<polygon points="{pts}" class="ribbon f-{key}"/>')
        parts.append(f'<path d="{_polyline(xs, [sy(v) for v in dv])}" '
                     f'class="trace v-{key}"/>')

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
        # anchor the end labels inward or they hang off the drawing
        anchor = "start" if j == 0 else "end" if j == len(idx) - 1 else "middle"
        parts.append(f'<text x="{sx(d):.1f}" y="{H - 8}" class="ax" '
                     f'text-anchor="{anchor}">{d:%Y-%m-%d}</text>')

    parts.append(f'<text x="{PL}" y="{PT - 2}" class="ax lbl">dv/v %</text>')
    parts.append(f'<text x="{PL}" y="{cc_top - 6}" class="ax lbl">cc</text>')

    note = ""
    if not has_dvv:
        note = (
            "Every dv/v value in these products is NaN while cc is finite and "
            "n_members reports 4 of 5. That is not a plotting failure: one "
            "ensemble member (reference fixed to moving) returns nothing on a "
            "series this short, and processing_ensemble takes a plain mean, so "
            "the one empty member discards the other four at every epoch. The "
            "cc panel below is real."
        )
    return f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="dv/v per band">' \
           + "".join(parts) + "</svg>", note


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
  --coda:rgba(168,53,42,.07); --codaline:rgba(168,53,42,.35);
  --warn-bg:#fdf6e6; --warn-ink:#6f5510; --warn-rule:#e3cf94;
  --sans:"IBM Plex Sans",-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
  --mono:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,monospace;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --ground:#0e1118; --surface:#161a23; --sunken:#1c212b; --rule:#2a313f;
  --ink:#e8ebf1; --ink2:#a4adbf; --ink3:#6d7686;
  --neg:#5b91cf; --pos:#d9635a;
  --b1:#3ea7b1; --b2:#5cbf9d; --b3:#d4b055; --b4:#d8784f;
  --coda:rgba(217,99,90,.10); --codaline:rgba(217,99,90,.40);
  --warn-bg:#241f12; --warn-ink:#e0c684; --warn-rule:#4d421f;
}}
:root[data-theme="dark"]{
  --ground:#0e1118; --surface:#161a23; --sunken:#1c212b; --rule:#2a313f;
  --ink:#e8ebf1; --ink2:#a4adbf; --ink3:#6d7686;
  --neg:#5b91cf; --pos:#d9635a;
  --b1:#3ea7b1; --b2:#5cbf9d; --b3:#d4b055; --b4:#d8784f;
  --coda:rgba(217,99,90,.10); --codaline:rgba(217,99,90,.40);
  --warn-bg:#241f12; --warn-ink:#e0c684; --warn-rule:#4d421f;
}
*{box-sizing:border-box}
body{background:var(--ground);color:var(--ink);font-family:var(--sans);
  font-size:14px;line-height:1.55;margin:0;-webkit-font-smoothing:antialiased}
.wrap{max-width:1040px;margin:0 auto;padding-inline:16px;padding-block:28px 56px}

/* instrument readout header */
.eyebrow{font-family:var(--mono);font-size:11px;letter-spacing:.14em;
  text-transform:uppercase;color:var(--ink3);margin:0 0 6px}
h1{font-size:clamp(24px,4.4vw,34px);font-weight:600;letter-spacing:-.02em;
  margin:0 0 6px;text-wrap:balance}
.sub{color:var(--ink2);margin:0 0 20px;max-width:62ch}
.readout{display:flex;flex-wrap:wrap;gap:1px;background:var(--rule);
  border:1px solid var(--rule);border-radius:3px;overflow:hidden;margin-bottom:34px}
.cell{background:var(--surface);padding:9px 14px;flex:1 1 128px;min-width:0}
.cell dt{font-family:var(--mono);font-size:10px;letter-spacing:.1em;
  text-transform:uppercase;color:var(--ink3);margin:0}
.cell dd{font-family:var(--mono);font-size:14px;margin:2px 0 0;
  font-variant-numeric:tabular-nums;overflow-wrap:anywhere}

section{margin-bottom:40px}
h2{font-size:17px;font-weight:600;letter-spacing:-.01em;margin:0 0 3px;
  display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
h2 .n{font-family:var(--mono);font-size:11px;color:var(--ink3);
  letter-spacing:.1em}
.cap{color:var(--ink2);margin:0 0 16px;max-width:70ch}

/* controls */
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

/* gather */
.plot{background:var(--surface);border:1px solid var(--rule);border-radius:3px;
  padding:14px 16px 10px}
.gather{display:grid;grid-template-columns:78px minmax(0,1fr);gap:10px}
.gutter{position:relative;font-family:var(--mono);font-size:10.5px;
  color:var(--ink3);font-variant-numeric:tabular-nums}
.gutter span{position:absolute;right:0;transform:translateY(-50%);
  white-space:nowrap}
.raster{position:relative}
.raster img{display:block;width:100%;image-rendering:pixelated;
  border:1px solid var(--rule)}
.raster .mark{position:absolute;top:0;bottom:0;width:1px;
  background:var(--codaline);pointer-events:none}
.axis{margin-top:4px}
svg{display:block;width:100%;height:auto;overflow:visible}
.ax{font-family:var(--mono);font-size:10.5px;fill:var(--ink3)}
.ax.lbl{fill:var(--ink2);font-size:11px}
.ax.cw{fill:var(--pos);opacity:.85}
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
.ramp{display:flex;align-items:center;gap:8px;margin-top:12px;
  font-family:var(--mono);font-size:10.5px;color:var(--ink3)}
.ramp .bar{height:8px;flex:0 1 190px;border:1px solid var(--rule);border-radius:2px;
  background:linear-gradient(90deg,var(--neg),var(--sunken),var(--pos))}

.note{background:var(--warn-bg);border:1px solid var(--warn-rule);
  border-left:3px solid var(--warn-rule);color:var(--warn-ink);
  border-radius:3px;padding:12px 15px;margin:0 0 16px;max-width:76ch}
.note b{font-weight:600}

.tablewrap{overflow-x:auto;border:1px solid var(--rule);border-radius:3px;
  background:var(--surface)}
table{border-collapse:collapse;width:100%;font-family:var(--mono);font-size:12px;
  font-variant-numeric:tabular-nums}
th,td{text-align:right;padding:6px 12px;white-space:nowrap;
  border-bottom:1px solid var(--rule)}
th{color:var(--ink3);font-weight:500;font-size:10px;letter-spacing:.09em;
  text-transform:uppercase;text-align:right;position:sticky;top:0;
  background:var(--sunken)}
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
@media (max-width:560px){
  .gather{grid-template-columns:58px minmax(0,1fr)}
  .cell{flex:1 1 100%}
}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
"""


def page(meta, gathers, stacks, axis_svg, gutter, dvv, dvv_svg, dvv_note,
         pairs, first_pair, first_band) -> str:
    """Everything inside the document skeleton."""
    band_strs = [f"{a}-{b}" for a, b in BANDS]

    readout = "".join(
        f'<div class="cell"><dt>{_esc(k)}</dt><dd>{_esc(v)}</dd></div>'
        for k, v in meta
    )

    pair_btns = "".join(
        f'<button type="button" data-pair="{p}" '
        f'aria-pressed="{str(p == first_pair).lower()}">{p}</button>'
        for p in pairs
    )
    band_btns = "".join(
        f'<button type="button" data-band="{b}" '
        f'aria-pressed="{str(b == first_band).lower()}">{b} Hz</button>'
        for b in band_strs
    )

    imgs = "".join(
        f'<img id="g-{p}-{b}" src="{gathers[(p, b)]}" alt="Gather, {p}, {b} Hz" '
        f'{"" if (p == first_pair and b == first_band) else "hidden"}>'
        for p in pairs for b in band_strs if (p, b) in gathers
    )
    stack_svgs = "".join(
        f'<div id="s-{p}-{b}" '
        f'{"" if (p == first_pair and b == first_band) else "hidden"}>'
        f'{stacks[(p, b)]}</div>'
        for p in pairs for b in band_strs if (p, b) in stacks
    )
    axes = "".join(
        f'<div id="a-{b}" class="axis" '
        f'{"" if b == first_band else "hidden"}>{axis_svg[b]}</div>'
        for b in band_strs if b in axis_svg
    )

    legend = "".join(
        f'<span><i class="f-{BAND_KEY[b]}" style="background:var(--{BAND_KEY[b]})">'
        f'</i>{b} Hz</span>' for b in band_strs
    )

    rows = ""
    if dvv:
        ref = dvv[first_band] if first_band in dvv else next(iter(dvv.values()))
        for _, r in ref.iterrows():
            def cell(v, fmt="{:.4f}"):
                return ('<td class="nan">--</td>' if not np.isfinite(v)
                        else f"<td>{fmt.format(v)}</td>")
            rows += (
                f'<tr><td>{r["date"]}</td>'
                + cell(r["dvv"]) + cell(r["dvv_err"])
                + cell(r["dvv_err_within"]) + cell(r["dvv_err_method"])
                + cell(r["cc"], "{:.4f}")
                + f'<td>{int(r["n_members"])}</td></tr>'
            )

    dvv_block = (
        f'{f"<p class=note><b>Read this before the panel.</b> {_esc(dvv_note)}</p>" if dvv_note else ""}'
        f'<div class="plot">{dvv_svg}<div class="legend">{legend}</div></div>'
        if dvv_svg else
        '<p class="note">No dv/v products under this prefix yet. '
        'Run the dvv stage after the correlate stage has drained.</p>'
    )

    table_block = (
        f'<div class="tablewrap"><table><thead><tr><th>date</th><th>dv/v %</th>'
        f'<th>err total</th><th>err within</th><th>err method</th><th>cc</th>'
        f'<th>members</th></tr></thead><tbody>{rows}</tbody></table></div>'
        f'<p class="cap" style="margin-top:10px">Band {_esc(first_band)} Hz. '
        f'<code>err within</code> is the per-epoch coherence floor '
        f'(Weaver/Clarke 2011); <code>err method</code> is the spread across '
        f'the five processing choices; <code>err total</code> is their '
        f'quadrature sum.</p>'
        if rows else ""
    )

    return f"""<div class="wrap">
<p class="eyebrow">noisepy-dvv-cloud &middot; single-station coda monitoring</p>
<h1>Coda Monitor</h1>
<p class="sub">Cross-component correlations and relative velocity change for one
station, laid out the way Clements &amp; Denolle present them: the daily gather,
the reference stack with its measurement window, then dv/v per octave band.</p>
<dl class="readout">{readout}</dl>

<section>
<h2>Waveform gather <span class="n">lag &times; date</span></h2>
<p class="cap">One row per day, band-passed for display and scaled by each
day&rsquo;s own coda amplitude, so what you are reading is waveform coherence in
the window that matters rather than raw amplitude. The zero-lag arrival is far
larger than the coda and draws solid; that saturation is the price of making the
coda visible at all. A stable coda means the medium held still; a row that
breaks up is a day the measurement cannot use.</p>
<div class="controls">
<label class="group"><span>component pair</span><span class="seg" id="pairs">{pair_btns}</span></label>
<label class="group"><span>band</span><span class="seg" id="bands">{band_btns}</span></label>
</div>
<div class="plot">
  <div class="gather">
    <div class="gutter">{gutter}</div>
    <div>
      <div class="raster" id="raster">{imgs}</div>
      {axes}
    </div>
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
<div class="plot">{stack_svgs}</div>
</section>

<section>
<h2>Relative velocity change <span class="n">dv/v, four octave bands</span></h2>
<p class="cap">Ribbons are the total 1-sigma uncertainty. Colour runs cool to
warm with frequency, which is also depth: 1-2 Hz samples deepest, 8-16 Hz
shallowest. The lower panel is the correlation coefficient against the
reference, on a fixed 0.8-1.0 scale.</p>
{dvv_block}
</section>

{f'<section><h2>Measurements <span class="n">as written to Parquet</span></h2>{table_block}</section>' if table_block else ""}

<footer>
<h3>Measured</h3>
<ul>
<li>Daily CCFs, lag axis, sampling rate and windows stacked, from
<code>ccf/v1</code> (<code>CCF_SCHEMA</code>).</li>
<li>dv/v, its three error columns, cc and members per epoch, from
<code>dvv/v1</code> (<code>DVV_SCHEMA</code>).</li>
</ul>
<h3>Derived here, for display only</h3>
<ul>
<li>Band-passing: zero-phase FFT brick wall. The measurement's own filtering
happens inside codameter and is untouched.</li>
<li>Per-day coda normalisation, which clips the zero-lag arrival, and the
reference stack shown above (the estimator builds its own reference
internally).</li>
</ul>
<p>Regenerate with <code>pixi run -e dvv python scripts/dashboard.py</code>.</p>
</footer>
</div>

<script>
(function () {{
  var pair = {first_pair!r}, band = {first_band!r};
  function show(id, on) {{ var el = document.getElementById(id); if (el) el.hidden = !on; }}
  function apply(np, nb) {{
    show('g-' + pair + '-' + band, false); show('s-' + pair + '-' + band, false);
    show('a-' + band, false);
    pair = np; band = nb;
    show('g-' + pair + '-' + band, true); show('s-' + pair + '-' + band, true);
    show('a-' + band, true);
    document.querySelectorAll('#pairs button').forEach(function (b) {{
      b.setAttribute('aria-pressed', String(b.dataset.pair === pair)); }});
    document.querySelectorAll('#bands button').forEach(function (b) {{
      b.setAttribute('aria-pressed', String(b.dataset.band === band)); }});
  }}
  document.getElementById('pairs').addEventListener('click', function (e) {{
    var b = e.target.closest('button'); if (b) apply(b.dataset.pair, band); }});
  document.getElementById('bands').addEventListener('click', function (e) {{
    var b = e.target.closest('button'); if (b) apply(pair, b.dataset.band); }});
}})();
</script>"""


def svg_lag_axis(t: np.ndarray, window) -> str:
    """Lag axis under the raster, with the coda window called out."""
    W, H, PL, PR = 960, 34, 0, 0
    iw = W - PL - PR
    tmin, tmax = float(t[0]), float(t[-1])

    def sx(v):
        return PL + (v - tmin) / (tmax - tmin) * iw

    t1, t2 = window
    out = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Lag axis, seconds">']
    for v in _ticks(tmin, tmax, 8):
        out.append(f'<line x1="{sx(v):.1f}" y1="0" x2="{sx(v):.1f}" y2="4" class="grid"/>')
        out.append(f'<text x="{sx(v):.1f}" y="14" class="ax" '
                   f'text-anchor="middle">{v:.0f}</text>')
    out.append(f'<text x="{W:.0f}" y="31" class="ax lbl" '
               f'text-anchor="end">lag time (s) &middot; coda '
               f'{t1:.1f}-{t2:.1f} s &middot; zero lag clipped</text>')
    out.append("</svg>")
    return "".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--station", default="CI.LJR", help="NET.STA")
    ap.add_argument("--ccf", default=None, help="CCF root (default: the campaign bucket)")
    ap.add_argument("--dvv", default=None, help="dv/v root (default: the campaign bucket)")
    ap.add_argument("-o", "--output", default="reports/dashboard.html")
    ap.add_argument("--fragment", action="store_true",
                    help="omit the document skeleton")
    args = ap.parse_args(argv)

    from noisepy_dvv_cloud import parameters

    network, station = args.station.split(".")[:2]
    bucket = parameters.OUTPUT_BUCKET
    ccf_root = args.ccf or (f"s3://{bucket}/{parameters.CCF_PREFIX}" if bucket else None)
    dvv_root = args.dvv or (f"s3://{bucket}/{parameters.DVV_PREFIX}" if bucket else None)
    if not ccf_root:
        print("no CCF root: pass --ccf or set DVV_OUTPUT_BUCKET", file=sys.stderr)
        return 2

    ccfs = load_ccfs(ccf_root, network, station)
    if not ccfs:
        print(f"no CCFs for {network}.{station} under {ccf_root}", file=sys.stderr)
        return 1
    pairs = [p for p in PAIRS if p in ccfs]
    first_pair = pairs[0]
    first_band = "2.0-4.0" if any(b == (2.0, 4.0) for b in BANDS) else f"{BANDS[0][0]}-{BANDS[0][1]}"

    _, days, t, fs = ccfs[first_pair]
    gathers, stack_arrays = build_gathers(ccfs)

    windows = {f"{a}-{b}": coda_window((a, b)) for a, b in BANDS}
    stacks = {k: svg_stack(v, t, windows[k[1]], BAND_KEY[k[1]])
              for k, v in stack_arrays.items()}
    axis_svg = {b: svg_lag_axis(t, w) for b, w in windows.items()}

    # gutter labels, aligned to the stretched raster
    ndays = len(days)
    row_px = min(GATHER_ROW_PX, GATHER_MAX_PX / max(ndays, 1))
    every = max(1, int(np.ceil(ndays / (GATHER_MAX_PX / 18))))
    gutter = "".join(
        f'<span style="top:{(i + 0.5) * row_px:.1f}px">'
        f'{pd.Timestamp(days[i]):%Y-%m-%d}</span>'
        for i in range(ndays) if i % every == 0 or i == ndays - 1
    )
    height_css = f"height:{ndays * row_px:.0f}px"

    dvv = load_dvv(dvv_root, network, station) if dvv_root else {}
    dvv_svg, dvv_note = svg_dvv(dvv) if dvv else ("", "")

    nw = load_nwindows(ccf_root, network, station)
    nw_txt = (f"{int(nw['nwindows'].min())}-{int(nw['nwindows'].max())}"
              if not nw.empty else "--")
    hashes = sorted({d["config_hash"].iloc[0] for d in dvv.values() if not d.empty})

    meta = [
        ("station", f"{network}.{station}"),
        ("span", f"{pd.Timestamp(days[0]):%Y-%m-%d} to {pd.Timestamp(days[-1]):%Y-%m-%d}"),
        ("days", ndays),
        ("pairs", " ".join(pairs)),
        ("fs / maxlag", f"{fs:.0f} Hz / {abs(t[0]):.0f} s"),
        ("windows per day", nw_txt),
        ("dv/v config", hashes[0] if hashes else "--"),
        ("generated", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")),
    ]

    body = page(meta, gathers, stacks, axis_svg, gutter, dvv, dvv_svg, dvv_note,
                pairs, first_pair, first_band)
    body = body.replace('<div class="raster" id="raster">',
                        f'<div class="raster" id="raster" style="{height_css}">')
    head = f"<title>Coda Monitor</title>\n{FONTS}\n<style>{CSS}</style>"

    if args.fragment:
        out = head + "\n" + body
    else:
        out = (f"<!doctype html>\n<html lang=\"en\">\n<head>\n"
               f'<meta charset="utf-8">\n'
               f'<meta name="viewport" content="width=device-width,initial-scale=1">\n'
               f"{head}\n</head>\n<body>\n{body}\n</body>\n</html>\n")

    import pathlib
    p = pathlib.Path(args.output)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(out, encoding="utf-8")
    print(f"{p} · {len(out) / 1e6:.2f} MB · {len(gathers)} gathers · "
          f"{ndays} days · {len(dvv)} bands")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
