"""`render_markdown` builds GFM pipe tables, so every interpolated cell must be escaped: a literal
`|` in a finding's detail or snippet (a field value, a table line a backend captured) would
otherwise open extra columns and shred the table. Column counts are checked structurally — the
rows are parsed back into cells on UNESCAPED pipes only, so `\\|` proves the escape is the form a
markdown renderer honors."""

from __future__ import annotations

import re

from openreading.comparison.ingest import load_subjects
from openreading.comparison.render import render_markdown
from openreading.comparison.report import build_report
from tests.fakes import make_envelope

_UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")


def _cells(row: str) -> list[str]:
    parts = _UNESCAPED_PIPE.split(row)
    assert parts[0] == "" and parts[-1] == "", f"not a well-formed pipe row: {row!r}"
    return [p.strip() for p in parts[1:-1]]


def _tables(md: str) -> list[list[list[str]]]:
    """Every pipe table in `md` as rows-of-cells, header included, GFM separator row dropped."""
    blocks: list[list[str]] = []
    for line in md.splitlines():
        if line.startswith("|"):
            if not blocks or not blocks[-1]:
                blocks.append([])
            blocks[-1].append(line)
        elif blocks and blocks[-1]:
            blocks.append([])
    return [[_cells(ln) for i, ln in enumerate(b) if i != 1] for b in blocks if b]


def _widths(table: list[list[str]]) -> set[int]:
    return {len(row) for row in table}


def _pipey_report() -> dict:
    """Two subjects whose disagreement is ABOUT text containing pipes — the field values differ and
    only one captured a pipe-delimited line, so the pipes reach both a finding detail and a
    snippet."""
    a = make_envelope(
        "alpha",
        text="Total | 42.00\nWidget",
        fields={"line_item": "Widget | Blue"},
        pages=[[{"type": "text", "text": "Total | 42.00"}, {"type": "text", "text": "Widget"}]],
        cost_usd=0.02,
        duration_ms=1200,
    )
    b = make_envelope(
        "beta",
        text="Widget",
        fields={"line_item": "Widget | Red"},
        pages=[[{"type": "text", "text": "Widget"}]],
    )
    return build_report(load_subjects([a, b]))


def test_pipes_in_finding_detail_and_snippet_keep_the_column_count_fixed():
    report = _pipey_report()
    codes = {f["code"] for f in report["findings"]}
    assert "field_value_conflict" in codes and "block_unique" in codes
    assert any("|" in f["detail"] for f in report["findings"])  # the raw pipes are really there
    assert any("|" in (f.get("snippet") or "") for f in report["findings"])

    scoreboard, findings = _tables(render_markdown(report))
    assert _widths(scoreboard) == {8}  # header + one row per subject, none wider
    assert len(scoreboard) == 1 + len(report["subjects"])
    assert _widths(findings) == {5}
    assert len(findings) == 1 + len(report["findings"])


def test_pipes_in_cells_survive_escaped_rather_than_being_stripped():
    md = render_markdown(_pipey_report())
    assert r"Widget \| Blue" in md and r"Widget \| Red" in md  # detail: field values
    assert r"Total \| 42.00" in md  # snippet: the line only alpha captured
    assert "|" in md.replace(r"\|", "")  # the table's own delimiters are untouched


def test_pipes_in_subject_labels_do_not_add_columns():
    # Labels come from `backend.id`, which the response schema leaves a free string.
    a = make_envelope("vendor|v2", text="alpha text here", fields={"total": "1"})
    b = make_envelope("other|v3", text="wholly different words", fields={"total": "2"})
    report = build_report(load_subjects([a, b]))

    scoreboard, findings = _tables(render_markdown(report))
    assert _widths(scoreboard) == {8}
    assert _widths(findings) == {5}
    assert [row[0] for row in scoreboard[1:]] == [r"vendor\|v2", r"other\|v3"]
    subject_cells = {row[3] for row in findings[1:]}
    assert subject_cells and all("\\|" in cell for cell in subject_cells)


def test_newline_in_a_cell_becomes_a_break_instead_of_ending_the_row():
    # A hand-built report: `render_markdown` is a pure reader over the report dict, and no builder
    # currently emits a multi-line detail — the renderer must still not be breakable by one.
    report = {
        "mode": "pairwise",
        "subjects": [
            {
                "label": "a",
                "backend": {"type": "hosted_api"},
                "facts": {
                    "pages": 1,
                    "blocks": 1,
                    "chars": 3,
                    "fields": 0,
                    "cost_usd": None,
                    "duration_ms": None,
                },
            }
        ],
        "findings": [
            {
                "severity": "warn",
                "code": "text_divergence",
                "field": "line\nitem",
                "page": None,
                "subjects": ["a"],
                "detail": "first line\nsecond line",
                "snippet": None,
            }
        ],
    }
    md = render_markdown(report)
    scoreboard, findings = _tables(md)
    assert _widths(scoreboard) == {8}
    assert _widths(findings) == {5}
    assert len(findings) == 2
    assert "first line<br>second line" in md
    assert "line<br>item" in md


def test_agreeing_subjects_render_a_scoreboard_and_no_findings_table():
    a = make_envelope("alpha", text="the same words exactly")
    b = make_envelope("beta", text="the same words exactly")
    md = render_markdown(build_report(load_subjects([a, b])))

    assert md.startswith("# Comparison — 2 subjects (pairwise)")
    assert "## Findings (0)" in md
    assert "_No differences on any compared dimension._" in md
    (scoreboard,) = _tables(md)  # the findings table is absent, not empty
    assert _widths(scoreboard) == {8}
    assert scoreboard[0][:2] == ["subject", "type"]
