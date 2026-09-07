"""Milestone 14.1 — decision-point plumbing: gate bands + decide nodes + engine defaults + the
two-key enablement gate + the compliance gate + the downgrade taxonomy (decider.md §2–§5).

Everything here is fully offline: `OPENREADING_LLM_DECIDER` is passed explicitly via the
`run_strategy(env=...)` seam (never mutating the real environment), and the LLM executor is either
absent (→ engine default) or a trivial in-process fake port. No live calls.
"""

from __future__ import annotations

import asyncio
import base64
import copy

from openreading.credentials import EnvCredentialBroker
from openreading.router.clock import FakeClock
from openreading.router.router import RouterConfig
from openreading.strategies import StrategyConfig, compile_strategy, run_strategy
from openreading.strategies.decider import (
    DeciderStatus,
    DecisionPoint,
    DecisionVerdict,
    JudgeCandidate,
    JudgeVerdict,
    _env_decider_backend,
    build_decider_tool,
    revalidate_action,
)
from openreading.strategies.engine import _decide, _WalkCtx
from openreading.strategies.trace import Trace
from openreading.testing.sample_pdf import build_sample_pdf
from openreading.types.request import OpenReadingRequest
from tests.fakes import ScriptedBackend, scripted_registry

CLEAN = "the quick brown fox jumps over the lazy dog every day here and now again " * 3


def _req(compliance: dict | None = None) -> OpenReadingRequest:
    body: dict = {
        "document": {
            "bytes_base64": base64.b64encode(build_sample_pdf()).decode(),
            "mime_type": "application/pdf",
        },
        "backend": {"id": "strategy:s"},
    }
    if compliance:
        body["compliance"] = compliance
    return OpenReadingRequest.model_validate(body)


def _run(
    cfg,
    reg,
    *,
    compliance=None,
    env=None,
    decider_llm=None,
    judge_llm=None,
    replay=None,
    backend_allowlist=None,
):
    r = _req(compliance)
    compiled = compile_strategy(
        r,
        "s",
        StrategyConfig.model_validate(cfg),
        reg,
        RouterConfig(),
        backend_allowlist=backend_allowlist,
    )
    return run_strategy(
        compiled,
        r,
        registry=reg,
        broker=EnvCredentialBroker(),
        clock=FakeClock(),
        env=env,
        decider_llm=decider_llm,
        judge_llm=judge_llm,
        replay=replay,
    )


def _decisions(res):
    return res.orchestration["decisions"]


# ---- gate band (review_if) --------------------------------------------------------------------


def _review_cfg(review_default: str, *, decider: dict | None = None):
    cfg = {
        "version": 1,
        "strategies": {
            "s": {
                "steps": [
                    {
                        "backend": "reducto",
                        "review_if": {"confidence_below": 0.85},
                        "review_default": review_default,
                    },
                    "pymupdf",
                ],
            }
        },
    }
    if decider is not None:
        cfg["decider"] = decider
    return cfg


def _review_reg():
    return scripted_registry(
        ScriptedBackend("reducto", cost_low=0.01, text=CLEAN, confidence=0.5),  # low conf → band
        ScriptedBackend("pymupdf", local=True, text=CLEAN),
    )


def test_review_band_accept_returns_first_step():
    res = _run(_review_cfg("accept"), _review_reg())
    assert res.response.backend.id == "reducto"  # engine default accept → keep the cheap rung
    d = _decisions(res)[0]
    assert d["point"] == "gate_band" and d["chosen"] == "accept" and d["decider"] == "engine"


def test_review_band_env_disabled_downgrade():
    # a decider block is configured but OPENREADING_LLM_DECIDER is not set → env_disabled (§3.4)
    res = _run(_review_cfg("escalate", decider={"llm": {"backend": "pymupdf"}}), _review_reg())
    d = _decisions(res)[0]
    assert d["decider"] == "engine" and d["downgraded"] == "env_disabled"
    assert d["chosen"] == "escalate"  # still the engine default


