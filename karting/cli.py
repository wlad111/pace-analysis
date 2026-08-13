"""Command line interface for pace-analysis (SPEC §7).

Usage::

    python -m karting.cli import <paths...>      # .eml files and/or directories
    python -m karting.cli sessions
    python -m karting.cli show <session_id>
    python -m karting.cli events <session_id> [--detect]
    python -m karting.cli export <session_id> [--json [out.json]]
    python -m karting.cli serve [--host H] [--port P] [--reload]

The database is ``$PACE_DB`` (default ``data/pace.db``); ``--db`` overrides it
for the whole invocation, including a reloading dev server.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps CLI start-up lazy
    from karting.stats import LapFilter
    from karting.storage import Database

PROG: str = "python -m karting.cli"
#: Sentinel value of ``--json`` used without an argument: write to stdout.
STDOUT: str = "-"
#: Widest row id SQLite can hold; larger ids cannot address a row at all.
MAX_ROW_ID: int = 2**63 - 1
#: Joker / pit thresholds of SPEC §10.1, repeated here so that `--help` does not
#: have to import the API and the statistics packages.
DEFAULT_PIT_RATIO: float = 1.25
DEFAULT_JOKER_RATIO: float = 0.97


# --------------------------------------------------------------------------- #
# Small formatting helpers
# --------------------------------------------------------------------------- #


def _get(row: Mapping[str, Any], *keys: str) -> Any:
    """First present, non-None value among `keys`."""
    for key in keys:
        value = row.get(key)
        if value is not None:
            return value
    return None


def _fmt_ms(value: Any) -> str:
    """Milliseconds as ``28.872`` / ``1:02.345``; ``-`` when unknown."""
    from karting.parsing.timeparse import format_duration

    if value is None:
        return "-"
    try:
        return format_duration(int(round(float(value))))
    except (TypeError, ValueError):
        return str(value)


def _fmt_num(value: Any, digits: int = 2) -> str:
    """Fixed-point number, ``-`` when unknown."""
    if value is None:
        return "-"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def _fmt_delta_ms(value: Any) -> str:
    """Signed difference in seconds (``+1.885`` / ``-1.270``); ``-`` when unknown."""
    if value is None:
        return "-"
    try:
        return f"{float(value) / 1000.0:+.3f}"
    except (TypeError, ValueError):
        return str(value)


def _official_best_cell(row: Mapping[str, Any]) -> str:
    """Official best lap of a pace row, marked ``J`` when it is a joker lap."""
    marker = " J" if row.get("official_best_is_joker") else ""
    return _fmt_ms(row.get("official_best_ms")) + marker


def _fmt_dt(value: Any) -> str:
    """Session start as ``YYYY-MM-DD HH:MM``."""
    if value is None:
        return "-"
    text = str(value)
    return text.replace("T", " ")[:16]


def _fmt_gap(entry: Mapping[str, Any]) -> str:
    """Classification gap: a duration, ``N Laps`` or empty for the leader."""
    laps = _get(entry, "gap_laps")
    if laps:
        return f"{int(laps)} Lap" + ("s" if int(laps) != 1 else "")
    gap_ms = _get(entry, "gap_ms")
    return _fmt_ms(gap_ms) if gap_ms is not None else ""


def render_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    aligns: Sequence[str] | None = None,
) -> str:
    """Render a fixed-width terminal table."""
    count = len(headers)
    align = list(aligns) if aligns else ["<"] * count
    widths = [len(header) for header in headers]
    for row in rows:
        for index in range(count):
            cell = row[index] if index < len(row) else ""
            widths[index] = max(widths[index], len(cell))
    lines = ["  ".join(f"{headers[i]:{align[i]}{widths[i]}}" for i in range(count)).rstrip()]
    lines.append("  ".join("-" * widths[i] for i in range(count)))
    for row in rows:
        cells = [f"{(row[i] if i < len(row) else ''):{align[i]}{widths[i]}}" for i in range(count)]
        lines.append("  ".join(cells).rstrip())
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Shared plumbing
# --------------------------------------------------------------------------- #


def _open_db() -> Database:
    from karting.api.app import open_database

    return open_database()


def _database_label() -> str:
    from karting.api.app import database_path

    return database_path()


def _as_float(text: str, what: str) -> float:
    """`float(text)`, reported in the words of the option rather than Python's.

    On a `ValueError` argparse would fall back to ``invalid <converter> value``,
    which leaks the name of a private helper into a user-facing message; an
    `ArgumentTypeError` is printed verbatim instead.
    """
    try:
        return float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number ({what})") from None


def _as_int(text: str, what: str) -> int:
    """`int(text)`, reported in the words of the option (see `_as_float`)."""
    try:
        return int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number ({what})") from None


def _positive_float(text: str) -> float:
    value = _as_float(text, "expected a positive number")
    if not value > 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return value


def _pit_ratio(text: str) -> float:
    value = _as_float(text, "expected a ratio above 1, e.g. 1.25")
    if not value > 1.0:
        raise argparse.ArgumentTypeError(
            "must be greater than 1 (a pit lap is slower than the baseline)"
        )
    return value


def _joker_ratio(text: str) -> float:
    value = _as_float(text, "expected a ratio between 0 and 1, e.g. 0.97")
    if not 0.0 < value < 1.0:
        raise argparse.ArgumentTypeError(
            "must be between 0 and 1 (a joker lap is faster than the baseline)"
        )
    return value


def _row_id(text: str) -> int:
    """A session / lap id argparse can accept: a positive 64-bit integer."""
    value = _as_int(text, "expected a session id, e.g. 1")
    if not 1 <= value <= MAX_ROW_ID:
        raise argparse.ArgumentTypeError(f"must be between 1 and {MAX_ROW_ID}")
    return value


def _positive_int(text: str) -> int:
    value = _as_int(text, "expected a positive whole number")
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def _lap_filter(args: argparse.Namespace) -> LapFilter:
    """Build a `LapFilter` from the shared filter options."""
    from karting.stats import LapFilter

    base = LapFilter()
    if args.exclude_tags is None:
        tags = base.exclude_tags
    else:
        tags = frozenset(
            chunk.strip().casefold() for chunk in args.exclude_tags.split(",") if chunk.strip()
        )
    return LapFilter(
        exclude_tags=tags,
        mad_k=base.mad_k if args.mad_k is None else args.mad_k,
        drop_first_lap=not args.keep_first_lap,
        drop_slow_outliers=not args.keep_slow_outliers,
        drop_fast_outliers=bool(args.drop_fast_outliers),
        min_laps=base.min_laps if args.min_laps is None else args.min_laps,
    )


def _describe_filter(flt: LapFilter) -> str:
    tags = ",".join(sorted(flt.exclude_tags)) or "none"
    return (
        f"filter: mad_k={flt.mad_k:g}  drop_first_lap={flt.drop_first_lap}  "
        f"drop_slow_outliers={flt.drop_slow_outliers}  drop_fast_outliers={flt.drop_fast_outliers}  "
        f"min_laps={flt.min_laps}  exclude_tags={tags}"
    )


def collect_eml_files(paths: Iterable[str]) -> tuple[list[Path], list[str]]:
    """Expand CLI paths into `.eml` files (directories are walked recursively)."""
    found: list[Path] = []
    seen: set[Path] = set()
    missing: list[str] = []
    for raw in paths:
        path = Path(raw).expanduser()
        if path.is_dir():
            candidates = sorted(
                child for child in path.rglob("*") if child.is_file() and child.suffix.lower() == ".eml"
            )
        elif path.is_file():
            candidates = [path]
        else:
            missing.append(raw)
            continue
        for candidate in candidates:
            key = candidate.resolve()
            if key not in seen:
                seen.add(key)
                found.append(candidate)
    return found, missing


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def _print_import_line(status: str, name: str, details: Iterable[str] = ()) -> None:
    """One aligned block of the `import` report."""
    print(f"{f'[{status}]':<18} {name}")
    for line in details:
        print(f"{'':<18} {line}")


def cmd_import(args: argparse.Namespace) -> int:
    """Parse `.eml` files and store them, printing one report per file."""
    from karting.api.schemas import ImportReportOut
    from karting.parsing import parse_email_file

    files, missing = collect_eml_files(args.paths)
    for path in missing:
        print(f"[skipped]  {path}: no such file or directory", file=sys.stderr)
    if not files:
        print("No .eml files found.", file=sys.stderr)
        return 1

    counts: dict[str, int] = {"imported": 0, "merged": 0, "already_imported": 0, "failed": 0}
    print(f"Database: {_database_label()}")
    db = _open_db()
    try:
        for path in files:
            try:
                raw = path.read_bytes()
                parsed = parse_email_file(path, strict=args.strict)
            except Exception as exc:  # one bad file must not stop the batch
                counts["failed"] += 1
                _print_import_line("failed", path.name, [f"cannot parse ({type(exc).__name__}: {exc})"])
                continue
            try:
                report = db.import_parsed(parsed, raw_bytes=raw)
            except Exception as exc:
                counts["failed"] += 1
                _print_import_line(
                    "failed", path.name, [f"parsed, but not stored ({type(exc).__name__}: {exc})"]
                )
                continue
            out = ImportReportOut.from_report(path.name, report, session_name=parsed.session.name)
            counts[out.status] += 1
            details = [out.detail]
            if out.drivers_without_joker:
                details.append(f"no joker detected for: {', '.join(out.drivers_without_joker)}")
            if out.drivers_without_pit:
                details.append(
                    f"no pit stop detected for: {', '.join(out.drivers_without_pit)} "
                    f"(every driver must pit once -- tag it by hand)"
                )
            details += [f"warning: {warning}" for warning in out.warnings]
            details += [f"conflict: {conflict}" for conflict in out.conflicts]
            _print_import_line(out.status, path.name, details)
    finally:
        db.close()

    print(
        f"\n{len(files)} file(s): {counts['imported']} imported, {counts['merged']} merged, "
        f"{counts['already_imported']} already known, {counts['failed']} failed."
    )
    return 1 if counts["failed"] or missing else 0


def cmd_sessions(args: argparse.Namespace) -> int:
    """List every stored session."""
    db = _open_db()
    try:
        rows = db.list_sessions()
    finally:
        db.close()
    if not rows:
        print(f"No sessions in {_database_label()}. Import one with: {PROG} import <path>")
        return 0
    table = [
        [
            str(_get(row, "id", "session_id") or "-"),
            _fmt_dt(_get(row, "started_at", "date")),
            str(_get(row, "name") or "-"),
            str(_get(row, "code") or ""),
            str(_get(row, "track") or ""),
            str(_get(row, "category") or ""),
            str(_get(row, "club") or ""),
            str(_get(row, "drivers_count") or 0),
            str(_get(row, "laps_count") or 0),
        ]
        for row in rows
    ]
    print(
        render_table(
            ["ID", "STARTED", "SESSION", "CODE", "TRACK", "CATEGORY", "CLUB", "DRIVERS", "LAPS"],
            table,
            aligns=[">", "<", "<", "<", "<", "<", "<", ">", ">"],
        )
    )
    return 0


def _print_session_header(detail: Mapping[str, Any]) -> None:
    session = detail["session"]
    club = detail.get("club") or {}
    parts = [
        f"#{_get(session, 'id', 'session_id')}",
        str(_get(session, "name") or "?"),
    ]
    code = _get(session, "code")
    if code:
        parts.append(f"({code})")
    line = " ".join(parts)
    meta = [
        _fmt_dt(_get(session, "started_at")),
        str(_get(session, "track") or ""),
        str(_get(session, "category") or ""),
        str(_get(club, "name") or ""),
    ]
    print(line)
    print("  ".join(item for item in meta if item))


#: Orders `show` can print the pace table in; the default keeps the email's.
SHOW_SORTS: Final[tuple[str, ...]] = ("position", "best", "median")


def _rank_by(rows: Sequence[Mapping[str, Any]], key: str) -> dict[str, int]:
    """Drivers ranked 1..n by `key` ascending; a missing value is not ranked.

    Used for the two orders SPEC §10.4 calls the key value of the product: the
    official best lap (a joker lap for five drivers of the reference race) and
    the clean best lap.  Printing both turns "the ranking is wrong" into a
    number the reader can see -- WLAD111 is 5th on the official best and 2nd on
    the clean one -- instead of an arithmetic exercise.
    """
    ranked = sorted(
        (row for row in rows if row.get(key) is not None),
        key=lambda row: float(row[key]),
    )
    return {str(row.get("driver") or "?"): index + 1 for index, row in enumerate(ranked)}


def _sorted_pace_rows(
    rows: Sequence[Mapping[str, Any]], order: str
) -> list[Mapping[str, Any]]:
    """Pace rows in the requested order; missing values always sort last."""
    if order == "position":
        return list(rows)
    key = "best_ms" if order == "best" else "median_ms"

    def sort_key(row: Mapping[str, Any]) -> tuple[int, float, str]:
        value = row.get(key)
        if value is None:
            return (1, 0.0, str(row.get("driver") or ""))
        return (0, float(value), str(row.get("driver") or ""))

    return sorted(rows, key=sort_key)


def cmd_show(args: argparse.Namespace) -> int:
    """Print the classification and the pace metrics of one session."""
    from karting.api.app import load_session, pace_rows

    flt = _lap_filter(args)
    db = _open_db()
    try:
        detail = load_session(db, args.session_id)
    finally:
        db.close()
    if detail is None:
        print(f"Session {args.session_id} not found in {_database_label()}.", file=sys.stderr)
        return 1

    _print_session_header(detail)

    print("\nClassification")
    classification = [
        [
            str(_get(entry, "position", "rank") or "-"),
            str(_get(entry, "kart") or ""),
            str(_get(entry, "driver") or "?"),
            str(_get(entry, "laps_count", "laps") or 0),
            _fmt_gap(entry),
            _fmt_ms(_get(entry, "best_lap_ms")),
        ]
        for entry in detail["entries"]
    ]
    print(
        render_table(
            ["RNK", "KART", "DRIVER", "LAPS", "GAP", "BEST LAP"],
            classification,
            aligns=[">", ">", "<", ">", ">", ">"],
        )
    )

    print("\nPace")
    print(_describe_filter(flt))
    try:
        rows = pace_rows(detail["entries"], detail["laps"], flt)
    except ValueError as exc:
        print(f"Cannot compute pace statistics: {exc}", file=sys.stderr)
        return 1
    official_rank = _rank_by(rows, "official_best_ms")
    clean_rank = _rank_by(rows, "best_ms")
    rows = _sorted_pace_rows(rows, args.sort)
    pace_table = [
        [
            str(row.get("position") or "-"),
            str(official_rank.get(str(row.get("driver") or "?")) or "-"),
            str(clean_rank.get(str(row.get("driver") or "?")) or "-"),
            str(row.get("driver") or "?"),
            str(row.get("n_laps") or 0),
            str(row.get("n_used") or 0),
            _fmt_ms(row.get("best_ms")),
            _official_best_cell(row),
            _fmt_delta_ms(row.get("best_delta_ms")),
            _fmt_ms(row.get("median_ms")),
            _fmt_ms(row.get("mean_ms")),
            _fmt_num(row.get("std_ms"), 1),
            _fmt_num((row["cv"] * 100) if row.get("cv") is not None else None, 2),
            _fmt_ms(row.get("theoretical_best_ms")),
            _fmt_num(row.get("degradation_ms_per_lap"), 1),
            _fmt_num(row.get("degradation_p_value"), 3),
            _fmt_num(row.get("pace_delta_to_best_ms"), 0),
        ]
        for row in rows
    ]
    print(
        render_table(
            [
                "RNK",
                "#OFF",
                "#PACE",
                "DRIVER",
                "LAPS",
                "USED",
                "BEST",
                "OFFICIAL",
                "Δ",
                "MEDIAN",
                "MEAN",
                "STD",
                "CV%",
                "THEO",
                "DEG/LAP",
                "P",
                "GAP",
            ],
            pace_table,
            aligns=[">", ">", ">", "<", ">", ">", ">", ">", ">", ">", ">", ">", ">", ">", ">", ">", ">"],
        )
    )
    print("\nSTD/DEG/GAP in milliseconds; GAP is the gap to the best mean pace; "
          "CV% = std / mean; P = OLS p-value of the degradation slope.")
    print(
        "BEST is the clean best lap; OFFICIAL is the best lap printed in the email classification; "
        "Δ = BEST - OFFICIAL in seconds."
    )
    print(
        "RNK is the finishing position from the email; #OFF ranks the drivers by OFFICIAL and "
        "#PACE by BEST. Where #OFF and #PACE differ, the published order rewards the joker lap."
    )
    joker_best = [
        str(row.get("driver") or "?") for row in rows if row.get("official_best_is_joker")
    ]
    if joker_best:
        print(
            f"! The official best lap is a joker lap (marked J) for {len(joker_best)} of "
            f"{len(rows)} drivers: {', '.join(joker_best)}. The joker is a mandatory shortcut, "
            f"so those times are not race pace -- compare drivers by BEST, not by OFFICIAL."
        )
    return 0


def cmd_events(args: argparse.Namespace) -> int:
    """List the joker and pit laps of a session, optionally re-tagging them."""
    from karting.api.app import (
        EventThresholds,
        build_event_config,
        detect_session_events,
        event_report_payload,
        load_session,
    )

    thresholds = EventThresholds(
        pit_ratio=args.pit_ratio,
        joker_ratio=args.joker_ratio,
        one_per_driver=not args.allow_multiple,
    )
    db = _open_db()
    try:
        detail = load_session(db, args.session_id)
        if detail is None:
            print(f"Session {args.session_id} not found in {_database_label()}.", file=sys.stderr)
            return 1
        config = build_event_config(thresholds)
        report, laps = detect_session_events(db, args.session_id, config, persist=args.detect)
    finally:
        db.close()
    payload = event_report_payload(args.session_id, report, config, laps, persisted=args.detect)

    _print_session_header(detail)
    print("\nJoker and pit laps")
    print(
        f"thresholds: pit_ratio={payload.config.pit_ratio:g}  "
        f"joker_ratio={payload.config.joker_ratio:g}  "
        f"one_per_driver={payload.config.one_per_driver}"
    )
    if args.detect:
        print("Automatic tags rewritten; manual annotations were left untouched.")
    else:
        print("Read-only view; run with --detect to store these tags.")

    events = sorted(payload.events, key=lambda event: (event.driver, event.lap_number))
    if not events:
        print("No joker or pit lap detected with these thresholds.")
    else:
        print(
            render_table(
                ["DRIVER", "KIND", "LAP", "TIME", "Δ", "RATIO", "SECTOR", "CONF", "STATE"],
                [
                    [
                        event.driver,
                        event.kind,
                        str(event.lap_number),
                        _fmt_ms(event.time_ms),
                        _fmt_delta_ms(event.delta_ms),
                        _fmt_num(event.ratio, 3),
                        "-" if event.sector_index is None else f"S{event.sector_index + 1}",
                        _fmt_num(event.confidence, 2),
                        _event_state(event.applied, event.overridden_by_manual),
                    ]
                    for event in events
                ],
                aligns=["<", "<", ">", ">", ">", ">", ">", ">", "<"],
            )
        )
    # How many stops the race mandated is read off the field, not assumed: an
    # endurance race runs two, a sprint one.
    expected = payload.expected_pits
    demand = (
        "one joker and at least one pit stop per driver is expected"
        if expected is None
        else f"one joker and {expected} pit stop(s) per driver is what the field shows"
    )
    print(
        f"\n{payload.counts.get('joker', 0)} joker and {payload.counts.get('pit', 0)} pit lap(s) "
        f"for {payload.counts.get('drivers', 0)} driver(s); {demand}."
    )
    print(f"Without a joker: {', '.join(payload.drivers_without_joker) or 'none'}")
    print(f"Without a pit:   {', '.join(payload.drivers_without_pit) or 'none'}")
    if payload.pit_candidates:
        # The pit stop is mandatory, so a missing one always comes with the lap
        # to confirm; the numbers are there to judge it, not to trust it.
        print("\nProposed pit laps (not tagged; confirm the right one by hand)")
        print(
            render_table(
                ["DRIVER", "LAP", "TIME", "Δ", "RATIO", "SECTOR"],
                [
                    [
                        event.driver,
                        str(event.lap_number),
                        _fmt_ms(event.time_ms),
                        _fmt_delta_ms(event.delta_ms),
                        _fmt_num(event.ratio, 3),
                        "-" if event.sector_index is None else f"S{event.sector_index + 1}",
                    ]
                    for event in payload.pit_candidates
                ],
                aligns=["<", ">", ">", ">", ">", ">"],
            )
        )
    if payload.drivers_with_multiple:
        print(f"More than one event of a kind: {', '.join(payload.drivers_with_multiple)}")
    for warning in payload.warnings:
        print(f"warning: {warning}")
    if not payload.complete:
        print(
            "Every driver must take the joker once and pit once, so a missing event is an "
            "invitation to tag that lap by hand, not a detector failure."
        )
    return 0


def _event_state(applied: bool, overridden: bool) -> str:
    """How a detected event relates to the annotations stored for its lap."""
    if overridden:
        return "overridden by a manual tag"
    return "tagged" if applied else "not tagged"


def cmd_export(args: argparse.Namespace) -> int:
    """Dump one session (classification, laps, rankings, pace stats) as JSON."""
    from karting.api.app import jsonable, load_session, pace_rows
    from karting.api.schemas import FilterInfo

    flt = _lap_filter(args)
    db = _open_db()
    try:
        detail = load_session(db, args.session_id)
        if detail is None:
            print(f"Session {args.session_id} not found in {_database_label()}.", file=sys.stderr)
            return 1
        rankings = db.rankings(args.session_id) or {}
    finally:
        db.close()

    try:
        rows = pace_rows(detail["entries"], detail["laps"], flt)
    except ValueError as exc:
        print(f"Cannot compute pace statistics: {exc}", file=sys.stderr)
        return 1

    payload: dict[str, Any] = {
        "session": jsonable(detail["session"]),
        "club": jsonable(detail["club"]),
        "entries": jsonable(detail["entries"]),
        "laps": jsonable(detail["laps"]),
        "rankings": {
            "weekly_best": jsonable(rankings.get("weekly_best") or []),
            "track_record": jsonable(rankings.get("track_record") or []),
        },
        "stats": {"filter": FilterInfo.from_filter(flt).model_dump(), "drivers": jsonable(rows)},
    }
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    if args.json and args.json != STDOUT:
        target = Path(args.json).expanduser()
        if str(target.parent):
            target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text + "\n", encoding="utf-8")
        print(f"Wrote {target} ({target.stat().st_size} bytes).")
    else:
        print(text)
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Run the FastAPI application with uvicorn."""
    import uvicorn

    print(f"Database: {_database_label()}")
    print(f"Serving pace-analysis API on http://{args.host}:{args.port} (docs at /docs)")
    uvicorn.run("karting.api.app:app", host=args.host, port=args.port, reload=args.reload)
    return 0


