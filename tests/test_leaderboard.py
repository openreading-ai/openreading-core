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
from openreading.evals.runner import run_dataset
from openreading.router import RouterConfig
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


def test_ac1_ac4_ac5_ranks_backends_with_cost_and_dataset_identity(tmp_path):
    ds = _two_case_dataset(tmp_path)
    good = ScriptedBackend("fast-local", local=True, text="a fox and a zebra ran together")
    bad = ScriptedBackend("slow-hosted", local=False, cost_low=0.02, text="nothing relevant here")
    reg = _registry(good, bad)

    report = run_leaderboard(str(ds), ["slow-hosted", "fast-local"], reg)

    # AC-5: dataset identity is IN the report, not implied.
    assert report.dataset.path == str(ds)
    assert report.dataset.case_count == 2
    assert report.dataset.case_names == ["case_a", "case_b"]

    # AC-1: ranked best-first by measured mean score, and that mean IS DatasetReport.mean_overall
    # for that exact adapter over that exact dataset — cross-checked against run_dataset directly,
    # not merely equal by coincidence, proving no second/parallel scoring path exists.
    assert [b.backend_id for b in report.backends] == ["fast-local", "slow-hosted"]
    direct_good = run_dataset(good, ds)
    direct_bad = run_dataset(bad, ds)
    assert report.backends[0].mean_score == pytest.approx(direct_good.mean_overall) == 1.0
    assert report.backends[1].mean_score == pytest.approx(direct_bad.mean_overall) == 0.0

    # AC-4: every ranked row carries score AND cost basis together — never one without the other,
    # and the cost is the REAL descriptor-derived number (0 for local, rate*25 for hosted), not a
    # placeholder.
    fast = next(b for b in report.backends if b.backend_id == "fast-local")
    slow = next(b for b in report.backends if b.backend_id == "slow-hosted")
    assert fast.cost_per_doc == pytest.approx(0.0)
    assert slow.cost_per_doc == pytest.approx(0.5)  # 0.02 * 25 (the calibrate.py cost proxy)

    # per-dimension breakdown is present and reflects the single exercised dimension.
    assert fast.dimensions == {"text_contains": pytest.approx(1.0)}
    assert slow.dimensions == {"text_contains": pytest.approx(0.0)}

    # per-case winner table names a winner for every case.
    names = [c.name for c in report.cases]
    assert names == ["case_a", "case_b"]
    assert all(c.winner == "fast-local" for c in report.cases)


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
            "compliance": {"require_baa": True},
            "expected": {},
        },
    )
    return ds


def test_ac2_compliance_refusal_is_scored_not_skipped_and_never_widens_eligibility(tmp_path):
    ds = _hipaa_dataset(tmp_path)
    allowed = ScriptedBackend("phi-yes", local=False, hipaa_baa="yes", text="ok")
    refused = ScriptedBackend("phi-no", local=False, hipaa_baa="no", text="ok")
    reg = _registry(allowed, refused)

    report = run_leaderboard(str(ds), ["phi-yes", "phi-no"], reg)

    # the case is still IN the report — never a silently-skipped case.
    assert [c.name for c in report.cases] == ["phi_case"]
    assert report.cases[0].scores["phi-no"] is None  # error-carrying, never a fabricated 0.0
    assert report.cases[0].winner is None or report.cases[0].winner == "phi-yes"

    refused_row = next(b for b in report.backends if b.backend_id == "phi-no")
    allowed_row = next(b for b in report.backends if b.backend_id == "phi-yes")
    assert refused_row.errors == 1  # visible in the error tally
    assert allowed_row.errors == 0

    # never a widened compliance-eligible set: the refused backend's submit() was NEVER called —
    # the SAME gate run_case already applies (comp.evaluate before submit()) fired here too.
    assert len(refused.requests) == 0
    assert len(allowed.requests) == 1

    # identical to run_case's own contract at the DatasetReport level too (the mechanism leaderboard
    # reuses, not reimplements).
    direct = run_dataset(refused, ds)
    assert direct.results[0].error is not None
    assert "ComplianceRefused" in direct.results[0].error


def test_ac2_router_config_is_threaded_through_for_a_tier_gated_baa(tmp_path):
    # Proves router_config genuinely reaches the per-case gate (not merely accepted and dropped) —
    # the same confirm-to-run behavior evals.runner.run_case already has, exercised here through
    # run_leaderboard's own parameter.
    ds = _hipaa_dataset(tmp_path)
    gated = ScriptedBackend("phi-gated", local=False, hipaa_baa="tier_gated", text="ok")
    other = ScriptedBackend("phi-other", local=False, hipaa_baa="yes", text="ok")
    reg = _registry(gated, other)

    refused = run_leaderboard(str(ds), ["phi-gated", "phi-other"], reg)
    assert next(b for b in refused.backends if b.backend_id == "phi-gated").errors == 1

    cfg = RouterConfig(baa_tier_confirmed=frozenset({"phi-gated"}))
    confirmed = run_leaderboard(str(ds), ["phi-gated", "phi-other"], reg, router_config=cfg)
    assert next(b for b in confirmed.backends if b.backend_id == "phi-gated").errors == 0


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


def test_ac9_leaderboard_never_touches_router_scoring_or_integration_priority(tmp_path):
    from openreading.adapters.registry import build_registry
    from openreading.router.router import _QUALITY_BY_PRIORITY

    def _snapshot():
        priorities = {
            a.descriptor.id: (
                a.descriptor.router.integration_priority if a.descriptor.router else None
            )
            for a in build_registry()
        }
        return dict(_QUALITY_BY_PRIORITY), priorities

    before_qbp, before_prio = _snapshot()

    ds = _two_case_dataset(tmp_path)
    a = ScriptedBackend("a", local=True, text="a fox and a zebra ran together")
    b = ScriptedBackend("b", local=True, text="a fox and a zebra ran together")
    run_leaderboard(str(ds), ["a", "b"], _registry(a, b))

    after_qbp, after_prio = _snapshot()
    assert before_qbp == after_qbp
    assert before_prio == after_prio