# ---- decide node ------------------------------------------------------------------------------


def _decide_cfg(*, decider: dict | None = None, among=None, otherwise="pymupdf"):
    cfg = {
        "version": 1,
        "strategies": {
            "s": {"decide": {"among": among or ["docling"], "otherwise": otherwise}},
        },
    }
    if decider is not None:
        cfg["decider"] = decider
    return cfg


def _decide_reg():
    return scripted_registry(
        ScriptedBackend("docling", local=True, text=CLEAN),
        ScriptedBackend("pymupdf", local=True, text=CLEAN),
        ScriptedBackend("reducto", cost_low=0.01, text=CLEAN),  # a hosted decider backend
    )


def test_decide_engine_default_takes_otherwise():
    res = _run(_decide_cfg(), _decide_reg())
    assert res.response.backend.id == "pymupdf"
    d = _decisions(res)[0]
    assert d["point"] == "decide"
    assert d["chosen"] == "otherwise"
    assert d["decider"] == "engine"
    assert d["downgraded"] is None
    assert d["eligible"] == ["docling", "otherwise"]
    assert d["decision_id"].startswith("dp_")


def test_decide_env_disabled_downgrade():
    res = _run(_decide_cfg(decider={"llm": {"backend": "reducto"}}), _decide_reg())
    d = _decisions(res)[0]
    assert d["decider"] == "engine" and d["downgraded"] == "env_disabled"


def test_decide_unavailable_when_enabled_eligible_but_no_executor():
    # env ON, decider backend local+eligible, but no LLM executor is wired (14.1 state) → unavailable
    res = _run(
        _decide_cfg(decider={"llm": {"backend": "pymupdf"}}),
        _decide_reg(),
        env={"OPENREADING_LLM_DECIDER": "1"},
    )
    d = _decisions(res)[0]
    assert d["decider"] == "engine" and d["downgraded"] == "unavailable"


# ---- the LLM executor seam (14.2 plugs in here) -----------------------------------------------


class _FakePort:
    """A trivial in-process decider — proves the seam + engine re-validation without a live call."""

    def __init__(self, action: str, cost_usd: float = 0.0):
        self.action = action
        self.cost_usd = cost_usd
        self.seen: list[DecisionPoint] = []

    def decide(self, dp: DecisionPoint) -> DecisionVerdict:
        self.seen.append(dp)
        return DecisionVerdict(action=self.action, cost_usd=self.cost_usd, rationale="fake")


def test_llm_port_out_of_set_revalidates_to_malformed():
    port = _FakePort("teleport")  # not a member of the candidate list
    res = _run(
        _review_cfg("escalate", decider={"llm": {"backend": "pymupdf"}}),
        _review_reg(),
        env={"OPENREADING_LLM_DECIDER": "1"},
        decider_llm=port,
    )
    d = _decisions(res)[0]
    assert d["decider"] == "engine" and d["downgraded"] == "malformed"
    assert d["chosen"] == "escalate"  # re-validation fell back to the engine default


# ---- executor outage: a raising port must never crash the walk --------------------------------


class _CrashingPort:
    """A decider whose call blows up mid-flight — an executor outage (transport error, SDK bug, a
    backend 500 the adapter re-raises). decider.md §3.4: EVERY failure resolves to the engine
    default, so one bad deploy of the decider must not take down every decide-node strategy."""

    def __init__(self) -> None:
        self.calls = 0

    def decide(self, dp: DecisionPoint) -> DecisionVerdict:
        self.calls += 1
        raise RuntimeError("decider backend exploded")


def test_decide_swallows_a_raising_port_and_returns_the_engine_default():
    port = _CrashingPort()
    ctx = _WalkCtx(
        req=_req(),
        registry=_decide_reg(),
        broker=EnvCredentialBroker(),
        clock=FakeClock(),
        trace=Trace("s", "cfg-hash"),
        trees={},
        eligible=[],
        decider_status=DeciderStatus("llm", None, "pymupdf"),
        decider_llm=port,
    )
    dp = DecisionPoint(
        decision_id="dp_unit",
        point="decide",
        node_path="s",
        label="s",
        candidates=["docling", "otherwise"],
        engine_default="otherwise",
    )
    assert asyncio.run(_decide(dp, ctx)) == (dp.engine_default, "engine", "malformed", {})
    assert port.calls == 1
    assert ctx.trace.attempts == []  # a call that never returned is never billed as a decider_call


