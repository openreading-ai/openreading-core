"""StepRequest, StepResult, StepRef, BlobRef — mirrors schemas/step.v0.1.json and
journal.v0.1.json (internal/design/ledger.md §5.4). `StepResult.payload` is `BlobRef | JsonValue`,
never `Any`: a non-serializable payload must raise before construction, not after the side effect
that produced it.

Beyond the design doc's literal required/optional lists, `StepResult` also carries `run_id`,
`step_path`, `step_seq`, `content_key` — additive fields free to add since this family is not yet
vendored (T1 ships it first). `step_path`/`step_seq` let `Journal.get(StepRef)` find every record
at a key without a separate index; `content_key` records the pure content-derived identity
alongside `idempotency_key` (which a caller may override) per §5.5's "three identities" table.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

StepKind = Literal["intake", "submit", "drive", "emit", "translate"]
StepStatus = Literal["attempted", "ok", "skipped", "retryable", "failed", "cancelled", "replayed"]

JsonValue = dict[str, Any] | list[Any] | str | int | float | bool | None


class BlobRef(BaseModel):
    """Addressed per run, never globally (§5.4): `(run_id, digest)`. `BlobStore.get` refuses a
    ref whose run_id differs from the requesting run — T1's single-process walk never constructs
    a mismatched ref, so this is satisfied structurally rather than by an explicit runtime check."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    digest: str  # "sha256:<64hex>" of the PLAINTEXT
    size_bytes: int
    media_type: str
    store: str | None = None


class StepError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    taxonomy: str
    detail: str | None = None


class StepRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_id: str
    run_id: str
    kind: StepKind
    step_path: str
    step_seq: int
    attempt: int
    idempotency_key: str | None = None
    content_key: str | None = None
    backend_id: str | None = None
    credentials_ref: list[str] = Field(default_factory=list)
    document: BlobRef | None = None
    options: dict[str, Any] = Field(default_factory=dict)
    absolute_deadline_epoch_ms: int | None = None
    parent_step_id: str | None = None
    # Ledger T3 (§4.3b): the primary env var of every credential/config field the caller already
    # found missing (readiness.missing_required's own return value) — additive, empty by default,
    # mirroring `journal_seq`'s own precedent below. The caller (`_run_branch`/`_run_leaf`) still
    # runs that check itself (it alone holds `ctx.broker`); this just hands the executor enough to
    # journal a real `StepResult(status="skipped", error=StepError(code="missing_credentials", ...))`
    # instead of the decision staying orchestration-only, with no journal record for a later resume
    # to consult (internal/eng-council/plans/sprint26-T3-plan.md §4.3b).
    missing_credentials: list[str] = Field(default_factory=list)


class StepResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_id: str
    status: StepStatus
    attempt: int
    run_id: str | None = None
    step_path: str | None = None
    step_seq: int | None = None
    backend_id: str | None = None
    idempotency_key: str | None = None
    content_key: str | None = None
    payload: BlobRef | JsonValue = None
    error: StepError | None = None
    started_epoch_ms: int | None = None
    ended_epoch_ms: int | None = None
    resolved_version: str | None = None
    # Ledger T3 (§4.1/§7.4): a per-run monotonic counter — the append count for this record's
    # `run_id` so far, assigned by `Journal.append` (`JsonlJournal`'s own instance-held counter;
    # `NullJournal` leaves it `None`, consistent with L1's zero-delta guarantee). Additive, mirrors
    # T1's own precedent for `run_id`/`step_path`/`step_seq`/`content_key` — a value ON the record
    # itself, not re-derived from `Journal.get`'s return order, which is a `JsonlJournal`
    # implementation detail no other conforming `Journal` is required to preserve as meaningfully
    # ordered (internal/design/ledger.md §16's substrate contract).
    journal_seq: int | None = None

    @field_validator("payload", mode="before")
    @classmethod
    def _payload_must_be_json_native(cls, v: Any) -> Any:
        """`JsonValue`'s `list[Any]` arm otherwise accepts any iterable — a `set`/`frozenset`/
        `tuple` would silently become a `list` rather than raising, which the design's own framing
        for this field ("never `Any`... a non-serializable payload must raise") argues against
        (Phase C round-1 F7)."""
        if isinstance(v, (set, frozenset, tuple)):
            raise ValueError(f"StepResult.payload must be JSON-native, not {type(v).__name__}")
        return v


class ExecResult(BaseModel):
    """`Executor.exec()`'s return contract (Ledger T3 §4.0, resolving round-2 F7): additive over
    T1's bare `run()` payload, carrying the three things a caller now needs and the bare payload
    structurally could not — a null/skip signal, a replayed-vs-live flag, and a per-branch
    `journal_seq` — without widening what a live "ok" dispatch hands back (`payload` is still
    exactly `run()`'s own result object on that path).

    `status` is only ever `"ok"`, `"skipped"`, or `"cancelled"` (the last via REPLAY of a recorded
    `"cancelled"` record — never live, since a live cancellation always re-raises,
    `InlineExecutor.exec`'s own `CancelledError` handler). A `"failed"` outcome — live or
    replayed — always raises instead of returning; it is never represented as an `ExecResult`, so a
    caller's own `except (TerminalError, RetryableError, UnsupportedFeatureError,
    ScopeRefused)` handling keeps working unmodified on both a live and a replayed failure."""

    model_config = ConfigDict(extra="forbid")

    payload: Any
    status: StepStatus
    journal_seq: int | None = None
    replayed: bool = False


class StepRef(BaseModel):
    """The `Journal.get` read/write key (plan §8): identifies every `StepResult` recorded for one
    leaf's dispatch — an `attempted` record and its terminal follow-on share one key."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    step_path: str
    step_seq: int
