"""Tests for `karting.stats.events`: joker-lap and pit-stop detection.

The centre of gravity is the acceptance test on the real Final A data, where the
right answer is known lap by lap from the hand-checked fixture: six pit stops,
five jokers, one driver whose joker is genuinely undetectable, and not a single
ordinary lap flagged.  The synthetic cases around it pin down the behaviour the
real data cannot exercise (empty input, ties, thresholds, degenerate stints).
"""

from __future__ import annotations

import json
import math
from statistics import median
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from karting.models import LapTag
from karting.stats import LapFilter, LapPoint
from karting.stats.events import (
    IMPLAUSIBLE_JOKER_RATIO,
    KIND_JOKER,
    KIND_PIT,
    MAX_CANDIDATE_SHARE,
    MIN_BASELINE_LAPS,
    SECTOR_ANOMALY_SHARE,
    DetectedEvent,
    EventDetectionConfig,
    EventReport,
    detect_events,
)

FIXTURE = Path(__file__).parent / "fixtures" / "final_a_expected.json"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def to_ms(text: str | None) -> int | None:
    """Local copy of the duration parser (tests must not depend on the parser)."""
    if text is None:
        return None
    seconds, _, fraction = text.partition(".")
    return int(seconds) * 1000 + int(fraction.ljust(3, "0"))


def make_laps(
    times: Sequence[int | None],
    *,
    start: int = 1,
    sectors: Sequence[tuple[int | None, ...]] | None = None,
    tags: Mapping[int, Sequence[str]] | None = None,
) -> list[LapPoint]:
    """Build `LapPoint`s numbered `start, start+1, ...` from raw times.

    `tags` maps a lap *number* to the annotations a human has already made on
    it, which the detector must treat as evidence (SPEC §10.3).
    """
    return [
        LapPoint(
            lap_number=start + index,
            time_ms=time,
            sectors=sectors[index] if sectors is not None else (),
            tags=tuple((tags or {}).get(start + index, ())),
        )
        for index, time in enumerate(times)
    ]


def pairs(report: EventReport, kind: str) -> set[tuple[str, int]]:
    """``{(driver, lap_number)}`` of every detected event of `kind`."""
    return {
        (event.driver, event.lap_number) for event in report.events if event.kind == kind
    }


def event_of(report: EventReport, driver: str, kind: str) -> DetectedEvent:
    found = [
        event for event in report.events if event.driver == driver and event.kind == kind
    ]
    assert len(found) == 1, f"expected exactly one {kind} for {driver}, got {found}"
    return found[0]


@pytest.fixture(scope="module")
def final_a() -> dict[str, list[LapPoint]]:
    """Real Final A laps; WLAD111 (the email recipient) carries sector times."""
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    laps = {
        driver: make_laps([to_ms(value) for value in times])
        for driver, times in data["laps"].items()
    }
    table = data["recipient_sector_table"]
    by_number = {row["lap"]: row for row in table["rows"]}
    for lap in laps[table["driver"]]:
        row = by_number[lap.lap_number]
        lap.sectors = tuple(to_ms(value) for value in row["sectors"])
    return laps


# --------------------------------------------------------------------------- #
# Acceptance test on the reference race
# --------------------------------------------------------------------------- #


