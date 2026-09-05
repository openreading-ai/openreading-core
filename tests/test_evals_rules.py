"""Publisher rules as a scorer dimension over your own documents.

The point of this dimension is absence. Every other scorer in this repo asks whether expected
content is present, so the guide's own demonstration doctors a good response with an invented
total and four hundred junk words and still scores 1.0. The test below runs that same doctoring
and asserts the rule dimension catches it, because if it does not, this feature has no reason to
exist.

Everything here is offline and free. The publisher's engine is an optional dependency that needs
Python 3.12, so the tests that use it are marked and skip where it is absent. The last two are
deliberately NOT marked: an empty rule list needs no engine, and the absent-package behaviour is
what CI's 3.11 leg is best placed to prove. A module-level importorskip would have skipped exactly
the test that leg exists to run.
"""

from __future__ import annotations

import copy
import importlib.util

import pytest

import openreading
from openreading.evals.rules import (
    RuleScoringUnavailable,
    RuleShapeError,
    failed_rules,
    score_rules,
    suggest_rules,
)
from openreading.evals.scorers import score
from openreading.testing.sample_pdf import build_sample_pdf

# Not a module-level importorskip: the LAST test asserts what happens when the publisher package
# is ABSENT, which is exactly the state of CI's Python 3.11 leg. Skipping it there would skip the
# one test that leg is best placed to prove.
engine = pytest.mark.skipif(
    importlib.util.find_spec("parse_bench") is None,
    reason="scoring rules needs the optional parsebench extra (Python 3.12 or newer)",
)

_MARKDOWN = "# Invoice\n\nRegion | Units\n--- | ---\nNorth | 120\n\nTotal due 42.00\n"


@pytest.fixture(scope="module")
def parsed() -> dict:
    """pymupdf's real response for the bundled sample, parsed once."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "sample.pdf"
        path.write_bytes(build_sample_pdf())
        return openreading.run(str(path), backend="pymupdf")


# --- the reason this exists -------------------------------------------------------------------


@engine
def test_rules_catch_invented_content_that_the_other_scorers_cannot(parsed) -> None:
    """The guide's own hallucination demonstration, which scores 1.0 without rules."""
    expected_without = {"text_contains": ["OpenReading Test Document", "twelve points"]}
    expected_with = dict(
        expected_without,
        rules=[{"type": "absent", "id": "no_invented_total", "text": "Total due: 9999999.00"}],
    )

    # Deep copy: the fixture is module scoped, and doctoring it in place would hand every later
    # test a hallucinated response.
    doctored = copy.deepcopy(parsed)
    doctored["document"]["markdown"] += "\nTotal due: 9999999.00\n" + "lorem ipsum " * 200

    blind = score(doctored, expected_without)["dimensions"]
    seeing = score(doctored, expected_with)["dimensions"]

    # Unchanged and still perfect: presence scorers cannot see an addition.
    assert blind["text_contains"] == 1.0
    assert seeing["text_contains"] == 1.0
    # The rule does see it. This single assertion is the whole feature.
    assert seeing["rule_pass_rate"] == 0.0


@engine
def test_a_clean_response_passes_the_same_rules(parsed) -> None:
    expected = {
        "rules": [
            {"type": "present", "id": "title", "text": "OpenReading Test Document"},
            {"type": "absent", "id": "no_total", "text": "Total due: 9999999.00"},
            {
                "type": "table",
                "id": "grid",
                "cell": "North",
                "right": "120",
                "top_heading": "Region",
            },
        ]
    }

    result = score(parsed, expected)

    assert result["dimensions"]["rule_pass_rate"] == 1.0
    assert result["overall"] == 1.0


# --- the dimension's contract -----------------------------------------------------------------


@engine
def test_the_dimension_appears_only_when_a_case_declares_rules(parsed) -> None:
    assert "rule_pass_rate" not in score(parsed, {"text_contains": ["twelve points"]})["dimensions"]
    assert "rule_pass_rate" in score(parsed, {"rules": []})["dimensions"]


