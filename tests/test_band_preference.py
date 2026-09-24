"""BH must win over HH per station, and an HH-only station must survive.

NoisePy dedups bands only after every channel has been read, so this filter is
what stops the HH day-files being downloaded and discarded. The saving is real
(6.6 s -> 3.6 s on CI.LJR 2023-01-01, bit-identical output) but it is only safe
if HH-only stations keep their channels: 9,837 of the 22,191 stations in the
inventory have no BH at all.
"""

from types import SimpleNamespace

from noisepy_dvv_cloud.constants import BAND_PRIORITY
from noisepy_dvv_cloud.correlate import PreferredBandStore


def _chan(net, sta, code):
    return SimpleNamespace(
        station=SimpleNamespace(network=net, name=sta),
        type=SimpleNamespace(name=code, get_orientation=lambda c=code: c[-1]),
    )


class _Fake:
    def __init__(self, chans):
        self.chans = chans
        self.reads = []

    def get_channels(self, timespan):
        return list(self.chans)

    def read_data(self, timespan, chan):
        self.reads.append(chan.type.name)
        return "data"

    def get_timespans(self):
        return ["ts"]

    def get_inventory(self, timespan, station):
        return "inv"


def _names(chans):
    return sorted(c.type.name for c in chans)


def test_bh_wins_over_hh_for_the_same_station():
    inner = _Fake([_chan("CI", "LJR", c)
                   for c in ("BHE", "BHN", "BHZ", "HHE", "HHN", "HHZ")])
    assert _names(PreferredBandStore(inner).get_channels("ts")) == ["BHE", "BHN", "BHZ"]


def test_hh_only_station_keeps_its_channels():
    """The case a blanket channels=['BH?'] would silently drop."""
    inner = _Fake([_chan("CI", "XYZ", c) for c in ("HHE", "HHN", "HHZ")])
    assert _names(PreferredBandStore(inner).get_channels("ts")) == ["HHE", "HHN", "HHZ"]


def test_mixed_shard_resolves_per_station():
    """A shard can hold both kinds; the preference is per station, not global."""
    inner = _Fake(
        [_chan("CI", "LJR", c) for c in ("BHE", "BHZ", "HHE", "HHZ")]
        + [_chan("CI", "XYZ", c) for c in ("HHE", "HHZ")]
    )
    got = PreferredBandStore(inner).get_channels("ts")
    by_sta = {}
    for c in got:
        by_sta.setdefault(c.station.name, []).append(c.type.name)
    assert sorted(by_sta["LJR"]) == ["BHE", "BHZ"]
    assert sorted(by_sta["XYZ"]) == ["HHE", "HHZ"]


def test_unknown_band_is_kept_when_nothing_better_exists():
    inner = _Fake([_chan("CI", "ABC", c) for c in ("ELE", "ELZ")])
    assert _names(PreferredBandStore(inner).get_channels("ts")) == ["ELE", "ELZ"]


def test_known_band_beats_unknown_band():
    inner = _Fake([_chan("CI", "ABC", c) for c in ("ELZ", "BHZ")])
    assert _names(PreferredBandStore(inner).get_channels("ts")) == ["BHZ"]


def _order(chans):
    """Actual returned order. `_names` sorts, which would make an ordering test
    pass on any permutation -- it answers "same set?", not "same order?"."""
    return [c.type.name for c in chans]


def test_output_order_is_deterministic():
    """Reversing the input must not reorder the output."""
    fwd = [_chan("CI", "LJR", c) for c in ("BHE", "BHN", "BHZ")]
    a = _order(PreferredBandStore(_Fake(fwd)).get_channels("ts"))
    b = _order(PreferredBandStore(_Fake(fwd[::-1])).get_channels("ts"))
    assert a == b == ["BHE", "BHN", "BHZ"]


def test_order_is_stable_across_stations_too():
    """Ordering is by (network, station, orientation), so a shard holding
    several stations is grouped and ordered the same way every run."""
    chans = ([_chan("CI", "RXH", c) for c in ("BHZ", "BHE")]
             + [_chan("CI", "ADO", c) for c in ("BHN", "BHZ")])
    got = _order(PreferredBandStore(_Fake(chans)).get_channels("ts"))
    assert got == _order(PreferredBandStore(_Fake(chans[::-1])).get_channels("ts"))
    assert got == ["BHN", "BHZ", "BHE", "BHZ"]   # ADO (N,Z) then RXH (E,Z)


def test_reads_are_delegated_untouched():
    inner = _Fake([_chan("CI", "LJR", "BHZ")])
    store = PreferredBandStore(inner)
    assert store.read_data("ts", _chan("CI", "LJR", "BHZ")) == "data"
    assert store.get_timespans() == ["ts"]
    assert store.get_inventory("ts", "sta") == "inv"


def test_priority_matches_the_configured_channel_list():
    assert BAND_PRIORITY[0] == "BH"
