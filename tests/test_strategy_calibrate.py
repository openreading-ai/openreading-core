"""Milestone 15.2 — openreading calibrate (signals.md §5): derive gate thresholds from a labeled
sample. The pure sweep is unit-tested with synthetic observations; one integration test drives
calibrate_strategy end-to-end offline over a temp dataset with a fake rung-1 backend that varies
its confidence per case. It reuses the eval scorers and NEVER mutates the config.
"""

from __future__ import annotations

import json

import pytest

from openreading.strategies import StrategyConfig
from openreading.strategies.calibrate import (
    CalibrationReport,
    Observation,
    calibratable_predicates,
    calibrate_strategy,
    sweep_predicate,
)
from openreading.types.errors import ComplianceRefused, RetryableError, TerminalError
from tests.fakes import ConfigurableBackend, make_backend

# ---- pure sweep (no backend) ------------------------------------------------------------------


def _obs(pairs):
    # pairs: list of (name, doc_confidence, scorer_overall)
    return [Observation(n, {"doc_confidence": c}, s) for n, c, s in pairs]


def test_calibratable_predicates_only_numeric():
    gate = {"confidence_below": 0.8, "garbled": True, "chars_per_page_below": 100}
    assert set(calibratable_predicates(gate)) == {"confidence_below", "chars_per_page_below"}
    assert calibratable_predicates(None) == []


def test_sweep_emits_operating_points_across_the_domain():
    obs = _obs([("a", 0.9, 0.95), ("b", 0.5, 0.40), ("c", 0.7, 0.85)])
    sweep = sweep_predicate(obs, "confidence_below", rung1_cost=0.0, rung2_cost=0.10)
    assert sweep.predicate == "confidence_below"
    assert [p.threshold for p in sweep.points] == [round(i / 20, 2) for i in range(21)]
    # escalation rate is monotonic non-decreasing in the threshold for a *_below predicate
    rates = [p.escalation_rate for p in sweep.points]
    assert rates == sorted(rates)
    assert rates[0] == 0.0 and rates[-1] == 1.0  # threshold 0 → nobody; 1.0 → everybody (<1.0)


def test_sweep_recommends_threshold_closest_to_target_escalation():
    # confidences 0.4/0.6/0.9/0.95 → target 0.25 (1 of 4 escalates) → threshold ~0.5 fires only 0.4
    obs = _obs([("a", 0.40, 0.3), ("b", 0.60, 0.9), ("c", 0.90, 0.95), ("d", 0.95, 0.97)])
    sweep = sweep_predicate(
        obs, "confidence_below", rung1_cost=0.0, rung2_cost=0.10, target_escalation=0.25
    )
    assert sweep.recommended is not None
    assert sweep.recommended.escalation_rate == 0.25  # exactly one of four


def test_sweep_budget_is_a_hard_filter():
    obs = _obs([("a", 0.40, 0.3), ("b", 0.60, 0.4), ("c", 0.90, 0.95), ("d", 0.95, 0.97)])
    # rung-2 is pricey; a tight budget forbids high escalation → recommend a low-escalation threshold
    sweep = sweep_predicate(
        obs, "confidence_below", rung1_cost=0.01, rung2_cost=1.0, max_cost_per_doc=0.30
    )
    assert sweep.recommended is not None
    assert sweep.recommended.cost_per_doc <= 0.30 + 1e-9


def test_sweep_agreement_tracks_the_scorer():
    # the two low-confidence docs are exactly the two low-scorer docs → a mid threshold agrees fully
    obs = _obs([("a", 0.30, 0.2), ("b", 0.40, 0.3), ("c", 0.90, 0.95), ("d", 0.95, 0.97)])
    sweep = sweep_predicate(
        obs, "confidence_below", rung1_cost=0.0, rung2_cost=0.1, quality_bar=0.8
    )
    best = max(sweep.points, key=lambda p: p.scorer_agreement)
    assert best.scorer_agreement == 1.0  # a threshold exists that fires iff the scorer says bad


