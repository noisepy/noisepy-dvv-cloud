"""Static routing and processing constants."""

# Which public S3 archive serves each network. Extend as coverage grows;
# unlisted networks raise in correlate.py rather than guessing.
NETWORK_MAPPING = {
    "CI": "scedc",
    "AZ": "scedc",
    "SB": "scedc",
    "NC": "ncedc",
    "BK": "ncedc",
    "NN": "ncedc",
}

S3_ARCHIVES = {
    "scedc": {
        "waveforms": "s3://scedc-pds/continuous_waveforms/",
        "stationxml": "s3://scedc-pds/FDSNstationXML/CI/",
        "xml_path_format": None,  # SCEDC default layout
    },
    "ncedc": {
        "waveforms": "s3://ncedc-pds/continuous_waveforms/NC/",
        "stationxml": "s3://ncedc-pds/FDSNstationXML/NC/",
        "xml_path_format": "{network}.{station}.xml",
    },
}

# Single-station component pairs. acorr_only=True in NoisePy yields the
# upper-triangle EE, EN, EZ, NN, NZ, ZZ; dv/v uses the three cross-components,
# following Clements & Denolle (2023) and Hobiger et al. (2014).
CROSS_COMPONENTS = ("EN", "EZ", "NZ")
AUTO_COMPONENTS = ("EE", "NN", "ZZ")

# Band preference, highest first. NoisePy's own dedup keeps one band per
# orientation but only AFTER every channel has been fetched and decoded, so
# without this the HH day-files are downloaded and thrown away. BH is native
# 40 Hz; HH is 100 Hz and must be resampled, costing ~2.75x the bytes and ~4x
# the preprocessing. A station with no BH keeps its HH.
BAND_PRIORITY = ("BH", "HH")

# Clements-Denolle octave bands (Hz); band directory names are "fmin-fmax".
FREQ_BANDS = ((1.0, 2.0), (2.0, 4.0), (4.0, 8.0), (8.0, 16.0))

# Correlation recipe carried over from Clements-Denolle-2022.
SAMPLING_RATE = 40.0
CC_LEN_S = 1800
CC_STEP_S = 450
MAXLAG_S = 32.0
FREQMIN = 0.5
FREQMAX = 19.0

DATE_FMT = "%Y.%j"  # YYYY.DDD everywhere, matching QuakeScope
