"""Ledger T2 — determinism (internal/design/ledger.md §7, internal/eng-council/plans/sprint26-T2-plan.md).

Required clock + RealClock banned from the walk (§7.2/§4.1); append-only follow-on records
(§7.3/§4.2); a visited-set guard for `_eval_reference` recursion (§4.3); decider/judge calls off
the event-loop thread (§4.4); and G4's own exit criterion (the BL-168 hash-seed matrix extended to
a full walk with a race and a paged cascade, journal diff empty).
"""

from __future__ import annotations

import base64
import difflib
import json
import os
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

from openreading.credentials import EnvCredentialBroker
from openreading.router.clock import FakeClock
from openreading.router.router import RouterConfig
from openreading.strategies import StrategyConfig, compile_strategy, run_strategy
from openreading.strategies.decider import DecisionVerdict
from openreading.strategies.engine import StrategyReferenceCycle
from openreading.testing.sample_pdf import build_sample_pdf
from openreading.types.request import OpenReadingRequest
from tests.fakes import ScriptedBackend, scripted_registry


def _req(name="a"):
    return OpenReadingRequest.model_validate(
        {
            "document": {"path": "/x.pdf", "mime_type": "application/pdf"},
            "backend": {"id": f"strategy:{name}"},
        }
    )


def _run(cfg_dict, name, registry):
    cfg = StrategyConfig.model_validate(cfg_dict)
    req = _req(name)
    compiled = compile_strategy(req, name, cfg, registry, RouterConfig())
    return run_strategy(
        compiled, req, registry=registry, broker=EnvCredentialBroker(), clock=FakeClock()
    )


# ---- §4.3: visited-set guard for _eval_reference recursion -------------------------------------


def test_direct_self_reference_raises_typed_cycle_error_not_recursion_error():
    reg = scripted_registry(ScriptedBackend("pymupdf", local=True))
    cfg = {"version": 1, "strategies": {"a": {"use": "a"}}}
    try:
        _run(cfg, "a", reg)
        raise AssertionError("expected StrategyReferenceCycle")
    except StrategyReferenceCycle as e:
        assert "a" in str(e)
    except RecursionError:
        raise AssertionError("cycle must raise a typed error, not exhaust the stack") from None


def test_indirect_reference_cycle_a_b_a_raises_typed_cycle_error():
    reg = scripted_registry(ScriptedBackend("pymupdf", local=True))
    cfg = {"version": 1, "strategies": {"a": {"use": "b"}, "b": {"use": "a"}}}
    try:
        _run(cfg, "a", reg)
        raise AssertionError("expected StrategyReferenceCycle")
    except StrategyReferenceCycle as e:
        assert "b" in str(e)
    except RecursionError:
        raise AssertionError("cycle must raise a typed error, not exhaust the stack") from None


def test_diamond_reference_same_tree_from_two_paths_is_not_a_false_positive_cycle():
    # `top` references `shared` twice via two different, non-overlapping paths — a legitimate
    # diamond shape the current no-guard code already permits; a walk-wide guard would wrongly
    # reject this as a cycle (T2 §4.3's own false-positive risk, resolved by per-path visited).
    reg = scripted_registry(ScriptedBackend("pymupdf", local=True))
    cfg = {
        "version": 1,
        "strategies": {
            "top": {"steps": [{"use": "shared"}, {"use": "shared2"}]},
            "shared": {"backend": "pymupdf"},
            "shared2": {"use": "shared"},
        },
    }
    result = _run(cfg, "top", reg)
    assert result.response.backend.id == "pymupdf"


# ---- §4.1: required clock, RealClock banned from the walk ---------------------------------------


def test_no_realclock_construction_or_import_in_engine_py():
    """Import/usage-shaped scan, not a literal substring scan (T2 §8 item 2, alex round-1 F1's
    concurrence): several prose comments in engine.py mention "RealClock" descriptively to explain
    why it must not be used, and a naive substring scan would flag that prose the moment its nearby
    code migrated to ctx.clock. Scoped to the two shapes that actually matter: an import naming
    RealClock, and a `RealClock(` constructor call — matching AC-18's own
    `"openreading.enterprise"`/`"import enterprise"` import-shaped precedent."""
    path = Path(__file__).resolve().parents[1] / "src" / "openreading" / "strategies" / "engine.py"
    text = path.read_text()
    offenders = [
        line
        for line in text.splitlines()
        if "RealClock(" in line or ("import" in line and "RealClock" in line)
    ]
    assert offenders == [], f"RealClock construction/import found in engine.py: {offenders}"


# ---- §4.4: decider/judge calls off the event-loop thread ---------------------------------------


def _decider_req():
    return OpenReadingRequest.model_validate(
        {
            "document": {
                "bytes_base64": base64.b64encode(build_sample_pdf()).decode(),
                "mime_type": "application/pdf",
            },
            "backend": {"id": "strategy:s"},
        }
    )