def test_missing_signal_never_escalates():
    obs = [Observation("x", {"doc_confidence": None}, 0.2)]
    sweep = sweep_predicate(obs, "confidence_below", rung1_cost=0.0, rung2_cost=0.1)
    assert all(p.escalation_rate == 0.0 for p in sweep.points)  # unavailable → never fires (§6)


def test_sweep_agreement_excludes_unscored_observations_not_false_agreement():
    # BL-79: an unlabeled document (scorer_overall=None, evals.scorers.score's honest "not labeled
    # yet" marker) must not read as agreeing with every threshold. Three equally low-confidence
    # docs are genuinely scored bad; a fourth, equally low-confidence document is unscored. A
    # threshold exists that (correctly) fires for all four by signal alone — scorer_agreement must
    # still be able to reach 1.0 over the three labeled docs, not cap at 0.75 the way silently
    # treating the unscored doc as a perfect (never-needs-escalation) label would.
    obs = _obs([("a", 0.2, 0.1), ("b", 0.2, 0.2), ("c", 0.2, 0.05)]) + [
        Observation("unlabeled", {"doc_confidence": 0.2}, None)
    ]
    sweep = sweep_predicate(
        obs, "confidence_below", rung1_cost=0.0, rung2_cost=0.1, quality_bar=0.8
    )
    best = max(sweep.points, key=lambda p: p.scorer_agreement)
    assert best.scorer_agreement == 1.0
    # the unscored doc still counts toward escalation_rate/cost — those are signal-only, no label
    # needed — so the same threshold that reaches full agreement escalates all four, not just the
    # three labeled ones.
    assert best.escalation_rate == 1.0


def test_sweep_is_deterministic():
    obs = _obs([("a", 0.4, 0.3), ("b", 0.9, 0.95)])
    kw = dict(rung1_cost=0.0, rung2_cost=0.1, target_escalation=0.5)
    a = sweep_predicate(obs, "confidence_below", **kw)
    b = sweep_predicate(obs, "confidence_below", **kw)
    assert [p.as_dict() for p in a.points] == [p.as_dict() for p in b.points]
    assert a.recommended.as_dict() == b.recommended.as_dict()


# ---- BL-87: n_scored surfaces how many observations actually carried a label -------------------


def test_calibration_report_as_dict_includes_n_scored_next_to_n_docs():
    report = CalibrationReport(
        strategy="s", n_docs=3, n_scored=1, rung1_backend="cheap", rung2_backend=None
    )
    d = report.as_dict()
    assert d["n_docs"] == 3
    assert d["n_scored"] == 1


# ---- integration: calibrate_strategy end-to-end, offline --------------------------------------


class _VaryingBackend(ConfigurableBackend):
    """A local rung-1 fake whose per-page confidence varies by case (consumed in dataset order), so
    a sweep has a real distribution to work over."""

    def __init__(self, backend_id, confidences):
        super().__init__(make_backend(backend_id, local=True).descriptor)
        self._confs = list(confidences)
        self._i = 0

    def normalize(self, job, ctx, req):
        from openreading.types.response import Document, Page

        c = self._confs[self._i % len(self._confs)]
        self._i += 1
        resp = super().normalize(job, ctx, req)
        resp.document = Document(
            text="a clean line of text " * 5, pages=[Page(page_number=1, text="x", confidence=c)]
        )
        return resp


