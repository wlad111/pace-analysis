"""Parser for Apex Timing result emails.

The email carries one race: a classification, a lap chart (every lap of every
driver), the recipient's own laps with sector splits, the recipient's session
history and two truncated leaderboards.  Everything is recognised by the
*header signature* of the leaf tables, never by their position in the
document, so extra decorative tables cannot shift the parse.

Nothing is invented and nothing is dropped silently: inconsistencies land in
``ParsedEmail.warnings`` (or raise :class:`ParseError` when ``strict=True``)
and unclassified data tables land in ``ParsedEmail.unparsed``.
"""

from __future__ import annotations

import email
import email.policy
import hashlib
import io
import re
from dataclasses import dataclass, field
from datetime import date as date_cls
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup

from karting.models import (
    Club,
    Driver,
    HistoryEntry,
    Lap,
    ParsedEmail,
    ParseError,
    Provenance,
    RankingEntry,
    RankingKind,
    Session,
    SessionEntry,
    UnparsedBlock,
)

from .html_tables import (
    Cell,
    LeafTable,
    Row,
    leaf_tables,
    make_soup,
    norm_key,
    normalize_text,
)
from .timeparse import is_missing, parse_duration, parse_gap

__all__ = ["parse_email_file", "parse_email_bytes", "parse_html"]


# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #

#: Header label -> canonical column role.  EN / FR / RU / DE synonyms.
ROLE_SYNONYMS: dict[str, frozenset[str]] = {
    "rank": frozenset(
        {
            "rnk", "rank", "clt", "class", "classement", "pos", "pos.",
            "position", "platz", "#", "n°", "no", "поз", "поз.", "место", "№",
        }
    ),
    "kart": frozenset({"kart", "cart", "kart n°", "n° kart", "карт", "№ карта"}),
    "driver": frozenset(
        {
            "driver", "pilote", "pilot", "fahrer", "name", "nom", "team",
            "пилот", "гонщик", "участник", "имя",
        }
    ),
    "laps": frozenset({"laps", "tours", "runden", "круги", "кругов", "круг."}),
    "gap": frozenset(
        {"gap", "écart", "ecart", "abstand", "int", "отставание", "разрыв", "отрыв"}
    ),
    "best_lap": frozenset(
        {
            "best lap", "best time", "best", "meilleur tour", "meilleur temps",
            "beste runde", "лучший круг", "лучшее время", "рекорд",
        }
    ),
    "lap_number": frozenset({"lap", "tour", "runde", "круг", "№ круга"}),
    "time": frozenset({"time", "temps", "zeit", "время"}),
    "date": frozenset({"date", "datum", "дата"}),
}

_ROLE_BY_LABEL: dict[str, str] = {}
for _role, _labels in ROLE_SYNONYMS.items():
    for _label in _labels:
        if _label in _ROLE_BY_LABEL:  # pragma: no cover - guards the table above
            raise RuntimeError(
                f"ambiguous header synonym {_label!r}: "
                f"{_ROLE_BY_LABEL[_label]} vs {_role}"
            )
        _ROLE_BY_LABEL[_label] = _role

#: Section captions printed above the tables, used for the ranking kind and
#: for the kart category suffix ("Best times of the week SR5" -> "SR5").
SECTION_TITLES: dict[str, tuple[str, ...]] = {
    "personal_laps": (
        "your lap time", "your lap times", "vos temps au tour",
        "ваши круги", "ваше время круга", "ваши времена кругов",
    ),
    "history": (
        "your last sessions", "vos dernières sessions", "vos dernieres sessions",
        "ваши последние сессии", "последние сессии",
    ),
    "weekly_best": (
        "best times of the week", "meilleurs temps de la semaine",
        "лучшие времена недели", "лучшее время недели",
    ),
    "track_record": (
        "track records", "records de la piste", "record de la piste",
        "рекорды трассы", "рекорд трассы",
    ),
}

_SECTOR_RE = re.compile(r"^(?:s|с|sec|secteur|сектор)\s*(\d{1,2})$")

