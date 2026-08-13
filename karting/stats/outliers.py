"""Lap selection and robust outlier detection.

The statistics layer never talks to the database or to the parser: it consumes
plain `LapPoint` values.  Every filtering decision is explicit and reversible --
`classify_laps` returns one `LapFlags` per input lap explaining *why* a lap was
dropped, so the UI can show the user what the numbers are based on.

Outlier detection is robust: median + scaled median absolute deviation
(``MAD_SCALE * MAD``), never mean + standard deviation, because a single 40 s
"stuck behind a spun kart" lap would otherwise inflate the threshold enough to
hide itself.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

import numpy as np

__all__ = [
    "IQR_SCALE",
    "MAD_SCALE",
    "MEAN_AD_SCALE",
    "LapFilter",
    "LapPoint",
    "LapFlags",
    "classify_laps",
    "robust_scale",
    "select_used",
    "median_abs_deviation",
]

#: Consistency constant that makes the MAD an unbiased estimator of the standard
#: deviation for normally distributed data.
MAD_SCALE: float = 1.4826
#: Same idea for the interquartile range: sigma ~ IQR / 1.349.
IQR_SCALE: float = 0.7413
#: ... and for the mean absolute deviation about the median: sigma ~ 1.2533 * MeanAD.
MEAN_AD_SCALE: float = 1.2533


@dataclass(slots=True, frozen=True)
class LapFilter:
    """Which laps count as "race pace" laps.

    Nothing is hard-wired: every consumer (API, CLI, tests) passes its own
    filter, and the resulting `PaceStats` always carries the exclusion list.
    """

    #: The joker lap and the pit-stop lap are mandatory in this race format and
    #: are not pace, so both are excluded by default -- see `karting.stats.events`.
    exclude_tags: frozenset[str] = frozenset(
        {"joker", "pit", "penalty", "invalid", "outlier", "boost"}
    )
    mad_k: float = 3.0
    #: Report laps without a time among the exclusions.  A lap with
    #: ``time_ms is None`` is never part of a numeric sample -- there is no
    #: number to average -- so this flag cannot keep such a lap *in*; what it
    #: controls is whether `PaceStats.excluded` lists it as a dropped lap
    #: (default) or passes over it as a lap that never existed for the metrics.
    drop_missing: bool = True
    drop_first_lap: bool = True  # first lap is the start / out-lap, not pace
    drop_slow_outliers: bool = True
    drop_fast_outliers: bool = False
    min_laps: int = 3


@dataclass(slots=True)
class LapPoint:
    """One lap as seen by the statistics layer (storage independent)."""

    lap_number: int
    time_ms: int | None
    sectors: tuple[int | None, ...] = ()
    tags: tuple[str, ...] = ()


@dataclass(slots=True)
class LapFlags:
    """Verdict of `classify_laps` for a single lap."""

    lap_number: int
    used: bool
    reason: str | None  # "missing" | "first_lap" | "tag:<tag>" | "slow_outlier" | "fast_outlier"
    suspicious_fast: bool = False


def _tag_text(tag: Any) -> str:
    """Normalise a tag to a plain string (accepts `LapTag` enum members)."""
    if isinstance(tag, Enum):
        return str(tag.value)
    return str(tag)


def median_abs_deviation(values: Sequence[float] | np.ndarray, *, scaled: bool = True) -> float:
    """Median absolute deviation of `values`; 0.0 for an empty input.

    With ``scaled=True`` the result is multiplied by `MAD_SCALE`, which turns it
    into a robust drop-in replacement for the standard deviation.
    """
    array = np.asarray(values, dtype=float)
    if array.size == 0:
        return 0.0
    mad = float(np.median(np.abs(array - float(np.median(array)))))
    return mad * MAD_SCALE if scaled else mad


def robust_scale(values: Sequence[float] | np.ndarray) -> float:
    """Robust sigma estimate of `values`, with fallbacks for a degenerate MAD.

    The MAD is 0 as soon as half of the sample shares one value -- perfectly
    possible with millisecond lap times of a very steady driver -- and a scale
    of 0 would switch outlier detection off and let an arbitrarily gross lap
    into the sample.  Two further robust estimators are tried in order before
    giving up: the interquartile range (``IQR / 1.349``) and the mean absolute
    deviation about the median (``1.2533 * MeanAD``, the estimator used by the
    modified z-score when the MAD collapses).  ``0.0`` means every value is
    identical, and only then is there genuinely no spread to measure.
    """
    array = np.asarray(values, dtype=float)
    if array.size == 0:
        return 0.0
    scaled_mad = median_abs_deviation(array)
    if scaled_mad > 0.0:
        return scaled_mad
    iqr = float(np.percentile(array, 75) - np.percentile(array, 25))
    if iqr > 0.0:
        return iqr * IQR_SCALE
    mean_ad = float(np.mean(np.abs(array - float(np.median(array)))))
    return mean_ad * MEAN_AD_SCALE if mean_ad > 0.0 else 0.0


def classify_laps(laps: Sequence[LapPoint], flt: LapFilter = LapFilter()) -> list[LapFlags]:
    """Decide, for every lap, whether it represents race pace.

    Exclusion reasons are checked in a fixed order so that the reported reason is
    the most fundamental one: ``missing`` -> ``first_lap`` -> ``tag:<tag>`` ->
    outliers.  Outlier thresholds are computed *after* the deterministic filters,
    on the surviving candidates only, so that a missing/penalised lap cannot move
    the median.

    Slow outliers (``t > median + k * scaled_MAD``) are dropped by default; fast
    outliers (``t < median - k * scaled_MAD``) are only flagged
    (`LapFlags.suspicious_fast`) because a genuinely brilliant lap and a cut
    corner look identical to the timing system -- that call belongs to a human.

    A lap without a time (``time_ms is None``) can never enter a numeric sample,
    so it is *always* excluded with reason ``"missing"``, whatever
    `LapFilter.drop_missing` says; that flag only decides whether such laps are
    listed among `PaceStats.excluded` (see `karting.stats.pace.pace_stats`).

    Degenerate inputs are handled, never raised on: an empty sequence returns an
    empty list, and a scale of exactly 0 -- reachable only when every candidate
    lap has the same time, see `robust_scale` -- disables outlier detection,
    because there is then no spread against which a lap could be an outlier.
    """
    flags: list[LapFlags] = [
        LapFlags(lap_number=int(lap.lap_number), used=True, reason=None) for lap in laps
    ]
    if not laps:
        return flags

    first_number = min(int(lap.lap_number) for lap in laps)
    for lap, flag in zip(laps, flags, strict=True):
        if lap.time_ms is None:
            # A lap without a time carries no pace information, so it can never
            # enter the numeric sample -- `drop_missing=False` cannot keep it.
            # What that flag does control is the *report*: see `pace_stats`.
            flag.used = False
            flag.reason = "missing"
            continue
        if flt.drop_first_lap and int(lap.lap_number) == first_number:
            flag.used = False
            flag.reason = "first_lap"
            continue
        excluded_tag = next(
            (_tag_text(tag) for tag in lap.tags if _tag_text(tag) in flt.exclude_tags), None
        )
        if excluded_tag is not None:
            flag.used = False
            flag.reason = f"tag:{excluded_tag}"

    candidates = [(lap, flag) for lap, flag in zip(laps, flags, strict=True) if flag.used]
    if len(candidates) < 2:
        return flags

    times = np.asarray([float(lap.time_ms) for lap, _ in candidates], dtype=float)
    median = float(np.median(times))
    spread = robust_scale(times)
    if spread <= 0.0:
        return flags

    high = median + flt.mad_k * spread
    low = median - flt.mad_k * spread
    for (_lap, flag), value in zip(candidates, times, strict=True):
        if value > high:
            if flt.drop_slow_outliers:
                flag.used = False
                flag.reason = "slow_outlier"
        elif value < low:
            flag.suspicious_fast = True
            if flt.drop_fast_outliers:
                flag.used = False
                flag.reason = "fast_outlier"
    return flags


def select_used(
    laps: Sequence[LapPoint], flt: LapFilter = LapFilter()
) -> tuple[list[LapPoint], list[LapFlags]]:
    """Return ``(kept_laps, all_flags)``; `kept_laps` keeps the input order."""
    flags = classify_laps(laps, flt)
    kept = [lap for lap, flag in zip(laps, flags, strict=True) if flag.used]
    return kept, flags
