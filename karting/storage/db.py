"""SQLite storage layer: idempotent import and merging of parsed emails.

Plain :mod:`sqlite3`, no ORM.  The schema lives in ``schema.sql`` next to this
module and is applied idempotently every time a connection is opened.

Design notes
------------
* A session is identified by ``(club_id, name, code, started_at)`` -- never by
  the message id, because the very same race is mailed separately to every
  recipient.  Re-importing an email that was already seen is a no-op; importing
  a *different* email about the same race only fills in what is missing.
  An email whose session has neither a name nor a start time carries no
  identity at all and is refused rather than stored as a nameless session that
  every other header-less email would merge into.
* A driver is identified by the Apex ``external_id`` when the email carries one
  (nicknames change), and by the nickname otherwise (SPEC 1.8).
* The merge is strictly additive: an existing non-NULL value is never
  overwritten.  A disagreement is reported in :attr:`ImportReport.conflicts`
  and logged in the ``import_conflict`` table.
* ``lap_annotation`` holds two kinds of rows (SPEC 10.3): what a human said
  (``source='manual'``) and what the joker/pit detector proposed
  (``source='auto'``).  The importer only ever writes automatic rows, and only
  through :meth:`Database.detect_and_tag_events`; manual rows are never touched
  by any automatic path.  The *effective* tags of a lap -- the ones the API and
  the statistics see -- are derived: a lap that carries at least one manual row
  ignores its automatic rows entirely.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from karting.models import (
    Club,
    Driver,
    Lap,
    LapTag,
    ParsedEmail,
    RankingKind,
    Session,
)

if TYPE_CHECKING:  # pragma: no cover - imported lazily at run time
    from karting.stats.events import EventDetectionConfig, EventReport

__all__ = [
    "AUTO_SOURCE",
    "DEFAULT_DB_PATH",
    "KNOWN_LAP_TAGS",
    "MANUAL_SOURCE",
    "OVERRIDE_TAG",
    "SCHEMA_PATH",
    "SCHEMA_VERSION",
    "ANNOTATION_SOURCES",
    "Database",
    "DetectorUnavailableError",
    "ImportReport",
    "NoSessionIdentityError",
    "StorageError",
    "UnknownLapError",
    "UnknownTagError",
    "content_digest",
    "open_db",
]

SCHEMA_PATH: Path = Path(__file__).with_name("schema.sql")
#: Bumped when `schema.sql` changes; an older file replays the script.
#: v2 added `email_import.content_sha256`, v3 the `lap_annotation.source` key.
SCHEMA_VERSION: int = 3
DEFAULT_DB_PATH: str = "data/pace.db"

#: Vocabulary accepted by :meth:`Database.add_lap_tag` (see ``models.LapTag``).
KNOWN_LAP_TAGS: frozenset[str] = frozenset(tag.value for tag in LapTag)

#: A human decision; always wins over the detector (SPEC 10.3).
MANUAL_SOURCE: str = "manual"
#: Produced by :meth:`Database.detect_and_tag_events`; disposable by design.
AUTO_SOURCE: str = "auto"
ANNOTATION_SOURCES: frozenset[str] = frozenset({MANUAL_SOURCE, AUTO_SOURCE})

#: Manual tag used to reject the detector's proposal for a lap: it carries no
#: exclusion of its own, and its mere presence hides every automatic row of
#: that lap, so a later detector run cannot resurrect the rejected tag.
OVERRIDE_TAG: str = LapTag.CLEAN.value

#: Automatic tags the detector may emit (``DetectedEvent.kind``).
_EVENT_TAGS: frozenset[str] = frozenset({LapTag.JOKER.value, LapTag.PIT.value})


class StorageError(RuntimeError):
    """Base class for storage-level failures."""


class UnknownTagError(StorageError, ValueError):
    """Raised when a lap tag is not part of :data:`KNOWN_LAP_TAGS`."""


class UnknownLapError(StorageError, LookupError):
    """Raised when an annotation targets a lap id that does not exist."""


class NoSessionIdentityError(StorageError, ValueError):
    """Raised when a parsed email has neither a session name nor a start time."""


class DetectorUnavailableError(StorageError, ImportError):
    """Raised when ``karting.stats.events`` cannot be imported.

    Automatic joker/pit tagging is part of the import (SPEC 10.3), so a build
    without the detector is broken rather than merely degraded.
    """


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class ImportReport:
    """Outcome of a single :meth:`Database.import_parsed` call."""

    session_id: int
    club_id: int
    session_created: bool
    already_imported: bool
    inserted_laps: int = 0
    updated_laps: int = 0
    inserted_entries: int = 0
    conflicts: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: Outcome of the automatic joker/pit tagging run at the end of the import
    #: (SPEC 10.3).  A race is expected to have exactly one joker and one pit
    #: stop per driver; the drivers that are missing one are listed verbatim so
    #: the UI can invite a human to annotate them by hand.
    auto_jokers: int = 0
    auto_pits: int = 0
    drivers_without_joker: list[str] = field(default_factory=list)
    drivers_without_pit: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Plain-JSON representation (used by the API and the CLI)."""
        return {
            "session_id": self.session_id,
            "club_id": self.club_id,
            "session_created": self.session_created,
            "already_imported": self.already_imported,
            "inserted_laps": self.inserted_laps,
            "updated_laps": self.updated_laps,
            "inserted_entries": self.inserted_entries,
            "conflicts": list(self.conflicts),
            "warnings": list(self.warnings),
            "auto_jokers": self.auto_jokers,
            "auto_pits": self.auto_pits,
            "drivers_without_joker": list(self.drivers_without_joker),
            "drivers_without_pit": list(self.drivers_without_pit),
        }


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _utcnow() -> str:
    """Current UTC time as ISO-8601 with a trailing 'Z'."""
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="microseconds") + "Z"


def _dt_text(value: datetime | None) -> str | None:
    """Serialise a datetime; the venue-local value is kept naive and verbatim."""
    if value is None:
        return None
    return value.isoformat()


