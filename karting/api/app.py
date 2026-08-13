"""FastAPI application exposing the pace-analysis data (SPEC §8.4).

The API is a thin layer on top of the other packages:

* `karting.parsing` turns an uploaded `.eml` into a `ParsedEmail`;
* `karting.storage` persists it and answers every read query;
* `karting.stats` computes pace metrics from filtered laps.

The database file is taken from the ``PACE_DB`` environment variable (default
``data/pace.db``) and opened per request, so tests and a dev server never share
a connection.  A handful of small adapters below normalise the storage rows to
the JSON shapes of SPEC §8.4; they are public because `karting.cli` reuses them.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import sqlite3
import uuid
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from dataclasses import fields as dataclass_fields
from dataclasses import is_dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Final

from fastapi import (
    Depends,
    FastAPI,
    File,
    HTTPException,
    Path as PathParam,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from karting.api.schemas import (
    JOKER_INFLATED_LABEL,
    JOKER_INFLATED_NOTE,
    DetectedEventOut,
    ErrorResponse,
    EventConfigInfo,
    EventReportOut,
    FilterInfo,
    HealthResponse,
    ImportReportOut,
    LapTagCreate,
    RankingsOut,
    TagOption,
)
from karting.models import LapTag
from karting.parsing import parse_email_bytes
from karting.stats import (
    LapFilter,
    LapPoint,
    PaceStats,
    classify_laps,
    compare_drivers,
    pace_delta_to_best_driver,
    pace_stats,
)
from karting.stats.events import (
    EventDetectionConfig,
    EventReport,
    detect_events,
)
from karting.storage import Database, StorageError, open_db

__all__ = [
    "app",
    "build_event_config",
    "create_app",
    "database_path",
    "detect_session_events",
    "effective_annotations",
    "effective_tags",
    "empty_pace_stats",
    "event_report_payload",
    "jsonable",
    "lap_points_by_driver",
    "load_session",
    "normalise_tags",
    "open_database",
    "pace_rows",
    "tag_dictionary",
]

LOGGER: Final[logging.Logger] = logging.getLogger("karting.api")

DEFAULT_DB_PATH: Final[str] = "data/pace.db"
#: Largest value a SQLite ``INTEGER`` (and therefore any row id) can hold.
MAX_ROW_ID: Final[int] = 2**63 - 1
DEFAULT_CORS_ORIGINS: Final[tuple[str, ...]] = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
)
MAX_UPLOAD_BYTES: Final[int] = 32 * 1024 * 1024
#: Default joker/pit thresholds of SPEC §10.1, repeated here so that the query
#: parameters keep their documented defaults without importing the detector.
DEFAULT_PIT_RATIO: Final[float] = 1.25
DEFAULT_JOKER_RATIO: Final[float] = 0.97
#: Where a lap annotation came from (SPEC §10.3).
TAG_SOURCE_MANUAL: Final[str] = "manual"
TAG_SOURCE_AUTO: Final[str] = "auto"
_TAG_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_TAG_LABELS: Final[dict[str, str]] = {
    LapTag.PENALTY.value: "Штраф",
    LapTag.JOKER.value: "Джокер (обязательная срезка)",
    LapTag.BOOST.value: "Буст / слипстрим",
    LapTag.PIT.value: "Пит или выездной круг",
    LapTag.TRAFFIC.value: "Трафик",
    LapTag.INCIDENT.value: "Инцидент",
    LapTag.OUTLIER.value: "Выброс",
    LapTag.INVALID.value: "Некорректный",
    LapTag.CLEAN.value: "Чистый",
}


# --------------------------------------------------------------------------- #
# JSON helpers
# --------------------------------------------------------------------------- #


def jsonable(value: Any) -> Any:
    """Recursively convert `value` into something `json.dumps` accepts.

    Dataclasses (stats results), enums, dates, sets and numpy scalars are
    converted; non-finite floats become ``None`` because the JSON responses are
    rendered with ``allow_nan=False`` (a NaN p-value must not break a response).
    """
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Enum):
        return jsonable(value.value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if hasattr(value, "tolist") and hasattr(value, "dtype"):  # numpy scalar / array
        return jsonable(value.tolist())
    if is_dataclass(value) and not isinstance(value, type):
        to_dict = getattr(value, "to_dict", None)
        if callable(to_dict):
            return jsonable(to_dict())
        return {field.name: jsonable(getattr(value, field.name)) for field in dataclass_fields(value)}
    if isinstance(value, Mapping):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return [jsonable(item) for item in sorted(value, key=str)]
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    return str(value)


# --------------------------------------------------------------------------- #
# Storage adapters
# --------------------------------------------------------------------------- #


def driver_name(value: Any) -> str:
    """Nickname out of a storage field that may be a string or a driver dict."""
    if value is None:
        return ""
    if isinstance(value, Mapping):
        for key in ("nickname", "driver", "name"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
        return ""
    return str(value)


def row_driver(row: Mapping[str, Any]) -> str:
    """Nickname of a storage row, whichever field carries it."""
    return driver_name(row.get("driver") or row.get("nickname"))


def normalise_tags(tags: Any) -> list[dict[str, Any]]:
    """Lap annotations as ``{tag, source, note, ...}`` dicts.

    A storage row may carry plain strings or dicts; an annotation without an
    explicit source counts as manual, because only the joker/pit detector ever
    writes ``'auto'`` (SPEC §10.3).
    """
    if not tags:
        return []
    items: list[dict[str, Any]] = []
    for item in tags:
        if isinstance(item, Mapping):
            name = ""
            for key in ("tag", "value", "name"):
                candidate = item.get(key)
                if isinstance(candidate, str) and candidate:
                    name = candidate
                    break
            if not name:
                continue
            entry = dict(item)
            source = entry.get("source")
            entry["tag"] = name
            entry["source"] = source if isinstance(source, str) and source else TAG_SOURCE_MANUAL
            items.append(entry)
        elif isinstance(item, str) and item:
            items.append({"tag": item, "source": TAG_SOURCE_MANUAL, "note": None})
    return items


def lap_annotations(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every annotation of a lap row, both sources.

    `Database.session_laps` reports the raw rows under ``annotations`` and the
    effective ones under ``tags``; a row that only carries ``tags`` (an older
    storage, or a row already adapted by `normalise_lap`) still works.
    """
    raw = row.get("annotations")
    return normalise_tags(row.get("tags") if not raw else raw)


