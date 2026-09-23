#!/usr/bin/env python
"""Station map for the dashboard: shaded relief under station triangles.

    pixi run -e map python scripts/station_map.py --stations CI.LJR,CI.RXH,CI.ADO

Writes `reports/map-light.png`, `reports/map-dark.png` and `reports/stations.json`.
`scripts/dashboard.py` embeds whichever of those it finds; it renders fine
without them, so the heavy dependency never blocks the dashboard.

Why its own pixi environment: GMT is a C library with its own data stack
(netCDF, GDAL) and has no business in the solve for either processing stage --
the same reason `awscli` sits in `ops`. Two PNGs rather than one because the
page is theme-aware and a map built for a white ground reads badly on a dark
one; both are written with a transparent background so the page's own surface
colour shows through.

Coordinates come from the archive's own StationXML, read anonymously, not from
a table in this repo -- so they cannot drift from the instrument metadata the
correlations were computed against.
"""

from __future__ import annotations

import argparse
import io
import json
import pathlib
import sys
import xml.etree.ElementTree as ET

FDSN_NS = {"s": "http://www.fdsn.org/xml/station/1"}

# light: warm grey relief on nothing; dark: cool grey relief on nothing.
THEMES = {
    "light": {
        "ramp": "#ffffff,#f1efeb",
        "transparency": 30,
        "coast": "0.4p,#9aa0ab",
        "tri_fill": "#a8352a",
        "tri_pen": "0.7p,#3a1512",
        "label": "#2a2f38",
        "frame_pen": "0.6p,#b9bec7",
        "font": "7p,Helvetica,#5a6074",
    },
    "dark": {
        "ramp": "#0d111a,#454e5e",
        "transparency": 18,
        "coast": "0.4p,#5d6676",
        "tri_fill": "#d9635a",
        "tri_pen": "0.7p,#160b0a",
        "label": "#e2e6ee",
        "frame_pen": "0.6p,#39414f",
        "font": "7p,Helvetica,#9aa3b4",
    },
}


def station_coords(station_id: str) -> dict:
    """(lat, lon, elevation, site) straight from the archive's StationXML."""
    import boto3
    from botocore import UNSIGNED
    from botocore.config import Config

    from noisepy_dvv_cloud import constants

    net, sta = station_id.split(".")[:2]
    archive = constants.S3_ARCHIVES[constants.NETWORK_MAPPING[net]]
    base = archive["stationxml"]
    fmt = archive["xml_path_format"]
    name = fmt.format(network=net, station=sta) if fmt else f"{net}_{sta}.xml"

    bucket, _, prefix = base.replace("s3://", "").partition("/")
    key = prefix.rstrip("/") + "/" + name

    s3 = boto3.client("s3", config=Config(signature_version=UNSIGNED))
    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()

    root = ET.parse(io.BytesIO(body)).getroot()
    for node in root.iterfind(".//s:Station", FDSN_NS):
        if node.get("code") != sta:
            continue

        def val(tag, cast=float):
            el = node.find(f"s:{tag}", FDSN_NS)
            return cast(el.text) if el is not None and el.text else None

        return {
            "id": f"{net}.{sta}",
            "network": net,
            "station": sta,
            "lat": val("Latitude"),
            "lon": val("Longitude"),
            "elevation_m": val("Elevation"),
            "site": (node.findtext("s:Site/s:Name", default="", namespaces=FDSN_NS)
                     or "").strip(),
        }
    raise KeyError(f"{station_id} not found in {base}{name}")


def _rgb(hex_colour: str) -> str:
    """'#aabbcc' -> 'r/g/b', the only colour syntax a CPT file takes."""
    h = hex_colour.lstrip("#")
    return "/".join(str(int(h[i:i + 2], 16)) for i in (0, 2, 4))