def _date_text(value: date | None) -> str | None:
    """Serialise a date as 'YYYY-MM-DD'."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    return value.isoformat()


def _text(value: Any) -> str | None:
    """Render a value for the conflict log."""
    return None if value is None else str(value)


#: Range of a SQLite ``INTEGER``; anything outside cannot even be bound.
_SQLITE_INT_MIN: int = -(2**63)
_SQLITE_INT_MAX: int = 2**63 - 1


def _row_id(value: Any) -> int | None:
    """A row id SQLite can bind, or ``None`` when it cannot exist.

    An id outside the 64-bit range matches no row, so callers treat ``None``
    exactly like "not found" instead of letting sqlite3 raise `OverflowError`.
    """
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if _SQLITE_INT_MIN <= number <= _SQLITE_INT_MAX else None


def _require_session_identity(session: Session) -> None:
    """Refuse a session that cannot be told apart from any other nameless one.

    The session key is ``(club, name, code, started_at)``; with an empty name
    and no start time every header-less email would collapse into a single
    phantom session and mix unrelated races together.
    """
    if not (session.name or "").strip() and session.started_at is None:
        raise NoSessionIdentityError(
            "the email carries no session identity (neither a session name nor a start "
            "time was parsed), so it cannot be stored without merging into unrelated races"
        )


def content_digest(parsed: ParsedEmail) -> str:
    """Stable sha256 of the *data* of a parsed email, provenance excluded.

    Input that arrives without headers (``parse_html``) has neither a message id
    nor a file digest, so this is the only key that can make it
    self-identifying and keep ``email_import`` from growing on every retry.
    Two emails of the same race sent to different recipients differ in their
    sector, history and ranking rows, so they do not collide.
    """
    payload = parsed.to_dict()
    payload.pop("provenance", None)
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _effective_tags(annotations: Mapping[int, Sequence[dict]]) -> dict[int, list[dict]]:
    """Apply the priority rule of SPEC 10.3 to raw annotation rows.

    A lap that carries at least one manual row keeps only its manual rows: the
    human overruled the detector for that lap as a whole, not tag by tag.  A lap
    without manual rows reports what the detector proposed.
    """
    effective: dict[int, list[dict]] = {}
    for lap_id, rows in annotations.items():
        manual = [dict(row) for row in rows if row.get("source") == MANUAL_SOURCE]
        kept = manual if manual else [dict(row) for row in rows]
        if kept:
            effective[int(lap_id)] = sorted(kept, key=lambda row: str(row.get("tag", "")))
    return effective


def _auto_note(event: Any) -> str:
    """Deterministic note of one detected event (SPEC 10.2 ``DetectedEvent``).

    The detector already words its verdict for a human; only its numbers are
    added when it does not.  Deterministic wording matters: an unchanged
    detection must produce an unchanged row, otherwise re-running the detector
    would rewrite the whole table.
    """
    parts = [f"auto-detected {str(event.kind).strip().casefold()}"]
    explanation = str(event.note).strip()
    if explanation:
        parts.append(explanation)
    else:
        parts.append(f"ratio {float(event.ratio):.3f}")
        parts.append(f"delta {int(event.delta_ms):+d} ms")
        if event.sector_index is not None:
            # `DetectedEvent.sector_index` is 0-based; humans read "S1".
            parts.append(f"sector S{int(event.sector_index) + 1}")
    parts.append(f"confidence {float(event.confidence):.2f}")
    return "; ".join(parts)


def _load_detector() -> tuple[Any, Any, Any]:
    """Import the joker/pit detector lazily (SPEC 10.2).

    Lazy because ``karting.stats`` pulls in numpy/scipy, which a storage-only
    caller has no reason to pay for until an import or a re-detection happens.
    """
    try:
        from karting.stats.events import EventDetectionConfig, detect_events
        from karting.stats.outliers import LapPoint
    except ImportError as exc:  # pragma: no cover - a broken installation
        raise DetectorUnavailableError(
            "the joker/pit detector (karting.stats.events) is unavailable, so laps "
            f"cannot be annotated automatically: {exc}"
        ) from exc
    return detect_events, EventDetectionConfig, LapPoint


@dataclass(slots=True)
class _Conflict:
    """One refused overwrite."""

    entity: str
    ref: str
    field_name: str
    stored: Any
    incoming: Any
    note: str | None = None  # replaces the default wording when set

    def message(self) -> str:
        if self.note is not None:
            return f"{self.entity} [{self.ref}]: {self.note}"
        return (
            f"{self.entity} [{self.ref}]: {self.field_name} kept stored="
            f"{_text(self.stored)}, incoming={_text(self.incoming)} ignored"
        )


class _ImportContext:
    """Mutable bookkeeping shared by the merge steps of one import."""

    __slots__ = ("conflicts", "driver_ids", "inserted_entries", "inserted_laps", "updated_laps")

    def __init__(self) -> None:
        self.conflicts: list[_Conflict] = []
        self.driver_ids: dict[str, int] = {}
        self.inserted_laps: int = 0
        self.updated_laps: int = 0
        self.inserted_entries: int = 0

    def conflict(
        self,
        entity: str,
        ref: str,
        field_name: str,
        stored: Any,
        incoming: Any,
        note: str | None = None,
    ) -> None:
        self.conflicts.append(_Conflict(entity, ref, field_name, stored, incoming, note))

    def duplicate(self, entity: str, ref: str) -> None:
        """A natural key repeated inside a single email: merge, never insert twice."""
        self.conflict(
            entity,
            ref,
            "duplicate_key",
            "first occurrence in this email",
            "repeated row",
            note="the same row appears twice in this email; the repetition was merged "
            "into the first occurrence instead of being inserted again",
        )


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #


class Database:
    """Connection wrapper owning the schema, the importer and the readers."""

    def __init__(
        self,
        path: str | Path = DEFAULT_DB_PATH,
        *,
        raw_dir: str | Path | None = None,
    ) -> None:
        self.path: str = str(path)
        self._memory: bool = self.path == ":memory:" or self.path.startswith("file::memory:")
        default_raw: Path | None
        if self._memory:
            # An in-memory database is a scratch database (SPEC 8.2 prescribes it
            # for tests): it must not litter the process' working directory with
            # copies of raw emails.  Pass `raw_dir=` explicitly to keep them.
            default_raw = None
        else:
            db_file = Path(self.path)
            db_file.parent.mkdir(parents=True, exist_ok=True)
            default_raw = db_file.parent / "raw_emails"
        self.raw_dir: Path | None = Path(raw_dir) if raw_dir is not None else default_raw

        # isolation_level=None -> autocommit; transactions are explicit below.
        # check_same_thread=False: the connection is owned by one request /
        # one caller at a time, but ASGI dependency injection may create it on
        # one threadpool thread and use it on another.  Writes are serialised by
        # `_lock` so a shared connection stays safe as well.
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.path, isolation_level=None, check_same_thread=False, timeout=30.0
        )
        self._conn.row_factory = sqlite3.Row
        self._execute("PRAGMA foreign_keys = ON")
        if not self._memory:
            # Switching the journal mode needs an exclusive lock, which a
            # concurrent writer would refuse: only ask for it when it is not
            # already WAL (the second and every later connection to a file).
            mode = self._one("PRAGMA journal_mode")
            if mode is None or str(mode[0]).lower() != "wal":
                try:
                    self._execute("PRAGMA journal_mode = WAL")
                except sqlite3.OperationalError:
                    # The exclusive lock this needs is not subject to the busy
                    # timeout, so several connections opening one fresh file at
                    # the same instant race for it.  Losing that race is not an
                    # error: the winner sets the mode for the *file*, and if
                    # nobody did the rollback journal serves just as well.
                    # Failing here would turn a concurrent import into a 503.
                    pass
        self._apply_schema()

    # -- lifecycle --------------------------------------------------------- #

    @property
    def connection(self) -> sqlite3.Connection:
        """The underlying connection (read-only use outside this module)."""
        return self._conn

    def close(self) -> None:
        """Close the connection; safe to call more than once."""
        try:
            self._conn.close()
        except sqlite3.ProgrammingError:  # pragma: no cover - already closed
            pass

    def __enter__(self) -> Database:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def _apply_schema(self) -> None:
        """Replay ``schema.sql`` unless this file is already at the right version.

        Every statement is ``IF NOT EXISTS``, so replaying is harmless -- but it
        is also a *write*, and taking a write lock on every connection would make
        opening the database fail while another connection is importing.
        """
        version = self._one("PRAGMA user_version")
        if version is not None and int(version[0]) == SCHEMA_VERSION:
            return
        with self._lock:
            # Migrations run first: `schema.sql` describes the current shape of
            # every table and its indexes, and an index over a column an older
            # file does not have yet cannot be created before the file has it.
            self._migrate()
            # executescript() implicitly commits any pending transaction, so it
            # must run outside of our explicit BEGIN/COMMIT blocks.
            self._conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        self._execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _migrate(self) -> None:
        """Upgrade an older file in place; `CREATE TABLE IF NOT EXISTS` cannot.

        Every step is idempotent and inspects the file rather than trusting
        ``PRAGMA user_version``, so a database written by any earlier version --
        or by a build that crashed halfway through an upgrade -- lands on the
        current schema without losing a row.
        """
        self._migrate_v2_content_digest()
        self._migrate_v3_annotation_source()

    def _migrate_v2_content_digest(self) -> None:
        """v2: `email_import.content_sha256`, the digest of the parsed payload."""
        columns = self._columns("email_import")
        if not columns:  # a fresh file: schema.sql creates the column itself
            return
        if "content_sha256" not in columns:
            self._execute("ALTER TABLE email_import ADD COLUMN content_sha256 TEXT")
            self._execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_email_import_content "
                "ON email_import (content_sha256)"
            )

    def _migrate_v3_annotation_source(self) -> None:
        """v3: `lap_annotation.source` and the UNIQUE(lap_id, tag, source) key.

        SQLite cannot alter a UNIQUE constraint, so the table is rebuilt.  Rows
        written before this migration are human annotations by definition (the
        detector did not exist yet), hence ``source='manual'``: the upgrade can
        only ever *strengthen* what a lap says, never let an old row be
        overwritten by the detector.
        """
        columns = self._columns("lap_annotation")
        if not columns:  # a fresh file: schema.sql already created the table
            return
        if "source" in columns and ("lap_id", "source", "tag") in self._unique_keys(
            "lap_annotation"
        ):
            return
        stored_source = (
            "CASE WHEN source = 'auto' THEN 'auto' ELSE 'manual' END"
            if "source" in columns
            else "'manual'"
        )
        with self._transaction():
            self._execute(
                """
                CREATE TABLE lap_annotation_v3 (
                    id         INTEGER PRIMARY KEY,
                    lap_id     INTEGER NOT NULL REFERENCES lap (id) ON DELETE CASCADE,
                    tag        TEXT NOT NULL,
                    note       TEXT,
                    created_at TEXT NOT NULL,
                    source     TEXT NOT NULL DEFAULT 'manual'
                               CHECK (source IN ('manual', 'auto')),
                    UNIQUE (lap_id, tag, source)
                )
                """
            )
            self._execute(
                f"""
                INSERT INTO lap_annotation_v3 (id, lap_id, tag, note, created_at, source)
                SELECT id, lap_id, tag, note, created_at, {stored_source} FROM lap_annotation
                """
            )
            self._execute("DROP TABLE lap_annotation")
            self._execute("ALTER TABLE lap_annotation_v3 RENAME TO lap_annotation")
            # The indexes of the dropped table went with it.
            self._execute(
                "CREATE INDEX IF NOT EXISTS ix_lap_annotation_lap ON lap_annotation (lap_id)"
            )
            self._execute(
                "CREATE INDEX IF NOT EXISTS ix_lap_annotation_source "
                "ON lap_annotation (source, lap_id)"
            )

    def _columns(self, table: str) -> set[str]:
        """Column names of a table; empty when the table does not exist."""
        return {str(row["name"]) for row in self._execute(f"PRAGMA table_info({table})")}

    def _unique_keys(self, table: str) -> set[tuple[str, ...]]:
        """Sorted column tuples of every UNIQUE index of a table."""
        keys: set[tuple[str, ...]] = set()
        for index in self._all(f"PRAGMA index_list({table})"):
            if not int(index["unique"]):
                continue
            columns = [
                str(row["name"])
                for row in self._all(f"PRAGMA index_info({index['name']})")
                if row["name"] is not None
            ]
            keys.add(tuple(sorted(columns)))
        return keys

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """Explicit single transaction; rolls back on any exception.

        Held under `_lock` so that two threads sharing one `Database` cannot
        interleave their statements inside a single ``BEGIN IMMEDIATE`` block.
        """
        with self._lock:
            self._execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._execute("ROLLBACK")
                raise
            self._execute("COMMIT")

    # -- generic row helpers ----------------------------------------------- #

    def _execute(
        self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()
    ) -> sqlite3.Cursor:
        """Run one statement under `_lock`, so a shared connection stays safe."""
        with self._lock:
            return self._conn.execute(sql, params)

    def _executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.executemany(sql, rows)

    def _one(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def _all(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _fill_missing(
        self,
        table: str,
        row_id: int,
        stored: sqlite3.Row,
        incoming: dict[str, Any],
        *,
        entity: str,
        ref: str,
        ctx: _ImportContext,
    ) -> list[str]:
        """Fill NULL columns from ``incoming``; report (never apply) mismatches.

        Returns the list of columns that were actually written.
        """
        updates: dict[str, Any] = {}
        for column, value in incoming.items():
            if value is None:
                continue
            current = stored[column]
            if current is None:
                updates[column] = value
            elif current != value:
                ctx.conflict(entity, ref, column, current, value)
        if updates:
            assignments = ", ".join(f"{column} = ?" for column in updates)
            self._execute(
                f"UPDATE {table} SET {assignments} WHERE id = ?",
                (*updates.values(), row_id),
            )
        return list(updates)

    # ------------------------------------------------------------------ #
    # Import
    # ------------------------------------------------------------------ #

    def import_parsed(self, parsed: ParsedEmail, *, raw_bytes: bytes | None = None) -> ImportReport:
        """Import one parsed email, merging it into whatever is already stored.

        The whole import -- dedup lookup included -- is a single
        ``BEGIN IMMEDIATE`` transaction: on failure nothing is written, and two
        writers (two threads, two processes, two connections) cannot both
        conclude that the email is new and race on the provenance keys.
        Importing the same email twice is a no-op reporting
        ``already_imported=True``.
        """
        with self._lock:
            return self._import_parsed_locked(parsed, raw_bytes)

    def _import_parsed_locked(
        self, parsed: ParsedEmail, raw_bytes: bytes | None
    ) -> ImportReport:
        _require_session_identity(parsed.session)

        prov = parsed.provenance
        sha256 = prov.sha256
        if sha256 is None and raw_bytes is not None:
            sha256 = hashlib.sha256(raw_bytes).hexdigest()
        digest = content_digest(parsed)

        ctx = _ImportContext()
        no_op: ImportReport | None = None
        club_id = 0
        session_id = 0
        session_created = False
        events: EventReport | None = None
        with self._transaction():
            stale_import_id: int | None = None
            seen = self._find_import(prov.message_id, sha256, digest)
            if seen is not None:
                no_op = self._already_imported_report(seen, parsed, sha256, digest, raw_bytes)
                if no_op is None:
                    # The session this email produced was deleted: the provenance
                    # row is an orphan, so drop it and rebuild from the same email.
                    stale_import_id = int(seen["id"])
            if no_op is None:
                # Content addressed, so a rollback can at worst leave an
                # unreferenced copy of the raw email behind.
                raw_path = self._store_raw(sha256, raw_bytes)
                if stale_import_id is not None:
                    self._execute("DELETE FROM email_import WHERE id = ?", (stale_import_id,))
                club_id = self._merge_club(parsed.club, ctx)
                session_id, session_created = self._merge_session(club_id, parsed.session, ctx)
                import_id = self._insert_email_import(
                    parsed, session_id, sha256, digest, raw_path
                )
                self._merge_entries(session_id, parsed, ctx)
                self._merge_laps(session_id, parsed, ctx)
                self._merge_rankings(session_id, parsed, ctx)
                self._merge_history(parsed, ctx)
                self._log_conflicts(import_id, session_id, ctx)
                # Same transaction as the data it describes: a session is never
                # visible without the joker/pit tags derived from its laps.
                events = self._retag_events(session_id, None)

        if no_op is not None:
            return no_op
        report = ImportReport(
            session_id=session_id,
            club_id=club_id,
            session_created=session_created,
            already_imported=False,
            inserted_laps=ctx.inserted_laps,
            updated_laps=ctx.updated_laps,
            inserted_entries=ctx.inserted_entries,
            conflicts=[conflict.message() for conflict in ctx.conflicts],
            warnings=list(parsed.warnings),
        )
        if events is not None:
            report.auto_jokers = sum(1 for event in events.events if event.kind == LapTag.JOKER)
            report.auto_pits = sum(1 for event in events.events if event.kind == LapTag.PIT)
            report.drivers_without_joker = list(events.drivers_without_joker)
            report.drivers_without_pit = list(events.drivers_without_pit)
        return report

    # -- import: provenance ------------------------------------------------ #

    def _find_import(
        self, message_id: str | None, sha256: str | None, content_sha256: str | None = None
    ) -> sqlite3.Row | None:
        """Look up a previous import by message id, file digest or content digest."""
        if message_id is None and sha256 is None and content_sha256 is None:
            return None
        return self._one(
            """
            SELECT * FROM email_import
             WHERE (:message_id IS NOT NULL AND message_id = :message_id)
                OR (:sha256 IS NOT NULL AND sha256 = :sha256)
                OR (:content IS NOT NULL AND content_sha256 = :content)
             LIMIT 1
            """,
            {"message_id": message_id, "sha256": sha256, "content": content_sha256},
        )

    def _already_imported_report(
        self,
        seen: sqlite3.Row,
        parsed: ParsedEmail,
        sha256: str | None = None,
        content_sha256: str | None = None,
        raw_bytes: bytes | None = None,
    ) -> ImportReport | None:
        """No-op report for an email already stored, or ``None`` if it is stale.

        ``None`` means the provenance row survived but the session it produced
        was deleted, so the caller must re-import instead of refusing.  Digests
        and the raw copy missing from the stored row are backfilled here: an
        email first imported without its bytes can acquire them later.
        """
        session_id = seen["session_id"]
        club_row = self._lookup_club(parsed.club)
        club_id = club_row["id"] if club_row is not None else None
        if session_id is not None:
            if self._one("SELECT id FROM session WHERE id = ?", (session_id,)) is None:
                return None
        elif club_id is not None:
            session_row = self._lookup_session(club_id, parsed.session)
            session_id = session_row["id"] if session_row is not None else None
        if session_id is None or club_id is None:
            return None
        self._backfill_import(seen, sha256, content_sha256, raw_bytes)
        return ImportReport(
            session_id=int(session_id),
            club_id=int(club_id),
            session_created=False,
            already_imported=True,
            conflicts=[],
            warnings=list(parsed.warnings),
        )

    def _backfill_import(
        self,
        seen: sqlite3.Row,
        sha256: str | None,
        content_sha256: str | None,
        raw_bytes: bytes | None,
    ) -> None:
        """Complete a provenance row matched on one key with the keys it lacks."""
        updates: dict[str, Any] = {}
        if sha256 is not None and seen["sha256"] is None:
            taken = self._one(
                "SELECT id FROM email_import WHERE sha256 = ? AND id <> ?", (sha256, seen["id"])
            )
            if taken is None:
                updates["sha256"] = sha256
        if content_sha256 is not None and seen["content_sha256"] is None:
            taken = self._one(
                "SELECT id FROM email_import WHERE content_sha256 = ? AND id <> ?",
                (content_sha256, seen["id"]),
            )
            if taken is None:
                updates["content_sha256"] = content_sha256
        if raw_bytes is not None and seen["raw_path"] is None:
            raw_path = self._store_raw(sha256, raw_bytes)
            if raw_path is not None:
                updates["raw_path"] = raw_path
        if not updates:
            return
        assignments = ", ".join(f"{column} = ?" for column in updates)
        # Called from inside the import transaction; no nested BEGIN here.
        self._execute(
            f"UPDATE email_import SET {assignments} WHERE id = ?",
            (*updates.values(), int(seen["id"])),
        )

    def _store_raw(self, sha256: str | None, raw_bytes: bytes | None) -> str | None:
        """Persist the raw email under ``<raw_dir>/<sha256>.eml``.

        ``None`` when there is nothing to store, or when the database keeps no
        raw directory at all (in-memory databases, see :meth:`__init__`).
        """
        if raw_bytes is None or self.raw_dir is None:
            return None
        digest = sha256 or hashlib.sha256(raw_bytes).hexdigest()
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        target = self.raw_dir / f"{digest}.eml"
        if not target.exists():
            target.write_bytes(raw_bytes)
        return str(target)

    def _insert_email_import(
        self,
        parsed: ParsedEmail,
        session_id: int,
        sha256: str | None,
        content_sha256: str | None,
        raw_path: str | None,
    ) -> int:
        prov = parsed.provenance
        cursor = self._execute(
            """
            INSERT INTO email_import (message_id, sha256, content_sha256, source_path, raw_path,
                                      subject, sent_at, recipient_email, recipient_nickname,
                                      session_id, imported_at, status, warnings)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'imported', ?)
            """,
            (
                prov.message_id,
                sha256,
                content_sha256,
                prov.source_path,
                raw_path,
                prov.subject,
                _dt_text(prov.sent_at),
                prov.recipient_email,
                prov.recipient_nickname,
                session_id,
                _utcnow(),
                json.dumps(list(parsed.warnings), ensure_ascii=False),
            ),
        )
        return int(cursor.lastrowid or 0)

    def _log_conflicts(self, import_id: int, session_id: int, ctx: _ImportContext) -> None:
        if not ctx.conflicts:
            return
        now = _utcnow()
        self._executemany(
            """
            INSERT INTO import_conflict (email_import_id, session_id, entity, ref, field,
                                         stored_value, incoming_value, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    import_id,
                    session_id,
                    conflict.entity,
                    conflict.ref,
                    conflict.field_name,
                    _text(conflict.stored),
                    _text(conflict.incoming),
                    now,
                )
                for conflict in ctx.conflicts
            ],
        )

    # -- import: entities -------------------------------------------------- #

    def _lookup_club(self, club: Club) -> sqlite3.Row | None:
        if club.external_id is not None:
            row = self._one("SELECT * FROM club WHERE external_id = ?", (club.external_id,))
            if row is not None:
                return row
        return self._one("SELECT * FROM club WHERE name = ?", (club.name,))

    def _merge_club(self, club: Club, ctx: _ImportContext) -> int:
        row = self._lookup_club(club)
        if row is None:
            cursor = self._execute(
                "INSERT INTO club (name, external_id, website, email) VALUES (?, ?, ?, ?)",
                (club.name, club.external_id, club.website, club.email),
            )
            return int(cursor.lastrowid or 0)
        club_id = int(row["id"])
        incoming: dict[str, Any] = {
            "name": club.name,
            "website": club.website,
            "email": club.email,
        }
        if club.external_id is not None and row["external_id"] is None:
            taken = self._one(
                "SELECT id FROM club WHERE external_id = ? AND id <> ?",
                (club.external_id, club_id),
            )
            if taken is None:
                incoming["external_id"] = club.external_id
            else:
                ctx.conflict("club", club.name, "external_id", "taken by another club",
                             club.external_id)
        elif club.external_id is not None:
            incoming["external_id"] = club.external_id
        self._fill_missing(
            "club", club_id, row, incoming, entity="club", ref=club.name, ctx=ctx
        )
        return club_id

    def _driver_id(self, driver: Driver, ctx: _ImportContext) -> int:
        """Resolve (or create) a driver, preferring the stable Apex external id.

        SPEC 1.8 makes ``client=<id>`` the identity of a driver precisely because
        the nickname can change.  A row found by external id under a different
        nickname is therefore a *rename*, not a second driver: the stored
        nickname is updated and the rename is recorded as a conflict so the
        change is visible in ``import_conflict``.
        """
        cached = ctx.driver_ids.get(driver.nickname)
        if cached is not None:
            return cached

        by_external: sqlite3.Row | None = None
        if driver.external_id is not None:
            by_external = self._one(
                "SELECT * FROM driver WHERE external_id = ?", (driver.external_id,)
            )
        by_nickname = self._one("SELECT * FROM driver WHERE nickname = ?", (driver.nickname,))

        external_id = driver.external_id
        if (
            by_external is not None
            and by_nickname is not None
            and int(by_external["id"]) != int(by_nickname["id"])
        ):
            # The external id already belongs to a *different* stored driver.
            # Two existing drivers must never be merged behind the user's back,
            # so keep both, stay on the nickname row and report the clash.
            ctx.conflict("driver", driver.nickname, "external_id",
                         f"already used by {by_external['nickname']}", driver.external_id)
            row, external_id = by_nickname, None
        else:
            row = by_external if by_external is not None else by_nickname

        if row is None:
            cursor = self._execute(
                "INSERT INTO driver (nickname, external_id) VALUES (?, ?)",
                (driver.nickname, external_id),
            )
            driver_id = int(cursor.lastrowid or 0)
        else:
            driver_id = int(row["id"])
            self._reconcile_driver(driver.nickname, external_id, row, driver_id, ctx)
        ctx.driver_ids[driver.nickname] = driver_id
        return driver_id

    def _reconcile_driver(
        self,
        nickname: str,
        external_id: str | None,
        row: sqlite3.Row,
        driver_id: int,
        ctx: _ImportContext,
    ) -> None:
        """Apply a rename / attach an external id to an existing driver row."""
        stored_nickname = str(row["nickname"])
        if stored_nickname != nickname:
            # Only reachable through the external id, and only when the new
            # nickname is free: the same person under a new name.
            self._execute(
                "UPDATE driver SET nickname = ? WHERE id = ?", (nickname, driver_id)
            )
            ctx.conflict(
                "driver", nickname, "nickname", stored_nickname, nickname,
                note=f"renamed from {stored_nickname} to {nickname} "
                f"(same Apex external_id {external_id})",
            )
            return

        if external_id is None:
            return
        if row["external_id"] is None:
            self._execute(
                "UPDATE driver SET external_id = ? WHERE id = ?", (external_id, driver_id)
            )
        elif row["external_id"] != external_id:
            ctx.conflict("driver", nickname, "external_id", row["external_id"], external_id)

    def _lookup_session(self, club_id: int, session: Session) -> sqlite3.Row | None:
        return self._one(
            """
            SELECT * FROM session
             WHERE club_id = ?
               AND name = ?
               AND COALESCE(code, '') = COALESCE(?, '')
               AND COALESCE(started_at, '') = COALESCE(?, '')
            """,
            (club_id, session.name, session.code, _dt_text(session.started_at)),
        )

    def _merge_session(
        self, club_id: int, session: Session, ctx: _ImportContext
    ) -> tuple[int, bool]:
        row = self._lookup_session(club_id, session)
        if row is None:
            cursor = self._execute(
                """
                INSERT INTO session (club_id, name, code, started_at, track, category,
                                     tz_name, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    club_id,
                    session.name,
                    session.code,
                    _dt_text(session.started_at),
                    session.track,
                    session.category,
                    session.tz_name,
                    _utcnow(),
                ),
            )
            return int(cursor.lastrowid or 0), True
        session_id = int(row["id"])
        self._fill_missing(
            "session",
            session_id,
            row,
            {"track": session.track, "category": session.category, "tz_name": session.tz_name},
            entity="session",
            ref=session.name,
            ctx=ctx,
        )
        return session_id, False

    def _merge_entries(self, session_id: int, parsed: ParsedEmail, ctx: _ImportContext) -> None:
        # `existing` is refreshed inside the loop: a natural key repeated inside
        # one email must merge, not hit the UNIQUE index and reject the email.
        existing = {
            int(row["driver_id"]): row
            for row in self._all("SELECT * FROM session_entry WHERE session_id = ?", (session_id,))
        }
        added: set[int] = set()
        for entry in parsed.entries:
            driver_id = self._driver_id(entry.driver, ctx)
            values: dict[str, Any] = {
                "position": entry.position,
                "kart": entry.kart,
                "laps_count": entry.laps_count,
                "gap_ms": entry.gap_ms,
                "gap_laps": entry.gap_laps,
                "best_lap_ms": entry.best_lap_ms,
            }
            row = existing.get(driver_id)
            if row is None:
                columns = ", ".join(values)
                placeholders = ", ".join("?" for _ in values)
                cursor = self._execute(
                    f"INSERT INTO session_entry (session_id, driver_id, {columns}) "
                    f"VALUES (?, ?, {placeholders})",
                    (session_id, driver_id, *values.values()),
                )
                ctx.inserted_entries += 1
                added.add(driver_id)
                inserted = self._one(
                    "SELECT * FROM session_entry WHERE id = ?", (int(cursor.lastrowid or 0),)
                )
                if inserted is not None:
                    existing[driver_id] = inserted
            else:
                if driver_id in added:
                    ctx.duplicate("session_entry", entry.driver.nickname)
                self._fill_missing(
                    "session_entry",
                    int(row["id"]),
                    row,
                    values,
                    entity="session_entry",
                    ref=entry.driver.nickname,
                    ctx=ctx,
                )

    def _merge_laps(self, session_id: int, parsed: ParsedEmail, ctx: _ImportContext) -> None:
        # `existing` is refreshed inside the loop: a lap number repeated inside
        # one email (a continuation table whose header was not recognised as
        # consecutive) must merge, not reject the whole email on the UNIQUE index.
        existing: dict[tuple[int, int], sqlite3.Row] = {
            (int(row["driver_id"]), int(row["lap_number"])): row
            for row in self._all("SELECT * FROM lap WHERE session_id = ?", (session_id,))
        }
        best_lap: dict[int, int] = {
            int(row["driver_id"]): int(row["lap_number"])
            for row in self._all(
                "SELECT driver_id, lap_number FROM lap WHERE session_id = ? AND is_best = 1",
                (session_id,),
            )
        }
        added: set[tuple[int, int]] = set()
        for lap in parsed.laps:
            driver_id = self._driver_id(lap.driver, ctx)
            ref = f"{lap.driver.nickname} lap {lap.lap_number}"
            key = (driver_id, int(lap.lap_number))
            row = existing.get(key)
            if row is None:
                is_best = self._accept_best(lap, driver_id, best_lap, ref, ctx)
                cursor = self._execute(
                    """
                    INSERT INTO lap (session_id, driver_id, lap_number, time_ms, is_best)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (session_id, driver_id, lap.lap_number, lap.time_ms, int(is_best)),
                )
                lap_id = int(cursor.lastrowid or 0)
                self._merge_sectors(lap_id, lap, ref, ctx)
                ctx.inserted_laps += 1
                added.add(key)
                inserted = self._one("SELECT * FROM lap WHERE id = ?", (lap_id,))
                if inserted is not None:
                    existing[key] = inserted
                continue

            if key in added:
                ctx.duplicate("lap", ref)
            lap_id = int(row["id"])
            changed = self._fill_missing(
                "lap", lap_id, row, {"time_ms": lap.time_ms}, entity="lap", ref=ref, ctx=ctx
            )
            if lap.is_best and not row["is_best"] and self._accept_best(
                lap, driver_id, best_lap, ref, ctx
            ):
                self._execute("UPDATE lap SET is_best = 1 WHERE id = ?", (lap_id,))
                existing[key] = self._one("SELECT * FROM lap WHERE id = ?", (lap_id,)) or row
                changed.append("is_best")
            if self._merge_sectors(lap_id, lap, ref, ctx):
                changed.append("sectors")
            if changed:
                ctx.updated_laps += 1

    def _accept_best(
        self,
        lap: Lap,
        driver_id: int,
        best_lap: dict[int, int],
        ref: str,
        ctx: _ImportContext,
    ) -> bool:
        """Whether this lap may carry the driver's ``is_best`` highlight.

        A driver has exactly one best lap per session.  When a second email
        highlights a different lap, the stored flag wins and the disagreement is
        reported instead of silently producing two "best" laps.
        """
        if not lap.is_best:
            return False
        marked = best_lap.get(driver_id)
        if marked is None:
            best_lap[driver_id] = int(lap.lap_number)
            return True
        if marked == int(lap.lap_number):
            return True
        ctx.conflict("lap", ref, "is_best", f"lap {marked}", f"lap {lap.lap_number}")
        return False

    def _merge_sectors(self, lap_id: int, lap: Lap, ref: str, ctx: _ImportContext) -> bool:
        """Add missing sector times of one lap; return True if anything changed."""
        if not lap.sectors:
            return False
        stored = {
            int(row["sector_index"]): row["time_ms"]
            for row in self._all(
                "SELECT sector_index, time_ms FROM lap_sector WHERE lap_id = ?", (lap_id,)
            )
        }
        changed = False
        for index, value in enumerate(lap.sectors, start=1):
            if index not in stored:
                self._execute(
                    "INSERT INTO lap_sector (lap_id, sector_index, time_ms) VALUES (?, ?, ?)",
                    (lap_id, index, value),
                )
                changed = changed or value is not None
            elif stored[index] is None:
                if value is not None:
                    self._execute(
                        "UPDATE lap_sector SET time_ms = ? WHERE lap_id = ? AND sector_index = ?",
                        (value, lap_id, index),
                    )
                    changed = True
            elif value is not None and stored[index] != value:
                ctx.conflict("lap_sector", ref, f"S{index}", stored[index], value)
        return changed

    def _merge_rankings(self, session_id: int, parsed: ParsedEmail, ctx: _ImportContext) -> None:
        existing = {
            (str(row["kind"]), int(row["rank"])): row
            for row in self._all("SELECT * FROM ranking_entry WHERE session_id = ?", (session_id,))
        }
        added: set[tuple[str, int]] = set()
        for entry in parsed.rankings:
            kind = entry.kind.value if isinstance(entry.kind, RankingKind) else str(entry.kind)
            driver_id = self._driver_id(entry.driver, ctx)
            ref = f"{kind} #{entry.rank}"
            key = (kind, int(entry.rank))
            row = existing.get(key)
            if row is None:
                cursor = self._execute(
                    """
                    INSERT INTO ranking_entry (session_id, kind, rank, driver_id,
                                               best_lap_ms, category)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (session_id, kind, entry.rank, driver_id, entry.best_lap_ms, entry.category),
                )
                added.add(key)
                inserted = self._one(
                    "SELECT * FROM ranking_entry WHERE id = ?", (int(cursor.lastrowid or 0),)
                )
                if inserted is not None:
                    existing[key] = inserted
                continue
            if key in added:
                ctx.duplicate("ranking_entry", ref)
            if int(row["driver_id"]) != driver_id:
                stored_nick = self._one("SELECT nickname FROM driver WHERE id = ?",
                                        (row["driver_id"],))
                ctx.conflict(
                    "ranking_entry",
                    ref,
                    "driver",
                    stored_nick["nickname"] if stored_nick else row["driver_id"],
                    entry.driver.nickname,
                )
                continue
            self._fill_missing(
                "ranking_entry",
                int(row["id"]),
                row,
                {"best_lap_ms": entry.best_lap_ms, "category": entry.category},
                entity="ranking_entry",
                ref=ref,
                ctx=ctx,
            )

    def _merge_history(self, parsed: ParsedEmail, ctx: _ImportContext) -> None:
        """Store the recipient's "Your last sessions" rows (whole row is the key)."""
        nickname = parsed.provenance.recipient_nickname
        if not nickname or not parsed.history:
            return
        driver_id = self._driver_id(Driver(nickname=nickname), ctx)
        for item in parsed.history:
            self._execute(
                """
                INSERT OR IGNORE INTO history_entry (driver_id, date, position, best_lap_ms,
                                                     laps_count, category)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    driver_id,
                    _date_text(item.date),
                    item.position,
                    item.best_lap_ms,
                    item.laps_count,
                    item.category,
                ),
            )

    # ------------------------------------------------------------------ #
    # Readers
    # ------------------------------------------------------------------ #

    def list_sessions(self) -> list[dict]:
        """All sessions, most recent first."""
        rows = self._all(
            """
            SELECT s.id, s.name, s.code, s.started_at, s.track, s.category,
                   s.club_id, c.name AS club,
                   (SELECT COUNT(*) FROM session_entry e WHERE e.session_id = s.id)
                       AS drivers_count,
                   (SELECT COUNT(*) FROM lap l WHERE l.session_id = s.id) AS laps_count
              FROM session s
              JOIN club c ON c.id = s.club_id
             ORDER BY COALESCE(s.started_at, '') DESC, s.id DESC
            """
        )
        return [dict(row) for row in rows]

    def get_session(self, session_id: int) -> dict | None:
        """Session header, its club and the classification, or None."""
        if _row_id(session_id) is None:
            return None
        row = self._one("SELECT * FROM session WHERE id = ?", (session_id,))
        if row is None:
            return None
        club = self._one("SELECT * FROM club WHERE id = ?", (row["club_id"],))
        entries = self._all(
            """
            SELECT e.id, e.driver_id, d.nickname AS driver, d.external_id AS driver_external_id,
                   e.position, e.kart, e.laps_count, e.gap_ms, e.gap_laps, e.best_lap_ms
              FROM session_entry e
              JOIN driver d ON d.id = e.driver_id
             WHERE e.session_id = ?
             ORDER BY (e.position IS NULL), e.position, d.nickname
            """,
            (session_id,),
        )
        return {
            "session": dict(row),
            "club": dict(club) if club is not None else None,
            "entries": [dict(entry) for entry in entries],
        }

    def session_laps(self, session_id: int) -> list[dict]:
        """Every lap of a session with its sectors and manual tags."""
        if _row_id(session_id) is None:
            return []
        rows = self._all(
            """
            SELECT l.id, l.session_id, l.driver_id, d.nickname AS driver,
                   l.lap_number, l.time_ms, l.is_best
              FROM lap l
              JOIN driver d ON d.id = l.driver_id
             WHERE l.session_id = ?
             ORDER BY d.nickname, l.lap_number
            """,
            (session_id,),
        )
        sectors: dict[int, list[int | None]] = {}
        for sector in self._all(
            """
            SELECT s.lap_id, s.sector_index, s.time_ms
              FROM lap_sector s
              JOIN lap l ON l.id = s.lap_id
             WHERE l.session_id = ?
             ORDER BY s.lap_id, s.sector_index
            """,
            (session_id,),
        ):
            sectors.setdefault(int(sector["lap_id"]), []).append(sector["time_ms"])
        annotations = self.lap_annotations(session_id)
        tags = _effective_tags(annotations)
        result: list[dict] = []
        for row in rows:
            lap = dict(row)
            lap_id = int(row["id"])
            lap["is_best"] = bool(row["is_best"])
            lap["sectors"] = sectors.get(lap_id, [])
            # `tags` is the effective set every consumer must agree on;
            # `annotations` keeps both sources so the UI can show what the
            # detector proposed next to what the human decided.
            lap["tags"] = tags.get(lap_id, [])
            lap["annotations"] = annotations.get(lap_id, [])
            result.append(lap)
        return result

    def list_drivers(self) -> list[dict]:
        """Every known driver with a few aggregates.

        Drivers that only ever appeared in a leaderboard of somebody else's
        email have no session of their own; their best lap is still known from
        that leaderboard, so it is reported and ``source`` says where it came
        from (``"session"`` or ``"ranking"``).
        """
        rows = self._all(
            """
            SELECT d.id AS driver_id, d.nickname, d.external_id,
                   (SELECT COUNT(DISTINCT e.session_id) FROM session_entry e
                     WHERE e.driver_id = d.id) AS sessions_count,
                   (SELECT MIN(l.time_ms) FROM lap l
                     WHERE l.driver_id = d.id AND l.time_ms IS NOT NULL) AS best_from_laps,
                   (SELECT MIN(e.best_lap_ms) FROM session_entry e
                     WHERE e.driver_id = d.id AND e.best_lap_ms IS NOT NULL) AS best_from_entries,
                   (SELECT MIN(r.best_lap_ms) FROM ranking_entry r
                     WHERE r.driver_id = d.id AND r.best_lap_ms IS NOT NULL) AS best_from_rankings,
                   (SELECT MAX(s.started_at) FROM session s
                      JOIN session_entry e ON e.session_id = s.id
                     WHERE e.driver_id = d.id) AS last_seen,
                   (SELECT MAX(s.started_at) FROM session s
                      JOIN ranking_entry r ON r.session_id = s.id
                     WHERE r.driver_id = d.id) AS ranked_at
              FROM driver d
             ORDER BY d.nickname
            """
        )
        drivers: list[dict] = []
        for row in rows:
            item = dict(row)
            ranked_at = item.pop("ranked_at")
            from_rankings = item.pop("best_from_rankings")
            candidates = [
                value
                for value in (item.pop("best_from_laps"), item.pop("best_from_entries"))
                if value is not None
            ]
            if candidates:
                item["best_lap_ms"] = min(candidates)
                item["source"] = "session"
            else:
                item["best_lap_ms"] = from_rankings
                item["source"] = "ranking" if from_rankings is not None else "unknown"
                if item["last_seen"] is None:
                    item["last_seen"] = ranked_at
            drivers.append(item)
        return drivers

    def driver_history(self, nickname: str) -> list[dict]:
        """Past results of a driver: stored sessions plus emailed history rows."""
        by_key: dict[tuple[Any, ...], dict] = {}
        for row in self._all(
            """
            SELECT substr(s.started_at, 1, 10) AS date, e.position, e.best_lap_ms,
                   e.laps_count, s.category, s.id AS session_id, s.name AS session_name,
                   s.started_at
              FROM session_entry e
              JOIN session s ON s.id = e.session_id
              JOIN driver d ON d.id = e.driver_id
             WHERE d.nickname = ?
            """,
            (nickname,),
        ):
            item = dict(row)
            item["source"] = "session"
            by_key[(item["date"], item["position"], item["best_lap_ms"], item["laps_count"])] = item
        for row in self._all(
            """
            SELECT h.date, h.position, h.best_lap_ms, h.laps_count, h.category
              FROM history_entry h
              JOIN driver d ON d.id = h.driver_id
             WHERE d.nickname = ?
            """,
            (nickname,),
        ):
            key = (row["date"], row["position"], row["best_lap_ms"], row["laps_count"])
            if key in by_key:
                continue
            item = dict(row)
            item.update({"session_id": None, "session_name": None, "started_at": None,
                         "source": "email_history"})
            by_key[key] = item
        history = list(by_key.values())
        history.sort(
            key=lambda item: (
                item["date"] or "",
                item["started_at"] or "",
                -(item["position"] or 0),
            ),
            reverse=True,
        )
        return history

    def rankings(self, session_id: int) -> dict:
        """Leaderboards captured together with a session."""
        result: dict[str, list[dict]] = {kind.value: [] for kind in RankingKind}
        if _row_id(session_id) is None:
            return result
        for row in self._all(
            """
            SELECT r.kind, r.rank, d.nickname AS driver, r.best_lap_ms, r.category
              FROM ranking_entry r
              JOIN driver d ON d.id = r.driver_id
             WHERE r.session_id = ?
             ORDER BY r.kind, r.rank
            """,
            (session_id,),
        ):
            item = dict(row)
            result.setdefault(str(item.pop("kind")), []).append(item)
        return result

    # ------------------------------------------------------------------ #
    # Lap annotations (manual + automatic)
    # ------------------------------------------------------------------ #

    def add_lap_tag(self, lap_id: int, tag: str, note: str | None = None) -> None:
        """Attach a *manual* tag to a lap (idempotent; the note is refreshed).

        This is the human path and the only writer of ``source='manual'``.
        Because a manual row hides every automatic row of the same lap, tagging
        a lap also silences whatever the detector proposed for it.
        """
        normalised = self._checked_tag(tag)
        with self._transaction():
            self._require_lap(lap_id)
            self._insert_manual_tag(lap_id, normalised, note)

    def remove_lap_tag(self, lap_id: int, tag: str) -> None:
        """Drop a tag from the effective set of a lap; unknown tags are ignored.

        Manual rows are deleted outright.  A tag that exists only as a detector
        proposal cannot be deleted -- the next :meth:`detect_and_tag_events`
        would insert it again -- so it is *overridden* instead: the automatic
        row is kept for the record and a manual :data:`OVERRIDE_TAG`
        annotation is written, which by the priority rule of SPEC 10.3 hides
        every automatic row of that lap for good.
        """
        if _row_id(lap_id) is None:
            return
        normalised = tag.strip().casefold()
        with self._transaction():
            self._execute(
                "DELETE FROM lap_annotation WHERE lap_id = ? AND tag = ? AND source = ?",
                (lap_id, normalised, MANUAL_SOURCE),
            )
            if normalised == OVERRIDE_TAG:
                return
            still_manual = self._one(
                "SELECT 1 FROM lap_annotation WHERE lap_id = ? AND source = ? LIMIT 1",
                (lap_id, MANUAL_SOURCE),
            )
            if still_manual is not None:
                return
            proposed = self._one(
                "SELECT 1 FROM lap_annotation WHERE lap_id = ? AND tag = ? AND source = ? LIMIT 1",
                (lap_id, normalised, AUTO_SOURCE),
            )
            if proposed is None:
                return
            self._insert_manual_tag(
                lap_id,
                OVERRIDE_TAG,
                f"manual override: the automatic {normalised!r} tag was rejected",
            )

    def set_manual_tags(
        self, lap_id: int, tags: Sequence[str], note: str | None = None
    ) -> None:
        """Replace the whole manual tag set of a lap in one transaction.

        The way to overrule the detector: ``set_manual_tags(lap, ["pit"])``
        makes the lap a pit lap whatever the detector thinks, and
        ``set_manual_tags(lap, [])`` hands the lap back to the detector.
        """
        normalised = [self._checked_tag(tag) for tag in tags]
        with self._transaction():
            self._require_lap(lap_id)
            self._execute(
                "DELETE FROM lap_annotation WHERE lap_id = ? AND source = ?",
                (lap_id, MANUAL_SOURCE),
            )
            for tag in normalised:
                self._insert_manual_tag(lap_id, tag, note)

    def reject_auto_tags(self, lap_id: int, note: str | None = None) -> None:
        """Declare a lap plain race pace, hiding every automatic tag of it.

        Recorded as a manual annotation, so re-running the detector cannot
        bring the rejected joker/pit tag back.
        """
        self.add_lap_tag(
            lap_id, OVERRIDE_TAG, note or "manual override: automatic tags rejected"
        )

    def lap_tags(self, session_id: int) -> dict[int, list[dict]]:
        """Effective tags of a session, grouped by lap id (SPEC 10.3).

        A lap with manual annotations reports only those; a lap without them
        reports the detector's.  This is what the API and the statistics use,
        so every layer sees the same picture -- see :meth:`lap_annotations` for
        the raw rows of both sources.
        """
        return _effective_tags(self.lap_annotations(session_id))

    def lap_annotations(self, session_id: int) -> dict[int, list[dict]]:
        """Raw annotation rows of a session (both sources), grouped by lap id."""
        grouped: dict[int, list[dict]] = {}
        if _row_id(session_id) is None:
            return grouped
        for row in self._all(
            """
            SELECT a.lap_id, a.tag, a.note, a.created_at, a.source
              FROM lap_annotation a
              JOIN lap l ON l.id = a.lap_id
             WHERE l.session_id = ?
             ORDER BY a.lap_id, a.source, a.tag
            """,
            (session_id,),
        ):
            item = dict(row)
            grouped.setdefault(int(item.pop("lap_id")), []).append(item)
        return grouped

    # -- annotation helpers ------------------------------------------------ #

    @staticmethod
    def _checked_tag(tag: str) -> str:
        """Normalise a tag and refuse anything outside :data:`KNOWN_LAP_TAGS`."""
        normalised = tag.strip().casefold()
        if normalised not in KNOWN_LAP_TAGS:
            raise UnknownTagError(
                f"unknown lap tag {tag!r}; known tags: {', '.join(sorted(KNOWN_LAP_TAGS))}"
            )
        return normalised

    def _require_lap(self, lap_id: int) -> int:
        """Return an existing lap id or raise :class:`UnknownLapError`."""
        if _row_id(lap_id) is None:
            raise UnknownLapError(f"no lap with id {lap_id}")
        if self._one("SELECT id FROM lap WHERE id = ?", (lap_id,)) is None:
            raise UnknownLapError(f"no lap with id {lap_id}")
        return int(lap_id)

    def _insert_manual_tag(self, lap_id: int, tag: str, note: str | None) -> None:
        """Upsert one manual row; callers own the transaction and the checks."""
        self._execute(
            """
            INSERT INTO lap_annotation (lap_id, tag, note, created_at, source)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (lap_id, tag, source) DO UPDATE SET note = excluded.note
            """,
            (lap_id, tag, note, _utcnow(), MANUAL_SOURCE),
        )

    # ------------------------------------------------------------------ #
    # Automatic joker / pit annotations
    # ------------------------------------------------------------------ #

    def detect_and_tag_events(
        self, session_id: int, config: EventDetectionConfig | None = None
    ) -> EventReport:
        """Re-run the joker/pit detector over a session and store its verdict.

        Every ``source='auto'`` row of the session is replaced by the fresh
        detection; ``source='manual'`` rows are never read, written or deleted.
        The result is deterministic, so calling this twice in a row leaves the
        database byte for byte identical (ids and timestamps of unchanged rows
        are preserved on purpose).
        """
        with self._transaction():
            return self._retag_events(session_id, config)

    def _retag_events(
        self, session_id: int, config: EventDetectionConfig | None
    ) -> EventReport:
        """Body of :meth:`detect_and_tag_events`; the caller owns the transaction."""
        detect_events, default_config, _ = _load_detector()
        settings = config if config is not None else default_config()
        if _row_id(session_id) is None:
            # An id no row can have: report nothing rather than bind it.
            return detect_events({}, settings)
        laps = self._laps_for_detection(session_id)
        points = {
            driver: [point for _, point in items] for driver, items in laps.items()
        }
        lap_ids = {
            (driver, point.lap_number): lap_id
            for driver, items in laps.items()
            for lap_id, point in items
        }
        # Events the detector reports because a human annotated the lap are the
        # human's own rows: writing an automatic copy would duplicate the verdict
        # and, worse, outlive the manual tag if it were ever removed.
        manual_pairs = {
            (lap_id, str(tag).strip().casefold())
            for items in laps.values()
            for lap_id, point in items
            for tag in point.tags
        }
        report = detect_events(points, settings)

        previous = {
            (int(row["lap_id"]), str(row["tag"])): (int(row["id"]), str(row["created_at"]))
            for row in self._all(
                """
                SELECT a.id, a.lap_id, a.tag, a.created_at
                  FROM lap_annotation a
                  JOIN lap l ON l.id = a.lap_id
                 WHERE l.session_id = ? AND a.source = ?
                """,
                (session_id, AUTO_SOURCE),
            )
        }
        # Every row of the replacement batch is inserted with an *explicit* id:
        # survivors keep theirs (so ids and timestamps stay stable across runs),
        # and new rows are numbered from the high-water mark taken *before* the
        # delete, which still counts the ids about to be re-used.  Leaving the
        # id NULL for new rows would let SQLite resolve it to max(rowid) + 1 in
        # the middle of the batch and hand it an id a later row then claims
        # explicitly -- a UNIQUE violation whenever a re-detection with looser
        # thresholds produces more events than the stored ones.
        high_water = self._one("SELECT COALESCE(MAX(id), 0) AS value FROM lap_annotation")
        next_id = int(high_water["value"]) if high_water is not None else 0
        self._execute(
            """
            DELETE FROM lap_annotation
             WHERE source = ?
               AND lap_id IN (SELECT id FROM lap WHERE session_id = ?)
            """,
            (AUTO_SOURCE, session_id),
        )
        now = _utcnow()
        rows: list[tuple[Any, ...]] = []
        seen: set[tuple[int, str]] = set()
        for event in report.events:
            tag = str(event.kind).strip().casefold()
            lap_id = lap_ids.get((event.driver, int(event.lap_number)))
            if lap_id is None:
                report.warnings.append(
                    f"{event.driver}: detected {tag} on lap {event.lap_number}, "
                    "which is not stored for this session; not tagged"
                )
                continue
            if tag not in _EVENT_TAGS:
                report.warnings.append(
                    f"{event.driver} lap {event.lap_number}: unknown event kind "
                    f"{event.kind!r}; not tagged"
                )
                continue
            key = (lap_id, tag)
            if key in seen:  # one lap cannot be the same event twice
                continue
            if key in manual_pairs:  # already stored, by hand
                continue
            seen.add(key)
            kept = previous.get(key)
            if kept is None:
                next_id += 1
            rows.append(
                (
                    kept[0] if kept is not None else next_id,
                    lap_id,
                    tag,
                    _auto_note(event),
                    kept[1] if kept is not None else now,
                    AUTO_SOURCE,
                )
            )
        if rows:
            self._executemany(
                """
                INSERT INTO lap_annotation (id, lap_id, tag, note, created_at, source)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        return report

    def _laps_for_detection(self, session_id: int) -> dict[str, list[tuple[int, Any]]]:
        """Session laps as ``{driver: [(lap_id, LapPoint), ...]}``, in lap order.

        Drivers come in classification order (entries without a position last,
        then alphabetically), because `detect_events` preserves the order of its
        input in every list of the report -- so "who has no pit" reads down the
        results sheet instead of down the alphabet.
        """
        _, _, lap_point = _load_detector()
        if _row_id(session_id) is None:
            return {}
        sectors: dict[int, list[int | None]] = {}
        for row in self._all(
            """
            SELECT s.lap_id, s.time_ms
              FROM lap_sector s
              JOIN lap l ON l.id = s.lap_id
             WHERE l.session_id = ?
             ORDER BY s.lap_id, s.sector_index
            """,
            (session_id,),
        ):
            sectors.setdefault(int(row["lap_id"]), []).append(row["time_ms"])
        # Only *manual* tags are fed to the detector: its own previous output is
        # no evidence about a lap, and re-detecting must not depend on what the
        # last detection concluded.
        tags: dict[int, list[str]] = {}
        for lap_id, rows in self.lap_annotations(session_id).items():
            manual = [str(row["tag"]) for row in rows if row["source"] == MANUAL_SOURCE]
            if manual:
                tags[lap_id] = manual
        grouped: dict[str, list[tuple[int, Any]]] = {}
        for row in self._all(
            """
            SELECT l.id, d.nickname AS driver, l.lap_number, l.time_ms
              FROM lap l
              JOIN driver d ON d.id = l.driver_id
              LEFT JOIN session_entry se
                     ON se.session_id = l.session_id AND se.driver_id = l.driver_id
             WHERE l.session_id = ?
             ORDER BY se.position IS NULL, se.position, d.nickname, l.lap_number
            """,
            (session_id,),
        ):
            lap_id = int(row["id"])
            grouped.setdefault(str(row["driver"]), []).append(
                (
                    lap_id,
                    lap_point(
                        lap_number=int(row["lap_number"]),
                        time_ms=row["time_ms"],
                        sectors=tuple(sectors.get(lap_id, ())),
                        tags=tuple(tags.get(lap_id, ())),
                    ),
                )
            )
        return grouped


def open_db(path: str | Path = DEFAULT_DB_PATH, *, raw_dir: str | Path | None = None) -> Database:
    """Open (creating if needed) the SQLite database and apply the schema."""
    return Database(path, raw_dir=raw_dir)