def tags_of_source(tags: Sequence[Mapping[str, Any]], source: str) -> list[str]:
    """Tag values of one origin, in storage order and without duplicates."""
    return list(dict.fromkeys(str(tag["tag"]) for tag in tags if tag.get("source") == source))


def effective_annotations(tags: Any) -> list[dict[str, Any]]:
    """Annotations that actually apply to a lap (SPEC §10.3).

    One manual annotation makes the whole automatic set of that lap irrelevant:
    a human looked at the lap, and their verdict beats the detector's.  The rule
    is idempotent, so feeding an already effective list back in is harmless.
    """
    normalised = normalise_tags(tags)
    manual = [item for item in normalised if item.get("source") == TAG_SOURCE_MANUAL]
    return manual or normalised


def effective_tags(tags: Any) -> list[str]:
    """Tag values that actually apply to a lap (SPEC §10.3)."""
    return list(dict.fromkeys(str(item["tag"]) for item in effective_annotations(tags)))


def _sector_list(value: Any) -> list[int | None]:
    """Sectors of a lap, accepting a list or a JSON-encoded list."""
    if value is None or value == "":
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if not isinstance(value, (list, tuple)):
        return []
    sectors: list[int | None] = []
    for item in value:
        if item is None or item == "":
            sectors.append(None)
        else:
            try:
                sectors.append(int(item))
            except (TypeError, ValueError):
                sectors.append(None)
    return sectors


def normalise_lap(row: Mapping[str, Any]) -> dict[str, Any]:
    """Adapt one `Database.session_laps` row to the SPEC §8.4 lap shape.

    Annotations are exposed several times on purpose: `annotations` keeps every
    row with its `source`, `manual_tags` / `auto_tags` split them by origin, and
    `tags` / `effective_tags` are what the filters actually honour.  A client can
    therefore tell "the detector proposed this" from "a human decided this", and
    still see a proposal that a manual annotation overrode (SPEC §10.3).
    """
    lap = dict(row)
    lap["driver"] = row_driver(row)
    lap["sectors"] = _sector_list(row.get("sectors"))
    annotations = lap_annotations(row)
    lap["annotations"] = annotations
    lap["tags"] = effective_annotations(annotations)
    lap["manual_tags"] = tags_of_source(annotations, TAG_SOURCE_MANUAL)
    lap["auto_tags"] = tags_of_source(annotations, TAG_SOURCE_AUTO)
    lap["effective_tags"] = [str(item["tag"]) for item in lap["tags"]]
    lap["manually_annotated"] = bool(lap["manual_tags"])
    lap["is_best"] = bool(row.get("is_best"))
    time_ms = row.get("time_ms")
    lap["time_ms"] = None if time_ms is None else int(time_ms)
    return lap