def test_decide_node_survives_a_crashing_decider_port():
    port = _CrashingPort()
    res = _run(
        _decide_cfg(decider={"llm": {"backend": "pymupdf"}}),
        _decide_reg(),
        env={"OPENREADING_LLM_DECIDER": "1"},
        decider_llm=port,
    )
    assert port.calls == 1
    assert res.response.backend.id == "pymupdf"  # `otherwise:` dispatched — the walk completed
    d = _decisions(res)[0]
    assert d["decider"] == "engine" and d["downgraded"] == "malformed"
    assert d["chosen"] == "otherwise"
    assert not [a for a in res.orchestration["attempts"] if a["category"] == "decider_call"]


# ---- decide: the named-candidate dispatch (decider.md §2.2) ------------------------------------
#
# The engine-default path only ever reaches `otherwise:`; these cover the other arm — the decider
# names a real `among:` member and the engine must dispatch THAT member's sub-tree. Each config
# gives the named candidate a different backend than `otherwise:`, so a label that maps back to
# the wrong sub-tree changes the answering backend.


def _named_decide_cfg(*, among, otherwise, library=None):
    strategies: dict = {"s": {"decide": {"among": among, "otherwise": otherwise}}}
    strategies.update(library or {})
    return {"version": 1, "decider": {"llm": {"backend": "pymupdf"}}, "strategies": strategies}


def _named_decide_reg():
    return scripted_registry(
        ScriptedBackend("docling", local=True, text=CLEAN),
        ScriptedBackend("tesseract", local=True, text=CLEAN),
        ScriptedBackend("pymupdf", local=True, text=CLEAN),  # the decider backend, not a candidate
    )


def test_decide_llm_dispatches_the_named_candidate_string_form():
    port = _FakePort("docling")
    res = _run(
        _named_decide_cfg(among=["docling", "tesseract"], otherwise="tesseract"),
        _named_decide_reg(),
        env={"OPENREADING_LLM_DECIDER": "1"},
        decider_llm=port,
    )
    d = _decisions(res)[0]
    assert d["point"] == "decide"
    assert d["eligible"] == ["docling", "tesseract", "otherwise"]
    assert port.seen[0].candidates == ["docling", "tesseract", "otherwise"]
    assert d["decider"] == "llm" and d["downgraded"] is None
    assert d["chosen"] == "docling"  # a real among member, not the otherwise fallthrough
    assert res.response.backend.id == "docling"  # the engine default would have answered tesseract


def test_decide_llm_dispatches_the_named_candidate_use_ref_form():
    port = _FakePort("tables")
    res = _run(
        _named_decide_cfg(
            among=[{"use": "tables"}, {"use": "prose"}],
            otherwise={"use": "prose"},
            library={"tables": "docling", "prose": "tesseract"},
        ),
        _named_decide_reg(),
        env={"OPENREADING_LLM_DECIDER": "1"},
        decider_llm=port,
    )
    d = _decisions(res)[0]
    assert d["eligible"] == ["tables", "prose", "otherwise"]
    assert d["decider"] == "llm" and d["downgraded"] is None
    assert d["chosen"] == "tables"  # a reference candidate is named by its ref, not its backend
    assert res.response.backend.id == "docling"  # the named ref's tree ran, not prose's


