"""Descriptive pace metrics for one driver in one session.

All functions are pure and operate on `LapPoint` sequences.  Every numeric field
of `PaceStats` is a plain Python `int`/`float`/`bool` (never a numpy scalar), so
`dataclasses.asdict` output goes straight into `json.dumps` / FastAPI.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import numpy as np
from scipy import stats as sps

from .outliers import LapFilter, LapFlags, LapPoint, classify_laps, median_abs_deviation

__all__ = ["PaceStats", "pace_stats", "pace_delta_to_best_driver", "finite_float"]

#: Fraction trimmed from *each* tail by `PaceStats.trimmed_mean_ms`.
TRIM_PROPORTION: float = 0.10
#: Below this many laps nothing can be trimmed without discarding a large part
#: of an already tiny sample, so `PaceStats.trimmed_mean_ms` is `None` instead of
#: a copy of the plain mean dressed up as a robust estimator.
MIN_TRIM_LAPS: int = 5


@dataclass(slots=True)
class PaceStats:
    """Descriptive statistics of a driver's race pace.

    Every metric is computed on the *used* laps only (see `classify_laps`).
    When fewer than `LapFilter.min_laps` laps survive filtering, all metrics are
    `None`: reporting a "median" of two laps would be a number without meaning.
    """

    n_laps: int
    n_used: int
    best_ms: int | None
    median_ms: float | None
    mean_ms: float | None
    trimmed_mean_ms: float | None
    std_ms: float | None
    iqr_ms: float | None
    mad_ms: float | None
    cv: float | None
    consistency: float | None
    theoretical_best_ms: int | None
    degradation_ms_per_lap: float | None
    degradation_p_value: float | None
    used_lap_numbers: list[int] = field(default_factory=list)
    excluded: list[LapFlags] = field(default_factory=list)


def finite_float(value: float | np.floating) -> float | None:
    """Cast a numpy/py scalar to a plain finite `float`, or `None`."""
    number = float(value)
    return number if math.isfinite(number) else None


def _theoretical_best(laps: Sequence[LapPoint]) -> int | None:
    """Sum of the best time seen in each sector across `laps`.

    Returns `None` unless every lap carries sector data of the same width and
    every sector has at least one recorded value -- an "ideal lap" assembled from
    a partially known set of sectors would be a fantasy, not a lower bound.
    """
    if not laps:
        return None
    widths = {len(lap.sectors) for lap in laps}
    if len(widths) != 1:
        return None
    width = widths.pop()
    if width == 0:
        return None

    total = 0
    for index in range(width):
        values = [lap.sectors[index] for lap in laps if lap.sectors[index] is not None]
        if not values:
            return None
        total += int(min(values))
    return total


def _trimmed_mean(times: np.ndarray) -> float | None:
    """Symmetric trimmed mean, or `None` when the sample is too small to trim.

    `scipy.stats.trim_mean` cuts ``int(n * proportion)`` values from each tail,
    which is 0 for every ``n <= 9``: a "10% trimmed mean" of a nine-lap heat is
    the plain mean under another name.  Here at least one lap is cut from each
    tail as soon as the sample reaches `MIN_TRIM_LAPS`, and below that the
    metric is reported as unavailable rather than as a duplicate of `mean_ms`.
    For ``n >= 10`` this is exactly `scipy.stats.trim_mean(times, 0.10)`.
    """
    size = int(times.size)
    if size < MIN_TRIM_LAPS:
        return None
    cut = max(1, int(size * TRIM_PROPORTION))
    if size - 2 * cut < 1:
        return None
    kept = np.sort(times)[cut : size - cut]
    return finite_float(np.mean(kept))


def _degradation(
    lap_numbers: Sequence[int], times: np.ndarray
) -> tuple[float | None, float | None]:
    """OLS slope of ``lap time ~ lap number`` in ms per lap, with its p-value.

    `None` for fewer than three laps (a two-point "trend" is just a line through
    both points) or when every lap shares the same lap number.
    """
    if len(lap_numbers) < 3:
        return None, None
    x = np.asarray(lap_numbers, dtype=float)
    if float(np.ptp(x)) == 0.0:
        return None, None
    regression = sps.linregress(x, times)
    return finite_float(regression.slope), finite_float(regression.pvalue)


def pace_stats(laps: Sequence[LapPoint], flt: LapFilter = LapFilter()) -> PaceStats:
    """Descriptive pace metrics over the laps that survive `flt`.

    Handles degenerate inputs without raising: an empty sequence, a single lap or
    a sample smaller than `LapFilter.min_laps` all yield a `PaceStats` whose
    metrics are `None` while `n_laps`, `n_used` and `excluded` still describe
    exactly what happened.

    `excluded` lists every lap kept out of the sample.  Laps that carry no time
    at all are listed with reason ``"missing"`` unless `LapFilter.drop_missing`
    is `False`, in which case they are passed over silently: they were never
    candidates for a pace metric in the first place.
    """
    flags = classify_laps(laps, flt)
    used = [lap for lap, flag in zip(laps, flags, strict=True) if flag.used]
    used_lap_numbers = [int(lap.lap_number) for lap in used]
    excluded = [
        flag
        for flag in flags
        if not flag.used and (flt.drop_missing or flag.reason != "missing")
    ]
    n_used = len(used)

    result = PaceStats(
        n_laps=len(laps),
        n_used=n_used,
        best_ms=None,
        median_ms=None,
        mean_ms=None,
        trimmed_mean_ms=None,
        std_ms=None,
        iqr_ms=None,
        mad_ms=None,
        cv=None,
        consistency=None,
        theoretical_best_ms=None,
        degradation_ms_per_lap=None,
        degradation_p_value=None,
        used_lap_numbers=used_lap_numbers,
        excluded=excluded,
    )
    if n_used == 0 or n_used < flt.min_laps:
        return result

    times = np.asarray([float(lap.time_ms) for lap in used], dtype=float)
    median = float(np.median(times))
    mean = float(np.mean(times))
    std = float(np.std(times, ddof=1)) if n_used >= 2 else None
    slope, p_value = _degradation(used_lap_numbers, times)

    result.best_ms = int(np.min(times))
    result.median_ms = median
    result.mean_ms = mean
    result.trimmed_mean_ms = _trimmed_mean(times)
    result.std_ms = std
    result.iqr_ms = float(np.percentile(times, 75) - np.percentile(times, 25))
    result.mad_ms = median_abs_deviation(times, scaled=False)
    result.cv = (std / mean) if (std is not None and mean != 0.0) else None
    result.consistency = (std / median) if (std is not None and median != 0.0) else None
    result.theoretical_best_ms = _theoretical_best(used)
    result.degradation_ms_per_lap = slope
    result.degradation_p_value = p_value
    return result


def pace_delta_to_best_driver(
    stats_by_driver: Mapping[str, PaceStats], *, metric: str = "mean_ms"
) -> dict[str, float | None]:
    """Per-driver gap (ms, >= 0) between their pace and the best driver's pace.

    The default estimator is the **mean**, not the median, because in a race the
    mean is the quantity with a physical meaning: total time over a stint is
    exactly ``n * mean``, so a gap of 0.4 s per lap over 90 laps is 36 seconds of
    real race time.  The median answers a different question -- "what does a
    typical lap look like" -- and no sum of medians equals anything a stopwatch
    measured.  The median stays available and is reported next to the mean: the
    two part company precisely when the distribution is skewed, which is itself
    worth seeing.

    Cross-driver by nature, so it lives next to `PaceStats` rather than inside it
    (the `PaceStats` shape is fixed by the module contract).  `metric` selects the
    pace estimator: ``"mean_ms"`` (default), ``"median_ms"``, ``"trimmed_mean_ms"``
    or ``"best_ms"``.  Drivers without that metric get `None`.
    """
    if metric not in {"median_ms", "mean_ms", "trimmed_mean_ms", "best_ms"}:
        raise ValueError(f"unsupported pace metric: {metric!r}")

    values = {
        name: getattr(stats, metric)
        for name, stats in stats_by_driver.items()
        if getattr(stats, metric) is not None
    }
    if not values:
        return {name: None for name in stats_by_driver}
    reference = float(min(values.values()))
    return {
        name: (float(values[name]) - reference if name in values else None)
        for name in stats_by_driver
    }
