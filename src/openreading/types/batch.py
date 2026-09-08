"""Pydantic mirrors of the envelope families: batch-result.v0.2 and
corpus-report.v0.1. Envelopes (`BatchResult`, `CorpusReport`) are extra="ignore" — forward-tolerant
of a newer producer's additive top-level fields, dropping them on re-serialization (Canon §8);
nested payload models keep extra="forbid" so construction typos are caught. `to_schema_dict()`
matches NormalizedResponse: enums→values, None dropped."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# --- batch-result -----------------------------------------------------------------------

# BL-84: the ceiling on `jobs` — a real ThreadPoolExecutor size (batch/runner.py), so this bounds a
# genuine resource-exhaustion primitive (OS threads / open sockets), not merely a validation
# nicety. Defined here — the base of the batch/types dependency graph (this module has no
# openreading imports of its own) — so both the runtime bounds-check (batch.runner.bound_jobs) and
# this module's own structural Field(le=...) backstop below share ONE number instead of two
# independently-maintained literals that could silently drift apart. "Low tens": mirrors the
# stdlib's own ThreadPoolExecutor(max_workers=None) heuristic, min(32, os.cpu_count() + 4).
MAX_BATCH_JOBS = 32


class SourceRef(BaseModel):
    """Identity of one intake source. `relpath` (path relative to an expanded directory root) is
    the stable cross-run pairing key for corpus compare; `sha256` is the content-hash fallback."""

    model_config = ConfigDict(extra="forbid")

    filename: str
    format: str
    path: str | None = None
    url: str | None = None
    relpath: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = None
    sha256: str | None = None


class BatchStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: Literal["succeeded", "partial", "failed"]


class BatchItemError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str | None = None


class BatchItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: SourceRef
    state: Literal["succeeded", "failed"]
    response: dict[str, Any] | None = None  # a full response.v0.3 envelope (validated separately)
    error: BatchItemError | None = None
    transport: Literal["platform", "native"] | None = None


class BatchSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total: int
    succeeded: int
    failed: int
    duration_ms: float | None = None
    pages_processed: int | None = None
    backends: dict[str, int] = Field(default_factory=dict)


class BatchRequestEcho(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backend: str | None = None
    strategy: str | None = None
    # BL-84 introduced this as a structural backstop alongside batch.runner.bound_jobs's
    # procedural check: this model must never represent a `jobs` value the schema itself would
    # reject (schema: "jobs": {"minimum": 1}; no "maximum" — the schema imposes no upper bound at
    # all). BL-84's own `le=MAX_BATCH_JOBS` went further than the schema requires: it pinned this
    # model to the DEFAULT ceiling regardless of any caller-supplied max_jobs= / --max-jobs
    # override, so the one input range where raising the ceiling is meaningful (jobs in
    # (MAX_BATCH_JOBS, max_jobs]) was unconditionally rejected here even after
    # batch.runner.bound_jobs had already, correctly, let it through (BL-94). `ge=1` alone already
    # fully satisfies the "never represent what the schema would reject" goal; the ceiling is
    # bound_jobs()'s alone to enforce, since only it ever learns about a per-call override.
    jobs: int | None = Field(default=None, ge=1)
    source_args: list[str] = Field(default_factory=list)


class BatchWarning(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str | None = None


class BatchResult(BaseModel):
    """Envelope for a batch run (extra="ignore" — forward-tolerant)."""

    model_config = ConfigDict(extra="ignore")

    schema_version: str = "0.2"
    status: BatchStatus
    request: BatchRequestEcho | None = None
    items: list[BatchItem] = Field(default_factory=list)
    summary: BatchSummary
    warnings: list[BatchWarning] | None = None

    def to_schema_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


# --- corpus-report ----------------------------------------------------------------------


class CorpusSubject(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    source: str | None = None
    backend_tally: dict[str, int] = Field(default_factory=dict)


class CorpusDocumentSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    relpath: str | None = None
    filename: str | None = None
    sha256: str | None = None


class CorpusDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: CorpusDocumentSource
    verdict: Literal["equivalent", "divergent", "mixed", "unpaired"]
    report: dict[str, Any] | None = None  # a full comparison-report v0.2 (validated separately)


class CorpusRollup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    documents: int
    equivalent: int = 0
    divergent: int = 0
    mixed: int = 0
    unpaired: int = 0
    by_finding_code: dict[str, int] = Field(default_factory=dict)


class CorpusReport(BaseModel):
    """Envelope for a corpus comparison (extra="ignore" — forward-tolerant)."""

    model_config = ConfigDict(extra="ignore")

    schema_version: str = "0.1"
    subjects: list[CorpusSubject] = Field(default_factory=list)
    documents: list[CorpusDocument] = Field(default_factory=list)
    rollup: CorpusRollup

    def to_schema_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)
