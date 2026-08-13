"""Tests for the SQLite storage layer.

Two independent sets:

* ``TestSyntheticImport`` -- builds :class:`ParsedEmail` objects by hand from
  ``karting.models``.  They cover schema creation, idempotency, merging,
  conflicts and manual tags without depending on the parser.
* ``TestRealEmail`` -- runs the same storage code on the reference ``.eml``;
  skipped while ``karting.parsing`` does not exist yet.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

from karting.models import (
    Club,
    Driver,
    HistoryEntry,
    Lap,
    ParsedEmail,
    Provenance,
    RankingEntry,
    RankingKind,
    Session,
    SessionEntry,
)
from karting.storage import (
    SCHEMA_VERSION,
    Database,
    ImportReport,
    NoSessionIdentityError,
    UnknownLapError,
    UnknownTagError,
    open_db,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

TABLES = [
    "club",
    "driver",
    "session",
    "session_entry",
    "lap",
    "lap_sector",
    "ranking_entry",
    "history_entry",
    "lap_annotation",
    "email_import",
    "import_conflict",
]

KARTS = {"KOLYA11": "11", "WLAD111": "2", "TWG": "4"}
# Apex "client" ids -- only ever known for the recipient of a given email.
EXTERNAL_IDS = {"KOLYA11": "10001", "WLAD111": "10001", "TWG": "10002"}
LAP_TIMES: dict[str, list[int | None]] = {
    "KOLYA11": [None, 27000, 26500, 26012, 26800],
    "WLAD111": [None, 27500, 26788, 27000, 26900],
    "TWG": [None, 27200, 26900, 25845, 26100],
}
SESSION_STARTED_AT = datetime(2026, 8, 3, 21, 40)


# --------------------------------------------------------------------------- #
# Synthetic fixtures
# --------------------------------------------------------------------------- #


def _sectors_for(time_ms: int | None, lap_number: int) -> list[int | None]:
    """Two sectors summing exactly to the lap time; lap 1 mimics the real email."""
    if lap_number == 1:
        return [56053, 14243]  # sectors present while the lap time is not
    if time_ms is None:
        return []
    first = time_ms // 2
    return [first, time_ms - first]


def make_parsed(
    recipient: str = "WLAD111",
    *,
    message_id: str,
    sha256: str | None = None,
    lap_times: dict[str, list[int | None]] | None = None,
    with_history: bool = True,
) -> ParsedEmail:
    """Build a ParsedEmail for one race, as mailed to ``recipient``."""
    times = lap_times or LAP_TIMES
    club = Club(name="PRIMO KARTING", external_id="51", website="http://www.primokarting.ru")
    session = Session(
        name="PRIMO GARA - Final A",
        started_at=SESSION_STARTED_AT,
        code="FA",
        track="Karting track",
        category="SR5",
    )
    drivers = {
        nickname: Driver(
            nickname=nickname,
            external_id=EXTERNAL_IDS[nickname] if nickname == recipient else None,
        )
        for nickname in times
    }

    entries: list[SessionEntry] = []
    laps: list[Lap] = []
    for position, (nickname, values) in enumerate(times.items(), start=1):
        clean = [value for value in values if value is not None]
        best = min(clean)
        entries.append(
            SessionEntry(
                driver=drivers[nickname],
                position=position,
                kart=KARTS[nickname],
                laps_count=len(values),
                gap_ms=None if position == 1 else 1000 * position,
                best_lap_ms=best,
            )
        )
        for lap_number, time_ms in enumerate(values, start=1):
            laps.append(
                Lap(
                    driver=drivers[nickname],
                    lap_number=lap_number,
                    time_ms=time_ms,
                    # Apex only sends sector times to the recipient of the email.
                    sectors=_sectors_for(time_ms, lap_number) if nickname == recipient else [],
                    is_best=time_ms == best,
                )
            )

    rankings = [
        RankingEntry(
            kind=RankingKind.WEEKLY_BEST,
            rank=1,
            driver=drivers["KOLYA11"],
            best_lap_ms=25640,
            category="SR5",
        ),
        RankingEntry(
            kind=RankingKind.TRACK_RECORD,
            rank=1,
            driver=drivers["TWG"],
            best_lap_ms=20255,
            category="SR5",
        ),
    ]
    history = (
        [
            HistoryEntry(date=date(2026, 8, 3), position=2, best_lap_ms=26788, laps_count=5),
            HistoryEntry(date=date(2026, 7, 20), position=4, best_lap_ms=27100, laps_count=12),
        ]
        if with_history
        else []
    )
    provenance = Provenance(
        message_id=message_id,
        subject="PRIMO KARTING : PRIMO GARA - Final A (FA)",
        sent_at=datetime(2026, 8, 3, 20, 52, 30),
        from_name="PRIMO KARTING",
        from_email="info@primokarting.ru",
        recipient_email=f"{recipient.lower()}@example.com",
        recipient_nickname=recipient,
        source_path=f"/tmp/{recipient}.eml",
        sha256=sha256 if sha256 is not None else hashlib.sha256(message_id.encode()).hexdigest(),
    )
    return ParsedEmail(
        club=club,
        session=session,
        provenance=provenance,
        entries=entries,
        laps=laps,
        rankings=rankings,
        history=history,
        podium=[(index, nickname) for index, nickname in enumerate(times, start=1)][:3],
        warnings=["lap 1 has sectors but no time"],
    )


def counts(db: Database) -> dict[str, int]:
    """Row count of every table -- the yardstick for idempotency."""
    return {
        table: db.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in TABLES
    }


@pytest.fixture()
def db(tmp_path: Path) -> Any:
    """File-backed database in a temp directory."""
    with open_db(tmp_path / "pace.db", raw_dir=tmp_path / "raw_emails") as database:
        yield database


# --------------------------------------------------------------------------- #
# Synthetic import tests
# --------------------------------------------------------------------------- #


class TestSchema:
    def test_tables_created(self, db: Database) -> None:
        names = {
            row["name"]
            for row in db.connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert set(TABLES) <= names

    def test_schema_is_reapplied_idempotently(self, tmp_path: Path) -> None:
        path = tmp_path / "pace.db"
        with open_db(path) as first:
            first.import_parsed(make_parsed(message_id="<a@x>"))
            before = counts(first)
        with open_db(path) as second:  # schema.sql replayed on the populated file
            assert counts(second) == before

    def test_pragmas(self, db: Database) -> None:
        assert db.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert db.connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert db.connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION

    def test_foreign_keys_are_enforced(self, db: Database) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            db.connection.execute(
                "INSERT INTO lap_annotation (lap_id, tag, created_at) VALUES (?, ?, ?)",
                (999_999, "pit", "2026-08-03T00:00:00Z"),
            )

    def test_in_memory_database(self) -> None:
        with open_db(":memory:") as memory:
            report = memory.import_parsed(make_parsed(message_id="<mem@x>"))
            assert report.session_created is True
            assert memory.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            # WAL is meaningless (and unavailable) for an in-memory database.
            assert memory.connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "memory"
            assert len(memory.session_laps(report.session_id)) == 15


class TestImport:
    def test_first_import(self, db: Database) -> None:
        parsed = make_parsed(message_id="<a@x>")
        report = db.import_parsed(parsed)

        assert isinstance(report, ImportReport)
        assert report.session_created is True
        assert report.already_imported is False
        assert report.inserted_entries == 3
        assert report.inserted_laps == 15
        assert report.updated_laps == 0
        assert report.conflicts == []
        assert report.warnings == parsed.warnings

        stored = counts(db)
        assert stored["club"] == 1
        assert stored["driver"] == 3
        assert stored["session"] == 1
        assert stored["session_entry"] == 3
        assert stored["lap"] == 15
        assert stored["ranking_entry"] == 2
        assert stored["history_entry"] == 2
        assert stored["email_import"] == 1
        assert stored["import_conflict"] == 0
        # sectors only for the recipient: lap 1 (2) + laps 2..5 (2 each)
        assert stored["lap_sector"] == 10

    def test_raw_email_is_stored(self, tmp_path: Path) -> None:
        raw = b"From: test\r\n\r\n<html></html>"
        digest = hashlib.sha256(raw).hexdigest()
        with open_db(tmp_path / "pace.db") as database:
            parsed = make_parsed(message_id="<raw@x>", sha256=digest)
            database.import_parsed(parsed, raw_bytes=raw)
            stored = tmp_path / "raw_emails" / f"{digest}.eml"
            assert stored.read_bytes() == raw
            row = database.connection.execute("SELECT raw_path, warnings FROM email_import").fetchone()
            assert row["raw_path"] == str(stored)
            assert "lap 1 has sectors but no time" in row["warnings"]

    def test_reimport_of_the_same_email_is_a_no_op(self, db: Database) -> None:
        parsed = make_parsed(message_id="<a@x>")
        first = db.import_parsed(parsed)
        before = counts(db)

        again = db.import_parsed(make_parsed(message_id="<a@x>"))
        assert again.already_imported is True
        assert again.session_created is False
        assert again.session_id == first.session_id
        assert again.club_id == first.club_id
        assert (again.inserted_laps, again.updated_laps, again.inserted_entries) == (0, 0, 0)
        assert counts(db) == before

    def test_reimport_detected_by_sha_when_message_id_differs(self, db: Database) -> None:
        digest = "f" * 64
        db.import_parsed(make_parsed(message_id="<a@x>", sha256=digest))
        before = counts(db)
        again = db.import_parsed(make_parsed(message_id="<forwarded@x>", sha256=digest))
        assert again.already_imported is True
        assert counts(db) == before

    def test_second_email_of_the_same_race_merges(self, db: Database) -> None:
        db.import_parsed(make_parsed("WLAD111", message_id="<a@x>"))
        before = counts(db)

        report = db.import_parsed(make_parsed("TWG", message_id="<b@x>"))
        after = counts(db)

        assert report.already_imported is False
        assert report.session_created is False          # same (club, name, code, started_at)
        assert report.inserted_laps == 0
        assert report.inserted_entries == 0
        assert report.conflicts == []
        assert report.updated_laps == 5                 # TWG's laps gained sectors

        assert after["session"] == before["session"] == 1
        assert after["driver"] == before["driver"] == 3
        assert after["session_entry"] == before["session_entry"] == 3
        assert after["lap"] == before["lap"] == 15
        assert after["ranking_entry"] == before["ranking_entry"] == 2
        assert after["email_import"] == 2
        assert after["lap_sector"] == before["lap_sector"] + 10

        laps = {
            (lap["driver"], lap["lap_number"]): lap for lap in db.session_laps(report.session_id)
        }
        assert laps[("TWG", 4)]["sectors"] == [12922, 12923]
        assert laps[("WLAD111", 3)]["sectors"] == [13394, 13394]
        assert laps[("KOLYA11", 2)]["sectors"] == []     # nobody mailed those

    def test_second_email_does_not_overwrite_existing_values(self, db: Database) -> None:
        first = db.import_parsed(make_parsed("WLAD111", message_id="<a@x>"))
        # The second email disagrees about one lap of another driver.
        divergent = {name: list(values) for name, values in LAP_TIMES.items()}
        divergent["TWG"][3] = 25999
        report = db.import_parsed(
            make_parsed("TWG", message_id="<b@x>", lap_times=divergent)
        )

        assert report.session_id == first.session_id
        assert any("TWG lap 4" in message and "time_ms" in message for message in report.conflicts)

        stored = db.connection.execute(
            """
            SELECT l.time_ms FROM lap l JOIN driver d ON d.id = l.driver_id
             WHERE d.nickname = 'TWG' AND l.lap_number = 4
            """
        ).fetchone()
        assert stored["time_ms"] == 25845               # original value survives

        logged = db.connection.execute(
            "SELECT entity, ref, field, stored_value, incoming_value FROM import_conflict"
        ).fetchall()
        assert ("lap", "TWG lap 4", "time_ms", "25845", "25999") in [
            tuple(row) for row in logged
        ]

    def test_missing_lap_time_is_filled_in(self, db: Database) -> None:
        partial = {name: list(values) for name, values in LAP_TIMES.items()}
        partial["KOLYA11"][4] = None
        db.import_parsed(make_parsed("WLAD111", message_id="<a@x>", lap_times=partial))

        report = db.import_parsed(make_parsed("TWG", message_id="<b@x>"))
        assert report.conflicts == []
        stored = db.connection.execute(
            """
            SELECT l.time_ms FROM lap l JOIN driver d ON d.id = l.driver_id
             WHERE d.nickname = 'KOLYA11' AND l.lap_number = 5
            """
        ).fetchone()
        assert stored["time_ms"] == 26800

    def test_session_fields_are_completed_not_overwritten(self, db: Database) -> None:
        first = make_parsed("WLAD111", message_id="<a@x>")
        first.session.category = None                    # this email did not carry it
        db.import_parsed(first)

        second = make_parsed("TWG", message_id="<b@x>")
        second.session.track = "Outdoor track"           # disagrees with the stored value
        report = db.import_parsed(second)

        row = db.connection.execute(
            "SELECT track, category FROM session WHERE id = ?", (report.session_id,)
        ).fetchone()
        assert row["track"] == "Karting track"           # kept
        assert row["category"] == "SR5"                  # filled in
        assert any("track" in message for message in report.conflicts)

    def test_external_id_clash_is_reported_not_applied(self, db: Database) -> None:
        db.import_parsed(make_parsed("WLAD111", message_id="<a@x>"))
        clashing = make_parsed("TWG", message_id="<b@x>")
        for entry in clashing.entries:
            if entry.driver.nickname == "TWG":
                entry.driver.external_id = EXTERNAL_IDS["WLAD111"]
        report = db.import_parsed(clashing)

        assert any("external_id" in message for message in report.conflicts)
        rows = dict(
            db.connection.execute("SELECT nickname, external_id FROM driver").fetchall()  # type: ignore[arg-type]
        )
        assert rows["WLAD111"] == EXTERNAL_IDS["WLAD111"]
        assert rows["TWG"] is None

    def test_report_is_json_serialisable(self, db: Database) -> None:
        report = db.import_parsed(make_parsed(message_id="<a@x>"))
        assert json.loads(json.dumps(report.to_dict())) == asdict(report)

    def test_failed_import_rolls_back(self, db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*args: object, **kwargs: object) -> None:
            raise RuntimeError("merge exploded")

        monkeypatch.setattr(Database, "_merge_history", boom)
        with pytest.raises(RuntimeError, match="merge exploded"):
            db.import_parsed(make_parsed(message_id="<a@x>"))

        assert counts(db) == dict.fromkeys(TABLES, 0)
        assert db.connection.in_transaction is False

        monkeypatch.undo()
        report = db.import_parsed(make_parsed(message_id="<a@x>"))
        assert report.already_imported is False
        assert report.inserted_laps == 15


class TestReaders:
    @pytest.fixture()
    def populated(self, db: Database) -> Database:
        db.import_parsed(make_parsed("WLAD111", message_id="<a@x>"))
        return db

    def test_list_sessions(self, populated: Database) -> None:
        sessions = populated.list_sessions()
        assert len(sessions) == 1
        session = sessions[0]
        assert session["name"] == "PRIMO GARA - Final A"
        assert session["code"] == "FA"
        assert session["started_at"] == "2026-08-03T21:40:00"
        assert session["track"] == "Karting track"
        assert session["category"] == "SR5"
        assert session["club"] == "PRIMO KARTING"
        assert session["drivers_count"] == 3
        assert session["laps_count"] == 15
        assert isinstance(session, dict)

    def test_get_session(self, populated: Database) -> None:
        session_id = populated.list_sessions()[0]["id"]
        detail = populated.get_session(session_id)
        assert detail is not None
        assert detail["club"]["external_id"] == "51"
        assert detail["session"]["name"] == "PRIMO GARA - Final A"
        assert [entry["driver"] for entry in detail["entries"]] == ["KOLYA11", "WLAD111", "TWG"]
        assert detail["entries"][0]["gap_ms"] is None
        assert detail["entries"][1]["kart"] == "2"
        assert detail["entries"][1]["best_lap_ms"] == 26788
        assert populated.get_session(4242) is None

    def test_session_laps(self, populated: Database) -> None:
        session_id = populated.list_sessions()[0]["id"]
        laps = populated.session_laps(session_id)
        assert len(laps) == 15
        first = next(lap for lap in laps if lap["driver"] == "WLAD111" and lap["lap_number"] == 1)
        assert first["time_ms"] is None
        assert first["sectors"] == [56053, 14243]
        assert first["is_best"] is False
        assert first["tags"] == []
        best = next(lap for lap in laps if lap["driver"] == "WLAD111" and lap["is_best"])
        assert best["lap_number"] == 3

    def test_list_drivers(self, populated: Database) -> None:
        drivers = {item["nickname"]: item for item in populated.list_drivers()}
        assert set(drivers) == {"KOLYA11", "TWG", "WLAD111"}
        assert drivers["WLAD111"]["external_id"] == "10001"
        assert drivers["TWG"]["external_id"] is None
        assert drivers["TWG"]["sessions_count"] == 1
        assert drivers["TWG"]["best_lap_ms"] == 25845
        assert drivers["TWG"]["last_seen"] == "2026-08-03T21:40:00"

    def test_driver_history(self, populated: Database) -> None:
        history = populated.driver_history("WLAD111")
        assert [item["date"] for item in history] == ["2026-08-03", "2026-07-20"]
        today = history[0]
        assert today["position"] == 2
        assert today["best_lap_ms"] == 26788
        assert today["laps_count"] == 5
        assert today["session_id"] == populated.list_sessions()[0]["id"]
        assert history[1]["session_id"] is None       # only known from the email table
        assert populated.driver_history("NOBODY") == []

    def test_rankings(self, populated: Database) -> None:
        session_id = populated.list_sessions()[0]["id"]
        rankings = populated.rankings(session_id)
        assert set(rankings) == {"weekly_best", "track_record"}
        assert rankings["weekly_best"] == [
            {"rank": 1, "driver": "KOLYA11", "best_lap_ms": 25640, "category": "SR5"}
        ]
        assert rankings["track_record"][0]["driver"] == "TWG"
        assert populated.rankings(4242) == {"weekly_best": [], "track_record": []}


class TestLapAnnotations:
    @pytest.fixture()
    def lap_id(self, db: Database) -> int:
        report = db.import_parsed(make_parsed("WLAD111", message_id="<a@x>"))
        laps = db.session_laps(report.session_id)
        return int(next(lap for lap in laps if lap["driver"] == "TWG" and lap["lap_number"] == 2)["id"])

    def test_add_and_remove(self, db: Database, lap_id: int) -> None:
        db.add_lap_tag(lap_id, "traffic", note="stuck behind KOLYA11")
        session_id = db.list_sessions()[0]["id"]
        tags = db.lap_tags(session_id)
        assert list(tags) == [lap_id]
        assert tags[lap_id][0]["tag"] == "traffic"
        assert tags[lap_id][0]["note"] == "stuck behind KOLYA11"
        assert tags[lap_id][0]["source"] == "manual"

        lap = next(item for item in db.session_laps(session_id) if item["id"] == lap_id)
        assert [tag["tag"] for tag in lap["tags"]] == ["traffic"]

        db.remove_lap_tag(lap_id, "traffic")
        assert db.lap_tags(session_id) == {}
        db.remove_lap_tag(lap_id, "traffic")  # removing twice is not an error

    def test_add_is_idempotent_and_updates_the_note(self, db: Database, lap_id: int) -> None:
        db.add_lap_tag(lap_id, "pit")
        db.add_lap_tag(lap_id, "PIT ", note="came in")
        rows = db.connection.execute(
            "SELECT tag, note FROM lap_annotation WHERE lap_id = ?", (lap_id,)
        ).fetchall()
        assert [tuple(row) for row in rows] == [("pit", "came in")]

    def test_unknown_tag_and_unknown_lap_are_rejected(self, db: Database, lap_id: int) -> None:
        with pytest.raises(UnknownTagError):
            db.add_lap_tag(lap_id, "nonsense")
        with pytest.raises(UnknownLapError):
            db.add_lap_tag(999_999, "pit")
        assert db.connection.execute("SELECT COUNT(*) FROM lap_annotation").fetchone()[0] == 0

    def test_import_never_touches_annotations(self, db: Database, lap_id: int) -> None:
        db.add_lap_tag(lap_id, "outlier", note="kept by hand")
        db.import_parsed(make_parsed("WLAD111", message_id="<a@x>"))   # duplicate
        db.import_parsed(make_parsed("TWG", message_id="<b@x>"))       # merge
        rows = db.connection.execute(
            "SELECT lap_id, tag, note FROM lap_annotation"
        ).fetchall()
        assert [tuple(row) for row in rows] == [(lap_id, "outlier", "kept by hand")]


# --------------------------------------------------------------------------- #
# The real reference email
# --------------------------------------------------------------------------- #


class TestRealEmail:
    @pytest.fixture()
    def parsed_real(self) -> Any:
        from karting.parsing import parse_email_file

        candidates = sorted(REPO_ROOT.glob("*.eml"))
        assert candidates, "the reference .eml must be in the repository root"
        path = candidates[0]
        # No `except: skip` here: a parser regression on the reference email is
        # a failure of this suite, not a reason to report it green.
        return parse_email_file(path), path

    def test_import_reference_email(self, db: Database, parsed_real: Any) -> None:
        parsed, path = parsed_real
        raw = path.read_bytes()
        report = db.import_parsed(parsed, raw_bytes=raw)

        assert report.session_created is True
        assert report.already_imported is False
        assert report.inserted_entries == len(parsed.entries) > 0
        assert report.inserted_laps == len(parsed.laps) > 0
        assert report.conflicts == []

        detail = db.get_session(report.session_id)
        assert detail is not None
        assert detail["session"]["name"]
        assert len(detail["entries"]) == len(parsed.entries)

        laps = db.session_laps(report.session_id)
        assert len(laps) == len(parsed.laps)
        assert any(lap["sectors"] for lap in laps)

        digest = hashlib.sha256(raw).hexdigest()
        assert (db.raw_dir / f"{digest}.eml").exists()

    def test_reference_email_reimport_is_idempotent(self, db: Database, parsed_real: Any) -> None:
        parsed, path = parsed_real
        raw = path.read_bytes()
        first = db.import_parsed(parsed, raw_bytes=raw)
        before = counts(db)

        again = db.import_parsed(parsed, raw_bytes=raw)
        assert again.already_imported is True
        assert again.session_id == first.session_id
        assert counts(db) == before


# --------------------------------------------------------------------------- #
# Regression tests for reported defects
# --------------------------------------------------------------------------- #


class TestDriverIdentity:
    """SPEC §1.8: the Apex `client` id identifies a driver, the nickname does not."""

    def test_rename_keeping_the_external_id_updates_one_driver(self, db: Database) -> None:
        first = db.import_parsed(make_parsed("WLAD111", message_id="<a@x>"))
        before = counts(db)

        renamed = make_parsed("WLAD111", message_id="<b@x>")
        for holder in (*renamed.entries, *renamed.laps):
            if holder.driver.nickname == "WLAD111":
                holder.driver.nickname = "WLAD222"
        renamed.provenance.recipient_nickname = "WLAD222"
        report = db.import_parsed(renamed)

        after = counts(db)
        assert report.session_id == first.session_id
        # One driver, one entry, one set of laps -- no split identity.
        for table in ("driver", "session_entry", "lap", "lap_sector", "history_entry"):
            assert after[table] == before[table], table
        rows = db.connection.execute(
            "SELECT nickname, external_id FROM driver WHERE external_id = ?",
            (EXTERNAL_IDS["WLAD111"],),
        ).fetchall()
        assert [tuple(row) for row in rows] == [("WLAD222", EXTERNAL_IDS["WLAD111"])]
        assert any("renamed from WLAD111 to WLAD222" in item for item in report.conflicts)

    def test_rename_is_recorded_in_the_conflict_table(self, db: Database) -> None:
        db.import_parsed(make_parsed("WLAD111", message_id="<a@x>"))
        renamed = make_parsed("WLAD111", message_id="<b@x>")
        for holder in (*renamed.entries, *renamed.laps):
            if holder.driver.nickname == "WLAD111":
                holder.driver.nickname = "WLAD222"
        renamed.provenance.recipient_nickname = "WLAD222"
        db.import_parsed(renamed)

        stored = db.connection.execute(
            "SELECT entity, field, stored_value, incoming_value FROM import_conflict "
            "WHERE field = 'nickname'"
        ).fetchall()
        assert [tuple(row) for row in stored] == [("driver", "nickname", "WLAD111", "WLAD222")]

    def test_a_new_nickname_without_external_id_is_a_new_driver(self, db: Database) -> None:
        db.import_parsed(make_parsed("WLAD111", message_id="<a@x>"))
        extra = make_parsed("WLAD111", message_id="<b@x>")
        for holder in (*extra.entries, *extra.laps):
            if holder.driver.nickname == "TWG":
                holder.driver.nickname = "NEWCOMER"
        db.import_parsed(extra)
        nicknames = {
            row["nickname"] for row in db.connection.execute("SELECT nickname FROM driver")
        }
        assert {"WLAD111", "TWG", "NEWCOMER"} <= nicknames


class TestBestLapFlag:
    def test_disagreement_about_the_best_lap_is_reported_not_applied(self, db: Database) -> None:
        first = db.import_parsed(make_parsed("WLAD111", message_id="<a@x>"))
        other = make_parsed("WLAD111", message_id="<b@x>")
        for lap in other.laps:
            if lap.driver.nickname == "KOLYA11":
                lap.is_best = lap.lap_number == 2
        report = db.import_parsed(other)

        marked = db.connection.execute(
            "SELECT l.lap_number FROM lap l JOIN driver d ON d.id = l.driver_id "
            "WHERE l.session_id = ? AND d.nickname = 'KOLYA11' AND l.is_best = 1",
            (first.session_id,),
        ).fetchall()
        assert [row["lap_number"] for row in marked] == [4]  # the stored flag wins
        assert any("is_best" in item for item in report.conflicts)

    def test_a_driver_never_ends_up_with_two_best_laps(self, db: Database) -> None:
        parsed = make_parsed("WLAD111", message_id="<a@x>")
        for lap in parsed.laps:
            if lap.driver.nickname == "TWG":
                lap.is_best = lap.lap_number in {2, 4}
        report = db.import_parsed(parsed)
        rows = db.connection.execute(
            "SELECT driver_id, COUNT(*) AS n FROM lap WHERE is_best = 1 GROUP BY driver_id"
        ).fetchall()
        assert all(row["n"] == 1 for row in rows)
        assert any("is_best" in item for item in report.conflicts)


class TestDuplicateKeysInsideOneEmail:
    def test_repeated_lap_number_is_merged_and_reported(self, db: Database) -> None:
        parsed = make_parsed("WLAD111", message_id="<a@x>")
        original = len(parsed.laps)
        parsed.laps.append(Lap(driver=Driver("KOLYA11"), lap_number=2, time_ms=99_999))

        report = db.import_parsed(parsed)

        assert report.inserted_laps == original  # the repeat did not create a row
        stored = db.connection.execute(
            "SELECT time_ms FROM lap l JOIN driver d ON d.id = l.driver_id "
            "WHERE d.nickname = 'KOLYA11' AND l.lap_number = 2"
        ).fetchone()
        assert stored["time_ms"] == LAP_TIMES["KOLYA11"][1]  # first occurrence kept
        assert any("appears twice" in item for item in report.conflicts)

    def test_repeated_classification_row_is_merged_and_reported(self, db: Database) -> None:
        parsed = make_parsed("WLAD111", message_id="<a@x>")
        original = len(parsed.entries)
        parsed.entries.append(SessionEntry(driver=Driver("KOLYA11"), position=99))

        report = db.import_parsed(parsed)

        assert report.inserted_entries == original
        stored = db.connection.execute(
            "SELECT position FROM session_entry e JOIN driver d ON d.id = e.driver_id "
            "WHERE d.nickname = 'KOLYA11'"
        ).fetchone()
        assert stored["position"] == 1
        assert any("appears twice" in item for item in report.conflicts)


class TestProvenanceRecovery:
    def test_a_deleted_session_can_be_rebuilt_from_the_same_email(self, db: Database) -> None:
        parsed = make_parsed("WLAD111", message_id="<a@x>")
        first = db.import_parsed(parsed)
        laps_before = counts(db)["lap"]

        db.connection.execute("DELETE FROM session WHERE id = ?", (first.session_id,))
        assert counts(db)["lap"] == 0

        again = db.import_parsed(make_parsed("WLAD111", message_id="<a@x>"))
        assert again.already_imported is False
        assert again.session_created is True
        assert counts(db)["lap"] == laps_before
        assert counts(db)["email_import"] == 1  # the orphaned row was replaced

    def test_input_without_headers_is_deduplicated_by_content(self, db: Database) -> None:
        def anonymous() -> ParsedEmail:
            parsed = make_parsed("WLAD111", message_id="<a@x>")
            parsed.provenance = Provenance()  # what `parse_html` produces
            return parsed

        first = db.import_parsed(anonymous())
        second = db.import_parsed(anonymous())
        third = db.import_parsed(anonymous())

        assert (first.already_imported, second.already_imported) == (False, True)
        assert third.already_imported is True
        assert counts(db)["email_import"] == 1

    def test_a_later_upload_backfills_the_file_digest_and_the_raw_copy(
        self, db: Database
    ) -> None:
        parsed = make_parsed("WLAD111", message_id="<a@x>")
        parsed.provenance.sha256 = None
        db.import_parsed(parsed)  # no bytes: no digest, no copy
        stored = db.connection.execute("SELECT sha256, raw_path FROM email_import").fetchone()
        assert (stored["sha256"], stored["raw_path"]) == (None, None)

        raw = b"the original bytes of this email"
        digest = hashlib.sha256(raw).hexdigest()
        with_bytes = make_parsed("WLAD111", message_id="<a@x>")
        with_bytes.provenance.sha256 = None
        report = db.import_parsed(with_bytes, raw_bytes=raw)

        assert report.already_imported is True
        stored = db.connection.execute("SELECT sha256, raw_path FROM email_import").fetchone()
        assert stored["sha256"] == digest
        assert db.raw_dir is not None
        assert (db.raw_dir / f"{digest}.eml").read_bytes() == raw


class TestSessionIdentity:
    def _headerless(self, message_id: str) -> ParsedEmail:
        return ParsedEmail(
            club=Club(name=""),
            session=Session(name="", started_at=None),
            provenance=Provenance(message_id=message_id),
        )

    def test_an_email_without_a_session_identity_is_refused(self, db: Database) -> None:
        with pytest.raises(NoSessionIdentityError):
            db.import_parsed(self._headerless("<junk1@x>"))
        assert counts(db)["session"] == 0

    def test_two_unrelated_headerless_emails_cannot_merge(self, db: Database) -> None:
        for message_id in ("<junk1@x>", "<junk2@x>"):
            with pytest.raises(NoSessionIdentityError):
                db.import_parsed(self._headerless(message_id))
        assert counts(db) == dict.fromkeys(TABLES, 0)

    def test_a_start_time_alone_is_enough_identity(self, db: Database) -> None:
        parsed = ParsedEmail(
            club=Club(name="PRIMO KARTING"),
            session=Session(name="", started_at=SESSION_STARTED_AT),
            provenance=Provenance(message_id="<dated@x>"),
        )
        assert db.import_parsed(parsed).session_created is True


class TestOutOfRangeIds:
    """A row id wider than a SQLite INTEGER means "not found", never a crash."""

    HUGE = 99_999_999_999_999_999_999_999

    def test_readers_answer_empty_instead_of_raising(self, db: Database) -> None:
        db.import_parsed(make_parsed("WLAD111", message_id="<a@x>"))
        assert db.get_session(self.HUGE) is None
        assert db.get_session(-self.HUGE) is None
        assert db.session_laps(self.HUGE) == []
        assert db.rankings(self.HUGE) == {"weekly_best": [], "track_record": []}
        assert db.lap_tags(self.HUGE) == {}

    def test_tagging_an_impossible_lap_is_a_clean_error(self, db: Database) -> None:
        with pytest.raises(UnknownLapError):
            db.add_lap_tag(self.HUGE, "pit")
        db.remove_lap_tag(self.HUGE, "pit")  # removing a missing tag stays a no-op


class TestThreadSafety:
    def test_a_connection_survives_being_used_from_another_thread(self, db: Database) -> None:
        """ASGI runs a sync dependency and its endpoint on different threads."""
        db.import_parsed(make_parsed("WLAD111", message_id="<a@x>"))
        results: list[int] = []
        errors: list[BaseException] = []

        def read() -> None:
            try:
                results.append(len(db.list_sessions()))
            except BaseException as error:  # noqa: BLE001 - re-raised in the assertion
                errors.append(error)

        threads = [threading.Thread(target=read) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert errors == []
        assert results == [1] * 8

    def test_concurrent_imports_do_not_interleave(self, tmp_path: Path) -> None:
        with open_db(tmp_path / "pace.db", raw_dir=tmp_path / "raw") as database:
            errors: list[BaseException] = []

            def write(index: int) -> None:
                try:
                    database.import_parsed(make_parsed("WLAD111", message_id=f"<m{index}@x>"))
                except BaseException as error:  # noqa: BLE001
                    errors.append(error)

            threads = [threading.Thread(target=write, args=(index,)) for index in range(6)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            assert errors == []
            assert counts(database)["session"] == 1
            assert counts(database)["lap"] == sum(len(v) for v in LAP_TIMES.values())


class TestInMemoryDatabase:
    def test_it_leaves_no_files_in_the_working_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        with open_db(":memory:") as database:
            report = database.import_parsed(
                make_parsed("WLAD111", message_id="<a@x>"), raw_bytes=b"raw email bytes"
            )
        assert report.session_created is True
        assert database.raw_dir is None
        assert list(tmp_path.iterdir()) == []

    def test_an_explicit_raw_dir_is_still_honoured(self, tmp_path: Path) -> None:
        with open_db(":memory:", raw_dir=tmp_path / "raw") as database:
            database.import_parsed(
                make_parsed("WLAD111", message_id="<a@x>"), raw_bytes=b"raw email bytes"
            )
        assert list((tmp_path / "raw").iterdir())


class TestRankingOnlyDrivers:
    def test_their_best_lap_comes_from_the_leaderboard(self, db: Database) -> None:
        parsed = make_parsed("WLAD111", message_id="<a@x>")
        parsed.rankings.append(
            RankingEntry(
                kind=RankingKind.TRACK_RECORD,
                rank=2,
                driver=Driver("SHINSILAXX"),
                best_lap_ms=26701,
                category="SR5",
            )
        )
        db.import_parsed(parsed)

        row = next(item for item in db.list_drivers() if item["nickname"] == "SHINSILAXX")
        assert row["sessions_count"] == 0
        assert row["best_lap_ms"] == 26701
        assert row["source"] == "ranking"
        racer = next(item for item in db.list_drivers() if item["nickname"] == "KOLYA11")
        assert racer["source"] == "session"
