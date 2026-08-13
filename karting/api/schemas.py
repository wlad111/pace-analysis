"""Pydantic schemas for the HTTP API.

Only the payloads the API fully owns are modelled here (health, tag dictionary,
lap-tag body, import reports, echoed filter).  Session/lap/ranking payloads are
passed through from `karting.storage` as plain JSON dicts so that the storage
layer stays the single source of truth for their shape (SPEC §8.2/§8.4).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids a hard import cycle
    from karting.stats import LapFilter
    from karting.stats.events import EventDetectionConfig
    from karting.storage import ImportReport


ImportStatus = Literal["imported", "merged", "already_imported", "failed"]

#: Wording shown next to every number that comes from the email's own "Best lap"
#: column: in this race format the official best lap is usually the joker lap
#: (SPEC §10.4), so those leaderboards are not a pace metric.
JOKER_INFLATED_LABEL: str = "official, joker-inflated"
JOKER_INFLATED_NOTE: str = (
    "Built from the official best laps of the email. In this format every driver runs one "
    "mandatory joker lap (~1.9 s faster), so these times are most likely joker laps and not "
    "representative pace. Compare them with the clean best lap of the pace table."
)


class ErrorResponse(BaseModel):
    """Standard error envelope used for every non-2xx response."""

    detail: str


class HealthResponse(BaseModel):
    """Answer of `GET /api/health`."""

    status: Literal["ok"] = "ok"
    sessions: int = Field(ge=0, description="Number of sessions stored in the database")


class TagOption(BaseModel):
    """One entry of the lap-tag dictionary (`GET /api/tags`)."""

    value: str
    label: str


class LapTagCreate(BaseModel):
    """Body of `POST /api/laps/{lap_id}/tags`."""

    model_config = ConfigDict(extra="forbid")

    tag: str = Field(
        min_length=1,
        max_length=32,
        description="Tag value, e.g. 'penalty', 'traffic', 'pit'.",
    )
    note: str | None = Field(
        default=None,
        max_length=500,
        description="Free-form note explaining why the lap was tagged.",
    )


class FilterInfo(BaseModel):
    """The `LapFilter` actually applied to a request, echoed back to the client."""

    mad_k: float
    drop_missing: bool
    drop_first_lap: bool
    drop_slow_outliers: bool
    drop_fast_outliers: bool
    exclude_tags: list[str]
    min_laps: int

    @classmethod
    def from_filter(cls, flt: LapFilter) -> FilterInfo:
        """Build the echo payload from a `karting.stats.LapFilter`."""
        return cls(
            mad_k=float(flt.mad_k),
            drop_missing=bool(getattr(flt, "drop_missing", True)),
            drop_first_lap=bool(flt.drop_first_lap),
            drop_slow_outliers=bool(flt.drop_slow_outliers),
            drop_fast_outliers=bool(flt.drop_fast_outliers),
            exclude_tags=sorted(str(tag) for tag in flt.exclude_tags),
            min_laps=int(flt.min_laps),
        )


class EventConfigInfo(BaseModel):
    """The joker/pit detection thresholds actually used, echoed to the client."""

    pit_ratio: float
    joker_ratio: float
    one_per_driver: bool
    require_single_sector: bool
    skip_first_lap: bool

    @classmethod
    def from_config(cls, config: EventDetectionConfig) -> EventConfigInfo:
        """Build the echo payload from a `karting.stats.events.EventDetectionConfig`."""
        return cls(
            pit_ratio=float(config.pit_ratio),
            joker_ratio=float(config.joker_ratio),
            one_per_driver=bool(config.one_per_driver),
            require_single_sector=bool(getattr(config, "require_single_sector", True)),
            skip_first_lap=bool(getattr(config, "skip_first_lap", True)),
        )


class DetectedEventOut(BaseModel):
    """One detected joker or pit lap (`karting.stats.events.DetectedEvent`).

    The three trailing fields are storage state rather than detector output: the
    lap the event points at, whether the matching tag is in force right now and
    whether a human decided otherwise (SPEC §10.3 — a manual annotation wins).
    """

    driver: str
    lap_number: int
    kind: str = Field(description="'joker' or 'pit'.")
    ratio: float | None = Field(
        default=None, description="lap_time / robust baseline of the driver."
    )
    delta_ms: int | None = Field(default=None, description="lap_time - baseline, in milliseconds.")
    sector_index: int | None = Field(
        default=None, description="0-based index of the sector holding the anomaly, when confirmed."
    )
    confidence: float | None = None
    note: str = ""
    time_ms: int | None = None
    lap_id: int | None = None
    applied: bool = Field(default=False, description="The tag is currently effective on that lap.")
    overridden_by_manual: bool = Field(
        default=False, description="A human annotated that lap, so the detector is ignored for it."
    )


class EventReportOut(BaseModel):
    """Answer of `GET`/`POST /api/sessions/{id}/events` (SPEC §10.2).

    `drivers_without_joker` / `drivers_without_pit` are not errors: every driver
    takes the joker once and pits at least once, so a missing one is an
    invitation to annotate the lap by hand.  A missing pit stop is the more
    serious of the two, so it also arrives with the lap to confirm in
    `pit_candidates`.  How many stops the race mandated is not assumed: it is
    read off the field into `expected_pits`, and `drivers_with_multiple` lists
    whoever disagrees with it.
    """

    session_id: int
    config: EventConfigInfo
    events: list[DetectedEventOut] = Field(default_factory=list)
    drivers_without_joker: list[str] = Field(default_factory=list)
    drivers_without_pit: list[str] = Field(default_factory=list)
    drivers_with_multiple: list[str] = Field(default_factory=list)
    pit_counts: dict[str, int] = Field(
        default_factory=dict,
        description="Pit stops detected per driver.",
    )
    expected_pits: int | None = Field(
        default=None,
        description=(
            "How many stops the field agrees on (the mode of `pit_counts` over drivers "
            "who stopped at all); `None` when there is nothing to agree on."
        ),
    )
    pit_candidates: list[DetectedEventOut] = Field(
        default_factory=list,
        description=(
            "One proposal per driver of `drivers_without_pit`: their slowest lap, with "
            "`confidence = 0` because the detector suggests rather than claims it (SPEC §10.2)."
        ),
    )
    warnings: list[str] = Field(default_factory=list)
    counts: dict[str, int] = Field(
        default_factory=dict, description="Number of drivers, joker events and pit events."
    )
    persisted: bool = Field(
        default=False, description="True when the run rewrote the automatic lap tags."
    )
    complete: bool = Field(
        default=False, description="True when every driver has exactly one joker and one pit."
    )


class RankingsOut(BaseModel):
    """Answer of `GET /api/sessions/{id}/rankings`.

    The leaderboards are copied verbatim from the email, and the email ranks
    drivers by their official best lap — a joker lap for five of the six drivers
    of the reference race.  The flags say so explicitly (SPEC §10.4).
    """

    weekly_best: list[dict[str, Any]] = Field(default_factory=list)
    track_record: list[dict[str, Any]] = Field(default_factory=list)
    official_best_based: bool = True
    joker_inflated: bool = True
    label: str = JOKER_INFLATED_LABEL
    note: str = JOKER_INFLATED_NOTE


class ImportReportOut(BaseModel):
    """Per-file result of `POST /api/imports`.

    Mirrors `karting.storage.ImportReport` and adds the originating file name, a
    machine readable `status` and a human readable `detail`, so that a single
    failing file inside a batch is reported instead of failing the whole call.
    """

    filename: str
    status: ImportStatus
    detail: str
    session_id: int | None = None
    club_id: int | None = None
    session_created: bool = False
    already_imported: bool = False
    inserted_laps: int = 0
    updated_laps: int = 0
    inserted_entries: int = 0
    #: Joker / pit laps the detector tagged during this import (SPEC §10.3).
    #: Zero on an `already_imported` file: nothing was written, so nothing was
    #: re-tagged either.
    auto_jokers: int = 0
    auto_pits: int = 0
    drivers_without_joker: list[str] = Field(default_factory=list)
    drivers_without_pit: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @classmethod
    def from_report(cls, filename: str, report: ImportReport, *, session_name: str | None = None) -> ImportReportOut:
        """Wrap a storage `ImportReport`, deriving `status` and `detail`."""
        status: ImportStatus
        title = f'#{report.session_id}' + (f' "{session_name}"' if session_name else "")
        jokers = int(getattr(report, "auto_jokers", 0) or 0)
        pits = int(getattr(report, "auto_pits", 0) or 0)
        # The joker and the pit stop are the two laps that are not pace, so how
        # many of them the import found is part of "what happened" (SPEC §10.3).
        events = f" Размечено джокеров: {jokers}, питов: {pits}."
        if report.already_imported:
            status = "already_imported"
            detail = f"Уже импортировано ранее, ничего не изменилось (сессия {title})."
        elif report.session_created:
            status = "imported"
            detail = (
                f"Импортирована сессия {title}: "
                f"участников: {report.inserted_entries}, кругов: {report.inserted_laps}." + events
            )
        else:
            status = "merged"
            detail = (
                f"Дополнена существующая сессия {title}: "
                f"+{report.inserted_entries} участников, +{report.inserted_laps} кругов, "
                f"{report.updated_laps} кругов дополнено секторами." + events
            )
        return cls(
            filename=filename,
            status=status,
            detail=detail,
            session_id=report.session_id,
            club_id=report.club_id,
            session_created=bool(report.session_created),
            already_imported=bool(report.already_imported),
            inserted_laps=int(report.inserted_laps),
            updated_laps=int(report.updated_laps),
            inserted_entries=int(report.inserted_entries),
            auto_jokers=jokers,
            auto_pits=pits,
            drivers_without_joker=[
                str(item) for item in getattr(report, "drivers_without_joker", []) or []
            ],
            drivers_without_pit=[
                str(item) for item in getattr(report, "drivers_without_pit", []) or []
            ],
            conflicts=[str(item) for item in report.conflicts],
            warnings=[str(item) for item in report.warnings],
        )

    @classmethod
    def failure(cls, filename: str, detail: str, *, warnings: list[str] | None = None) -> ImportReportOut:
        """Report for a file that could not be parsed or stored."""
        return cls(filename=filename, status="failed", detail=detail, warnings=warnings or [])