class _FaultAtCase(ConfigurableBackend):
    """A local rung-1 fake that behaves normally for every case except the `fail_at`-th (0-indexed)
    submit()/normalize() call (`stage`), where it raises `error` instead. Tracks real call counts so
    a test can prove the cases before the fault made genuine, already-completed backend calls — the
    "already-paid-for" calls BL-107's own finding says the old unguarded loop silently discarded."""

    def __init__(self, backend_id, *, fail_at, error, stage="submit"):
        super().__init__(make_backend(backend_id, local=True).descriptor)
        self._fail_at = fail_at
        self._error = error
        self._stage = stage
        self.submit_calls = 0
        self.normalize_calls = 0

    def submit(self, req, ctx):
        i = self.submit_calls
        self.submit_calls += 1
        if self._stage == "submit" and i == self._fail_at:
            raise self._error
        return super().submit(req, ctx)

    def normalize(self, job, ctx, req):
        i = self.normalize_calls
        self.normalize_calls += 1
        if self._stage == "normalize" and i == self._fail_at:
            raise self._error
        return super().normalize(job, ctx, req)


def _dataset(tmp_path, n):
    for i in range(n):
        d = tmp_path / f"case_{i:02d}"
        d.mkdir()
        (d / "case.json").write_text(
            json.dumps(
                {
                    "name": f"c{i}",
                    "input": {"builtin_sample": True},
                    "expected": {"text_contains": ["clean"]},
                }
            )
        )
    return str(tmp_path)


def _cfg():
    return StrategyConfig.model_validate(
        {
            "version": 1,
            "strategies": {
                "s": {
                    "steps": [
                        {"backend": "cheap", "escalate_if": {"confidence_below": 0.85}},
                        "premium",
                    ]
                }
            },
        }
    )


def _registry():
    from openreading.router.registry import Registry

    reg = Registry()
    reg.register(_VaryingBackend("cheap", [0.30, 0.50, 0.90, 0.95]))
    reg.register(ConfigurableBackend(make_backend("premium", cost_low=0.05).descriptor))
    return reg


def test_calibrate_strategy_recommends_and_reuses_scorers(tmp_path):
    cfg = _cfg()
    report = calibrate_strategy(_dataset(tmp_path, 4), cfg, "s", _registry(), target_escalation=0.5)
    assert report.n_docs == 4 and report.rung1_backend == "cheap"
    assert [s.predicate for s in report.sweeps] == ["confidence_below"]
    block = report.recommended_block()
    assert "escalate_if" in block and "confidence_below" in block["escalate_if"]
    # the scorer was consulted: every operating point carries a scorer_agreement in [0,1]
    pts = report.sweeps[0].points
    assert all(0.0 <= p.scorer_agreement <= 1.0 for p in pts)


def test_calibrate_never_mutates_the_config(tmp_path):
    cfg = _cfg()
    before = cfg.model_dump()
    calibrate_strategy(_dataset(tmp_path, 4), cfg, "s", _registry(), target_escalation=0.5)
    assert cfg.model_dump() == before  # the tool proposes; the config is untouched


def test_calibrate_strategy_is_deterministic(tmp_path):
    ds = _dataset(tmp_path, 4)
    a = calibrate_strategy(ds, _cfg(), "s", _registry(), target_escalation=0.5).as_dict()
    b = calibrate_strategy(ds, _cfg(), "s", _registry(), target_escalation=0.5).as_dict()
    assert a == b


# ---- BL-79: unlabeled cases must not read as agreement, end to end ----------------------------


def test_calibrate_strategy_scorer_agreement_ignores_unlabeled_cases(tmp_path):
    # A dataset mixing labeled and unlabeled cases (the real repro shape: three cases genuinely
    # scored bad — their expected text never appears in the fake backend's constant output — plus
    # one left unlabeled with expected: {}, the ordinary "not labeled yet" shape). All four share
    # the same low confidence, so a threshold exists that (correctly) fires for all four by signal
    # alone. Before the fix, the unlabeled case's absence-of-signal silently scored a false-perfect
    # 1.0 and therefore permanently disagreed with any threshold that correctly flagged it
    # alongside its three equally-bad siblings, capping achievable scorer_agreement at 0.75 instead
    # of 1.0.
    ds = tmp_path / "dataset"
    ds.mkdir()
    for i in range(3):
        d = ds / f"bad_{i:02d}"
        d.mkdir()
        (d / "case.json").write_text(
            json.dumps(
                {
                    "name": f"bad{i}",
                    "input": {"builtin_sample": True},
                    "expected": {"text_contains": ["nonexistent needle"]},
                }
            )
        )
    unlabeled_dir = ds / "unlabeled"
    unlabeled_dir.mkdir()
    (unlabeled_dir / "case.json").write_text(
        json.dumps({"name": "unlabeled", "input": {"builtin_sample": True}, "expected": {}})
    )

    from openreading.router.registry import Registry

    reg = Registry()
    reg.register(_VaryingBackend("cheap", [0.20]))  # constant low confidence for every case
    reg.register(ConfigurableBackend(make_backend("premium", cost_low=0.05).descriptor))

    report = calibrate_strategy(str(ds), _cfg(), "s", reg, quality_bar=0.8)
    assert report.n_docs == 4
    sweep = report.sweeps[0]
    assert max(p.scorer_agreement for p in sweep.points) == 1.0