class TestFinalA:
    """Ground truth, checked by hand against the rendered email."""

    EXPECTED_PITS = {
        ("KOLYA11", 17),
        ("WLAD111", 19),
        ("TWG", 3),
        ("DENISENKO", 5),
        ("PHREEMAN", 18),
        ("ИГОРЬ53", 14),
    }
    EXPECTED_JOKERS = {
        ("KOLYA11", 19),
        ("WLAD111", 3),
        ("TWG", 14),
        ("DENISENKO", 15),
        ("PHREEMAN", 5),
    }

    def test_every_driver_pits_exactly_once(self, final_a: dict[str, list[LapPoint]]) -> None:
        report = detect_events(final_a)
        assert pairs(report, KIND_PIT) == self.EXPECTED_PITS
        assert report.drivers_without_pit == []

    def test_five_jokers_and_igor_has_none(self, final_a: dict[str, list[LapPoint]]) -> None:
        report = detect_events(final_a)
        assert pairs(report, KIND_JOKER) == self.EXPECTED_JOKERS
        # Lap 15 (32.178) comes straight after his pit stop on lap 14: the joker
        # was evidently taken while losing time, so it is not detectable.  The
        # detector must say so instead of picking his best ordinary lap.
        assert report.drivers_without_joker == ["ИГОРЬ53"]

    def test_no_ordinary_lap_is_flagged(self, final_a: dict[str, list[LapPoint]]) -> None:
        report = detect_events(final_a)
        flagged = {(event.driver, event.lap_number) for event in report.events}
        assert flagged == self.EXPECTED_PITS | self.EXPECTED_JOKERS
        assert len(report.events) == 11

    def test_the_laps_a_naive_mad_threshold_gets_wrong(
        self, final_a: dict[str, list[LapPoint]]
    ) -> None:
        """These are ordinary laps that "median + 3 * MAD" calls pit stops."""
        flagged = {(event.driver, event.lap_number) for event in detect_events(final_a).events}
        for driver, lap_number, time in (
            ("KOLYA11", 16, "29.558"),
            ("KOLYA11", 18, "28.276"),
            ("PHREEMAN", 3, "29.349"),
            ("PHREEMAN", 4, "29.536"),
            ("ИГОРЬ53", 3, "29.938"),
            ("ИГОРЬ53", 15, "32.178"),
        ):
            assert (driver, lap_number) not in flagged, f"{driver} lap {lap_number} = {time}"

    def test_nothing_is_ambiguous_or_missing(self, final_a: dict[str, list[LapPoint]]) -> None:
        report = detect_events(final_a)
        assert report.drivers_with_multiple == []
        assert report.warnings == []

    def test_wlad111_sectors_localise_both_events(
        self, final_a: dict[str, list[LapPoint]]
    ) -> None:
        """The shortcut is in S1 (12.241 vs ~14.0); the pit lane is in S2 (26.343)."""
        report = detect_events(final_a)
        joker = event_of(report, "WLAD111", KIND_JOKER)
        pit = event_of(report, "WLAD111", KIND_PIT)
        assert joker.sector_index == 0  # S1, 0-based index into LapPoint.sectors
        assert pit.sector_index == 1  # S2
        assert "S1" in joker.note and "S2" in pit.note

        # Drivers without sector data are still detected, just not localised.
        for driver in ("KOLYA11", "TWG", "DENISENKO", "PHREEMAN"):
            assert event_of(report, driver, KIND_PIT).sector_index is None
            assert "нет секторных времён" in event_of(report, driver, KIND_PIT).note

    def test_ratios_and_deltas_match_the_email(self, final_a: dict[str, list[LapPoint]]) -> None:
        report = detect_events(final_a)
        pit = event_of(report, "WLAD111", KIND_PIT)
        assert pit.lap_number == 19
        assert pit.ratio == pytest.approx(40_342 / 28_058, abs=1e-4)
        assert pit.delta_ms == 12_284  # ~12.3 s lost, the format's ~13 s pit loss
        joker = event_of(report, "WLAD111", KIND_JOKER)
        assert joker.delta_ms == -1_270  # ~1.3 s gained on this driver's baseline
        assert joker.ratio < 0.97

        for event in report.events:
            assert 0.0 <= event.confidence <= 1.0
            assert event.note

    def test_the_official_best_lap_is_the_joker_for_five_drivers_of_six(
        self, final_a: dict[str, list[LapPoint]]
    ) -> None:
        """The point of the whole module: the published best lap is not pace."""
        data = json.loads(FIXTURE.read_text(encoding="utf-8"))
        official_best = data["best_lap_number"]  # highlighted lap per driver
        jokers = dict(pairs(detect_events(final_a), KIND_JOKER))
        matching = [driver for driver, lap in official_best.items() if jokers.get(driver) == lap]
        assert len(matching) == 5
        assert "ИГОРЬ53" not in matching  # the only driver whose best lap is real pace

    def test_pit_confidence_is_higher_than_the_tightest_joker(
        self, final_a: dict[str, list[LapPoint]]
    ) -> None:
        """Confidence must reflect the evidence, not be a constant."""
        report = detect_events(final_a)
        confidences = {
            (event.driver, event.kind): event.confidence for event in report.events
        }
        assert len(set(confidences.values())) > 1
        # WLAD111's pit is 0.19 past its threshold, his joker only 0.015 -- and
        # both are localised in a sector, so the tie-breaker is the margin.
        assert confidences[("WLAD111", KIND_PIT)] > confidences[("WLAD111", KIND_JOKER)]
        # The weakest joker (WLAD111, ratio 0.955) is less certain than the
        # strongest one (TWG, ratio 0.923) even though only WLAD111 has sectors.
        assert confidences[("TWG", KIND_JOKER)] > confidences[("WLAD111", KIND_JOKER)]
        assert all(value > 0.5 for value in confidences.values())
        assert all(value < 1.0 for value in confidences.values())  # never certain


# --------------------------------------------------------------------------- #
# Contract
# --------------------------------------------------------------------------- #


def test_default_config_matches_the_contract() -> None:
    config = EventDetectionConfig()
    assert (config.pit_ratio, config.joker_ratio) == (1.25, 0.97)
    assert config.one_per_driver is True
    assert config.require_single_sector is True
    assert config.skip_first_lap is True
    assert config == EventDetectionConfig() and hash(config)  # frozen / hashable


def test_kind_values_are_the_domain_tags() -> None:
    assert (KIND_JOKER, KIND_PIT) == (LapTag.JOKER.value, LapTag.PIT.value)
    assert {KIND_JOKER, KIND_PIT} <= LapFilter().exclude_tags  # both dropped from pace by default


def test_report_is_json_clean(final_a: dict[str, list[LapPoint]]) -> None:
    report = detect_events(final_a)
    payload = asdict(report)
    assert payload == report.to_dict()
    text = json.dumps(payload, allow_nan=False, ensure_ascii=False)
    assert json.loads(text)["events"][0]["kind"] in {KIND_JOKER, KIND_PIT}

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        else:
            assert type(value) in (bool, int, float, str, type(None)), repr(value)
            if isinstance(value, float):
                assert math.isfinite(value)

    walk(payload)


def test_driver_order_is_preserved(final_a: dict[str, list[LapPoint]]) -> None:
    reordered = dict(reversed(list(final_a.items())))
    events = detect_events(reordered).events
    assert [event.driver for event in events][:2] == ["ИГОРЬ53", "PHREEMAN"]
    # Within a driver the events are ordered by lap number.
    wlad = [event.lap_number for event in events if event.driver == "WLAD111"]
    assert wlad == sorted(wlad)


# --------------------------------------------------------------------------- #
# Degenerate and edge inputs
# --------------------------------------------------------------------------- #


def test_empty_input() -> None:
    report = detect_events({})
    assert report == EventReport()
    assert report.events == [] and report.warnings == []


def test_driver_without_laps_is_reported_not_crashed() -> None:
    report = detect_events({"GHOST": []})
    assert report.events == []
    assert report.drivers_without_joker == ["GHOST"]
    assert report.drivers_without_pit == ["GHOST"]
    assert any("обязаны все" in warning for warning in report.warnings)


