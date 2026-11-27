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

Criticality
-----------
A criticality label is the `x-read-criticality` keyword on a property of the case's extraction
schema: `critical`, `expected` (the default), `optional` or `ignore` (`openreading.derive.fields`
reads it). `score(resp, expected, schema=...)` adds a top-level `criticality` block when the
schema carries at least one label:
`{weighted_prf: {precision, recall, f1}, critical_pass, n_critical, weights_version}`.
The evals runner passes the case's `extraction_schema.json_schema`. `compare --truth` and
`calibrate` pass no schema, so they report no block.

The block sits beside `dimensions`, never inside them, so `overall` and every leaderboard mean
stay unweighted (law SX5). A weighted headline alone could hide an unweighted regression. With a
weight table of all 1.0, `weighted_prf` equals `field_prf` on scalar fields and `row_prf` on
rows, bit for bit, and a test holds it there.

- `CRITICALITY_WEIGHTS_V1` gives critical 1.0, expected 0.3, optional 0.1 and ignore 0.0.
  `weights_version` echoes `"v1"`, so two reports scored under different tables cannot be read
  as one series.
- `weighted_prf` is count-based, not a mean of per-field F1. Each item carries its field's weight.
  Precision is the weighted correct sum over the weighted sum of returned items, and recall the
  same over expected items. A scalar field is one item, as in `field_prf`. A field with labelled
  descendants splits into one item per leaf path, such as `statement_period.start` and
  `statement_period.end`, and an unlabelled leaf takes its parent's code. Each row of a
  `typed_rows` field is one item, matched as `row_prf` matches it, so an invented row lowers
  precision. A returned field the schema does not declare weighs as `expected`, because invented
  keys must cost something. `None` when the case declares neither `typed_fields` nor
  `typed_rows`.
- An `ignore` label below a field, such as a `category` column inside `items`, removes that key
  from the gold value. The comparisons read gold keys only, so the column goes unscored.
- `critical_pass` is 1.0 when every critical field the gold record names is right, else 0.0. A
  scalar is right under `_field_equal`. A row field is right when its own exact rows equal both
  its expected and its returned row counts. The matcher runs once per field, because `row_prf`
  pools its counts and a wrong optional list would blur a right critical one. A gold `null`
  asserts the field is absent, so any non-null reply fails it. A critical field the gold record
  omits is skipped. A critical column inside `items` is checked through the list holding it,
  because a value is right only in its own row. With no critical field checked, `critical_pass`
  is `None` and `n_critical` is 0, which leaves the case out of a pass rate.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from difflib import SequenceMatcher
from types import MappingProxyType
from typing import Any

from openreading.derive.fields import CRITICALITY_KEY, DEFAULT_CRITICALITY, field_criticality


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


def _match_rows(got_rows: list[Any], exp_rows: list[Any]) -> tuple[int, int, int]:
    """(exact rows, matching cells, labelled cells) for one field, matched as `row_prf` says."""
    width = [_row_cells(None, e)[1] for e in exp_rows]
    pairs = []
    for i, e in enumerate(exp_rows):
        for j, g in enumerate(got_rows):
            ok, _ = _row_cells(g, e)
            if ok:
                pairs.append((-ok / width[i], i, j, ok))
    pairs.sort()
    used_e: set[int] = set()
    used_g: set[int] = set()
    exact = cells_ok = 0
    for _, i, j, ok in pairs:
        if i in used_e or j in used_g:
            continue
        used_e.add(i)
        used_g.add(j)
        cells_ok += ok
        exact += ok == width[i]
    return exact, cells_ok, sum(width)


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
        field_exact, field_ok, field_total = _match_rows(got_rows, exp_rows)
        exact += field_exact
        cells_ok += field_ok
        cells_total += field_total
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


# --- criticality weighting ----------------------------------------------------------------

CRITICALITY_WEIGHTS_V1: Mapping[str, float] = MappingProxyType(
    {"critical": 1.0, "expected": 0.3, "optional": 0.1, "ignore": 0.0}
)
CRITICALITY_WEIGHTS_VERSION = "v1"

_ABSENT = object()


def _at(value: Any, path: tuple[str, ...]) -> Any:
    """The value at `path` through nested objects, or `_ABSENT` when any step is missing."""
    for key in path:
        if not isinstance(value, Mapping) or key not in value:
            return _ABSENT
        value = value[key]
    return value


