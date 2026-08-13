"""Pace statistics: robust lap filtering, descriptive metrics and driver comparison.

Pure functions over `LapPoint` sequences -- no database, no parser, no I/O.
Every returned dataclass is `dataclasses.asdict`-safe: fields hold plain Python
`int` / `float` / `bool` / `str` / `None` values, never numpy scalars, so the API
layer can serialise results directly.

Typical use::

    from karting.stats import LapFilter, LapPoint, compare_drivers, pace_stats

    stats = pace_stats(laps, LapFilter(mad_k=3.0))
    result = compare_drivers(laps_a, laps_b, name_a="WLAD111", name_b="TWG")
"""

from __future__ import annotations

from .compare import (
    DriverComparison,
    TestResult,
    cliffs_delta,
    compare_drivers,
    dominance_shares,
    hedges_g,
)
from .events import (
    KIND_JOKER,
    KIND_PIT,
    MIN_BASELINE_LAPS,
    SECTOR_ANOMALY_SHARE,
    DetectedEvent,
    EventDetectionConfig,
    EventReport,
    detect_events,
)
from .outliers import (
    IQR_SCALE,
    MAD_SCALE,
    MEAN_AD_SCALE,
    LapFilter,
    LapFlags,
    LapPoint,
    classify_laps,
    median_abs_deviation,
    robust_scale,
    select_used,
)
from .pace import PaceStats, pace_delta_to_best_driver, pace_stats

__all__ = [
    "IQR_SCALE",
    "KIND_JOKER",
    "KIND_PIT",
    "MAD_SCALE",
    "MEAN_AD_SCALE",
    "MIN_BASELINE_LAPS",
    "SECTOR_ANOMALY_SHARE",
    "DetectedEvent",
    "DriverComparison",
    "EventDetectionConfig",
    "EventReport",
    "LapFilter",
    "LapFlags",
    "LapPoint",
    "PaceStats",
    "TestResult",
    "classify_laps",
    "cliffs_delta",
    "compare_drivers",
    "detect_events",
    "dominance_shares",
    "hedges_g",
    "median_abs_deviation",
    "pace_delta_to_best_driver",
    "pace_stats",
    "robust_scale",
    "select_used",
]