def test_two_lap_driver_gets_no_invented_events() -> None:
    """With two laps a median is meaningless: 28.0 vs 41.0 has no baseline."""
    report = detect_events({"SHORT": make_laps([28_000, 41_000])})
    assert report.events == []
    assert report.drivers_without_pit == ["SHORT"]
    # Too little data to detect *and* too little to propose a lap: SPEC §10.4
    # still demands the missing pit be said out loud.
    assert report.pit_candidates == []
    assert report.warnings == [
        "SHORT: доступно всего 1 кругов с временем, "
        f"для базы нужно минимум {MIN_BASELINE_LAPS}; детекция пропущена",
        "SHORT: пит не обнаружен и предложить круг не из чего, хотя заезжать на пит "
        "обязаны все; данные этой сессии по нему неполны",
    ]


def test_identical_laps_produce_nothing() -> None:
    report = detect_events({"METRONOME": make_laps([28_000] * 12)})
    assert report.events == []
    assert report.drivers_without_joker == ["METRONOME"]
    assert report.drivers_without_pit == ["METRONOME"]
    # A missing joker is silent, a missing pit is not (SPEC §10.2).
    assert [warning.split(":")[0] for warning in report.warnings] == ["METRONOME"]
    assert "пит не обнаружен" in report.warnings[0]


def test_a_clean_stint_is_left_alone() -> None:
    """No joker, no pit: the detector must not manufacture the expected events."""
    times = [None, 28_500, 28_100, 27_980, 28_040, 27_950, 28_120, 28_060, 27_990, 28_200]
    report = detect_events({"STEADY": make_laps(times)})
    assert report.events == []
    assert report.drivers_without_joker == ["STEADY"]
    assert report.drivers_without_pit == ["STEADY"]


# --------------------------------------------------------------------------- #
# A missing pit stop is a problem, and comes with the lap to confirm (SPEC §10.2)
# --------------------------------------------------------------------------- #


def test_missing_pit_is_warned_about_and_comes_with_a_candidate() -> None:
    """The pit stop is mandatory, so its absence is reported *and* actionable."""
    times = [None, 28_500, 28_100, 27_980, 28_040, 27_950, 30_400, 28_060, 27_990, 28_200]
    report = detect_events({"STEADY": make_laps(times)})

    assert report.drivers_without_pit == ["STEADY"]
    (candidate,) = report.pit_candidates
    assert candidate.driver == "STEADY"
    assert candidate.kind == KIND_PIT
    assert candidate.lap_number == 7  # 30.400 is the slowest lap of the stint
    assert candidate.delta_ms > 0
    assert 1.0 < candidate.ratio < EventDetectionConfig().pit_ratio
    # A proposal is not a detection: it must never claim confidence, and it must
    # never reach `events`, where the storage would tag it automatically.
    assert candidate.confidence == 0.0
    assert candidate not in report.events
    assert report.events == []

    (warning,) = report.warnings
    assert "пит не обнаружен" in warning
    assert "заезжать на пит обязаны все" in warning  # nothing to compare against: a lone driver
    assert "круг — 7" in warning


def test_missing_joker_is_silent_while_a_missing_pit_is_not() -> None:
    """SPEC §10.2: a lost joker is a race fact, a lost pit stop is a defect."""
    times = [None, 28_500, 28_100, 41_000, 28_040, 27_950, 28_120, 28_060, 27_990, 28_200]
    report = detect_events({"ONLYPIT": make_laps(times)})
    assert pairs(report, KIND_PIT) == {("ONLYPIT", 4)}
    assert report.drivers_without_joker == ["ONLYPIT"]
    assert report.drivers_without_pit == []
    assert report.pit_candidates == []
    assert report.warnings == []


def test_pit_candidate_prefers_the_earlier_of_two_equally_slow_laps() -> None:
    times = [None, 28_000, 30_000, 28_100, 30_000, 28_050, 27_900, 28_020]
    report = detect_events({"TIED": make_laps(times)})
    (candidate,) = report.pit_candidates
    assert candidate.lap_number == 3


def test_pit_candidate_localises_its_sector_when_the_data_allows() -> None:
    """The proposal carries the same numbers as a detection, sector included."""
    sectors: list[tuple[int | None, ...]] = [
        (14_000, 14_000),
        (14_000, 14_000),
        (14_050, 13_950),
        (14_000, 14_100),
        (13_950, 14_000),
        (14_000, 14_000),
        (14_020, 16_980),  # the whole loss sits in S2
        (14_000, 14_000),
    ]
    times = [value[0] + value[1] for value in sectors]  # type: ignore[operator]
    report = detect_events({"SECTORED": make_laps(times, sectors=sectors)})
    (candidate,) = report.pit_candidates
    assert candidate.lap_number == 7
    assert candidate.sector_index == 1
    assert "заперта в секторе S2" in candidate.note


def test_untimed_laps_do_not_participate() -> None:
    times = [None, 28_000, None, 28_100, 27_950, 28_050, 41_000, 26_000, 28_020, None]
    report = detect_events({"GAPPY": make_laps(times)})
    assert pairs(report, KIND_PIT) == {("GAPPY", 7)}
    assert pairs(report, KIND_JOKER) == {("GAPPY", 8)}
    assert all(event.lap_number not in (1, 3, 10) for event in report.events)


def test_first_lap_is_skipped_unless_asked_otherwise() -> None:
    """A pit stop on the opening lap is invisible while lap 1 is excluded."""
    times = [41_500, 28_000, 28_100, 27_950, 28_050, 28_020, 27_990, 28_080]
    laps = {"EARLYBIRD": make_laps(times)}

    default = detect_events(laps)
    assert default.events == []
    assert default.drivers_without_pit == ["EARLYBIRD"]

    kept = detect_events(laps, EventDetectionConfig(skip_first_lap=False))
    assert pairs(kept, KIND_PIT) == {("EARLYBIRD", 1)}
    assert kept.drivers_without_pit == []


