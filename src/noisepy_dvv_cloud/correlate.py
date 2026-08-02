"""Stage 1: single-station cross-component correlation with NoisePy.

One Batch job = one shard of stations x one date range. Reads public miniSEED
archives on S3, writes NoisePy numpy-format CCF/stack stores to a scratch
prefix, then exports daily stacks as Parquet (parquet_io.CCF_SCHEMA).
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone

from datetimerange import DateTimeRange

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
    return ConfigParameters(
        start_date=start,
        end_date=end,
        networks=networks,
        stations=[s.split(".")[1] for s in stations],
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
        rm_resp=RmResp.INV,
        substack=True,
        substack_windows=1,
        stack_method=StackMethod.LINEAR,
        inc_hours=24,
    )


def config_hash(cfg) -> str:
    """Short provenance hash of the effective NoisePy config."""
    blob = cfg.model_dump_json().encode()
    return hashlib.sha1(blob).hexdigest()[:12]


def make_raw_store(cfg, stations: list[str], date_range: DateTimeRange):
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
    return LocationChannelFilterStore(store)


def run(stations: list[str], start: datetime, end: datetime, output: str, scratch: str) -> None:
    """Correlate + stack + export-to-parquet for one shard."""
    from noisepy.seis import cross_correlate, stack_cross_correlations
    from noisepy.seis.io.numpystore import NumpyCCStore, NumpyStackStore

    start = start.replace(tzinfo=timezone.utc)
    end = end.replace(tzinfo=timezone.utc)
    cfg = build_config(start, end, stations)
    chash = config_hash(cfg)
    date_range = DateTimeRange(start, end)

    raw_store = make_raw_store(cfg, stations, date_range)
    cc_store = NumpyCCStore(f"{scratch.rstrip('/')}/ccf_{chash}")
    logger.info("cross-correlating %d stations %s..%s", len(stations), start, end)
    cross_correlate(raw_store, cfg, cc_store)

    stack_store = NumpyStackStore(f"{scratch.rstrip('/')}/stack_{chash}")
    stack_cross_correlations(cc_store, stack_store, cfg)

    rows = export_daily_stacks(stack_store, cfg, chash)
    parquet_io.write_ccf_day_batch(output, rows)
    logger.info("wrote %d daily CCF rows to %s", len(rows), output)


def export_daily_stacks(stack_store, cfg, chash: str) -> list[dict]:
    """Flatten NoisePy Stack objects into CCF_SCHEMA rows (one per day+pair).

    TODO(first smoke test): confirm the Stack timestamp/name convention for
    daily substacks ('T<epoch>' names when keep_substack) and the component
    label set under acorr_only (expect EE, EN, EZ, NN, NZ, ZZ).
    """
    rows: list[dict] = []
    for src, rec in stack_store.get_station_pairs():
        for ts in stack_store.get_timespans(src, rec):
            for stack in stack_store.read(ts, src, rec):
                rows.append(
                    {
                        "network": src.network,
                        "station": src.name,
                        "location": src.location or "",
                        "pair": stack.component,
                        "date": ts.start_datetime.date(),
                        "ccf": stack.data.astype("float32"),
                        "fs": cfg.sampling_rate,
                        "maxlag_s": cfg.maxlag,
                        "nwindows": getattr(stack, "ngood", 0),
                        "config_hash": chash,
                    }
                )
    return rows
