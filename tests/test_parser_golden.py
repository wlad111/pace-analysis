"""Golden test: the real Apex Timing email vs the hand-checked fixture.

The fixture keeps every duration as the literal string printed in the email.
The converter below is deliberately local and naive -- the golden test must not
verify the parser with the parser.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from bs4 import BeautifulSoup

from karting.models import ParsedEmail, ParseError, RankingKind
from karting.parsing import parse_email_file, parse_html
from karting.parsing.html_tables import (
    Cell,
    css_color,
    leaf_tables,
    make_soup,
    norm_key,
    normalize_text,
    parse_style,
)

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "final_a_expected.json"


def to_ms(text: str | None) -> int | None:
    """Local ``"1:02.345" -> 62345`` helper, independent of ``karting``."""
    if text is None:
        return None
    minutes, _, seconds = text.rpartition(":")
    whole, _, frac = seconds.partition(".")
    return (int(minutes or 0) * 60 + int(whole)) * 1000 + int(frac.ljust(3, "0") or "0")


def test_local_helper_is_sane() -> None:
    assert to_ms("28.872") == 28872
    assert to_ms("1:02.345") == 62345
    assert to_ms(None) is None


@pytest.fixture(scope="module")
def eml_path() -> Path:
    # The file name carries invisible U+200B characters: always glob for it.
    matches = sorted(ROOT.glob("*.eml"))
    assert len(matches) == 1, f"expected exactly one .eml in {ROOT}, got {matches}"
    return matches[0]


@pytest.fixture(scope="module")
def expected() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def parsed(eml_path: Path) -> ParsedEmail:
    return parse_email_file(eml_path)


@pytest.fixture(scope="module")
def html_body(eml_path: Path) -> str:
    import email
    import email.policy

    with eml_path.open("rb") as stream:
        message = email.message_from_binary_file(stream, policy=email.policy.default)
    return message.get_body(preferencelist=("html",)).get_content()


# --------------------------------------------------------------------------- #
# Invariants
# --------------------------------------------------------------------------- #


def test_reference_email_produces_no_warnings(parsed: ParsedEmail) -> None:
    assert parsed.warnings == []


def test_reference_email_leaves_nothing_unparsed(parsed: ParsedEmail) -> None:
    assert [block.header for block in parsed.unparsed] == []


def test_strict_mode_accepts_the_reference_email(eml_path: Path) -> None:
    assert parse_email_file(eml_path, strict=True).entries


# --------------------------------------------------------------------------- #
# Headers, club, session
# --------------------------------------------------------------------------- #


def test_provenance(parsed: ParsedEmail, expected: dict[str, Any], eml_path: Path) -> None:
    want = expected["provenance"]
    prov = parsed.provenance
    assert prov.subject == want["subject_normalized"]
    assert prov.from_name == want["from_name"]
    assert prov.from_email == want["from_email"]
    assert prov.recipient_email == want["recipient_email"]
    assert prov.recipient_nickname == want["recipient_nickname"]
    assert prov.sent_at is not None
    assert prov.sent_at.isoformat() == want["sent_at"]
    assert prov.message_id == "<6a70e36e.4d29c541.283f17.7b1eSMTPIN_ADDED_MISSING@mx.google.com>"
    assert prov.source_path == str(eml_path)
    assert prov.sha256 == hashlib.sha256(eml_path.read_bytes()).hexdigest()


def test_subject_normalisation_removed_the_invisible_characters(parsed: ParsedEmail) -> None:
    subject = parsed.provenance.subject or ""
    assert "​" not in subject
    assert " " not in subject
    assert subject.endswith("(FA)")


def test_club(parsed: ParsedEmail, expected: dict[str, Any]) -> None:
    want = expected["club"]
    assert parsed.club.name == want["name"]
    assert parsed.club.external_id == want["external_id"]
    assert parsed.club.website == want["website"]
    assert parsed.club.email == "info@primokarting.ru"


def test_session(parsed: ParsedEmail, expected: dict[str, Any]) -> None:
    want = expected["session"]
    session = parsed.session
    assert session.name == want["name"]
    assert session.code == want["code"]
    assert session.track == want["track"]
    assert session.category == want["category"]
    assert session.started_at is not None
    assert session.started_at.isoformat() == want["started_at"]
    assert session.started_at.tzinfo is None  # naive local time at the venue
    assert session.tz_name is None


def test_recipient_external_id(parsed: ParsedEmail, expected: dict[str, Any]) -> None:
    recipient = expected["provenance"]["recipient_nickname"]
    drivers = {entry.driver.nickname: entry.driver for entry in parsed.entries}
    assert drivers[recipient].external_id == expected["recipient_external_id"]
    # Nobody else gets an id: only the recipient is identified by the email.
    others = [name for name, driver in drivers.items() if driver.external_id is not None]
    assert others == [recipient]


def test_podium(parsed: ParsedEmail, expected: dict[str, Any]) -> None:
    assert parsed.podium == [tuple(item) for item in expected["podium"]]


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #


def test_classification(parsed: ParsedEmail, expected: dict[str, Any]) -> None:
    want = expected["classification"]
    assert len(parsed.entries) == len(want) == 6
    for entry, row in zip(parsed.entries, want, strict=True):
        assert entry.position == row["position"]
        assert entry.kart == row["kart"]
        assert entry.driver.nickname == row["driver"]
        assert entry.laps_count == row["laps"]
        assert entry.gap_ms == to_ms(row["gap"])
        assert entry.gap_laps is None
        assert entry.best_lap_ms == to_ms(row["best_lap"])


def test_leader_has_no_gap(parsed: ParsedEmail) -> None:
    leader = parsed.entries[0]
    assert leader.position == 1
    assert leader.gap_ms is None and leader.gap_laps is None


def test_cyrillic_nickname_survives(parsed: ParsedEmail) -> None:
    last = parsed.entries[-1]
    assert last.driver.nickname == "ИГОРЬ53"
    assert last.best_lap_ms == 28380


# --------------------------------------------------------------------------- #
# Lap chart
# --------------------------------------------------------------------------- #


def test_every_lap_of_every_driver(parsed: ParsedEmail, expected: dict[str, Any]) -> None:
    grouped = parsed.laps_by_driver()
    assert set(grouped) == set(expected["laps"])
    for nickname, times in expected["laps"].items():
        laps = grouped[nickname]
        assert len(laps) == len(times) == 20, nickname
        assert [lap.lap_number for lap in laps] == list(range(1, 21)), nickname
        assert [lap.time_ms for lap in laps] == [to_ms(text) for text in times], nickname


def test_first_lap_has_no_time_but_still_exists(parsed: ParsedEmail) -> None:
    for nickname, laps in parsed.laps_by_driver().items():
        assert laps[0].lap_number == 1, nickname
        assert laps[0].time_ms is None, nickname


def test_lap_numbering_continues_across_the_wrap(parsed: ParsedEmail) -> None:
    """Lap 11 is the first cell of the second row of the driver's block."""
    laps = {lap.lap_number: lap for lap in parsed.laps_by_driver()["KOLYA11"]}
    assert laps[10].time_ms == 27983
    assert laps[11].time_ms == 27899