def test_first_lap_is_the_lowest_lap_number_present() -> None:
    laps = {"LATE": make_laps([41_500, 28_000, 28_100, 27_950, 28_050], start=7)}
    assert detect_events(laps).events == []
    kept = detect_events(laps, EventDetectionConfig(skip_first_lap=False))
    assert pairs(kept, KIND_PIT) == {("LATE", 7)}


# --------------------------------------------------------------------------- #
# Baseline, thresholds and configuration
# --------------------------------------------------------------------------- #


def test_the_baseline_ignores_the_events_it_finds() -> None:
    """Second pass: the pit lap must not drag the reference it is measured against.

    The stint is built so that the two passes genuinely disagree -- the rough
    median of all six usable laps is 28.250, the median of the five that are not
    candidates is 28.200 -- so deleting the second pass changes the answer.
    """
    times = [None, 28_000, 28_100, 28_200, 28_300, 28_400, 41_000]
    one_pass = median([28_000, 28_100, 28_200, 28_300, 28_400, 41_000])
    two_pass = median([28_000, 28_100, 28_200, 28_300, 28_400])
    assert (one_pass, two_pass) == (28_250.0, 28_200.0)

    event = event_of(detect_events({"A": make_laps(times)}), "A", KIND_PIT)
    assert event.delta_ms == 41_000 - 28_200
    assert event.ratio == pytest.approx(41_000 / 28_200)
    # And the one-pass answer really is a different number.
    assert event.ratio != pytest.approx(41_000 / 28_250)


def test_a_slow_driver_is_judged_against_their_own_pace() -> None:
    """Ratios are per driver, so a slow field member is not accused of pitting."""
    fast = make_laps([None] + [26_000, 26_100, 25_950, 26_050, 26_020, 26_080, 25_990])
    slow = make_laps([None] + [32_000, 32_100, 31_950, 32_050, 32_020, 32_080, 31_990])
    report = detect_events({"FAST": fast, "SLOW": slow})
    assert report.events == []
    assert sorted(report.drivers_without_pit) == ["FAST", "SLOW"]


def test_thresholds_are_configurable() -> None:
    times = [None] + [28_000] * 5 + [30_000] + [28_000] * 3 + [27_700]
    laps = {"A": make_laps(times)}
    assert detect_events(laps).events == []  # 1.071 and 0.989 are ordinary by default

    tight = detect_events(laps, EventDetectionConfig(pit_ratio=1.05, joker_ratio=0.99))
    assert pairs(tight, KIND_PIT) == {("A", 7)}
    assert pairs(tight, KIND_JOKER) == {("A", 11)}
    assert tight.drivers_without_joker == [] and tight.drivers_without_pit == []

    loose = detect_events(laps, EventDetectionConfig(pit_ratio=2.0, joker_ratio=0.5))
    assert loose.events == []


def test_a_degenerate_threshold_pair_is_reported_and_resolved_as_pit() -> None:
    laps = {"A": make_laps([None] + [28_000] * 6 + [41_000])}
    report = detect_events(laps, EventDetectionConfig(pit_ratio=0.9, joker_ratio=1.1))
    assert any("Вырожденная конфигурация" in warning for warning in report.warnings)
    assert all(event.kind == KIND_PIT for event in report.events)


# --------------------------------------------------------------------------- #
# One per driver
# --------------------------------------------------------------------------- #


class TestPerDriverLimits:
    #: Two pit-like laps (41.0 and 39.0) and two joker-like ones (26.0 and 26.4).
    TIMES = [None, 28_000, 41_000, 28_100, 26_000, 27_950, 39_000, 28_050, 26_400, 28_020]

    def test_every_pit_survives_but_only_the_best_joker(self) -> None:
        # A race may mandate several stops, so pit laps are not capped; a joker
        # sits close enough to an ordinary tow lap that only the clearest one is
        # claimed and the runner-up is reported instead.
        report = detect_events({"A": make_laps(self.TIMES)})
        assert pairs(report, KIND_PIT) == {("A", 3), ("A", 7)}
        assert pairs(report, KIND_JOKER) == {("A", 5)}  # 26.0 beats 26.4
        assert report.pit_counts == {"A": 2}
        assert report.expected_pits == 2
        assert report.drivers_without_joker == [] and report.drivers_without_pit == []
        # One driver cannot disagree with themselves.
        assert report.drivers_with_multiple == []

    def test_the_rejected_joker_is_reported_not_dropped(self) -> None:
        warnings = detect_events({"A": make_laps(self.TIMES)}).warnings
        assert len(warnings) == 1
        assert "круг 9 тоже похож на джокер" in warnings[0]
        assert "круг 5 — более выраженный кандидат" in warnings[0]

    def test_pits_can_be_capped_explicitly(self) -> None:
        config = EventDetectionConfig(max_pits_per_driver=1)
        report = detect_events({"A": make_laps(self.TIMES)}, config)
        assert pairs(report, KIND_PIT) == {("A", 3)}  # 41.0 beats 39.0
        assert any("круг 7 тоже похож на пит" in w for w in report.warnings)

    def test_switching_it_off_keeps_every_candidate(self) -> None:
        config = EventDetectionConfig(one_per_driver=False)
        report = detect_events({"A": make_laps(self.TIMES)}, config)
        assert pairs(report, KIND_PIT) == {("A", 3), ("A", 7)}
        assert pairs(report, KIND_JOKER) == {("A", 5), ("A", 9)}
        assert report.warnings == []  # nothing was rejected, so nothing to warn about
        assert [event.lap_number for event in report.events] == [3, 5, 7, 9]

    def test_a_joker_tie_goes_to_the_earlier_lap(self) -> None:
        times = [None, 28_000, 26_000, 28_100, 26_000, 27_950, 28_050, 28_020]
        report = detect_events({"A": make_laps(times)})
        assert pairs(report, KIND_JOKER) == {("A", 3)}

    def test_a_capped_pit_tie_goes_to_the_earlier_lap(self) -> None:
        times = [None, 28_000, 41_000, 28_100, 41_000, 27_950, 28_050, 28_020]
        config = EventDetectionConfig(max_pits_per_driver=1)
        assert pairs(detect_events({"A": make_laps(times)}, config), KIND_PIT) == {("A", 3)}


