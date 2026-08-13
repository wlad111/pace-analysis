"""Domain model for karting race results.

These dataclasses are the contract shared by the parser, the storage layer, the
stats layer and the API.  Every duration is an integer number of milliseconds;
`None` means "no time recorded" and must never be silently turned into 0.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any


# --------------------------------------------------------------------------- #
# Enumerations
# --------------------------------------------------------------------------- #


class RankingKind(str, Enum):
    """Which leaderboard a `RankingEntry` came from."""

    WEEKLY_BEST = "weekly_best"
    TRACK_RECORD = "track_record"


class LapTag(str, Enum):
    """Annotation applied to a single lap, explaining why it is (not) race pace.

    The importer never writes these.  They come either from the automatic
    joker/pit detector (`source='auto'`) or from a human (`source='manual'`);
    a manual annotation of a lap always overrides the automatic one.
    """

    PENALTY = "penalty"
    JOKER = "joker"  # mandatory shortcut lap: one per driver, ~1.9 s faster
    PIT = "pit"  # mandatory pit stop lap: one per driver, ~13 s slower
    BOOST = "boost"
    TRAFFIC = "traffic"
    INCIDENT = "incident"
    OUTLIER = "outlier"
    INVALID = "invalid"
    CLEAN = "clean"


# --------------------------------------------------------------------------- #
# Core entities
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Club:
    """Venue / organiser that runs the sessions."""

    name: str
    external_id: str | None = None  # Apex Timing "center" id, e.g. "51"
    website: str | None = None
    email: str | None = None


@dataclass(slots=True)
class Driver:
    """A competitor, identified by the nickname shown in the timing system."""

    nickname: str
    external_id: str | None = None  # Apex "client" id; known only for the recipient


@dataclass(slots=True)
class Session:
    """One race / heat / qualifying run."""

    name: str  # "PRIMO GARA - Final A"
    started_at: datetime | None  # naive local time at the venue
    code: str | None = None  # "FA"
    track: str | None = None  # "Karting track"
    category: str | None = None  # "SR5" (kart model / class)
    tz_name: str | None = None  # venue timezone, if ever known


@dataclass(slots=True)
class SessionEntry:
    """A driver's line in the session classification."""

    driver: Driver
    position: int | None = None
    kart: str | None = None
    laps_count: int | None = None
    gap_ms: int | None = None  # gap to the leader; None for the leader
    gap_laps: int | None = None  # set instead of gap_ms when the gap is "N Laps"
    best_lap_ms: int | None = None


@dataclass(slots=True)
class Lap:
    """A single lap of a single driver.

    `sectors` is empty when the email does not carry sector data for that
    driver (Apex only sends sectors for the recipient of the email).
    """

    driver: Driver
    lap_number: int
    time_ms: int | None = None
    sectors: list[int | None] = field(default_factory=list)
    is_best: bool = False  # highlighted as the driver's best lap in the email


@dataclass(slots=True)
class RankingEntry:
    """A row of a truncated leaderboard (week bests / all-time track records)."""

    kind: RankingKind
    rank: int
    driver: Driver
    best_lap_ms: int | None = None
    category: str | None = None


@dataclass(slots=True)
class HistoryEntry:
    """A row of the recipient's "Your last sessions" table."""

    date: date | None
    position: int | None = None
    best_lap_ms: int | None = None
    laps_count: int | None = None
    category: str | None = None


@dataclass(slots=True)
class Provenance:
    """Where the parsed data came from."""

    message_id: str | None = None
    subject: str | None = None
    sent_at: datetime | None = None
    from_name: str | None = None
    from_email: str | None = None
    recipient_email: str | None = None
    recipient_nickname: str | None = None
    source_path: str | None = None
    sha256: str | None = None


@dataclass(slots=True)
class UnparsedBlock:
    """A table the parser recognised as data-like but could not classify."""

    header: list[str]
    rows: list[list[str]]
    note: str = ""


@dataclass(slots=True)
class ParsedEmail:
    """Everything extracted from one Apex Timing result email."""

    club: Club
    session: Session
    provenance: Provenance
    entries: list[SessionEntry] = field(default_factory=list)
    laps: list[Lap] = field(default_factory=list)
    rankings: list[RankingEntry] = field(default_factory=list)
    history: list[HistoryEntry] = field(default_factory=list)
    podium: list[tuple[int, str]] = field(default_factory=list)  # (position, nickname)
    warnings: list[str] = field(default_factory=list)
    unparsed: list[UnparsedBlock] = field(default_factory=list)

    def laps_by_driver(self) -> dict[str, list[Lap]]:
        """Laps grouped by driver nickname, each list ordered by lap number."""
        grouped: dict[str, list[Lap]] = {}
        for lap in self.laps:
            grouped.setdefault(lap.driver.nickname, []).append(lap)
        for laps in grouped.values():
            laps.sort(key=lambda item: item.lap_number)
        return grouped

    def to_dict(self) -> dict[str, Any]:
        """Plain-JSON representation (used by the CLI's `export` and by tests)."""
        from dataclasses import asdict

        def _default(value: Any) -> Any:
            if isinstance(value, datetime):
                return value.isoformat()
            if isinstance(value, date):
                return value.isoformat()
            if isinstance(value, Enum):
                return value.value
            return value

        def _walk(value: Any) -> Any:
            if isinstance(value, dict):
                return {key: _walk(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [_walk(item) for item in value]
            return _default(value)

        return _walk(asdict(self))


class ParseError(Exception):
    """Raised by the parser in strict mode, or on structurally broken input."""