_SESSION_LINE_RE = re.compile(
    r"^(?P<name>.+)\s+[-–—]\s+"
    r"(?P<day>\d{1,2})[./-](?P<month>\d{1,2})[./-](?P<year>\d{2,4})"
    r"\s+(?:à|a|at|в|um|alle|el)\s+"
    r"(?P<hour>\d{1,2})[:.](?P<minute>\d{2})"
    r"(?:\s*\((?P<track>[^()]*)\))?\s*$",
    re.IGNORECASE,
)
_SESSION_CODE_RE = re.compile(r"\(([^()]+)\)\s*$")
_GREETING_RE = re.compile(
    r"^(?:hello|hi|bonjour|salut|hallo|guten tag|привет|здравствуйте|здравствуй)"
    r"[\s,]+(?P<nick>.+?)\s*[,!]",
    re.IGNORECASE,
)
_DATE_RE = re.compile(r"^(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})$")
_INT_RE = re.compile(r"^-?\d+$")
_SOCIAL_HOSTS = ("facebook", "twitter", "instagram", "youtube", "vk.com", "t.me")


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _to_int(text: str) -> int | None:
    value = normalize_text(text).replace(" ", "")
    return int(value) if _INT_RE.match(value) else None


def _to_date(text: str) -> date_cls | None:
    match = _DATE_RE.match(normalize_text(text))
    if match is None:
        return None
    day, month, year = (int(part) for part in match.groups())
    if year < 100:
        year += 2000
    try:
        return date_cls(year, month, day)
    except ValueError:
        return None


def _column_roles(header: Row) -> dict[str, int]:
    """Map canonical role -> first column index carrying it."""
    roles: dict[str, int] = {}
    for index, cell in enumerate(header.cells):
        role = _ROLE_BY_LABEL.get(cell.key)
        if role is not None and role not in roles:
            roles[role] = index
    return roles


def _sector_columns(header: Row) -> dict[int, str]:
    """Map column index -> sector label for ``S1``-like columns."""
    sectors: dict[int, str] = {}
    for index, cell in enumerate(header.cells):
        match = _SECTOR_RE.match(cell.key)
        if match is not None:
            sectors[index] = cell.text
    return sectors


def _numeric_columns(header: Row) -> dict[int, int]:
    """Map column index -> printed lap number for ``1 | 2 | ...`` headers."""
    numbers: dict[int, int] = {}
    for index, cell in enumerate(header.cells):
        if cell.text.isdigit():
            numbers[index] = int(cell.text)
    return numbers


@dataclass(slots=True)
class _Block:
    """One leaf table together with everything needed to classify it."""

    table: LeafTable
    kind: str
    roles: dict[str, int] = field(default_factory=dict)
    sectors: dict[int, str] = field(default_factory=dict)
    numbers: dict[int, int] = field(default_factory=dict)


def _classify(table: LeafTable) -> _Block:
    """Decide what a leaf table is, using its header signature only."""
    filled = table.filled_rows

    if len(filled) < 2:
        return _Block(table, "text")

    texts = table.texts
    if (
        len(texts) == 2
        and all(len([c for c in row.cells if c.text]) <= 1 for row in table.rows)
        and re.fullmatch(r"\d{1,2}", texts[0]) is not None
        and not texts[1].isdigit()
        and parse_duration(texts[1]) is None
    ):
        return _Block(table, "podium")

    header = filled[0]
    roles = _column_roles(header)
    sectors = _sector_columns(header)
    numbers = _numeric_columns(header)

    if "kart" in roles and "driver" in roles and numbers:
        return _Block(table, "lap_chart", roles, sectors, numbers)
    if {"rank", "driver", "best_lap"} <= roles.keys() and "date" not in roles:
        if {"kart", "gap", "laps"} & roles.keys():
            return _Block(table, "classification", roles, sectors, numbers)
        return _Block(table, "ranking", roles, sectors, numbers)
    if {"rank", "date", "best_lap"} <= roles.keys():
        return _Block(table, "history", roles, sectors, numbers)
    if "lap_number" in roles and ("time" in roles or sectors):
        return _Block(table, "personal_laps", roles, sectors, numbers)
    return _Block(table, "unknown", roles, sectors, numbers)