@engine
def test_rules_never_fold_into_another_dimension(parsed) -> None:
    """A rule pass rate and a difflib ratio are different measurements."""
    dimensions = score(
        parsed,
        {
            "text_contains": ["twelve points"],
            "rules": [{"type": "absent", "id": "x", "text": "nowhere at all"}],
        },
    )["dimensions"]

    assert set(dimensions) == {"text_contains", "rule_pass_rate"}
    assert dimensions["text_contains"] == 1.0
    assert dimensions["rule_pass_rate"] == 1.0


@engine
def test_overall_is_the_mean_across_both_kinds_of_expectation(parsed) -> None:
    result = score(
        parsed,
        {
            "text_contains": ["twelve points"],
            "rules": [{"type": "present", "id": "x", "text": "definitely not present"}],
        },
    )

    # text_contains 1.0, rule_pass_rate 0.0. The design record left "does overall combine them?"
    # open; it does, unweighted, and every dimension is still reported separately.
    assert result["dimensions"] == {"text_contains": 1.0, "rule_pass_rate": 0.0}
    assert result["overall"] == 0.5


# --- score_rules directly ---------------------------------------------------------------------


def test_an_empty_rule_list_asserts_nothing_and_is_satisfied() -> None:
    detail = score_rules(_MARKDOWN, [])

    # Same treatment as an expected empty table list: you asserted nothing, nothing contradicted.
    assert detail == {"pass_rate": 1.0, "passed": 0, "evaluated": 0, "results": []}


@engine
def test_matching_ignores_case_and_spacing_but_not_punctuation() -> None:
    """Measured against ParseBench 1.0.2; a converter must not add punctuation of its own."""
    detail = score_rules(
        _MARKDOWN,
        [
            {"type": "present", "id": "exact", "text": "Total due 42.00"},
            {"type": "present", "id": "lower", "text": "total due 42.00"},
            {"type": "present", "id": "spaced", "text": "Total  due  42.00"},
            {"type": "present", "id": "trailing_dot", "text": "Total due 42.00."},
        ],
    )

    assert failed_rules(detail) == ["trailing_dot"]
    assert (detail["passed"], detail["evaluated"]) == (3, 4)


@engine
def test_a_rule_the_publisher_refuses_is_named_not_swallowed() -> None:
    with pytest.raises(RuleShapeError, match="ParseBench accepts"):
        score_rules(_MARKDOWN, [{"type": "no_such_rule_type", "text": "x"}])


@engine
def test_failed_rules_names_what_to_go_and_look_at() -> None:
    detail = score_rules(
        _MARKDOWN,
        [
            {"type": "present", "id": "ok", "text": "North"},
            {"type": "present", "id": "missing_one", "text": "South"},
        ],
    )

    assert failed_rules(detail) == ["missing_one"]


@engine
def test_no_markdown_scores_zero_rather_than_raising() -> None:
    """A backend that produced nothing failed the rules; it did not make them uncheckable."""
    detail = score_rules("", [{"type": "present", "id": "x", "text": "North"}])

    assert detail["pass_rate"] == 0.0


# --- the optional dependency ------------------------------------------------------------------


def test_declaring_rules_without_the_engine_raises_rather_than_scoring_zero(monkeypatch) -> None:
    """A silent zero reads as "your backend failed" and a silent omission as "your rules passed".

    Both are lies about a measurement that never happened, so the missing package is an error
    naming the install command.
    """
    import builtins

    real_import = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name.startswith("parse_bench"):
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)

    with pytest.raises(RuleScoringUnavailable, match=r"openreading\[parsebench\]"):
        score_rules(_MARKDOWN, [{"type": "present", "text": "North"}])


# --- generating rules from labels a case already carries --------------------------------------


