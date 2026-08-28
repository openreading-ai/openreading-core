"""Structure deltas for the superpowered `--format diffs`: how subjects PACKAGE the same content.

Content equivalence is deltas.py; this is the structure axis (DESIGN §9) — the "same words,
different shape" difference a content-only diff is blind to. `table_deltas` pairs tables by index
across subjects and reports each one's grid shape + which subjects flattened it (the pulse `(1,17)`
vs reducto `(4,5)` signal). Type and granularity deltas are assembled in the renderer from data the
report already computes (type_conflict findings, block-count facts)."""

from __future__ import annotations

from typing import Any

from openreading.comparison.ingest import Subject

_FLAT_MIN_COLS = 3  # a single row with ≥ this many columns reads as a flattened multi-row table


def _tables_with_pages(resp: dict[str, Any]) -> list[tuple[int, list[list[Any]]]]:
    """Every table grid in a response, paired with its page number, in document order."""
    out: list[tuple[int, list[list[Any]]]] = []
    for page in (resp.get("document") or {}).get("pages") or []:
        pn = int(page.get("page_number") or 0)
        for b in page.get("blocks") or []:
            if b.get("type") == "table" and (b.get("table") or {}).get("rows"):
                out.append((pn, b["table"]["rows"]))
    return out


def _shape(rows: list[list[Any]]) -> tuple[int, int]:
    return (len(rows), max((len(r) for r in rows), default=0))


def _label(rows: list[list[Any]]) -> str:
    """A short human label for a table: the most descriptive (most letters) cell in its first row —
    a title cell like 'Account Summary' when present, else a leading data cell. Intra-cell
    whitespace/newlines are collapsed so the label never breaks a one-line layout."""
    first = rows[0] if rows else []
    cells = [" ".join(str(c).split()) for c in first if str(c).split()]
    if not cells:
        return ""
    best = max(cells, key=lambda c: sum(ch.isalpha() for ch in c))
    return best if len(best) <= 28 else best[:27] + "…"


def table_deltas(subjects: list[Subject]) -> dict[str, Any]:
    """Pair tables by index across subjects and describe each one's reconstruction. Returns
    ``{"counts": {label: n_tables}, "tables": [{index, page, label, shapes, flat}, ...]}`` where
    ``shapes`` maps each subject that has this table to its ``(rows, cols)`` and ``flat`` is the set
    of subjects that flattened it to a single row while another subject built a real grid."""
    tbls = {s.label: _tables_with_pages(s.response) for s in subjects}
    counts = {label: len(t) for label, t in tbls.items()}
    n = max(counts.values(), default=0)
    tables: list[dict[str, Any]] = []
    for idx in range(n):
        shapes: dict[str, tuple[int, int]] = {}
        page: int | None = None
        label = ""
        for s in subjects:
            if idx < len(tbls[s.label]):
                pn, rows = tbls[s.label][idx]
                shapes[s.label] = _shape(rows)
                if page is None and pn:
                    page = pn
                if not label:
                    label = _label(rows)
                elif len(_label(rows)) > len(label):
                    label = _label(
                        rows
                    )  # prefer the most descriptive first-row cell across subjects
        multi_row = any(r >= 2 for r, _ in shapes.values())
        flat = {
            lbl for lbl, (r, c) in shapes.items() if r == 1 and c >= _FLAT_MIN_COLS and multi_row
        }
        tables.append({"index": idx, "page": page, "label": label, "shapes": shapes, "flat": flat})
    return {"counts": counts, "tables": tables}