def test_best_lap_flags(parsed: ParsedEmail, expected: dict[str, Any]) -> None:
    for nickname, laps in parsed.laps_by_driver().items():
        marked = [lap.lap_number for lap in laps if lap.is_best]
        assert marked == [expected["best_lap_number"][nickname]], nickname


def test_best_lap_flag_agrees_with_the_classification(parsed: ParsedEmail) -> None:
    grouped = parsed.laps_by_driver()
    for entry in parsed.entries:
        laps = grouped[entry.driver.nickname]
        best = next(lap for lap in laps if lap.is_best)
        assert best.time_ms == entry.best_lap_ms
        assert best.time_ms == min(lap.time_ms for lap in laps if lap.time_ms is not None)


# --------------------------------------------------------------------------- #
# Recipient sectors
# --------------------------------------------------------------------------- #


def test_recipient_sectors(parsed: ParsedEmail, expected: dict[str, Any]) -> None:
    want = expected["recipient_sector_table"]
    laps = {lap.lap_number: lap for lap in parsed.laps_by_driver()[want["driver"]]}
    assert len(want["rows"]) == 20
    for row in want["rows"]:
        lap = laps[row["lap"]]
        assert lap.sectors == [to_ms(text) for text in row["sectors"]], row["lap"]
        assert lap.time_ms == to_ms(row["time"]), row["lap"]