def test_suggest_turns_existing_expectations_into_publisher_rules() -> None:
    """Nobody should hand-author another company's JSON to get started."""
    suggested = suggest_rules(
        {
            "text_contains": ["Total due"],
            "tables": [[["Region", "Units"], ["North", "120"]]],
        }
    )

    assert suggested == [
        {"type": "present", "id": "contains_0", "text": "Total due"},
        {
            "type": "table",
            "id": "table0_r1_c0",
            "cell": "North",
            "right": "120",
            "top_heading": "Region",
        },
        {"type": "table", "id": "table0_r1_c1", "cell": "120", "top_heading": "Units"},
    ]


def test_suggest_never_invents_an_assertion_the_case_did_not_make() -> None:
    """A whole-document string is a similarity measure, not an assertion.

    Forcing `text` into a `present` rule would demand a character-exact reproduction no backend
    passes, turning a generated rule set into a guaranteed failure.
    """
    assert suggest_rules({"text": "a whole page of text", "markdown": "# x"}) == []
    assert suggest_rules({"typed_fields": {"total": 1}}) == []
    assert suggest_rules({}) == []


def test_suggest_cannot_produce_the_rule_that_matters_most() -> None:
    """`absent` is the point of rules and cannot be generated: nothing says what must NOT appear."""
    generated = suggest_rules({"text_contains": ["Total due"], "tables": [[["a"], ["b"]]]})

    assert {rule["type"] for rule in generated} == {"present", "table"}
    assert "absent" not in {rule["type"] for rule in generated}


@engine
def test_generated_rules_are_shapes_the_publisher_accepts() -> None:
    """The generator and the grader must agree, or `rules --write` produces a broken case."""
    import json
    from pathlib import Path

    expected = json.loads(Path("src/openreading/evals/sample/loan_page1/case.json").read_text())[
        "expected"
    ]

    detail = score_rules(_MARKDOWN, suggest_rules(expected))

    # It runs and returns a verdict rather than raising RuleShapeError. Whether these particular
    # rules pass against unrelated markdown is not the point.
    assert detail["evaluated"] == 11


# --- running the publisher's metric inside a library ------------------------------------------


@engine
def test_scoring_writes_nothing_to_stdout(capsys) -> None:
    """The CLI's invariant is that stdout carries only the envelope or the rendered report.

    The publisher's metric prints three progress lines per document. Left alone they land in the
    middle of `leaderboard --format json`, so they are redirected to stderr rather than dropped:
    a person watching a long run should still see them.
    """
    score_rules(_MARKDOWN, [{"type": "present", "id": "x", "text": "North"}])

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Rules: done" in captured.err


@engine
def test_scoring_works_off_the_main_thread() -> None:
    """A batch worker or a server request scores off the main thread.

    The publisher guards each rule with `signal.alarm`, gated only on the platform having
    SIGALRM and never on the thread, so this used to raise `ValueError: signal only works in
    main thread of the main interpreter` instead of scoring.
    """
    import threading

    outcome: list[object] = []

    def score_in_a_worker() -> None:
        try:
            outcome.append(
                score_rules(_MARKDOWN, [{"type": "present", "id": "x", "text": "North"}])
            )
        except BaseException as exc:  # noqa: BLE001 - the point is that nothing escapes
            outcome.append(exc)

    worker = threading.Thread(target=score_in_a_worker)
    worker.start()
    worker.join()

    assert not isinstance(outcome[0], BaseException), outcome[0]
    assert outcome[0]["pass_rate"] == 1.0


@engine
def test_the_alarm_is_restored_after_scoring_off_thread() -> None:
    """Hiding SIGALRM is a borrowed workaround, not a permanent change to the process."""
    import signal
    import threading

    before = signal.SIGALRM
    worker = threading.Thread(
        target=lambda: score_rules(_MARKDOWN, [{"type": "present", "id": "x", "text": "North"}])
    )
    worker.start()
    worker.join()

    assert signal.SIGALRM is before