def test_decide_llm_dispatches_a_labelled_sub_tree_candidate():
    port = _FakePort("two_pass")
    res = _run(
        _named_decide_cfg(
            among=[{"label": "two_pass", "steps": ["docling", "tesseract"]}, "tesseract"],
            otherwise="tesseract",
        ),
        _named_decide_reg(),
        env={"OPENREADING_LLM_DECIDER": "1"},
        decider_llm=port,
    )
    d = _decisions(res)[0]
    assert d["eligible"] == ["two_pass", "tesseract", "otherwise"]
    assert d["decider"] == "llm" and d["downgraded"] is None
    assert d["chosen"] == "two_pass"  # a keyless sub-tree is named by its label
    assert res.response.backend.id == "docling"  # the labelled cascade's first rung


# ---- unit: enablement + env override ----------------------------------------------------------


def test_env_decider_backend_tokens():
    assert _env_decider_backend({}, "file-be") == (False, "file-be")
    assert _env_decider_backend({"OPENREADING_LLM_DECIDER": "0"}, "file-be") == (False, "file-be")
    assert _env_decider_backend({"OPENREADING_LLM_DECIDER": "1"}, "file-be") == (True, "file-be")
    # a backend-id value both enables and overrides the file backend
    assert _env_decider_backend({"OPENREADING_LLM_DECIDER": "anthropic-claude"}, "file-be") == (
        True,
        "anthropic-claude",
    )


# ---- determinism ------------------------------------------------------------------------------


def test_decision_records_are_deterministic():
    def once():
        res = _run(_review_cfg("escalate"), _review_reg())
        return _decisions(res)

    assert once() == once()  # identical decision records incl. decision_id under FakeClock


# ---- decider_call metering (14.2) -------------------------------------------------------------


def test_decider_call_is_metered_as_an_attempt():
    port = _FakePort("escalate", cost_usd=0.02)
    res = _run(
        _review_cfg("escalate", decider={"llm": {"backend": "pymupdf"}}),
        _review_reg(),
        env={"OPENREADING_LLM_DECIDER": "1"},
        decider_llm=port,
    )
    metered = [a for a in res.orchestration["attempts"] if a["category"] == "decider_call"]
    assert len(metered) == 1
    assert metered[0]["cost_usd"] == 0.02  # the port's spend billed as a decider_call (rail 3)
    d = _decisions(res)[0]
    assert d["decider"] == "llm" and d["rationale"] == "fake"


# ---- strict tool + re-validation (unit) -------------------------------------------------------


def test_build_decider_tool_action_enum_is_the_candidate_list():
    tool = build_decider_tool(["accept", "escalate"])
    assert tool["strict"] is True
    assert tool["input_schema"]["properties"]["action"]["enum"] == ["accept", "escalate"]
    assert tool["input_schema"]["additionalProperties"] is False
    assert tool["input_schema"]["properties"]["rationale"]["maxLength"] == 280


def test_revalidate_action():
    assert revalidate_action("accept", ["accept", "escalate"]) == ("accept", None)
    assert revalidate_action(None, ["accept", "escalate"]) == (None, "refusal")
    assert revalidate_action("teleport", ["accept", "escalate"]) == (None, "malformed")


# ---- LLM-as-judge (pick: best + judge:) -------------------------------------------------------

GARBLED_SHORT = "Ã©Ã¨ÃªÃ« Å â€™Ã±Â§Â¶"  # low quality AND shorter than CLEAN


class _LongestJudge:
    """Content-based judge: prefers the longer excerpt (a proxy for 'more complete'). Decides by
    CONTENT, so it is consistent across both orderings — sources are hidden."""

    def __init__(self, cost_usd: float = 0.0):
        self.cost_usd = cost_usd
        self.seen: list[tuple[JudgeCandidate, JudgeCandidate]] = []

    def compare(self, a: JudgeCandidate, b: JudgeCandidate, intent):
        self.seen.append((a, b))
        winner = "A" if len(a.excerpt) >= len(b.excerpt) else "B"
        return JudgeVerdict(winner=winner, cost_usd=self.cost_usd)


class _PositionJudge:
    """A pathologically position-biased judge: always picks position A. Across both orderings this
    yields an inconsistent verdict → a tie the engine must break deterministically."""

    def compare(self, a: JudgeCandidate, b: JudgeCandidate, intent):
        return JudgeVerdict(winner="A")


