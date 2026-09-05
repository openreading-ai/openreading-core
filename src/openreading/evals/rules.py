"""Score your own documents with a publisher's rule engine.

The five scorers beside this one mostly ask whether the content you expected is PRESENT. None of
them asks whether the backend added anything you did not expect, so a response carrying an
invented total, a fabricated table row and four hundred words of filler still scores 1.0. That
matters because four backends in the catalog write text rather than read it.

ParseBench publishes a rule vocabulary that does ask. A rule is one named assertion about one
document, such as "this word must appear", "this word must NOT appear", or "this table cell sits
to the left of that one". Write those in a case's ``expected.rules`` and this module hands them to
the publisher's own ``RuleBasedMetric``, unchanged, over the markdown your backend produced.

What this is NOT is a second scoring path. The law in the evals guide is that ``leaderboard``,
``calibrate`` and a plain dataset run all reach the same ``run_case`` and ``score``, so two
harnesses can never disagree about one document. Rules honour that: they are one more dimension
inside ``openreading.evals.scorers.score``, gated on one key, reached through the same call as
every other dimension. There is no second runner, no second command, and no second entry point.

Three decisions worth the words:

- **A rule dimension is its own named dimension.** It never folds into ``text_contains`` or any
  other, because a rule pass rate and a difflib ratio are different measurements and averaging
  them silently would produce a number neither engine defines.
- **The publisher owns the verdict.** Nothing here reimplements a rule. This module validates the
  rules through the publisher's own schema and calls the publisher's own metric, so a score here
  and a score from ``benchmark run`` cannot drift apart.
- **A missing package is an error, not a zero.** Rules without ParseBench installed raise rather
  than scoring 0 or quietly vanishing. A silent zero would read as "your backend failed", and a
  silent omission would read as "your rules passed", and both are lies about a run that never
  happened.

Rule shapes verified against ParseBench 1.0.2, scoring pymupdf's real output:

    {"type": "present", "text": "Total due"}                      the string must appear
    {"type": "absent",  "text": "Total due 9999999"}              it must NOT appear
    {"type": "table", "cell": "North", "right": "120",            table structure, by neighbour
     "top_heading": "Region"}                                     and by column heading

Matching is case-insensitive and collapses whitespace, and it does NOT strip punctuation, so a
trailing period the document lacks fails the rule. The publisher's full vocabulary is 78 types;
``uv run python -m pydoc parse_bench.test_cases.parse_rule_schemas`` lists them once installed.
"""

from __future__ import annotations

import contextlib
import signal
import sys
import threading
from typing import Any

# The publisher's metric guards each rule with `signal.alarm`, gated only on `hasattr(signal,
# "SIGALRM")` and never on which thread it is running on. Off the main thread that raises
# `ValueError: signal only works in main thread of the main interpreter`, so scoring a case from
# a batch worker or a server request would crash rather than score. Hiding the attribute takes
# the publisher's own "this platform has no alarm" branch, which is exactly the right behaviour:
# the per-rule timeout is a runaway guard, and losing it costs nothing but the guard.
_ALARM_LOCK = threading.Lock()


@contextlib.contextmanager
def _publisher_runtime():
    """Run the publisher's metric safely from any thread, without it writing to stdout.

    Two things it does that a library must not. It prints three progress lines per document, and
    stdout on this CLI carries only the response envelope, so `leaderboard --format json | jq`
    would break on the noise. It is sent to stderr rather than discarded, because a person
    watching a long scoring run should still see it.
    """

    on_main = threading.current_thread() is threading.main_thread()
    with contextlib.ExitStack() as stack:
        stack.enter_context(contextlib.redirect_stdout(sys.stderr))
        if not on_main and hasattr(signal, "SIGALRM"):
            # Serialized because this mutates a stdlib module. The only effect on a concurrent
            # scorer is the loss of its timeout for the duration, never a wrong score.
            stack.enter_context(_ALARM_LOCK)
            sigalrm = signal.SIGALRM
            del signal.SIGALRM
            stack.callback(setattr, signal, "SIGALRM", sigalrm)
        yield


# The publisher scores a rule against the document's markdown. Anything false-y means the backend
# produced no markdown at all, which is a zero rather than an error: the rules were checkable and
# nothing satisfied them.
_INSTALL_HINT = (
    "scoring `expected.rules` needs ParseBench. Use Python 3.12 or newer and run "
    "`pip install 'openreading[parsebench]'`"
)


class RuleScoringUnavailable(RuntimeError):
    """Raised when a case declares rules and the publisher's engine is not installed."""


class RuleShapeError(ValueError):
    """Raised when a declared rule is not a shape the publisher's schema accepts."""


