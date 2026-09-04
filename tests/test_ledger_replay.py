"""Ledger T3 — replay and resume (internal/eng-council/plans/sprint26-T3-plan.md). G5's exit
criterion, a test per ship item (§6): zero-network replay (race-free + `on_win: cancel`), header
mismatch refusal (AC-4), `journal_seq` race/partial-`require` ordering (§7.4), a composite-vs-leaf
tie not crashing (§4.0 F11), `pinned_eligible` actually armed on resume with a live compliance-gate
refusal that fails the run rather than failing over (AC-14), missing-credentials journaled and
terminal on resume including "credentials became available" (AC-15), shred-then-resume-reports-
expired (AC-10), the AC-12 child-process-kill test scoped to `open_ocr`/`aws_textract`, and a
`KeyboardInterrupt`-during-`cmd_parse` test for exit code 6.

Most tests call `_arm_ledger`/`run_strategy` directly rather than through `api.run_request` —
mirroring `test_strategy_determinism.py`'s own `test_a_walk_with_a_race_and_a_paged_cascade_...`
pattern — since the public API always builds its own real production registry internally, with no
way to inject a `ScriptedBackend`-based one. `_resume` below is the same shape, with
`resume=True`, standing in for what `openreading resume <RUN_ID>` (`api.resume_run`) does against
the real registry.
"""

from __future__ import annotations

import asyncio
import base64
import json
import threading
import time
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from openreading import api
from openreading.api import _arm_ledger
from openreading.credentials import EnvCredentialBroker
from openreading.ledger.header import HeaderMismatch
from openreading.ledger.inline import InlineExecutor
from openreading.ledger.jsonl import JsonlJournal
from openreading.ledger.localfs import LocalFsBlobStore, LocalFsKeyStore
from openreading.ledger.step import StepRef, StepRequest, StepResult
from openreading.router.clock import RealClock
from openreading.router.router import RouterConfig
from openreading.strategies import StrategyConfig, compile_strategy, run_strategy
from openreading.strategies.engine import _step_id, _step_request
from openreading.testing.sample_pdf import build_sample_pdf
from openreading.types.errors import ComplianceRefused, PlanExhaustedError, TerminalError
from openreading.types.request import OpenReadingRequest
from tests.fakes import ScriptedBackend, scripted_registry

pytest.importorskip("fitz", reason="pymupdf not installed")


