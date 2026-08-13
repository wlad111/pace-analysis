"""Unit tests for `karting.parsing.timeparse` (SPEC section 2)."""

from __future__ import annotations

import pytest

from karting.parsing.timeparse import format_duration, parse_duration, parse_gap

NBSP = " "
ZWSP = "​"
NNBSP = " "


# --------------------------------------------------------------------------- #
# parse_duration
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # plain seconds, 1..3 fractional digits normalised by padding
        ("28.872", 28872),
        ("28.87", 28870),
        ("28.8", 28800),
        ("28", 28000),
        ("0.001", 1),
        ("100.500", 100500),
        # minutes
        ("1:02.345", 62345),
        ("01:02.345", 62345),
        ("1:02", 62000),
        ("10:00.000", 600000),
        # hours
        ("1:02:03.456", 3723456),
        ("01:02:03.456", 3723456),
        ("2:00:00", 7200000),
        # apostrophe as the minute separator, comma as the decimal separator
        ("1'02.345", 62345),
        ("1'02,345", 62345),
        ("1’02.345", 62345),
        ("1:02,345", 62345),
        ("28,872", 28872),
        # double-prime for the fraction
        ('1\'02"345', 62345),
        # surrounding and exotic whitespace
        ("  28.872  ", 28872),
        (f"{NBSP}28.872{NBSP}", 28872),
        (f"{ZWSP}28.872{ZWSP}", 28872),
        (f"{NNBSP}1:02.345", 62345),
        ("&nbsp;28.872", 28872),
    ],
)
def test_parse_duration_values(text: str, expected: int) -> None:
    assert parse_duration(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "   ",
        "-",
        "--",
        "---",
        "–",  # en dash
        "—",  # em dash
        NBSP,
        ZWSP,
        "&nbsp;",
        f" {NBSP}&nbsp;{ZWSP} ",
        "-" + NBSP,
    ],
)
def test_parse_duration_missing(text: str | None) -> None:
    assert parse_duration(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "abc",
        "1 Lap",
        "2 Laps",
        "28.8721",  # more than three fractional digits: not a lap time
        "1:2:3:4.5",  # too many groups
        "12:34:56:78",
        "n/a",
        "26.0a2",
    ],
)
def test_parse_duration_rejects_garbage(text: str) -> None:
    assert parse_duration(text) is None


def test_parse_duration_is_padding_not_multiplication() -> None:
    """`28.8` is 28 s 800 ms, not 28 s 8 ms and not 28.8 * 1000 float noise."""
    assert parse_duration("28.8") == 28800
    assert parse_duration("28.08") == 28080
    assert parse_duration("28.008") == 28008
    assert isinstance(parse_duration("28.8"), int)


def test_parse_duration_signed() -> None:
    assert parse_duration("-1.500") == -1500
    assert parse_duration("+1.500") == 1500
    assert parse_duration("−0.250") == -250  # unicode minus


# --------------------------------------------------------------------------- #
# format_duration
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("ms", "expected"),
    [
        (None, "-"),
        (0, "0.000"),
        (1, "0.001"),
        (8100, "8.100"),
        (28872, "28.872"),
        (59999, "59.999"),
        (60000, "1:00.000"),
        (62345, "1:02.345"),
        (600000, "10:00.000"),
        (3599999, "59:59.999"),
        (3600000, "1:00:00.000"),
        (3723456, "1:02:03.456"),
        (-1500, "-1.500"),
    ],
)
def test_format_duration(ms: int | None, expected: str) -> None:
    assert format_duration(ms) == expected


@pytest.mark.parametrize(
    "text",
    ["0.000", "8.100", "28.872", "59.999", "1:00.000", "1:02.345", "10:00.000", "1:02:03.456"],
)
def test_round_trip_text_to_ms_to_text(text: str) -> None:
    assert format_duration(parse_duration(text)) == text


@pytest.mark.parametrize("ms", [0, 1, 999, 28872, 60000, 62345, 599999, 3723456, 86399999])
def test_round_trip_ms_to_text_to_ms(ms: int) -> None:
    assert parse_duration(format_duration(ms)) == ms


def test_round_trip_of_missing_value() -> None:
    assert format_duration(None) == "-"
    assert parse_duration(format_duration(None)) is None


# --------------------------------------------------------------------------- #
# parse_gap
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2.022", (2022, None)),
        ("21.300", (21300, None)),
        ("1:05.412", (65412, None)),
        ("1'05.412", (65412, None)),
        ("1 Lap", (None, 1)),
        ("2 Laps", (None, 2)),
        ("1lap", (None, 1)),
        ("10 LAPS", (None, 10)),
        ("2 Tours", (None, 2)),
        ("1 Tour", (None, 1)),
        ("3 круга", (None, 3)),
        ("5 кругов", (None, 5)),
        ("1 круг", (None, 1)),
        (f"1{NBSP}Lap", (None, 1)),
        # leader / unknown
        (None, (None, None)),
        ("", (None, None)),
        ("-", (None, None)),
        (NBSP, (None, None)),
        ("&nbsp;", (None, None)),
        # unreadable input must not invent a number
        ("later", (None, None)),
    ],
)
def test_parse_gap(text: str | None, expected: tuple[int | None, int | None]) -> None:
    assert parse_gap(text) == expected


def test_parse_gap_never_mixes_units() -> None:
    gap_ms, gap_laps = parse_gap("1 Lap")
    assert gap_ms is None and gap_laps == 1
    gap_ms, gap_laps = parse_gap("2.022")
    assert gap_ms == 2022 and gap_laps is None
