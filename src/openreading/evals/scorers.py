"""Scorers for the eval harness.

Few backends publish an independent benchmark, and the ones that do measure documents that are
not yours. Quality routing can only use numbers you measured yourself, on the documents you
actually process.

Four scorers over the normalized response, so any backend is scored on the same axes:
- text_similarity: difflib ratio on normalized text/markdown, plus a contains-fraction check.
- field_prf: precision / recall / F1 over typed_fields (the extraction axis).
- row_prf: the same over the ROWS of repeated typed fields (`expected.typed_rows`), such as a
  statement's transactions or an RFQ's line items. field_prf compares a list as one string, so
  one dropped row would fail the whole field and a reordered list would fail it too.
- table_grid: cell-grid accuracy (exact + whitespace/case-tolerant).
- rule_pass_rate: a publisher's own rule engine over `expected.rules`
  (`openreading.evals.rules`), which is the only dimension here that can see content the backend
  INVENTED rather than merely missed.

Absence is why the fourth exists. The first three ask whether what you expected is present, so a
response with an invented total, a fabricated table row and four hundred junk words still scores
1.0 on them. `expected.rules` carries assertions like `absent` and `unexpected_word` that fail on
exactly that.

`overall` is the unweighted mean of the dimensions a case actually names, rules included. That is
a deliberate answer to a question the design record left open, and the honest reading of it: a
rule pass rate is a fraction of assertions satisfied, on the same [0,1] scale as the others, so a
case that declares both kinds of expectation gets one mean over both. It is a reading aid, not a
number to defend on its own, which is why every dimension is also reported separately.
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


def _field_equal(got: Any, expected: Any) -> bool:
    """A scalar compares as a normalized string, exactly as it always has, so existing scalar
    scores do not move. An object or list compares structurally (`_cell_equal`), because its
    string form depends on key order, and a statement period returned `{end, start}` is not
    wrong."""
    if isinstance(expected, (dict, list)):
        return _cell_equal(got, expected)
    return _norm(got) == _norm(expected)


def field_prf(got: dict[str, Any], expected: dict[str, Any]) -> dict[str, float]:
    """Precision/recall/F1 over a field map. A field is correct when the key is present AND its
    normalized value matches expected."""
    correct = sum(1 for k, v in expected.items() if k in got and _field_equal(got[k], v))
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


_PLAIN_NUMBER = re.compile(r"\d{1,3}(,\d{3})+(\.\d+)?|\d+(\.\d+)?|\.\d+")


def _as_number(v: Any) -> float | None:
    """A number, or a string a statement would print for one: `-$42.18`, `$2,500.00`, `(310.00)`.

    Only US-style separators are read, because `1.234` is a thousand in one locale and one point
    two in another and the scorer cannot tell which. A digit string with a leading zero (`0042`,
    an account suffix) stays a string, since its zeros are part of the identifier.
    """
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if not isinstance(v, str):
        return None
    s = v.strip().replace(" ", "")
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg, s = True, s[1:-1]
    if s.startswith("-"):
        neg, s = not neg, s[1:]
    s = s.lstrip("$€£")
    if s.startswith("-"):
        neg, s = not neg, s[1:]
    if not _PLAIN_NUMBER.fullmatch(s) or re.match(r"0\d", s):
        return None
    x = float(s.replace(",", ""))
    return -x if neg else x


def _cell_equal(got: Any, expected: Any) -> bool:
    """One cell: numbers as numbers, lists element-wise in order, objects on the expected keys,
    everything else as normalized strings."""
    if isinstance(expected, list):
        return (
            isinstance(got, list)
            and len(got) == len(expected)
            and all(_cell_equal(g, e) for g, e in zip(got, expected, strict=True))
        )
    if isinstance(expected, dict):
        return isinstance(got, dict) and all(
            k in got and _cell_equal(got[k], v) for k, v in expected.items()
        )
    a, b = _as_number(got), _as_number(expected)
    if a is not None and b is not None:
        return abs(a - b) <= 1e-6
    return _norm(got) == _norm(expected)


def _row_cells(got: Any, expected: Any) -> tuple[int, int]:
    """(matching cells, labeled cells) for one candidate pair. An object row is judged on the
    columns the case labeled; a scalar row (a list of certifications) is one cell."""
    if isinstance(expected, dict):
        if not isinstance(got, dict):
            return 0, len(expected)
        return sum(1 for k, v in expected.items() if k in got and _cell_equal(got[k], v)), len(
            expected
        )
    return int(_cell_equal(got, expected)), 1


def row_prf(got: dict[str, Any], expected: dict[str, list[Any]]) -> dict[str, float]:
    """Precision, recall and F1 over the rows of repeated fields, pooled across every field named.

    `expected` maps a typed-field name to its list of rows, such as a statement's transactions or
    an RFQ's line items. A row counts as correct only when every column the case labeled matches,
    so one wrong amount fails its row. `cell_accuracy` keeps partial credit visible beside it.

    Rows are matched without regard to order, best pair first: every candidate pair is scored by
    matching cells, and the highest pairs are taken greedily, ties broken by position. Order-free
    matching keeps a backend that returns rows in a different order from scoring as wrong on all
    of them. Greedy rather than optimal assignment keeps the scorer dependency-free; on rows with
    a distinguishing column such as a date or a line number the two agree.

    A field the response lacks, or holds as something other than a list, recalls nothing.
    Columns the response adds beyond the labeled ones are ignored, while rows it adds lower
    precision. That split is the point: an invented transaction is an error, and an extra
    `category` column on a real one is not.
    """
    n_exp = n_got = exact = cells_ok = cells_total = 0
    for name, exp_rows in expected.items():
        got_rows = got.get(name)
        got_rows = got_rows if isinstance(got_rows, list) else []
        n_exp += len(exp_rows)
        n_got += len(got_rows)
        width = [_row_cells(None, e)[1] for e in exp_rows]
        cells_total += sum(width)
        pairs = []
        for i, e in enumerate(exp_rows):
            for j, g in enumerate(got_rows):
                ok, _ = _row_cells(g, e)
                if ok:
                    pairs.append((-ok / width[i], i, j, ok))
        pairs.sort()
        used_e: set[int] = set()
        used_g: set[int] = set()
        for _, i, j, ok in pairs:
            if i in used_e or j in used_g:
                continue
            used_e.add(i)
            used_g.add(j)
            cells_ok += ok
            exact += ok == width[i]
    precision = exact / n_got if n_got else (1.0 if not n_exp else 0.0)
    recall = exact / n_exp if n_exp else 1.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    cell_accuracy = cells_ok / cells_total if cells_total else (1.0 if not n_got else 0.0)
    return {
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "cell_accuracy": cell_accuracy,
        "rows_exact": float(exact),
        "rows_expected": float(n_exp),
        "rows_got": float(n_got),
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
    """The ONE canonical text derivation shared by evals and compare: document
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
    when `expected` names none of the six recognized dimensions (an ordinary "not labeled yet"
    shape — most simply `expected == {}` — never a claim that every measured dimension came back
    perfect; callers must not treat `None` as agreement with anything, BL-79)."""
    dims: dict[str, Any] = {}
    if "text" in expected:
        dims["text_similarity"] = text_similarity(response_text(resp), expected["text"])
    if "text_contains" in expected:
        dims["text_contains"] = contains_fraction(response_text(resp), expected["text_contains"])
    if "markdown" in expected:
        dims["markdown_similarity"] = text_similarity(response_markdown(resp), expected["markdown"])
    if "typed_fields" in expected:
        got_fields = response_typed_fields(resp)
        # A field the case scores as rows is scored once. Left in, a response returning the
        # whole schema would lose field precision for every row field it got right.
        for name in expected.get("typed_rows") or {}:
            got_fields.pop(name, None)
        dims["field_prf"] = field_prf(got_fields, expected["typed_fields"])
    if "typed_rows" in expected:
        dims["row_prf"] = row_prf(response_typed_fields(resp), expected["typed_rows"])
    if "rules" in expected or "text_absent" in expected:
        # The publisher's engine scores its own rules, against the markdown channel it grades
        # everywhere else. Reached here, through the same `score` every other dimension goes
        # through, so the one-scoring-path law holds: there is no second runner and no second
        # entry point, only one more dimension gated on one more key.
        #
        # `text_absent` is the plain-strings spelling of the same thing, scored by the SAME
        # engine rather than by a native "is this substring missing" check. Two absence verdicts
        # that could disagree would be worse than the small dependency, and a user who wrote
        # `text_absent` and got silence because they had not run `openreading rules` would be
        # worse still.
        from openreading.evals.rules import score_rules, suggest_rules

        declared = list(expected.get("rules") or [])
        implied = suggest_rules({"text_absent": expected.get("text_absent") or []})
        detail = score_rules(response_markdown(resp), implied + declared)
        dims["rule_pass_rate"] = detail["pass_rate"]
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