def normalise_session_detail(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Adapt `Database.get_session` output to ``{session, club, entries}``."""
    nested = raw.get("session")
    if isinstance(nested, Mapping):
        session = dict(nested)
    else:
        session = {key: item for key, item in raw.items() if key not in {"club", "entries", "laps"}}
    club = raw.get("club")
    if club is not None and not isinstance(club, Mapping):
        club = {"name": str(club)}
    entries_raw = raw.get("entries") or []
    entries = [dict(entry) for entry in entries_raw if isinstance(entry, Mapping)]
    for entry in entries:
        entry["driver"] = row_driver(entry)
    return {
        "session": session,
        "club": dict(club) if isinstance(club, Mapping) else None,
        "entries": entries,
    }


def load_session(db: Database, session_id: int) -> dict[str, Any] | None:
    """Full session payload (``session``/``club``/``entries``/``laps``) or None."""
    raw = db.get_session(session_id)
    if not raw:
        return None
    detail = normalise_session_detail(raw)
    detail["laps"] = [normalise_lap(lap) for lap in db.session_laps(session_id)]
    return detail


def lap_points_by_driver(
    laps: Iterable[Mapping[str, Any]], *, manual_tags_only: bool = False
) -> dict[str, list[LapPoint]]:
    """Group storage lap rows into `karting.stats.LapPoint` lists per driver.

    With `manual_tags_only` each point carries only the human annotations of its
    lap, which is what the joker/pit detector must be fed: its own previous
    output is not evidence about a lap, and re-detecting must not depend on what
    the last detection concluded (SPEC §10.3).  Everything else -- the pace
    statistics above all -- wants the effective set.
    """
    grouped: dict[str, list[LapPoint]] = {}
    for lap in laps:
        name = row_driver(lap)
        number = lap.get("lap_number")
        if not name or number is None:
            continue
        time_ms = lap.get("time_ms")
        annotations = lap_annotations(lap)
        tags = (
            tags_of_source(annotations, TAG_SOURCE_MANUAL)
            if manual_tags_only
            else effective_tags(annotations)
        )
        grouped.setdefault(name, []).append(
            LapPoint(
                lap_number=int(number),
                time_ms=None if time_ms is None else int(time_ms),
                sectors=tuple(_sector_list(lap.get("sectors"))),
                tags=tuple(tags),
            )
        )
    for points in grouped.values():
        points.sort(key=lambda point: point.lap_number)
    return grouped


def empty_pace_stats() -> dict[str, Any]:
    """Neutral `PaceStats` payload for a driver without a single lap."""
    row: dict[str, Any] = {}
    for field in dataclass_fields(PaceStats):
        if field.name in {"n_laps", "n_used"}:
            row[field.name] = 0
        elif field.name in {"used_lap_numbers", "excluded"}:
            row[field.name] = []
        else:
            row[field.name] = None
    return row


def laps_by_driver(laps: Iterable[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
    """Storage lap rows grouped by driver nickname, ordered by lap number."""
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for lap in laps:
        name = row_driver(lap)
        if name and lap.get("lap_number") is not None:
            grouped.setdefault(name, []).append(lap)
    for rows in grouped.values():
        rows.sort(key=lambda row: int(row["lap_number"]))
    return grouped


def official_best_lap(
    laps: Sequence[Mapping[str, Any]], official_ms: int | None
) -> Mapping[str, Any] | None:
    """The lap the email's "Best lap" column points at.

    Preference order: the lap that both matches the official time and is
    highlighted in the email, then a plain time match, then the highlighted lap,
    then simply the fastest recorded lap.
    """
    timed = [lap for lap in laps if lap.get("time_ms") is not None]
    if not timed:
        return None
    target = None if official_ms is None else int(official_ms)
    exact = [lap for lap in timed if target is not None and int(lap["time_ms"]) == target]
    for candidates in (
        [lap for lap in exact if lap.get("is_best")],
        exact,
        [lap for lap in timed if lap.get("is_best")],
    ):
        if candidates:
            return min(candidates, key=lambda lap: int(lap["lap_number"]))
    return min(timed, key=lambda lap: (int(lap["time_ms"]), int(lap["lap_number"])))


def official_best_fields(
    laps: Sequence[Mapping[str, Any]], official_ms: Any, best_ms: Any
) -> dict[str, Any]:
    """Official vs clean best lap of one driver — the product's key metric.

    The email ranks drivers by a "Best lap" that is the joker lap for five of
    the six drivers of the reference race, so it lies about pace (SPEC §10.4).
    Every consumer therefore gets both numbers and the delta between them:
    `best_delta_ms = best_ms - official_best_ms` is positive exactly when the
    official time is unrepresentative (a joker lap, a lap dropped as an outlier
    or one the filter excluded for any other reason).
    """
    lap = official_best_lap(laps, None if official_ms is None else int(official_ms))
    if official_ms is not None:
        official = int(official_ms)
        source: str | None = "classification"
    elif lap is not None:
        official = int(lap["time_ms"])
        source = "laps"
    else:
        official = None
        source = None
    tags = effective_tags(lap_annotations(lap)) if lap is not None else []
    return {
        "official_best_ms": official,
        "official_best_source": source,
        "official_best_lap_number": None if lap is None else int(lap["lap_number"]),
        "official_best_lap_id": None if lap is None else _row_id(lap.get("id")),
        "official_best_tags": tags,
        "official_best_is_joker": LapTag.JOKER.value in tags,
        "best_delta_ms": (
            None if best_ms is None or official is None else int(round(float(best_ms))) - official
        ),
    }


def _row_id(value: Any) -> int | None:
    """A row id coming from storage, or ``None`` when it is not an integer."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def pace_rows(
    entries: Sequence[Mapping[str, Any]],
    laps: Sequence[Mapping[str, Any]],
    flt: LapFilter,
) -> list[dict[str, Any]]:
    """Per-driver pace metrics, ordered by classification position.

    Each row is ``{driver, position, kart, **PaceStats, pace_delta_to_best_ms,
    suspicious_fast_lap_numbers, **official_best_fields}`` where the delta is
    `karting.stats.pace_delta_to_best_driver` on the means (SPEC §6) and the
    official-best block contrasts the clean `best_ms` with the email's own best
    lap (SPEC §10.4).

    `PaceStats.excluded` only describes laps that were *dropped*, so the laps a
    filter keeps but flags as suspiciously fast would otherwise be invisible to
    a client; they are listed separately so the dashboard can mark exactly the
    laps a human is asked to review and tag (SPEC §6).
    """
    points = lap_points_by_driver(laps)
    rows_by_driver = laps_by_driver(laps)
    meta: dict[str, Mapping[str, Any]] = {}
    for entry in entries:
        name = row_driver(entry)
        if name:
            meta.setdefault(name, entry)
    names = list(meta) + [name for name in points if name not in meta]

    computed: dict[str, PaceStats] = {
        name: pace_stats(points[name], flt) for name in names if points.get(name)
    }
    deltas = pace_delta_to_best_driver(computed, metric="mean_ms")

    rows: list[dict[str, Any]] = []
    for name in names:
        stats = computed.get(name)
        entry = meta.get(name, {})
        row: dict[str, Any] = {
            "driver": name,
            "position": entry.get("position"),
            "kart": entry.get("kart"),
        }
        row.update(jsonable(stats) if stats is not None else empty_pace_stats())
        delta = deltas.get(name)
        row["pace_delta_to_best_ms"] = None if delta is None else round(float(delta), 3)
        row["suspicious_fast_lap_numbers"] = [
            int(flag.lap_number)
            for flag in classify_laps(points.get(name, []), flt)
            if flag.suspicious_fast
        ]
        row.update(
            official_best_fields(
                rows_by_driver.get(name, []), entry.get("best_lap_ms"), row.get("best_ms")
            )
        )
        rows.append(row)

    rows.sort(key=lambda item: (item.get("position") is None, item.get("position") or 0, item["driver"]))
    return rows


