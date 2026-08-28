"""The canonical table grid + its projections (DESIGN §5, §4.7).

`cells_to_grid` is the ONE occupancy-cursor implementation — every adapter that walks provider
cells (Textract MERGED_CELL, Google headerRows/bodyRows, Docling span holes, HTML/pipe tables)
routes through it, so merged cells get true grid coordinates in exactly one place. `Table.rows`
carries the value at each span's origin and `None` at covered positions; the HTML/pipe renditions
are projections of the grid, never alternatives to it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from html.parser import HTMLParser
from typing import Any, NamedTuple

from openreading.types.blocks import Table, TableCell
from openreading.types.geometry import BBox


class GridCell(NamedTuple):
    """One provider cell fed to `cells_to_grid`, in provider order. `col=None` means 'let the
    occupancy cursor assign the column' (for providers whose column indices shift under spans —
    the P9 enumeration bug); an explicit `col` is honored as the span origin."""

    row: int
    col: int | None = None
    row_span: int = 1
    col_span: int = 1
    text: str | None = None
    is_header: bool = False
    bbox: dict[str, Any] | None = None


def cells_to_grid(cells: Iterable[GridCell]) -> Table:
    """Place provider cells onto a true grid under merged cells (occupancy cursor).

    Each cell's row is authoritative; its column is either the explicit `col` or the next free
    column in that row skipping positions already occupied by spans (from cells earlier in
    provider order, including row-spans descending from above)."""
    occupied: set[tuple[int, int]] = set()
    placed: list[tuple[int, int, GridCell]] = []
    for c in cells:
        rs = max(1, c.row_span or 1)
        cs = max(1, c.col_span or 1)
        if c.col is not None:
            col = c.col
        else:
            col = 0
            while (c.row, col) in occupied:
                col += 1
        for dr in range(rs):
            for dc in range(cs):
                occupied.add((c.row + dr, col + dc))
        placed.append((c.row, col, c))

    if not placed:
        return Table(n_rows=0, n_cols=0, cells=[], rows=[])

    n_rows = max(r + max(1, c.row_span or 1) for r, _, c in placed)
    n_cols = max(col + max(1, c.col_span or 1) for _, col, c in placed)

    rows: list[list[str | None]] = [[None] * n_cols for _ in range(n_rows)]
    out_cells: list[TableCell] = []
    for r, col, c in placed:
        rows[r][col] = c.text
        out_cells.append(
            TableCell(
                row=r,
                col=col,
                row_span=max(1, c.row_span or 1),
                col_span=max(1, c.col_span or 1),
                text=c.text,
                is_header=bool(c.is_header),
                bbox=BBox.model_validate(c.bbox) if c.bbox is not None else None,
            )
        )
    return Table(n_rows=n_rows, n_cols=n_cols, cells=out_cells, rows=rows)


def _grid_rows(table: Table) -> list[list[str | None]]:
    if table.rows is not None:
        return table.rows
    # reconstruct from cells if rows was not populated
    if not table.cells:
        return []
    n_rows = max((c.row or 0) + 1 for c in table.cells)
    n_cols = max((c.col or 0) + 1 for c in table.cells)
    rows: list[list[str | None]] = [[None] * n_cols for _ in range(n_rows)]
    for c in table.cells:
        rows[c.row or 0][c.col or 0] = c.text
    return rows


def table_to_text(table: Table) -> str:
    """Grid → plain text (the C2 projection): each row a line, cells joined by a single tab."""
    rows = _grid_rows(table)
    return "\n".join("\t".join("" if cell is None else str(cell) for cell in row) for row in rows)


def _esc_pipe(cell: str | None) -> str:
    return ("" if cell is None else str(cell)).replace("|", r"\|").replace("\n", "<br>")


def _header_row_index(table: Table) -> int | None:
    hdr = [c.row for c in (table.cells or []) if c.is_header and c.row is not None]
    return min(hdr) if hdr else None


def table_to_pipe_md(table: Table) -> str:
    """Grid → GFM pipe table. `|`→`\\|`, newlines→`<br>`. The header row comes only from
    `is_header`; GFM requires a header, so a grid with no header cell gets a blank one."""
    rows = _grid_rows(table)
    if not rows:
        return ""
    n_cols = table.n_cols or max(len(r) for r in rows)

    def line(cells: Sequence[str | None]) -> str:
        padded = list(cells) + [None] * (n_cols - len(cells))
        return "| " + " | ".join(_esc_pipe(c) for c in padded) + " |"

    hdr_idx = _header_row_index(table)
    if hdr_idx is None:
        header: Sequence[str | None] = [None] * n_cols
        body = rows
    else:
        header = rows[hdr_idx]
        body = [r for i, r in enumerate(rows) if i != hdr_idx]

    separator = "| " + " | ".join("---" for _ in range(n_cols)) + " |"
    return "\n".join([line(header), separator, *(line(r) for r in body)])


class _TableHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.cells: list[GridCell] = []
        self._row = -1
        self._cur: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "tr":
            self._row += 1
        elif tag in ("td", "th"):
            a = {k.lower(): (v or "") for k, v in attrs}

            def _span(key: str) -> int:
                try:
                    return max(1, int(a.get(key, "1")))
                except ValueError:
                    return 1

            self._cur = {
                "rs": _span("rowspan"),
                "cs": _span("colspan"),
                "th": tag == "th",
                "text": [],
            }

    def handle_data(self, data: str) -> None:
        if self._cur is not None:
            self._cur["text"].append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in ("td", "th") and self._cur is not None:
            text = " ".join("".join(self._cur["text"]).split())
            row = self._row if self._row >= 0 else 0
            self.cells.append(
                GridCell(row, None, self._cur["rs"], self._cur["cs"], text, self._cur["th"])
            )
            self._cur = None


def html_table_to_table(html: str) -> Table | None:
    """Parse an HTML `<table>` (th/td, rowspan/colspan) into the canonical grid. `None` when no
    cells are found. A thin wrapper over `cells_to_grid` (the column cursor resolves spans)."""
    parser = _TableHTMLParser()
    parser.feed(html)
    parser.close()
    if not parser.cells:
        return None
    return cells_to_grid(parser.cells)


_SEP_CELL = re.compile(r"^\s*:?-+:?\s*$")


def _split_pipe_row(line: str) -> list[str]:
    inner = line.strip()
    if inner.startswith("|"):
        inner = inner[1:]
    if inner.endswith("|") and not inner.endswith(r"\|"):
        inner = inner[:-1]
    parts = re.split(r"(?<!\\)\|", inner)
    return [p.replace(r"\|", "|").strip() for p in parts]


def md_table_to_table(md: str) -> Table | None:
    """Parse a GFM pipe table into the canonical grid. The `---|---` separator row marks the
    header. `None` when the text is not a pipe table."""
    lines = [ln for ln in md.strip().splitlines() if "|" in ln]
    if len(lines) < 2:
        return None
    rows = [_split_pipe_row(ln) for ln in lines]
    sep_idx = next(
        (i for i, r in enumerate(rows) if r and all(_SEP_CELL.match(c) for c in r)), None
    )
    if not sep_idx:  # separator absent, or at index 0 (no header) → not a well-formed GFM table
        return None
    header_rows = rows[:sep_idx]
    body_rows = rows[sep_idx + 1 :]
    grid_rows = header_rows + body_rows
    cells: list[GridCell] = []
    for ri, row in enumerate(grid_rows):
        is_h = ri < len(header_rows)
        for ci, val in enumerate(row):
            cells.append(GridCell(ri, ci, text=val, is_header=is_h))
    return cells_to_grid(cells)
