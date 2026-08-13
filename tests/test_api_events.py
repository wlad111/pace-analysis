"""Joker / pit endpoints and the clean-vs-official best lap (SPEC §10).

Every test runs the real stack -- the reference Apex Timing email is imported
into a throw-away SQLite file, then parsed, tagged and served over HTTP -- so
the numbers asserted here are the numbers of the reference race, not fixtures.

The domain rule under test: each driver takes one joker lap (~1.9 s faster) and
one pit stop (~13 s slower), neither of which is pace, and the "Best lap" column
of the email is the joker lap for five of the six drivers.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:  # allow a bare `pytest` run from anywhere
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from karting.api.app import create_app  # noqa: E402

_EML_CANDIDATES = sorted(PROJECT_ROOT.glob("*.eml"))
if not _EML_CANDIDATES:  # pragma: no cover - the fixture email ships with the repo
    pytest.skip("no .eml sample in the repository root", allow_module_level=True)
EML_PATH = _EML_CANDIDATES[0]
EML_BYTES = EML_PATH.read_bytes()
EML_UPLOAD_NAME = "final_a.eml"

DRIVERS = ("KOLYA11", "WLAD111", "TWG", "DENISENKO", "PHREEMAN", "ИГОРЬ53")
#: The five drivers whose official best lap is their joker lap (SPEC §10.4).
JOKER_BEST_DRIVERS = ("KOLYA11", "WLAD111", "TWG", "DENISENKO", "PHREEMAN")
#: Of those, the four whose joker gains the full ~1.9 s over their clean best.
FULL_GAIN_DRIVERS = ("KOLYA11", "TWG", "DENISENKO", "PHREEMAN")
#: Detected joker / pit lap of every driver of the reference race.
JOKER_LAPS = {"KOLYA11": 19, "WLAD111": 3, "TWG": 14, "DENISENKO": 15, "PHREEMAN": 5}
PIT_LAPS = {"KOLYA11": 17, "WLAD111": 19, "TWG": 3, "DENISENKO": 5, "PHREEMAN": 18, "ИГОРЬ53": 14}
#: Official "Best lap" of the classification, i.e. what the email advertises.
OFFICIAL_BESTS = {
    "KOLYA11": 26012,
    "WLAD111": 26788,
    "TWG": 25845,
    "DENISENKO": 26341,
    "PHREEMAN": 26359,
    "ИГОРЬ53": 28380,
}


# --------------------------------------------------------------------------- #
# Fixtures and helpers
# --------------------------------------------------------------------------- #


@pytest.fixture()
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the application at a private database file."""
    path = tmp_path / "pace.db"
    monkeypatch.setenv("PACE_DB", str(path))
    return path


@pytest.fixture()
def client(db_path: Path) -> Iterator[TestClient]:
    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture()
def session_id(client: TestClient) -> int:
    """Import the reference email once and return the created session id."""
    response = client.post(
        "/api/imports",
        files=[("files", (EML_UPLOAD_NAME, EML_BYTES, "message/rfc822"))],
    )
    assert response.status_code == 200, response.text
    report = response.json()[0]
    assert report["status"] == "imported", report
    return int(report["session_id"])


def as_json(response: Any) -> Any:
    """The decoded body, proving it is finite, plain JSON on the way."""
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/json")
    payload = response.json()
    json.dumps(payload, allow_nan=False, ensure_ascii=False)  # no NaN, no exotic types
    return payload


def events_of(payload: Mapping[str, Any], kind: str) -> dict[str, Mapping[str, Any]]:
    """Detected events of one kind, keyed by driver."""
    return {event["driver"]: event for event in payload["events"] if event["kind"] == kind}


def stats_rows(client: TestClient, session_id: int, **params: Any) -> dict[str, Mapping[str, Any]]:
    payload = as_json(client.get(f"/api/sessions/{session_id}/stats", params=params))
    return {row["driver"]: row for row in payload["drivers"]}


def lap_of(client: TestClient, session_id: int, driver: str, lap_number: int) -> Mapping[str, Any]:
    detail = as_json(client.get(f"/api/sessions/{session_id}"))
    for lap in detail["laps"]:
        if lap["driver"] == driver and lap["lap_number"] == lap_number:
            return lap
    pytest.fail(f"lap {lap_number} of {driver} is missing from session {session_id}")


