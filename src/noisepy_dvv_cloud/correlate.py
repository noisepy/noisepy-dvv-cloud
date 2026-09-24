"""Stage 1: single-station cross-component correlation with NoisePy.

One Batch job = one shard of stations x one date range. Reads public miniSEED
archives on S3, writes NoisePy numpy-format CCF/stack stores to a scratch
prefix, then exports daily stacks as Parquet (parquet_io.CCF_SCHEMA).
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone

from . import constants, parquet_io

logger = logging.getLogger(__name__)


def build_config(start: datetime, end: datetime, stations: list[str]):
    """NoisePy ConfigParameters for the Clements-Denolle single-station recipe."""
    from noisepy.seis.io.datatypes import (
        CCMethod,
        ConfigParameters,
        FreqNorm,
        RmResp,
        StackMethod,
        TimeNorm,
    )

    networks = sorted({s.split(".")[0] for s in stations})
    cfg = ConfigParameters(
        start_date=start,
        end_date=end,
        networks=networks,
        stations=[s.split(".")[1] for s in stations],
        # prefer BH (native 40 Hz): HH must be resampled 100->40 Hz, which
        # costs ~4x in preprocessing; keep HH only for BH-less stations
        channels=["BH?", "HH?"],
        sampling_rate=constants.SAMPLING_RATE,
        cc_len=constants.CC_LEN_S,
        step=constants.CC_STEP_S,
        maxlag=constants.MAXLAG_S,
        freqmin=constants.FREQMIN,
        freqmax=constants.FREQMAX,
        # single-station: same-station upper-triangle pairs only
        acorr_only=True,
        ncomp=3,
        rotation=False,  # rotation needs all 9 components; acorr_only yields 6
        cc_method=CCMethod.XCORR,
        freq_norm=FreqNorm.RMA,
        time_norm=TimeNorm.NO,
        # raw amplitudes: dv/v only needs phase, and response removal is one
        # of the slowest preprocessing steps (2026 campaign decision)
        rm_resp=RmResp.NO,
        # substack=False with inc_hours=24 still yields exactly one CCF per
        # component per day (daily dv/v resolution preserved) but averages the
        # 188 windows in the spectral domain: 1 ifft/pair/day instead of 188.
        # Measured: ~25-30% of compute and ~99% of CC-store size (2026 audit).
        substack=False,
        stack_method=StackMethod.LINEAR,
        inc_hours=24,
    )
    # the public archive buckets (scedc-pds/ncedc-pds) must be read
    # anonymously — without this, fsspec looks for AWS credentials and the
    # container dies with NoCredentialsError (found on the first smoke test)
    cfg.storage_options["s3"] = {"anon": True}
    return cfg


def config_hash(cfg) -> str:
    """Short provenance hash of the effective NoisePy config."""
    blob = cfg.model_dump_json().encode()
    return hashlib.sha1(blob).hexdigest()[:12]


def make_raw_store(cfg, stations: list[str], date_range):
    # date_range: datetimerange.DateTimeRange. Not imported at module scope --
    # it ships only in the `correlate` extra, and importing it here would make
    # this module unimportable in the `dvv` environment the tests run in.
    """Route each shard to its archive. A shard must be single-archive
    (submit_helper groups stations by archive before sharding)."""
    from noisepy.seis.io.channel_filter_store import LocationChannelFilterStore, channel_filter
    from noisepy.seis.io.channelcatalog import XMLStationChannelCatalog
    from noisepy.seis.io.s3store import NCEDCS3DataStore, SCEDCS3DataStore

    networks = {s.split(".")[0] for s in stations}
    archives = {constants.NETWORK_MAPPING[n] for n in networks}
    if len(archives) != 1:
        raise ValueError(f"shard spans multiple archives: {archives}")
    archive = constants.S3_ARCHIVES[archives.pop()]

    catalog_kwargs = {}
    if archive["xml_path_format"]:
        catalog_kwargs["path_format"] = archive["xml_path_format"]
    catalog = XMLStationChannelCatalog(
        archive["stationxml"], storage_options=cfg.storage_options, **catalog_kwargs
    )
    cls = SCEDCS3DataStore if "scedc" in archive["waveforms"] else NCEDCS3DataStore
    store = cls(
        archive["waveforms"],
        catalog,
        channel_filter(cfg.networks, cfg.stations, cfg.channels),
        date_range,
        storage_options=cfg.storage_options,
    )
    return PreferredBandStore(LocationChannelFilterStore(store))


class PreferredBandStore:
    """Drop HH where the same station already offers BH, before anything reads it.

    NoisePy keeps one band per orientation, but it does so AFTER `cc_timespan`
    has read every channel `get_channels` returned. Measured on CI.LJR
    2023-01-01: six channel-days fetched and mseed-decoded to preprocess three,
    and because HH at 100 sps is ~2.75x the bytes of BH at 40 sps, **73% of the
    bytes downloaded were discarded**. Filtering here instead took the
    station-day from 6.6 s to 3.6 s with bit-identical output -- max |diff| 0.0
    on all six pairs, since BH is what NoisePy kept either way.

    Per station rather than `cfg.channels = ["BH?"]`, because a station with no
    BH must still be correlated from HH: on the full inventory that is 9,837 of
    22,191 stations. A shard can mix the two cases.
    """

    def __init__(self, store, priority: tuple[str, ...] = constants.BAND_PRIORITY):
        self.store = store
        self.priority = priority

    def _rank(self, chan) -> int:
        band = chan.type.name[:2].upper()
        return self.priority.index(band) if band in self.priority else len(self.priority)

    def get_channels(self, timespan):
        best: dict[tuple, tuple[int, object]] = {}
        for ch in self.store.get_channels(timespan):
            key = (ch.station.network, ch.station.name, ch.type.get_orientation())
            rank = self._rank(ch)
            if key not in best or rank < best[key][0]:
                best[key] = (rank, ch)
        # sorted so a shard's channel order does not depend on dict insertion
        return sorted((ch for _, ch in best.values()), key=str)

    # everything else is the wrapped store's job
    def get_timespans(self, *a, **k):
        return self.store.get_timespans(*a, **k)

    def read_data(self, timespan, chan):
        return self.store.read_data(timespan, chan)

    def get_inventory(self, timespan, station):
        return self.store.get_inventory(timespan, station)


def run(stations: list[str], start: datetime, end: datetime, output: str, scratch: str) -> None:
    """Correlate + export-to-parquet for one shard.

    No separate stacking stage: with substack=False and inc_hours=24 each
    chunk already IS the daily linear stack, so stack_cross_correlations
    would only re-read the whole CC store to recompute a mean the CC stage
    had in memory (plus a process-pool spawn per worker). We export daily
    CCFs straight from the CC store instead (2026 efficiency audit).
    """
    from datetimerange import DateTimeRange
    from noisepy.seis import cross_correlate
    from noisepy.seis.io.numpystore import NumpyCCStore

    start = start.replace(tzinfo=timezone.utc)
    end = end.replace(tzinfo=timezone.utc)
    cfg = build_config(start, end, stations)
    chash = config_hash(cfg)
    date_range = DateTimeRange(start, end)

    raw_store = make_raw_store(cfg, stations, date_range)
    cc_store = NumpyCCStore(f"{scratch.rstrip('/')}/ccf_{chash}")
    logger.info("cross-correlating %d stations %s..%s", len(stations), start, end)
    cross_correlate(raw_store, cfg, cc_store)

    rows = export_daily_ccfs(cc_store, cfg, chash)
    parquet_io.write_ccf_day_batch(output, rows)
    logger.info("wrote %d daily CCF rows to %s", len(rows), output)


def export_daily_ccfs(cc_store, cfg, chash: str) -> list[dict]:
    """Flatten CrossCorrelation objects into CCF_SCHEMA rows (one per day+pair).

    Field names confirmed on the 2026-08-08 smoke test: ``cc.src``/``cc.rec``
    are ``ChannelType`` objects (orientation via ``get_orientation()``), and
    window counts live in ``cc.parameters["ngood"]``.
    """
    rows: list[dict] = []
    for src, rec in cc_store.get_station_pairs():
        for ts in cc_store.get_timespans(src, rec):
            for cc in cc_store.read(ts, src, rec):
                pair = (
                    f"{cc.src.get_orientation()}{cc.rec.get_orientation()}"
                ).upper()
                params = getattr(cc, "parameters", {}) or {}
                rows.append(
                    {
                        "network": src.network,
                        "station": src.name,
                        "location": src.location or "",
                        "pair": pair,
                        "date": ts.start_datetime.date(),
                        "ccf": cc.data.squeeze().astype("float32"),
                        "fs": cfg.sampling_rate,
                        "maxlag_s": cfg.maxlag,
                        "nwindows": int(params.get("ngood", 0)),
                        "config_hash": chash,
                    }
                )
    return rows
