"""Tests for `karting.stats`: filtering, pace metrics and driver comparison.

Most cases use synthetic laps with a known answer (a known shift, a known
trend, a known set of dirty laps); the last block is a smoke test on the real
Final A data from the hand-checked fixture.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from types import NoneType
from typing import Any

import numpy as np
import pytest

from karting.stats import (
    DriverComparison,
    LapFilter,
    LapFlags,
    LapPoint,
    PaceStats,
    classify_laps,
    cliffs_delta,
    compare_drivers,
    hedges_g,
    median_abs_deviation,
    pace_delta_to_best_driver,
    pace_stats,
    select_used,
)
from karting.stats import TestResult as StatTestResult  # aliased: pytest must not collect it

FIXTURE = Path(__file__).parent / "fixtures" / "final_a_expected.json"
ALL_LAPS = LapFilter(drop_first_lap=False)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def to_ms(text: str | None) -> int | None:
    """Local copy of the duration parser (tests must not depend on the parser)."""
    if text is None:
        return None
    minutes, _, rest = text.partition(":")
    if rest:
        seconds, _, fraction = rest.partition(".")
        return (int(minutes) * 60 + int(seconds)) * 1000 + int(fraction.ljust(3, "0"))
    seconds, _, fraction = text.partition(".")
    return int(seconds) * 1000 + int(fraction.ljust(3, "0"))


def make_laps(
    times: Sequence[int | None],
    *,
    start: int = 1,
    tags: dict[int, tuple[str, ...]] | None = None,
    sectors: Sequence[tuple[int | None, ...]] | None = None,
) -> list[LapPoint]:
    """Build `LapPoint`s numbered `start, start+1, ...` from raw times."""
    tags = tags or {}
    return [
        LapPoint(
            lap_number=start + index,
            time_ms=time,
            sectors=sectors[index] if sectors is not None else (),
            tags=tags.get(start + index, ()),
        )
        for index, time in enumerate(times)
    ]


def flags_by_lap(flags: Sequence[LapFlags]) -> dict[int, LapFlags]:
    return {flag.lap_number: flag for flag in flags}


def assert_plain_json_types(value: Any, path: str = "$") -> None:
    """Fail on any numpy scalar / exotic type hiding inside an asdict() result."""
    allowed = (bool, int, float, str, NoneType)
    if isinstance(value, dict):
        for key, item in value.items():
            assert type(key) is str, f"{path}: non-str key {key!r} of type {type(key)}"
            assert_plain_json_types(item, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            assert_plain_json_types(item, f"{path}[{index}]")
        return
    assert type(value) in allowed, f"{path}: {value!r} has non-builtin type {type(value)}"
    if isinstance(value, float):
        assert math.isfinite(value), f"{path}: non-finite float {value!r}"


def normal_laps(loc: float, scale: float, n: int, seed: int) -> list[LapPoint]:
    rng = np.random.default_rng(seed)
    return make_laps([int(round(value)) for value in rng.normal(loc, scale, n)])


# --------------------------------------------------------------------------- #
# classify_laps
# --------------------------------------------------------------------------- #


def test_missing_first_lap_and_tags_are_excluded_with_reasons() -> None:
    laps = make_laps(
        [28500, 28000, 28100, None, 28050, 28020],
        tags={3: ("penalty",), 2: ("traffic",), 5: ("clean", "pit")},
    )
    flags = flags_by_lap(classify_laps(laps))

    assert (flags[1].used, flags[1].reason) == (False, "first_lap")
    assert (flags[3].used, flags[3].reason) == (False, "tag:penalty")
    assert (flags[4].used, flags[4].reason) == (False, "missing")
    assert (flags[5].used, flags[5].reason) == (False, "tag:pit")
    # "traffic" is not in the default exclude set: informative, not disqualifying.
    assert (flags[2].used, flags[2].reason) == (True, None)
    assert (flags[6].used, flags[6].reason) == (True, None)


def test_untimed_first_lap_is_reported_as_missing_and_lap_two_survives() -> None:
    """Apex sends lap 1 as "-"; the first *timed* lap is a normal racing lap."""
    flags = flags_by_lap(classify_laps(make_laps([None, 28000, 28100, 27950, 28050])))
    assert (flags[1].used, flags[1].reason) == (False, "missing")
    assert flags[2].used is True


def test_first_lap_is_the_lowest_lap_number_and_flag_can_be_switched_off() -> None:
    laps = make_laps([28000, 28100, 27950], start=5)
    assert flags_by_lap(classify_laps(laps))[5].reason == "first_lap"
    assert all(flag.used for flag in classify_laps(laps, ALL_LAPS))


def test_missing_lap_never_enters_the_sample() -> None:
    laps = make_laps([28000, 28100, None, 27950])
    flags = flags_by_lap(classify_laps(laps, LapFilter(drop_first_lap=False, drop_missing=False)))
    assert (flags[3].used, flags[3].reason) == (False, "missing")


def test_custom_exclude_tags_are_honoured() -> None:
    laps = make_laps([28000, 28100, 27950], tags={2: ("traffic",)})
    flt = LapFilter(exclude_tags=frozenset({"traffic"}), drop_first_lap=False)
    assert flags_by_lap(classify_laps(laps, flt))[2].reason == "tag:traffic"


def test_lap_tag_enum_members_are_accepted_as_tags() -> None:
    from karting.models import LapTag

    laps = make_laps([28000, 28100, 27950], tags={2: (LapTag.PENALTY,)})
    assert flags_by_lap(classify_laps(laps, ALL_LAPS))[2].reason == "tag:penalty"


def test_default_filter_matches_the_contract() -> None:
    flt = LapFilter()
    # SPEC 10.4: the mandatory joker lap joins the default exclusions, because it
    # is a shortcut and not a pace lap (it is the "official best lap" of five of
    # the six drivers of the reference race).
    assert flt.exclude_tags == frozenset(
        {"joker", "pit", "penalty", "invalid", "outlier", "boost"}
    )
    assert (flt.mad_k, flt.min_laps) == (3.0, 3)
    assert (flt.drop_missing, flt.drop_first_lap) == (True, True)
    assert (flt.drop_slow_outliers, flt.drop_fast_outliers) == (True, False)
    assert LapFilter() == LapFilter() and hash(LapFilter()) == hash(flt)  # frozen / hashable


def test_slow_outlier_dropped_fast_outlier_only_flagged() -> None:
    times = [28000, 28050, 27980, 28010, 27990, 28030, 41000, 25000]
    flags = flags_by_lap(classify_laps(make_laps(times), ALL_LAPS))

    assert (flags[7].used, flags[7].reason) == (False, "slow_outlier")
    assert flags[7].suspicious_fast is False
    # Fast anomaly stays in the sample but is marked for human review.
    assert (flags[8].used, flags[8].reason) == (True, None)
    assert flags[8].suspicious_fast is True
    assert all(not flags[n].suspicious_fast for n in range(1, 7))


def test_fast_outlier_dropped_when_requested() -> None:
    times = [28000, 28050, 27980, 28010, 27990, 28030, 25000]
    flt = LapFilter(drop_first_lap=False, drop_fast_outliers=True)
    flag = flags_by_lap(classify_laps(make_laps(times), flt))[7]
    assert (flag.used, flag.reason, flag.suspicious_fast) == (False, "fast_outlier", True)


def test_mad_k_controls_the_threshold() -> None:
    times = [28000, 28100, 28200, 28300, 28400, 29500]
    laps = make_laps(times)
    strict = flags_by_lap(classify_laps(laps, LapFilter(drop_first_lap=False, mad_k=1.0)))
    loose = flags_by_lap(classify_laps(laps, LapFilter(drop_first_lap=False, mad_k=10.0)))
    assert strict[6].reason == "slow_outlier"
    assert loose[6].used is True


def test_zero_mad_does_not_divide_by_zero_or_drop_anyone() -> None:
    laps = make_laps([28000] * 8)
    flags = classify_laps(laps, ALL_LAPS)
    assert all(flag.used and flag.reason is None for flag in flags)
    assert median_abs_deviation([28000] * 8) == 0.0

    # More than half identical: MAD is still 0, so detection must stay disabled.
    mixed = make_laps([28000, 28000, 28000, 28000, 28000, 30000, 26000])
    assert all(flag.used for flag in classify_laps(mixed, ALL_LAPS))


def test_degenerate_inputs_do_not_raise() -> None:
    assert classify_laps([]) == []
    assert classify_laps(make_laps([28000])) == [LapFlags(1, False, "first_lap")]
    assert classify_laps(make_laps([28000]), ALL_LAPS) == [LapFlags(1, True, None)]
    assert classify_laps(make_laps([None, None])) == [
        LapFlags(1, False, "missing"),
        LapFlags(2, False, "missing"),
    ]


def test_select_used_keeps_order() -> None:
    laps = make_laps([None, 28000, 27900, 28100])
    kept, flags = select_used(laps, ALL_LAPS)
    assert [lap.lap_number for lap in kept] == [2, 3, 4]
    assert len(flags) == 4


# --------------------------------------------------------------------------- #
# pace_stats
# --------------------------------------------------------------------------- #


def test_pace_stats_known_values() -> None:
    times = [28000, 28100, 28200, 28300, 28400]
    stats = pace_stats(make_laps(times), ALL_LAPS)

    assert (stats.n_laps, stats.n_used) == (5, 5)
    assert stats.used_lap_numbers == [1, 2, 3, 4, 5]
    assert stats.excluded == []
    assert stats.best_ms == 28000
    assert stats.median_ms == pytest.approx(28200.0)
    assert stats.mean_ms == pytest.approx(28200.0)
    assert stats.trimmed_mean_ms == pytest.approx(28200.0)
    assert stats.std_ms == pytest.approx(float(np.std(times, ddof=1)))
    assert stats.iqr_ms == pytest.approx(200.0)
    assert stats.mad_ms == pytest.approx(100.0)
    assert stats.cv == pytest.approx(stats.std_ms / stats.mean_ms)
    assert stats.consistency == pytest.approx(stats.std_ms / stats.median_ms)
    assert stats.theoretical_best_ms is None


def test_trimmed_mean_ignores_the_extremes() -> None:
    # The outlier filter is off here: this case is about the trimming itself.
    keep_all = LapFilter(drop_first_lap=False, drop_slow_outliers=False)
    times = [27000] + [28000] * 18 + [29000]
    stats = pace_stats(make_laps(times), keep_all)
    assert stats.mean_ms == pytest.approx(28000.0)
    # 10% trimmed from each side removes both tails of this symmetric sample.
    assert stats.trimmed_mean_ms == pytest.approx(28000.0)

    skewed = pace_stats(make_laps([28000] * 19 + [40000]), keep_all)
    assert skewed.mean_ms == pytest.approx(28600.0)
    assert skewed.trimmed_mean_ms == pytest.approx(28000.0)


def test_pace_stats_uses_only_clean_laps() -> None:
    laps = make_laps(
        [None, 28000, 28100, 27900, 45000, 28050],
        tags={6: ("boost",)},
    )
    stats = pace_stats(laps)
    assert stats.n_laps == 6
    assert stats.used_lap_numbers == [2, 3, 4]
    reasons = {flag.lap_number: flag.reason for flag in stats.excluded}
    assert reasons == {1: "missing", 5: "slow_outlier", 6: "tag:boost"}


def test_below_min_laps_returns_empty_metrics_not_an_exception() -> None:
    stats = pace_stats(make_laps([28000, 28100, 28050]))  # lap 1 dropped -> n_used = 2
    assert (stats.n_laps, stats.n_used) == (3, 2)
    assert stats.used_lap_numbers == [2, 3]
    assert stats.best_ms is None
    assert stats.median_ms is None
    assert stats.degradation_ms_per_lap is None


def test_empty_and_single_lap_inputs() -> None:
    empty = pace_stats([])
    assert (empty.n_laps, empty.n_used) == (0, 0)
    assert empty.median_ms is None and empty.excluded == []

    single = pace_stats(make_laps([28123]), LapFilter(drop_first_lap=False, min_laps=1))
    assert (single.n_used, single.best_ms) == (1, 28123)
    assert single.median_ms == pytest.approx(28123.0)
    assert single.std_ms is None  # sample SD of one lap is undefined, not 0
    assert single.cv is None and single.consistency is None
    assert single.iqr_ms == pytest.approx(0.0)
    assert single.degradation_ms_per_lap is None


def test_theoretical_best_sums_the_best_sectors() -> None:
    sectors = [(14000, 14100), (13900, 14200), (14050, 13800)]
    stats = pace_stats(make_laps([28100, 28100, 27850], sectors=sectors), ALL_LAPS)
    assert stats.theoretical_best_ms == 13900 + 13800

    # A used lap without sectors makes the ideal lap unknowable.
    partial = pace_stats(
        make_laps([28100, 28100, 27850], sectors=[(14000, 14100), (), (14050, 13800)]),
        ALL_LAPS,
    )
    assert partial.theoretical_best_ms is None

    # Sectors of excluded laps must not contribute (lap 1 is the out-lap here).
    out_lap_sectors = [(30000, 20000), (14000, 14100), (14050, 13800), (13990, 14010)]
    with_outlap = pace_stats(
        make_laps([50000, 28100, 27850], sectors=out_lap_sectors[:3])
    )
    assert with_outlap.theoretical_best_ms is None  # only 2 laps left, below min_laps
    kept = pace_stats(make_laps([50000, 28100, 27850, 28000], sectors=out_lap_sectors))
    assert kept.used_lap_numbers == [2, 3, 4]
    assert kept.theoretical_best_ms == 13990 + 13800


def test_degradation_detects_a_real_trend_and_ignores_flat_noise() -> None:
    rng = np.random.default_rng(7)
    noise = rng.normal(0, 30, 20)
    rising = pace_stats(
        make_laps([int(28000 + 50 * index + noise[index]) for index in range(20)]), ALL_LAPS
    )
    assert rising.degradation_ms_per_lap == pytest.approx(50, abs=10)
    assert rising.degradation_p_value < 1e-6

    flat = pace_stats(make_laps([int(28000 + value) for value in noise]), ALL_LAPS)
    assert abs(flat.degradation_ms_per_lap) < 10
    assert flat.degradation_p_value > 0.05


def test_degradation_needs_three_laps() -> None:
    stats = pace_stats(make_laps([28000, 28100]), LapFilter(drop_first_lap=False, min_laps=1))
    assert stats.n_used == 2
    assert stats.degradation_ms_per_lap is None
    assert stats.degradation_p_value is None


def test_pace_delta_to_best_driver() -> None:
    stats_by_driver = {
        "FAST": pace_stats(make_laps([28000, 28000, 28000, 28000]), ALL_LAPS),
        "SLOW": pace_stats(make_laps([28500, 28500, 28500, 28500]), ALL_LAPS),
        "DNS": pace_stats([]),
    }
    deltas = pace_delta_to_best_driver(stats_by_driver)
    assert deltas["FAST"] == pytest.approx(0.0)
    assert deltas["SLOW"] == pytest.approx(500.0)
    assert deltas["DNS"] is None
    assert pace_delta_to_best_driver({}) == {}
    with pytest.raises(ValueError):
        pace_delta_to_best_driver(stats_by_driver, metric="nonsense")


def test_pace_delta_defaults_to_the_mean_not_the_median() -> None:
    """The gap is a race quantity: total time over a stint is n * mean.

    A skewed stint separates the two estimators. SKEWED has the better median
    (a tidier typical lap) but the worse mean, and it is the mean that decides
    who was ahead after 6 laps -- so the default must follow the mean.
    """
    # Outlier dropping off, so the estimator question is isolated from filtering.
    keep_all = LapFilter(drop_first_lap=False, drop_slow_outliers=False)
    skewed = pace_stats(make_laps([27_800, 27_800, 27_800, 27_800, 27_800, 31_400]), keep_all)
    steady = pace_stats(make_laps([28_100, 28_100, 28_100, 28_100, 28_100, 28_100]), keep_all)
    stats_by_driver = {"SKEWED": skewed, "STEADY": steady}

    assert skewed.median_ms < steady.median_ms  # tidier typical lap
    assert skewed.mean_ms > steady.mean_ms  # but slower over the stint

    by_mean = pace_delta_to_best_driver(stats_by_driver)
    assert by_mean["STEADY"] == pytest.approx(0.0)
    assert by_mean["SKEWED"] > 0.0

    # The median-based view reverses the verdict; both are available, and the
    # default is the one that matches the clock.
    by_median = pace_delta_to_best_driver(stats_by_driver, metric="median_ms")
    assert by_median["SKEWED"] == pytest.approx(0.0)
    assert by_median["STEADY"] > 0.0


# --------------------------------------------------------------------------- #
# compare_drivers
# --------------------------------------------------------------------------- #


def test_comparison_detects_a_known_shift() -> None:
    slow = normal_laps(28500, 150, 40, seed=1)
    fast = normal_laps(28000, 150, 40, seed=2)
    result = compare_drivers(slow, fast, name_a="SLOW", name_b="FAST")

    assert result.mean_diff_ms == pytest.approx(500, abs=120)
    assert result.median_diff_ms == pytest.approx(500, abs=120)
    tests = {test.name: test for test in result.tests}
    assert tests["welch_t"].p_value < 1e-6
    assert tests["mann_whitney_u"].p_value < 1e-6
    assert tests["welch_t"].effect_size > 0.8  # large Hedges' g
    assert tests["mann_whitney_u"].effect_size > 0.474  # large Cliff's delta
    assert tests["bootstrap_median_diff"].ci_low > 0  # CI excludes zero
    assert "быстрее FAST" in tests["welch_t"].interpretation


def test_comparison_does_not_invent_a_shift_that_is_not_there() -> None:
    a = normal_laps(28000, 150, 40, seed=11)
    b = normal_laps(28000, 150, 40, seed=12)
    result = compare_drivers(a, b, name_a="A", name_b="B")

    tests = {test.name: test for test in result.tests}
    assert tests["welch_t"].p_value > 0.05
    assert tests["mann_whitney_u"].p_value > 0.05
    assert abs(tests["welch_t"].effect_size) < 0.5
    boot = tests["bootstrap_median_diff"]
    assert boot.ci_low < 0 < boot.ci_high  # CI contains zero
    assert "включает 0" in boot.interpretation


def test_levene_detects_a_consistency_difference() -> None:
    steady = normal_laps(28000, 40, 40, seed=21)
    erratic = normal_laps(28000, 400, 40, seed=22)
    tests = {test.name: test for test in compare_drivers(steady, erratic).tests}
    assert tests["levene_brown_forsythe"].p_value < 0.01
    assert "Стабильность различается" in tests["levene_brown_forsythe"].interpretation


def test_all_four_tests_are_always_reported() -> None:
    expected = ["welch_t", "mann_whitney_u", "levene_brown_forsythe", "bootstrap_median_diff"]
    normal = compare_drivers(normal_laps(28000, 100, 10, 1), normal_laps(28000, 100, 10, 2))
    assert [test.name for test in normal.tests] == expected
    # ... including when they cannot be run.
    tiny = compare_drivers(make_laps([28000, 28100]), make_laps([28000, 28100]))
    assert [test.name for test in tiny.tests] == expected
    assert all(test.statistic is None and test.p_value is None for test in tiny.tests)
    assert all(test.interpretation.startswith("Не выполнен:") for test in tiny.tests)
    assert tiny.mean_diff_ms is None and tiny.median_diff_ms is None


def test_comparison_with_empty_input_is_reported_not_raised() -> None:
    result = compare_drivers([], normal_laps(28000, 100, 10, 3), name_a="GHOST", name_b="REAL")
    assert result.n_a == 0 and result.n_b > 0
    assert result.stats_a.n_used == 0
    assert all(test.p_value is None for test in result.tests)
    assert any("У GHOST всего 0 зачётных кругов" in caveat for caveat in result.caveats)


def test_bootstrap_is_reproducible_for_a_given_seed() -> None:
    a = normal_laps(28200, 200, 25, seed=31)
    b = normal_laps(28000, 200, 25, seed=32)

    def boot(seed: int, n_boot: int = 2000) -> StatTestResult:
        result = compare_drivers(a, b, n_boot=n_boot, seed=seed)
        return {test.name: test for test in result.tests}["bootstrap_median_diff"]

    first, again = boot(777), boot(777)
    assert (first.ci_low, first.ci_high) == (again.ci_low, again.ci_high)

    other = boot(778)
    assert (other.ci_low, other.ci_high) != (first.ci_low, first.ci_high)
    # Different seeds must still agree on the point estimate and roughly on the CI.
    assert other.statistic == first.statistic
    assert other.ci_low == pytest.approx(first.ci_low, abs=60)


def test_bootstrap_without_resamples_still_reports_the_point_estimate() -> None:
    a, b = normal_laps(28200, 100, 12, 33), normal_laps(28000, 100, 12, 34)
    result = compare_drivers(a, b, n_boot=0)
    boot = {test.name: test for test in result.tests}["bootstrap_median_diff"]
    assert boot.statistic == pytest.approx(result.median_diff_ms)
    assert boot.ci_low is None and boot.ci_high is None


def test_effect_sizes_are_orientation_aware() -> None:
    a = np.array([28500.0, 28600.0, 28400.0, 28550.0])
    b = np.array([28000.0, 28100.0, 27900.0, 28050.0])
    assert hedges_g(a, b) > 0 and hedges_g(b, a) < 0
    assert cliffs_delta(a, b) == pytest.approx(1.0)
    assert cliffs_delta(b, a) == pytest.approx(-1.0)
    assert cliffs_delta(a, a) == pytest.approx(0.0)
    assert hedges_g(np.array([1.0]), b) is None
    assert cliffs_delta(np.array([]), b) is None


# --------------------------------------------------------------------------- #
# Caveats
# --------------------------------------------------------------------------- #


def test_dependence_caveat_is_always_first() -> None:
    result = compare_drivers(normal_laps(28000, 100, 30, 41), normal_laps(28000, 100, 30, 42))
    assert result.caveats
    head = result.caveats[0]
    assert "не являются независимыми наблюдениями" in head
    assert "трафик" in head and "деградация" in head
    assert "присмотреться" in head


def test_small_sample_and_imbalance_caveats() -> None:
    result = compare_drivers(
        normal_laps(28000, 100, 6, 51), normal_laps(28000, 100, 30, 52), name_a="FEW", name_b="MANY"
    )
    assert any("Малая выборка" in caveat for caveat in result.caveats)
    assert any("Несбалансированные выборки" in caveat for caveat in result.caveats)


def test_suspicious_fast_caveat() -> None:
    a = make_laps([28000, 28050, 27980, 28010, 27990, 28030, 28020, 25000])
    b = normal_laps(28000, 60, 20, seed=61)
    result = compare_drivers(a, b, name_a="ROCKET", name_b="STEADY")
    fast_caveats = [caveat for caveat in result.caveats if "подозрительно быстрые круги" in caveat]
    assert len(fast_caveats) == 1
    assert "ROCKET" in fast_caveats[0] and "[8]" in fast_caveats[0]
    assert "оставлены в выборке" in fast_caveats[0]


def test_degradation_caveat() -> None:
    rng = np.random.default_rng(71)
    fading = make_laps([int(28000 + 60 * index + rng.normal(0, 30)) for index in range(20)])
    steady = normal_laps(28500, 30, 20, seed=72)
    result = compare_drivers(fading, steady, name_a="FADING", name_b="STEADY")
    trend = [caveat for caveat in result.caveats if "значимый тренд времени круга" in caveat]
    assert len(trend) == 1
    assert "FADING" in trend[0] and "замедлялся" in trend[0]


def test_variance_caveat_mentions_robust_alternatives() -> None:
    result = compare_drivers(normal_laps(28000, 30, 40, 81), normal_laps(28000, 400, 40, 82))
    assert any("различается разброс времён круга" in caveat for caveat in result.caveats)


# --------------------------------------------------------------------------- #
# Serialisation
# --------------------------------------------------------------------------- #


def test_pace_stats_asdict_is_json_clean() -> None:
    laps = make_laps(
        [None, 28000, 28100, 27900, 45000, 28050, 26000, 28020],
        tags={6: ("penalty",)},
        sectors=[(14000, 14000)] * 8,
    )
    payload = asdict(pace_stats(laps))
    assert_plain_json_types(payload)
    assert json.loads(json.dumps(payload))["n_laps"] == 8


def test_comparison_asdict_is_json_clean() -> None:
    result = compare_drivers(
        normal_laps(28300, 200, 25, 91), normal_laps(28000, 150, 18, 92), name_a="A", name_b="B"
    )
    payload = asdict(result)
    assert_plain_json_types(payload)
    text = json.dumps(payload, allow_nan=False)  # NaN/Infinity would be invalid JSON
    assert json.loads(text)["driver_a"] == "A"


def test_no_numpy_scalars_survive_in_degenerate_results() -> None:
    for laps_a, laps_b in (
        ([], []),
        (make_laps([None]), make_laps([28000])),
        (make_laps([28000] * 10), make_laps([28000] * 10)),  # zero variance everywhere
    ):
        payload = asdict(compare_drivers(laps_a, laps_b))
        assert_plain_json_types(payload)
        json.dumps(payload, allow_nan=False)


def test_dataclass_field_types_are_builtins() -> None:
    stats = pace_stats(make_laps([28000, 28100, 27900, 28200]), ALL_LAPS)
    assert type(stats.best_ms) is int
    assert type(stats.median_ms) is float
    assert type(stats.mad_ms) is float
    assert all(type(number) is int for number in stats.used_lap_numbers)
    flag = classify_laps(make_laps([None, 28000, 28100]))[0]
    assert type(flag.lap_number) is int and type(flag.used) is bool


# --------------------------------------------------------------------------- #
# Real data smoke test (hand-checked fixture)
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def final_a_laps() -> dict[str, list[LapPoint]]:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return {
        driver: make_laps([to_ms(value) for value in times])
        for driver, times in data["laps"].items()
    }


def test_fixture_pace_stats_are_plausible(final_a_laps: dict[str, list[LapPoint]]) -> None:
    stats = {driver: pace_stats(laps) for driver, laps in final_a_laps.items()}
    assert set(stats) == {"KOLYA11", "WLAD111", "TWG", "DENISENKO", "PHREEMAN", "ИГОРЬ53"}

    for driver, item in stats.items():
        assert item.n_laps == 20
        assert item.n_used >= 15, driver
        assert 1 not in item.used_lap_numbers  # lap 1 has no time in this email
        assert 25_000 < item.median_ms < 30_000, driver
        assert item.best_ms >= 25_845
        assert item.theoretical_best_ms is None  # lap chart carries no sectors

    # Every driver lost their 40+ s incident lap to the robust filter.
    incidents = {
        driver: [flag.lap_number for flag in item.excluded if flag.reason == "slow_outlier"]
        for driver, item in stats.items()
    }
    assert 17 in incidents["KOLYA11"] and 19 in incidents["WLAD111"]
    assert 3 in incidents["TWG"] and 5 in incidents["DENISENKO"]
    assert 18 in incidents["PHREEMAN"] and 14 in incidents["ИГОРЬ53"]

    deltas = pace_delta_to_best_driver(stats)
    assert min(deltas.values()) == pytest.approx(0.0)
    assert deltas["ИГОРЬ53"] > deltas["TWG"]  # last on the road is slowest on median pace


def test_fixture_wlad111_vs_twg(final_a_laps: dict[str, list[LapPoint]]) -> None:
    result = compare_drivers(
        final_a_laps["WLAD111"], final_a_laps["TWG"], name_a="WLAD111", name_b="TWG"
    )
    assert isinstance(result, DriverComparison)
    assert isinstance(result.stats_a, PaceStats)
    assert (result.n_a, result.n_b) == (result.stats_a.n_used, result.stats_b.n_used)
    assert result.n_a >= 15 and result.n_b >= 15

    # Their published best laps: 26.788 vs 25.845; both are flagged as unusually fast.
    assert result.stats_a.best_ms == 26_788
    assert result.stats_b.best_ms == 25_845
    fast_caveats = [caveat for caveat in result.caveats if "подозрительно быстрые круги" in caveat]
    assert len(fast_caveats) == 2

    # Their medians are within a tenth of a second, so nothing should look decisive.
    assert abs(result.median_diff_ms) < 100
    for test in result.tests:
        assert test.interpretation
        if test.p_value is not None:
            assert 0.0 <= test.p_value <= 1.0
            assert test.p_value > 0.05, f"{test.name} claims a difference that is not there"
    boot = {test.name: test for test in result.tests}["bootstrap_median_diff"]
    assert boot.ci_low < 0 < boot.ci_high

    assert result.caveats[0].startswith("Круги одной гонки не являются независимыми")
    assert_plain_json_types(asdict(result))


# --------------------------------------------------------------------------- #
# Regression tests for reported defects
# --------------------------------------------------------------------------- #


class TestDropMissingIsHonoured:
    """`LapFilter.drop_missing` must do something observable, or it is a lie."""

    LAPS = [28_000, None, 28_100, 28_050, 28_020, 27_980]

    def test_a_missing_lap_never_enters_the_sample_either_way(self) -> None:
        for drop_missing in (True, False):
            flt = LapFilter(drop_missing=drop_missing, drop_first_lap=False)
            stats = pace_stats(make_laps(self.LAPS), flt)
            assert stats.n_used == 5
            assert 2 not in stats.used_lap_numbers

    def test_the_flag_controls_whether_it_is_reported_as_excluded(self) -> None:
        reported = pace_stats(make_laps(self.LAPS), LapFilter(drop_first_lap=False))
        assert [flag.reason for flag in reported.excluded] == ["missing"]

        ignored = pace_stats(
            make_laps(self.LAPS), LapFilter(drop_first_lap=False, drop_missing=False)
        )
        assert ignored.excluded == []
        assert ignored.n_used == reported.n_used  # the numbers are untouched

    def test_other_exclusions_are_reported_whatever_the_flag(self) -> None:
        laps = make_laps([28_500, None, 28_000, 28_100, 27_950, 45_000])
        stats = pace_stats(laps, LapFilter(drop_missing=False))
        reasons = {flag.lap_number: flag.reason for flag in stats.excluded}
        assert reasons == {1: "first_lap", 6: "slow_outlier"}


class TestRobustScaleFallback:
    """A zero MAD must not switch outlier detection off (a tie is not "no spread")."""

    def test_a_gross_outlier_is_caught_when_the_mad_collapses(self) -> None:
        laps = make_laps([28_000] * 7 + [90_000])
        stats = pace_stats(laps, LapFilter(drop_first_lap=False))
        assert median_abs_deviation([28_000.0] * 7 + [90_000.0]) == 0.0  # premise
        assert [flag.reason for flag in stats.excluded] == ["slow_outlier"]
        assert stats.n_used == 7
        assert stats.median_ms == pytest.approx(28_000.0)
        assert stats.std_ms == pytest.approx(0.0)

    def test_the_iqr_is_used_before_the_mean_absolute_deviation(self) -> None:
        from karting.stats.outliers import IQR_SCALE, robust_scale

        # Three quarters of the values tie (MAD = 0) but the quartiles differ.
        values = [100.0, 100.0, 100.0, 200.0]
        assert median_abs_deviation(values) == 0.0
        assert robust_scale(values) == pytest.approx(25.0 * IQR_SCALE)

    def test_a_genuinely_constant_sample_has_no_outliers(self) -> None:
        stats = pace_stats(make_laps([28_000] * 6), LapFilter(drop_first_lap=False))
        assert stats.excluded == []
        assert stats.n_used == 6


class TestTrimmedMean:
    def test_it_is_none_when_nothing_could_be_trimmed(self) -> None:
        for size in (3, 4):
            stats = pace_stats(make_laps([28_000 + i for i in range(size)]), ALL_LAPS)
            assert stats.mean_ms is not None
            assert stats.trimmed_mean_ms is None, size

    def test_a_short_sample_still_loses_one_lap_per_tail(self) -> None:
        # scipy's trim_mean(0.10) would cut int(7 * 0.1) = 0 and return the mean.
        stats = pace_stats(
            make_laps([28_000] * 6 + [90_000]),
            LapFilter(drop_first_lap=False, drop_slow_outliers=False),
        )
        assert stats.mean_ms == pytest.approx(36_857.142857, rel=1e-9)
        assert stats.trimmed_mean_ms == pytest.approx(28_000.0)

    def test_it_matches_scipy_once_scipy_actually_trims(self) -> None:
        from scipy import stats as sps

        times = [28_000, 28_100, 28_200, 28_300, 28_400, 28_500, 28_600, 28_700, 28_800, 40_000]
        stats = pace_stats(make_laps(times), LapFilter(drop_first_lap=False, drop_slow_outliers=False))
        assert stats.trimmed_mean_ms == pytest.approx(float(sps.trim_mean(times, 0.10)))


class TestComparisonEdgeCases:
    def test_plain_differences_survive_a_sample_too_small_to_test(self) -> None:
        flt = LapFilter(min_laps=1, drop_first_lap=False)
        result = compare_drivers(
            make_laps([28_000]), make_laps([28_000, 28_100, 28_200]), flt=flt
        )
        assert result.mean_diff_ms == pytest.approx(-100.0)
        assert result.median_diff_ms == pytest.approx(-100.0)
        # ... while every test still refuses to run, and says so.
        assert all(test.statistic is None and test.p_value is None for test in result.tests)
        assert any("зачётных кругов" in caveat for caveat in result.caveats)

    def test_constant_but_different_samples_are_not_called_undecidable(self) -> None:
        result = compare_drivers(
            make_laps([28_000] * 6),
            make_laps([29_000] * 6),
            name_a="A",
            name_b="B",
            flt=LapFilter(drop_first_lap=False),
        )
        by_name = {test.name: test for test in result.tests}
        welch = by_name["welch_t"]
        assert welch.statistic is None and welch.p_value is None
        assert "-1000 мс" in welch.interpretation.replace("−", "-")
        assert "A" in welch.interpretation and "быстрее" in welch.interpretation
        levene = by_name["levene_brown_forsythe"]
        assert "одинаковы" in levene.interpretation
        assert result.median_diff_ms == pytest.approx(-1000.0)

    def test_identical_constant_samples_have_nothing_to_report(self) -> None:
        result = compare_drivers(
            make_laps([28_000] * 6), make_laps([28_000] * 6), flt=LapFilter(drop_first_lap=False)
        )
        welch = {test.name: test for test in result.tests}["welch_t"]
        assert "проверять нечего" in welch.interpretation

    def test_the_cliffs_delta_prose_counts_ties_as_ties(self) -> None:
        a = make_laps([1_000, 2_000, 3_000])
        b = make_laps([2_000, 3_000, 4_000])
        result = compare_drivers(a, b, name_a="A", name_b="B", flt=LapFilter(drop_first_lap=False))
        mann = {test.name: test for test in result.tests}["mann_whitney_u"]
        # 1 of 9 pairs has A strictly slower; (delta + 1) / 2 would claim 22%.
        assert "строго медленнее случайного круга B в 11% случаев" in mann.interpretation
        assert "точных совпадений среди пар: 22%" in mann.interpretation
        assert mann.effect_size == pytest.approx(-5 / 9)