# ---- BL-87: n_scored, end to end through calibrate_strategy ------------------------------------


def _mixed_dataset(tmp_path, cases):
    # cases: list of (name, expected) — unlike _dataset above, expected is caller-controlled so a
    # test can mix labeled and unlabeled ("not labeled yet", expected={}) cases in one dataset.
    for i, (name, expected) in enumerate(cases):
        d = tmp_path / f"case_{i:02d}"
        d.mkdir()
        (d / "case.json").write_text(
            json.dumps({"name": name, "input": {"builtin_sample": True}, "expected": expected})
        )
    return str(tmp_path)


def test_calibrate_strategy_reports_n_scored_for_a_partially_unlabeled_dataset(tmp_path):
    # Two cases carry the recognized text_contains dimension; one is unlabeled (expected={}, the
    # ordinary "not labeled yet" shape). n_scored must land strictly under n_docs, not equal it.
    ds = _mixed_dataset(
        tmp_path,
        [
            ("labeled_a", {"text_contains": ["clean"]}),
            ("labeled_b", {"text_contains": ["clean"]}),
            ("unlabeled", {}),
        ],
    )
    report = calibrate_strategy(ds, _cfg(), "s", _registry(), target_escalation=0.5)
    assert report.n_docs == 3
    assert report.n_scored == 2
    assert report.as_dict()["n_scored"] == 2


def test_calibrate_strategy_n_scored_is_zero_when_nothing_is_labeled(tmp_path):
    # The exact fixture shape the backlog's own repro uses: every case's expected is {}.
    # escalation_rate still varies (real signal) but n_scored must say plainly that
    # scorer_agreement measured nothing at all.
    ds = _mixed_dataset(tmp_path, [(f"c{i}", {}) for i in range(3)])
    report = calibrate_strategy(ds, _cfg(), "s", _registry(), target_escalation=0.5)
    assert report.n_docs == 3
    assert report.n_scored == 0


# ---- BL-107: per-case fault isolation — calibrate_strategy fails fast, naming the case, instead
# of leaking a bare RetryableError/UnsupportedFeatureError or a plain normalize() crash and
# silently discarding every already-scored case's Observation -------------------------------------


def _fault_registry(fake):
    from openreading.router.registry import Registry

    reg = Registry()
    reg.register(fake)
    reg.register(ConfigurableBackend(make_backend("premium", cost_low=0.05).descriptor))
    return reg