# --------------------------------------------------------------------------- #
# Argument parsing
# --------------------------------------------------------------------------- #


def _add_filter_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("lap filter")
    group.add_argument(
        "--mad-k", type=_positive_float, default=None, metavar="K",
        help="Outlier threshold: median + K * 1.4826 * MAD (default 3).",
    )
    group.add_argument(
        "--min-laps", type=_positive_int, default=None, metavar="N",
        help="Minimum number of clean laps required for statistics (default 3).",
    )
    group.add_argument(
        "--exclude-tags", default=None, metavar="CSV",
        help="Comma separated tags to exclude; pass an empty string to keep every lap.",
    )
    group.add_argument("--keep-first-lap", action="store_true", help="Keep lap 1 (dropped by default).")
    group.add_argument(
        "--keep-slow-outliers", action="store_true", help="Keep laps slower than the robust threshold."
    )
    group.add_argument(
        "--drop-fast-outliers", action="store_true", help="Also drop suspiciously fast laps."
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the top level argument parser."""
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Import and analyse Apex Timing karting result emails.",
    )
    parser.add_argument(
        "--db", default=None, metavar="PATH",
        help="SQLite database to use for this run (overrides $PACE_DB, default data/pace.db).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="command")

    importer = subparsers.add_parser("import", help="Import .eml files and/or directories of them.")
    importer.add_argument("paths", nargs="+", metavar="PATH", help="Files or directories to scan for .eml.")
    importer.add_argument(
        "--strict", action="store_true", help="Fail a file whose parsing produced any warning."
    )
    importer.set_defaults(handler=cmd_import)

    sessions = subparsers.add_parser("sessions", help="List stored sessions.")
    sessions.set_defaults(handler=cmd_sessions)

    show = subparsers.add_parser("show", help="Show classification and pace metrics of a session.")
    show.add_argument("session_id", type=_row_id)
    show.add_argument(
        "--sort", choices=SHOW_SORTS, default="position", metavar="{" + ",".join(SHOW_SORTS) + "}",
        help="Order of the pace table: finishing position (default), clean best lap, or median.",
    )
    _add_filter_args(show)
    show.set_defaults(handler=cmd_show)

    events = subparsers.add_parser(
        "events", help="Show the joker and pit laps of a session (SPEC §10)."
    )
    events.add_argument("session_id", type=_row_id)
    events.add_argument(
        "--detect",
        action="store_true",
        help="Rewrite the automatic joker/pit tags of the session; manual tags are kept.",
    )
    events.add_argument(
        "--pit-ratio", type=_pit_ratio, default=DEFAULT_PIT_RATIO, metavar="R",
        help="A lap at least R times the driver's baseline is a pit lap "
        f"(default {DEFAULT_PIT_RATIO}).",
    )
    events.add_argument(
        "--joker-ratio", type=_joker_ratio, default=DEFAULT_JOKER_RATIO, metavar="R",
        help="A lap at most R times the driver's baseline is a joker lap "
        f"(default {DEFAULT_JOKER_RATIO}).",
    )
    events.add_argument(
        "--allow-multiple",
        action="store_true",
        help="Report every candidate instead of only the most extreme one of each kind per driver.",
    )
    events.set_defaults(handler=cmd_events)

    export = subparsers.add_parser("export", help="Export a session as JSON.")
    export.add_argument("session_id", type=_row_id)
    export.add_argument(
        "--json",
        nargs="?",
        const=STDOUT,
        default=None,
        metavar="PATH",
        help="Emit JSON: bare `--json` writes to stdout (the default), "
        "`--json PATH` writes to PATH.",
    )
    _add_filter_args(export)
    export.set_defaults(handler=cmd_export)

    serve = subparsers.add_parser("serve", help="Run the HTTP API.")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true", help="Reload on source changes (development).")
    serve.set_defaults(handler=cmd_serve)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: ``python -m karting.cli ...``."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.db:
        os.environ["PACE_DB"] = args.db
    try:
        code = int(args.handler(args))
        sys.stdout.flush()
        return code
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except BrokenPipeError:
        # Downstream pipe closed early (`... | head`): silence the shutdown flush.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 141
    except sqlite3.Error as exc:
        print(f"Database error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"I/O error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # a CLI reports a problem, it never dumps a traceback
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
