"""Cloud-scale single-station dv/v monitoring pipeline.

Stage 1 (correlate): NoisePy single-station cross-component correlations -> Parquet on S3.
Stage 2 (dvv): codameter dv/v per component pair, combined -> Parquet on S3.
"""

__version__ = "0.1.0"