def draw(coords: list[dict], theme: str, out: pathlib.Path, pad: float) -> None:
    import pygmt

    t = THEMES[theme]
    lons = [c["lon"] for c in coords]
    lats = [c["lat"] for c in coords]
    # a single station would give a zero-width region
    region = [
        min(lons) - pad, max(lons) + pad,
        min(lats) - pad, max(lats) + pad,
    ]

    grid = pygmt.datasets.load_earth_relief(resolution="15s", region=region)
    # The amplitude here, not the colour ramp, is what decides how light the
    # relief reads. GMT intensity runs -1..+1 and a negative intensity drives
    # the colour toward BLACK whatever the CPT says, so a near-white ramp under
    # a full-strength hillshade still renders as a heavy sepia basemap. -Nt0.3
    # caps the excursion at +/-0.3 and leaves the relief as texture rather than
    # subject. Transparency cannot substitute: the PNG is saved with an alpha
    # background, so it yields alpha rather than a blend toward white.
    shade = pygmt.grdgradient(grid=grid, radiance=[270, 30], normalize="t0.22")
    zlo, zhi = float(grid.min()), float(grid.max())

    fig = pygmt.Figure()
    # COLOR_HSV_*_S = 0 is the one that matters, and it cost an afternoon.
    # GMT applies hillshade intensity in HSV space and ADDS saturation as it
    # darkens (defaults MIN_S 1, MAX_S 0.1). A white or grey colour has no
    # defined hue, so GMT falls back to hue 0 -- red -- and a near-white relief
    # ramp renders as a brown basemap no matter what the CPT says. Pinning both
    # saturation limits to zero makes intensity change value only, which is
    # what "shaded grey relief" means.
    pygmt.config(MAP_FRAME_TYPE="plain", MAP_FRAME_PEN=t["frame_pen"],
                 FONT_ANNOT_PRIMARY=t["font"], MAP_TICK_PEN_PRIMARY=t["frame_pen"],
                 COLOR_HSV_MIN_S=0, COLOR_HSV_MAX_S=0)
    fig.basemap(region=region, projection="M11c", frame=["WSne", "af"])
    # The CPT is written by hand rather than through makecpt. Neither the
    # session CPT (`cmap=True`) nor `makecpt(output=...)` reliably reached
    # grdimage here -- GMT kept falling back to its default `geo` ramp, which
    # is why a near-white two-stop ramp rendered as a brown relief map. The
    # format is two z/colour stops and the three out-of-range slots, so writing
    # it directly is both shorter and unambiguous.
    cpt = out.with_suffix(".cpt")
    c0, c1 = (_rgb(c) for c in t["ramp"].split(","))
    cpt.write_text(
        "# COLOR_MODEL = RGB\n"
        f"{zlo:.1f}\t{c0}\t{zhi:.1f}\t{c1}\n"
        f"B\t{c0}\nF\t{c1}\nN\t{c0}\n"
    )
    # the relief is a ground, not the subject: kept light and see-through so the
    # triangles and the page behind it stay dominant
    fig.grdimage(grid=grid, shading=shade, cmap=str(cpt),
                 transparency=t["transparency"], nan_transparent=True)
    fig.coast(shorelines=t["coast"], borders=[f"2/{t['coast']}"],
              transparency=20)
    fig.plot(x=lons, y=lats, style="t0.42c", fill=t["tri_fill"], pen=t["tri_pen"])
    fig.text(x=lons, y=lats, text=[c["id"] for c in coords],
             font=f"8p,Helvetica-Bold,{t['label']}", justify="LM",
             offset="0.28c/0c", fill=None)
    fig.savefig(str(out), dpi=115, transparent=True, crop=True)
    cpt.unlink(missing_ok=True)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stations", default="CI.LJR,CI.RXH,CI.ADO",
                    help="comma-separated NET.STA")
    ap.add_argument("--pad", type=float, default=0.55,
                    help="degrees of margin around the stations")
    ap.add_argument("-d", "--outdir", default="reports")
    args = ap.parse_args(argv)

    ids = [s.strip() for s in args.stations.split(",") if s.strip()]
    coords = []
    for sid in ids:
        try:
            coords.append(station_coords(sid))
        except Exception as exc:
            print(f"  {sid}: {type(exc).__name__}: {exc}", file=sys.stderr)
    if not coords:
        print("no coordinates resolved", file=sys.stderr)
        return 1

    outdir = pathlib.Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "stations.json").write_text(json.dumps(coords, indent=2))
    for c in coords:
        print(f"  {c['id']:10} {c['lat']:8.4f} {c['lon']:10.4f}  "
              f"{c['elevation_m']:6.0f} m  {c['site']}")

    for theme in THEMES:
        out = outdir / f"map-{theme}.png"
        draw(coords, theme, out, args.pad)
        print(f"  {out} · {out.stat().st_size / 1e3:.0f} kB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