def _engine():
    """Import the publisher's rule schema and metric, or say how to install them."""

    try:
        from parse_bench.evaluation.metrics.parse.rule_based_metric import RuleBasedMetric
        from parse_bench.test_cases.parse_rule_schemas import coerce_parse_rule_list
    except ModuleNotFoundError as exc:  # pragma: no cover - exercised by the monkeypatched test
        raise RuleScoringUnavailable(f"{_INSTALL_HINT}. Missing module: {exc.name}") from exc
    return coerce_parse_rule_list, RuleBasedMetric


def score_rules(markdown: str, rules: list[dict[str, Any]]) -> dict[str, Any]:
    """Score one document's markdown against publisher rules, and report what each one did.

    Returns ``{"pass_rate", "passed", "evaluated", "results"}``. ``pass_rate`` is a float in [0,1]
    so it drops into the leaderboard report's existing ``dimensions`` map without a schema change.
    An empty rule list scores 1.0 over 0 rules, matching how the table scorer treats an expected
    empty list: you asserted nothing, and nothing contradicted you.
    """

    if not rules:
        return {"pass_rate": 1.0, "passed": 0, "evaluated": 0, "results": []}
    coerce, metric_cls = _engine()
    try:
        typed = coerce(list(rules))
    except Exception as exc:  # noqa: BLE001 - the publisher raises pydantic and ValueError alike
        raise RuleShapeError(
            f"a rule in `expected.rules` is not a shape ParseBench accepts: {exc}"
        ) from exc

    with _publisher_runtime():
        outcome = metric_cls().compute([rule.model_dump() for rule in typed], markdown or "")
    meta = outcome.metadata or {}
    results = [
        {
            "id": entry.get("id"),
            "type": entry.get("type"),
            "passed": bool(entry.get("passed")),
        }
        for entry in meta.get("rule_results") or []
    ]
    evaluated = int(meta.get("total") or len(results))
    passed = int(meta.get("passed") or sum(1 for entry in results if entry["passed"]))
    value = outcome.value
    return {
        "pass_rate": float(value) if isinstance(value, (int, float)) else 0.0,
        "passed": passed,
        "evaluated": evaluated,
        "results": results,
    }


def failed_rules(detail: dict[str, Any]) -> list[str]:
    """Name the rules that did not pass, for a message a person can act on."""

    return [
        entry.get("id") or entry.get("type") or "?"
        for entry in detail.get("results") or []
        if not entry.get("passed")
    ]


def suggest_rules(expected: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn the expectations a case already carries into publisher rules, verbatim.

    Nobody should hand-author another company's JSON to get started. A case that already names
    `text_contains` and `tables` carries enough to generate the presence and structure rules
    mechanically, and the result is a starting point to edit rather than an answer: the rule that
    matters most, `absent`, cannot be generated at all, because nothing in a case says what must
    NOT be there. That one is yours to write.

    Deliberately conservative. Only assertions the case already makes are emitted, so generating
    rules can never make a backend look better than the labels justify:

    - `text_contains[i]` becomes a `present` rule, with the string passed through untouched.
      Matching folds case and whitespace but not punctuation, so a string is never "tidied".
    - each table cell becomes a `table` rule carrying its right neighbour and its column heading,
      which is what makes the rule structural rather than a second presence check.
    - `text`, `markdown` and `typed_fields` produce nothing. A whole-document string is a
      similarity measure, not an assertion, and forcing it into `present` would demand a
      character-exact reproduction that no backend passes.
    """

    rules: list[dict[str, Any]] = []
    for index, needle in enumerate(expected.get("text_contains") or []):
        if isinstance(needle, str) and needle.strip():
            rules.append({"type": "present", "id": f"contains_{index}", "text": needle})
    for table_index, table in enumerate(expected.get("tables") or []):
        if not table or not isinstance(table, list):
            continue
        header = table[0] if isinstance(table[0], list) else []
        for row_index, row in enumerate(table[1:], start=1):
            if not isinstance(row, list):
                continue
            for cell_index, cell in enumerate(row):
                if not isinstance(cell, str) or not cell.strip():
                    continue
                rule: dict[str, Any] = {
                    "type": "table",
                    "id": f"table{table_index}_r{row_index}_c{cell_index}",
                    "cell": cell,
                }
                if cell_index + 1 < len(row) and isinstance(row[cell_index + 1], str):
                    rule["right"] = row[cell_index + 1]
                if cell_index < len(header) and isinstance(header[cell_index], str):
                    rule["top_heading"] = header[cell_index]
                rules.append(rule)
    return rules
