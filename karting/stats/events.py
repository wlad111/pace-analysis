"""Detection of the two mandatory, non-pace laps: the joker lap and the pit stop.

In this race format every driver must take the joker (a shortcut that makes the
lap roughly 1.9 s faster) exactly once and must come in for a pit stop (roughly
13 s slower) exactly once.  Neither lap is race pace, so both have to be found
and excluded before any pace metric is computed -- and, crucially, the official
"best lap" printed in the email is the joker lap for five drivers out of six, so
the detector is what makes the published ranking comparable at all.

Why a ratio to the driver's own baseline and not a MAD threshold
---------------------------------------------------------------
A robust ``median +- k * MAD`` rule finds the joker cleanly but produces false
pit stops: for a very steady driver the MAD is tiny, so an ordinary lost-a-bit
lap (KOLYA11's 29.558 and 28.276, PHREEMAN's 29.349 and 29.536) sits far outside
it.  The ratio to the driver's own robust baseline separates the classes with an
enormous margin instead -- on the reference race the worst non-pit lap is at
1.120 x baseline while the mildest pit lap is at 1.438 x, and the best non-joker
lap is at 0.987 x while the weakest joker is at 0.954 x.  Hence the defaults
``pit_ratio = 1.25`` and ``joker_ratio = 0.97``, both of which sit in the middle
of a wide empty band.

The detector never invents an event to satisfy the "one of each per driver"
expectation: a driver whose joker is not detectable (because it was lost in
traffic, say) is reported in `EventReport.drivers_without_joker` for a human to
resolve, and extra candidates are reported as warnings rather than silently
dropped.  Nor does it insist once a human has answered: `LapPoint.tags` carries
the manual annotations already in force, a lap annotated ``joker`` or ``pit``
counts as that event, and the report comes back settled instead of asking the
same question after every run (SPEC §10.3).

The one assumption underneath all of it is that the anomalies are a *minority*
of a driver's laps, which is what makes their median an ordinary lap.  When a
stint is too short and too broken for that to hold, `_drop_implausible` refuses
the detection and says so rather than tagging away a clean lap.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from statistics import median
from typing import Any, Final

from karting.models import LapTag

from .outliers import LapPoint

__all__ = [
    "CONFIDENCE_BASE",
    "IMPLAUSIBLE_JOKER_RATIO",
    "KIND_JOKER",
    "KIND_PIT",
    "MAX_CANDIDATE_SHARE",
    "MIN_BASELINE_LAPS",
    "SECTOR_ANOMALY_SHARE",
    "DetectedEvent",
    "EventDetectionConfig",
    "EventReport",
    "detect_events",
]

#: Tag written for a detected event; kept in sync with the domain enum.
KIND_JOKER: Final[str] = LapTag.JOKER.value
KIND_PIT: Final[str] = LapTag.PIT.value

#: Russian labels for user-facing messages; the tag values stay English.
KIND_LABELS: Final[dict[str, str]] = {KIND_JOKER: "джокер", KIND_PIT: "пит"}

#: The two kinds a lap annotation can assert about an event, in report order.
EVENT_KINDS: Final[tuple[str, str]] = (KIND_JOKER, KIND_PIT)

#: Fewest timed laps (after `EventDetectionConfig.skip_first_lap`) a driver needs
#: before a median is a trustworthy baseline.  With one joker and one pit among
#: n laps the median is still a clean lap for n >= 3; below that the "baseline"
#: would be the anomaly itself and the detector would invent events.
MIN_BASELINE_LAPS: Final[int] = 3

#: Largest share of a driver's usable laps that may look like the *same* event
#: before the detection is refused.  The median is only a clean lap while the
#: anomalies are a minority; once half a short stint is slow (a driver who pits,
#: has an incident and retires) the median lands on the anomalies and the
#: ordinary laps become "joker" candidates -- tagging away the driver's real
#: best lap, the exact inversion of what this module exists for.  At the
#: intended scale (one joker and one pit in ~18 laps) the share is ~0.06.
MAX_CANDIDATE_SHARE: Final[float] = 0.5

#: A lap this much faster than the baseline is not a joker but proof that the
#: baseline is contaminated.  Physics bounds a lap from below and not from
#: above: pace sits near the limit of the kart, so a lap cannot be a third
#: quicker than the driver's own median, while any amount of *lost* time is
#: possible.  The joker is a shortcut worth ~1.9 s on a ~28 s lap (ratio ~0.93)
#: and the weakest one of the reference race is at 0.954, so the bound is far
#: from anything real.
IMPLAUSIBLE_JOKER_RATIO: Final[float] = 0.85

#: Share of the lap-time deviation that a single sector must carry for the
#: anomaly to count as localised, and that no other sector may reach (in either
#: direction).  On the reference race the joker's sector carries 140% of the lap
#: delta (the other sector gives some of it back) and the pit's carries 100%.
SECTOR_ANOMALY_SHARE: Final[float] = 0.5

#: Fewest baseline laps with a usable value for a per-sector median.
MIN_SECTOR_SAMPLES: Final[int] = 3

# Confidence weights: how far past the threshold the candidate is, and how big
# the empty band between it and the most extreme ordinary lap is.  The weights
# add up to 0.98, so a detection is never claimed to be beyond doubt.
CONFIDENCE_BASE: Final[float] = 0.15
CONFIDENCE_MARGIN_WEIGHT: Final[float] = 0.50
CONFIDENCE_SEPARATION_WEIGHT: Final[float] = 0.25
SECTOR_CONFIRMED_BONUS: Final[float] = 0.08
SECTOR_UNCONFIRMED_PENALTY: Final[float] = 0.25

#: Outcome of the sector check: localised, contradicted, or simply not testable
#: (a driver who is not the recipient of the email has no sector times at all,
#: and that absence of evidence must not count against the detection).
SECTOR_CONFIRMED: Final[str] = "confirmed"
SECTOR_UNCONFIRMED: Final[str] = "unconfirmed"
SECTOR_UNAVAILABLE: Final[str] = "unavailable"


@dataclass(slots=True, frozen=True)
class EventDetectionConfig:
    """Thresholds and expectations of the joker / pit detector.

    `require_single_sector` only decides whether a *failed* sector check counts
    against the event: sector localisation is always attempted when the data is
    there (only the recipient of an Apex email has sectors at all), it always
    fills `DetectedEvent.sector_index` and raises the confidence when it
    succeeds, and it never cancels an event.
    """

    pit_ratio: float = 1.25  # lap_time >= ratio * baseline -> pit candidate
    joker_ratio: float = 0.97  # lap_time <= ratio * baseline -> joker candidate
    #: Cap the *joker* at one per driver.  A joker sits only ~7% under the
    #: baseline, close enough to an ordinary tow lap that tagging every
    #: candidate would invent facts; the runners-up are reported instead.
    one_per_driver: bool = True
    #: How many pit stops one driver may have.  `None` means "as many as clear
    #: `pit_ratio`", which is the honest default: an endurance race mandates
    #: several stops, and a pit lap is unmistakable (in the reference data the
    #: worst ordinary lap sits at 1.12 x baseline against 1.44 for the mildest
    #: pit lap), so counting them needs no cap.  Deviations from the count the
    #: rest of the field shows are reported, not silently trimmed.
    max_pits_per_driver: int | None = None
    require_single_sector: bool = True  # if sectors exist, the anomaly must sit in one of them
    skip_first_lap: bool = True  # lap 1 (the start) is neither baseline nor candidate

    def limit_for(self, kind: str) -> int | None:
        """How many events of `kind` one driver may keep; `None` is unlimited."""
        if not self.one_per_driver:
            return None
        return self.max_pits_per_driver if kind == KIND_PIT else 1


@dataclass(slots=True)
class DetectedEvent:
    """One detected joker or pit-stop lap."""

    driver: str
    lap_number: int
    kind: str  # 'joker' | 'pit'
    ratio: float  # lap_time / baseline
    delta_ms: int  # lap_time - baseline
    #: 0-based index into `LapPoint.sectors` (S1 -> 0) of the sector that carries
    #: the anomaly; `None` when there is no sector data or it is inconclusive.
    sector_index: int | None = None
    confidence: float = 0.0  # 0..1
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Plain-JSON representation."""
        return asdict(self)