class TestPitCountConsensus:
    """How many stops the race mandates is read off the field, not assumed."""

    def field(self, pit_laps: dict[str, list[int]]) -> dict[str, list[LapPoint]]:
        drivers: dict[str, list[LapPoint]] = {}
        for name, laps in pit_laps.items():
            times: list[int | None] = [28_000] * 40
            for lap in laps:
                times[lap - 1] = 64_000
            drivers[name] = make_laps([None, *times])
        return drivers

    def test_a_two_stop_race_is_accepted_without_complaint(self) -> None:
        report = detect_events(self.field({"A": [10, 30], "B": [12, 28], "C": [9, 33]}))
        assert report.expected_pits == 2
        assert report.pit_counts == {"A": 2, "B": 2, "C": 2}
        assert report.drivers_with_multiple == []
        assert report.drivers_without_pit == []
        assert report.warnings == []

    def test_a_driver_out_of_step_with_the_field_is_flagged(self) -> None:
        report = detect_events(self.field({"A": [10, 30], "B": [12, 28], "C": [9, 20, 33]}))
        assert report.expected_pits == 2
        assert report.drivers_with_multiple == ["C"]
        assert any("обнаружено питов: 3, тогда как у остальных 2" in w
                   for w in report.warnings)

    def test_a_driver_who_never_pitted_is_told_what_the_field_did(self) -> None:
        report = detect_events(self.field({"A": [10, 30], "B": [12, 28], "C": []}))
        assert report.drivers_without_pit == ["C"]
        assert any("остальные пилоты заезжали 2 раз(а)" in w for w in report.warnings)


# --------------------------------------------------------------------------- #
# Sector localisation
# --------------------------------------------------------------------------- #


class TestSectorLocalisation:
    NORMAL = (14_000, 14_000)

    def build(self, pit_sectors: tuple[int, int]) -> dict[str, list[LapPoint]]:
        times = [28_000] * 5 + [sum(pit_sectors)] + [28_000] * 4
        sectors = [self.NORMAL] * 5 + [pit_sectors] + [self.NORMAL] * 4
        return {"A": make_laps(times, sectors=sectors)}

    def test_a_localised_anomaly_is_confirmed(self) -> None:
        report = detect_events(self.build((14_050, 27_000)))
        event = event_of(report, "A", KIND_PIT)
        assert event.sector_index == 1
        assert "заперта в секторе S2" in event.note

    def test_a_spread_anomaly_lowers_the_confidence_but_keeps_the_event(self) -> None:
        spread = detect_events(self.build((20_500, 20_500)))
        event = event_of(spread, "A", KIND_PIT)
        assert event.sector_index is None
        assert "не заперта в одном секторе" in event.note

        localised = event_of(detect_events(self.build((14_050, 27_000))), "A", KIND_PIT)
        assert event.confidence < localised.confidence
        assert event.confidence > 0.0  # penalised, not cancelled

    def test_the_penalty_is_lifted_when_the_check_is_not_required(self) -> None:
        laps = self.build((20_500, 20_500))
        strict = event_of(detect_events(laps), "A", KIND_PIT)
        lenient = event_of(
            detect_events(laps, EventDetectionConfig(require_single_sector=False)), "A", KIND_PIT
        )
        assert lenient.confidence > strict.confidence
        assert lenient.sector_index is None

    def test_a_sector_moving_the_wrong_way_does_not_break_confirmation(self) -> None:
        """WLAD111's joker gains 1.776 s in S1 while giving 0.531 s back in S2."""
        times = [28_000] * 5 + [26_800] + [28_000] * 4
        sectors = [self.NORMAL] * 5 + [(12_300, 14_500)] + [self.NORMAL] * 4
        event = event_of(detect_events({"A": make_laps(times, sectors=sectors)}), "A", KIND_JOKER)
        assert event.sector_index == 0
        assert "заперта в секторе S1" in event.note

    def test_incomplete_sector_data_is_not_guessed(self) -> None:
        times = [28_000] * 5 + [41_000] + [28_000] * 4
        sectors: list[tuple[int | None, ...]] = [self.NORMAL] * 5 + [(None, 27_000)] + [
            self.NORMAL
        ] * 4
        event = event_of(detect_events({"A": make_laps(times, sectors=sectors)}), "A", KIND_PIT)
        assert event.sector_index is None
        assert "нет секторных времён" in event.note


# --------------------------------------------------------------------------- #
# The guard band the design is sold on (SPEC §10.1)
# --------------------------------------------------------------------------- #


def _driver_baselines(report: EventReport, laps: Mapping[str, list[LapPoint]]) -> dict[str, float]:
    """Each driver's baseline, recovered from an event's `delta_ms`."""
    baselines: dict[str, float] = {}
    for event in report.events:
        time_ms = next(
            lap.time_ms for lap in laps[event.driver] if lap.lap_number == event.lap_number
        )
        baselines[event.driver] = float(time_ms) - float(event.delta_ms)
    return baselines


