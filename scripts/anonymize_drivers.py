#!/usr/bin/env python
"""Replace real personal names in the database with pseudonyms.

Apex Timing lets a driver register under anything, so most entries are handles
(``KOLYA11``, ``TWG``) that the club already publishes in its own results and
records. A few are a real first name and surname, which is personal data of
people who never agreed to appear on a public dashboard next to their lap
times. This renames those and leaves the handles alone.

The rename happens in the ``driver`` table only: every other table refers to a
driver by id, so lap times, positions and annotations follow automatically and
no statistic changes. Run it against a copy first and compare the numbers —
``--check`` does exactly that without writing.

    python scripts/anonymize_drivers.py data/pace.db --check
    python scripts/anonymize_drivers.py data/pace.db --apply
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path

#: A nickname that looks like "FIRST LAST": two or more words, letters only.
#: Handles with digits (``НИКИТОС111``) or punctuation (``KRIS.SAFARLI``) are
#: not personal names and stay untouched.
NAME_LIKE = re.compile(r"^[^\W\d_]+(?:\s+[^\W\d_]+)+$", re.UNICODE)


def looks_like_a_person(nickname: str) -> bool:
    return bool(NAME_LIKE.match(nickname.strip()))


def pseudonym(index: int) -> str:
    """Stable, obviously fake, and clearly not a nickname someone might own."""
    return f"DRIVER{index:02d}"


def plan(connection: sqlite3.Connection) -> list[tuple[int, str, str]]:
    rows = connection.execute("SELECT id, nickname FROM driver ORDER BY id").fetchall()
    taken = {str(nickname).casefold() for _, nickname in rows}
    changes: list[tuple[int, str, str]] = []
    counter = 1
    for driver_id, nickname in rows:
        if not looks_like_a_person(str(nickname)):
            continue
        while pseudonym(counter).casefold() in taken:
            counter += 1
        replacement = pseudonym(counter)
        taken.add(replacement.casefold())
        counter += 1
        changes.append((int(driver_id), str(nickname), replacement))
    return changes


def fingerprint(connection: sqlite3.Connection) -> list[tuple]:
    """Lap totals per driver id: must be identical before and after."""
    return connection.execute(
        "SELECT driver_id, count(*), coalesce(sum(time_ms), 0) FROM lap GROUP BY driver_id"
        " ORDER BY driver_id"
    ).fetchall()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("--apply", action="store_true", help="write the changes")
    parser.add_argument("--check", action="store_true", help="show what would change")
    args = parser.parse_args()

    if not args.apply and not args.check:
        parser.error("pass --check to preview or --apply to write")
    if not args.database.exists():
        parser.error(f"{args.database} does not exist")

    connection = sqlite3.connect(args.database)
    connection.execute("PRAGMA foreign_keys = ON")
    changes = plan(connection)

    if not changes:
        print("Nothing to rename: no nickname looks like a personal name.")
        return 0

    width = max(len(old) for _, old, _ in changes)
    for _, old, new in changes:
        print(f"  {old:<{width}}  ->  {new}")
    print(f"{len(changes)} driver(s)")

    if not args.apply:
        print("\nPreview only. Re-run with --apply to write.")
        return 0

    before = fingerprint(connection)
    with connection:
        connection.executemany(
            "UPDATE driver SET nickname = ? WHERE id = ?",
            [(new, driver_id) for driver_id, _, new in changes],
        )
    after = fingerprint(connection)

    # The rename touches one column of one table; if a lap moved, something is
    # wrong with the schema's assumptions and the change must not be trusted.
    if before != after:
        print("lap totals changed — rolling back is not possible, restore a backup", file=sys.stderr)
        return 1

    remaining = [
        nickname
        for (nickname,) in connection.execute("SELECT nickname FROM driver")
        if looks_like_a_person(str(nickname))
    ]
    if remaining:
        print(f"still personal-looking: {remaining}", file=sys.stderr)
        return 1

    print("\nDone. Lap counts and totals per driver are unchanged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