def test_sectors_add_up_to_the_lap_time(parsed: ParsedEmail, expected: dict[str, Any]) -> None:
    recipient = expected["recipient_sector_table"]["driver"]
    checked = 0
    for lap in parsed.laps_by_driver()[recipient]:
        if lap.time_ms is None:
            continue
        assert None not in lap.sectors
        assert sum(lap.sectors) == lap.time_ms, lap.lap_number
        checked += 1
    assert checked == 19  # every lap but the first, which has no time


def test_first_lap_keeps_its_sectors_without_inventing_a_time(parsed: ParsedEmail) -> None:
    first = parsed.laps_by_driver()["WLAD111"][0]
    assert first.time_ms is None
    assert first.sectors == [56053, 14243]


def test_only_the_recipient_has_sectors(parsed: ParsedEmail) -> None:
    for nickname, laps in parsed.laps_by_driver().items():
        if nickname == "WLAD111":
            continue
        assert all(lap.sectors == [] for lap in laps), nickname


# --------------------------------------------------------------------------- #
# History and rankings
# --------------------------------------------------------------------------- #


def test_history(parsed: ParsedEmail, expected: dict[str, Any]) -> None:
    want = expected["history"]
    assert len(parsed.history) == len(want) == 20
    for entry, row in zip(parsed.history, want, strict=True):
        assert entry.position == row["position"]
        assert entry.date is not None and entry.date.isoformat() == row["date"]
        assert entry.best_lap_ms == to_ms(row["best_lap"])
        assert entry.laps_count == row["laps"]
        assert entry.category == "SR5"


@pytest.mark.parametrize(
    ("kind", "key"),
    [(RankingKind.WEEKLY_BEST, "weekly_best"), (RankingKind.TRACK_RECORD, "track_records")],
)
def test_rankings(
    parsed: ParsedEmail, expected: dict[str, Any], kind: RankingKind, key: str
) -> None:
    want = expected[key]
    rows = [entry for entry in parsed.rankings if entry.kind is kind]
    assert len(rows) == len(want) == 6
    for entry, row in zip(rows, want, strict=True):
        assert entry.rank == row["rank"]
        assert entry.driver.nickname == row["driver"]
        assert entry.best_lap_ms == to_ms(row["best_lap"])
        assert entry.category == "SR5"


def test_ranking_tables_are_told_apart_by_their_caption(parsed: ParsedEmail) -> None:
    weekly = [e for e in parsed.rankings if e.kind is RankingKind.WEEKLY_BEST]
    records = [e for e in parsed.rankings if e.kind is RankingKind.TRACK_RECORD]
    assert weekly[0].driver.nickname == "KOLYA11"
    assert records[0].driver.nickname == "PHREEMAN"
    assert records[0].best_lap_ms == 20255


# --------------------------------------------------------------------------- #
# parse_html on the same body
# --------------------------------------------------------------------------- #