def resolve_name(requested: str, known: Iterable[str]) -> str | None:
    """Exact nickname match, falling back to a case-insensitive one."""
    names = list(known)
    if requested in names:
        return requested
    folded = requested.casefold()
    for name in names:
        if name.casefold() == folded:
            return name
    return None


def tag_dictionary() -> list[TagOption]:
    """The lap-tag dictionary served by `GET /api/tags`."""
    return [TagOption(value=tag.value, label=_TAG_LABELS.get(tag.value, tag.value.title())) for tag in LapTag]


# --------------------------------------------------------------------------- #
# Joker / pit events (SPEC §10)
# --------------------------------------------------------------------------- #


@dataclass(slots=True, frozen=True)
class EventThresholds:
    """Detector thresholds as they arrive from a query string or the CLI."""

    pit_ratio: float = DEFAULT_PIT_RATIO
    joker_ratio: float = DEFAULT_JOKER_RATIO
    one_per_driver: bool = True


def build_event_config(thresholds: EventThresholds) -> EventDetectionConfig:
    """Validate the thresholds and turn them into an `EventDetectionConfig`.

    A pit lap is slower and a joker lap is faster than the driver's own
    baseline, so the ratios are bounded by 1 from below and from above; anything
    else would describe a different phenomenon (SPEC §10.1).
    """
    if not thresholds.pit_ratio > 1.0:
        raise ValueError(
            f"pit_ratio must be greater than 1 (a pit lap is slower than the baseline), "
            f"got {thresholds.pit_ratio!r}"
        )
    if not 0.0 < thresholds.joker_ratio < 1.0:
        raise ValueError(
            f"joker_ratio must be between 0 and 1 (a joker lap is faster than the baseline), "
            f"got {thresholds.joker_ratio!r}"
        )
    return EventDetectionConfig(
        pit_ratio=float(thresholds.pit_ratio),
        joker_ratio=float(thresholds.joker_ratio),
        one_per_driver=bool(thresholds.one_per_driver),
    )


def detect_session_events(
    db: Database, session_id: int, config: EventDetectionConfig, *, persist: bool = False
) -> tuple[EventReport, list[dict[str, Any]]]:
    """Detect joker and pit laps of one session, optionally storing the tags.

    With `persist` the automatic annotations are rewritten through
    `Database.detect_and_tag_events` (manual ones are never touched, SPEC §10.3)
    and the laps are re-read afterwards, so the returned rows show the new tags.
    """
    report = db.detect_and_tag_events(session_id, config) if persist else None
    laps = [normalise_lap(lap) for lap in db.session_laps(session_id)]
    if report is None:
        report = detect_events(lap_points_by_driver(laps, manual_tags_only=True), config)
    return report, laps


def _event_attr(event: Any, name: str, default: Any = None) -> Any:
    """One field of a `DetectedEvent`, whether it is a dataclass or a dict."""
    if isinstance(event, Mapping):
        value = event.get(name, default)
    else:
        value = getattr(event, name, default)
    return default if value is None else value


def _optional_int(value: Any) -> int | None:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _string_list(values: Any) -> list[str]:
    if not values:
        return []
    return [str(item) for item in values]