def run_cli(*args: str, database: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "karting.cli", *args],
        cwd=PROJECT_ROOT,
        env={**os.environ, "PACE_DB": str(database)},
        capture_output=True,
        text=True,
        check=False,
    )


# --------------------------------------------------------------------------- #
# POST /api/imports -- auto-tagging happens with the import (SPEC §10.3)
# --------------------------------------------------------------------------- #


def test_the_import_report_says_how_many_jokers_and_pits_were_tagged(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/imports",
        files=[("files", (EML_UPLOAD_NAME, EML_BYTES, "message/rfc822"))],
    )
    report = as_json(response)[0]

    assert report["status"] == "imported"
    assert report["auto_jokers"] == 5
    assert report["auto_pits"] == 6
    assert report["drivers_without_joker"] == ["ИГОРЬ53"]
    assert report["drivers_without_pit"] == []
    assert "Размечено джокеров: 5, питов: 6." in report["detail"]


def test_re_importing_the_same_email_tags_nothing_again(client: TestClient) -> None:
    """Idempotence: the second import writes nothing, so it claims nothing."""
    files = [("files", (EML_UPLOAD_NAME, EML_BYTES, "message/rfc822"))]
    as_json(client.post("/api/imports", files=files))
    again = as_json(client.post("/api/imports", files=files))[0]

    assert again["status"] == "already_imported"
    assert again["auto_jokers"] == 0 and again["auto_pits"] == 0
    assert "Tagged" not in again["detail"]


# --------------------------------------------------------------------------- #
# GET /api/sessions/{id}/events
# --------------------------------------------------------------------------- #


def test_events_report_matches_the_reference_race(client: TestClient, session_id: int) -> None:
    payload = as_json(client.get(f"/api/sessions/{session_id}/events"))

    assert payload["session_id"] == session_id
    assert payload["config"] == {
        "pit_ratio": 1.25,
        "joker_ratio": 0.97,
        "one_per_driver": True,
        "require_single_sector": True,
        "skip_first_lap": True,
    }
    assert payload["persisted"] is False

    jokers, pits = events_of(payload, "joker"), events_of(payload, "pit")
    assert set(jokers) == set(JOKER_LAPS)
    assert set(pits) == set(PIT_LAPS)
    assert {driver: event["lap_number"] for driver, event in jokers.items()} == JOKER_LAPS
    assert {driver: event["lap_number"] for driver, event in pits.items()} == PIT_LAPS

    # SPEC §10.2: 6 pits out of 6, 5 jokers out of 6 -- ИГОРЬ53 lost his.
    assert payload["counts"] == {"drivers": 6, "joker": 5, "pit": 6}
    assert payload["drivers_without_joker"] == ["ИГОРЬ53"]
    assert payload["drivers_without_pit"] == []
    assert payload["drivers_with_multiple"] == []
    assert payload["complete"] is False


def test_every_event_carries_the_fields_of_the_contract(
    client: TestClient, session_id: int
) -> None:
    payload = as_json(client.get(f"/api/sessions/{session_id}/events"))
    for event in payload["events"]:
        assert set(event) >= {
            "driver", "lap_number", "kind", "ratio", "delta_ms",
            "sector_index", "confidence", "note", "lap_id", "applied",
        }
        assert event["kind"] in {"joker", "pit"}
        assert 0.0 <= event["confidence"] <= 1.0
        assert event["note"]
        assert isinstance(event["lap_id"], int)
        assert event["applied"] is True  # the import tags the session already
        assert event["overridden_by_manual"] is False
        if event["kind"] == "joker":
            assert event["delta_ms"] < 0 and event["ratio"] <= 0.97
        else:
            assert event["delta_ms"] > 0 and event["ratio"] >= 1.25

    # Sectors are only known for the recipient of the email; there the anomaly
    # sits in one sector: S1 for the joker shortcut, S2 for the pit lane.
    jokers, pits = events_of(payload, "joker"), events_of(payload, "pit")
    assert jokers["WLAD111"]["sector_index"] == 0
    assert pits["WLAD111"]["sector_index"] == 1
    assert jokers["TWG"]["sector_index"] is None