def test_parse_html_matches_the_eml_for_everything_but_the_headers(
    parsed: ParsedEmail, html_body: str
) -> None:
    from_html = parse_html(html_body)
    assert from_html.warnings == []
    assert from_html.session == parsed.session
    assert from_html.podium == parsed.podium
    assert from_html.club.external_id == "51"
    assert from_html.club.website == parsed.club.website
    # The nickname comes from the greeting when there is no To: header.
    assert from_html.provenance.recipient_nickname == "WLAD111"
    assert from_html.provenance.recipient_email == "driver@example.com"
    assert [lap.time_ms for lap in from_html.laps] == [lap.time_ms for lap in parsed.laps]


# --------------------------------------------------------------------------- #
# Synthetic structure tests
# --------------------------------------------------------------------------- #


def test_text_normalisation_and_style_reading() -> None:
    assert normalize_text("  a   b​c  ") == "a b c"
    assert norm_key(" Best Lap ") == "best lap"
    assert parse_style("WIDTH: 42px; Background-Color : #C0C0C0") == {
        "width": "42px",
        "background-color": "#c0c0c0",
    }
    assert css_color("#FFF") == "#ffffff"
    assert Cell(text="26.012", style="background-color:#C0C0C0;color:#000").is_highlighted is False
    assert Cell(text="26.012", style="BACKGROUND-COLOR:#515151").is_highlighted is True
    assert Cell(text="26.012", style="background : #515151 ; color : #ffffff").is_highlighted


def test_leaf_tables_skip_layout_wrappers() -> None:
    html = """
    <table><tr><td><table><tr><th>Rnk</th><th>Driver</th></tr>
    <tr><td>1</td><td>A</td></tr></table></td></tr></table>
    """
    tables = leaf_tables(make_soup(html))
    assert len(tables) == 1
    assert tables[0].signature() == ("rnk", "driver")
    assert [row.texts for row in tables[0].body_rows] == [["1", "A"]]


def _lap_chart_html(width: int) -> str:
    numbers = "".join(f"<th>{index}</th>" for index in range(1, width + 1))
    return f"""
    <html><body><table>
      <tr><th>Kart</th><th>Driver</th>{numbers}</tr>
      <tr><td>7</td><td>ALFA</td>
          <td>-</td><td>30.100</td><td>30.200</td><td>30.300</td><td>30.400</td></tr>
      <tr><td></td><td></td>
          <td>30.500</td>
          <td style="BACKGROUND-COLOR : #515151 ; COLOR : #FFFFFF">29.900</td>
          <td>30.700</td><td>30.800</td><td>30.900</td></tr>
      <tr><td></td><td></td>
          <td>31.000</td><td>31.100</td><td></td><td></td><td></td></tr>
    </table></body></html>
    """


def test_wrap_width_comes_from_the_header_not_from_a_constant() -> None:
    parsed = parse_html(_lap_chart_html(5))
    laps = parsed.laps_by_driver()["ALFA"]
    assert [lap.lap_number for lap in laps] == list(range(1, 13))
    assert laps[0].time_ms is None
    assert laps[5].lap_number == 6 and laps[5].time_ms == 30500
    assert laps[11].lap_number == 12 and laps[11].time_ms == 31100


def test_trailing_empty_cells_do_not_create_laps() -> None:
    parsed = parse_html(_lap_chart_html(5))
    assert len(parsed.laps_by_driver()["ALFA"]) == 12


def test_is_best_is_read_from_the_inline_style_whatever_its_spelling() -> None:
    parsed = parse_html(_lap_chart_html(5))
    marked = [lap.lap_number for lap in parsed.laps_by_driver()["ALFA"] if lap.is_best]
    assert marked == [7]


def _classification_html(header: list[str], rows: list[list[str]], caption: str) -> str:
    def row_html(cells: list[str], tag: str = "td") -> str:
        return "<tr>" + "".join(f"<{tag}>{cell}</{tag}>" for cell in cells) + "</tr>"

    body = "".join(row_html(cells) for cells in rows)
    return (
        f"<html><body><table><tr><td>{caption}</td></tr></table>"
        f"<table>{row_html(header, 'th')}{body}</table></body></html>"
    )