class _ThreadRecordingDeciderPort:
    """Records which thread `decide()` actually ran on."""

    def __init__(self, action: str):
        self.action = action
        self.thread: threading.Thread | None = None

    def decide(self, dp):
        self.thread = threading.current_thread()
        return DecisionVerdict(action=self.action, cost_usd=0.0, rationale="fake")


def test_decider_port_call_runs_off_the_event_loop_thread():
    # T2 §4.4: a synchronous DeciderPort call on the event-loop thread can starve sibling
    # coroutines — dispatched via asyncio.to_thread instead, so it never runs on the main thread.
    main_thread = threading.current_thread()
    port = _ThreadRecordingDeciderPort("escalate")
    reg = scripted_registry(
        ScriptedBackend("reducto", cost_low=0.01, confidence=0.5),  # low conf → review band fires
        ScriptedBackend("pymupdf", local=True),
    )
    cfg = {
        "version": 1,
        "strategies": {
            "s": {
                "steps": [
                    {
                        "backend": "reducto",
                        "review_if": {"confidence_below": 0.85},
                        "review_default": "accept",
                    },
                    "pymupdf",
                ],
            }
        },
        "decider": {"llm": {"backend": "pymupdf"}},
    }
    compiled = compile_strategy(
        _decider_req(), "s", StrategyConfig.model_validate(cfg), reg, RouterConfig()
    )
    run_strategy(
        compiled,
        _decider_req(),
        registry=reg,
        broker=EnvCredentialBroker(),
        clock=FakeClock(),
        env={"OPENREADING_LLM_DECIDER": "1"},
        decider_llm=port,
    )
    assert port.thread is not None, "decider port was never called"
    assert port.thread is not main_thread


# ---- §4.2: append-only follow-on records, not in-place mutation --------------------------------