def test_event_lap_ids_point_at_the_laps_of_the_session(
    client: TestClient, session_id: int
) -> None:
    payload = as_json(client.get(f"/api/sessions/{session_id}/events"))
    detail = as_json(client.get(f"/api/sessions/{session_id}"))
    laps = {lap["id"]: lap for lap in detail["laps"]}
    for event in payload["events"]:
        lap = laps[event["lap_id"]]
        assert (lap["driver"], lap["lap_number"]) == (event["driver"], event["lap_number"])
        assert lap["time_ms"] == event["time_ms"]
        assert event["kind"] in lap["effective_tags"]


def test_getting_the_events_does_not_change_the_stored_tags(
    client: TestClient, session_id: int
) -> None:
    before = as_json(client.get(f"/api/sessions/{session_id}"))["laps"]
    as_json(client.get(f"/api/sessions/{session_id}/events", params={"joker_ratio": 0.9}))
    after = as_json(client.get(f"/api/sessions/{session_id}"))["laps"]
    assert before == after


def test_thresholds_change_what_is_detected(client: TestClient, session_id: int) -> None:
    strict = as_json(
        client.get(f"/api/sessions/{session_id}/events", params={"joker_ratio": 0.95})
    )
    # WLAD111's joker only gains 4.5% of his baseline, so 0.95 misses it.
    assert set(events_of(strict, "joker")) == set(JOKER_LAPS) - {"WLAD111"}
    assert sorted(strict["drivers_without_joker"]) == sorted(["WLAD111", "ИГОРЬ53"])
    assert strict["config"]["joker_ratio"] == 0.95

    loose = as_json(client.get(f"/api/sessions/{session_id}/events", params={"pit_ratio": 1.9}))
    assert events_of(loose, "pit") == {}
    assert sorted(loose["drivers_without_pit"]) == sorted(DRIVERS)


def test_the_reference_race_needs_no_pit_proposal(client: TestClient, session_id: int) -> None:
    """Every driver pitted, so there is nothing left for a human to confirm."""
    payload = as_json(client.get(f"/api/sessions/{session_id}/events"))
    assert payload["drivers_without_pit"] == []
    assert payload["pit_candidates"] == []
    assert payload["warnings"] == []


def test_a_missing_pit_arrives_with_the_lap_to_confirm(
    client: TestClient, session_id: int
) -> None:
    """SPEC §10.2: the pit stop is mandatory, so its absence is actionable."""
    payload = as_json(client.get(f"/api/sessions/{session_id}/events", params={"pit_ratio": 1.9}))
    candidates = {event["driver"]: event for event in payload["pit_candidates"]}

    assert sorted(candidates) == sorted(DRIVERS)
    for driver, lap_number in PIT_LAPS.items():
        candidate = candidates[driver]
        # The slowest lap of each driver is exactly the pit lap the default
        # thresholds find, so one click restores the truth.
        assert candidate["lap_number"] == lap_number
        assert candidate["kind"] == "pit"
        assert candidate["confidence"] == 0.0
        assert candidate["ratio"] > 1.0 and candidate["delta_ms"] > 0
        assert candidate["lap_id"] == lap_of(client, session_id, driver, lap_number)["id"]
        # `applied` is storage state, not a verdict of this run: the import
        # tagged that lap with the default thresholds and this read-only call
        # changed nothing.
        assert candidate["applied"] is True

    assert events_of(payload, "pit") == {}
    assert len(payload["warnings"]) == len(DRIVERS)
    assert all("заезжать на пит обязаны все" in warning for warning in payload["warnings"])


def test_a_proposed_pit_lap_is_never_tagged_automatically(
    client: TestClient, session_id: int
) -> None:
    """A proposal is an invitation to a human, not a detection to store."""
    payload = as_json(
        client.post(f"/api/sessions/{session_id}/events/detect", params={"pit_ratio": 1.9})
    )
    assert sorted(event["driver"] for event in payload["pit_candidates"]) == sorted(DRIVERS)
    for driver, lap_number in PIT_LAPS.items():
        lap = lap_of(client, session_id, driver, lap_number)
        assert "pit" not in lap["effective_tags"]
        assert lap["auto_tags"] == []
    # And every candidate now honestly reports that nothing is in force on it.
    assert all(event["applied"] is False for event in payload["pit_candidates"])


# --------------------------------------------------------------------------- #
# POST /api/sessions/{id}/events/detect
# --------------------------------------------------------------------------- #


