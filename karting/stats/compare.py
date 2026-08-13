"""Head-to-head comparison of two drivers' race pace.

The tests below are the standard toolbox (Welch t, Mann-Whitney U, Levene /
Brown-Forsythe, percentile bootstrap of the median difference) plus two effect
sizes (Hedges' g, Cliff's delta).  They are deliberately shipped together with
`DriverComparison.caveats`, because the statistical assumptions they rest on do
*not* hold for laps of a single race: consecutive laps of one driver are
correlated (traffic, battles, kart differences, tyre warm-up, degradation), so a
p-value here is a heuristic flag for "worth a closer look", not an inference
about a population.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np
from scipy import stats as sps

from .outliers import LapFilter, LapFlags, LapPoint, classify_laps
from .pace import PaceStats, finite_float, pace_stats

__all__ = ["TestResult", "DriverComparison", "compare_drivers", "dominance_shares"]

#: Below this many usable laps per driver the comparison is called out as small.
SMALL_SAMPLE: int = 8
#: Ratio of sample sizes above which the imbalance is called out.
IMBALANCE_RATIO: float = 1.5
#: Conventional alpha, used only to word the interpretations.
ALPHA: float = 0.05
#: The tests reported by `compare_drivers`, always in this order.
TEST_NAMES: tuple[str, ...] = (
    "welch_t",
    "mann_whitney_u",
    "levene_brown_forsythe",
    "bootstrap_median_diff",
)

INDEPENDENCE_CAVEAT: str = (
    "Круги одной гонки не являются независимыми наблюдениями: трафик, борьба, разница "
    "между картами, прогрев резины и деградация связывают соседние круги и связывают "
    "двух пилотов друг с другом. Все тесты ниже предполагают независимые наблюдения, "
    "поэтому p-значения и доверительные интервалы стоит трактовать как повод "
    "присмотреться, а не как строгий вывод."
)


@dataclass(slots=True)
class TestResult:
    """One statistical test or interval estimate, with a plain-language reading."""

    name: str
    statistic: float | None
    p_value: float | None
    ci_low: float | None = None
    ci_high: float | None = None
    effect_size: float | None = None
    effect_name: str | None = None
    interpretation: str = ""


@dataclass(slots=True)
class DriverComparison:
    """Result of `compare_drivers`; positive differences mean A is slower."""

    driver_a: str
    driver_b: str
    stats_a: PaceStats
    stats_b: PaceStats
    n_a: int
    n_b: int
    mean_diff_ms: float | None
    median_diff_ms: float | None
    tests: list[TestResult] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Effect sizes
# --------------------------------------------------------------------------- #


def hedges_g(a: np.ndarray, b: np.ndarray) -> float | None:
    """Bias-corrected standardised mean difference (a - b), or `None`."""
    n_a, n_b = a.size, b.size
    if n_a < 2 or n_b < 2:
        return None
    var_a = float(np.var(a, ddof=1))
    var_b = float(np.var(b, ddof=1))
    pooled = ((n_a - 1) * var_a + (n_b - 1) * var_b) / (n_a + n_b - 2)
    if pooled <= 0.0:
        return None
    cohen_d = (float(np.mean(a)) - float(np.mean(b))) / float(np.sqrt(pooled))
    correction = 1.0 - 3.0 / (4.0 * (n_a + n_b) - 9.0)
    return finite_float(cohen_d * correction)


def dominance_shares(a: np.ndarray, b: np.ndarray) -> tuple[float, float, float] | None:
    """``(P(a > b), P(a < b), P(a == b))`` over all pairs, or `None` if empty."""
    if a.size == 0 or b.size == 0:
        return None
    diff = a[:, None] - b[None, :]
    pairs = float(a.size * b.size)
    greater = int(np.count_nonzero(diff > 0)) / pairs
    smaller = int(np.count_nonzero(diff < 0)) / pairs
    return greater, smaller, 1.0 - greater - smaller


def cliffs_delta(a: np.ndarray, b: np.ndarray) -> float | None:
    """P(a > b) - P(a < b) in [-1, 1]; non-parametric, ties count as neither."""
    shares = dominance_shares(a, b)
    if shares is None:
        return None
    greater, smaller, _ties = shares
    return finite_float(greater - smaller)


def _magnitude(value: float, thresholds: tuple[float, float, float]) -> str:
    """Map |value| onto negligible/small/medium/large via `thresholds`."""
    magnitude = abs(value)
    small, medium, large = thresholds
    if magnitude < small:
        return "negligible"
    if magnitude < medium:
        return "small"
    if magnitude < large:
        return "medium"
    return "large"


def _significance(p_value: float | None) -> str:
    if p_value is None:
        return "p-значение недоступно"
    return (
        f"p={p_value:.4g} (меньше alpha={ALPHA})"
        if p_value < ALPHA
        else f"p={p_value:.4g} (не меньше alpha={ALPHA})"
    )


def _unavailable(name: str, reason: str) -> TestResult:
    """A test that could not be run, reported instead of silently omitted."""
    return TestResult(name=name, statistic=None, p_value=None, interpretation=f"Не выполнен: {reason}.")


# --------------------------------------------------------------------------- #
# Individual tests
# --------------------------------------------------------------------------- #


def _both_constant(a: np.ndarray, b: np.ndarray) -> bool:
    return float(np.ptp(a)) == 0.0 and float(np.ptp(b)) == 0.0


def _welch(a: np.ndarray, b: np.ndarray, name_a: str, name_b: str) -> TestResult:
    if _both_constant(a, b):
        diff = float(np.mean(a)) - float(np.mean(b))
        if diff == 0.0:
            return _unavailable(
                "welch_t",
                "обе выборки постоянны и одинаковы, проверять нечего",
            )
        # Zero variance on both sides makes the t statistic undefined (0/0), but
        # the comparison itself is not: the two constant values simply differ.
        faster = name_b if diff > 0 else name_a
        return TestResult(
            name="welch_t",
            statistic=None,
            p_value=None,
            interpretation=(
                f"Не выполнен: обе выборки постоянны, поэтому t-статистика не определена "
                f"(zero variance in both groups). The two constant lap times still "
                f"— значения различаются ровно на {diff:+.0f} мс ({name_a} минус {name_b}), "
                f"то есть {faster} быстрее. Это факт, а не статистический вывод."
            ),
        )
    result = sps.ttest_ind(a, b, equal_var=False)
    interval = result.confidence_interval(confidence_level=1.0 - ALPHA)
    p_value = finite_float(result.pvalue)
    diff = float(np.mean(a)) - float(np.mean(b))
    effect = hedges_g(a, b)
    effect_text = (
        f"размер эффекта Hedges' g={effect:+.2f} ({_magnitude(effect, (0.2, 0.5, 0.8))})"
        if effect is not None
        else "размер эффекта Hedges' g недоступен (нулевая объединённая дисперсия)"
    )
    faster = name_b if diff > 0 else name_a
    return TestResult(
        name="welch_t",
        statistic=finite_float(result.statistic),
        p_value=p_value,
        ci_low=finite_float(interval.low),
        ci_high=finite_float(interval.high),
        effect_size=effect,
        effect_name="hedges_g",
        interpretation=(
            f"Тест Уэлча по среднему времени круга (дисперсии могут различаться): "
            f"{name_a} минус {name_b} = {diff:+.0f} мс, "
            f"95% ДИ [{interval.low:+.0f}, {interval.high:+.0f}] мс, "
            f"t={result.statistic:+.2f}, {_significance(p_value)}. "
            + (
                f"По среднему темпу быстрее {faster}; {effect_text}."
                if p_value is not None and p_value < ALPHA
                else f"Явной разницы средних на фоне разброса нет; {effect_text}."
            )
        ),
    )


def _mann_whitney(a: np.ndarray, b: np.ndarray, name_a: str, name_b: str) -> TestResult:
    result = sps.mannwhitneyu(a, b, alternative="two-sided")
    p_value = finite_float(result.pvalue)
    shares = dominance_shares(a, b)
    delta = None if shares is None else finite_float(shares[0] - shares[1])
    if shares is not None and delta is not None:
        # P(a > b) comes straight from the pair count. Deriving it from delta as
        # (delta + 1) / 2 silently splits the tied pairs between both drivers and
        # overstates the share by half the tie mass.
        strictly_slower, _, ties = shares
        delta_text = (
            f"Cliff's delta={delta:+.2f} ({_magnitude(delta, (0.147, 0.33, 0.474))}): "
            f"случайный круг {name_a} строго медленнее случайного круга {name_b} "
            f"в {strictly_slower * 100:.0f}% случаев"
            + (f" (точных совпадений среди пар: {ties * 100:.0f}%)" if ties > 0.0 else "")
        )
    else:
        delta_text = "Cliff's delta недоступна"
    return TestResult(
        name="mann_whitney_u",
        statistic=finite_float(result.statistic),
        p_value=p_value,
        effect_size=delta,
        effect_name="cliffs_delta",
        interpretation=(
            f"U-критерий Манна — Уитни (двусторонний, ранговый, без предположения о нормальности): "
            f"U={result.statistic:.1f}, {_significance(p_value)}. "
            + (
                "Распределения времён круга сдвинуты друг относительно друга. "
                if p_value is not None and p_value < ALPHA
                else "Заметного сдвига между распределениями времён круга нет. "
            )
            + delta_text
            + "."
        ),
    )


def _levene(a: np.ndarray, b: np.ndarray, name_a: str, name_b: str) -> TestResult:
    if _both_constant(a, b):
        return TestResult(
            name="levene_brown_forsythe",
            statistic=None,
            p_value=None,
            interpretation=(
                f"Не выполнен: у {name_a} и {name_b} нулевой разброс времён круга, поэтому "
                f"статистика теста не определена. Разбросы одинаковы (оба 0 мс) "
                f"по построению; различается только уровень."
            ),
        )
    result = sps.levene(a, b, center="median")
    p_value = finite_float(result.pvalue)
    std_a = float(np.std(a, ddof=1))
    std_b = float(np.std(b, ddof=1))
    steadier = name_a if std_a < std_b else name_b
    return TestResult(
        name="levene_brown_forsythe",
        statistic=finite_float(result.statistic),
        p_value=p_value,
        interpretation=(
            f"Тест Левене / Брауна — Форсайта на равенство разброса (центрирован по медиане): "
            f"W={result.statistic:.2f}, {_significance(p_value)}. "
            f"СКО времён круга: {name_a} {std_a:.0f} мс против {name_b} {std_b:.0f} мс. "
            + (
                f"Стабильность различается; в этой сессии ровнее едет {steadier}."
                if p_value is not None and p_value < ALPHA
                else "Свидетельств разницы в стабильности нет."
            )
        ),
    )


def _bootstrap_median_diff(
    a: np.ndarray, b: np.ndarray, name_a: str, name_b: str, n_boot: int, seed: int
) -> TestResult:
    """Percentile bootstrap CI for median(a) - median(b), reproducible per seed."""
    observed = float(np.median(a)) - float(np.median(b))
    if n_boot < 1:
        return TestResult(
            name="bootstrap_median_diff",
            statistic=observed,
            p_value=None,
            interpretation=(
                f"Наблюдаемая разность медиан {name_a} минус {name_b}: "
                f"{observed:+.0f} мс. Доверительный интервал не построен: n_boot={n_boot}."
            ),
        )
    rng = np.random.default_rng(seed)
    draws_a = rng.integers(0, a.size, size=(n_boot, a.size))
    draws_b = rng.integers(0, b.size, size=(n_boot, b.size))
    diffs = np.median(a[draws_a], axis=1) - np.median(b[draws_b], axis=1)
    low, high = (float(value) for value in np.percentile(diffs, [2.5, 97.5]))
    excludes_zero = (low > 0.0) or (high < 0.0)
    return TestResult(
        name="bootstrap_median_diff",
        statistic=observed,
        p_value=None,
        ci_low=low,
        ci_high=high,
        interpretation=(
            f"Перцентильный бутстрэп ({n_boot} ресэмплов, seed={seed}) разности медиан "
            f"времён круга {name_a} минус {name_b}: {observed:+.0f} мс, "
            f"95% ДИ [{low:+.0f}, {high:+.0f}] мс. "
            + (
                "Интервал не включает 0, поэтому разрыв медиан устойчив к ресэмплингу."
                if excludes_zero
                else "Интервал включает 0, поэтому разрыв медиан не выходит за шум ресэмплинга."
            )
            + " Ресэмплинг кругов по отдельности игнорирует их связь внутри гонки, "
            "поэтому интервал получается оптимистично узким."
        ),
    )


# --------------------------------------------------------------------------- #
# Caveats
# --------------------------------------------------------------------------- #


def _fast_lap_numbers(flags: Sequence[LapFlags]) -> list[int]:
    return [flag.lap_number for flag in flags if flag.suspicious_fast]


def _dropped(flags: Sequence[LapFlags], reason: str) -> list[int]:
    return [flag.lap_number for flag in flags if flag.reason == reason]


def _build_caveats(
    name_a: str,
    name_b: str,
    stats_a: PaceStats,
    stats_b: PaceStats,
    flags_a: Sequence[LapFlags],
    flags_b: Sequence[LapFlags],
    n_a: int,
    n_b: int,
    flt: LapFilter,
    levene: TestResult | None,
) -> list[str]:
    """Honest reading instructions for the numbers above."""
    caveats: list[str] = [INDEPENDENCE_CAVEAT]

    for name, count in ((name_a, n_a), (name_b, n_b)):
        if count < flt.min_laps:
            caveats.append(
                f"У {name} всего {count} зачётных кругов, меньше min_laps={flt.min_laps}: "
                f"метрики темпа и тесты помечены как недоступные, а не додуманы."
            )
    if 0 < min(n_a, n_b) < SMALL_SAMPLE:
        caveats.append(
            f"Малая выборка: {name_a} n={n_a}, {name_b} n={n_b}. На таком числе кругов тесты "
            f"улавливают только крупные различия, а доверительные интервалы широки; "
            f"отсутствие значимости не означает равенства темпа."
        )
    if min(n_a, n_b) > 0 and max(n_a, n_b) / min(n_a, n_b) >= IMBALANCE_RATIO:
        caveats.append(
            f"Несбалансированные выборки ({name_a} n={n_a} против {name_b} n={n_b}): у пилота с "
            f"меньшим числом зачётных кругов оценка менее надёжна, а сами исключения могут "
            f"быть неслучайны (инциденты, питы)."
        )

    for name, flags in ((name_a, flags_a), (name_b, flags_b)):
        fast = _fast_lap_numbers(flags)
        if fast:
            kept = "оставлены в выборке" if not flt.drop_fast_outliers else "удалены"
            caveats.append(
                f"У {name} есть подозрительно быстрые круги {fast} (ниже медианы на "
                f"{flt.mad_k:g} × масштабированный MAD), текущим фильтром {kept}. Такой круг может "
                f"быть чистым кругом без трафика, слипстримом, срезкой или сбоем тайминга — "
                f"разметьте их, прежде чем доверять сравнению."
            )
        slow = _dropped(flags, "slow_outlier")
        total = len(flags)
        if slow and total and len(slow) / total > 0.2:
            caveats.append(
                f"{name}: {len(slow)} из {total} кругов отброшены как медленные выбросы {slow}. "
                f"Оставшаяся выборка описывает только темп в чистом воздухе и занижает "
                f"реальное время пилота в гонке."
            )

    for name, stats in ((name_a, stats_a), (name_b, stats_b)):
        slope = stats.degradation_ms_per_lap
        p_value = stats.degradation_p_value
        if slope is not None and p_value is not None and p_value < ALPHA:
            direction = "замедлялся" if slope > 0 else "ускорялся"
            caveats.append(
                f"У {name} значимый тренд времени круга ({slope:+.0f} мс на круг, "
                f"p={p_value:.4g}): по ходу отрезка {direction}. Значит выборки темпа не "
                f"одинаково распределены по гонке, и одно среднее или медиана скрывают, "
                f"когда именно каждый был быстр."
            )

    if levene is not None and levene.p_value is not None and levene.p_value < ALPHA:
        caveats.append(
            "У пилотов различается разброс времён круга, поэтому разницу средних могут создавать "
            "несколько кругов; надёжнее смотреть на медианные и ранговые результаты."
        )
    return caveats


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def compare_drivers(
    a: Sequence[LapPoint],
    b: Sequence[LapPoint],
    *,
    name_a: str = "A",
    name_b: str = "B",
    flt: LapFilter = LapFilter(),
    n_boot: int = 10000,
    seed: int = 12345,
) -> DriverComparison:
    """Compare the race pace of two drivers over the laps that survive `flt`.

    Differences are oriented ``A - B``: a positive value means A's lap times are
    higher, i.e. A is slower.  Tests that cannot be run (empty or too small a
    sample) are returned with `None` statistics and an explaining
    `TestResult.interpretation` instead of being dropped, and `caveats` always
    starts with the within-race dependence warning.
    """
    stats_a = pace_stats(a, flt)
    stats_b = pace_stats(b, flt)
    flags_a = classify_laps(a, flt)
    flags_b = classify_laps(b, flt)

    times_a = np.asarray(
        [float(lap.time_ms) for lap, flag in zip(a, flags_a, strict=True) if flag.used], dtype=float
    )
    times_b = np.asarray(
        [float(lap.time_ms) for lap, flag in zip(b, flags_b, strict=True) if flag.used], dtype=float
    )
    n_a, n_b = int(times_a.size), int(times_b.size)

    threshold = max(2, flt.min_laps)
    runnable = n_a >= threshold and n_b >= threshold
    tests: list[TestResult] = []
    levene: TestResult | None = None

    if runnable:
        tests.append(_welch(times_a, times_b, name_a, name_b))
        tests.append(_mann_whitney(times_a, times_b, name_a, name_b))
        levene = _levene(times_a, times_b, name_a, name_b)
        tests.append(levene)
        tests.append(_bootstrap_median_diff(times_a, times_b, name_a, name_b, n_boot, seed))
    else:
        reason = (
            f"{name_a} has {n_a} and {name_b} has {n_b} usable laps, "
            f"at least {threshold} are required per driver"
        )
        tests = [_unavailable(name, reason) for name in TEST_NAMES]

    # Plain descriptive differences: they carry no distributional assumption, so
    # they are reported whenever both operands exist, even when the sample is too
    # small for any test to run (`caveats` says so).
    mean_diff: float | None = None
    median_diff: float | None = None
    if stats_a.mean_ms is not None and stats_b.mean_ms is not None:
        mean_diff = float(stats_a.mean_ms) - float(stats_b.mean_ms)
    if stats_a.median_ms is not None and stats_b.median_ms is not None:
        median_diff = float(stats_a.median_ms) - float(stats_b.median_ms)

    return DriverComparison(
        driver_a=name_a,
        driver_b=name_b,
        stats_a=stats_a,
        stats_b=stats_b,
        n_a=n_a,
        n_b=n_b,
        mean_diff_ms=mean_diff,
        median_diff_ms=median_diff,
        tests=tests,
        caveats=_build_caveats(
            name_a, name_b, stats_a, stats_b, flags_a, flags_b, n_a, n_b, flt, levene
        ),
    )