def _match_section(text: str) -> tuple[str, str | None] | None:
    """Recognise a section caption -> ``(section key, category suffix)``."""
    key = norm_key(text)
    for section, prefixes in SECTION_TITLES.items():
        for prefix in prefixes:
            if key.startswith(prefix):
                suffix = normalize_text(text[len(prefix) :]).strip(" :-")
                return section, suffix or None
    return None


# --------------------------------------------------------------------------- #
# The parser
# --------------------------------------------------------------------------- #


class _EmailParser:
    """Stateful worker; one instance per parsed document."""

    def __init__(self, html: str, provenance: Provenance) -> None:
        self.soup: BeautifulSoup = make_soup(html)
        self.provenance = provenance
        self.warnings: list[str] = []
        self.unparsed: list[UnparsedBlock] = []
        self.drivers: dict[str, Driver] = {}
        self.entries: list[SessionEntry] = []
        self.laps: list[Lap] = []
        self.rankings: list[RankingEntry] = []
        self.history: list[HistoryEntry] = []
        self.podium: list[tuple[int, str]] = []
        self.karts: dict[str, str] = {}
        self.category: str | None = None
        self.session_line: str = ""
        self.session_match: re.Match[str] | None = None
        self.recipient: str | None = normalize_text(provenance.recipient_nickname) or None
        self.recipient_external_id: str | None = None
        self.club_external_id: str | None = None
        self.website: str | None = None
        self.personal_rows: list[tuple[int, list[int | None], int | None, bool]] = []
        self.personal_labels: list[str] = []
        self.seen_kinds: set[str] = set()

    # -- infrastructure ---------------------------------------------------- #

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def driver(self, nickname: str) -> Driver:
        """Registry so that every mention of a nickname shares one object."""
        name = normalize_text(nickname)
        driver = self.drivers.get(name)
        if driver is None:
            driver = Driver(nickname=name)
            self.drivers[name] = driver
        return driver

    # -- links -------------------------------------------------------------- #

    def read_links(self) -> None:
        """Club website plus the ids hidden in the unsubscribe URL."""
        for anchor in self.soup.find_all("a"):
            href = anchor.get("href")
            if not isinstance(href, str):
                continue
            href = normalize_text(href)
            split = urlsplit(href)
            if split.scheme not in ("http", "https"):
                continue
            query = parse_qs(split.query)
            if "center" in query or "client" in query or "unsubscribe" in split.query:
                self.club_external_id = self.club_external_id or _first(query.get("center"))
                self.recipient_external_id = self.recipient_external_id or _first(
                    query.get("client")
                )
                continue
            host = split.netloc.casefold()
            if self.website is None and not any(bad in host for bad in _SOCIAL_HOSTS):
                self.website = href
        if self.club_external_id is None:
            self.warn("unsubscribe link with center= not found; club external id unknown")

        if self.provenance.recipient_email is None:
            for anchor in self.soup.find_all("a", href=True):
                href = str(anchor["href"])
                if href.casefold().startswith("mailto:"):
                    self.provenance.recipient_email = normalize_text(href[7:]) or None
                    break

    # -- captions ----------------------------------------------------------- #

    def read_caption(self, text: str) -> None:
        """Consume a caption-like table: session line, greeting or section."""
        if not text:
            return
        if self.session_match is None:
            match = _SESSION_LINE_RE.match(text)
            if match is not None:
                self.session_line, self.session_match = text, match
        if self.recipient is None:
            greeting = _GREETING_RE.match(text)
            if greeting is not None:
                self.recipient = normalize_text(greeting.group("nick"))
        section = _match_section(text)
        if section is not None and self.category is None and section[1]:
            self.category = section[1]

    def read_loose_captions(self) -> None:
        """Fallback for layouts that put the captions outside of a table."""
        for tag in self.soup.find_all(["p", "div", "h1", "h2", "h3", "h4", "font", "span"]):
            if self.session_match is not None and self.recipient is not None:
                return
            self.read_caption(normalize_text(tag.get_text(" ", strip=True)))

    # -- session ------------------------------------------------------------ #

    def build_session(self) -> Session:
        match = self.session_match
        if match is None:
            self.warn("session header line not found")
            return Session(name="", started_at=None, category=self.category)
        name = normalize_text(match.group("name"))
        code: str | None = None
        code_match = _SESSION_CODE_RE.search(name)
        if code_match is not None:
            code = normalize_text(code_match.group(1))
            name = normalize_text(name[: code_match.start()])
        year = int(match.group("year"))
        if year < 100:
            year += 2000
        try:
            started_at: datetime | None = datetime(
                year,
                int(match.group("month")),
                int(match.group("day")),
                int(match.group("hour")),
                int(match.group("minute")),
            )
        except ValueError:
            started_at = None
            self.warn(f"invalid session date/time in header line {self.session_line!r}")
        track = normalize_text(match.group("track")) or None
        return Session(
            name=name,
            started_at=started_at,
            code=code,
            track=track,
            category=self.category,
        )

    # -- tables ------------------------------------------------------------- #

    def read_podium(self, table: LeafTable) -> None:
        position, nickname = table.texts
        self.podium.append((int(position), nickname))

    def read_classification(self, block: _Block) -> None:
        roles = block.roles
        for row in block.table.filled_rows[1:]:
            position = _to_int(row.text(roles["rank"]))
            nickname = row.text(roles["driver"])
            if not nickname:
                self.warn(
                    f"classification row without a driver: {row.texts!r}"
                )
                continue
            gap_ms, gap_laps = (None, None)
            if "gap" in roles:
                gap_ms, gap_laps = parse_gap(row.text(roles["gap"]))
            kart = row.text(roles["kart"]) if "kart" in roles else ""
            entry = SessionEntry(
                driver=self.driver(nickname),
                position=position,
                kart=kart or None,
                laps_count=_to_int(row.text(roles["laps"])) if "laps" in roles else None,
                gap_ms=gap_ms,
                gap_laps=gap_laps,
                best_lap_ms=parse_duration(row.text(roles["best_lap"])),
            )
            self.entries.append(entry)

    def read_lap_chart(self, block: _Block) -> None:
        roles = block.roles
        columns = sorted(block.numbers)
        wrap = len(columns)  # width of the wrap, taken from the header
        printed = [block.numbers[column] for column in columns]
        # Normally the header reads 1..W; a continuation table may start higher.
        consecutive = printed == list(range(printed[0], printed[0] + wrap))
        if not consecutive:
            self.warn(f"lap chart header is not a consecutive sequence: {printed!r}")
        first_lap = printed[0] if consecutive else 1
        kart_column, driver_column = roles["kart"], roles["driver"]

        current: Driver | None = None
        row_in_block = 0
        for row in block.table.body_rows:
            if row.is_empty:
                continue
            nickname = row.text(driver_column)
            if nickname:
                current = self.driver(nickname)
                row_in_block = 0
                kart = row.text(kart_column)
                if kart:
                    self.karts[current.nickname] = kart
            elif current is None:
                self.warn(f"lap chart row before any driver: {row.texts!r}")
                continue

            values: list[Cell] = [row.cell(column) for column in columns]
            while values and values[-1].is_empty:
                values.pop()  # trailing padding of the last row of a block
            for offset, cell in enumerate(values):
                lap_number = first_lap + row_in_block * wrap + offset
                if cell.is_empty:
                    self.warn(
                        f"{current.nickname}: empty lap cell inside lap "
                        f"{lap_number}, stored as no time"
                    )
                time_ms = parse_duration(cell.text)
                if time_ms is None and not is_missing(cell.text):
                    self.warn(
                        f"{current.nickname} lap {lap_number}: "
                        f"unreadable time {cell.text!r}"
                    )
                self.laps.append(
                    Lap(
                        driver=current,
                        lap_number=lap_number,
                        time_ms=time_ms,
                        is_best=cell.is_highlighted,
                    )
                )
            row_in_block += 1

    def read_personal_laps(self, block: _Block) -> None:
        roles = block.roles
        sector_columns = sorted(block.sectors)
        self.personal_labels = [block.sectors[column] for column in sector_columns]
        lap_column = roles["lap_number"]
        time_column = roles.get("time")
        for row in block.table.filled_rows[1:]:
            lap_number = _to_int(row.text(lap_column))
            if lap_number is None:
                self.warn(f"personal lap row without a lap number: {row.texts!r}")
                continue
            sectors = [parse_duration(row.text(column)) for column in sector_columns]
            time_ms = parse_duration(row.text(time_column)) if time_column is not None else None
            self.personal_rows.append(
                (lap_number, sectors, time_ms, row.cell(lap_column).is_highlighted)
            )

    def read_history(self, block: _Block) -> None:
        roles = block.roles
        for row in block.table.filled_rows[1:]:
            when = _to_date(row.text(roles["date"]))
            if when is None:
                self.warn(f"unreadable date in history row: {row.texts!r}")
            self.history.append(
                HistoryEntry(
                    date=when,
                    position=_to_int(row.text(roles["rank"])),
                    best_lap_ms=parse_duration(row.text(roles["best_lap"])),
                    laps_count=_to_int(row.text(roles["laps"])) if "laps" in roles else None,
                    category=self.category,
                )
            )

    def read_ranking(self, block: _Block, kind: RankingKind) -> None:
        roles = block.roles
        for row in block.table.filled_rows[1:]:
            nickname = row.text(roles["driver"])
            if not nickname:
                self.warn(f"ranking row without a driver: {row.texts!r}")
                continue
            rank = _to_int(row.text(roles["rank"]))
            if rank is None:
                self.warn(f"ranking row without a rank: {row.texts!r}")
                continue
            self.rankings.append(
                RankingEntry(
                    kind=kind,
                    rank=rank,
                    driver=self.driver(nickname),
                    best_lap_ms=parse_duration(row.text(roles["best_lap"])),
                    category=self.category,
                )
            )

    def read_unknown(self, table: LeafTable) -> None:
        rows = [row.texts for row in table.filled_rows]
        header = rows[0] if rows else []
        self.unparsed.append(
            UnparsedBlock(header=header, rows=rows[1:], note="unclassified data table")
        )
        self.warn(f"unclassified data table with header {header!r}")

    # -- assembly ------------------------------------------------------------ #

    def run(self) -> ParsedEmail:
        self.read_links()

        blocks = [_classify(table) for table in leaf_tables(self.soup)]
        pending_ranking: list[RankingKind] = []
        for block in blocks:
            if block.kind == "text":
                text = block.table.text
                self.read_caption(text)
                section = _match_section(text)
                if section is not None and section[0] in ("weekly_best", "track_record"):
                    pending_ranking.append(RankingKind(section[0]))
                continue
            self.seen_kinds.add(block.kind)
            if block.kind == "podium":
                self.read_podium(block.table)
            elif block.kind == "classification":
                self.read_classification(block)
            elif block.kind == "lap_chart":
                self.read_lap_chart(block)
            elif block.kind == "personal_laps":
                self.read_personal_laps(block)
            elif block.kind == "history":
                self.read_history(block)
            elif block.kind == "ranking":
                kind = pending_ranking.pop(0) if pending_ranking else None
                if kind is None:
                    kind = (
                        RankingKind.WEEKLY_BEST
                        if not any(r.kind is RankingKind.WEEKLY_BEST for r in self.rankings)
                        else RankingKind.TRACK_RECORD
                    )
                    self.warn(
                        f"ranking table without a caption, assumed {kind.value}"
                    )
                self.read_ranking(block, kind)
            else:
                self.read_unknown(block.table)

        if self.session_match is None or self.recipient is None:
            self.read_loose_captions()

        self.podium.sort(key=lambda item: item[0])
        session = self.build_session()
        self.finish_recipient()
        # The classification may omit the kart column; the lap chart never does.
        for entry in self.entries:
            if entry.kart is None:
                entry.kart = self.karts.get(entry.driver.nickname)
        self.validate()

        club = Club(
            name=self.club_name(),
            external_id=self.club_external_id,
            website=self.website,
            email=self.provenance.from_email,
        )
        self.provenance.recipient_nickname = self.recipient
        return ParsedEmail(
            club=club,
            session=session,
            provenance=self.provenance,
            entries=self.entries,
            laps=self.laps,
            rankings=self.rankings,
            history=self.history,
            podium=self.podium,
            warnings=self.warnings,
            unparsed=self.unparsed,
        )

    def club_name(self) -> str:
        if self.provenance.from_name:
            return normalize_text(self.provenance.from_name)
        subject = normalize_text(self.provenance.subject)
        if ":" in subject:
            return subject.split(":", 1)[0].strip()
        if self.website:
            host = urlsplit(self.website).netloc
            return host[4:] if host.casefold().startswith("www.") else host
        return ""

    def finish_recipient(self) -> None:
        """Attach the recipient's id and sector times to their laps."""
        if self.recipient is None:
            self.warn("recipient nickname is unknown")
        else:
            driver = self.driver(self.recipient)
            if self.recipient_external_id is not None:
                driver.external_id = self.recipient_external_id

        if not self.personal_rows:
            if "personal_laps" not in self.seen_kinds:
                self.warn("personal lap/sector table not found")
            return
        if self.recipient is None:
            self.warn("sector table found but the recipient is unknown; sectors dropped")
            return

        by_number = {
            lap.lap_number: lap
            for lap in self.laps
            if lap.driver.nickname == self.recipient
        }
        from_chart = bool(by_number)
        driver = self.driver(self.recipient)
        highlighted: list[int] = []
        for lap_number, sectors, time_ms, is_best in self.personal_rows:
            if is_best:
                highlighted.append(lap_number)
            lap = by_number.get(lap_number)
            if lap is None:
                if from_chart:
                    self.warn(
                        f"{self.recipient}: lap {lap_number} is in the sector "
                        "table but not in the lap chart"
                    )
                lap = Lap(driver=driver, lap_number=lap_number, time_ms=time_ms)
                by_number[lap_number] = lap
                self.laps.append(lap)
            elif lap.time_ms != time_ms:
                self.warn(
                    f"{self.recipient} lap {lap_number}: lap chart says "
                    f"{lap.time_ms}, sector table says {time_ms}"
                )
            lap.sectors = list(sectors)
            known = [value for value in sectors if value is not None]
            if lap.time_ms is not None and len(known) == len(sectors) and sectors:
                total = sum(known)
                if total != lap.time_ms:
                    labels = "+".join(self.personal_labels) or "sectors"
                    self.warn(
                        f"{self.recipient} lap {lap_number}: {labels} sum to "
                        f"{total} but the lap time is {lap.time_ms}"
                    )
        chart_best = sorted(
            lap.lap_number for lap in by_number.values() if lap.is_best
        )
        if highlighted and chart_best and highlighted != chart_best:
            self.warn(
                f"{self.recipient}: best lap highlighted as {highlighted} in the "
                f"sector table but {chart_best} in the lap chart"
            )
        for lap_number in highlighted:
            by_number[lap_number].is_best = True

    def validate(self) -> None:
        """Cross-checks required by SPEC section 4.3."""
        if not self.entries:
            self.warn("classification table not found")
        if not any(lap.time_ms is not None for lap in self.laps):
            self.warn("lap chart not found or empty")

        laps_by_driver: dict[str, list[Lap]] = {}
        for lap in self.laps:
            laps_by_driver.setdefault(lap.driver.nickname, []).append(lap)

        classified = {entry.driver.nickname for entry in self.entries}
        for position, nickname in self.podium:
            if classified and nickname not in classified:
                self.warn(
                    f"podium driver {nickname!r} (P{position}) "
                    "is missing from the classification"
                )
        for nickname in laps_by_driver:
            if classified and nickname not in classified:
                kart = self.karts.get(nickname)
                detail = f" (kart {kart})" if kart else ""
                self.warn(
                    f"driver {nickname!r}{detail} has laps but no classification row"
                )

        for entry in self.entries:
            nickname = entry.driver.nickname
            laps = laps_by_driver.get(nickname)
            if not laps:
                if laps_by_driver:
                    self.warn(f"no laps found for {nickname!r}")
                continue
            if entry.laps_count is not None and entry.laps_count != len(laps):
                self.warn(
                    f"{nickname}: classification says {entry.laps_count} laps "
                    f"but the lap chart has {len(laps)}"
                )
            timed = [lap for lap in laps if lap.time_ms is not None]
            if not timed:
                continue
            fastest = min(timed, key=lambda lap: lap.time_ms or 0)
            if entry.best_lap_ms is not None and entry.best_lap_ms != fastest.time_ms:
                self.warn(
                    f"{nickname}: classification best lap {entry.best_lap_ms} "
                    f"differs from the fastest lap in the chart {fastest.time_ms}"
                )
            marked = [lap.lap_number for lap in laps if lap.is_best]
            if len(marked) > 1:
                self.warn(f"{nickname}: several laps highlighted as best: {marked}")
            elif marked and marked[0] != fastest.lap_number:
                self.warn(
                    f"{nickname}: lap {marked[0]} is highlighted as best but "
                    f"lap {fastest.lap_number} is faster"
                )
            elif not marked:
                self.warn(f"{nickname}: no lap highlighted as best")