def event_report_payload(
    session_id: int,
    report: EventReport,
    config: EventDetectionConfig,
    laps: Sequence[Mapping[str, Any]],
    *,
    persisted: bool = False,
) -> EventReportOut:
    """Render an `EventReport` for the API, joined with the stored lap tags.

    Detection alone does not tell a client what the session looks like now: the
    same lap may already carry a human verdict that overrides the detector, so
    each event also reports its lap id, whether the tag is in force and whether
    a manual annotation took it over (SPEC §10.3).
    """
    index = {
        (row_driver(lap), int(lap["lap_number"])): lap
        for lap in laps
        if lap.get("lap_number") is not None
    }

    def render(event: Any) -> tuple[str, DetectedEventOut]:
        """One `DetectedEvent`, joined with the annotations stored on its lap."""
        driver = str(_event_attr(event, "driver", ""))
        lap_number = _optional_int(_event_attr(event, "lap_number"))
        kind = str(_event_attr(event, "kind", ""))
        lap = index.get((driver, lap_number)) if lap_number is not None else None
        annotations = lap_annotations(lap) if lap is not None else []
        manual = tags_of_source(annotations, TAG_SOURCE_MANUAL)
        return kind, DetectedEventOut(
            driver=driver,
            lap_number=lap_number if lap_number is not None else 0,
            kind=kind,
            ratio=_optional_float(_event_attr(event, "ratio")),
            delta_ms=_optional_int(_event_attr(event, "delta_ms")),
            sector_index=_optional_int(_event_attr(event, "sector_index")),
            confidence=_optional_float(_event_attr(event, "confidence")),
            note=str(_event_attr(event, "note", "")),
            time_ms=None if lap is None else _optional_int(lap.get("time_ms")),
            lap_id=None if lap is None else _row_id(lap.get("id")),
            applied=kind in effective_tags(annotations),
            overridden_by_manual=bool(manual) and kind not in manual,
        )

    events: list[DetectedEventOut] = []
    counts: dict[str, int] = {"drivers": len({key[0] for key in index}), "joker": 0, "pit": 0}
    for event in _event_attr(report, "events", []) or []:
        kind, rendered = render(event)
        counts[kind] = counts.get(kind, 0) + 1
        events.append(rendered)
    # Proposals, not detections: they never count towards `counts` and they are
    # never tagged -- they exist so a human can confirm a missing pit in one
    # click instead of hunting for the lap (SPEC §10.2).
    candidates = [render(event)[1] for event in _event_attr(report, "pit_candidates", []) or []]
    without_joker = _string_list(_event_attr(report, "drivers_without_joker", []))
    without_pit = _string_list(_event_attr(report, "drivers_without_pit", []))
    with_multiple = _string_list(_event_attr(report, "drivers_with_multiple", []))
    return EventReportOut(
        session_id=session_id,
        config=EventConfigInfo.from_config(config),
        events=events,
        drivers_without_joker=without_joker,
        drivers_without_pit=without_pit,
        drivers_with_multiple=with_multiple,
        pit_counts={
            str(name): int(count)
            for name, count in (_event_attr(report, "pit_counts", {}) or {}).items()
        },
        expected_pits=_event_attr(report, "expected_pits", None),
        pit_candidates=candidates,
        warnings=_string_list(_event_attr(report, "warnings", [])),
        counts=counts,
        persisted=persisted,
        complete=not (without_joker or without_pit or with_multiple),
    )


# --------------------------------------------------------------------------- #
# Database wiring
# --------------------------------------------------------------------------- #


def database_path() -> str:
    """Database location: ``$PACE_DB`` or ``data/pace.db``."""
    return os.environ.get("PACE_DB", "").strip() or DEFAULT_DB_PATH


#: Values of ``$PACE_READ_ONLY`` that mean "serve reads only".
_TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})


def read_only() -> bool:
    """Whether the write endpoints are switched off (``$PACE_READ_ONLY``).

    Read-only is enforced by *not registering* the routes, so an import or a
    tag write answers 404 rather than 403: a public deployment should not even
    advertise a surface it will refuse.  Hiding the buttons in the browser is
    not a control -- the API is reachable with curl no matter what the page
    renders -- so this flag, not the frontend, is what makes a deployment safe
    to expose.
    """
    return os.environ.get("PACE_READ_ONLY", "").strip().casefold() in _TRUTHY


def open_database(path: str | None = None) -> Database:
    """Open the database, creating the parent directory when needed."""
    target = path or database_path()
    if target == ":memory:":
        return open_db(target)
    resolved = Path(target).expanduser()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return open_db(resolved)


def get_db() -> Iterator[Database]:
    """Request-scoped database connection."""
    try:
        db = open_database()
    except sqlite3.Error as exc:  # unreadable / corrupt file, bad path
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Cannot open database {database_path()!r}: {exc}",
        ) from exc
    try:
        yield db
    finally:
        db.close()


def _parse_exclude_tags(raw: str) -> frozenset[str]:
    tags: set[str] = set()
    for chunk in raw.split(","):
        tag = chunk.strip().casefold()
        if not tag:
            continue
        if not _TAG_RE.match(tag):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Invalid tag {chunk.strip()!r} in 'exclude_tags'; expected [a-z0-9_-]{{1,32}}.",
            )
        tags.add(tag)
    return frozenset(tags)


_FILTER_DEFAULTS: Final[LapFilter] = LapFilter()


def get_lap_filter(
    mad_k: Annotated[
        float,
        Query(gt=0.0, le=100.0, description="Robust outlier threshold: median + k * 1.4826 * MAD."),
    ] = _FILTER_DEFAULTS.mad_k,
    drop_first_lap: Annotated[
        bool, Query(description="Drop lap 1 (grid start / out lap).")
    ] = _FILTER_DEFAULTS.drop_first_lap,
    drop_slow_outliers: Annotated[
        bool, Query(description="Drop laps slower than the robust threshold.")
    ] = _FILTER_DEFAULTS.drop_slow_outliers,
    drop_fast_outliers: Annotated[
        bool, Query(description="Drop suspiciously fast laps instead of only flagging them.")
    ] = _FILTER_DEFAULTS.drop_fast_outliers,
    exclude_tags: Annotated[
        str | None,
        Query(description="Comma separated tags to exclude; empty string keeps every tagged lap."),
    ] = None,
    min_laps: Annotated[
        int, Query(ge=1, le=10_000, description="Minimum number of clean laps required for statistics.")
    ] = _FILTER_DEFAULTS.min_laps,
) -> LapFilter:
    """Build the `LapFilter` shared by `/stats` and `/compare` (SPEC §8.4)."""
    tags = _FILTER_DEFAULTS.exclude_tags if exclude_tags is None else _parse_exclude_tags(exclude_tags)
    return LapFilter(
        exclude_tags=tags,
        mad_k=mad_k,
        drop_first_lap=drop_first_lap,
        drop_slow_outliers=drop_slow_outliers,
        drop_fast_outliers=drop_fast_outliers,
        min_laps=min_laps,
    )