def _label(node: Any) -> str | None:
    if isinstance(node, Mapping) and isinstance(node.get(CRITICALITY_KEY), str):
        return node[CRITICALITY_KEY]
    return None


def _labelled_below(node: Any) -> bool:
    """True when an object property below `node` carries a label. Arrays are not entered,
    because a path through a list names a column of every row, not one value."""
    props = node.get("properties") if isinstance(node, Mapping) else None
    if not isinstance(props, Mapping):
        return False
    return any(_label(sub) is not None or _labelled_below(sub) for sub in props.values())


def _leaf_items(name: str, node: Any, code: str, gold: Any) -> list[tuple[tuple[str, ...], str]]:
    """The scored items of one field as (path, code). A field with labelled descendants splits
    into its leaf paths, and an unlabelled child takes its parent's code. A gold key the schema
    does not declare stays an item, so no gold value drops out of recall."""
    out: list[tuple[tuple[str, ...], str]] = []

    def expand(path: tuple[str, ...], sub: Any, inherited: str, gold_here: Any) -> None:
        if not _labelled_below(sub):
            out.append((path, inherited))
            return
        props = sub["properties"]
        names = list(props)
        if isinstance(gold_here, Mapping):
            names += [k for k in gold_here if k not in props]
        for child in names:
            child_node = props.get(child)
            child_gold = gold_here.get(child, _ABSENT) if isinstance(gold_here, Mapping) else None
            expand((*path, child), child_node, _label(child_node) or inherited, child_gold)

    expand((name,), node, code, gold)
    return out


def _array_prefix(schema: Any, path: tuple[str, ...]) -> int | None:
    """When `path` steps into an array's `items`, the length of the prefix naming that array."""
    node = schema
    for i, key in enumerate(path):
        props = node.get("properties") if isinstance(node, Mapping) else None
        if isinstance(props, Mapping) and key in props:
            node = props[key]
            continue
        items = node.get("items") if isinstance(node, Mapping) else None
        if (
            isinstance(items, Mapping)
            and isinstance(items.get("properties"), Mapping)
            and key in items["properties"]
        ):
            return i
        return None
    return None


def _without(value: Any, path: tuple[str, ...]) -> Any:
    """A copy of `value` without the key at `path`, applied to every row of a list on the way."""
    if not path:
        return value
    if isinstance(value, list):
        return [_without(v, path) for v in value]
    if not isinstance(value, Mapping) or path[0] not in value:
        return value
    if len(path) == 1:
        return {k: v for k, v in value.items() if k != path[0]}
    return {**value, path[0]: _without(value[path[0]], path[1:])}


def _gold_without_ignored(gold: Mapping[str, Any], ignored: list[tuple[str, ...]]) -> dict:
    out = dict(gold)
    for path in ignored:
        if path[0] in out:
            out[path[0]] = _without(out[path[0]], path[1:])
    return out


def _prf(correct: float, got: float, exp: float) -> dict[str, float]:
    # The same expressions and empty-input guards as field_prf and row_prf, so a table of all
    # 1.0 reproduces them bit for bit (law SX5).
    precision = correct / got if got else (1.0 if not exp else 0.0)
    recall = correct / exp if exp else 1.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return {"precision": precision, "recall": recall, "f1": f1}