class TestGuardBand:
    """The empty band between the events and ordinary racing, measured.

    SPEC §10.1 justifies the ratio rule (and rejects a MAD threshold) with four
    numbers: the worst non-pit lap, the mildest pit, the best non-joker lap and
    the weakest joker.  The thresholds are only safe while that band stays wide,
    so any change to the baseline or to the classification that eats into it has
    to fail here rather than quietly halve the margin.
    """

    def boundaries(self, final_a: dict[str, list[LapPoint]]) -> dict[str, float]:
        report = detect_events(final_a)
        baselines = _driver_baselines(report, final_a)
        kinds = {(event.driver, event.lap_number): event.kind for event in report.events}
        worst_non_pit = best_non_joker = mildest_pit = weakest_joker = None
        for driver, laps in final_a.items():
            baseline = baselines[driver]
            for lap in laps[1:]:  # lap 1 is the start and never classified
                if lap.time_ms is None:
                    continue
                ratio = float(lap.time_ms) / baseline
                kind = kinds.get((driver, lap.lap_number))
                if kind == KIND_PIT:
                    mildest_pit = ratio if mildest_pit is None else min(mildest_pit, ratio)
                elif kind == KIND_JOKER:
                    weakest_joker = ratio if weakest_joker is None else max(weakest_joker, ratio)
                else:
                    worst_non_pit = ratio if worst_non_pit is None else max(worst_non_pit, ratio)
                    best_non_joker = ratio if best_non_joker is None else min(best_non_joker, ratio)
        assert None not in (worst_non_pit, best_non_joker, mildest_pit, weakest_joker)
        return {
            "worst_non_pit": float(worst_non_pit),
            "mildest_pit": float(mildest_pit),
            "best_non_joker": float(best_non_joker),
            "weakest_joker": float(weakest_joker),
        }

    def test_the_four_boundary_ratios_match_the_spec(
        self, final_a: dict[str, list[LapPoint]]
    ) -> None:
        found = self.boundaries(final_a)
        assert found["worst_non_pit"] == pytest.approx(1.120, abs=5e-4)
        assert found["mildest_pit"] == pytest.approx(1.438, abs=5e-4)
        assert found["best_non_joker"] == pytest.approx(0.988, abs=5e-4)
        assert found["weakest_joker"] == pytest.approx(0.955, abs=5e-4)

    def test_the_band_around_each_threshold_stays_wide(
        self, final_a: dict[str, list[LapPoint]]
    ) -> None:
        found = self.boundaries(final_a)
        config = EventDetectionConfig()
        # The pit threshold has ~0.13 of clearance on the ordinary side and
        # ~0.19 on the event side; the joker band is ten times tighter, which is
        # precisely why it needs a test of its own.
        assert found["worst_non_pit"] < config.pit_ratio - 0.12
        assert found["mildest_pit"] > config.pit_ratio + 0.18
        assert found["best_non_joker"] > config.joker_ratio + 0.014
        assert found["weakest_joker"] < config.joker_ratio - 0.014

    @pytest.mark.parametrize("pit_ratio", [1.20, 1.25, 1.35])
    @pytest.mark.parametrize("joker_ratio", [0.96, 0.97, 0.98])
    def test_the_answer_is_the_same_anywhere_inside_the_band(
        self, final_a: dict[str, list[LapPoint]], pit_ratio: float, joker_ratio: float
    ) -> None:
        report = detect_events(
            final_a, EventDetectionConfig(pit_ratio=pit_ratio, joker_ratio=joker_ratio)
        )
        assert pairs(report, KIND_PIT) == TestFinalA.EXPECTED_PITS
        assert pairs(report, KIND_JOKER) == TestFinalA.EXPECTED_JOKERS
        assert report.drivers_without_pit == []
        assert report.drivers_without_joker == ["ИГОРЬ53"]


# --------------------------------------------------------------------------- #
# Thresholds are inclusive
# --------------------------------------------------------------------------- #


def test_a_lap_exactly_on_the_pit_threshold_is_a_pit() -> None:
    """`lap_time >= ratio * baseline` (SPEC §10.2), boundary included."""
    times = [None, 28_000, 28_000, 28_000, 28_000, 35_000]  # 35_000 == 1.25 * 28_000
    assert pairs(detect_events({"A": make_laps(times)}), KIND_PIT) == {("A", 6)}


def test_a_lap_exactly_on_the_joker_threshold_is_a_joker() -> None:
    """`lap_time <= ratio * baseline` (SPEC §10.2), boundary included."""
    times = [None, 28_000, 28_000, 28_000, 28_000, 27_160]  # 27_160 == 0.97 * 28_000
    assert pairs(detect_events({"A": make_laps(times)}), KIND_JOKER) == {("A", 6)}


def test_a_joker_tie_goes_to_the_earlier_lap() -> None:
    """The mirror of `test_a_tie_goes_to_the_earlier_lap` on the fast side."""
    times = [None, 28_000, 26_000, 28_100, 26_000, 27_950, 28_050, 28_020]
    report = detect_events({"A": make_laps(times)})
    assert pairs(report, KIND_JOKER) == {("A", 3)}
    assert any("круг 5 тоже похож на джокер" in text for text in report.warnings)


# --------------------------------------------------------------------------- #
# A contaminated baseline is refused, not turned into events
# --------------------------------------------------------------------------- #


