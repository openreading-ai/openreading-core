"""Scorers for the eval harness. OpenReading's own eval harness is a build prerequisite, not a
nice-to-have: 123 of 127 profiled backends have no independent benchmark (recommendations.md), and
the two highest-value wedge doc types (bank statements/paystubs, IDs) have none at all. Quality
routing can only use numbers you measured yourself, on your own lending/health documents.

Three scorers over the normalized response, so any backend is scored on the same axes:
- text_similarity: difflib ratio on normalized text/markdown, plus a contains-fraction check.
- field_prf: precision / recall / F1 over typed_fields (the extraction axis).
- table_grid: cell-grid accuracy (exact + whitespace/case-tolerant).
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any


def _norm(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s if s is not None else "").strip().lower())


def text_similarity(got: str | None, expected: str | None) -> float:
    """Normalized difflib ratio in [0,1]; 1.0 when both are empty."""
    a, b = _norm(got), _norm(expected)
    if not a and not b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def contains_fraction(text: str | None, needles: list[str]) -> float:
    """Fraction of `needles` that appear as substrings of `text` (normalized)."""
    if not needles:
        return 1.0
    hay = _norm(text)
    return sum(1 for n in needles if _norm(n) in hay) / len(needles)


def field_prf(got: dict[str, Any], expected: dict[str, Any]) -> dict[str, float]:
    """Precision/recall/F1 over a field map. A field is correct when the key is present AND its
    normalized value matches expected."""
    correct = sum(1 for k, v in expected.items() if k in got and _norm(got[k]) == _norm(v))
    precision = correct / len(got) if got else (1.0 if not expected else 0.0)
    recall = correct / len(expected) if expected else 1.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return {
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "correct": float(correct),
        "expected": float(len(expected)),
        "got": float(len(got)),
    }


def table_grid(
    got: list[list[Any]], expected: list[list[Any]], *, tolerant: bool = True
) -> dict[str, float]:
    """Cell-grid accuracy vs the expected grid. `exact` is 1.0 iff every expected cell matches;
    `cell_accuracy` is the fraction of expected cells matched (tolerant = normalized compare)."""
    total = sum(len(r) for r in expected)
    if total == 0:
        return {"exact": 1.0, "cell_accuracy": 1.0, "matched": 0.0, "cells": 0.0}
    eq = (lambda a, b: _norm(a) == _norm(b)) if tolerant else (lambda a, b: a == b)
    matched = 0
    for ri, row in enumerate(expected):
        got_row = got[ri] if ri < len(got) else []
        for ci, cell in enumerate(row):
            if ci < len(got_row) and eq(got_row[ci], cell):
                matched += 1
    return {
        "exact": 1.0 if matched == total else 0.0,
        "cell_accuracy": matched / total,
        "matched": float(matched),
        "cells": float(total),
    }


# --- extraction from a normalized response dict ----------------------------------------


def _doc(resp: dict) -> dict:
    return resp.get("document", {}) or {}


def response_text(resp: dict) -> str:
    return _doc(resp).get("text") or ""


def response_markdown(resp: dict) -> str:
    return _doc(resp).get("markdown") or ""


def canonical_text(resp: dict) -> str:
    """The ONE canonical text derivation shared by evals and compare (DESIGN §4C): document
    text, else document markdown, else block text concatenated in reading order. A single rule so
    every consumer derives 'the text of this response' identically. Text is preferred over
    markdown because it is the guaranteed, packaging-immune channel (C1): two subjects can have
    byte-identical text but differently-styled markdown, and that styling difference must never
    read as content divergence."""
    doc = _doc(resp)
    txt = doc.get("text")
    if txt:
        return txt
    md = doc.get("markdown")
    if md:
        return md
    parts: list[str] = []
    for page in doc.get("pages", []) or []:
        blocks = page.get("blocks", []) or []
        ordered = sorted(
            blocks,
            key=lambda b: b["reading_order"] if b.get("reading_order") is not None else 1 << 30,
        )
        parts.extend(b["text"] for b in ordered if b.get("text"))
    return "\n".join(parts)


def response_typed_fields(resp: dict) -> dict[str, Any]:
    return {k: v.get("value") for k, v in (resp.get("typed_fields") or {}).items()}


def response_tables(resp: dict) -> list[list[list[Any]]]:
    tables = []
    for page in _doc(resp).get("pages", []) or []:
        for b in page.get("blocks", []) or []:
            if b.get("type") == "table" and (b.get("table") or {}).get("rows"):
                tables.append(b["table"]["rows"])
    return tables


def score(resp: dict, expected: dict) -> dict[str, Any]:
    """Score a normalized response dict against an expected dict. Only the dimensions present in
    `expected` are scored; `overall` is the mean of the per-dimension headline scores, or `None`
    when `expected` names none of the five recognized dimensions (an ordinary "not labeled yet"
    shape — most simply `expected == {}` — never a claim that every measured dimension came back
    perfect; callers must not treat `None` as agreement with anything, BL-79/Jin)."""
    dims: dict[str, Any] = {}
    if "text" in expected:
        dims["text_similarity"] = text_similarity(response_text(resp), expected["text"])
    if "text_contains" in expected:
        dims["text_contains"] = contains_fraction(response_text(resp), expected["text_contains"])
    if "markdown" in expected:
        dims["markdown_similarity"] = text_similarity(response_markdown(resp), expected["markdown"])
    if "typed_fields" in expected:
        dims["field_prf"] = field_prf(response_typed_fields(resp), expected["typed_fields"])
    if "tables" in expected:
        got_tables = response_tables(resp)
        exp_tables = expected["tables"]
        if not exp_tables:
            # "this document correctly has zero tables" (BL-95) — vacuously satisfied unless the
            # response hallucinated one, mirroring field_prf/table_grid's own empty-input guards.
            dims["table_cell_accuracy"] = 1.0 if not got_tables else 0.0
        else:
            table_scores = [
                max((table_grid(gt, exp_tbl)["cell_accuracy"] for gt in got_tables), default=0.0)
                for exp_tbl in exp_tables
            ]
            dims["table_cell_accuracy"] = sum(table_scores) / len(table_scores)

    headline = [v["f1"] if isinstance(v, dict) else v for v in dims.values()]
    overall = sum(headline) / len(headline) if headline else None
    return {"overall": overall, "dimensions": dims}