def get_event_thresholds(
    pit_ratio: Annotated[
        float,
        Query(
            gt=1.0,
            le=100.0,
            description="A lap at least this many times the baseline is a pit lap.",
        ),
    ] = DEFAULT_PIT_RATIO,
    joker_ratio: Annotated[
        float,
        Query(
            gt=0.0,
            lt=1.0,
            description="A lap at most this many times the baseline is a joker lap.",
        ),
    ] = DEFAULT_JOKER_RATIO,
    one_per_driver: Annotated[
        bool, Query(description="Keep only the most extreme candidate of each kind per driver.")
    ] = True,
) -> EventThresholds:
    """Detector thresholds of `/events` and `/events/detect` (SPEC §10.2).

    The bounds are part of the meaning of the numbers, so an inverted threshold
    is rejected as a request error (422) instead of quietly finding nothing.
    """
    return EventThresholds(
        pit_ratio=pit_ratio, joker_ratio=joker_ratio, one_per_driver=one_per_driver
    )


DbDep = Annotated[Database, Depends(get_db)]
FilterDep = Annotated[LapFilter, Depends(get_lap_filter)]
EventThresholdsDep = Annotated[EventThresholds, Depends(get_event_thresholds)]

#: Row ids are validated by FastAPI, so an out-of-range id is a 422 request
#: error instead of an `OverflowError` raised while binding it to SQLite.
SessionIdParam = Annotated[
    int, PathParam(ge=1, le=MAX_ROW_ID, description="Id of a stored session.")
]
LapIdParam = Annotated[int, PathParam(ge=1, le=MAX_ROW_ID, description="Id of a stored lap.")]

_ERROR_RESPONSES: Final[dict[int | str, dict[str, Any]]] = {
    404: {"model": ErrorResponse, "description": "Unknown session, driver or lap"},
}
def _label_official_best(row: dict[str, Any]) -> dict[str, Any]:
    """Mark a `best_lap_ms` that comes from the classification, not from pace.

    The official best lap of the email is the joker lap for five drivers out of
    six (SPEC §10.4).  `/stats` and `/rankings` already say so; this keeps the
    driver endpoints from being the one place that serves the same number bare.
    """
    if "best_lap_ms" not in row:
        return row
    row["official_best_based"] = True
    row["joker_inflated"] = True
    row["best_lap_label"] = JOKER_INFLATED_LABEL
    row["best_lap_note"] = JOKER_INFLATED_NOTE
    return row


def _session_or_404(db: Database, session_id: int) -> dict[str, Any]:
    detail = load_session(db, session_id)
    if detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Session {session_id} not found."
        )
    return detail


def _lap_exists(db: Database, lap_id: int) -> bool | None:
    """Whether `lap_id` exists; ``None`` when the storage cannot answer cheaply."""
    try:
        row = db.connection.execute("SELECT 1 FROM lap WHERE id = ?", (lap_id,)).fetchone()
    except sqlite3.Error:
        return None
    return row is not None


def _lap_or_404(db: Database, lap_id: int) -> None:
    """Fail with 404 when the lap is known not to exist."""
    if _lap_exists(db, lap_id) is False:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Lap {lap_id} not found.")


def _event_report_or_error(
    db: Database, session_id: int, thresholds: EventThresholds, *, persist: bool
) -> EventReportOut:
    """Run the joker/pit detector for a route, mapping failures to HTTP codes."""
    try:
        config = build_event_config(thresholds)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    try:
        report, laps = detect_session_events(db, session_id, config, persist=persist)
    except LookupError as exc:  # the storage does not know that session or lap
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except (StorageError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Cannot detect joker and pit laps of session {session_id}: {exc}",
        ) from exc
    return event_report_payload(session_id, report, config, laps, persisted=persist)


# --------------------------------------------------------------------------- #
# Application factory
# --------------------------------------------------------------------------- #


def cors_origins() -> list[str]:
    """Allowed browser origins: the Vite dev server unless ``$PACE_CORS_ORIGINS``."""
    configured = os.environ.get("PACE_CORS_ORIGINS", "").strip()
    if not configured:
        return list(DEFAULT_CORS_ORIGINS)
    return [origin.strip() for origin in configured.split(",") if origin.strip()]


def create_app() -> FastAPI:
    """Build the FastAPI application (SPEC §8.4)."""
    application = FastAPI(
        title="Pace Analysis API",
        version="1.0.0",
        summary="Karting race results: import, classification, lap times and pace statistics.",
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins(),
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )

    def _server_error(request: Request, exc: Exception, kind: str) -> JSONResponse:
        """Log the failure in full, answer with a correlation id and nothing else.

        Exception types, SQL fragments and thread ids are internals: they help an
        attacker map the server and help the user not at all.  The `error_id` in
        the response is the key to the matching server-side traceback.
        """
        error_id = uuid.uuid4().hex[:12]
        LOGGER.exception(
            "%s while handling %s %s [error_id=%s]",
            kind,
            request.method,
            request.url.path,
            error_id,
            exc_info=exc,
        )
        return JSONResponse(
            status_code=500,
            content={
                "detail": "Internal server error. Quote the error id when reporting it.",
                "error_id": error_id,
            },
        )

    async def sqlite_error_handler(request: Request, exc: Exception) -> JSONResponse:
        return _server_error(request, exc, "Database error")

    async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
        return _server_error(request, exc, "Unhandled error")

    application.add_exception_handler(sqlite3.Error, sqlite_error_handler)
    application.add_exception_handler(Exception, unexpected_error_handler)

    _register_routes(application)
    return application