def test_gate_escalation_appends_a_follow_on_revision_not_a_silent_overwrite():
    # T2 §7.3/§4.2: an Attempt's .gates/.category getting bound/recategorized after its own
    # creation must show up as a follow-on `revisions` entry, not just a silently-changed final
    # value — the Attempt's own identity (backend/node) is unchanged, satisfying no-schema-break.
    reg = scripted_registry(
        ScriptedBackend("reducto", cost_low=0.01, confidence=0.5),  # low conf → review band fires
        ScriptedBackend("pymupdf", local=True),
    )
    cfg = {
        "version": 1,
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
    compiled = compile_strategy(
        _decider_req(), "s", StrategyConfig.model_validate(cfg), reg, RouterConfig()
    )
    result = run_strategy(
        compiled, _decider_req(), registry=reg, broker=EnvCredentialBroker(), clock=FakeClock()
    )
    reducto_attempt = next(a for a in result.orchestration["attempts"] if a["backend"] == "reducto")
    assert (
        reducto_attempt["category"] == "review_escalated"
    )  # final value unchanged (no schema break)
    revisions = reducto_attempt["revisions"]
    kinds = [r["kind"] for r in revisions]
    assert "gates_bound" in kinds
    assert "recategorized" in kinds
    recategorized = next(r for r in revisions if r["kind"] == "recategorized")
    assert recategorized["from"] == "succeeded" and recategorized["to"] == "review_escalated"
    # gates_bound must be ORDERED before recategorized (the gate must exist before it can fire).
    assert kinds.index("gates_bound") < kinds.index("recategorized")


def test_two_paged_cascades_in_one_walk_both_contribute_pages_neither_clobbers_the_other():
    # T2 §7.3/§4.2: `ctx.trace.pages = [...]` was a bare reassignment — a second paged cascade in
    # one walk wiped out the first's own contribution. `assign_pages` must extend instead.
    from openreading.strategies.trace import Trace

    trace = Trace(strategy="s", config_hash="h")
    trace.assign_pages([{"page": 1, "backend": "a"}])
    trace.assign_pages([{"page": 1, "backend": "b"}])
    assert trace.pages == [{"page": 1, "backend": "a"}, {"page": 1, "backend": "b"}]


# ---- G4's own exit criterion: hash-seed matrix over a full walk with a race + a paged cascade --


def test_a_walk_with_a_race_and_a_paged_cascade_produces_a_byte_identical_journal_across_hash_seeds():
    """G4 (internal/runs/RUN.md §1): the same strategy walk, containing a race and a paged cascade, run
    under PYTHONHASHSEED 0-4 in separate processes, must produce a byte-identical journal.

    INLINE-mode ScriptedBackends only (never a POLL-mode backend) for the race component: a
    coordinated FakeClock cannot safely back a POLL-mode branch dispatched via asyncio.to_thread
    (internal/eng-council/FOUNDER-INBOX.md 2026-08-22, router/clock.py's own FakeClock docstring) — not
    what this test is probing anyway, since production always uses RealClock, which this test
    also uses (a coordinated FakeClock is a test-only construct; PYTHONHASHSEED sensitivity is
    orthogonal to which clock backs the walk).

    Calls `run_strategy`/`api._arm_ledger` directly rather than through `api.run_request`:
    the public API always builds its own real production registry internally
    (`_run_strategy_request`'s own `registry = build_registry()`) with no way to inject a fake
    one, so a `ScriptedBackend`-based registry has to be wired in by hand, mirroring exactly what
    `_arm_ledger` does for a real caller.

    The loser's `test_latency_ms` is a wall-clock margin, not a duration: under `RealClock` it is a
    real `asyncio.sleep` that runs BEFORE `ctx.exec()`, so it decides whether the loser is still
    pre-dispatch when the winner is picked — and therefore whether it journals two records or none.
    At the 5ms it used to carry, a contended runner sometimes let it wake first: 48 concurrent
    copies of this walk produced two different journals (37 without the loser's records, 11 with),
    which this test then reported as a hash-seed failure it never was. The margin has to be wider
    than any scheduling jitter, and costs nothing at any width because the branch is cancelled
    while it sleeps — the walk never waits it out. It is only ever waited out if `on_win: cancel`
    stops cancelling, which is the one regression that should be slow and loud here."""
    script = textwrap.dedent("""
        import json
        import sys
        import tempfile
        import os
        import uuid

        ledger_root = tempfile.mkdtemp()
        os.environ["OPENREADING_LEDGER"] = ledger_root

        from openreading.api import _arm_ledger
        from openreading.credentials import EnvCredentialBroker
        from openreading.router.clock import RealClock
        from openreading.strategies import StrategyConfig, compile_strategy, run_strategy
        from openreading.router.router import RouterConfig
        from openreading.types.request import OpenReadingRequest
        from tests.fakes import ScriptedBackend, scripted_registry

        GARBLED = "Ã©Ã¨ÃªÃ« " * 4
        GOOD = "the quick brown fox jumps over the lazy dog and it reads perfectly clean here " * 2

        from openreading.types.response import Page

        reg = scripted_registry(
            ScriptedBackend("fast_a", local=True, text=GARBLED, latency_ms=0),
            ScriptedBackend("fast_b", local=True, text=GARBLED, latency_ms=5000),
            ScriptedBackend(
                "page_ocr",
                local=True,
                pages=[
                    Page(page_number=1, text=GOOD, confidence=0.95),
                    Page(page_number=2, text=GOOD, confidence=0.95),
                ],
            ),
        )
        cfg = StrategyConfig.model_validate({
            "version": 1,
            "strategies": {
                "s": {
                    "steps": [
                        {
                            "parallel": [{"backend": "fast_a"}, {"backend": "fast_b"}],
                            "pick": "fastest",
                            "escalate_if": {"garbled": True},
                        },
                        {"granularity": "page", "steps": [{"backend": "page_ocr"}]},
                    ],
                }
            },
        })
        req = OpenReadingRequest.model_validate({
            "document": {"bytes_base64": "ZmFrZSBwZGYgYnl0ZXM=", "mime_type": "application/pdf"},
            "backend": {"id": "strategy:s"},
        })
        compiled = compile_strategy(req, "s", cfg, reg, RouterConfig())
        run_id = str(uuid.uuid4())
        clock = RealClock()
        executor = _arm_ledger(run_id, req, reg, EnvCredentialBroker(), clock, compiled.eligible)
        run_strategy(
            compiled, req, registry=reg, broker=EnvCredentialBroker(), clock=clock,
            run_id=run_id, executor=executor,
        )

        (journal_path,) = list(__import__("pathlib").Path(ledger_root).glob("*.jsonl"))
        lines = [json.loads(line) for line in journal_path.read_text().splitlines() if line.strip()]
        # normalize the genuinely run-scoped fields — everything else (content_key,
        # idempotency_key, category, gates, revisions, payload digest) must be identical.
        for rec in lines:
            rec.pop("run_id", None)
            rec.pop("step_id", None)
            rec.pop("started_epoch_ms", None)
            rec.pop("ended_epoch_ms", None)
            if isinstance(rec.get("payload"), dict):
                rec["payload"].pop("run_id", None)
        print(json.dumps(lines, sort_keys=True))
    """)
    outputs = []
    for seed in range(5):
        env = dict(os.environ, PYTHONHASHSEED=str(seed))
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, env=env
        )
        assert result.returncode == 0, result.stderr
        outputs.append(result.stdout)
    assert len(outputs[0].strip()) > 0  # sanity: the journal actually had content
    # Report the DIFF, not the journals: five full journals overflow pytest's own truncation, and
    # the one failure this test ever had arrived as five elided blobs with the difference cut out
    # of the middle. A record-level unified diff names the records that moved instead.
    odd = next((i for i, out in enumerate(outputs) if out != outputs[0]), None)
    assert odd is None, "seed 0 and seed {} produced different journals:\n{}".format(
        odd,
        "\n".join(
            difflib.unified_diff(
                [json.dumps(rec, sort_keys=True) for rec in json.loads(outputs[0])],
                [json.dumps(rec, sort_keys=True) for rec in json.loads(outputs[odd or 0])],
                "seed-0",
                f"seed-{odd}",
                lineterm="",
                n=0,
            )
        ),
    )
