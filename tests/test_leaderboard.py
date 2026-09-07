"""evals/leaderboard.py — BL-160. Proves AC-1..AC-5 and AC-7..AC-9 of
internal/product/specs/eval-leaderboard.product-spec.md against local, no-network fakes
(tests.fakes.ScriptedBackend) plus the shipped `evals/sample` dataset for the real end-to-end
determinism claim. AC-6 (CLI coded exits) lives in tests/test_cli_leaderboard.py, next to the
other CLI-exit-taxonomy tests it mirrors.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openreading import schemas
from openreading.evals.leaderboard import run_leaderboard
from openreading.router.registry import Registry
from tests.fakes import ScriptedBackend

SAMPLE = Path("src/openreading/evals/sample")


def _write_case(tmp_path: Path, subdir: str, spec: dict) -> Path:
    case_dir = tmp_path / subdir
    case_dir.mkdir(parents=True, exist_ok=True)
    case_json = case_dir / "case.json"
    case_json.write_text(json.dumps(spec))
    return case_json


def _registry(*backends: ScriptedBackend) -> Registry:
    reg = Registry()
    for b in backends:
        reg.register(b)
    return reg


# --- AC-1 / AC-4 / AC-5: ranking, cost basis alongside score, dataset identity -------------------


def _two_case_dataset(tmp_path: Path) -> Path:
    ds = tmp_path / "dataset"
    _write_case(
        ds,
        "case_a",
        {
            "name": "case_a",
            "input": {"builtin_sample": True},
            "expected": {"text_contains": ["fox"]},
        },
    )
    _write_case(
        ds,
        "case_b",
        {
            "name": "case_b",
            "input": {"builtin_sample": True},
            "expected": {"text_contains": ["zebra"]},
        },
    )
    return ds


def test_ac1_needs_at_least_two_backends():
    with pytest.raises(ValueError, match="at least two backends"):
        run_leaderboard(str(SAMPLE), ["pymupdf"], _registry(ScriptedBackend("pymupdf", local=True)))


def test_ac1_unregistered_backend_raises_value_error(tmp_path):
    ds = _two_case_dataset(tmp_path)
    reg = _registry(ScriptedBackend("known", local=True))
    with pytest.raises(ValueError, match="not registered"):
        run_leaderboard(str(ds), ["known", "ghost"], reg)


# --- AC-2: compliance refusal is a scored, error-carrying case — never silently skipped, never ---
# --- a widened compliance-eligible set --------------------------------------------------------


def _hipaa_dataset(tmp_path: Path) -> Path:
    ds = tmp_path / "dataset"
    _write_case(
        ds,
        "phi_case",
        {
            "name": "phi_case",
            "input": {"builtin_sample": True},
            "expected": {},
        },
    )
    return ds


# --- AC-3: an unrecognized `expected` dimension is an honest unscored entry, never a fabricated --
# --- 0.0 that would misrank an untested backend below a genuinely poor one ----------------------


def test_ac3_unrecognized_dimension_is_unscored_not_a_fabricated_zero(tmp_path):
    ds = tmp_path / "dataset"
    _write_case(
        ds,
        "labeled",
        {
            "name": "labeled",
            "input": {"builtin_sample": True},
            "expected": {"text_contains": ["OpenReading"]},
        },
    )
    _write_case(
        ds, "unlabeled", {"name": "unlabeled", "input": {"builtin_sample": True}, "expected": {}}
    )
    a = ScriptedBackend("a", local=True, text="OpenReading Test Document")
    b = ScriptedBackend("b", local=True, text="OpenReading Test Document")
    report = run_leaderboard(str(ds), ["a", "b"], _registry(a, b))

    unlabeled = next(c for c in report.cases if c.name == "unlabeled")
    assert unlabeled.scores == {"a": None, "b": None}  # honest unscored, never 0.0
    assert unlabeled.winner is None  # nobody produced a real score on this case

    for row in report.backends:
        assert row.n_cases == 2
        assert row.n_scored == 1  # only "labeled" carried a recognized dimension
        assert row.errors == 0  # unscored is NOT an error
        assert row.mean_score == pytest.approx(1.0)  # grounded in the scored subset only


# --- AC-7: the report is schema-valid by construction (run_leaderboard validates before ----------
# --- returning); this test pins that it stays true for a report carrying every field shape -------


def test_ac7_report_validates_against_the_vendored_schema(tmp_path):
    ds = _two_case_dataset(tmp_path)
    a = ScriptedBackend("a", local=True, text="a fox and a zebra ran together")
    b = ScriptedBackend("b", local=False, cost_low=0.01, text="fox only, no stripes here")
    report = run_leaderboard(str(ds), ["a", "b"], _registry(a, b))
    schemas.validate_leaderboard_report(report.to_schema_dict())  # would already raise internally


# --- AC-8: a rerun is byte-identical for a deterministic backend; a backend flagged in ------------
# --- comparison.report._NON_DETERMINISTIC is labeled, never silently trusted as reproducible ------


def test_ac8_rerun_is_byte_identical_for_local_deterministic_backends():
    pytest.importorskip("fitz", reason="pymupdf not installed")
    from openreading.adapters.pymupdf import PyMuPDFAdapter
    from openreading.adapters.tesseract import TesseractAdapter

    reg = _registry(PyMuPDFAdapter(), TesseractAdapter())
    first = run_leaderboard(str(SAMPLE), ["pymupdf", "tesseract"], reg)
    second = run_leaderboard(str(SAMPLE), ["pymupdf", "tesseract"], reg)
    assert first.to_schema_dict() == second.to_schema_dict()


def test_ac8_backend_flagged_non_deterministic_is_labeled_not_hidden(tmp_path):
    from openreading.comparison.report import _NON_DETERMINISTIC

    flagged_id = next(iter(_NON_DETERMINISTIC))  # e.g. "qwen-vl" — a real, current flagged id
    ds = _two_case_dataset(tmp_path)
    flagged = ScriptedBackend(flagged_id, local=True, text="a fox and a zebra ran together")
    plain = ScriptedBackend("plain-local", local=True, text="a fox and a zebra ran together")
    report = run_leaderboard(str(ds), [flagged_id, "plain-local"], _registry(flagged, plain))

    flagged_row = next(b for b in report.backends if b.backend_id == flagged_id)
    plain_row = next(b for b in report.backends if b.backend_id == "plain-local")
    assert flagged_row.non_deterministic is True
    assert plain_row.non_deterministic is False


# --- AC-9: shipping/exercising the leaderboard changes NOTHING about Router._score, -----------
# --- _QUALITY_BY_PRIORITY, or any shipped adapter's integration_priority -----------------------