def _first(values: list[str] | None) -> str | None:
    for value in values or ():
        cleaned = normalize_text(value)
        if cleaned:
            return cleaned
    return None


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def parse_html(
    html: str,
    *,
    strict: bool = False,
    provenance: Provenance | None = None,
) -> ParsedEmail:
    """Parse the HTML body alone (no email headers available)."""
    parsed = _EmailParser(html, provenance or Provenance()).run()
    if strict and parsed.warnings:
        raise ParseError(
            "strict mode: " + "; ".join(parsed.warnings)
        )
    return parsed


def parse_email_bytes(
    raw: bytes, *, source_path: str | None = None, strict: bool = False
) -> ParsedEmail:
    """Parse a raw ``.eml`` payload."""
    message = email.message_from_binary_file(io.BytesIO(raw), policy=email.policy.default)
    body = message.get_body(preferencelist=("html",))
    if body is None:
        raise ParseError("the email has no text/html part")
    content = body.get_content()
    if isinstance(content, bytes):  # pragma: no cover - only for broken charsets
        content = content.decode("utf-8", "replace")

    provenance = Provenance(
        message_id=_header(message, "Message-ID"),
        subject=normalize_text(_header(message, "Subject")) or None,
        sent_at=_sent_at(message),
        source_path=source_path,
        sha256=hashlib.sha256(raw).hexdigest(),
    )
    from_name, from_email = _address(message, "From")
    provenance.from_name, provenance.from_email = from_name, from_email
    to_name, to_email = _address(message, "To")
    provenance.recipient_nickname, provenance.recipient_email = to_name, to_email
    return parse_html(content, strict=strict, provenance=provenance)


def parse_email_file(path: str | Path, *, strict: bool = False) -> ParsedEmail:
    """Parse an ``.eml`` file from disk."""
    file_path = Path(path)
    return parse_email_bytes(
        file_path.read_bytes(), source_path=str(file_path), strict=strict
    )


def _header(message: email.message.EmailMessage, name: str) -> str | None:
    value = message[name]
    return str(value) if value is not None else None


def _sent_at(message: email.message.EmailMessage) -> datetime | None:
    header = message["Date"]
    return getattr(header, "datetime", None)


def _address(message: email.message.EmailMessage, name: str) -> tuple[str | None, str | None]:
    header = message[name]
    for address in getattr(header, "addresses", ()):
        display = normalize_text(address.display_name) or None
        return display, (address.addr_spec or None)
    return None, None