def test_detect_persists_the_automatic_tags_and_is_idempotent(
    client: TestClient, session_id: int
) -> None:
    first = as_json(client.post(f"/api/sessions/{session_id}/events/detect"))
    assert first["persisted"] is True
    assert first["counts"] == {"drivers": 6, "joker": 5, "pit": 6}

    laps_after_first = as_json(client.get(f"/api/sessions/{session_id}"))["laps"]
    second = as_json(client.post(f"/api/sessions/{session_id}/events/detect"))
    assert second == first
    assert as_json(client.get(f"/api/sessions/{session_id}"))["laps"] == laps_after_first

    tagged = {
        (lap["driver"], lap["lap_number"]): lap["auto_tags"]
        for lap in laps_after_first
        if lap["auto_tags"]
    }
    assert {key: tags for key, tags in tagged.items() if tags == ["joker"]} == {
        (driver, lap): ["joker"] for driver, lap in JOKER_LAPS.items()
    }
    assert {key: tags for key, tags in tagged.items() if tags == ["pit"]} == {
        (driver, lap): ["pit"] for driver, lap in PIT_LAPS.items()
    }


def test_detect_rewrites_the_previous_automatic_tags(client: TestClient, session_id: int) -> None:
    strict = as_json(
        client.post(f"/api/sessions/{session_id}/events/detect", params={"joker_ratio": 0.95})
    )
    assert "WLAD111" in strict["drivers_without_joker"]
    assert lap_of(client, session_id, "WLAD111", 3)["effective_tags"] == []
    # Without his joker, WLAD111's clean best becomes the official one again.
    assert stats_rows(client, session_id)["WLAD111"]["best_ms"] == OFFICIAL_BESTS["WLAD111"]

    restored = as_json(client.post(f"/api/sessions/{session_id}/events/detect"))
    assert restored["drivers_without_joker"] == ["ИГОРЬ53"]
    assert lap_of(client, session_id, "WLAD111", 3)["effective_tags"] == ["joker"]


def test_manual_annotations_survive_and_beat_the_detector(
    client: TestClient, session_id: int
) -> None:
    joker_lap = lap_of(client, session_id, "WLAD111", 3)
    assert joker_lap["auto_tags"] == ["joker"]

    tagged = client.post(
        f"/api/laps/{joker_lap['id']}/tags", json={"tag": "traffic", "note": "checked by hand"}
    )
    assert tagged.status_code == 204, tagged.text

    payload = as_json(client.post(f"/api/sessions/{session_id}/events/detect"))
    event = events_of(payload, "joker")["WLAD111"]
    assert event["applied"] is False
    assert event["overridden_by_manual"] is True

    lap = lap_of(client, session_id, "WLAD111", 3)
    assert lap["auto_tags"] == ["joker"]  # the proposal is still visible
    assert lap["manual_tags"] == ["traffic"]
    assert lap["effective_tags"] == ["traffic"]  # ... but the human decides
    assert lap["manually_annotated"] is True

    # A human verdict of "this is not a joker" restores the official best lap.
    assert stats_rows(client, session_id)["WLAD111"]["official_best_is_joker"] is False


def test_a_manual_joker_makes_the_official_best_of_a_missing_driver_a_joker(
    client: TestClient, session_id: int
) -> None:
    """ИГОРЬ53 is the one driver a detector cannot resolve -- a human can."""
    row = stats_rows(client, session_id)["ИГОРЬ53"]
    assert row["official_best_is_joker"] is False

    lap = lap_of(client, session_id, "ИГОРЬ53", int(row["official_best_lap_number"]))
    assert client.post(f"/api/laps/{lap['id']}/tags", json={"tag": "joker"}).status_code == 204

    updated = stats_rows(client, session_id)["ИГОРЬ53"]
    assert updated["official_best_is_joker"] is True
    assert updated["best_ms"] > OFFICIAL_BESTS["ИГОРЬ53"]


@pytest.mark.parametrize(
    "params",
    [
        {"pit_ratio": 1.0},
        {"pit_ratio": 0.9},
        {"pit_ratio": -1},
        {"pit_ratio": "slow"},
        {"joker_ratio": 1.0},
        {"joker_ratio": 1.5},
        {"joker_ratio": 0},
        {"joker_ratio": "fast"},
        {"one_per_driver": "maybe"},
    ],
)
def test_impossible_thresholds_are_422(
    client: TestClient, session_id: int, params: Mapping[str, Any]
) -> None:
    for response in (
        client.get(f"/api/sessions/{session_id}/events", params=params),
        client.post(f"/api/sessions/{session_id}/events/detect", params=params),
    ):
        assert response.status_code == 422, response.text
        assert response.json()["detail"]


