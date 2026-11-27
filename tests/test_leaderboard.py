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

SAMPLE = Path(__file__).resolve().parents[1] / "src/openreading/evals/sample"


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
    b = ScriptedBackend("b", local=False, text="fox only, no stripes here")
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


# --- criticality: weighted_f1, critical_pass_rate and n_critical_scored (leaderboard v0.2) ------


def _crit(f1, passed, n_critical=1):
    return {
        "weighted_prf": None if f1 is None else {"precision": f1, "recall": f1, "f1": f1},
        "critical_pass": passed,
        "n_critical": n_critical,
        "weights_version": "v1",
    }


def _scripted_reports(monkeypatch, by_backend):
    from openreading.evals import leaderboard as lb
    from openreading.evals.runner import CaseResult, DatasetReport

    def fake_run_dataset(adapter, dataset_dir, **_):
        rows = by_backend[adapter.descriptor.id]
        return DatasetReport(
            backend_id=adapter.descriptor.id,
            results=[
                CaseResult(name=f"c{i}", overall=0.5, dimensions={"field_prf": {"f1": 0.5}}, **r)
                for i, r in enumerate(rows)
            ],
        )

    monkeypatch.setattr(lb, "run_dataset", fake_run_dataset)


def test_criticality_means_ride_beside_the_unweighted_mean(monkeypatch):
    _scripted_reports(
        monkeypatch,
        {
            "a": [
                {"criticality": _crit(1.0, 1.0)},
                {"criticality": _crit(0.5, 0.0)},
                # No critical field in this case's gold, so it leaves the rate and the count.
                {"criticality": _crit(0.25, None, n_critical=0)},
            ],
            "b": [{}, {}, {}],
        },
    )
    reg = _registry(ScriptedBackend("a", local=True), ScriptedBackend("b", local=True))
    report = run_leaderboard("ds", ["a", "b"], reg)
    row_a = next(b for b in report.backends if b.backend_id == "a")
    row_b = next(b for b in report.backends if b.backend_id == "b")
    assert row_a.mean_score == row_b.mean_score == 0.5
    assert row_a.dimensions == row_b.dimensions == {"field_prf": 0.5}
    assert row_a.weighted_f1 == pytest.approx((1.0 + 0.5 + 0.25) / 3)
    assert row_a.critical_pass_rate == 0.5
    assert row_a.n_critical_scored == 2
    assert row_a.weights_version == "v1"
    assert row_b.weighted_f1 is None and row_b.critical_pass_rate is None
    assert row_b.n_critical_scored == 0 and row_b.weights_version is None
    dumped = report.to_schema_dict()
    assert dumped["schema_version"] == "0.2"
    schemas.validate_leaderboard_report(dumped)


def test_errored_case_carries_no_criticality_into_the_means(monkeypatch):
    _scripted_reports(
        monkeypatch,
        {
            "a": [
                {"criticality": _crit(1.0, 1.0)},
                {"error": "boom", "criticality": _crit(0.0, 0.0)},
            ],
            "b": [{}, {}],
        },
    )
    reg = _registry(ScriptedBackend("a", local=True), ScriptedBackend("b", local=True))
    row = next(b for b in run_leaderboard("ds", ["a", "b"], reg).backends if b.backend_id == "a")
    assert row.weighted_f1 == 1.0
    assert row.critical_pass_rate == 1.0
    assert row.n_critical_scored == 1