def _judge_cfg(judge_backend="pymupdf", branches=None):
    return {
        "version": 1,
        "strategies": {
            "s": {
                "parallel": branches or ["reducto", "aws-textract"],
                "pick": "best",
                "judge": {"backend": judge_backend, "intent": "pick the most complete"},
            }
        },
    }


def test_judge_picks_higher_quality_candidate():
    # aws-textract (CLEAN, longer) vs reducto (GARBLED, shorter); the longest-judge picks CLEAN.
    reg = scripted_registry(
        ScriptedBackend("reducto", cost_low=0.01, text=GARBLED_SHORT),
        ScriptedBackend("aws-textract", cost_low=0.01, text=CLEAN),
        ScriptedBackend("pymupdf", local=True, text=CLEAN),  # the (local, eligible) judge backend
    )
    res = _run(
        _judge_cfg(),
        reg,
        env={"OPENREADING_LLM_DECIDER": "1"},
        judge_llm=_LongestJudge(),
    )
    assert res.response.backend.id == "aws-textract"
    jd = [d for d in _decisions(res) if d["point"] == "judge"][0]
    assert jd["decider"] == "llm" and jd["downgraded"] is None and jd["chosen"] == "aws-textract"
    # metered: 2 judge_call attempts (both orderings of the single pair)
    calls = [a for a in res.orchestration["attempts"] if a["category"] == "judge_call"]
    assert len(calls) == 2


def test_judge_never_synthesizes_returns_a_real_candidate():
    reg = scripted_registry(
        ScriptedBackend("reducto", cost_low=0.01, text=GARBLED_SHORT),
        ScriptedBackend("aws-textract", cost_low=0.01, text=CLEAN),
        ScriptedBackend("pymupdf", local=True, text=CLEAN),
    )
    res = _run(_judge_cfg(), reg, env={"OPENREADING_LLM_DECIDER": "1"}, judge_llm=_LongestJudge())
    # the winner is a real backend's output verbatim, never a merge (never-fabricate)
    assert res.response.document.text == CLEAN
    assert res.response.backend.id == "aws-textract"


def test_judge_position_bias_is_a_tie_broken_deterministically():
    # both CLEAN (engine would tie on quality); the position-biased judge is inconsistent → tie →
    # cheaper backend wins (aws-textract 0.01 < reducto 0.10), regardless of branch order.
    reg = scripted_registry(
        ScriptedBackend("reducto", cost_low=0.10, text=CLEAN),
        ScriptedBackend("aws-textract", cost_low=0.01, text=CLEAN),
        ScriptedBackend("pymupdf", local=True, text=CLEAN),
    )
    res = _run(_judge_cfg(), reg, env={"OPENREADING_LLM_DECIDER": "1"}, judge_llm=_PositionJudge())
    assert res.response.backend.id == "aws-textract"  # tie → cheaper backend, deterministic


def test_judge_ungated_downgrades_to_engine_score():
    reg = scripted_registry(
        ScriptedBackend("reducto", cost_low=0.01, text=GARBLED_SHORT),
        ScriptedBackend("aws-textract", cost_low=0.01, text=CLEAN),
    )
    res = _run(_judge_cfg(), reg, judge_llm=_LongestJudge())  # no env → judge disabled
    assert res.response.backend.id == "aws-textract"  # engine composite picks CLEAN
    jd = [d for d in _decisions(res) if d["point"] == "judge"][0]
    assert jd["decider"] == "engine" and jd["downgraded"] == "env_disabled"


def test_decide_out_of_scope_decider_backend_downgrades_to_engine_traced():
    """The decider carries the identical hole as the judge above and is closed the same way — a
    `decider:` backend is called with the operator's key too. Closing one and leaving its twin is
    how a hole reopens the moment someone reads the fixed half and assumes the pattern."""
    res = _run(
        _decide_cfg(decider={"llm": {"backend": "reducto"}}),
        _decide_reg(),
        env={"OPENREADING_LLM_DECIDER": "1"},
        backend_allowlist=frozenset({"pymupdf", "docling", "tesseract"}),
    )
    d = _decisions(res)[0]
    assert d["decider"] == "engine" and d["downgraded"] == "scope_denied"