def test_unknown_session_is_a_404_on_both_endpoints(client: TestClient) -> None:
    for response in (
        client.get("/api/sessions/424242/events"),
        client.post("/api/sessions/424242/events/detect"),
    ):
        assert response.status_code == 404, response.text
        assert "424242" in response.json()["detail"]


def test_impossible_session_ids_never_reach_the_database(client: TestClient) -> None:
    for path in ("/api/sessions/0/events", "/api/sessions/99999999999999999999999/events"):
        assert client.get(path).status_code == 422
    assert client.post("/api/sessions/0/events/detect").status_code == 422


# --------------------------------------------------------------------------- #
# Official vs clean best lap -- the product's key metric (SPEC §10.4)
# --------------------------------------------------------------------------- #


def test_official_best_is_a_joker_lap_for_five_drivers_out_of_six(
    client: TestClient, session_id: int
) -> None:
    rows = stats_rows(client, session_id)
    assert set(rows) == set(DRIVERS)

    flagged = [driver for driver, row in rows.items() if row["official_best_is_joker"]]
    assert sorted(flagged) == sorted(JOKER_BEST_DRIVERS)
    assert rows["ИГОРЬ53"]["official_best_is_joker"] is False

    for driver in JOKER_BEST_DRIVERS:
        row = rows[driver]
        assert row["official_best_ms"] == OFFICIAL_BESTS[driver]
        assert row["official_best_source"] == "classification"
        assert row["official_best_lap_number"] == JOKER_LAPS[driver]
        assert row["official_best_tags"] == ["joker"]
        assert row["best_ms"] > row["official_best_ms"]
        assert row["best_delta_ms"] == row["best_ms"] - row["official_best_ms"]


def test_the_joker_is_worth_about_one_and_a_half_to_two_seconds(
    client: TestClient, session_id: int
) -> None:
    rows = stats_rows(client, session_id)
    for driver in FULL_GAIN_DRIVERS:
        delta = rows[driver]["best_delta_ms"]
        assert 1800 <= delta <= 2000, f"{driver}: {delta} ms"

    # WLAD111's joker was a poor one (only ~1.0 s), and ИГОРЬ53 has none at all,
    # so his official best lap *is* his clean best lap.
    assert 900 <= rows["WLAD111"]["best_delta_ms"] <= 1100
    assert rows["ИГОРЬ53"]["best_delta_ms"] == 0
    assert rows["ИГОРЬ53"]["best_ms"] == OFFICIAL_BESTS["ИГОРЬ53"]


def test_the_two_metrics_rank_the_drivers_differently(client: TestClient, session_id: int) -> None:
    """Why the clean best matters: WLAD111 is 5th officially, 2nd on pace."""
    rows = stats_rows(client, session_id)
    by_official = sorted(rows, key=lambda name: rows[name]["official_best_ms"])
    by_clean = sorted(rows, key=lambda name: rows[name]["best_ms"])

    assert by_official.index("WLAD111") == 4
    assert by_clean.index("WLAD111") == 1
    assert by_clean[0] == "TWG"
    assert rows["WLAD111"]["best_ms"] - rows["TWG"]["best_ms"] < 50
    assert by_official != by_clean


def test_stats_summarise_the_joker_inflated_official_bests(
    client: TestClient, session_id: int
) -> None:
    payload = as_json(client.get(f"/api/sessions/{session_id}/stats"))
    summary = payload["official_best"]
    assert sorted(summary["joker_inflated_drivers"]) == sorted(JOKER_BEST_DRIVERS)
    assert summary["joker_inflated_count"] == 5
    assert summary["drivers_count"] == 6
    assert "joker" in summary["label"]
    assert "joker" in summary["note"].casefold()


def test_official_best_is_reported_even_without_a_classification(
    client: TestClient, session_id: int
) -> None:
    """The delta must not vanish when the filter keeps the joker lap in."""
    rows = stats_rows(client, session_id, exclude_tags="")
    for driver in FULL_GAIN_DRIVERS:
        row = rows[driver]
        assert row["official_best_ms"] == OFFICIAL_BESTS[driver]
        assert row["best_ms"] == row["official_best_ms"]
        assert row["best_delta_ms"] == 0
        assert row["official_best_is_joker"] is True


