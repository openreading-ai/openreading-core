"""`InlineExecutor` — T1's one core `Executor`: runs every step in this process (today's
behaviour, preserved exactly when unarmed, L1) and journals an `attempted` record strictly before
dispatch and a terminal record strictly after (internal/design/ledger.md §11, plan §6, resolving Phase
A round-1 F1). `NullJournal` is the no-op `Journal` L1 wires in when `OPENREADING_LEDGER` is unset
— every `append` is dropped and `get` returns nothing, so an unarmed run touches no disk (AC-1).

T1 does not implement §5.3's full `_exec_leaf` submit/drive/emit decomposition — `exec`'s `run`
callable is the caller's already-built closure over `_execute_leaf_sync` (submit + drive-to-
completion + normalize + cost-report as one synchronous call). Exceptions from `run()` are
journaled as a `failed` terminal record and then re-raised unchanged, so every existing call
site's own `except (TerminalError, RetryableError, UnsupportedFeatureError, ComplianceRefused)`
handling keeps working without modification — T1 wraps the existing call in a step boundary, it
does not rewrite how the walk consumes results.

Ledger T3 (internal/eng-council/plans/sprint26-T3-plan.md §4.0/§4.3/§4.3a/§4.3b) adds the replay
consumer T1 deliberately left absent: `exec` now returns an `ExecResult` (never the bare `run()`
payload) and, before ever calling `run()`, checks the journal for a terminal record already at this
step's key — a resumed process serves that recorded outcome back instead of re-dispatching. Two
gates run before a fresh dispatch (`_gate`'s pinned-eligible check and the new missing-credentials
check); a `CancelledError` out of `run()` — the shape a `parallel:` node's `on_win: cancel` produces
for a losing branch — now gets its own terminal record too, so a resumed process can tell "this
step lost a race" from "this process crashed mid-dispatch" (AC-12/AC-13's territory) instead of
re-attempting a real dispatch for a step that was already decided.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from openreading.ledger.descriptor import ExecutorCapabilities, ExecutorDescriptor, ExecutorLimits
from openreading.ledger.ports import BlobStore, Journal, PayloadExpired
from openreading.ledger.retention import tighten_retention
from openreading.ledger.sanitizer import Sanitizer
from openreading.ledger.step import BlobRef, ExecResult, StepError, StepRef, StepRequest, StepResult
from openreading.router.clock import Clock
from openreading.types.errors import (
    AdapterError,
    ComplianceRefused,
    RetryableError,
    TerminalError,
    UnsupportedFeatureError,
)

# Mirrors router/executor.py's own _TAXONOMY tuple (not imported from there to avoid a router ->
# ledger -> router layering loop) — the four base exception types core's error taxonomy is built
# from. Walked by isinstance so a private subtype reports as its base, exactly like that module's
# own use of the same shape (design doc §11, "Retry policy by taxonomy"). Typed as `AdapterError`
# (their common base, `backend_code`-accepting constructor) rather than the bare `Exception` every
# member ultimately is, so `_reconstruct_error`'s own `cls(message, backend_code=...)` call
# type-checks.
_TAXONOMY: tuple[type[AdapterError], ...] = (
    TerminalError,
    RetryableError,
    UnsupportedFeatureError,
    ComplianceRefused,
)
_TAXONOMY_BY_NAME: dict[str, type[AdapterError]] = {cls.__name__: cls for cls in _TAXONOMY}

# A terminal outcome — anything a replay can serve back without re-dispatching. "attempted" is a
# record of INTENT, not an outcome (plan §4.3): a step whose only record is "attempted" is
# indistinguishable from one that never dispatched, and correctly falls through to a real re-run
# (AC-12/AC-13's crash-recovery territory).
_TERMINAL_STATUSES = frozenset({"ok", "skipped", "failed", "cancelled"})


def _classify_taxonomy(exc: Exception) -> str:
    for cls in _TAXONOMY:
        if isinstance(exc, cls):
            return cls.__name__
    return "error"


def _reconstruct_error(err: StepError) -> Exception:
    """The inverse of `_classify_taxonomy` (and of `_gate`'s own live raise, below): rebuilds a
    real exception from a recorded `StepError` so a replayed `"failed"` record re-raises the same
    way a live failure would (plan §4.0: "reconstructs and re-raises the recorded error the same
    way, uniformly") — every real call site's own `except (TerminalError, RetryableError,
    UnsupportedFeatureError, ComplianceRefused)` handling then applies unmodified, live or
    replayed. An unrecognized taxonomy (defensive — `_classify_taxonomy`'s own "error" fallback for
    an exception outside the four-type taxonomy is not a real class name) reconstructs as a
    `TerminalError`, matching how the engine's own broad `except Exception` already treats that
    case identically to a `TerminalError` today (`classify_error`'s fallthrough to
    "provider_error")."""
    message = err.detail or err.code
    if err.taxonomy == "ComplianceRefused":
        return ComplianceRefused(message, constraint=err.code)
    cls = _TAXONOMY_BY_NAME.get(err.taxonomy, TerminalError)
    return cls(message, backend_code=err.code)


class NullJournal:
    """L1's no-op `Journal` (AC-1): every write drops, every read returns nothing. `append`
    returns its argument unchanged (Ledger T3 §4.0) — `journal_seq` stays `None`, consistent with
    L1's zero-delta guarantee: an unarmed run's `ExecResult.journal_seq` is always `None` too."""

    def append(self, result: StepResult) -> StepResult:
        return result

    def get(self, ref: Any) -> list[StepResult]:
        return []


def descriptor_digest(descriptor: Any) -> str:
    """sha256 of a descriptor's schema dict — the same computation `strategies/prune.py`'s
    `config_hash` already makes per eligible backend (BL-163), reused here for the L2 gate."""
    return hashlib.sha256(
        json.dumps(descriptor.to_schema_dict(), sort_keys=True, default=str).encode()
    ).hexdigest()


class InlineExecutor:
    """`registry`/`clock` back the L2 gate and the journal's timestamps; `pinned_eligible`, when
    given, maps `backend_id -> descriptor_digest` snapshotted at plan-compile time (mirroring
    `config_hash`'s own per-backend digests). The gate is inert in T1 (inline execution can never
    diverge from its own plan mid-run — there is no second worker to disagree with the pinned set)
    but the mechanism and its test exist, per AC-14's T1 row.

    ZDR (Phase C round-1, High; rescoped Phase C round-2, Findings 8 and 6):
    `internal/design/ledger.md` §9.4 requires zero retained CONTENT for a ZDR-flagged backend's own
    step — `exec` still writes the ordinary `attempted`/terminal journal records (topology, digests,
    costs — audit metadata, not content) but never calls `blobs.put(...)` for that step. Originally
    a single whole-run boolean (`zdr=`, precomputed from the run's registry-wide eligible set at arm
    time) suppressed the blob write for EVERY step of the run — so an unrelated, never-dispatched
    ZDR-capable backend merely being eligible for the request's document type silently suppressed a
    completely unrelated backend's own successful payload too. `_is_zdr_backend` (below) replaces
    that with a per-step lookup, straight off the backend `req.backend_id` actually names — a step's
    payload is suppressed iff the backend that ACTUALLY produced it is ZDR-flagged, never because
    some other, merely-eligible backend elsewhere in the registry happens to be. `ledger_root`
    (also new this round) lets a live "ok" dispatch tighten the run's own stamped retention ceiling
    (`retention.tighten_retention`) from that backend's own declared limit — the ceiling's
    equivalent fix, since the same registry-wide-eligible-set conflation collapsed it too (see
    `ledger/retention.py`'s module docstring)."""

    def __init__(
        self,
        *,
        journal: Journal,
        blobs: BlobStore | None,
        registry: Any,
        clock: Clock,
        pinned_eligible: dict[str, str] | None = None,
        sanitizer: Sanitizer | None = None,
        ledger_root: Path | None = None,
    ) -> None:
        self._journal = journal
        self._blobs = blobs
        self._registry = registry
        self._clock = clock
        self._pinned_eligible = pinned_eligible or {}
        self._sanitizer = sanitizer or Sanitizer()
        self._ledger_root = ledger_root

    def _is_zdr_backend(self, backend_id: str | None) -> bool:
        """Phase C round-2 (Findings 8 and 6, "Narrowest fix"): whether the backend
        that ACTUALLY dispatched this step (not the run's whole eligible set) is itself ZDR-flagged.
        `backend_id is None` (a composite step) or an unregistered/unresolvable id are both treated
        as "not ZDR" — the same as before this fix, since neither ever populated `descriptors` under
        the old whole-run computation either. Mirrors `retention.compute_retention_ceiling_hours`'s
        own guard exactly (`runs_fully_local` skipped before `zdr_flag` is even consulted) — AC-11's
        own text is "a HOSTED backend flagged zero-data-retention"; a local backend already retains
        nothing at any vendor by construction, so its own `zdr_flag` (if ever set) adds nothing."""
        if backend_id is None or self._registry is None:
            return False
        adapter = self._registry.get(backend_id)
        if adapter is None:
            return False
        profile = getattr(adapter.descriptor, "compliance", None)
        if profile is None or profile.runs_fully_local:
            return False
        return profile.zdr_flag is not None

    @property
    def descriptor(self) -> ExecutorDescriptor:
        return ExecutorDescriptor(
            id="inline",
            limits=ExecutorLimits(),  # every limit None == unbounded (§5.4)
            capabilities=ExecutorCapabilities(native_timers=True),
        )

    def _gate(self, req: StepRequest) -> StepResult | None:
        """L2 (§5.3): every executor enforces this at `exec`, before any I/O. A refusal writes a
        single `failed` terminal record and no `attempted` write at all — a refused step was never
        going to dispatch (plan §6, resolving round-2 F2's ordering clarification)."""
        if not self._pinned_eligible or req.backend_id is None:
            return None
        pinned_digest = self._pinned_eligible.get(req.backend_id)
        code: str | None
        if pinned_digest is None:
            code = "not_in_pinned_set"
        else:
            adapter = self._registry.get(req.backend_id)
            live_digest = descriptor_digest(adapter.descriptor) if adapter is not None else None
            code = None if live_digest == pinned_digest else "descriptor_digest_mismatch"
        if code is None:
            return None
        return StepResult(
            step_id=req.step_id,
            run_id=req.run_id,
            step_path=req.step_path,
            step_seq=req.step_seq,
            backend_id=req.backend_id,
            status="failed",
            attempt=req.attempt,
            idempotency_key=req.idempotency_key,
            content_key=req.content_key,
            error=StepError(code=code, taxonomy="ComplianceRefused"),
            ended_epoch_ms=int(self._clock.now_wall_ms()),
        )

    def _missing_credentials_gate(self, req: StepRequest) -> StepResult | None:
        """Ledger T3 §4.3b: a second gate, parallel to `_gate`, driven by information the caller
        (`_run_branch`/`_run_leaf` — the only holders of `ctx.broker`) already computed via
        `readiness.missing_required` and threads through `req.missing_credentials`. Makes the
        missing-credentials skip a real, journaled outcome instead of staying orchestration-only —
        design doc §9.2's own "a worker returns a typed StepResult(status='skipped',
        code='missing_credentials')", not yet true of the pre-T3 code."""
        if not req.missing_credentials:
            return None
        return StepResult(
            step_id=req.step_id,
            run_id=req.run_id,
            step_path=req.step_path,
            step_seq=req.step_seq,
            backend_id=req.backend_id,
            status="skipped",
            attempt=req.attempt,
            idempotency_key=req.idempotency_key,
            content_key=req.content_key,
            error=StepError(
                code="missing_credentials",
                taxonomy="missing_credentials",
                detail=",".join(req.missing_credentials),
            ),
            ended_epoch_ms=int(self._clock.now_wall_ms()),
        )

    def _resolve_replay_payload(self, result: StepResult) -> Any:
        """A replayed non-`"failed"` terminal record's `payload`, resolved through the blob store
        when the live dispatch spilled it there (plan §4.0: "resolving a BlobRef payload via
        self._blobs.get when armed"). Returns the plain JSON value `BlobStore.put`'s own caller
        wrote (`InlineExecutor` never imports a concrete response type — deliberately
        response-shape-agnostic, per this module's own docstring; a caller that needs a typed
        object reconstructs it from this JSON itself, exactly as it would from any other
        `ctx.exec` payload).

        `BlobStore.get` raises `PayloadExpired` once the run's key has been shredded (AC-10). Every
        real call site (`_run_branch`/`_run_leaf`/`_eval_paged_cascade`) already carries `except
        (TerminalError, RetryableError, UnsupportedFeatureError, ComplianceRefused) as e:` around
        `ctx.exec` — letting the bare `PayloadExpired` (not a member of that taxonomy) propagate
        would fall through to the engine's OWN broad `except Exception`, which reclassifies it as
        an ordinary `provider_error` with no trace of "payload_expired" anywhere a caller can name
        it, exactly the "reports expired... rather than crashing" failure mode AC-10 exists to
        rule out. Re-raised as a `TerminalError` instead — `backend_code="payload_expired"`
        preserved verbatim in the exception (and, via `str(exc)`, in the resulting Attempt's own
        `detail`) — so the walk degrades through the SAME graceful, already-handled path a live
        permanent failure takes, naming the real reason rather than crashing OR papering over it.

        A ZDR-flagged backend's own "ok" record is the other reason `payload` can legitimately be
        absent (Phase C round-1, two reviewers independently): §9.4's "zero retained content" means
        `exec`'s own dispatch branch below never calls `blobs.put` for a ZDR run, so the journaled
        `"ok"` record's `payload` is `None` by design, not a missing write. Before this fix, that
        `None` reached `engine.py`'s `_response_payload` unchanged and crashed with an uncaught
        `pydantic.ValidationError` trying `NormalizedResponse.model_validate(None)`. Byte-identical
        replay is structurally impossible here — the content was never retained anywhere to
        reconstruct, a genuine, disclosed tension between AC-3 and §9.4, not a bug in the retention
        decision itself — so this degrades through the exact same named, typed-error path
        `PayloadExpired` above already uses, rather than crashing."""
        if result.status == "ok" and result.payload is None:
            raise TerminalError(
                "zdr_payload_not_retained: a ZDR-flagged backend's response body is never "
                "retained (§9.4); replay cannot reconstruct it",
                backend_code="zdr_payload_not_retained",
            )
        if isinstance(result.payload, BlobRef):
            assert self._blobs is not None, "a recorded BlobRef payload needs an armed blob store"
            try:
                raw = self._blobs.get(result.payload)
            except PayloadExpired as exc:
                raise TerminalError(
                    f"payload_expired: {exc}", backend_code="payload_expired"
                ) from exc
            return json.loads(raw)
        return result.payload

    async def exec(self, req: StepRequest, *, run: Callable[[], Any]) -> ExecResult:
        # Replay (plan §4.3): a step already terminal in the journal serves its recorded outcome
        # back instead of ever calling run() — the mechanism that makes AC-3's "the registry is
        # never consulted" true (run()'s closure, which holds the real adapter call, is simply not
        # entered). Checked before either gate: a step's outcome, once decided, replays uniformly
        # regardless of what a LIVE re-check of either gate would say today (e.g. AC-15's
        # "credentials became available since the original run" case must still replay the
        # original skip, not attempt a live dispatch just because the world changed).
        ref = StepRef(run_id=req.run_id, step_path=req.step_path, step_seq=req.step_seq)
        terminal = next((r for r in self._journal.get(ref) if r.status in _TERMINAL_STATUSES), None)
        if terminal is not None:
            if terminal.status == "failed":
                assert terminal.error is not None
                raise _reconstruct_error(terminal.error)
            return ExecResult(
                payload=self._resolve_replay_payload(terminal),
                status=terminal.status,
                journal_seq=terminal.journal_seq,
                replayed=True,
            )

        refusal = self._gate(req)
        if refusal is not None:
            appended = self._journal.append(self._sanitizer_scrub(refusal))
            assert appended.error is not None
            # Round-4 F13's corrected call shape — matching the idiom every other real call site
            # already uses (`router/router.py:209`, `evals/runner.py:88`): one positional message,
            # `constraint=` keyword-only. `_gate`'s own `code` — "not_in_pinned_set" or
            # "descriptor_digest_mismatch" — becomes `constraint`; the message is synthesized from
            # it since `_gate` never records a longer human-readable detail.
            raise ComplianceRefused(
                appended.error.detail or appended.error.code, constraint=appended.error.code
            )

        skip = self._missing_credentials_gate(req)
        if skip is not None:
            appended = self._journal.append(self._sanitizer_scrub(skip))
            return ExecResult(
                payload=None, status="skipped", journal_seq=appended.journal_seq, replayed=False
            )

        self._journal.append(
            self._sanitizer_scrub(
                StepResult(
                    step_id=req.step_id,
                    run_id=req.run_id,
                    step_path=req.step_path,
                    step_seq=req.step_seq,
                    backend_id=req.backend_id,
                    status="attempted",
                    attempt=req.attempt,
                    idempotency_key=req.idempotency_key,
                    content_key=req.content_key,
                    started_epoch_ms=int(self._clock.now_wall_ms()),
                )
            )
        )

        try:
            result = await asyncio.to_thread(run)
        except asyncio.CancelledError:
            # Round-1 F1: a losing `parallel:` branch's task.cancel() raises THIS — a
            # BaseException, not an Exception, so the clause below cannot catch it — while `run()`
            # is still in flight inside asyncio.to_thread. Without a terminal record here, this
            # step is indistinguishable on replay from a genuine crash-before-dispatch (AC-12/
            # AC-13's territory) and would re-dispatch for real on resume, silently breaking AC-3
            # for the single most common shape a real strategy uses (a race). Fix: a "cancelled"
            # terminal record (no error/cost fields — this was never a failure), then re-raise
            # unchanged so `t.cancel()`'s caller-visible semantics are preserved exactly.
            self._journal.append(
                self._sanitizer_scrub(
                    StepResult(
                        step_id=req.step_id,
                        run_id=req.run_id,
                        step_path=req.step_path,
                        step_seq=req.step_seq,
                        backend_id=req.backend_id,
                        status="cancelled",
                        attempt=req.attempt,
                        idempotency_key=req.idempotency_key,
                        content_key=req.content_key,
                        ended_epoch_ms=int(self._clock.now_wall_ms()),
                    )
                )
            )
            raise
        except Exception as exc:
            self._journal.append(
                self._sanitizer_scrub(
                    StepResult(
                        step_id=req.step_id,
                        run_id=req.run_id,
                        step_path=req.step_path,
                        step_seq=req.step_seq,
                        backend_id=req.backend_id,
                        status="failed",
                        attempt=req.attempt,
                        idempotency_key=req.idempotency_key,
                        content_key=req.content_key,
                        error=StepError(
                            code=type(exc).__name__,
                            taxonomy=_classify_taxonomy(exc),
                            # M9 (security review): the ORIGINAL message, not just the taxonomy
                            # class name — `_reconstruct_error`'s own `err.detail or err.code`
                            # fallback otherwise has nothing but the type name to rebuild a
                            # replayed exception's message from, silently changing client-visible
                            # retry/behavior decisions. `_sanitizer_scrub` (below `exec`, wrapping
                            # this whole record) already walks `error.detail` for secret values —
                            # this text passes through that same chokepoint before it ever reaches
                            # the journal.
                            detail=str(exc),
                        ),
                        ended_epoch_ms=int(self._clock.now_wall_ms()),
                    )
                )
            )
            raise

        # Phase C round-2 (Findings 8 and 6, "the retention ceiling" half): this
        # step's own backend just genuinely dispatched — tighten (never widen) the run's stamped
        # ceiling from THIS descriptor's own declared limit, if it's stricter than what's already
        # recorded. A backend that stays merely eligible never reaches this line at all, so it can
        # never affect the stamp — see `retention.tighten_retention`'s own docstring for why this
        # replaces the old arm-time, whole-eligible-set computation.
        # `now_wall_ms()`, matching the base `_arm_ledger` stamped with: `tighten_retention` takes
        # `min(recorded, now + hours)`, so feeding it the other clock would make a monotonic
        # reading (~1e9) win against a wall-clock stamp (~1e12) every time and shred the run on its
        # first hosted dispatch. Mixing the two bases in one comparison is the defect, either way.
        if self._ledger_root is not None and req.backend_id is not None:
            adapter = self._registry.get(req.backend_id) if self._registry is not None else None
            if adapter is not None:
                tighten_retention(
                    self._ledger_root,
                    req.run_id,
                    adapter.descriptor,
                    now_epoch_ms=int(self._clock.now_wall_ms()),
                )

        # Phase C round-1 (Low/confirm-only, not filed as a defect): the full response body —
        # whatever include_backend_raw/typed_fields/image settings the request itself asked for —
        # is retained as one blob whenever the ledger is armed; there is no separate opt-out for
        # just the highest-sensitivity fields short of not arming the ledger at all. §9.4's "opt-in"
        # framing for those specific fields is ambiguous and not a T1 plan commitment either way.
        payload = None
        zdr = self._is_zdr_backend(req.backend_id)
        if self._blobs is not None and not zdr and hasattr(result, "to_schema_dict"):
            body = self._sanitizer.scrub_bytes(
                json.dumps(result.to_schema_dict(), sort_keys=True, default=str).encode("utf-8")
            )
            digest = "sha256:" + hashlib.sha256(body).hexdigest()
            payload = self._blobs.put(req.run_id, digest, body, "application/json")

        appended = self._journal.append(
            self._sanitizer_scrub(
                StepResult(
                    step_id=req.step_id,
                    run_id=req.run_id,
                    step_path=req.step_path,
                    step_seq=req.step_seq,
                    backend_id=req.backend_id,
                    status="ok",
                    attempt=req.attempt,
                    idempotency_key=req.idempotency_key,
                    content_key=req.content_key,
                    payload=payload,
                    ended_epoch_ms=int(self._clock.now_wall_ms()),
                )
            )
        )
        return ExecResult(
            payload=result, status="ok", journal_seq=appended.journal_seq, replayed=False
        )

    def _sanitizer_scrub(self, result: StepResult) -> StepResult:
        if result.error is None or result.error.detail is None:
            return result
        scrubbed = self._sanitizer.scrub_text(result.error.detail)
        if scrubbed == result.error.detail:
            return result
        return result.model_copy(
            update={"error": result.error.model_copy(update={"detail": scrubbed})}
        )