def test_calibrate_strategy_retryable_error_from_submit_fails_fast_naming_the_case(tmp_path):
    # A RetryableError from submit() — rate-limit exhaustion, or the hard-coded 60s per-case
    # deadline tripping (router/driver.py's _DriveSliceExpired) — used to propagate bare. It must
    # keep its type (cmd_calibrate maps RetryableError to its own clean, coded exit) but gain the
    # case context submit() itself can't know about.
    ds = _dataset(tmp_path, 4)
    fault = RetryableError("rate limited", backend_code="rate_limited")
    fake = _FaultAtCase("cheap", fail_at=2, error=fault, stage="submit")

    with pytest.raises(RetryableError) as exc_info:
        calibrate_strategy(ds, _cfg(), "s", _fault_registry(fake), target_escalation=0.5)

    msg = str(exc_info.value)
    assert "c2" in msg  # names the failing case
    assert "2 case(s) already scored" in msg  # c0 and c1 succeeded first
    assert exc_info.value.backend_code == "rate_limited"  # AdapterError metadata survives
    # c0/c1 genuinely ran to completion (real, already-paid-for backend calls); c3 was never
    # reached — the loop stopped at the fault, it didn't bail out at case 0.
    assert fake.submit_calls == 3
    assert fake.normalize_calls == 2


def test_calibrate_strategy_plain_crash_from_normalize_wraps_into_a_clean_terminal_error(tmp_path):
    # A plain, non-AdapterError crash out of normalize() — the exact class BL-99 hardened five
    # other call sites against but never reached here — used to propagate as a bare
    # KeyError/IndexError/ValueError/AttributeError, discarding every case's Observation computed
    # before it. It must come out as a clean TerminalError instead, never a raw traceback.
    ds = _dataset(tmp_path, 4)
    fake = _FaultAtCase("cheap", fail_at=1, error=KeyError("malformed_field"), stage="normalize")

    with pytest.raises(TerminalError) as exc_info:
        calibrate_strategy(ds, _cfg(), "s", _fault_registry(fake), target_escalation=0.5)

    msg = str(exc_info.value)
    assert "c1" in msg
    assert "1 case(s) already scored" in msg  # c0 succeeded first
    assert "KeyError" in msg
    # c0's normalize() genuinely completed; c1's submit() succeeded but its normalize() is where
    # the fault hit; c2/c3 were never reached.
    assert fake.submit_calls == 2
    assert fake.normalize_calls == 2


# ---- BL-112: calibrate_strategy gates the rung-1 backend on compliance, per case, before any
# adapter.submit() call — the strategy file's own policy: block, folded in exactly the way
# compile_strategy folds it, is a live channel through the ordinary, documented `calibrate` CLI
# workflow regardless of what any individual case.json carries. -------------------------------


class _CountingBackend(ConfigurableBackend):
    """Counts real submit() calls, so a compliance-refusal test can prove ComplianceRefused fires
    before adapter.submit() is ever reached — not merely that the report ends up empty."""

    def __init__(self, descriptor):
        super().__init__(descriptor)
        self.submit_calls = 0

    def submit(self, req, ctx):
        self.submit_calls += 1
        return super().submit(req, ctx)


def _cfg_with_policy(policy):
    return StrategyConfig.model_validate(
        {
            "version": 1,
            "policy": policy,
            "strategies": {
                "s": {
                    "steps": [
                        {"backend": "cheap", "escalate_if": {"confidence_below": 0.85}},
                        "premium",
                    ]
                }
            },
        }
    )


def _registry_with(cheap):
    from openreading.router.registry import Registry

    reg = Registry()
    reg.register(cheap)
    reg.register(ConfigurableBackend(make_backend("premium", cost_low=0.05).descriptor))
    return reg


def test_calibrate_strategy_refuses_a_noncompliant_rung1_backend_before_submit(tmp_path):
    # The strategy file's own policy: block requires a local backend; "cheap" (rung 1) is a
    # non-local, no-BAA hosted vendor. Before this fix, calibrate_strategy never read
    # router_config/config.policy at all and ran the hosted backend to completion regardless of
    # policy — this must now raise ComplianceRefused before adapter.submit() is ever called,
    # matching compile_strategy's own behavior for the identical policy + backend pair.
    fake = _CountingBackend(make_backend("cheap", local=False, hipaa_baa="no").descriptor)
    reg = _registry_with(fake)
    cfg = _cfg_with_policy({"require_local": True})

    with pytest.raises(ComplianceRefused):
        calibrate_strategy(_dataset(tmp_path, 4), cfg, "s", reg, target_escalation=0.5)

    assert fake.submit_calls == 0  # refused before any backend call


