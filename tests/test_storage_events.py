"""Storage tests for automatic joker/pit annotations (SPEC §10.3).

Covered here and nowhere else:

* the schema v3 migration -- a database written before ``lap_annotation.source``
  existed opens, upgrades and keeps every row as a *manual* annotation;
* automatic tagging at the end of a successful import (6 pits, 5 jokers on the
  reference race) and its idempotency;
* the priority rule: one manual annotation hides every automatic annotation of
  that lap, and a human decision survives any number of detector runs;
* the effective tag set being the same for ``session_laps`` and for the
  statistics layer.

The reference ``.eml`` of the repository root is the input: joker/pit detection
is a domain rule about real races, and synthetic five-lap fixtures cannot
express it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from karting.models import LapTag
from karting.storage import (
    AUTO_SOURCE,
    MANUAL_SOURCE,
    OVERRIDE_TAG,
    SCHEMA_VERSION,
    Database,
    open_db,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

#: SPEC §10.2: every driver pits, only five of the six jokers are detectable.
EXPECTED_PITS = 6
EXPECTED_JOKERS = 5
DRIVER_WITHOUT_JOKER = "ИГОРЬ53"
#: The reference race, as detected from the lap chart (see SPEC §10).
JOKER_LAPS = {"KOLYA11": 19, "WLAD111": 3, "TWG": 14, "DENISENKO": 15, "PHREEMAN": 5}
PIT_LAPS = {
    "KOLYA11": 17,
    "WLAD111": 19,
    "TWG": 3,
    "DENISENKO": 5,
    "PHREEMAN": 18,
    "ИГОРЬ53": 14,
}

# The lap_annotation table exactly as schema v2 wrote it: no `source` column and
# a UNIQUE(lap_id, tag) key.  Kept verbatim so the migration is tested against
# the real predecessor rather than against a paraphrase of it.
LEGACY_ANNOTATION_DDL = """
CREATE TABLE lap_annotation (
    id         INTEGER PRIMARY KEY,
    lap_id     INTEGER NOT NULL REFERENCES lap (id) ON DELETE CASCADE,
    tag        TEXT NOT NULL,
    note       TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (lap_id, tag)
);
CREATE INDEX ix_lap_annotation_lap ON lap_annotation (lap_id);
"""


# --------------------------------------------------------------------------- #
# Fixtures and helpers
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def reference_email() -> tuple[Any, bytes]:
    """The parsed reference email and its raw bytes."""
    import karting.parsing as parsing
    candidates = sorted(REPO_ROOT.glob("*.eml"))
    if not candidates:
        pytest.skip("no reference .eml in the repository root")
    path = candidates[0]
    return parsing.parse_email_file(path), path.read_bytes()


@pytest.fixture()
def db(tmp_path: Path) -> Iterator[Database]:
    with open_db(tmp_path / "pace.db", raw_dir=tmp_path / "raw_emails") as database:
        yield database


@pytest.fixture()
def session_id(db: Database, reference_email: tuple[Any, bytes]) -> int:
    """The reference race, imported once (auto-tagging included)."""
    parsed, raw = reference_email
    return db.import_parsed(parsed, raw_bytes=raw).session_id


def annotation_rows(db: Database) -> list[tuple[Any, ...]]:
    """Every annotation row, ids and timestamps included, in a stable order."""
    return [
        tuple(row)
        for row in db.connection.execute(
            "SELECT id, lap_id, tag, note, created_at, source FROM lap_annotation "
            "ORDER BY id"
        )
    ]


def lap_id_of(db: Database, session_id: int, driver: str, lap_number: int) -> int:
    """Row id of one lap of one driver."""
    for lap in db.session_laps(session_id):
        if lap["driver"] == driver and lap["lap_number"] == lap_number:
            return int(lap["id"])
    raise AssertionError(f"lap {lap_number} of {driver} is missing")


def tags_of(rows: Sequence[Mapping[str, Any]]) -> set[str]:
    return {str(row["tag"]) for row in rows}


def lap_row(db: Database, session_id: int, lap_id: int) -> Mapping[str, Any]:
    return next(lap for lap in db.session_laps(session_id) if int(lap["id"]) == lap_id)


def tagged(db: Database, session_id: int, tag: str) -> dict[str, int]:
    """``{driver: lap_number}`` of every lap whose effective tags contain ``tag``."""
    return {
        str(lap["driver"]): int(lap["lap_number"])
        for lap in db.session_laps(session_id)
        if tag in tags_of(lap["tags"])
    }


# --------------------------------------------------------------------------- #
# Schema migration
# --------------------------------------------------------------------------- #


class TestMigration:
    """A database written before SPEC §10.3 opens, upgrades and keeps its rows."""

    def _legacy_db(self, path: Path) -> int:
        """Create a v2 database with one lap and two hand-made annotations."""
        with open_db(path) as fresh:
            fresh.connection.executescript(
                """
                DROP TABLE lap_annotation;
                """
                + LEGACY_ANNOTATION_DDL
            )
            fresh.connection.execute(
                "INSERT INTO club (id, name) VALUES (1, 'PRIMO KARTING')"
            )
            fresh.connection.execute("INSERT INTO driver (id, nickname) VALUES (1, 'WLAD111')")
            fresh.connection.execute(
                "INSERT INTO session (id, club_id, name, created_at) "
                "VALUES (1, 1, 'PRIMO GARA - Final A', '2026-08-03T00:00:00Z')"
            )
            fresh.connection.execute(
                "INSERT INTO lap (id, session_id, driver_id, lap_number, time_ms) "
                "VALUES (7, 1, 1, 3, 26788)"
            )
            fresh.connection.executemany(
                "INSERT INTO lap_annotation (id, lap_id, tag, note, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (1, 7, "traffic", "boxed in", "2026-08-03T10:00:00Z"),
                    (2, 7, "incident", None, "2026-08-03T10:01:00Z"),
                ],
            )
            fresh.connection.execute("PRAGMA user_version = 2")
        return 7

    def test_old_database_opens_and_upgrades(self, tmp_path: Path) -> None:
        path = tmp_path / "legacy.db"
        lap_id = self._legacy_db(path)

        with open_db(path) as upgraded:
            assert upgraded.connection.execute("PRAGMA user_version").fetchone()[0] == (
                SCHEMA_VERSION
            )
            rows = annotation_rows(upgraded)
            # Nothing lost, nothing renumbered, everything is a human decision.
            assert rows == [
                (1, lap_id, "traffic", "boxed in", "2026-08-03T10:00:00Z", MANUAL_SOURCE),
                (2, lap_id, "incident", None, "2026-08-03T10:01:00Z", MANUAL_SOURCE),
            ]
            # The new key admits an automatic row next to the manual one...
            upgraded.connection.execute(
                "INSERT INTO lap_annotation (lap_id, tag, created_at, source) "
                "VALUES (?, 'traffic', '2026-08-03T11:00:00Z', 'auto')",
                (lap_id,),
            )
            # ... but still not a second row of the same source.
            with pytest.raises(sqlite3.IntegrityError):
                upgraded.connection.execute(
                    "INSERT INTO lap_annotation (lap_id, tag, created_at, source) "
                    "VALUES (?, 'traffic', '2026-08-03T12:00:00Z', 'manual')",
                    (lap_id,),
                )
            # The source vocabulary is closed.
            with pytest.raises(sqlite3.IntegrityError):
                upgraded.connection.execute(
                    "INSERT INTO lap_annotation (lap_id, tag, created_at, source) "
                    "VALUES (?, 'boost', '2026-08-03T12:00:00Z', 'guess')",
                    (lap_id,),
                )

    def test_upgraded_database_keeps_working(self, tmp_path: Path) -> None:
        path = tmp_path / "legacy.db"
        lap_id = self._legacy_db(path)
        with open_db(path) as upgraded:
            assert tags_of(upgraded.lap_tags(1)[lap_id]) == {"traffic", "incident"}
            upgraded.add_lap_tag(lap_id, "penalty", note="added after the upgrade")
            assert tags_of(upgraded.lap_tags(1)[lap_id]) == {
                "traffic",
                "incident",
                "penalty",
            }
            # Foreign keys and cascades survived the table rebuild.
            with pytest.raises(sqlite3.IntegrityError):
                upgraded.connection.execute(
                    "INSERT INTO lap_annotation (lap_id, tag, created_at) "
                    "VALUES (999999, 'pit', '2026-08-03T12:00:00Z')"
                )
            upgraded.connection.execute("DELETE FROM lap WHERE id = ?", (lap_id,))
            assert annotation_rows(upgraded) == []

    def test_migration_is_not_run_twice(self, tmp_path: Path) -> None:
        path = tmp_path / "legacy.db"
        lap_id = self._legacy_db(path)
        with open_db(path) as first:
            first.add_lap_tag(lap_id, "boost")
            before = annotation_rows(first)
        with open_db(path) as second:  # already at SCHEMA_VERSION
            assert annotation_rows(second) == before


# --------------------------------------------------------------------------- #
# Automatic tagging at import time
# --------------------------------------------------------------------------- #


class TestAutoTaggingOnImport:
    def test_import_tags_jokers_and_pits(self, db: Database, reference_email: Any) -> None:
        parsed, raw = reference_email
        report = db.import_parsed(parsed, raw_bytes=raw)

        assert report.auto_pits == EXPECTED_PITS
        assert report.auto_jokers == EXPECTED_JOKERS
        assert report.drivers_without_pit == []
        assert report.drivers_without_joker == [DRIVER_WITHOUT_JOKER]
        assert report.to_dict()["drivers_without_joker"] == [DRIVER_WITHOUT_JOKER]

        assert tagged(db, report.session_id, LapTag.JOKER.value) == JOKER_LAPS
        assert tagged(db, report.session_id, LapTag.PIT.value) == PIT_LAPS

        rows = db.connection.execute(
            "SELECT DISTINCT source FROM lap_annotation"
        ).fetchall()
        assert [tuple(row) for row in rows] == [(AUTO_SOURCE,)]
        note = db.connection.execute(
            "SELECT note FROM lap_annotation WHERE tag = 'joker' LIMIT 1"
        ).fetchone()[0]
        assert "auto-detected joker" in note

    def test_reimporting_the_same_email_changes_nothing(
        self, db: Database, reference_email: Any
    ) -> None:
        parsed, raw = reference_email
        db.import_parsed(parsed, raw_bytes=raw)
        before = annotation_rows(db)

        again = db.import_parsed(parsed, raw_bytes=raw)
        assert again.already_imported is True
        assert annotation_rows(db) == before

    def test_detect_and_tag_events_is_idempotent(self, db: Database, session_id: int) -> None:
        before = annotation_rows(db)
        assert before  # the import already tagged the session

        report = db.detect_and_tag_events(session_id)
        # Ids and timestamps of unchanged rows are preserved on purpose: an
        # unchanged verdict must leave the table byte for byte identical.
        assert annotation_rows(db) == before
        assert len(report.events) == EXPECTED_JOKERS + EXPECTED_PITS

        db.detect_and_tag_events(session_id)
        assert annotation_rows(db) == before

    def test_detection_config_is_honoured(self, db: Database, session_id: int) -> None:
        import karting.stats.events as events
        # A pit ratio no lap can reach leaves the jokers and drops every pit.
        report = db.detect_and_tag_events(
            session_id, events.EventDetectionConfig(pit_ratio=99.0)
        )
        assert report.events and all(event.kind != LapTag.PIT.value for event in report.events)
        assert tagged(db, session_id, LapTag.PIT.value) == {}
        assert tagged(db, session_id, LapTag.JOKER.value) == JOKER_LAPS
        assert sorted(report.drivers_without_pit) == sorted(PIT_LAPS)

        # The default configuration restores exactly what the import produced.
        db.detect_and_tag_events(session_id)
        assert tagged(db, session_id, LapTag.PIT.value) == PIT_LAPS

    def test_re_detecting_with_more_events_does_not_collide(
        self, db: Database, session_id: int
    ) -> None:
        """The documented reason `--detect` exists: change a threshold, re-run.

        The replacement batch reuses the row ids of the verdicts that survived
        the previous run.  Mixing those explicit ids with NULLs for the new rows
        let SQLite hand a new row an id a later row of the same batch then
        claimed -- ``UNIQUE constraint failed: lap_annotation.id`` -- and only in
        the direction that *adds* events, which nothing used to exercise.
        """
        import karting.stats.events as events

        before = len(annotation_rows(db))
        report = db.detect_and_tag_events(
            session_id, events.EventDetectionConfig(joker_ratio=0.99)
        )
        assert len(report.events) > EXPECTED_JOKERS + EXPECTED_PITS
        rows = annotation_rows(db)
        assert len(rows) > before
        assert len({row[0] for row in rows}) == len(rows)  # ids are still unique

    def test_a_threshold_round_trip_restores_the_original_tags(
        self, db: Database, session_id: int
    ) -> None:
        """default -> looser -> default leaves the table exactly as it was."""
        import karting.stats.events as events

        before = annotation_rows(db)
        db.detect_and_tag_events(session_id, events.EventDetectionConfig(joker_ratio=0.99))
        db.detect_and_tag_events(session_id, events.EventDetectionConfig(pit_ratio=99.0))
        db.detect_and_tag_events(session_id)
        after = annotation_rows(db)
        assert {(row[1], row[2], row[5]) for row in after} == {
            (row[1], row[2], row[5]) for row in before
        }
        assert tagged(db, session_id, LapTag.PIT.value) == PIT_LAPS
        assert tagged(db, session_id, LapTag.JOKER.value) == JOKER_LAPS

    def test_the_report_follows_the_classification_not_the_alphabet(
        self, db: Database, session_id: int
    ) -> None:
        """A human scanning "who has no pit" reads down the results sheet."""
        order = [str(row["driver"]) for row in db.get_session(session_id)["entries"]]
        assert order != sorted(order)  # otherwise the test proves nothing
        report = db.detect_and_tag_events(session_id)
        seen = list(dict.fromkeys(event.driver for event in report.events))
        assert seen == [name for name in order if name in set(seen)]
        assert report.drivers_without_joker == ["ИГОРЬ53"]

    def test_unknown_session_is_a_no_op(self, db: Database, session_id: int) -> None:
        before = annotation_rows(db)
        report = db.detect_and_tag_events(session_id + 1000)
        assert report.events == []
        assert annotation_rows(db) == before


# --------------------------------------------------------------------------- #
# Manual beats automatic
# --------------------------------------------------------------------------- #


class TestManualPriority:
    def test_manual_tag_hides_the_automatic_ones_of_that_lap(
        self, db: Database, session_id: int
    ) -> None:
        lap_id = lap_id_of(db, session_id, "WLAD111", JOKER_LAPS["WLAD111"])
        assert tags_of(lap_row(db, session_id, lap_id)["tags"]) == {LapTag.JOKER.value}

        db.add_lap_tag(lap_id, "traffic", note="lapped a backmarker")

        lap = lap_row(db, session_id, lap_id)
        assert tags_of(lap["tags"]) == {"traffic"}
        assert tags_of(db.lap_tags(session_id)[lap_id]) == {"traffic"}
        # The detector's proposal is still on file for the UI to show.
        assert {(row["tag"], row["source"]) for row in lap["annotations"]} == {
            ("traffic", MANUAL_SOURCE),
            (LapTag.JOKER.value, AUTO_SOURCE),
        }
        # Only that lap is affected; every other driver keeps its joker.
        assert tagged(db, session_id, LapTag.JOKER.value) == {
            driver: number for driver, number in JOKER_LAPS.items() if driver != "WLAD111"
        }

    def test_manual_tags_survive_a_detector_rerun(self, db: Database, session_id: int) -> None:
        manual_lap = lap_id_of(db, session_id, "TWG", 11)
        joker_lap = lap_id_of(db, session_id, "TWG", JOKER_LAPS["TWG"])
        db.add_lap_tag(manual_lap, "boost", note="tow from KOLYA11")
        db.add_lap_tag(joker_lap, "invalid", note="cut the chicane")

        db.detect_and_tag_events(session_id)
        db.detect_and_tag_events(session_id)

        manual = [
            (row["lap_id"], row["tag"], row["note"])
            for row in db.connection.execute(
                "SELECT lap_id, tag, note FROM lap_annotation WHERE source = ? "
                "ORDER BY lap_id, tag",
                (MANUAL_SOURCE,),
            )
        ]
        assert sorted(manual) == sorted(
            [
                (joker_lap, "invalid", "cut the chicane"),
                (manual_lap, "boost", "tow from KOLYA11"),
            ]
        )
        assert tags_of(lap_row(db, session_id, joker_lap)["tags"]) == {"invalid"}
        assert tags_of(lap_row(db, session_id, manual_lap)["tags"]) == {"boost"}
        # The rejected joker is still proposed, and still hidden.
        assert {row["source"] for row in lap_row(db, session_id, joker_lap)["annotations"]} == {
            MANUAL_SOURCE,
            AUTO_SOURCE,
        }

    def test_annotating_the_missing_joker_settles_the_report(
        self, db: Database, session_id: int
    ) -> None:
        """SPEC §10.2 invites a human to resolve a missing event -- so it must work.

        Before this, the detector never read `LapPoint.tags`, so a session the
        human had fully annotated kept reporting the same gap after every
        re-detection and the API kept calling it incomplete.
        """
        assert db.detect_and_tag_events(session_id).drivers_without_joker == ["ИГОРЬ53"]

        lap_id = lap_id_of(db, session_id, "ИГОРЬ53", 15)
        db.add_lap_tag(lap_id, LapTag.JOKER.value, note="stopwatch says so")

        report = db.detect_and_tag_events(session_id)
        assert report.drivers_without_joker == []
        assert report.drivers_without_pit == []
        assert report.drivers_with_multiple == []
        assert tagged(db, session_id, LapTag.JOKER.value)["ИГОРЬ53"] == 15
        # The verdict is the human's own row; no automatic copy is written.
        annotations = lap_row(db, session_id, lap_id)["annotations"]
        assert [row["source"] for row in annotations] == [MANUAL_SOURCE]

    def test_annotating_a_missing_pit_removes_the_proposal(
        self, db: Database, session_id: int
    ) -> None:
        import karting.stats.events as events

        strict = events.EventDetectionConfig(pit_ratio=99.0)
        assert sorted(db.detect_and_tag_events(session_id, strict).drivers_without_pit) == sorted(
            PIT_LAPS
        )
        for driver, lap_number in PIT_LAPS.items():
            db.add_lap_tag(lap_id_of(db, session_id, driver, lap_number), LapTag.PIT.value)

        report = db.detect_and_tag_events(session_id, strict)
        assert report.drivers_without_pit == []
        assert report.pit_candidates == []
        assert tagged(db, session_id, LapTag.PIT.value) == PIT_LAPS

    def test_rejecting_an_automatic_tag_is_permanent(
        self, db: Database, session_id: int
    ) -> None:
        lap_id = lap_id_of(db, session_id, "PHREEMAN", JOKER_LAPS["PHREEMAN"])
        db.remove_lap_tag(lap_id, LapTag.JOKER.value)

        assert tags_of(lap_row(db, session_id, lap_id)["tags"]) == {OVERRIDE_TAG}
        db.detect_and_tag_events(session_id)
        assert tags_of(lap_row(db, session_id, lap_id)["tags"]) == {OVERRIDE_TAG}
        assert "PHREEMAN" not in tagged(db, session_id, LapTag.JOKER.value)

        # And the human can change their mind again.
        db.remove_lap_tag(lap_id, OVERRIDE_TAG)
        assert tags_of(lap_row(db, session_id, lap_id)["tags"]) == {LapTag.JOKER.value}

    def test_reject_auto_tags_and_manual_override(self, db: Database, session_id: int) -> None:
        lap_id = lap_id_of(db, session_id, "DENISENKO", PIT_LAPS["DENISENKO"])
        db.reject_auto_tags(lap_id, note="the pit stop was on the next lap")
        assert tags_of(lap_row(db, session_id, lap_id)["tags"]) == {OVERRIDE_TAG}

        neighbour = lap_id_of(db, session_id, "DENISENKO", PIT_LAPS["DENISENKO"] + 1)
        db.set_manual_tags(neighbour, [LapTag.PIT.value], note="stopwatch says so")
        db.detect_and_tag_events(session_id)
        assert tagged(db, session_id, LapTag.PIT.value)["DENISENKO"] == (
            PIT_LAPS["DENISENKO"] + 1
        )

        # An empty manual set hands the lap back to the detector.
        db.set_manual_tags(neighbour, [])
        db.set_manual_tags(lap_id, [])
        assert tagged(db, session_id, LapTag.PIT.value)["DENISENKO"] == PIT_LAPS["DENISENKO"]

    def test_removing_an_unrelated_manual_tag_restores_the_proposal(
        self, db: Database, session_id: int
    ) -> None:
        lap_id = lap_id_of(db, session_id, "KOLYA11", PIT_LAPS["KOLYA11"])
        db.add_lap_tag(lap_id, "incident")
        assert tags_of(lap_row(db, session_id, lap_id)["tags"]) == {"incident"}

        db.remove_lap_tag(lap_id, "incident")
        # Nothing was said about the pit stop itself, so the detector rules again.
        assert tags_of(lap_row(db, session_id, lap_id)["tags"]) == {LapTag.PIT.value}

    def test_manual_annotations_survive_a_second_email_of_the_same_race(
        self, db: Database, session_id: int, reference_email: Any
    ) -> None:
        parsed, _ = reference_email
        lap_id = lap_id_of(db, session_id, "WLAD111", 13)
        db.add_lap_tag(lap_id, "traffic", note="kept by hand")

        db.import_parsed(parsed)  # the very same email again
        rows = [
            (row["lap_id"], row["tag"], row["note"])
            for row in db.connection.execute(
                "SELECT lap_id, tag, note FROM lap_annotation WHERE source = ?",
                (MANUAL_SOURCE,),
            )
        ]
        assert rows == [(lap_id, "traffic", "kept by hand")]


# --------------------------------------------------------------------------- #
# One picture for every layer
# --------------------------------------------------------------------------- #


class TestEffectiveTagsAreShared:
    def test_session_laps_matches_lap_tags(self, db: Database, session_id: int) -> None:
        lap_id = lap_id_of(db, session_id, "TWG", JOKER_LAPS["TWG"])
        db.add_lap_tag(lap_id, "outlier", note="under investigation")

        effective = db.lap_tags(session_id)
        from_laps = {
            int(lap["id"]): tags_of(lap["tags"])
            for lap in db.session_laps(session_id)
            if lap["tags"]
        }
        assert from_laps == {key: tags_of(rows) for key, rows in effective.items()}
        # The raw view is strictly richer than the effective one.
        raw = db.lap_annotations(session_id)
        assert set(raw) == set(effective)
        assert sum(len(rows) for rows in raw.values()) == sum(
            len(rows) for rows in effective.values()
        ) + 1

    def test_statistics_see_the_same_tags(self, db: Database, session_id: int) -> None:
        import karting.stats as stats
        laps = db.session_laps(session_id)
        points = [
            stats.LapPoint(
                lap_number=int(lap["lap_number"]),
                time_ms=lap["time_ms"],
                sectors=tuple(lap["sectors"]),
                tags=tuple(tags_of(lap["tags"])),
            )
            for lap in laps
            if lap["driver"] == "WLAD111"
        ]
        flt = stats.LapFilter(
            exclude_tags=frozenset({LapTag.JOKER.value, LapTag.PIT.value}),
            drop_slow_outliers=False,
        )
        excluded = {
            flag.lap_number: flag.reason
            for flag in stats.classify_laps(points, flt)
            if not flag.used
        }
        assert excluded[JOKER_LAPS["WLAD111"]] == f"tag:{LapTag.JOKER.value}"
        assert excluded[PIT_LAPS["WLAD111"]] == f"tag:{LapTag.PIT.value}"

        # The official best lap of WLAD111 *is* the joker lap: the clean best
        # must therefore be slower, which is the whole point of SPEC §10.4.
        official = min(
            lap["time_ms"]
            for lap in laps
            if lap["driver"] == "WLAD111" and lap["time_ms"] is not None
        )
        clean = min(
            lap["time_ms"]
            for lap in laps
            if lap["driver"] == "WLAD111"
            and lap["time_ms"] is not None
            and int(lap["lap_number"]) not in excluded
        )
        assert official == 26788
        assert clean > official

    def test_manual_override_reaches_the_statistics(
        self, db: Database, session_id: int
    ) -> None:
        import karting.stats as stats
        joker_lap = JOKER_LAPS["KOLYA11"]
        flt = stats.LapFilter(
            exclude_tags=frozenset({LapTag.JOKER.value}), drop_slow_outliers=False
        )

        def flags_of(driver: str) -> dict[int, Any]:
            points = [
                stats.LapPoint(
                    lap_number=int(lap["lap_number"]),
                    time_ms=lap["time_ms"],
                    tags=tuple(tags_of(lap["tags"])),
                )
                for lap in db.session_laps(session_id)
                if lap["driver"] == driver
            ]
            return {flag.lap_number: flag for flag in stats.classify_laps(points, flt)}

        assert flags_of("KOLYA11")[joker_lap].reason == f"tag:{LapTag.JOKER.value}"

        lap_id = lap_id_of(db, session_id, "KOLYA11", joker_lap)
        db.reject_auto_tags(lap_id, note="checked on video: no shortcut")

        assert flags_of("KOLYA11")[joker_lap].used is True