def _register_routes(application: FastAPI) -> None:
    """Attach every `/api` route to `application`."""

    @application.get("/api/health", response_model=HealthResponse, tags=["meta"])
    def health(db: DbDep) -> HealthResponse:
        """Liveness probe, session count, and whether writes are switched off."""
        return HealthResponse(
            status="ok", sessions=len(db.list_sessions()), read_only=read_only()
        )

    @application.get("/api/tags", response_model=list[TagOption], tags=["tags"])
    def tags() -> list[TagOption]:
        """Dictionary of lap tags available for manual annotation."""
        return tag_dictionary()

    @application.get("/api/sessions", tags=["sessions"])
    def list_sessions(db: DbDep) -> list[dict[str, Any]]:
        """All stored sessions, newest first if the storage orders them so."""
        return [jsonable(row) for row in db.list_sessions()]

    @application.get("/api/sessions/{session_id}", tags=["sessions"], responses=_ERROR_RESPONSES)
    def get_session(session_id: SessionIdParam, db: DbDep) -> dict[str, Any]:
        """Session details: classification entries and every lap with its tags."""
        return jsonable(_session_or_404(db, session_id))

    @application.get("/api/sessions/{session_id}/stats", tags=["stats"], responses=_ERROR_RESPONSES)
    def session_stats(session_id: SessionIdParam, db: DbDep, flt: FilterDep) -> dict[str, Any]:
        """Pace metrics per driver for one session, using the query filter."""
        detail = _session_or_404(db, session_id)
        try:
            rows = pace_rows(detail["entries"], detail["laps"], flt)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Cannot compute pace statistics with these filter parameters: {exc}",
            ) from exc
        joker_inflated = [row["driver"] for row in rows if row.get("official_best_is_joker")]
        return {
            "filter": FilterInfo.from_filter(flt).model_dump(),
            "drivers": jsonable(rows),
            "official_best": {
                "joker_inflated_drivers": joker_inflated,
                "joker_inflated_count": len(joker_inflated),
                "drivers_count": len(rows),
                "label": JOKER_INFLATED_LABEL,
                "note": JOKER_INFLATED_NOTE,
            },
        }

    @application.get(
        "/api/sessions/{session_id}/events",
        response_model=EventReportOut,
        tags=["events"],
        responses=_ERROR_RESPONSES,
    )
    def session_events(
        session_id: SessionIdParam, db: DbDep, thresholds: EventThresholdsDep
    ) -> EventReportOut:
        """Joker and pit laps of one session, detected on the fly (SPEC §10.2).

        Read-only: the stored annotations are left alone, and every event says
        whether its tag is currently in force.
        """
        _session_or_404(db, session_id)
        return _event_report_or_error(db, session_id, thresholds, persist=False)

    # Everything below writes. In read-only mode the routes are never attached,
    # so the paths simply do not exist (404) instead of existing-but-refusing.
    if read_only():
        return

    @application.post(
        "/api/sessions/{session_id}/events/detect",
        response_model=EventReportOut,
        status_code=status.HTTP_200_OK,
        tags=["events"],
        responses=_ERROR_RESPONSES,
    )
    def detect_session_events_route(
        session_id: SessionIdParam, db: DbDep, thresholds: EventThresholdsDep
    ) -> EventReportOut:
        """Recompute the automatic joker/pit tags of a session (SPEC §10.3).

        Rows with ``source='auto'`` are rewritten from scratch; manual
        annotations are never touched and keep overriding the detector.
        """
        _session_or_404(db, session_id)
        return _event_report_or_error(db, session_id, thresholds, persist=True)

    @application.get("/api/sessions/{session_id}/compare", tags=["stats"], responses=_ERROR_RESPONSES)
    def compare_session_drivers(
        session_id: SessionIdParam,
        db: DbDep,
        flt: FilterDep,
        a: Annotated[str, Query(min_length=1, description="First driver nickname.")],
        b: Annotated[str, Query(min_length=1, description="Second driver nickname.")],
    ) -> dict[str, Any]:
        """Statistical comparison of two drivers of the same session."""
        detail = _session_or_404(db, session_id)
        points = lap_points_by_driver(detail["laps"])
        name_a = resolve_name(a, points)
        if name_a is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Driver {a!r} has no laps in session {session_id}.",
            )
        name_b = resolve_name(b, points)
        if name_b is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Driver {b!r} has no laps in session {session_id}.",
            )
        if name_a == name_b:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Parameters 'a' and 'b' must reference two different drivers.",
            )
        try:
            comparison = compare_drivers(
                points[name_a], points[name_b], name_a=name_a, name_b=name_b, flt=flt
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Cannot compare these drivers with the given filter: {exc}",
            ) from exc
        return jsonable(comparison)

    @application.get(
        "/api/sessions/{session_id}/rankings",
        response_model=RankingsOut,
        tags=["sessions"],
        responses=_ERROR_RESPONSES,
    )
    def session_rankings(session_id: SessionIdParam, db: DbDep) -> RankingsOut:
        """Weekly bests and track records shipped with the session's email.

        Both tables rank drivers by their official best lap, which in this race
        format is usually the joker lap, so the payload carries the flags a UI
        needs to label them "official, joker-inflated" (SPEC §10.4).
        """
        _session_or_404(db, session_id)
        raw = db.rankings(session_id) or {}
        return RankingsOut(
            weekly_best=jsonable(raw.get("weekly_best") or []),
            track_record=jsonable(raw.get("track_record") or []),
            official_best_based=True,
            joker_inflated=True,
        )

    @application.get("/api/drivers", tags=["drivers"])
    def list_drivers(db: DbDep) -> list[dict[str, Any]]:
        """Every known driver with a few aggregates.

        `best_lap_ms` is the *official* best lap, which for five drivers of the
        reference race is the joker lap (SPEC §10.4) -- so it is labelled here
        exactly as the rankings are, and no consumer can quote it as pace.
        """
        return [_label_official_best(jsonable(row)) for row in db.list_drivers()]

    @application.get("/api/drivers/{nickname}/history", tags=["drivers"], responses=_ERROR_RESPONSES)
    def driver_history(nickname: str, db: DbDep) -> list[dict[str, Any]]:
        """Past sessions of one driver."""
        known = [row_driver(row) for row in db.list_drivers()]
        resolved = resolve_name(nickname, [name for name in known if name])
        if resolved is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=f"Driver {nickname!r} not found."
            )
        return [_label_official_best(jsonable(row)) for row in db.driver_history(resolved)]

    @application.post(
        "/api/imports",
        response_model=list[ImportReportOut],
        tags=["imports"],
        responses={400: {"model": ErrorResponse, "description": "Every uploaded file is unusable"}},
    )
    def create_imports(
        db: DbDep,
        files: Annotated[list[UploadFile], File(description="One or more Apex Timing .eml files.")],
    ) -> list[ImportReportOut]:
        """Import one or more `.eml` files; each file gets its own report.

        One contract for one file and for many: `200` with a per-file report
        whenever at least one file could be used, `400` only when the whole
        request produced nothing (SPEC §8.4 "400 on an unparsable .eml").
        """
        if not files:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="At least one file is required in the 'files' field.",
            )
        reports = [_import_upload(db, upload) for upload in files]
        if all(report.status == "failed" for report in reports):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    reports[0].detail
                    if len(reports) == 1
                    else "None of the uploaded files could be imported: "
                    + "; ".join(f"{item.filename}: {item.detail}" for item in reports)
                ),
            )
        return reports

    @application.post(
        "/api/laps/{lap_id}/tags",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=["tags"],
        responses=_ERROR_RESPONSES,
    )
    def add_lap_tag(lap_id: LapIdParam, body: LapTagCreate, db: DbDep) -> None:
        """Attach a manual tag to a lap."""
        tag = body.tag.strip().casefold()
        if not _TAG_RE.match(tag):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Invalid tag {body.tag!r}; expected [a-z0-9_-]{{1,32}}.",
            )
        _lap_or_404(db, lap_id)
        try:
            db.add_lap_tag(lap_id, tag, body.note)
        except (LookupError, sqlite3.IntegrityError) as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=f"Lap {lap_id} not found: {exc}"
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=f"Cannot tag lap {lap_id}: {exc}"
            ) from exc

    @application.delete(
        "/api/laps/{lap_id}/tags/{tag}",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=["tags"],
        responses=_ERROR_RESPONSES,
    )
    def delete_lap_tag(lap_id: LapIdParam, tag: str, db: DbDep) -> None:
        """Remove a manual tag from a lap; removing a missing tag is a no-op."""
        _lap_or_404(db, lap_id)
        try:
            db.remove_lap_tag(lap_id, tag.strip().casefold())
        except (LookupError, sqlite3.IntegrityError) as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=f"Lap {lap_id} not found: {exc}"
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=f"Cannot untag lap {lap_id}: {exc}"
            ) from exc