@pytest.mark.parametrize(
    ("header", "caption"),
    [
        (
            ["Clt", "Kart", "Pilote", "Tours", "Ecart", "Meilleur tour"],
            "COURSE - Final A (FA) - 03.08.2026 à 21:40 (Piste)",
        ),
        (
            ["Поз", "Карт", "Пилот", "Круги", "Отставание", "Лучший круг"],
            "ГОНКА - Финал A (FA) - 03.08.2026 в 21:40 (Трасса)",
        ),
        (
            ["Rnk", "Kart", "Driver", "Laps", "Gap", "Best lap"],
            "RACE - Final A (FA) - 03.08.2026 at 21:40 (Track)",
        ),
    ],
)
def test_tables_are_classified_by_header_synonyms(header: list[str], caption: str) -> None:
    html = _classification_html(
        header,
        [["1", "11", "KOLYA11", "20", "", "26.012"], ["2", "2", "ИГОРЬ53", "20", "1 Lap", "26.788"]],
        caption,
    )
    parsed = parse_html(html)
    assert parsed.unparsed == []
    assert [entry.driver.nickname for entry in parsed.entries] == ["KOLYA11", "ИГОРЬ53"]
    assert parsed.entries[0].gap_ms is None and parsed.entries[0].gap_laps is None
    assert parsed.entries[1].gap_laps == 1 and parsed.entries[1].gap_ms is None
    assert parsed.entries[1].best_lap_ms == 26788
    assert parsed.session.code == "FA"
    assert parsed.session.started_at is not None
    assert parsed.session.started_at.isoformat() == "2026-08-03T21:40:00"
    assert parsed.session.name.endswith("Final A") or parsed.session.name.endswith("Финал A")


def test_session_name_keeps_its_internal_dash() -> None:
    html = _classification_html(
        ["Rnk", "Kart", "Driver", "Laps", "Gap", "Best lap"],
        [["1", "1", "A", "1", "", "26.012"]],
        "PRIMO GARA - Final A (FA) - 03.08.2026 at 21:40 (Karting track)",
    )
    session = parse_html(html).session
    assert session.name == "PRIMO GARA - Final A"
    assert session.code == "FA"
    assert session.track == "Karting track"


def test_unknown_data_table_goes_to_unparsed() -> None:
    html = """
    <html><body><table>
      <tr><th>Weather</th><th>Temperature</th></tr>
      <tr><td>Sunny</td><td>28</td></tr>
    </table></body></html>
    """
    parsed = parse_html(html)
    assert len(parsed.unparsed) == 1
    block = parsed.unparsed[0]
    assert block.header == ["Weather", "Temperature"]
    assert block.rows == [["Sunny", "28"]]
    assert any("unclassified" in warning for warning in parsed.warnings)


def test_captions_outside_tables_are_still_read() -> None:
    html = """
    <html><body>
      <div>Hello NEO, your results !</div>
      <p>DEMO CUP - Sprint 1 (S1) - 03.08.2026 at 21:40 (Karting track)</p>
      <table>
        <tr><th>Rnk</th><th>Kart</th><th>Driver</th><th>Laps</th><th>Gap</th><th>Best lap</th></tr>
        <tr><td>1</td><td>3</td><td>NEO</td><td>1</td><td></td><td>30.100</td></tr>
      </table>
    </body></html>
    """
    parsed = parse_html(html)
    assert parsed.provenance.recipient_nickname == "NEO"
    assert parsed.session.name == "DEMO CUP - Sprint 1"
    assert parsed.session.code == "S1"


