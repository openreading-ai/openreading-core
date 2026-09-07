"""Pinned envelopes for every surface, so a removal moves a field in one visible line.

The removal set in `design/` deletes compliance, the scorer, the capability gate, `auto`, ledger
retention and cost across six changes. Each record predicts what it touches. The risk is not what
anyone predicted: it is the coupling nobody thought to mention, where a change lands somewhere
else entirely and every existing test still passes because nothing asserted the connection.

These tests pin what each surface returns TODAY. A change that moves a pinned field fails here
with a diff naming the field, which is either the change working as designed (re-pin it, and say
why in the commit) or a coupling nobody predicted (stop, and read it). Both outcomes are useful;
silence is not.

Determinism is the whole value, so every surface here runs on local backends over documents this
repository ships, with no network, no key and no clock. `_scrub` drops the fields that legitimately
vary between two identical runs. Today that is exactly one, `summary.duration_ms`, and the scrub
list is deliberately short: a field that must be scrubbed to make a test pass is a field the pin
cannot protect, so each addition should be argued for rather than assumed.

What is pinned is the SHAPE, not the content: every key path, every id, code, enum and count,
with long strings and long lists collapsed. These tests answer "did a field move", and the
extracted text of a PDF is a question other suites already own.

Re-pin with `OPENREADING_REPIN=1 uv run pytest tests/test_characterization.py`, then READ the
diff before committing it. A snapshot regenerated without reading the diff asserts whatever the
code now does, which is worse than having no snapshot at all.

One residual machine-dependency is accepted knowingly: `leaderboard`'s `winner` is `pymupdf`
because it scores 1.0 on a born-digital page, which tesseract can tie but not beat. If a tie ever
breaks the other way on someone's machine, that is the field to look at first.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from openreading import api

GOLDEN = Path(__file__).parent / "golden" / "characterization"
EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
_DOC = str(EXAMPLES / "schedule_a_2024.pdf")
_DOC2 = str(EXAMPLES / "1040_2024.pdf")

# Keys whose value legitimately differs between two identical runs. Kept minimal on purpose: see
# the module docstring. A wall-clock duration is the only honest member today.
_VOLATILE = frozenset({"duration_ms"})

# A fixed second subject for `compare`, so the pin does not depend on whether the machine running
# it has a tesseract binary. `scripts/compare_smoke.py` uses the same trick for the same reason.
_FIXTURE_SUBJECT: dict[str, Any] = {
    "schema_version": "0.3",
    "status": {"state": "succeeded"},
    "backend": {"id": "fixture-ocr", "type": "oss_library"},
    "document": {"text": "Schedule A\nItemized Deductions\nTotal 1234"},
    "usage": {"pages_processed": 1},
}


# Bulk content is collapsed rather than pinned. A parse of one shipped PDF serializes to 531 KB
# and a two-document batch to 2 MB, nearly all of it block geometry and extracted text that
# `tests/test_derive_*.py` and the adapter suites already own. Pinning it here would add megabytes
# that churn on any text change while telling us nothing about the question these tests exist to
# answer, which is whether a FIELD moved. So long strings become their length and long lists
# become a count plus the shape of their first element: ids, codes, enums, states and counts all
# survive, and the diff on a real change stays readable.
_TEXT_CAP = 80
_LIST_CAP = 3


def _scrub(value: Any) -> Any:
    """Drop volatile keys and collapse bulk content, leaving structure and every small value."""
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items() if k not in _VOLATILE}
    if isinstance(value, list):
        # A list of scalars is kept whole however long it is. The fallback chain is fourteen
        # strings and its ORDER is precisely what the scorer produces, so collapsing it to a count
        # would discard the single most important thing these pins protect. Only lists of
        # containers are bulk, and those are the block trees worth collapsing.
        if value and all(not isinstance(v, dict | list) for v in value):
            return [_scrub(v) for v in value]
        if len(value) > _LIST_CAP:
            return {"<list>": len(value), "first": _scrub(value[0])}
        return [_scrub(v) for v in value]
    if isinstance(value, str) and len(value) > _TEXT_CAP:
        return f"<str:{len(value)}>"
    return value


# ---- the surfaces ------------------------------------------------------------------------------


def _parse_single() -> Any:
    return api.run(_DOC, backend="pymupdf")


def _parse_batch() -> Any:
    return api.run_batch([_DOC, _DOC2], backend="pymupdf")


def _route() -> Any:
    """The plan itself, not an envelope: chain order and every drop reason, which is what the
    scorer, the compliance filter and the capability gate all feed. Three of the six changes are
    expected to move this one, and that is the point of pinning it."""
    plan = api.route(_DOC)
    return {
        "chosen": plan.chosen.descriptor.id if plan.chosen else None,
        "fallbacks": [a.descriptor.id for a in plan.fallbacks],
        "dropped": {i: {"stage": d.stage, "code": d.code} for i, d in sorted(plan.dropped.items())},
        "terminal_reason": plan.terminal_reason,
    }


def _compare() -> Any:
    import openreading

    return openreading.compare([api.run(_DOC, backend="pymupdf"), _FIXTURE_SUBJECT])


def _strategy() -> Any:
    """A cascade over a born-digital document, so the orchestration block is populated and the
    gate does not fire.

    Deliberately does NOT escalate to tesseract. `scripts/strategy_smoke.py` does, which is right
    for a smoke test that tolerates either outcome, but a PIN cannot: the escalation's result
    depends on whether this machine's OCR binary works, and a value that changes with the machine
    fails for the next person for a reason unrelated to their change. What is pinned here is the
    SHAPE of an orchestration block, which is what a field move would disturb."""
    config = {
        "version": 1,
        "strategies": {"local_only": {"steps": ["pymupdf"], "escalate_if": "default"}},
    }
    return api.run(_DOC, strategy="local_only", config=config)


def _leaderboard() -> Any:
    """Two local backends over the one dataset this repository ships.

    It used to carry `cost_per_doc`, a descriptor-derived column never measured by the benchmark
    it sat in. deleted it, and this pin is how that change proved its
    blast radius stopped where it said it would: the ranking, the dimensions and the error tally
    came through unmoved."""
    from openreading.evals import run_leaderboard

    report = run_leaderboard(
        "src/openreading/evals/sample", ["pymupdf", "tesseract"], api.build_registry()
    ).to_schema_dict()
    # Two backends are required by `run_leaderboard`, and pymupdf plus tesseract is the only
    # keyless local pair. Tesseract's own rows are dropped from the pin because its scores depend
    # on whether this machine's OCR binary works. The ranking machinery and the dimensions both
    # survive on the pymupdf row, which is what this pin is for.
    report["backends"] = [b for b in report["backends"] if b["backend_id"] == "pymupdf"]
    for case in report.get("cases", []):
        if isinstance(case.get("scores"), dict):
            case["scores"] = {k: v for k, v in case["scores"].items() if k == "pymupdf"}
    return report


def _resume(tmp_ledger: Path) -> Any:
    """An armed run replayed from its journal. deletes
    retention, the reaper and encryption at rest, all of which sit on this path, so a resumed
    envelope that still matches afterwards is the evidence that only policy was removed.

    Uses the shipped `offline_first` preset rather than an inline config, because `resume`
    recompiles the strategy by name and would otherwise need a discoverable `openreading.yaml`.
    """
    import openreading

    previous = os.environ.get("OPENREADING_LEDGER")
    os.environ["OPENREADING_LEDGER"] = str(tmp_ledger)
    try:
        api.run(_DOC, strategy="offline_first")
        run_id = next(tmp_ledger.glob("*.jsonl")).stem
        return openreading.resume(run_id)
    finally:
        if previous is None:
            os.environ.pop("OPENREADING_LEDGER", None)
        else:
            os.environ["OPENREADING_LEDGER"] = previous


SURFACES = {
    "parse_single": _parse_single,
    "parse_batch": _parse_batch,
    "route": _route,
    "compare": _compare,
    "strategy": _strategy,
    "leaderboard": _leaderboard,
}

# `resume` needs a writable ledger root, so it takes a fixture argument the others do not and is
# pinned by its own pair of tests below rather than through SURFACES.

# Nothing here depends on whether this machine has a working tesseract. That is a deliberate
# constraint rather than an accident: `tesseract_ocr_works()` exists because a present-but-broken
# install answers `which` and then fails at the job (BL-170), so a pin that ran real OCR would
# hold a different value on three otherwise identical machines. `_strategy` therefore does not
# escalate and `_leaderboard` drops tesseract's rows, both explained where they are defined.


# ---- the pin -----------------------------------------------------------------------------------


def _assert_pinned(name: str, produced: Any) -> None:
    path = GOLDEN / f"{name}.json"

    if os.environ.get("OPENREADING_REPIN"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(produced, indent=2, sort_keys=True) + "\n")
        pytest.skip(f"re-pinned {path.name}; read the diff before committing it")

    assert path.exists(), (
        f"no pin for {name!r}. Create it with "
        f"OPENREADING_REPIN=1 uv run pytest tests/test_characterization.py"
    )
    assert produced == json.loads(path.read_text()), (
        f"{name} moved. Either the change intended this, in which case re-pin and say why in the "
        f"commit, or it is a coupling nobody predicted, in which case stop and read it."
    )


@pytest.mark.parametrize("name", sorted(SURFACES))
def test_surface_matches_its_pin(name: str) -> None:
    _assert_pinned(name, _scrub(SURFACES[name]()))


def test_resume_matches_its_pin(tmp_path: Path) -> None:
    _assert_pinned("resume", _scrub(_resume(tmp_path / "ledger")))


def test_resume_is_deterministic(tmp_path: Path) -> None:
    first = _scrub(_resume(tmp_path / "a"))
    second = _scrub(_resume(tmp_path / "b"))
    assert first == second


@pytest.mark.parametrize("name", sorted(SURFACES))
def test_surface_is_deterministic(name: str) -> None:
    """The pin is only worth having if the surface returns the same thing twice. A surface that
    fails here has an unscrubbed volatile field, and adding it to `_VOLATILE` is a decision to
    stop protecting it rather than a fix.

    """
    assert _scrub(SURFACES[name]()) == _scrub(SURFACES[name]())