class _DelayedScriptedBackend(ScriptedBackend):
    """A `ScriptedBackend` whose `submit()` takes real wall-clock time — a genuine, threaded
    delay, unlike the engine's own per-branch `test_latency_ms` (which sleeps BEFORE `ctx.exec()`
    is ever reached and so can't exercise `InlineExecutor`'s own `CancelledError` handling around
    `asyncio.to_thread(run)`, §4.3a's own fix). Needed so an `on_win: cancel` race/require test has
    a branch still genuinely in flight — inside `run()` — when its sibling wins.

    `signal`/`wait_for` order two branches against each other WITHOUT relying on their delays
    racing. A bare `delay_s` says only "this branch takes 100ms longer to start finishing", which
    is not the same claim as "this branch finishes second": on a contended runner the sibling's
    own dispatch can be preempted for longer than the delay, and it then journals first and wins
    the race for real (`_drive_race` judges by `journal_seq`, the true completion order). That is
    a correct engine and a test asserting timing it cannot guarantee — observed as a 3.13-only CI
    failure on an otherwise-green commit. A branch given `wait_for` blocks until the branch given
    the matching `signal` has returned from `submit()`, so the ordering is a happens-before rather
    than a wager, and `delay_s` on top is then a margin for the winner's journal append (an
    already-runnable event-loop callback) rather than the whole ordering guarantee.

    `gate_timeout_s` bounds the wait so that an engine change which stopped running branches
    concurrently fails this test on its assertion, the way it does today, instead of hanging."""

    def __init__(
        self,
        *args,
        delay_s: float = 0.0,
        signal: threading.Event | None = None,
        wait_for: threading.Event | None = None,
        gate_timeout_s: float = 5.0,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._delay_s = delay_s
        self._signal = signal
        self._wait_for = wait_for
        self._gate_timeout_s = gate_timeout_s

    def submit(self, req, ctx):
        if self._wait_for is not None:
            self._wait_for.wait(timeout=self._gate_timeout_s)
        if self._delay_s:
            time.sleep(self._delay_s)
        job = super().submit(req, ctx)
        if self._signal is not None:
            self._signal.set()
        return job


def _req(doc_bytes: bytes = b"fake pdf bytes for ledger replay tests") -> OpenReadingRequest:
    return OpenReadingRequest.model_validate(
        {
            "document": {
                "bytes_base64": base64.b64encode(doc_bytes).decode(),
                "mime_type": "application/pdf",
            },
            "backend": {"id": "strategy:s"},
        }
    )


def _cfg(steps: list[dict]) -> StrategyConfig:
    return StrategyConfig.model_validate({"version": 1, "strategies": {"s": {"steps": steps}}})


def _run_once(ledger_root, reg, cfg, req, *, name: str = "s", run_id: str | None = None):
    """A "fresh" arm + walk (the ORIGINAL run) — returns (run_id, compiled, StrategyResult)."""
    run_id = run_id or str(uuid.uuid4())
    clock = RealClock()
    compiled = compile_strategy(req, name, cfg, reg, RouterConfig())
    executor = _arm_ledger(
        run_id,
        req,
        reg,
        EnvCredentialBroker(),
        clock,
        compiled.eligible,
        config_hash=compiled.config_hash,
        plan_tree=compiled.root,
        strategy_name=name,
    )
    result = run_strategy(
        compiled,
        req,
        registry=reg,
        broker=EnvCredentialBroker(),
        clock=clock,
        run_id=run_id,
        executor=executor,
    )
    return run_id, compiled, result


def _resume(ledger_root, run_id, reg, cfg, req, *, name: str = "s"):
    """A resume-mode arm + re-drive against the SAME journal — the injectable-registry sibling of
    `api.resume_run` (which always builds the real production registry)."""
    clock = RealClock()
    compiled = compile_strategy(req, name, cfg, reg, RouterConfig())
    executor = _arm_ledger(
        run_id,
        req,
        reg,
        EnvCredentialBroker(),
        clock,
        compiled.eligible,
        config_hash=compiled.config_hash,
        plan_tree=compiled.root,
        strategy_name=name,
        resume=True,
    )
    return run_strategy(
        compiled,
        req,
        registry=reg,
        broker=EnvCredentialBroker(),
        clock=clock,
        run_id=run_id,
        executor=executor,
    )


# ---- Zero-network replay (AC-3) ----------------------------------------------------------------


def test_resume_replays_a_race_free_walk_with_zero_network_calls(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENREADING_LEDGER", str(tmp_path / "ledger"))
    ledger_root = tmp_path / "ledger"
    reg = scripted_registry(ScriptedBackend("solo", local=True, text="hello world"))
    cfg = _cfg([{"backend": "solo"}])
    req = _req()

    run_id, _compiled, result = _run_once(ledger_root, reg, cfg, req)
    assert result.response.document.text == "hello world"

    # A registry whose every adapter raises unconditionally on submit() — if the resumed walk ever
    # touched it for real, the AssertionError below would propagate straight out of this test.
    raising_reg = scripted_registry(
        ScriptedBackend("solo", local=True, error=AssertionError("must not dispatch"))
    )
    resumed = _resume(ledger_root, run_id, raising_reg, cfg, req)
    assert resumed.response.document.text == "hello world"


def test_resume_replays_a_cancelled_step_with_zero_network_calls(tmp_path):
    """§4.3a's own acceptance test (round-1 F1's replacement for the original false-negative-prone
    one): `InlineExecutor.exec`'s dispatch is cancelled while genuinely mid-flight — inside its own
    `asyncio.to_thread(run)` — exactly the shape a `parallel:` node's `on_win: cancel` produces for
    a losing branch still inside `ctx.exec()` when its sibling wins. Driven directly against
    `InlineExecutor` (rather than through a full `parallel:` walk under `RealClock`): `_drive_race`/
    `_drive_best` only ever declare a winner once every currently-dispatching branch's OWN
    `inflight` count has dropped to zero (`engine.py`'s own D-v3-16 comment — "no branch mid-
    execution"), so under `RealClock` a losing branch is only ever `t.cancel()`-ed while it is
    still in its PRE-dispatch `test_latency_ms`/`start_after` sleep — before `ctx.exec()` is ever
    entered at all (`test_race_loser_cancelled_before_running`'s own "cancelled during its latency
    sleep, before submit"). Proving §4.3a's own `CancelledError` handler therefore needs to cancel
    the task directly, the same way `test_ledger_journal.py`'s other `InlineExecutor.exec` tests
    exercise `exec()` in isolation rather than through a full engine walk."""
    ledger_root = tmp_path / "ledger"
    journal = JsonlJournal(ledger_root / "run1.jsonl")
    keys = LocalFsKeyStore(ledger_root / "keys")
    blobs = LocalFsBlobStore(ledger_root / "blobs", keys)
    ex = InlineExecutor(journal=journal, blobs=blobs, registry=None, clock=RealClock())
    req = StepRequest(
        step_id="s1",
        run_id="run1",
        kind="submit",
        step_path="root",
        step_seq=0,
        attempt=1,
        backend_id="slow",
    )

    async def _cancel_mid_flight() -> None:
        task = asyncio.ensure_future(ex.exec(req, run=lambda: time.sleep(0.2)))
        await asyncio.sleep(0.02)  # let it actually enter asyncio.to_thread(run)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_cancel_mid_flight())

    recs = journal.get(StepRef(run_id="run1", step_path="root", step_seq=0))
    assert [r.status for r in recs] == ["attempted", "cancelled"], (
        "a losing branch cancelled mid-dispatch must get its own terminal record, not just "
        "'attempted' (§4.3a) — otherwise it is indistinguishable from a genuine crash and would "
        "re-dispatch for real on resume"
    )

    # resume: a fresh reader Journal + a run() that would raise if ever called — zero-network proof.
    reader = JsonlJournal(ledger_root / "run1.jsonl")
    ex2 = InlineExecutor(journal=reader, blobs=blobs, registry=None, clock=RealClock())
    result = asyncio.run(
        ex2.exec(req, run=lambda: (_ for _ in ()).throw(AssertionError("must not dispatch")))
    )
    assert result.status == "cancelled"
    assert result.replayed is True
    assert result.journal_seq == recs[-1].journal_seq


def test_a_replayed_cancelled_cascade_rung_keeps_its_own_attempt_category(tmp_path, monkeypatch):
    """A rung whose journal record is `cancelled` is the shape an interrupted run leaves behind,
    and the ledger guide's own interrupt-and-resume walkthrough produces one. The resumed walk
    must not label that rung `skipped(missing_credentials)`, because a local backend needs no
    credentials and a fabricated reason reads downstream exactly like a measured one."""
    ledger_root = tmp_path / "ledger"
    monkeypatch.setenv("OPENREADING_LEDGER", str(ledger_root))
    reg = scripted_registry(
        ScriptedBackend("a", local=True, text="from a"),
        ScriptedBackend("b", local=True, text="from b"),
    )
    cfg = _cfg([{"backend": "a"}, {"backend": "b"}])
    req = _req()
    run_id = str(uuid.uuid4())
    clock = RealClock()
    compiled = compile_strategy(req, "s", cfg, reg, RouterConfig())
    _arm_ledger(
        run_id,
        req,
        reg,
        EnvCredentialBroker(),
        clock,
        compiled.eligible,
        config_hash=compiled.config_hash,
        plan_tree=compiled.root,
        strategy_name="s",
    )

    # The interrupted dispatch's own records, written directly: `attempted` then the `cancelled`
    # terminal record `InlineExecutor.exec`'s CancelledError handler leaves behind (§4.3a).
    step_req = _step_request(
        SimpleNamespace(run_id=run_id),
        "root.steps[0]",
        "a",
        reg.get("a").descriptor,
        req,
    )
    journal = JsonlJournal(ledger_root / f"{run_id}.jsonl")
    for status in ("attempted", "cancelled"):
        journal.append(
            StepResult(
                step_id=_step_id(run_id, "root.steps[0]", 0),
                run_id=run_id,
                step_path="root.steps[0]",
                step_seq=0,
                backend_id="a",
                status=status,
                attempt=1,
                idempotency_key=step_req.idempotency_key,
                content_key=step_req.content_key,
            )
        )

    resumed = _resume(ledger_root, run_id, reg, cfg, req)
    cats = [(a["backend"], a["category"]) for a in resumed.orchestration["attempts"]]
    assert resumed.response.document.text == "from b"
    assert ("a", "skipped(cancelled)") in cats, cats
    assert ("a", "skipped(missing_credentials)") not in cats, cats


# ---- M9: a replayed failure carries the ORIGINAL message, not a generic taxonomy name ----------


def test_resume_reconstructs_the_original_exception_message_not_a_generic_taxonomy_name(tmp_path):
    """M9 (security review): the failure record site (`InlineExecutor.exec`'s own `except
    Exception` clause) used to build `StepError(code=type(exc).__name__, taxonomy=...)` with no
    `detail` — `_reconstruct_error`'s own `err.detail or err.code` fallback then had nothing but
    the TAXONOMY CLASS NAME to reconstruct a message from, so a resumed run's re-raised exception
    read `TerminalError("TerminalError", ...)`: the original "quota exceeded" text a caller's own
    retry/backoff logic (or a human reading the trail) needs was silently gone. Zero-network proof,
    matching every other AC-3 test in this file: the resumed executor's `run` callable would raise
    if ever actually called."""
    ledger_root = tmp_path / "ledger"
    journal = JsonlJournal(ledger_root / "run1.jsonl")
    ex = InlineExecutor(journal=journal, blobs=None, registry=None, clock=RealClock())
    req = StepRequest(
        step_id="s1",
        run_id="run1",
        kind="submit",
        step_path="root",
        step_seq=0,
        attempt=1,
        backend_id="fake",
    )

    def boom():
        raise TerminalError("quota exceeded for tenant-42", backend_code="quota")

    with pytest.raises(TerminalError):
        asyncio.run(ex.exec(req, run=boom))

    # resume: a fresh reader Journal + a run() that would raise if ever called.
    reader = JsonlJournal(ledger_root / "run1.jsonl")
    ex2 = InlineExecutor(journal=reader, blobs=None, registry=None, clock=RealClock())
    with pytest.raises(TerminalError) as exc_info:
        asyncio.run(
            ex2.exec(req, run=lambda: (_ for _ in ()).throw(AssertionError("must not dispatch")))
        )
    assert "quota exceeded" in str(exc_info.value)


# ---- journal_seq ordering (§7.4) ----------------------------------------------------------------


def test_resume_picks_the_same_race_winner_via_journal_seq_not_scheduling_order(
    tmp_path, monkeypatch
):
    """`on_win: drain` so BOTH branches complete for real in the original run (neither is
    cancelled) — the winner is decided purely by `_drive_race`'s own real-completion-order
    tie-break. On resume every branch's `exec()` call resolves near-instantly (a replay lookup,
    never a real wait), so an index-order tie-break would pick branch 0 ("a") instead — proving
    the fix actually reads `journal_seq`, not merely that it compiles."""
    monkeypatch.setenv("OPENREADING_LEDGER", str(tmp_path / "ledger"))
    ledger_root = tmp_path / "ledger"
    # "b" finishes first BY CONSTRUCTION, not by winning a 100ms race: "a" cannot enter its own
    # delay until "b" has returned from submit(). See _DelayedScriptedBackend for why the delay
    # alone was not a guarantee.
    b_submitted = threading.Event()
    reg = scripted_registry(
        _DelayedScriptedBackend("a", local=True, text="from-a", delay_s=0.2, wait_for=b_submitted),
        _DelayedScriptedBackend("b", local=True, text="from-b", signal=b_submitted),
    )
    cfg = _cfg(
        [{"parallel": [{"backend": "a"}, {"backend": "b"}], "pick": "fastest", "on_win": "drain"}]
    )
    req = _req()

    run_id, _compiled, result = _run_once(ledger_root, reg, cfg, req)
    assert result.response.document.text == "from-b"  # "b" genuinely finishes first

    journal = JsonlJournal(ledger_root / f"{run_id}.jsonl")
    a_ok = journal.get(StepRef(run_id=run_id, step_path="root.steps[0].parallel[0]", step_seq=0))
    b_ok = journal.get(StepRef(run_id=run_id, step_path="root.steps[0].parallel[1]", step_seq=0))
    assert [r.status for r in a_ok] == ["attempted", "ok"]
    assert [r.status for r in b_ok] == ["attempted", "ok"]
    assert b_ok[-1].journal_seq < a_ok[-1].journal_seq, "b really did finish (and journal) first"

    raising_reg = scripted_registry(
        ScriptedBackend("a", local=True, error=AssertionError("must not dispatch")),
        ScriptedBackend("b", local=True, error=AssertionError("must not dispatch")),
    )
    resumed = _resume(ledger_root, run_id, raising_reg, cfg, req)
    assert resumed.response.document.text == "from-b"


def test_resume_admits_the_same_branches_under_drive_bests_partial_require(tmp_path, monkeypatch):
    """`pick: best`/`require: 2` (of 3): `_drive_best` only ever evaluates its threshold once every
    currently-dispatching branch's `inflight` count has dropped to zero (the same D-v3-16 gating
    `_drive_race` uses), so under `RealClock` all three branches genuinely complete before ANY
    admission decision is made — `require` bounds how many SUCCESSES are needed, not which ones
    arrive first. Resume must reproduce the identical winner with zero network calls, proving the
    `journal_seq`-ordered admission fix (§4.1, F5) is at minimum harmless and, for whichever shape
    of race a future engine change makes genuinely early-exiting, already replay-stable."""
    monkeypatch.setenv("OPENREADING_LEDGER", str(tmp_path / "ledger"))
    ledger_root = tmp_path / "ledger"
    reg = scripted_registry(
        ScriptedBackend("b1", local=True, text="one two three four five six seven"),
        ScriptedBackend("b2", local=True, text="one two three four five six seven eight"),
        ScriptedBackend("b3", local=True, text="one two three four five six seven eight nine"),
    )
    cfg = _cfg(
        [
            {
                "parallel": [{"backend": "b1"}, {"backend": "b2"}, {"backend": "b3"}],
                "pick": "best",
                "require": 2,
            }
        ]
    )
    req = _req()

    run_id, _compiled, result = _run_once(ledger_root, reg, cfg, req)
    original_text = result.response.document.text

    journal = JsonlJournal(ledger_root / f"{run_id}.jsonl")
    for i in range(3):
        recs = journal.get(
            StepRef(run_id=run_id, step_path=f"root.steps[0].parallel[{i}]", step_seq=0)
        )
        assert [r.status for r in recs] == ["attempted", "ok"]

    raising_reg = scripted_registry(
        ScriptedBackend("b1", local=True, error=AssertionError("must not dispatch")),
        ScriptedBackend("b2", local=True, error=AssertionError("must not dispatch")),
        ScriptedBackend("b3", local=True, error=AssertionError("must not dispatch")),
    )
    resumed = _resume(ledger_root, run_id, raising_reg, cfg, req)
    assert resumed.response.document.text == original_text


def test_a_composite_branch_tied_with_a_leaf_branch_does_not_crash_the_journal_seq_tiebreak(
    tmp_path, monkeypatch
):
    """§4.0 F11's own disclosed exclusion: a composite branch's `journal_seq` is always `None`
    (it never calls `ctx.exec()` at its own level). The `(journal_seq is None, journal_seq or 0,
    i)` sort key must not raise a `TypeError` comparing `None` against a real int when a composite
    branch ties with — or merely races alongside — a leaf branch."""
    monkeypatch.setenv("OPENREADING_LEDGER", str(tmp_path / "ledger"))
    ledger_root = tmp_path / "ledger"
    reg = scripted_registry(
        ScriptedBackend("composite-leaf", local=True, text="from composite"),
        ScriptedBackend("plain-leaf", local=True, text="from leaf"),
    )
    cfg = _cfg(
        [
            {
                "parallel": [
                    {"steps": [{"backend": "composite-leaf"}]},  # a nested cascade — composite
                    {"backend": "plain-leaf"},  # an ordinary leaf
                ],
                "pick": "fastest",
            }
        ]
    )
    req = _req()

    _run_id, _compiled, result = _run_once(ledger_root, reg, cfg, req)
    assert result.response.document.text in ("from composite", "from leaf")


# ---- Header mismatch refusal (AC-4) --------------------------------------------------------------


def test_resume_refuses_by_name_when_the_strategy_config_changed_since_the_original_run(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENREADING_LEDGER", str(tmp_path / "ledger"))
    pdf_path = tmp_path / "doc.pdf"
    pdf_path.write_bytes(build_sample_pdf())
    (tmp_path / "openreading.yaml").write_text(
        "version: 1\nstrategies:\n  s:\n    steps:\n      - backend: pymupdf\n"
    )
    armed: list[str] = []
    api.run(str(pdf_path), strategy="s", on_run_armed=armed.append)
    run_id = armed[0]

    (tmp_path / "openreading.yaml").write_text(
        "version: 1\nstrategies:\n  s:\n    steps:\n      - backend: pymupdf\n"
        "      - backend: pymupdf\n"
    )
    with pytest.raises(HeaderMismatch) as exc_info:
        api.resume_run(run_id)
    fields = {f[0] for f in exc_info.value.fields}
    assert fields & {"config_hash", "plan_hash"}


def test_cmd_resume_prints_the_refusal_shape_and_returns_exit_3(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENREADING_LEDGER", str(tmp_path / "ledger"))
    pdf_path = tmp_path / "doc.pdf"
    pdf_path.write_bytes(build_sample_pdf())
    (tmp_path / "openreading.yaml").write_text(
        "version: 1\nstrategies:\n  s:\n    steps:\n      - backend: pymupdf\n"
    )
    armed: list[str] = []
    api.run(str(pdf_path), strategy="s", on_run_armed=armed.append)
    run_id = armed[0]
    (tmp_path / "openreading.yaml").write_text(
        "version: 1\nstrategies:\n  s:\n    steps:\n      - backend: pymupdf\n"
        "      - backend: pymupdf\n"
    )

    from openreading.cli.app import build_parser

    args = build_parser().parse_args(["resume", run_id])
    rc = args.func(args)
    assert rc == 3
    err = capsys.readouterr().err
    assert "[resume] refused:" in err
    assert "changed since" in err
    assert "a resumed run replays recorded decisions; start a new run instead" in err


# ---- the REAL api.resume_run/cmd_resume path, end-to-end (Finding 10b) --------------------------


def test_api_resume_run_reconstructs_the_request_from_a_header_on_disk_and_replays_successfully(
    tmp_path, monkeypatch
):
    """Finding 5/10(b) (Phase C round-1): every OTHER replay test in this file drives
    `_arm_ledger`/`run_strategy` directly via `_resume` above, reusing the SAME in-memory `req`
    object the test itself built — never exercising `api.resume_run`/`_request_from_header`'s own
    disk-read reconstruction for a SUCCESSFUL resume (the only two existing `api.resume_run` call
    sites, above, both exercise the REFUSAL path). This calls the real `api.resume_run` — no
    injected registry, no in-memory shortcut — against a header actually read from disk, and proves
    the reconstructed request replays to the identical result.

    No `policy={"require_local": True}` workaround needed (Phase C round-2 Finding 8 / a reviewer
    Finding 6, now fixed): against the REAL, whole-registry `eligible` set (`prune.py`'s own
    `plan.eligible_ids`, computed over every registered backend, not just the named strategy step —
    see `slim_request_dict`'s own docstring), `reducto`'s real descriptor (the only built-in with a
    genuine `zdr_flag`, and `max_retention_hours=0`) is ALSO eligible for a plain PDF — but it is
    never named by this strategy nor dispatched, so it no longer has any effect on this run's ZDR
    gating or retention ceiling. This is exactly the ordinary, real-registry shape both reviewers'
    Finding 8/6 repros used, run through the real production registry (not a test fixture)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENREADING_LEDGER", str(tmp_path / "ledger"))
    pdf_path = tmp_path / "doc.pdf"
    pdf_path.write_bytes(build_sample_pdf())
    (tmp_path / "openreading.yaml").write_text(
        "version: 1\nstrategies:\n  s:\n    steps:\n      - backend: pymupdf\n"
    )
    armed: list[str] = []
    original = api.run(str(pdf_path), strategy="s", on_run_armed=armed.append)
    run_id = armed[0]

    resumed = api.resume_run(run_id)
    assert resumed["status"]["state"] == "succeeded"
    assert resumed["document"] == original["document"]


def test_an_unusable_ledger_root_fails_with_a_named_config_error_not_a_bare_oserror(
    tmp_path, monkeypatch, capsys
):
    """A45: arming the ledger turns journalling from a side-car into a hard dependency — an
    unwritable or non-directory `$OPENREADING_LEDGER` takes the whole parse down. That is the
    right posture (a run that cannot be journalled must not pretend it is resumable), but it used
    to arrive as the generic `[tag] error: PermissionError: [Errno 13] ...` at exit 1, the CLI's
    "unexpected error" rung, with the variable that caused it named nowhere. It is an operator
    config error like a malformed `OPENREADING_API_KEYS`, so it belongs on the same rung: exit 3,
    one tagged line, and the name of the knob to fix."""
    monkeypatch.chdir(tmp_path)
    pdf_path = tmp_path / "doc.pdf"
    pdf_path.write_bytes(build_sample_pdf())
    (tmp_path / "openreading.yaml").write_text(
        "version: 1\nstrategies:\n  s:\n    steps:\n      - backend: pymupdf\n"
    )
    not_a_dir = tmp_path / "a-file"
    not_a_dir.write_text("")
    monkeypatch.setenv("OPENREADING_LEDGER", str(not_a_dir))

    from openreading.cli.app import main

    rc = main(["parse", str(pdf_path), "--strategy", "s"])
    cap = capsys.readouterr()

    assert rc == 3
    assert cap.out == ""
    assert "OPENREADING_LEDGER" in cap.err  # names the knob, not just an errno and a path
    assert str(not_a_dir) in cap.err
    assert "Traceback" not in cap.err


def test_cmd_resume_keeps_backend_chatter_off_stdout(tmp_path, monkeypatch, capsys):
    """`resume` is the one command that let a backend's stdout advisory reach stdout ahead of the
    envelope, so `openreading resume $ID > out.json` produced a file `jq` refuses. Every other
    command that dispatches a backend already runs the call inside `redirect_stdout(sys.stderr)`;
    this pins that `resume` does too, on the recovery path where a broken redirect is discovered
    at 3 a.m. The advisory is injected rather than coaxed out of PyMuPDF: which library prints,
    and when, is not the invariant — stdout holding exactly one JSON document is."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENREADING_LEDGER", str(tmp_path / "ledger"))
    pdf_path = tmp_path / "doc.pdf"
    pdf_path.write_bytes(build_sample_pdf())
    (tmp_path / "openreading.yaml").write_text(
        "version: 1\nstrategies:\n  s:\n    steps:\n      - backend: pymupdf\n"
    )
    armed: list[str] = []
    api.run(str(pdf_path), strategy="s", on_run_armed=armed.append)

    real = api.resume_run

    def _chatty(run_id):
        print(
            "Consider using the pymupdf_layout package for a greatly improved page layout analysis."
        )
        return real(run_id)

    monkeypatch.setattr("openreading.cli.app.api.resume_run", _chatty)
    from openreading.cli.app import build_parser, cmd_resume

    rc = cmd_resume(build_parser().parse_args(["resume", armed[0]]))
    cap = capsys.readouterr()

    assert rc == 0
    assert json.loads(cap.out)["status"]["state"] == "succeeded"  # stdout is one JSON document
    assert "pymupdf_layout" in cap.err  # the advisory went somewhere, and that somewhere is stderr


def test_cmd_resume_reports_payload_expired_when_the_original_documents_blob_was_shredded(
    tmp_path, monkeypatch, capsys
):
    """Finding 10(b): `cmd_resume`'s own `except PayloadExpired` clause exists specifically for
    `_request_from_header`'s un-wrapped `blobs.get(header.document)` call (`api.py`'s own comment:
    "left to propagate uncaught... the whole-run analog of AC-10's per-step signal") — but no test
    ever constructed a bytes_base64-sourced run, shredded ITS OWN key (not a step's), and called
    `api.resume_run`/`cmd_resume` before `run_strategy` is ever reached. This is that test."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENREADING_LEDGER", str(tmp_path / "ledger"))
    ledger_root = tmp_path / "ledger"
    pdf_path = tmp_path / "doc.pdf"
    pdf_path.write_bytes(build_sample_pdf())
    (tmp_path / "openreading.yaml").write_text(
        "version: 1\nstrategies:\n  s:\n    steps:\n      - backend: pymupdf\n"
    )
    armed: list[str] = []
    api.run(str(pdf_path), strategy="s", on_run_armed=armed.append)
    run_id = armed[0]

    keys = LocalFsKeyStore(ledger_root / "keys")
    keys.destroy(run_id)  # shreds the ORIGINAL document's own blob key, not a step payload's

    from openreading.cli.app import build_parser

    args = build_parser().parse_args(["resume", run_id])
    rc = args.func(args)
    assert rc == 3
    err = capsys.readouterr().err
    assert "[resume]" in err and "key destroyed" in err


# ---- pinned_eligible armed on resume + a live compliance-gate refusal (AC-14) --------------------


def test_pinned_eligible_is_armed_on_resume_and_the_gate_refuses_a_changed_descriptor(
    tmp_path, monkeypatch
):
    """Round-1 F6: the one production line item that gives AC-14 teeth — `pinned_eligible=` must
    actually be passed to the resume-mode `InlineExecutor` (sourced from the header), not defaulted
    to empty. A live refusal must RAISE (fail the run), never degrade to an ordinary fail-over-
    eligible skip (round-3 F10)."""
    monkeypatch.setenv("OPENREADING_LEDGER", str(tmp_path / "ledger"))
    run_id = str(uuid.uuid4())
    req = _req()
    clock = RealClock()

    v1_reg = scripted_registry(ScriptedBackend("x", local=True, text="hi"))
    fresh = _arm_ledger(
        run_id,
        req,
        v1_reg,
        EnvCredentialBroker(),
        clock,
        ["x"],
        config_hash="fixed-hash",
        plan_tree={"steps": [{"backend": "x"}]},
        strategy_name="s",
    )
    assert fresh is not None

    # A "live" registry whose "x" descriptor has genuinely changed since the original run pinned
    # it (a different cost profile) — config_hash/plan_hash are held fixed here (matching the
    # header) so this test isolates the gate mechanism itself, per F6's own narrow finding, rather
    # than the header-comparison path AC-4's own test already covers.
    v2_reg = scripted_registry(ScriptedBackend("x", local=True, text="hi", cost_low=99.0))
    resumed_executor = _arm_ledger(
        run_id,
        req,
        v2_reg,
        EnvCredentialBroker(),
        clock,
        ["x"],
        config_hash="fixed-hash",
        plan_tree={"steps": [{"backend": "x"}]},
        strategy_name="s",
        resume=True,
    )
    assert isinstance(resumed_executor, InlineExecutor)
    assert resumed_executor._pinned_eligible, "pinned_eligible must be armed on resume, not empty"

    step_req = StepRequest(
        step_id="s1",
        run_id=run_id,
        kind="submit",
        step_path="root.steps[0]",
        step_seq=0,
        attempt=1,
        backend_id="x",
    )
    with pytest.raises(ComplianceRefused):
        asyncio.run(
            resumed_executor.exec(
                step_req, run=lambda: (_ for _ in ()).throw(AssertionError("must not dispatch"))
            )
        )


# ---- missing-credentials journaled and terminal on resume (AC-15) -------------------------------


def test_missing_credentials_is_journaled_and_stays_terminal_on_resume_even_once_creds_appear(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OPENREADING_LEDGER", str(tmp_path / "ledger"))
    monkeypatch.delenv("FAKE_LEDGER_T3_KEY", raising=False)
    ledger_root = tmp_path / "ledger"
    reg = scripted_registry(
        ScriptedBackend("needs-key", local=False, required_env=["FAKE_LEDGER_T3_KEY"])
    )
    cfg = _cfg([{"backend": "needs-key"}])
    req = _req()

    run_id = str(uuid.uuid4())
    with pytest.raises(PlanExhaustedError):
        _run_once(ledger_root, reg, cfg, req, run_id=run_id)

    journal_file = ledger_root / f"{run_id}.jsonl"
    parsed = [json.loads(line) for line in journal_file.read_text().splitlines() if line.strip()]
    assert [p["status"] for p in parsed] == ["skipped"]
    assert parsed[0]["error"]["code"] == "missing_credentials"

    raising_reg = scripted_registry(
        ScriptedBackend(
            "needs-key",
            local=False,
            required_env=["FAKE_LEDGER_T3_KEY"],
            error=AssertionError("must not dispatch"),
        )
    )
    # resume with credentials STILL absent — replays the skip, no dispatch
    with pytest.raises(PlanExhaustedError):
        _resume(ledger_root, run_id, raising_reg, cfg, req)

    # resume with credentials NOW present — still replays the skip, does not attempt for real
    # (the exact scenario the pre-T3 design could not have passed: no journal record existed then)
    monkeypatch.setenv("FAKE_LEDGER_T3_KEY", "now-set")
    with pytest.raises(PlanExhaustedError):
        _resume(ledger_root, run_id, raising_reg, cfg, req)


def test_resume_replays_both_records_of_a_two_rung_cascade_whose_early_rung_was_skipped(
    tmp_path, monkeypatch
):
    """Finding 1/Finding 8 (Phase C round-1, two reviewers independently): the ONLY existing
    missing-credentials test above uses a SINGLE-rung cascade — `continue` and the (now removed)
    buggy `return Outcome.err(...)` are behaviorally indistinguishable there (both end in
    `PlanExhaustedError`), so it could not catch a regression in `_eval_cascade`'s own skip-check.
    This test uses the exact shape both reviewers' repros used: rung 0 needs a credential the
    operator doesn't have (skips, live), rung 1 is a local backend that always succeeds — both
    rungs are independently terminal in the journal. Resuming, with credentials STILL absent, must
    replay BOTH records and reproduce the SAME successful result the original run got, not a hard
    `PlanExhaustedError` at rung 0 (this test fails against the pre-fix code and passes after it)."""
    monkeypatch.setenv("OPENREADING_LEDGER", str(tmp_path / "ledger"))
    monkeypatch.delenv("FAKE_LEDGER_T3_KEY", raising=False)
    ledger_root = tmp_path / "ledger"
    reg = scripted_registry(
        ScriptedBackend("needs-key", local=False, required_env=["FAKE_LEDGER_T3_KEY"]),
        ScriptedBackend("fallback", local=True, text="fallback succeeded"),
    )
    cfg = _cfg([{"backend": "needs-key"}, {"backend": "fallback"}])
    req = _req()

    run_id, _compiled, result = _run_once(ledger_root, reg, cfg, req)
    assert result.response.document.text == "fallback succeeded"

    journal_file = ledger_root / f"{run_id}.jsonl"
    parsed = [json.loads(line) for line in journal_file.read_text().splitlines() if line.strip()]
    terminal_statuses = [p["status"] for p in parsed if p["status"] != "attempted"]
    assert terminal_statuses == ["skipped", "ok"], "both rungs must be independently terminal"

    # resume with credentials STILL absent, against a registry whose every adapter raises on
    # dispatch — a zero-network proof, exactly like the other AC-3 tests above.
    raising_reg = scripted_registry(
        ScriptedBackend(
            "needs-key",
            local=False,
            required_env=["FAKE_LEDGER_T3_KEY"],
            error=AssertionError("must not dispatch"),
        ),
        ScriptedBackend("fallback", local=True, error=AssertionError("must not dispatch")),
    )
    resumed = _resume(ledger_root, run_id, raising_reg, cfg, req)
    assert resumed.response.document.text == "fallback succeeded"


# ---- shred-then-resume-reports-expired (AC-10) ---------------------------------------------------


def test_shred_then_resume_reports_payload_expired(tmp_path, monkeypatch):
    """T1's own shred mechanism destroys the run's key; the journal record survives (§9.4). A
    resumed step whose recorded payload was spilled to the (now-unreadable) blob store must report
    `payload_expired` BY NAME — rather than either succeed with stale-looking data or crash the
    walk outright. `InlineExecutor._resolve_replay_payload` converts the raw `PayloadExpired` into
    a `TerminalError(backend_code="payload_expired")`, so the walk degrades through the SAME
    already-handled taxonomy a live permanent failure takes (never an uncaught crash) while still
    naming the real reason in the resulting `PlanExhaustedError`'s own trail."""
    monkeypatch.setenv("OPENREADING_LEDGER", str(tmp_path / "ledger"))
    ledger_root = tmp_path / "ledger"
    reg = scripted_registry(ScriptedBackend("solo", local=True, text="hello"))
    cfg = _cfg([{"backend": "solo"}])
    req = _req()

    run_id, _compiled, _result = _run_once(ledger_root, reg, cfg, req)

    keys = LocalFsKeyStore(ledger_root / "keys")
    keys.destroy(run_id)  # T1's own shred mechanism — the journal record survives, content doesn't

    raising_reg = scripted_registry(
        ScriptedBackend("solo", local=True, error=AssertionError("must not dispatch"))
    )
    with pytest.raises(PlanExhaustedError) as exc_info:
        _resume(ledger_root, run_id, raising_reg, cfg, req)
    assert any("payload_expired" in a.get("detail", "") for a in exc_info.value.trail)


# ---- ZDR + replay no longer crashes (Finding 2) --------------------------------------------------


def test_resume_reports_a_typed_error_for_a_zdr_flagged_backends_replayed_ok_step(
    tmp_path, monkeypatch
):
    """Finding 2 (Phase C round-1, two reviewers independently): a ZDR-flagged backend's live "ok"
    dispatch never calls `blobs.put` (§9.4 — zero retained content), so the journaled record's own
    `payload` is `None` by design. Before this fix, replaying that record reached
    `engine._response_payload` unchanged and crashed with an uncaught `pydantic.ValidationError`
    trying `NormalizedResponse.model_validate(None)` — 100% of the time, for any resume that
    touched a ZDR backend. This proves the fix: the resumed walk degrades to a clean, named
    `TerminalError(backend_code="zdr_payload_not_retained")` through the SAME `PlanExhaustedError`
    taxonomy AC-10's own `payload_expired` case already uses, and never crashes."""
    monkeypatch.setenv("OPENREADING_LEDGER", str(tmp_path / "ledger"))
    ledger_root = tmp_path / "ledger"

    def _zdr_backend(**kwargs: Any) -> ScriptedBackend:
        # `local=False` (hosted): §9.4's retention math only applies ZDR to the HOSTED path
        # (`InlineExecutor._is_zdr_backend` skips `runs_fully_local` descriptors before ever
        # consulting `zdr_flag`, mirroring `compute_retention_ceiling_hours`'s own guard — a fully
        # local backend already retains nothing at any vendor by construction) — a `local=True`
        # backend's own step payload can never be suppressed this way, regardless of `zdr_flag`.
        # Both the original and the resumed registry build this THE SAME way — an inconsistent
        # descriptor between them would (correctly) refuse via `config_hash`, AC-4's own territory,
        # not what this test exists to prove.
        b = ScriptedBackend("zdr-backend", local=False, **kwargs)
        b.descriptor = b.descriptor.model_copy(
            update={
                "compliance": b.descriptor.compliance.model_copy(
                    update={"zdr_flag": "vendor-attested"}
                )
            }
        )
        return b

    reg = scripted_registry(_zdr_backend(text="zdr succeeded"))
    cfg = _cfg([{"backend": "zdr-backend"}])
    req = _req()

    run_id, _compiled, result = _run_once(ledger_root, reg, cfg, req)
    assert result.response.document.text == "zdr succeeded"

    journal_file = ledger_root / f"{run_id}.jsonl"
    parsed = [json.loads(line) for line in journal_file.read_text().splitlines() if line.strip()]
    ok_records = [p for p in parsed if p["status"] == "ok"]
    assert ok_records and ok_records[0].get("payload") is None, "ZDR must retain zero content"

    # Non-regression (Phase C round-2, item 2's own safety property): a REAL dispatch to this
    # ZDR-flagged backend must still be reflected in the run's own retention stamp — the fix that
    # stops an eligible-but-undispatched backend from tainting the stamp (below) must not also stop
    # a genuinely-dispatched one from doing so.
    from openreading.ledger.retention import stamp_path

    stamp = json.loads(stamp_path(ledger_root, run_id).read_text())
    assert stamp["zdr"], "a backend that actually dispatched IS ZDR-flagged — the stamp must say so"

    raising_reg = scripted_registry(_zdr_backend(error=AssertionError("must not dispatch")))
    with pytest.raises(PlanExhaustedError) as exc_info:
        _resume(ledger_root, run_id, raising_reg, cfg, req)
    assert any("zdr_payload_not_retained" in a.get("detail", "") for a in exc_info.value.trail)


# ---- ZDR/retention scoped to the run's ACTUAL PATH, not the whole eligible set (Finding 8/6) -----


def test_resume_retains_and_replays_a_local_only_run_even_when_an_undispatched_zdr_backend_is_eligible(
    tmp_path, monkeypatch
):
    """Phase C round-2 (Findings 8 and 6) — the exact repro shape both reviewers
    used against the REAL production registry (`reducto`, `zdr_flag` + `max_retention_hours=0`,
    eligible for essentially any plain PDF whether or not it's ever named or credentialed),
    reproduced here against a small fake registry so it runs offline. A strategy naming ONLY a
    local, non-ZDR backend, against a registry that ALSO contains an eligible-but-UNDISPATCHED
    hosted ZDR/zero-retention backend.

    Before this fix, `_arm_ledger` computed `zdr`/the retention ceiling from
    `compute_retention_ceiling_hours(descriptors, ...)` over `compiled.eligible` — the registry-WIDE
    compliance/capability survivor list, which includes BOTH backends here even though the strategy
    names only `local-solo` — so the local step's own successful payload was never written to blob
    storage (whole-run `zdr=True`) and the ceiling collapsed to ~0 hours (`min()` included the
    undispatched backend's own zero). Both effects made this ordinary, already-successful run's
    resume fail unconditionally, for a document that never touched ZDR content at all."""
    monkeypatch.setenv("OPENREADING_LEDGER", str(tmp_path / "ledger"))
    ledger_root = tmp_path / "ledger"

    def _zdr_zero_retention_backend(**kwargs: Any) -> ScriptedBackend:
        b = ScriptedBackend("undispatched-zdr", local=False, **kwargs)
        b.descriptor = b.descriptor.model_copy(
            update={
                "compliance": b.descriptor.compliance.model_copy(
                    update={"zdr_flag": "vendor-attested", "max_retention_hours": 0}
                )
            }
        )
        return b

    local_backend = ScriptedBackend("local-solo", local=True, text="local succeeded")
    reg = scripted_registry(local_backend, _zdr_zero_retention_backend())
    cfg = _cfg([{"backend": "local-solo"}])  # the strategy names ONLY the local backend
    req = _req()

    before_ms = int(RealClock().now_wall_ms())  # the base the stamp uses (see ledger.retention)
    run_id, compiled, result = _run_once(ledger_root, reg, cfg, req)
    assert result.response.document.text == "local succeeded"
    # both backends really are eligible — the shape both reviews' repros used, not a narrower one.
    assert set(compiled.eligible) == {"local-solo", "undispatched-zdr"}

    journal_file = ledger_root / f"{run_id}.jsonl"
    parsed = [json.loads(line) for line in journal_file.read_text().splitlines() if line.strip()]
    ok_records = [p for p in parsed if p["status"] == "ok"]
    assert ok_records and ok_records[0].get("payload") is not None, (
        "the DISPATCHED local backend's own content must be retained — an eligible-but-undispatched "
        "ZDR backend elsewhere in the registry must have zero effect on it"
    )

    from openreading.ledger.retention import stamp_path

    stamp = json.loads(stamp_path(ledger_root, run_id).read_text())
    assert not stamp["zdr"], "an eligible-but-undispatched ZDR backend must not flag the whole run"
    generous_floor_ms = 3600_000  # well under the 24h default — proves the ceiling wasn't collapsed
    assert stamp["expires_epoch_ms"] - before_ms > generous_floor_ms

    raising_reg = scripted_registry(
        ScriptedBackend("local-solo", local=True, error=AssertionError("must not dispatch")),
        _zdr_zero_retention_backend(error=AssertionError("must not dispatch")),
    )
    resumed = _resume(ledger_root, run_id, raising_reg, cfg, req)
    assert resumed.response.document.text == "local succeeded"


def test_a_dispatched_hosted_backends_own_retention_limit_tightens_the_runs_ceiling(
    tmp_path, monkeypatch
):
    """Non-regression on the compliance guarantee item 2's fix must not weaken (Phase C round-2):
    when a step genuinely DOES dispatch to a hosted backend with a strict retention limit, the run's
    overall ceiling must still collapse to respect it. `_arm_ledger`'s new arm-time stamp (the
    operator default alone) is only the un-narrowed STARTING point; `InlineExecutor.exec`'s live
    "ok" branch tightens it right back down, via `retention.tighten_retention`, the moment that
    backend actually dispatches. The companion test above proves the opposite direction: a backend
    that stays merely eligible must never do this."""
    monkeypatch.setenv("OPENREADING_LEDGER", str(tmp_path / "ledger"))
    ledger_root = tmp_path / "ledger"

    def _short_retention_backend(**kwargs: Any) -> ScriptedBackend:
        b = ScriptedBackend("short-hosted", local=False, **kwargs)
        b.descriptor = b.descriptor.model_copy(
            update={
                "compliance": b.descriptor.compliance.model_copy(update={"max_retention_hours": 1})
            }
        )
        return b

    reg = scripted_registry(_short_retention_backend(text="short-lived"))
    cfg = _cfg([{"backend": "short-hosted"}])
    req = _req()

    before_ms = int(RealClock().now_wall_ms())  # the base the stamp uses (see ledger.retention)
    run_id, _compiled, result = _run_once(ledger_root, reg, cfg, req)
    assert result.response.document.text == "short-lived"

    from openreading.ledger.retention import stamp_path

    stamp = json.loads(stamp_path(ledger_root, run_id).read_text())
    # tightened from the (much longer) operator default down to ~1h — this backend's own declared
    # limit, not the un-narrowed arm-time default (`DEFAULT_RETENTION_HOURS` == 24).
    assert stamp["expires_epoch_ms"] - before_ms <= 2 * 3600_000
    # ...and tightened on the SAME clock base it was stamped with. `tighten_retention` takes a
    # `min`, so a monotonic `now` here would beat the wall-clock stamp outright and collapse the
    # ceiling into 1970 — a hosted dispatch shredding its own run. The upper bound above cannot
    # see that (a far-too-small stamp satisfies it), so assert the floor too.
    assert stamp["expires_epoch_ms"] > before_ms


# ---- AC-12, scoped to open_ocr/aws_textract (§4.6) -----------------------------------------------


def test_ac12_a_crash_retried_open_ocr_step_reconciles_by_idempotency_key_not_paid_twice(
    tmp_path, monkeypatch
):
    """AC-12's own verification method (§4.6) — a step whose only journal record is `attempted`
    (the realistic shape a `kill -9` between the vendor call and the terminal write leaves) is
    retried for real on resume. Simulates the crash by directly inserting the `attempted` record a
    real dispatch would have left (rather than literally killing an OS process, which would add
    fragile, platform-dependent timing to prove the identical journal state) — `DedupingVendor`
    then proves the RESUMED retry reuses the SAME `idempotency_key` the crashed attempt used, the
    mechanism `open_ocr`'s own module docstring names as what "keeps a network retry from double-
    charging this backend's own metered, billed-per-page cost."

    The VENDOR-facing key is `RunContext.idempotency_key` (`build_run_context`'s own
    `_default_idempotency_key`, keyed off `document_identity` + `content_key`) — a DIFFERENT
    computation from the LEDGER's own `StepRequest.idempotency_key` (`_step_request`'s own,
    `document_digest`-based one): internal/design/ledger.md §5.5's "three identities, deliberately
    distinct" means the journal's own idempotency_key field is audit metadata, not necessarily the
    literal bytes the adapter sends to the vendor — this test uses each for its own real purpose."""
    from openreading.adapters.open_ocr.adapter import OpenOCRAdapter
    from openreading.credentials import build_run_context
    from openreading.router.registry import Registry

    monkeypatch.setenv("OPENOCR_API_KEY", "sk-ocr-test")  # readiness only — the client is injected
    ledger_root = tmp_path / "ledger"
    monkeypatch.setenv("OPENREADING_LEDGER", str(ledger_root))

    class DedupingVendor:
        """A real open-ocr.com-style vendor recognizes a repeated Idempotency-Key and never bills
        twice for it — this is what AC-12 depends on; openreading's own job is only to SEND the
        same key twice, which is what this test actually verifies."""

        def __init__(self) -> None:
            self.charges = 0
            self._by_key: dict[str | None, dict] = {}

        def create_ocr(self, body, idempotency_key):
            if idempotency_key in self._by_key:
                return self._by_key[idempotency_key]
            self.charges += 1
            result = {
                "status": "succeeded",
                "request_id": f"req-{self.charges}",
                "extracted_text": "hi",
            }
            self._by_key[idempotency_key] = result
            return result

        def get_request(self, request_id):
            raise AssertionError("sync mode never polls")

    vendor = DedupingVendor()
    adapter = OpenOCRAdapter(client=vendor)
    reg = Registry()
    reg.register(adapter)

    cfg = _cfg([{"backend": "open-ocr"}])
    req = _req()
    run_id = str(uuid.uuid4())
    clock = RealClock()
    compiled = compile_strategy(req, "s", cfg, reg, RouterConfig())

    # "first arm" — establishes the header, matching what happened before the simulated crash.
    _arm_ledger(
        run_id,
        req,
        reg,
        EnvCredentialBroker(),
        clock,
        compiled.eligible,
        config_hash=compiled.config_hash,
        plan_tree=compiled.root,
        strategy_name="s",
    )

    # The crashed attempt's own real vendor call already happened (one genuine charge) but the
    # process died before the terminal write landed — an "attempted"-only record, AC-12/AC-13's
    # own territory. Uses the SAME idempotency_key a real dispatch computes (_step_request is the
    # one function that derives it) so the resumed retry's own key genuinely matches.
    step_req = _step_request(
        SimpleNamespace(run_id=run_id), "root.steps[0]", "open-ocr", adapter.descriptor, req
    )
    journal = JsonlJournal(ledger_root / f"{run_id}.jsonl")
    journal.append(
        StepResult(
            step_id=_step_id(run_id, "root.steps[0]", 0),
            run_id=run_id,
            step_path="root.steps[0]",
            step_seq=0,
            backend_id="open-ocr",
            status="attempted",
            attempt=1,
            idempotency_key=step_req.idempotency_key,
            content_key=step_req.content_key,
        )
    )
    vendor_key = build_run_context(
        req, adapter.descriptor, broker=EnvCredentialBroker()
    ).idempotency_key
    vendor.create_ocr({"engine": "openocr/tesseract"}, vendor_key)
    assert vendor.charges == 1  # the pre-crash attempt's own real charge

    resumed_executor = _arm_ledger(
        run_id,
        req,
        reg,
        EnvCredentialBroker(),
        clock,
        compiled.eligible,
        config_hash=compiled.config_hash,
        plan_tree=compiled.root,
        strategy_name="s",
        resume=True,
    )
    result = run_strategy(
        compiled,
        req,
        registry=reg,
        broker=EnvCredentialBroker(),
        clock=clock,
        run_id=run_id,
        executor=resumed_executor,
    )
    assert result.response.status.state.value == "succeeded"
    assert vendor.charges == 1, "the resumed retry reused the SAME idempotency key — no new charge"


# ---- Exit code 6 reachable (§4.4) -----------------------------------------------------------------


def test_keyboard_interrupt_during_cmd_parse_prints_resumable_message_and_exits_6(
    tmp_path, monkeypatch, capsys
):
    """F3(b): the minimal live trigger for exit code 6. Ctrl-C is simulated by making the walk
    itself raise `KeyboardInterrupt` AFTER the ledger has genuinely armed (patching
    `openreading.strategies.run_strategy` — the name `_run_strategy_request`'s own local `from
    openreading.strategies import run_strategy` resolves at call time — rather than
    `api.run_request`, which would bypass `_arm_ledger`/`on_run_armed` entirely and never populate
    the CLI's own `armed_run_id`)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENREADING_LEDGER", str(tmp_path / "ledger"))
    pdf_path = tmp_path / "doc.pdf"
    pdf_path.write_bytes(build_sample_pdf())
    (tmp_path / "openreading.yaml").write_text(
        "version: 1\nstrategies:\n  s:\n    steps:\n      - backend: pymupdf\n"
    )

    import openreading.strategies as strategies_pkg

    def _boom(*_a, **_kw):
        raise KeyboardInterrupt()

    monkeypatch.setattr(strategies_pkg, "run_strategy", _boom)

    from openreading.cli.app import build_parser

    args = build_parser().parse_args(["parse", str(pdf_path), "--strategy", "s"])
    rc = args.func(args)
    assert rc == 6
    err = capsys.readouterr().err
    assert "interrupted" in err and "resumable" in err
    assert "openreading resume" in err