def test_judge_determinism():
    def once():
        reg = scripted_registry(
            ScriptedBackend("reducto", cost_low=0.01, text=GARBLED_SHORT),
            ScriptedBackend("aws-textract", cost_low=0.01, text=CLEAN),
            ScriptedBackend("pymupdf", local=True, text=CLEAN),
        )
        res = _run(
            _judge_cfg(), reg, env={"OPENREADING_LLM_DECIDER": "1"}, judge_llm=_LongestJudge()
        )
        return res.response.backend.id, [d for d in _decisions(res) if d["point"] == "judge"]

    assert once() == once()


# ---- replay (14.3 — TraceDecider) -------------------------------------------------------------


def test_replay_reproduces_the_logged_decision():
    # a live run: the port chooses accept (overriding the default escalate)
    live = _run(
        _review_cfg("escalate", decider={"llm": {"backend": "pymupdf"}}),
        _review_reg(),
        env={"OPENREADING_LLM_DECIDER": "1"},
        decider_llm=_FakePort("accept"),
    )
    assert live.response.backend.id == "reducto"
    decisions = live.orchestration["decisions"]

    # replay the trace with NO port and NO env — the logged choice is taken deterministically
    rep = _run(_review_cfg("escalate"), _review_reg(), replay=decisions)
    assert rep.response.backend.id == "reducto"  # same outcome as the live run
    d = _decisions(rep)[0]
    assert d["decider"] == "trace" and d["chosen"] == "accept" and d["downgraded"] is None
    # replay makes no live call → no decider_call attempt is billed
    assert not [a for a in rep.orchestration["attempts"] if a["category"] == "decider_call"]


def test_replay_missing_decision_is_trace_missing():
    # replay with an EMPTY trace → the decision point falls back to the engine default, traced
    rep = _run(_review_cfg("escalate"), _review_reg(), replay=[])
    d = _decisions(rep)[0]
    assert d["decider"] == "engine" and d["downgraded"] == "trace_missing"
    assert d["chosen"] == "escalate"  # the review_default


def test_replay_reproduces_the_judge_winner():
    def reg():
        return scripted_registry(
            ScriptedBackend("reducto", cost_low=0.01, text=GARBLED_SHORT),
            ScriptedBackend("aws-textract", cost_low=0.01, text=CLEAN),
            ScriptedBackend("pymupdf", local=True, text=CLEAN),
        )

    live = _run(
        _judge_cfg(), reg(), env={"OPENREADING_LLM_DECIDER": "1"}, judge_llm=_LongestJudge()
    )
    decisions = live.orchestration["decisions"]
    rep = _run(_judge_cfg(), reg(), replay=decisions)  # no judge port, no env
    assert rep.response.backend.id == live.response.backend.id
    jd = [d for d in _decisions(rep) if d["point"] == "judge"][0]
    assert jd["decider"] == "trace" and jd["chosen"] == "aws-textract"
    assert not [a for a in rep.orchestration["attempts"] if a["category"] == "judge_call"]