@dataclass(slots=True)
class EventReport:
    """Detected events plus every deviation from the "one of each" expectation."""

    events: list[DetectedEvent] = field(default_factory=list)
    drivers_without_joker: list[str] = field(default_factory=list)
    drivers_without_pit: list[str] = field(default_factory=list)
    drivers_with_multiple: list[str] = field(default_factory=list)
    #: Pit stops per driver, and the count the field agrees on.  A race mandates
    #: a fixed number of stops, but the detector is not told which -- it reads it
    #: off the field (the mode) and reports whoever deviates.  `expected_pits`
    #: is `None` when there is nothing to agree on (no drivers, no pit at all).
    pit_counts: dict[str, int] = field(default_factory=dict)
    expected_pits: int | None = None
    warnings: list[str] = field(default_factory=list)
    #: One proposal per driver of `drivers_without_pit` (SPEC §10.2): the pit
    #: stop is mandatory, so a missing one is a defect of the data or of the
    #: detection, and the slowest lap of that driver is offered -- with its
    #: `ratio` and `delta_ms` -- as the lap a human can confirm in one action.
    #: These are *not* detections: they carry `confidence = 0.0` and are never
    #: part of `events`, so nothing tags them automatically.
    pit_candidates: list[DetectedEvent] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Plain-JSON representation."""
        return asdict(self)


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #


def _seconds(value: float) -> str:
    """Milliseconds as seconds with three decimals, e.g. ``28.058``."""
    return f"{value / 1000.0:.3f}"


def _signed_seconds(value: float) -> str:
    """Milliseconds as a signed second delta, e.g. ``+12.284``."""
    return f"{value / 1000.0:+.3f}"


def _clamp01(value: float) -> float:
    return 0.0 if value < 0.0 else 1.0 if value > 1.0 else value


def _lap_list(numbers: Sequence[int]) -> str:
    """Lap numbers as ``4``, ``4 и 7`` or ``4, 7 и 9``."""
    items = [str(number) for number in numbers]
    if not items:
        return "none"
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} и {items[-1]}"


# --------------------------------------------------------------------------- #
# Per-driver detection
# --------------------------------------------------------------------------- #


def _is_timed(lap: LapPoint) -> bool:
    """Whether a lap carries a usable duration.

    `detect_events` is a public, storage-independent entry point, so a caller
    may hand it anything: a zero or negative time is not a fast lap but broken
    input, and reading it as one would manufacture a joker at ratio <= 0.
    """
    return lap.time_ms is not None and float(lap.time_ms) > 0.0


def _usable_laps(laps: Sequence[LapPoint], config: EventDetectionConfig) -> list[LapPoint]:
    """Timed laps eligible for the baseline and for candidacy.

    A lap without a positive time carries no information at all, and the first
    lap of the stint (the lowest lap number present, which is the standing start
    / out lap) is neither pace nor a possible event when `skip_first_lap` is set.
    """
    if not laps:
        return []
    first_number = min(int(lap.lap_number) for lap in laps)
    return [
        lap
        for lap in laps
        if _is_timed(lap)
        and not (config.skip_first_lap and int(lap.lap_number) == first_number)
    ]


def _unusable_lap_numbers(laps: Sequence[LapPoint], config: EventDetectionConfig) -> list[int]:
    """Lap numbers dropped for a non-positive time, so bad input stays visible."""
    if not laps:
        return []
    first_number = min(int(lap.lap_number) for lap in laps)
    return sorted(
        int(lap.lap_number)
        for lap in laps
        if lap.time_ms is not None
        and float(lap.time_ms) <= 0.0
        and not (config.skip_first_lap and int(lap.lap_number) == first_number)
    )


def _lap_tags(lap: LapPoint) -> frozenset[str]:
    """Annotations already in force on a lap, normalised.

    Storage feeds only *manual* tags here (SPEC §10.3): the detector's own
    previous output is not evidence about a lap, but a human's verdict is.
    """
    return frozenset(str(tag).strip().casefold() for tag in lap.tags if str(tag).strip())


def _declared_kind(lap: LapPoint) -> str | None:
    """The event a human has already pinned on this lap, if any."""
    tags = _lap_tags(lap)
    for kind in EVENT_KINDS:
        if kind in tags:
            return kind
    return None


def _candidate_kind(time_ms: float, baseline: float, config: EventDetectionConfig) -> str | None:
    """Which event, if any, a lap time looks like against `baseline`."""
    if time_ms >= config.pit_ratio * baseline:
        return KIND_PIT
    if time_ms <= config.joker_ratio * baseline:
        return KIND_JOKER
    return None


def _baseline(laps: Sequence[LapPoint], config: EventDetectionConfig) -> float:
    """Robust baseline of a driver, computed in two passes.

    The first pass takes the plain median of all usable laps; the second one
    recomputes it over the laps that the first pass did not consider candidates,
    so that the joker and the pit stop cannot drag the reference they are
    measured against.  If every lap looks like a candidate -- impossible for real
    data, since the median lap always sits at ratio 1.0 -- the rough median
    stands.
    """
    values = [float(lap.time_ms) for lap in laps if lap.time_ms is not None]
    rough = float(median(values))
    if rough <= 0.0:
        return rough
    remaining = [value for value in values if _candidate_kind(value, rough, config) is None]
    return float(median(remaining)) if remaining else rough


def _sector_medians(laps: Sequence[LapPoint], size: int) -> list[float | None]:
    """Per-sector medians over the baseline laps; `None` where there is too little data."""
    medians: list[float | None] = []
    for index in range(size):
        values = [
            float(lap.sectors[index])
            for lap in laps
            if len(lap.sectors) > index and lap.sectors[index] is not None
        ]
        medians.append(float(median(values)) if len(values) >= MIN_SECTOR_SAMPLES else None)
    return medians


def _localise_in_sector(
    lap: LapPoint,
    kind: str,
    lap_delta: float,
    baseline_laps: Sequence[LapPoint],
) -> tuple[int | None, str, str]:
    """Try to pin the lap-time anomaly on a single sector.

    Returns ``(sector_index, note, status)`` where `status` is one of
    `SECTOR_CONFIRMED`, `SECTOR_UNCONFIRMED` or `SECTOR_UNAVAILABLE`; the index
    is set only when the check confirms the anomaly.  Confirmation means that
    exactly one sector deviates from its own median by at least
    `SECTOR_ANOMALY_SHARE` of the whole lap deviation *in the expected direction*
    (slower for a pit stop, faster for a joker) while no other sector moves that
    much either way -- the joker is a shortcut and the pit lane is a detour, so
    both are physically confined to the sector that contains them.  Note that a
    sector may well give some of the anomaly back (WLAD111's joker gains 1.776 s
    in S1 and loses 0.531 s in S2), which is why the test is on the share of the
    lap deviation and not on the sector's own spread.
    """
    sectors = lap.sectors
    if not sectors or any(value is None for value in sectors):
        return None, "по этому кругу нет секторных времён", SECTOR_UNAVAILABLE
    if len(sectors) < 2:
        return None, "один сектор ничего не локализует", SECTOR_UNAVAILABLE
    medians = _sector_medians(baseline_laps, len(sectors))
    if any(value is None for value in medians):
        return None, "секторных данных не хватает, чтобы локализовать аномалию", SECTOR_UNAVAILABLE

    direction = 1.0 if kind == KIND_PIT else -1.0
    scale = abs(lap_delta)
    if scale <= 0.0:
        return None, "время круга равно базе, локализовать нечего", SECTOR_UNAVAILABLE

    deltas = [float(value) - float(base) for value, base in zip(sectors, medians, strict=True)]
    shares = [direction * delta / scale for delta in deltas]
    anomalous = [index for index, share in enumerate(shares) if share >= SECTOR_ANOMALY_SHARE]
    noisy = [index for index, share in enumerate(shares) if abs(share) >= SECTOR_ANOMALY_SHARE]
    detail = ", ".join(
        f"S{index + 1} {_signed_seconds(delta)}" for index, delta in enumerate(deltas)
    )
    if len(anomalous) == 1 and noisy == anomalous:
        index = anomalous[0]
        return (
            index,
            (
                f"аномалия заперта в секторе S{index + 1} "
                f"({_signed_seconds(deltas[index])} s vs its median, "
                f"{shares[index]:.0%} отклонения круга)"
            ),
            SECTOR_CONFIRMED,
        )
    return None, f"аномалия не заперта в одном секторе ({detail})", SECTOR_UNCONFIRMED


def _confidence(
    ratio: float,
    kind: str,
    other_ratios: Sequence[float],
    config: EventDetectionConfig,
) -> float:
    """How safe the call is, from the threshold margin and the gap to normal laps.

    Two independent things make a candidate convincing: how far past the
    threshold it is (``margin``), and how wide the empty band between it and the
    most extreme *ordinary* lap of the same driver is (``separation``).  Both are
    measured in units of the threshold's own distance from 1.0, so a pit stop at
    1.50 x with the worst normal lap at 1.12 x scores full marks on both, while
    a lap that only just crosses the line scores close to the base value.
    """
    if kind == KIND_PIT:
        scale = max(config.pit_ratio - 1.0, 1e-3)
        excess = ratio - config.pit_ratio
        nearest = max(other_ratios) if other_ratios else ratio
        gap = ratio - nearest
    else:
        scale = max(1.0 - config.joker_ratio, 1e-3)
        excess = config.joker_ratio - ratio
        nearest = min(other_ratios) if other_ratios else ratio
        gap = nearest - ratio
    margin = _clamp01(excess / scale)
    separation = _clamp01(gap / scale)
    return (
        CONFIDENCE_BASE
        + CONFIDENCE_MARGIN_WEIGHT * margin
        + CONFIDENCE_SEPARATION_WEIGHT * separation
    )


def _drop_implausible(
    driver: str,
    candidates: dict[str, list[LapPoint]],
    usable: Sequence[LapPoint],
    baseline: float,
) -> list[str]:
    """Refuse a whole kind when its candidates say the baseline is contaminated.

    The detector rests on one assumption: the anomalies are a minority, so the
    driver's median is an ordinary lap.  Two symptoms show that the assumption
    has failed, and in both cases the honest answer is "I cannot tell", not a
    joker invented on a perfectly good lap -- which would be worse than no
    detection at all, because `LapFilter.exclude_tags` would then throw that lap
    out of the pace statistics.

    * Too many laps of the same kind: at least `MAX_CANDIDATE_SHARE` of the
      stint (a driver who pits, has an incident and retires four laps in).
    * A joker faster than `IMPLAUSIBLE_JOKER_RATIO`: a shortcut worth a third of
      the lap does not exist, so the reference, not the lap, is wrong.

    Mutates `candidates` in place and returns the warnings to report.
    """
    warnings: list[str] = []
    limit = MAX_CANDIDATE_SHARE * len(usable)
    for kind in EVENT_KINDS:
        group = candidates[kind]
        if not group:
            continue
        reason: str | None = None
        kind_ru = KIND_LABELS.get(kind, kind)
        if len(group) >= limit:
            reason = (
                f"на {kind_ru} похожи {len(group)} из {len(usable)} кругов с временем, поэтому "
                f"медиана не является чистым кругом и базе доверять нельзя"
            )
        elif kind == KIND_JOKER:
            wild = [
                lap for lap in group if float(lap.time_ms) / baseline <= IMPLAUSIBLE_JOKER_RATIO
            ]
            if wild:
                reason = (
                    f"круг {_lap_list([int(lap.lap_number) for lap in wild])} оказался бы джокером "
                    f"с отношением {min(float(lap.time_ms) / baseline for lap in wild):.3f} — "
                    f"быстрее, чем способна сделать любая срезка, так что загрязнена база "
                    f"{_seconds(baseline)}, а не круг исключителен"
                )
        if reason is None:
            continue
        warnings.append(
            f"{driver}: {reason}; {kind_ru} для этого пилота не заявлен — "
            f"разметьте круги вручную, если знаете, что произошло"
        )
        candidates[kind] = []
    return warnings


def _pit_proposal(
    driver: str,
    usable: Sequence[LapPoint],
    baseline: float,
    baseline_laps: Sequence[LapPoint],
) -> DetectedEvent | None:
    """The lap to offer when no pit stop was detected (SPEC §10.2).

    The pit stop is mandatory in this format, so "no pit found" is a problem to
    resolve rather than a fact about the race.  The slowest lap of the driver is
    the only sensible starting point, and it comes with the same numbers as a
    real detection so that a human can judge and confirm it in one action.
    `confidence` is 0.0 on purpose: the detector proposes, it does not claim.
    """
    if not usable or baseline <= 0.0:
        return None
    lap = max(usable, key=lambda item: (float(item.time_ms), -int(item.lap_number)))
    time_ms = float(lap.time_ms)
    lap_delta = time_ms - baseline
    sector_index, sector_note, _ = _localise_in_sector(lap, KIND_PIT, lap_delta, baseline_laps)
    return DetectedEvent(
        driver=driver,
        lap_number=int(lap.lap_number),
        kind=KIND_PIT,
        ratio=time_ms / baseline,
        delta_ms=int(round(lap_delta)),
        sector_index=sector_index,
        confidence=0.0,
        note=(
            f"предложенный пит (ниже порога детекции): самый медленный круг, "
            f"{_seconds(time_ms)} против базы {_seconds(baseline)} "
            f"({_signed_seconds(lap_delta)} s, ratio {time_ms / baseline:.3f}); {sector_note}"
        ),
    )


def _detect_for_driver(
    driver: str,
    laps: Sequence[LapPoint],
    config: EventDetectionConfig,
) -> tuple[list[DetectedEvent], dict[str, int], list[str], DetectedEvent | None]:
    """Detect the events of one driver.

    Returns ``(events, event_counts_by_kind, warnings, pit_proposal)``.  The
    counts are of *candidates and human verdicts*, not of emitted events, so
    that a driver who produced two pit candidates is reported as such even when
    `one_per_driver` kept only one.  `pit_proposal` is set only when the driver
    has neither a detected nor a hand-annotated pit stop.

    `LapPoint.tags` carries the annotations a human has already made (SPEC
    §10.3), and they outrank the detector: a lap annotated ``joker`` or ``pit``
    *is* that event and is never classified again, and a lap annotated anything
    else is still classified -- the verdict is reported so the caller can show
    and store it -- but it no longer counts towards the "one of each per driver"
    tally, because the human's tag is what is in force.
    """
    warnings: list[str] = []
    usable = _usable_laps(laps, config)
    broken = _unusable_lap_numbers(laps, config)
    if broken:
        warnings.append(
            f"{driver}: круг {_lap_list(broken)} имеет неположительное время и был "
            f"проигнорирован; круг не может длиться ноль секунд или меньше"
        )
    if len(usable) < MIN_BASELINE_LAPS:
        warnings.append(
            f"{driver}: доступно всего {len(usable)} кругов с временем, "
            f"для базы нужно минимум {MIN_BASELINE_LAPS}; детекция пропущена"
        )
        return [], {}, warnings, None

    # Human verdicts come first.  A declared joker or pit is a known non-pace
    # lap, so it is kept out of the baseline and never classified again.
    declared: dict[str, list[LapPoint]] = {KIND_JOKER: [], KIND_PIT: []}
    annotated_numbers: set[int] = set()
    declared_numbers: set[int] = set()
    for lap in usable:
        if not _lap_tags(lap):
            continue
        annotated_numbers.add(int(lap.lap_number))
        kind = _declared_kind(lap)
        if kind is not None:
            declared[kind].append(lap)
            declared_numbers.add(int(lap.lap_number))
    pace_pool = [lap for lap in usable if int(lap.lap_number) not in declared_numbers]

    baseline = _baseline(pace_pool or usable, config)
    if baseline <= 0.0:
        warnings.append(f"{driver}: базовое время круга неположительно; детекция пропущена")
        return [], {}, warnings, None

    candidates: dict[str, list[LapPoint]] = {KIND_PIT: [], KIND_JOKER: []}
    for lap in usable:
        if int(lap.lap_number) in declared_numbers:
            continue
        kind = _candidate_kind(float(lap.time_ms), baseline, config)
        if kind is not None:
            candidates[kind].append(lap)

    warnings.extend(_drop_implausible(driver, candidates, usable, baseline))

    candidate_numbers = {
        int(lap.lap_number) for group in candidates.values() for lap in group
    } | {int(lap.lap_number) for group in declared.values() for lap in group}
    baseline_laps = [lap for lap in usable if int(lap.lap_number) not in candidate_numbers]

    events: list[DetectedEvent] = []
    counts: dict[str, int] = {}
    for kind in EVENT_KINDS:
        kind_ru = KIND_LABELS.get(kind, kind)
        group = candidates[kind]
        confirmed = declared[kind]
        # An automatic verdict on a lap a human has already annotated is not in
        # force -- the manual tag hides it (SPEC §10.3) -- but it is still
        # reported, so the caller can show "the detector says joker, you said
        # traffic" and so that clearing the manual tag restores the detection.
        overridden = [lap for lap in group if int(lap.lap_number) in annotated_numbers]
        group = [lap for lap in group if int(lap.lap_number) not in annotated_numbers]
        if not group and not confirmed and not overridden:
            continue
        group_numbers = {int(lap.lap_number) for lap in group + confirmed + overridden}
        limit = config.limit_for(kind)
        if limit is not None and len(confirmed) >= limit and group:
            # The human already filled the quota for this kind; the detector's
            # own guesses step aside rather than adding more (SPEC §10.3).
            for lap in group:
                warnings.append(
                    f"{driver}: круг {int(lap.lap_number)} тоже похож на {kind_ru} "
                    f"(отношение {float(lap.time_ms) / baseline:.3f}), но не выбран: "
                    f"круг {_lap_list([int(item.lap_number) for item in confirmed])} "
                    f"размечен как {kind_ru} вручную"
                )
            group = []
        elif limit is not None and len(group) + len(confirmed) > limit:
            # Most extreme first; ties go to the earlier lap.
            ordered = sorted(
                group,
                key=(
                    (lambda lap: (-float(lap.time_ms), int(lap.lap_number)))
                    if kind == KIND_PIT
                    else (lambda lap: (float(lap.time_ms), int(lap.lap_number)))
                ),
            )
            keep = ordered[: max(0, limit - len(confirmed))]
            kept_numbers = {int(lap.lap_number) for lap in keep}
            for lap in group:
                if int(lap.lap_number) in kept_numbers:
                    continue
                warnings.append(
                    f"{driver}: круг {int(lap.lap_number)} тоже похож на {kind_ru} "
                    f"(отношение {float(lap.time_ms) / baseline:.3f}), но не выбран: "
                    f"круг {_lap_list(sorted(kept_numbers))} — более выраженный кандидат"
                )
            group = sorted(keep, key=lambda lap: int(lap.lap_number))

        # Counted after trimming: a candidate that was rejected is reported in
        # `warnings`, not carried into the per-driver totals the session-level
        # consensus is built from.
        counts[kind] = len(group) + len(confirmed)

        # Separation is measured against the laps that are *not* candidates of
        # this kind: the empty band between the event and ordinary racing.
        other_ratios = [
            float(item.time_ms) / baseline
            for item in usable
            if int(item.lap_number) not in group_numbers
        ]
        for lap in confirmed + group + overridden:
            declared_here = int(lap.lap_number) in declared_numbers
            time_ms = float(lap.time_ms)
            ratio = time_ms / baseline
            lap_delta = time_ms - baseline
            confidence = (
                1.0 if declared_here else _confidence(ratio, kind, other_ratios, config)
            )
            sector_index, sector_note, status = _localise_in_sector(
                lap, kind, lap_delta, baseline_laps
            )
            if declared_here:
                pass  # A human verdict is not up for revision by the sector check.
            elif status == SECTOR_CONFIRMED:
                confidence += SECTOR_CONFIRMED_BONUS
            elif status == SECTOR_UNCONFIRMED and config.require_single_sector:
                # Sector times exist and contradict the "one sector" rule: that
                # weakens the call, but it does not cancel a 13 s lap.
                confidence -= SECTOR_UNCONFIRMED_PENALTY
            if declared_here:
                headline = f"{KIND_LABELS.get(kind, kind)} размечен вручную"
            elif int(lap.lap_number) in annotated_numbers:
                headline = f"{KIND_LABELS.get(kind, kind)} (перекрыт ручной разметкой этого круга)"
            else:
                headline = kind_ru
            events.append(
                DetectedEvent(
                    driver=driver,
                    lap_number=int(lap.lap_number),
                    kind=kind,
                    ratio=float(ratio),
                    delta_ms=int(round(lap_delta)),
                    sector_index=sector_index,
                    confidence=round(_clamp01(confidence), 4),
                    note=(
                        f"{headline}: {_seconds(time_ms)} против базы {_seconds(baseline)} "
                        f"({_signed_seconds(lap_delta)} с, отношение {ratio:.3f}); {sector_note}"
                    ),
                )
            )

    events.sort(key=lambda event: event.lap_number)
    proposal = (
        None
        if counts.get(KIND_PIT, 0) > 0
        else _pit_proposal(driver, usable, baseline, baseline_laps)
    )
    return events, counts, warnings, proposal


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


def detect_events(
    laps_by_driver: Mapping[str, Sequence[LapPoint]],
    config: EventDetectionConfig = EventDetectionConfig(),
) -> EventReport:
    """Find the joker lap and the pit-stop lap of every driver in a session.

    Each driver is judged against their own robust baseline (see `_baseline`),
    never against the field, so a slow driver is not accused of pitting every
    lap.  The session-level expectation -- exactly one joker and exactly one pit
    per driver -- is checked but never enforced: missing and surplus events are
    reported in `EventReport` for manual annotation instead of being invented or
    hidden.  That annotation comes back in through `LapPoint.tags`, and it is
    the last word: a hand-tagged joker or pit is counted as the driver's event
    (see `_detect_for_driver`), so a session a human has resolved reports itself
    resolved.

    Driver order of the input mapping is preserved in every list of the report,
    and events of one driver are ordered by lap number, so the result is stable
    between runs.  Callers that read from storage pass drivers in classification
    order, which is the order the lists come back in.
    """
    report = EventReport()
    if config.pit_ratio <= config.joker_ratio:
        report.warnings.append(
            f"Вырожденная конфигурация: pit_ratio {config.pit_ratio} не больше "
            f"joker_ratio {config.joker_ratio}; круги, подходящие под оба правила, "
            f"отнесены к питам"
        )

    proposals: dict[str, DetectedEvent] = {}
    joker_counts: dict[str, int] = {}
    for driver, laps in laps_by_driver.items():
        events, counts, warnings, proposal = _detect_for_driver(driver, laps, config)
        report.events.extend(events)
        report.warnings.extend(warnings)
        report.pit_counts[driver] = counts.get(KIND_PIT, 0)
        joker_counts[driver] = counts.get(KIND_JOKER, 0)
        if proposal is not None:
            proposals[driver] = proposal
        if counts.get(KIND_JOKER, 0) == 0:
            # A joker driven with a mistake is statistically an ordinary lap, so
            # a missing one is a soft signal and not a warning (SPEC §10.2).
            report.drivers_without_joker.append(driver)

    # How many stops this race mandates is a property of the race, not of our
    # configuration: read it off the field instead of assuming one.
    stopped = [count for count in report.pit_counts.values() if count > 0]
    if stopped:
        report.expected_pits = max(sorted(set(stopped)), key=stopped.count)

    for driver, count in report.pit_counts.items():
        if count == 0:
            report.drivers_without_pit.append(driver)
            proposal = proposals.get(driver)
            expected = report.expected_pits
            demand = (
                "заезжать на пит обязаны все"
                if expected is None
                else f"остальные пилоты заезжали {expected} раз(а)"
            )
            if proposal is not None:
                report.pit_candidates.append(proposal)
                report.warnings.append(
                    f"{driver}: пит не обнаружен, хотя {demand} — это проблема данных или "
                    f"детекции, а не факт гонки. Самый медленный круг — "
                    f"{proposal.lap_number}, отношение {proposal.ratio:.3f} "
                    f"({_signed_seconds(proposal.delta_ms)} с к базе); "
                    f"подтвердите вручную, если это был пит"
                )
            else:
                report.warnings.append(
                    f"{driver}: пит не обнаружен и предложить круг не из чего, хотя {demand}; "
                    f"данные этой сессии по нему неполны"
                )
        elif report.expected_pits is not None and count != report.expected_pits:
            report.warnings.append(
                f"{driver}: обнаружено питов: {count}, тогда как у остальных {report.expected_pits} — "
                f"проверьте круги вручную"
            )

    for driver in report.pit_counts:
        deviates_pit = (
            report.expected_pits is not None
            and report.pit_counts[driver] not in (0, report.expected_pits)
        )
        if joker_counts.get(driver, 0) > 1 or deviates_pit:
            report.drivers_with_multiple.append(driver)
    return report