# --------------------------------------------------------------------------- #
# Lap tags with their source, and the joker-inflated leaderboards
# --------------------------------------------------------------------------- #


def test_laps_expose_the_source_of_every_tag(client: TestClient, session_id: int) -> None:
    detail = as_json(client.get(f"/api/sessions/{session_id}"))
    annotated = 0
    for lap in detail["laps"]:
        assert set(lap) >= {"tags", "annotations", "manual_tags", "auto_tags", "effective_tags"}
        for annotation in lap["annotations"]:
            assert annotation["source"] in {"manual", "auto"}
            assert annotation["tag"]
            annotated += 1
        assert lap["manually_annotated"] is bool(lap["manual_tags"])
        assert lap["effective_tags"] == [tag["tag"] for tag in lap["tags"]]
    assert annotated == len(JOKER_LAPS) + len(PIT_LAPS)  # 5 jokers + 6 pits

    joker_lap = lap_of(client, session_id, "TWG", JOKER_LAPS["TWG"])
    assert joker_lap["auto_tags"] == ["joker"]
    assert joker_lap["manual_tags"] == []
    assert [tag["source"] for tag in joker_lap["tags"]] == ["auto"]
    assert joker_lap["tags"][0]["note"]


def test_rankings_are_flagged_as_joker_inflated(client: TestClient, session_id: int) -> None:
    payload = as_json(client.get(f"/api/sessions/{session_id}/rankings"))
    assert payload["weekly_best"] and payload["track_record"]
    assert payload["official_best_based"] is True
    assert payload["joker_inflated"] is True
    assert payload["label"] == "official, joker-inflated"
    assert "joker" in payload["note"].casefold()
    assert "official best lap" in payload["note"].casefold()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def test_cli_import_reports_the_joker_and_pit_laps_it_tagged(tmp_path: Path) -> None:
    database = tmp_path / "cli.db"
    imported = run_cli("import", str(EML_PATH), database=database)
    assert imported.returncode == 0, imported.stderr
    assert "Размечено джокеров: 5, питов: 6." in imported.stdout
    assert "no joker detected for: ИГОРЬ53" in imported.stdout
    assert "no pit stop detected for" not in imported.stdout

    again = run_cli("import", str(EML_PATH), database=database)
    assert again.returncode == 0, again.stderr
    assert "уже импортировано" in again.stdout.casefold()


def test_cli_events_proposes_a_lap_when_no_pit_is_found(tmp_path: Path) -> None:
    database = tmp_path / "cli.db"
    assert run_cli("import", str(EML_PATH), database=database).returncode == 0

    listed = run_cli("events", "1", "--pit-ratio", "1.9", database=database)
    assert listed.returncode == 0, listed.stderr
    assert "Proposed pit laps (not tagged; confirm the right one by hand)" in listed.stdout
    assert "40.342" in listed.stdout  # WLAD111's pit lap, offered as the candidate
    for driver in DRIVERS:
        assert driver in listed.stdout.split("Proposed pit laps")[1]
    assert "заезжать на пит обязаны все" in listed.stdout


def test_cli_events_lists_the_detected_laps(tmp_path: Path) -> None:
    database = tmp_path / "cli.db"
    imported = run_cli("import", str(EML_PATH), database=database)
    assert imported.returncode == 0, imported.stderr

    listed = run_cli("events", "1", database=database)
    assert listed.returncode == 0, listed.stderr
    assert "joker" in listed.stdout and "pit" in listed.stdout
    assert "Without a joker: ИГОРЬ53" in listed.stdout
    assert "5 joker and 6 pit lap(s) for 6 driver(s)" in listed.stdout
    assert "+12.284" in listed.stdout  # WLAD111's pit stop, in seconds
    assert "S2" in listed.stdout  # ... localised in his second sector

    detected = run_cli("events", "1", "--detect", "--joker-ratio", "0.95", database=database)
    assert detected.returncode == 0, detected.stderr
    assert "Automatic tags rewritten" in detected.stdout
    assert "Without a joker: WLAD111, ИГОРЬ53" in detected.stdout

    bad = run_cli("events", "1", "--pit-ratio", "0.5", database=database)
    assert bad.returncode != 0
    assert "greater than 1" in bad.stderr
    assert "Traceback" not in bad.stderr

    missing = run_cli("events", "99999", database=database)
    assert missing.returncode == 1
    assert "not found" in missing.stderr


