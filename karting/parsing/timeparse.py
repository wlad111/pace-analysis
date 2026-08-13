"""Parsing and formatting of lap times, sector times and gaps.

Every duration is handled as an integer number of milliseconds.  Anything the
timing system prints for "no time" (an empty cell, ``-``, ``--``, ``&nbsp;``)
becomes ``None``; it must never silently become ``0``.

Accepted input forms (see SPEC section 2)::

    28.872      1:02.345    1'02.345    1:02,345
    01:02.345   1:02:03.456 28.8        28.87

The fractional part may hold 1..3 digits and is normalised by padding on the
right (``28.8`` -> 28800 ms), never by multiplying.
"""

from __future__ import annotations

import re

__all__ = ["parse_duration", "format_duration", "parse_gap", "is_missing"]


# Characters that browsers render as a space but ``str.strip`` ignores.
_SPACE_CHARS = (
    # NBSP, EN/EM quads, thin/hair spaces, zero-width chars, BOM.
    "\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007"
    "\u2008\u2009\u200a\u200b\u200c\u200d\u202f\u205f\u3000\ufeff"
)
_SPACE_TRANSLATION = {ord(char): " " for char in _SPACE_CHARS}

# Every flavour of dash Apex may print for "no time".
_DASHES = "-‐‑‒–—―−"

# ``'`` and friends separate minutes from seconds, ``"`` separates the fraction.
_MINUTE_MARKS = "'’ʼ′"
_SECOND_MARKS = '"”″'

_DURATION_RE = re.compile(
    r"^(?P<sign>[+-])?(?P<parts>\d{1,3}(?::\d{1,3}){0,2})(?:\.(?P<frac>\d{1,3}))?$"
)

_LAPS_RE = re.compile(
    r"^[+-]?(?P<count>\d{1,3})\s*"
    r"(?:laps?|tours?|tr\.?|runden?|rd\.?|круг(?:а|ов)?|кр\.?)$"
)


def _clean(text: str) -> str:
    """Collapse exotic whitespace and unify decimal/minute separators."""
    value = text.replace("&nbsp;", " ").translate(_SPACE_TRANSLATION)
    value = " ".join(value.split())
    for mark in _MINUTE_MARKS:
        value = value.replace(mark, ":")
    for mark in _SECOND_MARKS:
        value = value.replace(mark, ".")
    return value.replace(",", ".")


def is_missing(text: str | None) -> bool:
    """True when the cell explicitly means "no time" (empty or a dash)."""
    if text is None:
        return True
    value = _clean(text).replace(" ", "")
    if not value:
        return True
    return all(char in _DASHES for char in value)


def parse_duration(text: str | None) -> int | None:
    """Convert a duration such as ``"1:02.345"`` into milliseconds.

    Returns ``None`` for missing values (``None``, empty, ``-``, ``--``,
    ``&nbsp;``) and for anything that does not look like a duration at all.
    """
    if text is None:
        return None
    value = _clean(text).replace(" ", "")
    if not value or all(char in _DASHES for char in value):
        return None

    # Normalise the unicode dashes a sign may be written with.
    if value[0] in _DASHES:
        value = "-" + value[1:]

    match = _DURATION_RE.match(value)
    if match is None:
        return None

    parts = [int(part) for part in match.group("parts").split(":")]
    seconds = 0
    for part in parts:  # hours -> minutes -> seconds, read left to right
        seconds = seconds * 60 + part

    frac = match.group("frac") or ""
    milliseconds = int(frac.ljust(3, "0")) if frac else 0

    total = seconds * 1000 + milliseconds
    return -total if match.group("sign") == "-" else total


def format_duration(ms: int | None) -> str:
    """Render milliseconds the way Apex does: ``28.872`` / ``1:02.345``."""
    if ms is None:
        return "-"
    sign = "-" if ms < 0 else ""
    total = abs(int(ms))
    millis = total % 1000
    seconds_total = total // 1000
    seconds = seconds_total % 60
    minutes = (seconds_total // 60) % 60
    hours = seconds_total // 3600
    if hours:
        return f"{sign}{hours}:{minutes:02d}:{seconds:02d}.{millis:03d}"
    if seconds_total >= 60:
        return f"{sign}{minutes}:{seconds:02d}.{millis:03d}"
    return f"{sign}{seconds}.{millis:03d}"


def parse_gap(text: str | None) -> tuple[int | None, int | None]:
    """Split a classification gap into ``(gap_ms, gap_laps)``.

    ``"2.022"`` -> ``(2022, None)``, ``"1 Lap"`` -> ``(None, 1)``,
    empty/leader -> ``(None, None)``.
    """
    if text is None:
        return (None, None)
    value = _clean(text)
    if is_missing(value):
        return (None, None)
    laps = _LAPS_RE.match(value.casefold())
    if laps is not None:
        return (None, int(laps.group("count")))
    return (parse_duration(value), None)
