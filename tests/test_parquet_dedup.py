"""Overlapping shards must not lengthen the CCF matrix past its date axis."""

import numpy as np
import pandas as pd
import pytest

from noisepy_dvv_cloud import parquet_io


def _write(root, rows):
    parquet_io.write_ccf_day_batch(str(root), rows)


def _row(date, nwindows, value, config_hash="aaaaaaaaaaaa"):
    return {
        "network": "CI", "station": "LJR", "location": "", "pair": "EN",
        "date": date, "ccf": np.full(5, value, dtype=np.float32),
        "fs": np.float32(40.0), "maxlag_s": np.float32(0.05),
        "nwindows": np.int32(nwindows), "config_hash": config_hash,
    }


def test_overlapping_shards_collapse_to_one_row_per_day(tmp_path):
    """The 2026-09-23 failure: a 10-day smoke shard overlapping a campaign shard
    added rows to the matrix, and dvv.station_dvv died on a length mismatch far
    from the cause."""
    d0, d1 = pd.Timestamp("2023-01-01").date(), pd.Timestamp("2023-01-02").date()
    _write(tmp_path, [_row(d0, 100, 1.0), _row(d1, 100, 2.0)])
    # a second shard covering the same days, stacked from fewer windows
    _write(tmp_path, [_row(d0, 40, 9.0, "bbbbbbbbbbbb")])

    ccfs, days, t, fs = parquet_io.read_ccf_matrix(str(tmp_path), "CI", "LJR", "EN")
    assert len(days) == 2 == ccfs.shape[0]
    assert len(np.unique(days)) == 2
    # the better-observed day wins
    np.testing.assert_allclose(ccfs[0], 1.0)
    assert fs == pytest.approx(40.0)
    assert len(t) == ccfs.shape[1]


def test_no_duplicates_is_untouched(tmp_path):
    d0 = pd.Timestamp("2023-02-01").date()
    _write(tmp_path, [_row(d0, 77, 3.0)])
    ccfs, days, _, _ = parquet_io.read_ccf_matrix(str(tmp_path), "CI", "LJR", "EN")
    assert ccfs.shape[0] == 1 and len(days) == 1
    np.testing.assert_allclose(ccfs[0], 3.0)
