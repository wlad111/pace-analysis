"""Low level HTML helpers used by the Apex Timing email parser.

The email is a stack of nested layout tables; the payload always sits in
*leaf* tables (tables that contain no other table).  This module knows how to

* normalise cell text (NBSP / zero width space / repeated whitespace),
* read inline styles robustly (Apex encodes "best lap" purely as a colour),
* flatten a leaf table into rows of :class:`Cell` objects.

It knows nothing about karting semantics -- that lives in ``apex_email``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup
from bs4.element import Tag

__all__ = [
    "HIGHLIGHT_BACKGROUND",
    "HIGHLIGHT_FOREGROUND",
    "Cell",
    "Row",
    "LeafTable",
    "css_color",
    "leaf_tables",
    "make_soup",
    "norm_key",
    "normalize_text",
    "parse_style",
]

# Cells that Apex paints as "personal best".  Ordinary data cells are #C0C0C0.
HIGHLIGHT_BACKGROUND = "#515151"
HIGHLIGHT_FOREGROUND = "#ffffff"

_SPACE_CHARS = (
    # NBSP, EN/EM quads, thin/hair spaces, zero-width chars, BOM.
    "\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007"
    "\u2008\u2009\u200a\u200b\u200c\u200d\u202f\u205f\u3000\ufeff"
)
_SPACE_TRANSLATION = {ord(char): " " for char in _SPACE_CHARS}

_STYLE_DECLARATION_RE = re.compile(r"\s*([A-Za-z-]+)\s*:\s*([^;]*)")
_BACKGROUND_COLOR_RE = re.compile(
    r"(?:^|[;\s])background(?:-color)?\s*:\s*([^;]+)", re.IGNORECASE
)
_COLOR_RE = re.compile(r"(?:^|[;\s])color\s*:\s*([^;]+)", re.IGNORECASE)
_SHORT_HEX_RE = re.compile(r"^#([0-9a-f])([0-9a-f])([0-9a-f])$")


def normalize_text(value: str | None) -> str:
    """NBSP/ZWSP -> space, collapse runs of whitespace, strip."""
    if value is None:
        return ""
    return " ".join(value.translate(_SPACE_TRANSLATION).split())


def norm_key(value: str | None) -> str:
    """Normalised, case-insensitive form used to compare header labels."""
    return normalize_text(value).casefold()


def parse_style(style: str | None) -> dict[str, str]:
    """Parse an inline ``style`` attribute into a lowercase property map."""
    if not style:
        return {}
    declarations: dict[str, str] = {}
    for name, value in _STYLE_DECLARATION_RE.findall(style):
        declarations[name.strip().casefold()] = " ".join(value.split()).casefold()
    return declarations


def css_color(value: str | None) -> str | None:
    """Normalise a CSS colour: ``#FFF`` / ``#FFFFFF`` -> ``#ffffff``."""
    if value is None:
        return None
    text = " ".join(value.split()).casefold()
    if not text:
        return None
    # A shorthand such as "background: #c0c0c0 none repeat" -- keep the colour.
    if text.startswith("#"):
        token = text.split()[0]
        short = _SHORT_HEX_RE.match(token)
        if short is not None:
            return "#" + "".join(part * 2 for part in short.groups())
        return token
    return text


@dataclass(slots=True)
class Cell:
    """One ``<td>``/``<th>`` with its normalised text and inline style."""

    text: str = ""
    style: str = ""
    is_header: bool = False

    @property
    def key(self) -> str:
        """Case-folded text, for header comparisons."""
        return self.text.casefold()

    @property
    def is_empty(self) -> bool:
        return not self.text

    @property
    def background(self) -> str | None:
        """Value of ``background-color`` (or ``background``), normalised."""
        match = _BACKGROUND_COLOR_RE.search(self.style)
        return css_color(match.group(1)) if match else None

    @property
    def color(self) -> str | None:
        """Value of the ``color`` property, normalised.

        ``background-color`` cannot match: the pattern requires a declaration
        boundary (start of string, ``;`` or whitespace) in front of ``color``.
        """
        match = _COLOR_RE.search(self.style)
        return css_color(match.group(1)) if match else None

    @property
    def is_highlighted(self) -> bool:
        """True when Apex painted this cell as the driver's best time."""
        if self.background != HIGHLIGHT_BACKGROUND:
            return False
        foreground = self.color
        return foreground is None or foreground == HIGHLIGHT_FOREGROUND


@dataclass(slots=True)
class Row:
    """A ``<tr>`` flattened into cells (``colspan`` expanded)."""

    cells: list[Cell] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.cells)

    def cell(self, index: int) -> Cell:
        """Cell at ``index``, or an empty placeholder when the row is short."""
        if 0 <= index < len(self.cells):
            return self.cells[index]
        return Cell()

    def text(self, index: int) -> str:
        return self.cell(index).text

    @property
    def texts(self) -> list[str]:
        return [cell.text for cell in self.cells]

    @property
    def is_empty(self) -> bool:
        return all(cell.is_empty for cell in self.cells)


@dataclass(slots=True)
class LeafTable:
    """A table with no nested table -- i.e. one that actually carries data."""

    index: int
    rows: list[Row] = field(default_factory=list)

    @property
    def filled_rows(self) -> list[Row]:
        """Rows that hold at least one non-empty cell."""
        return [row for row in self.rows if not row.is_empty]

    @property
    def texts(self) -> list[str]:
        """Every non-empty cell text, in document order."""
        return [cell.text for row in self.rows for cell in row.cells if cell.text]

    @property
    def text(self) -> str:
        """All non-empty cell texts joined -- used for caption-like tables."""
        return " ".join(self.texts)

    @property
    def body_rows(self) -> list[Row]:
        """Every row after the header row, separators included."""
        for index, row in enumerate(self.rows):
            if not row.is_empty:
                return self.rows[index + 1 :]
        return []

    def header(self) -> Row | None:
        """First non-empty row, treated as the header row."""
        filled = self.filled_rows
        return filled[0] if filled else None

    def signature(self) -> tuple[str, ...]:
        """Case-folded header labels; the only key used for classification."""
        header = self.header()
        return tuple(cell.key for cell in header.cells) if header else ()


def _cell_from_tag(tag: Tag) -> Cell:
    style = tag.get("style") or ""
    if not isinstance(style, str):  # defensive: bs4 may return a list
        style = " ".join(style)
    return Cell(
        text=normalize_text(tag.get_text(" ", strip=True)),
        style=style,
        is_header=tag.name == "th",
    )


def _colspan(tag: Tag) -> int:
    raw = tag.get("colspan")
    if not isinstance(raw, str):
        return 1
    try:
        return max(1, int(raw.strip()))
    except ValueError:
        return 1


def make_soup(html: str) -> BeautifulSoup:
    """Parse HTML with lxml (the parser the SPEC mandates)."""
    return BeautifulSoup(html, "lxml")


def leaf_tables(root: BeautifulSoup | Tag) -> list[LeafTable]:
    """All tables without a nested table, in document order."""
    tables: list[LeafTable] = []
    for index, table in enumerate(root.find_all("table")):
        if table.find("table") is not None:
            continue
        rows: list[Row] = []
        for tr in table.find_all("tr"):
            cells: list[Cell] = []
            for tag in tr.find_all(["td", "th"]):
                cell = _cell_from_tag(tag)
                cells.append(cell)
                # Expand colspan so that column indices stay meaningful.
                for _ in range(_colspan(tag) - 1):
                    cells.append(Cell(style=cell.style, is_header=cell.is_header))
            rows.append(Row(cells))
        tables.append(LeafTable(index=index, rows=rows))
    return tables