def test_replay_stale_judge_winner_falls_back_to_the_engine():
    # A STALE trace: the logged judge winner names a backend that is not a candidate in this run
    # (here `pymupdf` — registered, and the judge backend, but never a `parallel` branch). Replay
    # must refuse the stale name, fall back to the engine comparator, and trace the downgrade —
    # never synthesize a winner and never crash looking the stale name up among the candidates.
    def reg():
        return scripted_registry(
            ScriptedBackend("reducto", cost_low=0.01, text=GARBLED_SHORT),
            ScriptedBackend("aws-textract", cost_low=0.01, text=CLEAN),
            ScriptedBackend("pymupdf", local=True, text=CLEAN),
        )

    live = _run(
        _judge_cfg(), reg(), env={"OPENREADING_LLM_DECIDER": "1"}, judge_llm=_LongestJudge()
    )
    decisions = copy.deepcopy(live.orchestration["decisions"])
    judged = [d for d in decisions if d["point"] == "judge"]
    assert len(judged) == 1
    judged[0]["chosen"] = "pymupdf"  # the decision_id still matches; only the winner went stale

    rep = _run(_judge_cfg(), reg(), replay=decisions)  # no judge port, no env
    jd = [d for d in _decisions(rep) if d["point"] == "judge"][0]
    assert jd["decider"] == "engine" and jd["downgraded"] == "trace_missing"
    assert jd["eligible"] == ["reducto", "aws-textract"]  # the stale name is not among them
    assert jd["chosen"] == "aws-textract"  # the engine composite winner, a real candidate
    assert rep.response.backend.id == "aws-textract"
    assert rep.response.document.text == CLEAN  # that candidate's output verbatim
    assert not [a for a in rep.orchestration["attempts"] if a["category"] == "judge_call"]


def test_replay_is_deterministic():
    live = _run(
        _review_cfg("escalate", decider={"llm": {"backend": "pymupdf"}}),
        _review_reg(),
        env={"OPENREADING_LLM_DECIDER": "1"},
        decider_llm=_FakePort("accept"),
    )
    d = live.orchestration["decisions"]
    assert _decisions(_run(_review_cfg("escalate"), _review_reg(), replay=d)) == _decisions(
        _run(_review_cfg("escalate"), _review_reg(), replay=d)
    )


# ---- mask_fields + H6 canary redaction --------------------------------------------------------

CANARY = "SSN-000-CANARY-11-2222"


class _RecordingPort:
    """Captures every DecisionPoint it is shown, so a test can assert what the decider could see."""

    def __init__(self, action: str):
        self.action = action
        self.seen: list[DecisionPoint] = []

    def decide(self, dp: DecisionPoint) -> DecisionVerdict:
        self.seen.append(dp)
        return DecisionVerdict(action=self.action)


def _mask_cfg():
    return {
        "version": 1,
        "decider": {"llm": {"backend": "pymupdf", "mask_fields": ["ssn"]}},
        "strategies": {
            "s": {
                "steps": [
                    {
                        "backend": "reducto",
                        "review_if": {"confidence_below": 0.85},
                        "review_default": "escalate",
                    },
                    "pymupdf",
                ],
            }
        },
    }


def _mask_reg():
    return scripted_registry(
        ScriptedBackend(
            "reducto",
            cost_low=0.01,
            text=CLEAN,
            confidence=0.5,
            typed_fields={"ssn": {"value": CANARY}, "amount": {"value": "100.00"}},
        ),
        ScriptedBackend("pymupdf", local=True, text=CLEAN),
    )


def test_mask_fields_strips_from_both_the_port_input_and_the_log():
    port = _RecordingPort("escalate")
    res = _run(_mask_cfg(), _mask_reg(), env={"OPENREADING_LLM_DECIDER": "1"}, decider_llm=port)
    dp = port.seen[0]
    tf = dp.signals.get("typed_fields", {})
    assert "ssn" not in tf and "amount" in tf  # the masked field is gone; the rest remains
    logged = _decisions(res)[0]["signals"].get("typed_fields", {})
    assert "ssn" not in logged and "amount" in logged  # one rule, both sinks


def test_canary_masked_field_never_leaks_anywhere():
    # H6 extension: the masked field's value must not appear in ANY decider input NOR the trace.
    port = _RecordingPort("escalate")
    res = _run(_mask_cfg(), _mask_reg(), env={"OPENREADING_LLM_DECIDER": "1"}, decider_llm=port)
    # nowhere in what the decider saw
    assert CANARY not in str([dp.__dict__ for dp in port.seen])
    # nowhere in the orchestration (attempts + decisions + everything)
    import json as _json

    assert CANARY not in _json.dumps(res.orchestration, default=str)