def _prepared(
    resp: dict, expected: dict, schema: Any
) -> tuple[Mapping[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    """(top-level schema properties, every returned value, gold fields, gold rows). Every
    `ignore` path below a field is removed from the gold values. The comparisons only look at
    gold keys, so the column then goes unscored on both sides."""
    props = schema.get("properties") if isinstance(schema, Mapping) else None
    props = props if isinstance(props, Mapping) else {}
    ignored = [p for p, code, _ in field_criticality(schema) if code == "ignore" and len(p) > 1]
    gold_fields = _gold_without_ignored(expected.get("typed_fields") or {}, ignored)
    gold_rows = _gold_without_ignored(expected.get("typed_rows") or {}, ignored)
    return props, response_typed_fields(resp), gold_fields, gold_rows


def weighted_prf(
    resp: dict,
    expected: dict,
    schema: Any,
    *,
    weights: Mapping[str, float] = CRITICALITY_WEIGHTS_V1,
) -> dict[str, float] | None:
    """Count-based precision, recall and F1, with each scored item carrying its field's weight.

    Items come from the dimensions the case declares. `expected.typed_fields` gives one item per
    scalar field, or one per leaf path when the field has labelled descendants. Each row of an
    `expected.typed_rows` field is one item, matched as `row_prf` matches it. The result is
    `None` when the case declares neither, because there is nothing to weigh."""
    if "typed_fields" not in expected and "typed_rows" not in expected:
        return None
    props, got_all, gold_fields, gold_rows = _prepared(resp, expected, schema)
    default = weights[DEFAULT_CRITICALITY]
    correct = got_w = exp_w = 0.0
    if "typed_fields" in expected:
        got = {k: v for k, v in got_all.items() if k not in gold_rows}
        for name in list(gold_fields) + [k for k in got if k not in gold_fields]:
            node = props.get(name)
            code = _label(node) or DEFAULT_CRITICALITY
            for path, item_code in _leaf_items(name, node, code, gold_fields.get(name, _ABSENT)):
                w = weights.get(item_code, default)
                g, r = _at(gold_fields, path), _at(got, path)
                if g is not _ABSENT:
                    exp_w += w
                if r is not _ABSENT:
                    got_w += w
                if g is not _ABSENT and r is not _ABSENT and _field_equal(r, g):
                    correct += w
    for name, exp_rows in gold_rows.items():
        w = weights.get(_label(props.get(name)) or DEFAULT_CRITICALITY, default)
        exp_list = exp_rows if isinstance(exp_rows, list) else []
        got_rows = got_all.get(name)
        got_list = got_rows if isinstance(got_rows, list) else []
        exact, _, _ = _match_rows(got_list, exp_list)
        correct += w * exact
        got_w += w * len(got_list)
        exp_w += w * len(exp_list)
    return _prf(correct, got_w, exp_w)


def _field_equal_present(got: Any, gold: Any) -> bool:
    return got is not _ABSENT and _field_equal(got, gold)


def _critical_targets(schema: Any) -> list[tuple[str, ...]]:
    """The paths `critical_pass` checks. A critical column inside `items` is checked through the
    list that holds it, because a value is right only in the row it belongs to."""
    targets: list[tuple[str, ...]] = []
    for path, code, _ in field_criticality(schema):
        if code != "critical":
            continue
        cut = _array_prefix(schema, path)
        target = path[:cut] if cut else path
        if target not in targets:
            targets.append(target)
    return targets


def critical_pass(resp: dict, expected: dict, schema: Any) -> tuple[float | None, int]:
    """(1.0 when every critical field the gold record names is right, else 0.0; fields checked).

    The first value is `None` when the gold record names no critical field."""
    _, got_all, gold_fields, gold_rows = _prepared(resp, expected, schema)
    checked = 0
    passed = True
    for target in _critical_targets(schema):
        if len(target) == 1 and target[0] in gold_rows:
            exp_rows = gold_rows[target[0]]
            got_rows = got_all.get(target[0])
            if exp_rows is None:
                ok = got_rows is None
            else:
                # Per field, because row_prf pools its counts and another field's rows would
                # blur this one's.
                got_list = got_rows if isinstance(got_rows, list) else []
                exact, _, _ = _match_rows(got_list, exp_rows)
                ok = exact == len(exp_rows) == len(got_list)
        else:
            g = _at(gold_fields, target)
            if g is _ABSENT:
                continue
            r = _at(got_all, target)
            # A gold null asserts absence, so any non-null reply fails it.
            ok = (r is _ABSENT or r is None) if g is None else _field_equal_present(r, g)
        checked += 1
        passed = passed and ok
    if not checked:
        return None, 0
    return (1.0 if passed else 0.0), checked


def _criticality_block(resp: dict, expected: dict, schema: Any) -> dict[str, Any] | None:
    if not any(labelled for _, _, labelled in field_criticality(schema)):
        return None
    passed, n_critical = critical_pass(resp, expected, schema)
    return {
        "weighted_prf": weighted_prf(resp, expected, schema),
        "critical_pass": passed,
        "n_critical": n_critical,
        "weights_version": CRITICALITY_WEIGHTS_VERSION,
    }


def score(resp: dict, expected: dict, *, schema: Any = None) -> dict[str, Any]:
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
    out: dict[str, Any] = {"overall": overall, "dimensions": dims}
    # Beside the dimensions, never inside them: `overall` and the leaderboard means average every
    # dimension, so a weighted metric there would move the unweighted headline (law SX5).
    block = _criticality_block(resp, expected, schema) if schema is not None else None
    if block is not None:
        out["criticality"] = block
    return out