def test_cli_show_contrasts_the_official_best_lap_with_the_clean_one(tmp_path: Path) -> None:
    database = tmp_path / "cli.db"
    assert run_cli("import", str(EML_PATH), database=database).returncode == 0

    shown = run_cli("show", "1", database=database)
    assert shown.returncode == 0, shown.stderr
    assert "OFFICIAL" in shown.stdout
    assert "Δ" in shown.stdout
    assert "26.012 J" in shown.stdout  # KOLYA11's official best is his joker
    assert "+1.885" in shown.stdout  # ... and it is 1.885 s off his real pace
    assert "official best lap is a joker lap (marked J) for 5 of 6 drivers" in shown.stdout


def test_cli_export_carries_the_official_best_metric(tmp_path: Path) -> None:
    database = tmp_path / "cli.db"
    assert run_cli("import", str(EML_PATH), database=database).returncode == 0

    exported = run_cli("export", "1", "--json", database=database)
    assert exported.returncode == 0, exported.stderr
    payload = json.loads(exported.stdout)
    rows: Sequence[Mapping[str, Any]] = payload["stats"]["drivers"]
    flagged = [row["driver"] for row in rows if row["official_best_is_joker"]]
    assert sorted(flagged) == sorted(JOKER_BEST_DRIVERS)


def test_a_hand_annotated_session_is_reported_complete(
    client: TestClient, session_id: int
) -> None:
    """SPEC §10.2 invites manual annotation; accepting the invitation must end it.

    The one gap of the reference race is ИГОРЬ53's joker.  Once a human names
    the lap, the report has to stop asking: `drivers_without_joker` empties, the
    warning disappears and `complete` flips to true -- both on the read endpoint
    and after a fresh detection run.
    """
    before = as_json(client.get(f"/api/sessions/{session_id}/events"))
    assert before["drivers_without_joker"] == ["ИГОРЬ53"]
    assert before["complete"] is False

    lap = lap_of(client, session_id, "ИГОРЬ53", 15)
    assert client.post(f"/api/laps/{lap['id']}/tags", json={"tag": "joker"}).status_code == 204

    after = as_json(client.get(f"/api/sessions/{session_id}/events"))
    assert after["drivers_without_joker"] == []
    assert after["drivers_without_pit"] == []
    assert after["drivers_with_multiple"] == []
    assert after["counts"] == {"drivers": 6, "joker": 6, "pit": 6}
    assert after["complete"] is True
    assert not any("ИГОРЬ53" in text for text in after["warnings"])

    joker = events_of(after, "joker")["ИГОРЬ53"]
    assert joker["lap_number"] == 15
    assert joker["applied"] is True
    assert joker["overridden_by_manual"] is False
    assert joker["confidence"] == 1.0

    # A re-detection must not undo the human's verdict either.
    redetected = as_json(client.post(f"/api/sessions/{session_id}/events/detect"))
    assert redetected["complete"] is True
    assert events_of(redetected, "joker")["ИГОРЬ53"]["lap_number"] == 15


def test_a_confirmed_pit_proposal_closes_the_gap(client: TestClient, session_id: int) -> None:
    """The one-click flow SPEC §10.2 asks for: proposal in, confirmation out."""
    payload = as_json(
        client.get(f"/api/sessions/{session_id}/events", params={"pit_ratio": 1.9})
    )
    assert sorted(payload["drivers_without_pit"]) == sorted(DRIVERS)
    proposals = {event["driver"]: event for event in payload["pit_candidates"]}

    for driver, proposal in proposals.items():
        response = client.post(f"/api/laps/{proposal['lap_id']}/tags", json={"tag": "pit"})
        assert response.status_code == 204, (driver, response.text)

    settled = as_json(
        client.get(f"/api/sessions/{session_id}/events", params={"pit_ratio": 1.9})
    )
    assert settled["drivers_without_pit"] == []
    assert settled["pit_candidates"] == []
    assert settled["counts"]["pit"] == len(DRIVERS)