class TestContaminatedBaseline:
    """Half a stint of trouble must not turn the clean laps into jokers.

    The whole method rests on the anomalies being a minority of a driver's laps.
    When they are not -- a driver who pits, has an incident and retires -- the
    median lands on the anomalies and the *ordinary* laps cross the joker
    threshold.  Emitting that would be worse than emitting nothing: the joker
    tag removes the lap from `pace_stats`, so the driver's genuinely best lap
    would disappear from the very statistic this module exists to protect.
    """

    def test_half_a_slow_stint_produces_no_events(self) -> None:
        report = detect_events({"A": make_laps([None, 28_000, 28_100, 41_000, 40_500])})
        assert report.events == []
        assert report.drivers_without_joker == ["A"]
        assert any("похожи" in text for text in report.warnings)

    def test_a_third_of_a_slow_stint_produces_no_events(self) -> None:
        report = detect_events(
            {"A": make_laps([None, 28_000, 28_100, 28_200, 41_000, 40_500, 40_900])}
        )
        assert report.events == []
        assert any(
            "на джокер похожи 3 из 6 кругов" in text for text in report.warnings
        )

    def test_an_impossibly_fast_joker_means_the_baseline_is_wrong(self) -> None:
        """A "shortcut" worth a third of the lap does not exist."""
        report = detect_events({"A": make_laps([None, 28_000, 41_000, 41_500])})
        assert report.events == []
        assert report.drivers_without_joker == ["A"]
        assert any("быстрее, чем способна сделать любая срезка" in text for text in report.warnings)
        assert IMPLAUSIBLE_JOKER_RATIO < 28_000 / 41_250 + 0.2  # the bound bites here

    def test_the_reference_race_is_nowhere_near_the_guards(
        self, final_a: dict[str, list[LapPoint]]
    ) -> None:
        """One joker and one pit in twenty laps: 0.05 of the stint, not 0.5."""
        report = detect_events(final_a)
        for driver, laps in final_a.items():
            timed = sum(1 for lap in laps[1:] if lap.time_ms is not None)
            for kind in (KIND_JOKER, KIND_PIT):
                found = sum(
                    1
                    for event in report.events
                    if event.driver == driver and event.kind == kind
                )
                assert found / timed < MAX_CANDIDATE_SHARE / 2


# --------------------------------------------------------------------------- #
# Broken input is rejected, not reinterpreted
# --------------------------------------------------------------------------- #


def test_a_non_positive_lap_time_is_not_a_joker() -> None:
    """`detect_events` is a public entry point and takes whatever it is given."""
    report = detect_events({"A": make_laps([None, 0, 28_000, 28_100, -5_000, 41_000, 27_950])})
    assert pairs(report, KIND_JOKER) == set()
    assert pairs(report, KIND_PIT) == {("A", 6)}
    assert any(
        "круг 2 и 5 имеет неположительное время" in text for text in report.warnings
    )


def test_a_zero_lap_never_becomes_the_baseline() -> None:
    report = detect_events({"A": make_laps([None, 28_000, 28_100, 0, 27_950, 28_050])})
    assert report.events == []
    assert any("неположительное время" in text for text in report.warnings)


# --------------------------------------------------------------------------- #
# Manual annotation outranks the detector (SPEC §10.3)
# --------------------------------------------------------------------------- #


class TestManualAnnotation:
    """`LapPoint.tags` carries the human's verdict, and it is the last word.

    SPEC §10.2 invites a human to resolve every deviation from "one joker and
    one pit per driver".  That invitation is only real if accepting it settles
    the matter: a session annotated by hand has to come back complete, not keep
    reporting the same missing event forever.
    """

    ORDINARY = [None, 28_000, 28_100, 27_950, 28_050, 28_020, 28_080, 27_990]

    def test_a_hand_annotated_joker_settles_the_missing_joker(self) -> None:
        laps = make_laps(self.ORDINARY, tags={4: ["joker"]})
        report = detect_events({"A": laps})
        assert pairs(report, KIND_JOKER) == {("A", 4)}
        assert report.drivers_without_joker == []
        assert report.drivers_with_multiple == []
        event = event_of(report, "A", KIND_JOKER)
        assert event.confidence == 1.0
        assert "размечен вручную" in event.note

    def test_a_hand_annotated_pit_settles_the_missing_pit(self) -> None:
        laps = make_laps(self.ORDINARY, tags={6: ["pit"]})
        report = detect_events({"A": laps})
        assert pairs(report, KIND_PIT) == {("A", 6)}
        assert report.drivers_without_pit == []
        assert report.pit_candidates == []
        assert not any("пит не обнаружен" in text for text in report.warnings)

    def test_a_hand_annotated_event_is_not_pace_either(self) -> None:
        """The declared lap is kept out of the baseline, like a detected one."""
        times = [None, 28_000, 28_000, 28_000, 28_000, 41_000, 28_000]
        plain = detect_events({"A": make_laps(times)})
        declared = detect_events({"A": make_laps(times, tags={6: ["pit"]})})
        assert event_of(plain, "A", KIND_PIT).delta_ms == event_of(
            declared, "A", KIND_PIT
        ).delta_ms

    def test_the_detector_yields_the_kind_a_human_has_named(self) -> None:
        """One joker per driver: the human's lap wins, the detector's steps aside."""
        times = [None, 28_000, 26_000, 28_100, 27_950, 28_050, 28_020, 28_080]
        report = detect_events({"A": make_laps(times, tags={5: ["joker"]})})
        assert pairs(report, KIND_JOKER) == {("A", 5)}
        assert report.drivers_without_joker == []
        assert any("размечен как джокер вручную" in text for text in report.warnings)

    def test_both_are_kept_when_one_per_driver_is_off(self) -> None:
        times = [None, 28_000, 26_000, 28_100, 27_950, 28_050, 28_020, 28_080]
        report = detect_events(
            {"A": make_laps(times, tags={5: ["joker"]})},
            EventDetectionConfig(one_per_driver=False),
        )
        assert pairs(report, KIND_JOKER) == {("A", 3), ("A", 5)}
        assert report.drivers_with_multiple == ["A"]

    def test_an_overruled_detection_is_reported_but_not_counted(self) -> None:
        """"The detector says joker, the human says traffic": both are visible.

        The event stays in the report -- the caller shows it as overruled and
        storage keeps the automatic row, so clearing the manual tag restores the
        detection -- but the driver counts as having no joker, because none is
        in force.
        """
        times = [None, 28_000, 26_000, 28_100, 27_950, 28_050, 28_020, 28_080]
        report = detect_events({"A": make_laps(times, tags={3: ["traffic"]})})
        assert pairs(report, KIND_JOKER) == {("A", 3)}
        assert report.drivers_without_joker == ["A"]
        assert "перекрыт ручной разметкой" in event_of(report, "A", KIND_JOKER).note

    def test_the_reference_race_is_untouched_without_annotations(
        self, final_a: dict[str, list[LapPoint]]
    ) -> None:
        assert all(lap.tags == () for laps in final_a.values() for lap in laps)
        report = detect_events(final_a)
        assert len(report.events) == 11