_INCONSISTENT_EMAIL = """
<html><body>
<table><tr><td>Hello ALFA, your results !</td></tr></table>
<table><tr><td>DEMO - Race (R1) - 01.02.2026 at 10:00 (Karting track)</td></tr></table>
<table>
  <tr><th>Rnk</th><th>Kart</th><th>Driver</th><th>Laps</th><th>Gap</th><th>Best lap</th></tr>
  <tr><td>1</td><td>3</td><td>ALFA</td><td>4</td><td></td><td>30.100</td></tr>
</table>
<table>
  <tr><th>Kart</th><th>Driver</th><th>1</th><th>2</th><th>3</th></tr>
  <tr><td>3</td><td>ALFA</td>
      <td>-</td>
      <td style="background-color:#515151;color:#FFFFFF">30.100</td>
      <td>30.500</td></tr>
</table>
<table><tr><td>Your lap time DEMO</td></tr></table>
<table>
  <tr><th>Lap</th><th>S1</th><th>S2</th><th></th><th>Time</th></tr>
  <tr><td>1</td><td>15.000</td><td>15.100</td><td></td><td>-</td></tr>
  <tr><td>2</td><td>15.000</td><td>15.100</td><td></td><td>30.100</td></tr>
  <tr><td>3</td><td>15.000</td><td>15.100</td><td></td><td>30.900</td></tr>
</table>
</body></html>
"""


def test_inconsistencies_are_reported_but_do_not_stop_the_parse() -> None:
    parsed = parse_html(_INCONSISTENT_EMAIL)
    joined = " | ".join(parsed.warnings)
    # classification says 4 laps, the chart holds 3
    assert "classification says 4 laps" in joined
    # lap 3: 30.500 in the chart, 30.900 in the sector table
    assert "lap chart says 30500, sector table says 30900" in joined
    # lap 3: 15.000 + 15.100 != 30.500
    assert "S1+S2 sum to 30100" in joined
    # the raw data survives untouched
    laps = parsed.laps_by_driver()["ALFA"]
    assert [lap.time_ms for lap in laps] == [None, 30100, 30500]
    assert [lap.sectors for lap in laps] == [[15000, 15100]] * 3
    assert parsed.session.category == "DEMO"


def test_strict_mode_raises_on_inconsistencies() -> None:
    with pytest.raises(ParseError):
        parse_html(_INCONSISTENT_EMAIL, strict=True)


# --------------------------------------------------------------------------- #
# Degradation
# --------------------------------------------------------------------------- #


def _drop_sector_table(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for table in soup.find_all("table"):
        if table.find("table") is not None:
            continue
        labels = [cell.get_text(strip=True) for cell in table.find_all(["td", "th"])[:5]]
        if labels[:3] == ["Lap", "S1", "S2"]:
            table.decompose()
            return str(soup)
    raise AssertionError("sector table not found in the reference email")


def test_missing_sector_table_warns_instead_of_crashing(html_body: str) -> None:
    parsed = parse_html(_drop_sector_table(html_body))
    assert parsed.warnings, "a missing sector table must be reported"
    assert any("sector" in warning for warning in parsed.warnings)
    # Everything else is still parsed.
    assert len(parsed.entries) == 6
    assert len(parsed.laps) == 120
    assert all(lap.sectors == [] for lap in parsed.laps)
    assert parsed.session.name == "PRIMO GARA - Final A"


def test_strict_mode_raises_on_a_degraded_email(html_body: str) -> None:
    with pytest.raises(ParseError):
        parse_html(_drop_sector_table(html_body), strict=True)


def test_truncated_html_does_not_crash(html_body: str) -> None:
    cut = html_body.index("Your last sessions")
    parsed = parse_html(html_body[:cut])
    assert parsed.warnings
    assert parsed.history == []
    assert parsed.rankings == []
    assert len(parsed.entries) == 6  # the classification survived the cut


def test_empty_html_does_not_crash() -> None:
    parsed = parse_html("<html><body></body></html>")
    assert parsed.entries == []
    assert parsed.laps == []
    assert parsed.warnings