def test_the_detector_survives_a_threshold_change(client: TestClient, session_id: int) -> None:
    """Re-detecting with looser thresholds used to be the only 500 of the API."""
    looser = client.post(
        f"/api/sessions/{session_id}/events/detect", params={"joker_ratio": 0.99}
    )
    assert looser.status_code == 200, looser.text
    assert as_json(looser)["counts"]["joker"] >= len(JOKER_LAPS)

    back = client.post(f"/api/sessions/{session_id}/events/detect")
    assert back.status_code == 200, back.text
    payload = as_json(back)
    assert {driver: event["lap_number"] for driver, event in events_of(payload, "joker").items()} == (
        JOKER_LAPS
    )
    assert {driver: event["lap_number"] for driver, event in events_of(payload, "pit").items()} == (
        PIT_LAPS
    )


def test_the_official_best_lap_is_labelled_everywhere_it_is_served(
    client: TestClient, session_id: int
) -> None:
    """SPEC §10.4: the joker-inflated number must never be served bare."""
    for row in as_json(client.get("/api/drivers")):
        assert row["official_best_based"] is True
        assert row["joker_inflated"] is True
        assert row["best_lap_label"] == "official, joker-inflated"

    history = as_json(client.get("/api/drivers/WLAD111/history"))
    assert history
    for row in history:
        assert row["best_lap_label"] == "official, joker-inflated"


def test_cli_re_detects_with_a_changed_threshold(tmp_path: Path) -> None:
    """README's stated reason for `--detect`: the thresholds moved."""
    database = tmp_path / "cli.db"
    assert run_cli("import", str(EML_PATH), database=database).returncode == 0

    looser = run_cli("events", "1", "--detect", "--joker-ratio", "0.99", database=database)
    assert looser.returncode == 0, looser.stderr + looser.stdout
    assert "Database error" not in looser.stderr

    back = run_cli("events", "1", "--detect", database=database)
    assert back.returncode == 0, back.stderr
    assert "no joker detected" in back.stdout.lower() or "ИГОРЬ53" in back.stdout


def test_cli_show_prints_both_orders(tmp_path: Path) -> None:
    """SPEC §10.4's headline, as a column instead of an arithmetic exercise."""
    database = tmp_path / "cli.db"
    assert run_cli("import", str(EML_PATH), database=database).returncode == 0

    shown = run_cli("show", "1", database=database)
    assert shown.returncode == 0, shown.stderr
    assert "#OFF" in shown.stdout and "#PACE" in shown.stdout
    # WLAD111 is fifth on the official best lap and second on the clean one.
    row = next(line for line in shown.stdout.splitlines() if "WLAD111" in line and "27.804" in line)
    assert row.split()[:4] == ["2", "5", "2", "WLAD111"]  # finished 2nd, #OFF 5, #PACE 2

    by_best = run_cli("show", "1", "--sort", "best", database=database)
    assert by_best.returncode == 0, by_best.stderr
    pace_table = by_best.stdout.split("\nPace\n", 1)[1]
    order = [
        line.split()[3]
        for line in pace_table.splitlines()
        if any(line.split()[3:4] == [name] for name in DRIVERS)
    ]
    assert order[:3] == ["TWG", "WLAD111", "KOLYA11"]


def test_cli_reports_the_real_size_of_the_file_it_wrote(tmp_path: Path) -> None:
    """The payload holds Cyrillic nicknames: characters are not bytes."""
    database = tmp_path / "cli.db"
    assert run_cli("import", str(EML_PATH), database=database).returncode == 0

    target = tmp_path / "final_a.json"
    written = run_cli("export", "1", "--json", str(target), database=database)
    assert written.returncode == 0, written.stderr
    assert f"({target.stat().st_size} bytes)" in written.stdout
    assert target.stat().st_size > len(target.read_text(encoding="utf-8"))


def test_cli_type_errors_are_written_for_a_human(tmp_path: Path) -> None:
    database = tmp_path / "cli.db"
    for args, expected in (
        (("show", "abc"), "is not a whole number"),
        (("events", "1", "--pit-ratio", "abc"), "is not a number"),
    ):
        failed = run_cli(*args, database=database)
        assert failed.returncode == 2
        assert expected in failed.stderr
        assert "_row_id" not in failed.stderr and "_pit_ratio" not in failed.stderr
