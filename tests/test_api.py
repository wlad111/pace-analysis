"""End-to-end tests for the HTTP API (SPEC §8.4) plus a CLI smoke test.

Every test runs against a throw-away SQLite file (``tmp_path`` + ``PACE_DB``)
and imports the real Apex Timing email from the repository root, so the whole
stack (parsing -> storage -> stats -> JSON) is exercised for real.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:  # allow a bare `pytest` run from anywhere
    sys.path.insert(0, str(PROJECT_ROOT))

# The parser, the storage and the stats packages are written in parallel with
# this module: skip instead of failing while they are still missing.

from fastapi.testclient import TestClient  # noqa: E402

from karting.api.app import create_app  # noqa: E402

_EML_CANDIDATES = sorted(PROJECT_ROOT.glob("*.eml"))
if not _EML_CANDIDATES:  # pragma: no cover - the fixture email ships with the repo
    pytest.skip("no .eml sample in the repository root", allow_module_level=True)
EML_PATH = _EML_CANDIDATES[0]
EML_BYTES = EML_PATH.read_bytes()
# The real file name carries U+200B characters; uploads use an ASCII name so
# that the tests assert on the API, not on multipart header encoding.
EML_UPLOAD_NAME = "final_a.eml"

DRIVERS = ("KOLYA11", "WLAD111", "TWG", "DENISENKO", "PHREEMAN", "ИГОРЬ53")


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


def upload(client: TestClient, *files: tuple[str, bytes]) -> Any:
    """POST /api/imports with the given ``(filename, payload)`` pairs."""
    parts = [("files", (name, payload, "message/rfc822")) for name, payload in files]
    return client.post("/api/imports", files=parts)


def upload_sample(client: TestClient) -> Any:
    return upload(client, (EML_UPLOAD_NAME, EML_BYTES))


@pytest.fixture()
def session_id(client: TestClient) -> int:
    """Import the sample email once and return the created session id."""
    response = upload_sample(client)
    assert response.status_code == 200, response.text
    reports = response.json()
    assert len(reports) == 1
    assert reports[0]["status"] == "imported", reports[0]
    identifier = reports[0]["session_id"]
    assert isinstance(identifier, int)
    return identifier


def lap_of(detail: Mapping[str, Any], driver: str, lap_number: int) -> Mapping[str, Any]:
    for lap in detail["laps"]:
        if lap["driver"] == driver and lap["lap_number"] == lap_number:
            return lap
    pytest.fail(f"lap {lap_number} of {driver} is missing from the session payload")


def tag_values(lap: Mapping[str, Any]) -> set[str]:
    """Tag values of a lap, accepting plain strings or ``{tag: ...}`` dicts."""
    values: set[str] = set()
    for item in lap.get("tags") or []:
        if isinstance(item, Mapping):
            for key in ("tag", "value", "name"):
                if isinstance(item.get(key), str):
                    values.add(item[key])
                    break
        elif isinstance(item, str):
            values.add(item)
    return values


def driver_row(rows: Sequence[Mapping[str, Any]], nickname: str) -> Mapping[str, Any]:
    for row in rows:
        if row.get("driver") == nickname:
            return row
    pytest.fail(f"{nickname} is missing from {[row.get('driver') for row in rows]}")


def run_cli(*args: str, database: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "karting.cli", *args],
        cwd=PROJECT_ROOT,
        env={**os.environ, "PACE_DB": str(database)},
        capture_output=True,
        text=True,
        timeout=300,
    )


# --------------------------------------------------------------------------- #
# Import
# --------------------------------------------------------------------------- #


def test_health_on_empty_database(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    # `read_only` rides along so the frontend can hide controls it cannot use.
    assert response.json() == {"status": "ok", "sessions": 0, "read_only": False}


def test_import_creates_one_session(client: TestClient, session_id: int) -> None:
    assert client.get("/api/health").json()["sessions"] == 1
    listing = client.get("/api/sessions").json()
    assert [row["id"] for row in listing] == [session_id]


def test_reimport_is_idempotent(client: TestClient, session_id: int) -> None:
    response = upload_sample(client)
    assert response.status_code == 200, response.text
    report = response.json()[0]
    assert report["already_imported"] is True
    assert report["status"] == "already_imported"
    assert report["session_created"] is False
    assert report["inserted_laps"] == 0
    assert report["session_id"] == session_id
    assert "уже импортировано" in report["detail"].lower()

    assert client.get("/api/health").json()["sessions"] == 1
    assert len(client.get(f"/api/sessions/{session_id}").json()["laps"]) == 120


def test_batch_import_continues_after_an_unusable_file(client: TestClient) -> None:
    response = upload(client, ("empty.eml", b""), (EML_UPLOAD_NAME, EML_BYTES))
    assert response.status_code == 200, response.text
    reports = response.json()
    assert [report["filename"] for report in reports] == ["empty.eml", EML_UPLOAD_NAME]
    assert reports[0]["status"] == "failed"
    assert reports[0]["session_id"] is None
    assert "empty" in reports[0]["detail"].lower()
    assert reports[1]["status"] == "imported"
    assert len(client.get("/api/sessions").json()) == 1


def test_single_unusable_file_is_a_400(client: TestClient) -> None:
    response = upload(client, ("empty.eml", b""))
    assert response.status_code == 400
    assert "empty" in response.json()["detail"].lower()
    assert client.get("/api/health").json()["sessions"] == 0


def test_garbage_upload_never_returns_a_bare_500(client: TestClient) -> None:
    response = upload(client, ("junk.eml", b"\x00\x01 this is not an email at all"))
    assert response.status_code in (200, 400), response.text
    payload = response.json()
    if response.status_code == 400:
        assert isinstance(payload["detail"], str)
    else:
        assert isinstance(payload, list) and len(payload) == 1


def test_imports_without_files_is_a_422(client: TestClient) -> None:
    assert client.post("/api/imports").status_code == 422


# --------------------------------------------------------------------------- #
# Sessions, laps, rankings
# --------------------------------------------------------------------------- #


def test_sessions_listing(client: TestClient, session_id: int) -> None:
    rows = client.get("/api/sessions").json()
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == session_id
    assert row["name"] == "PRIMO GARA - Final A"
    assert row["code"] == "FA"
    assert str(row["started_at"]).startswith("2026-08-03")


def test_session_detail(client: TestClient, session_id: int) -> None:
    detail = client.get(f"/api/sessions/{session_id}").json()
    assert set(detail) >= {"session", "club", "entries", "laps"}
    assert detail["session"]["name"] == "PRIMO GARA - Final A"

    entries = detail["entries"]
    assert [entry["position"] for entry in entries] == [1, 2, 3, 4, 5, 6]
    assert [entry["driver"] for entry in entries] == list(DRIVERS)
    assert {entry["driver"]: entry["best_lap_ms"] for entry in entries}["TWG"] == 25845

    laps = detail["laps"]
    assert len(laps) == 120
    assert all(isinstance(lap["id"], int) for lap in laps)
    assert lap_of(detail, "WLAD111", 1)["time_ms"] is None
    assert lap_of(detail, "WLAD111", 3)["time_ms"] == 26788
    assert lap_of(detail, "WLAD111", 3)["is_best"] is True
    assert lap_of(detail, "WLAD111", 2)["sectors"] == [14218, 14654]
    assert lap_of(detail, "ИГОРЬ53", 20)["time_ms"] == 28559


def test_rankings(client: TestClient, session_id: int) -> None:
    payload = client.get(f"/api/sessions/{session_id}/rankings").json()
    assert set(payload) >= {"weekly_best", "track_record"}
    assert payload["weekly_best"] and payload["track_record"]
    # Both leaderboards rank by the official (joker-inflated) best lap, §10.4.
    assert payload["joker_inflated"] is True
    assert "KOLYA11" in json.dumps(payload["weekly_best"], ensure_ascii=False)
    assert "PHREEMAN" in json.dumps(payload["track_record"], ensure_ascii=False)


def test_unknown_session_is_a_404(client: TestClient) -> None:
    for path in (
        "/api/sessions/424242",
        "/api/sessions/424242/stats",
        "/api/sessions/424242/rankings",
        "/api/sessions/424242/compare?a=WLAD111&b=TWG",
    ):
        response = client.get(path)
        assert response.status_code == 404, path
        assert "424242" in response.json()["detail"]


# --------------------------------------------------------------------------- #
# Statistics
# --------------------------------------------------------------------------- #


def test_session_stats_defaults(client: TestClient, session_id: int) -> None:
    payload = client.get(f"/api/sessions/{session_id}/stats").json()
    assert payload["filter"]["mad_k"] == 3.0
    assert payload["filter"]["drop_first_lap"] is True
    assert payload["filter"]["drop_fast_outliers"] is False
    assert "penalty" in payload["filter"]["exclude_tags"]

    rows = payload["drivers"]
    assert [row["driver"] for row in rows] == list(DRIVERS)
    assert [row["position"] for row in rows] == [1, 2, 3, 4, 5, 6]

    wlad = driver_row(rows, "WLAD111")
    assert wlad["n_laps"] == 20
    # The clean best, not the official 26.788: lap 3 is his joker (SPEC §10.4).
    assert wlad["best_ms"] == 27804
    assert wlad["official_best_ms"] == 26788
    assert 15 <= wlad["n_used"] <= 18
    assert 1 not in wlad["used_lap_numbers"]  # first lap has no time at all
    assert 19 not in wlad["used_lap_numbers"]  # 40.342 is the pit stop
    assert 3 not in wlad["used_lap_numbers"]  # 26.788 is the joker lap
    assert wlad["median_ms"] is not None and 27_000 < wlad["median_ms"] < 29_000
    assert wlad["pace_delta_to_best_ms"] is not None

    deltas = [row["pace_delta_to_best_ms"] for row in rows if row["pace_delta_to_best_ms"] is not None]
    assert len(deltas) == 6
    assert min(deltas) == 0.0 and all(delta >= 0 for delta in deltas)


def test_session_stats_honours_query_filter(client: TestClient, session_id: int) -> None:
    default_used = driver_row(
        client.get(f"/api/sessions/{session_id}/stats").json()["drivers"], "WLAD111"
    )["n_used"]

    response = client.get(
        f"/api/sessions/{session_id}/stats",
        params={
            "mad_k": 1.5,
            "drop_first_lap": "false",
            "drop_slow_outliers": "true",
            "drop_fast_outliers": "true",
            "min_laps": 1,
            "exclude_tags": "pit,penalty",
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["filter"] == {
        "mad_k": 1.5,
        "drop_missing": True,
        "drop_first_lap": False,
        "drop_slow_outliers": True,
        "drop_fast_outliers": True,
        "exclude_tags": ["penalty", "pit"],
        "min_laps": 1,
    }
    strict_used = driver_row(payload["drivers"], "WLAD111")["n_used"]
    assert strict_used <= default_used


@pytest.mark.parametrize(
    "params",
    [
        {"mad_k": 0},
        {"mad_k": -2},
        {"mad_k": "fast"},
        {"min_laps": 0},
        {"drop_first_lap": "maybe"},
        {"exclude_tags": "not a tag!"},
    ],
)
def test_invalid_filter_parameters_are_422(
    client: TestClient, session_id: int, params: dict[str, Any]
) -> None:
    response = client.get(f"/api/sessions/{session_id}/stats", params=params)
    assert response.status_code == 422, response.text
    assert "detail" in response.json()


def test_compare_two_drivers(client: TestClient, session_id: int) -> None:
    response = client.get(
        f"/api/sessions/{session_id}/compare", params={"a": "WLAD111", "b": "TWG"}
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["driver_a"] == "WLAD111"
    assert payload["driver_b"] == "TWG"
    assert payload["n_a"] > 0 and payload["n_b"] > 0
    assert payload["stats_a"]["n_used"] > 0 and payload["stats_b"]["n_used"] > 0
    assert payload["mean_diff_ms"] is not None
    assert payload["median_diff_ms"] is not None
    assert payload["caveats"], "the dependence of laps within a race must be spelled out"

    names = {test["name"] for test in payload["tests"]}
    assert len(payload["tests"]) >= 2 and all(isinstance(name, str) for name in names)
    for test in payload["tests"]:
        assert set(test) >= {"name", "statistic", "p_value"}


def test_compare_rejects_unknown_and_identical_drivers(client: TestClient, session_id: int) -> None:
    unknown = client.get(f"/api/sessions/{session_id}/compare", params={"a": "NOBODY", "b": "TWG"})
    assert unknown.status_code == 404
    assert "NOBODY" in unknown.json()["detail"]

    same = client.get(f"/api/sessions/{session_id}/compare", params={"a": "TWG", "b": "TWG"})
    assert same.status_code == 422

    missing = client.get(f"/api/sessions/{session_id}/compare", params={"a": "TWG"})
    assert missing.status_code == 422


# --------------------------------------------------------------------------- #
# Drivers
# --------------------------------------------------------------------------- #


def test_drivers_listing_and_history(client: TestClient, session_id: int) -> None:
    drivers = client.get("/api/drivers").json()
    nicknames = {row["nickname"] for row in drivers}
    assert set(DRIVERS) <= nicknames

    history = client.get("/api/drivers/WLAD111/history")
    assert history.status_code == 200
    assert isinstance(history.json(), list) and history.json()

    cyrillic = client.get("/api/drivers/ИГОРЬ53/history")
    assert cyrillic.status_code == 200
    assert isinstance(cyrillic.json(), list)


def test_unknown_driver_is_a_404(client: TestClient, session_id: int) -> None:
    response = client.get("/api/drivers/NOBODY/history")
    assert response.status_code == 404
    assert "NOBODY" in response.json()["detail"]


# --------------------------------------------------------------------------- #
# Lap tags
# --------------------------------------------------------------------------- #


def test_tag_dictionary(client: TestClient) -> None:
    tags = client.get("/api/tags").json()
    values = {item["value"] for item in tags}
    assert {"penalty", "traffic", "pit", "outlier", "invalid"} <= values
    assert all(item["label"] for item in tags)


def test_lap_tag_add_and_remove(client: TestClient, session_id: int) -> None:
    detail = client.get(f"/api/sessions/{session_id}").json()
    lap = lap_of(detail, "WLAD111", 5)
    lap_id = lap["id"]
    assert "traffic" not in tag_values(lap)

    stats_url = f"/api/sessions/{session_id}/stats"
    default_payload = client.get(stats_url).json()
    before = driver_row(default_payload["drivers"], "WLAD111")
    assert 5 in before["used_lap_numbers"]
    # `exclude_tags` replaces the default set, so keep it and add the new tag.
    with_traffic = ",".join([*default_payload["filter"]["exclude_tags"], "traffic"])

    created = client.post(f"/api/laps/{lap_id}/tags", json={"tag": "traffic", "note": "boxed in"})
    assert created.status_code == 204, created.text

    tagged = client.get(f"/api/sessions/{session_id}").json()
    assert "traffic" in tag_values(lap_of(tagged, "WLAD111", 5))

    filtered = driver_row(
        client.get(stats_url, params={"exclude_tags": with_traffic}).json()["drivers"], "WLAD111"
    )
    # Excluding a used lap shrinks the clean set (and, with it, the robust
    # threshold), so the count can only go down.
    assert 5 not in filtered["used_lap_numbers"]
    assert filtered["n_used"] < before["n_used"]

    removed = client.delete(f"/api/laps/{lap_id}/tags/traffic")
    assert removed.status_code == 204
    assert "traffic" not in tag_values(lap_of(client.get(f"/api/sessions/{session_id}").json(), "WLAD111", 5))


def test_lap_tag_errors(client: TestClient, session_id: int) -> None:
    detail = client.get(f"/api/sessions/{session_id}").json()
    lap_id = lap_of(detail, "TWG", 4)["id"]

    unknown_lap = client.post("/api/laps/987654/tags", json={"tag": "traffic"})
    assert unknown_lap.status_code == 404
    assert client.delete("/api/laps/987654/tags/traffic").status_code == 404

    bad_tag = client.post(f"/api/laps/{lap_id}/tags", json={"tag": "not a tag!"})
    assert bad_tag.status_code == 422
    assert client.post(f"/api/laps/{lap_id}/tags", json={}).status_code == 422


# --------------------------------------------------------------------------- #
# CORS (dev front-end)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("origin", ["http://localhost:5173", "http://127.0.0.1:5173"])
def test_cors_allows_the_vite_dev_server(client: TestClient, origin: str) -> None:
    preflight = client.options(
        "/api/sessions",
        headers={"Origin": origin, "Access-Control-Request-Method": "GET"},
    )
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == origin

    response = client.get("/api/sessions", headers={"Origin": origin})
    assert response.headers["access-control-allow-origin"] == origin


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def test_cli_import_sessions_show_export(tmp_path: Path) -> None:
    database = tmp_path / "cli.db"
    maildir = tmp_path / "mail" / "2026-08"
    maildir.mkdir(parents=True)
    (maildir / "final_a.eml").write_bytes(EML_BYTES)

    first = run_cli("import", str(EML_PATH), database=database)
    assert first.returncode == 0, first.stderr or first.stdout
    assert "[imported]" in first.stdout

    # A directory is scanned recursively; the same race is recognised again.
    again = run_cli("import", str(tmp_path / "mail"), database=database)
    assert again.returncode == 0, again.stderr or again.stdout
    assert "final_a.eml" in again.stdout
    assert "[imported]" not in again.stdout

    listing = run_cli("sessions", database=database)
    assert listing.returncode == 0, listing.stderr
    assert "PRIMO GARA - Final A" in listing.stdout
    rows = listing.stdout.splitlines()[2:]
    identifier = rows[0].split()[0]
    assert identifier.isdigit()

    show = run_cli("show", identifier, database=database)
    assert show.returncode == 0, show.stderr
    assert "Classification" in show.stdout and "WLAD111" in show.stdout
    assert "26.788" in show.stdout

    out_file = tmp_path / "export.json"
    export = run_cli("export", identifier, "--json", str(out_file), database=database)
    assert export.returncode == 0, export.stderr
    payload = json.loads(out_file.read_text(encoding="utf-8"))
    assert payload["session"]["name"] == "PRIMO GARA - Final A"
    assert len(payload["laps"]) == 120
    assert payload["stats"]["drivers"]


def test_cli_reports_missing_input_and_unknown_session(tmp_path: Path) -> None:
    database = tmp_path / "cli.db"
    missing = run_cli("import", str(tmp_path / "nope.eml"), database=database)
    assert missing.returncode == 1
    assert "no such file" in (missing.stderr + missing.stdout).lower()

    unknown = run_cli("show", "999", database=database)
    assert unknown.returncode == 1
    assert "not found" in (unknown.stderr + unknown.stdout).lower()


# --------------------------------------------------------------------------- #
# Regression tests for reported defects
# --------------------------------------------------------------------------- #


def test_parallel_requests_never_return_5xx(client: TestClient, session_id: int) -> None:
    """A browser dashboard fires several calls at once (SPEC §7).

    The sqlite connection is created by a sync dependency and used by the
    endpoint, which anyio may schedule on a different worker thread.
    """
    paths = [
        "/api/sessions",
        f"/api/sessions/{session_id}",
        f"/api/sessions/{session_id}/stats",
        f"/api/sessions/{session_id}/rankings",
        f"/api/sessions/{session_id}/compare?a=KOLYA11&b=WLAD111",
        "/api/drivers",
        "/api/tags",
        "/api/health",
    ] * 3

    with ThreadPoolExecutor(max_workers=12) as pool:
        responses = list(pool.map(lambda path: client.get(path), paths))

    failures = [
        (response.request.url.path, response.status_code, response.text[:200])
        for response in responses
        if response.status_code >= 500
    ]
    assert failures == []
    assert {response.status_code for response in responses} == {200}


def test_parallel_uploads_of_the_same_email_all_succeed(client: TestClient) -> None:
    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(pool.map(lambda _: upload_sample(client), range(6)))

    assert [response.status_code for response in responses] == [200] * 6
    statuses = {response.json()[0]["status"] for response in responses}
    assert statuses <= {"imported", "already_imported", "merged"}
    assert client.get("/api/health").json()["sessions"] == 1


@pytest.mark.parametrize(
    "path",
    [
        "/api/sessions/99999999999999999999999",
        "/api/sessions/99999999999999999999999/stats",
        "/api/sessions/99999999999999999999999/rankings",
        "/api/sessions/99999999999999999999999/compare?a=A&b=B",
        "/api/sessions/-99999999999999999999999",
    ],
)
def test_out_of_range_session_ids_are_rejected_not_crashed(
    client: TestClient, path: str
) -> None:
    response = client.get(path)
    assert response.status_code == 422, response.text
    assert "detail" in response.json()


def test_out_of_range_lap_ids_are_rejected_not_crashed(client: TestClient) -> None:
    huge = 99999999999999999999999
    created = client.post(f"/api/laps/{huge}/tags", json={"tag": "pit"})
    assert created.status_code == 422, created.text
    deleted = client.delete(f"/api/laps/{huge}/tags/pit")
    assert deleted.status_code == 422, deleted.text


def test_internal_errors_do_not_leak_their_type_or_message(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from karting.storage import Database

    def explode(self: Database) -> list[dict[str, Any]]:
        raise RuntimeError("connection to /secret/path.db died in thread 281472597815680")

    monkeypatch.setattr(Database, "list_sessions", explode)
    # The default TestClient re-raises server errors; here the *response* is
    # what matters, exactly as a browser would see it.
    with TestClient(create_app(), raise_server_exceptions=False) as quiet:
        response = quiet.get("/api/sessions")

    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == "Internal server error. Quote the error id when reporting it."
    assert len(body["error_id"]) == 12
    for secret in ("RuntimeError", "/secret/path.db", "281472597815680"):
        assert secret not in response.text


def test_a_bad_file_in_a_batch_does_not_change_the_status_code(client: TestClient) -> None:
    """One contract for one file and for many: 400 only when nothing worked."""
    junk = ("notes.txt", b"just a text file, not an email")

    mixed = upload(client, (EML_UPLOAD_NAME, EML_BYTES), junk)
    assert mixed.status_code == 200, mixed.text
    assert [report["status"] for report in mixed.json()] == ["imported", "failed"]

    only_bad = upload(client, junk)
    assert only_bad.status_code == 400
    assert "detail" in only_bad.json()

    all_bad = upload(client, junk, ("other.txt", b"also not an email"))
    assert all_bad.status_code == 400
    assert "notes.txt" in all_bad.json()["detail"]


def test_stats_expose_why_a_fast_lap_was_dropped(client: TestClient, session_id: int) -> None:
    """Fast laps are cut by a meaningful tag, not by a blind threshold (§10.4)."""
    payload = client.get(f"/api/sessions/{session_id}/stats").json()
    assert payload["filter"]["drop_fast_outliers"] is False
    row = driver_row(payload["drivers"], "WLAD111")

    # Lap 3 (26.788) is his joker, so it is excluded by tag and not merely
    # flagged as suspiciously fast; nothing is left for a human to eyeball.
    reasons = {flag["lap_number"]: flag["reason"] for flag in row["excluded"]}
    assert reasons[3] == "tag:joker"
    assert reasons[19] == "tag:pit"
    assert 3 not in row["used_lap_numbers"]
    for other in payload["drivers"]:
        assert isinstance(other["suspicious_fast_lap_numbers"], list)


def test_ranking_only_drivers_carry_the_best_lap_they_are_known_for(
    client: TestClient, session_id: int
) -> None:
    rankings = client.get(f"/api/sessions/{session_id}/rankings").json()
    known = {
        row["driver"]: row["best_lap_ms"]
        for row in (*rankings["weekly_best"], *rankings["track_record"])
        if row["best_lap_ms"] is not None
    }
    drivers = {row["nickname"]: row for row in client.get("/api/drivers").json()}

    ranking_only = [row for row in drivers.values() if row["sessions_count"] == 0]
    assert ranking_only, "the reference email lists drivers that never raced here"
    for row in ranking_only:
        assert row["source"] == "ranking"
        assert row["best_lap_ms"] == known[row["nickname"]]
    assert drivers["WLAD111"]["source"] == "session"


def test_cli_export_accepts_the_documented_invocation(tmp_path: Path) -> None:
    """README / SPEC §8 show `export <id> --json` with the JSON on stdout."""
    database = tmp_path / "cli.db"
    imported = run_cli("import", str(EML_PATH), database=database)
    assert imported.returncode == 0, imported.stderr

    to_stdout = run_cli("export", "1", "--json", database=database)
    assert to_stdout.returncode == 0, to_stdout.stderr
    payload = json.loads(to_stdout.stdout)
    assert payload["session"]["name"] == "PRIMO GARA - Final A"
    assert len(payload["laps"]) == 120

    # `--json PATH` keeps writing to a file, and both produce the same document.
    target = tmp_path / "final_a.json"
    to_file = run_cli("export", "1", "--json", str(target), database=database)
    assert to_file.returncode == 0, to_file.stderr
    assert json.loads(target.read_text(encoding="utf-8")) == payload


def test_cli_rejects_an_impossible_session_id_without_a_traceback(tmp_path: Path) -> None:
    database = tmp_path / "cli.db"
    for command in ("show", "export"):
        result = run_cli(command, "99999999999999999999999", database=database)
        assert result.returncode != 0
        output = result.stderr + result.stdout
        assert "Traceback" not in output
        assert "OverflowError" not in output


# --------------------------------------------------------------------------- #
# Read-only deployments
# --------------------------------------------------------------------------- #


class TestReadOnlyMode:
    """`$PACE_READ_ONLY` is what makes a public deployment safe to expose.

    Hiding the controls in the browser is not a control: the API answers curl
    whatever the page renders. These tests pin the server-side behaviour.
    """

    WRITES = (
        ("post", "/api/sessions/1/events/detect", None),
        ("post", "/api/laps/1/tags", {"tag": "pit"}),
        ("delete", "/api/laps/1/tags/pit", None),
    )

    def test_writes_are_reachable_by_default(self, client: TestClient) -> None:
        # The point of the flag is that it changes something: without it every
        # write path exists and answers (200/204/404-on-missing-row, never 405).
        app = client.app
        methods = {
            (method, route.path)
            for route in app.routes
            if (method := next(iter(route.methods - {"HEAD", "OPTIONS", "GET"}), None))
        }
        assert ("POST", "/api/imports") in methods
        assert ("POST", "/api/laps/{lap_id}/tags") in methods
        assert ("DELETE", "/api/laps/{lap_id}/tags/{tag}") in methods

    def test_read_only_removes_every_write_route(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("PACE_READ_ONLY", "1")
        monkeypatch.setenv("PACE_DB", str(tmp_path / "ro.db"))
        from karting.api.app import create_app

        app = create_app()
        writes = [
            route.path
            for route in app.routes
            if getattr(route, "methods", set()) - {"GET", "HEAD", "OPTIONS"}
        ]
        assert writes == []

        with TestClient(app) as ro:
            assert ro.get("/api/health").json()["read_only"] is True
            assert ro.get("/api/sessions").status_code == 200
            for method, path, payload in self.WRITES:
                response = getattr(ro, method)(path, **({"json": payload} if payload else {}))
                assert response.status_code == 404, f"{method} {path} still reachable"
            files = {"files": ("x.eml", b"nope", "message/rfc822")}
            assert ro.post("/api/imports", files=files).status_code == 404

    def test_read_only_keeps_every_read_route(self, monkeypatch, tmp_path) -> None:
        """The regression this class missed the first time.

        The guard used to be an early return placed at the first write route,
        which also dropped /compare, /rankings, /drivers and the driver history:
        the routes are grouped by subject, not by verb. Asserting that writes
        disappear is not enough — reads have to survive intact.
        """
        from karting.api.app import create_app

        monkeypatch.setenv("PACE_DB", str(tmp_path / "full.db"))
        monkeypatch.delenv("PACE_READ_ONLY", raising=False)
        full = {
            (method, route.path)
            for route in create_app().routes
            if (method := next(iter(sorted(getattr(route, "methods", set()) - {"HEAD"})), None))
        }

        monkeypatch.setenv("PACE_READ_ONLY", "1")
        limited = {
            (method, route.path)
            for route in create_app().routes
            if (method := next(iter(sorted(getattr(route, "methods", set()) - {"HEAD"})), None))
        }

        reads = {route for route in full if route[0] == "GET"}
        assert reads <= limited, f"read routes lost: {sorted(reads - limited)}"
        assert full - limited == {
            ("POST", "/api/sessions/{session_id}/events/detect"),
            ("POST", "/api/imports"),
            ("POST", "/api/laps/{lap_id}/tags"),
            ("DELETE", "/api/laps/{lap_id}/tags/{tag}"),
        }

    def test_the_flag_accepts_the_usual_spellings(self, monkeypatch) -> None:
        from karting.api.app import read_only

        for value in ("1", "true", "TRUE", "yes", "on"):
            monkeypatch.setenv("PACE_READ_ONLY", value)
            assert read_only() is True
        for value in ("", "0", "false", "no", "off", "maybe"):
            monkeypatch.setenv("PACE_READ_ONLY", value)
            assert read_only() is False