def test_calibrate_strategy_compliant_rung1_backend_is_unaffected(tmp_path):
    # Positive control: calibrate must keep working, unaffected, for every deployment this bug
    # hasn't been silently letting through — a rung-1 backend that DOES satisfy the strategy
    # file's own policy: block runs to completion exactly as before.
    fake = _CountingBackend(make_backend("cheap", local=True).descriptor)
    reg = _registry_with(fake)
    cfg = _cfg_with_policy({"require_local": True})

    report = calibrate_strategy(_dataset(tmp_path, 4), cfg, "s", reg, target_escalation=0.5)

    assert report.n_docs == 4
    assert fake.submit_calls == 4


def test_calibrate_strategy_refuses_on_a_per_case_compliance_field_once_forwarded(tmp_path):
    # The second, non-blocking channel (acceptance criteria's "or by a request-level compliance
    # field, once evals/dataset.py forwards one"): a case.json's own compliance block, forwarded
    # by load_case (the sub-requirement), must gate exactly like the file policy: block above —
    # with NO strategy-file policy: set at all, so this exercises req.compliance alone.
    fake = _CountingBackend(make_backend("cheap", local=False, hipaa_baa="no").descriptor)
    reg = _registry_with(fake)
    cfg = _cfg()  # no policy: block

    ds = tmp_path / "dataset"
    ds.mkdir()
    case_dir = ds / "case_00"
    case_dir.mkdir()
    case_dir.joinpath("case.json").write_text(
        json.dumps(
            {
                "name": "c0",
                "input": {"builtin_sample": True},
                "compliance": {"require_local": True},
                "expected": {"text_contains": ["clean"]},
            }
        )
    )

    with pytest.raises(ComplianceRefused):
        calibrate_strategy(str(ds), cfg, "s", reg, target_escalation=0.5)

    assert fake.submit_calls == 0


def test_calibrate_strategy_two_cases_may_carry_two_different_compliance_blocks(tmp_path):
    # Per-case gating (not once, unlike _run_native's uniform-batch invariant): case 0 is
    # unconstrained and must succeed; case 1 requires a local backend the hosted rung-1 backend
    # can't satisfy and must refuse — proving the gate runs fresh for each case rather than being
    # decided once for the whole sample.
    fake = _CountingBackend(make_backend("cheap", local=False, hipaa_baa="no").descriptor)
    reg = _registry_with(fake)
    cfg = _cfg()  # no policy: block — only the per-case field is in play

    ds = tmp_path / "dataset"
    ds.mkdir()
    ds.joinpath("case_00").mkdir()
    ds.joinpath("case_00", "case.json").write_text(
        json.dumps(
            {"name": "c0", "input": {"builtin_sample": True}, "expected": {"text_contains": []}}
        )
    )
    ds.joinpath("case_01").mkdir()
    ds.joinpath("case_01", "case.json").write_text(
        json.dumps(
            {
                "name": "c1",
                "input": {"builtin_sample": True},
                "compliance": {"require_local": True},
                "expected": {},
            }
        )
    )

    with pytest.raises(ComplianceRefused) as exc_info:
        calibrate_strategy(str(ds), cfg, "s", reg, target_escalation=0.5)

    # BL-123: the compliance gate (BL-112) sits outside BL-107's own per-case fault-isolation try,
    # so a ComplianceRefused past case 0 reached the caller with no case name and no "already
    # scored" count — unlike every other AdapterError this same loop raises. Same assertion style
    # as test_calibrate_strategy_retryable_error_from_submit_fails_fast_naming_the_case above.
    msg = str(exc_info.value)
    assert "c1" in msg  # names the failing case
    assert "1 case(s) already scored" in msg  # c0 succeeded first

    # c0 (no compliance constraint) genuinely ran before c1's refusal stopped the loop.
    assert fake.submit_calls == 1