# --------------------------------------------------------------------------- #
# Sector localisation: direction and sample hygiene
# --------------------------------------------------------------------------- #


class TestSectorEvidence:
    NORMAL = (14_000, 14_000)

    def test_a_pit_is_not_localised_in_a_sector_that_got_faster(self) -> None:
        """Direction matters: a detour cannot make its own sector quicker.

        The sectors here do not add up to the lap time (Apex sends partial
        sector data), which is exactly when the sign of the deviation is the
        only thing separating "the pit lane is in S1" from "S1 was quick".
        """
        times = [28_000] * 5 + [41_000] + [28_000] * 4
        sectors = [self.NORMAL] * 5 + [(1_000, 14_000)] + [self.NORMAL] * 4
        event = event_of(detect_events({"A": make_laps(times, sectors=sectors)}), "A", KIND_PIT)
        assert event.sector_index is None
        assert "не заперта в одном секторе" in event.note

    def test_candidate_laps_do_not_feed_the_per_sector_medians(self) -> None:
        """The joker and the pit are excluded from the sample they are judged by.

        Only two ordinary laps carry sectors here, one short of
        `MIN_SECTOR_SAMPLES`, so the honest answer is "секторных данных не хватает".
        Counting the two candidates would make four samples and manufacture a
        confirmation out of the anomalies themselves.
        """
        times = [None, 28_000, 28_000, 28_000, 26_000, 41_000]
        sectors: list[tuple[int | None, ...]] = [
            (),
            self.NORMAL,
            self.NORMAL,
            (),
            (12_000, 14_000),
            (14_000, 27_000),
        ]
        report = detect_events({"A": make_laps(times, sectors=sectors)})
        event = event_of(report, "A", KIND_PIT)
        assert event.sector_index is None
        assert "секторных данных не хватает" in event.note

    def test_a_sector_carrying_most_of_the_loss_still_localises_it(self) -> None:
        """`SECTOR_ANOMALY_SHARE`: "most of it, and no other sector", not "all of it".

        A pit lane costs its own sector nearly the whole lap deviation, but the
        rest of the lap is still raced, so demanding 100% would reject real pit
        stops.  Here S1 carries 70% of the +13.000 s and S2 the other 30%: one
        sector past the share, none of the others near it, so the anomaly is
        localised.
        """
        times = [28_000] * 5 + [41_000] + [28_000] * 4
        sectors = [self.NORMAL] * 5 + [(23_100, 17_900)] + [self.NORMAL] * 4
        event = event_of(detect_events({"A": make_laps(times, sectors=sectors)}), "A", KIND_PIT)
        assert 0.5 <= SECTOR_ANOMALY_SHARE < 0.7  # the fixture straddles the constant
        assert event.sector_index == 0
        assert "заперта в секторе S1" in event.note


# --------------------------------------------------------------------------- #
# Regression: the endurance race that exposed the "one pit per driver" bug
# --------------------------------------------------------------------------- #


class TestTwoStopEnduranceRace:
    """A 100-lap race with two mandatory stops (session #4 of the real data).

    Before the fix `one_per_driver` kept the more extreme stop and left the
    other one untagged, so a +35 s lap stayed in the pace sample and rode
    through the rolling average as a hump `window` laps wide.
    """

    #: Ratios measured on the real endurance data: stops land at 2.18-2.54 x
    #: the baseline, ordinary laps never pass 1.12 x.
    def build(self, pit_laps: list[int], laps: int = 98) -> list[LapPoint]:
        times: list[int | None] = [None]
        for number in range(1, laps + 1):
            times.append(64_000 if number in pit_laps else 28_000 + (number % 7) * 40)
        return make_laps(times)

    def test_both_stops_are_tagged(self) -> None:
        report = detect_events(
            {
                "WLAD111": self.build([29, 65]),
                "ANDRACER": self.build([19, 59]),
                "KADZHICK": self.build([20, 60]),
            }
        )
        assert pairs(report, KIND_PIT) == {
            ("WLAD111", 30),
            ("WLAD111", 66),
            ("ANDRACER", 20),
            ("ANDRACER", 60),
            ("KADZHICK", 21),
            ("KADZHICK", 61),
        }
        assert report.expected_pits == 2
        assert report.drivers_without_pit == []
        assert report.drivers_with_multiple == []

    def test_no_ordinary_lap_is_swept_up(self) -> None:
        report = detect_events({"WLAD111": self.build([29, 65])})
        assert len(pairs(report, KIND_PIT)) == 2
        assert pairs(report, KIND_JOKER) == set()

    def test_the_second_stop_is_not_left_behind_for_the_pace_sample(self) -> None:
        # The whole point of the fix: every stop carries a tag, so nothing that
        # slow can reach a pace metric through the "untagged" door.
        laps = self.build([29, 65])
        report = detect_events({"WLAD111": laps})
        tagged = {event.lap_number for event in report.events if event.kind == KIND_PIT}
        slow = {
            int(lap.lap_number)
            for lap in laps
            if lap.time_ms is not None and lap.time_ms > 40_000
        }
        assert slow == tagged