def _import_upload(db: Database, upload: UploadFile) -> ImportReportOut:
    """Parse and store one uploaded file, never raising on bad input."""
    filename = upload.filename or "upload.eml"
    try:
        raw = upload.file.read()
    except OSError as exc:
        return ImportReportOut.failure(filename, f"Cannot read the uploaded file: {exc}")
    finally:
        upload.file.close()

    if not raw:
        return ImportReportOut.failure(filename, "The uploaded file is empty.")
    if len(raw) > MAX_UPLOAD_BYTES:
        return ImportReportOut.failure(
            filename, f"File is larger than the {MAX_UPLOAD_BYTES // (1024 * 1024)} MiB limit."
        )

    try:
        parsed = parse_email_bytes(raw, source_path=filename)
    except Exception as exc:  # the parser must never break the whole batch
        return ImportReportOut.failure(
            filename, f"Not a readable Apex Timing result email ({type(exc).__name__}: {exc})."
        )

    warnings = [str(item) for item in parsed.warnings]
    try:
        report = db.import_parsed(parsed, raw_bytes=raw)
    except StorageError as exc:  # refused by the storage rules: actionable
        return ImportReportOut.failure(
            filename, f"Parsed, but could not be stored: {exc}.", warnings=warnings
        )
    except Exception as exc:  # disk error, corrupt database, ...
        error_id = uuid.uuid4().hex[:12]
        LOGGER.exception("Import of %r failed [error_id=%s]", filename, error_id, exc_info=exc)
        return ImportReportOut.failure(
            filename,
            f"Parsed, but could not be stored because of an internal error "
            f"(error id {error_id}).",
            warnings=warnings,
        )
    return ImportReportOut.from_report(filename, report, session_name=parsed.session.name)


app = create_app()
