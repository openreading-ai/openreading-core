"""Liveness types (internal/design/liveness.md "Pulse").

Two types, deliberately on opposite sides of DECISIONS D5's dataclass/pydantic split:

* `ProbeResult` — a control-plane **dataclass**, exactly like `Health`. It is what an adapter's
  optional 9th method returns: only what the adapter can honestly *observe*. It is never
  serialized and never leaves the process.
* `LivenessReport` — the schema-bound **pydantic** model mirroring
  `src/openreading/schemas/liveness-report.v0.1.json`, which every surface (HTTP API, CLI)
  speaks. Round-trip tested against the vendored file.

The split is the protocol's whole ergonomic claim: an adapter author implements one narrow
measurement and never has to reproduce the status ladder. `openreading.liveness.check_liveness`
owns the ladder — including the rule that a missing required env var beats everything, and the
redaction pass — so it exists once rather than in every adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ProbeKind(StrEnum):
    """What performing this backend's liveness probe actually *does* — the static fact a UI needs
    before it offers a button. Declared on the descriptor, readable offline with no call.

    The kind is decision-relevant rather than trivia: it answers "does checking this leave my
    network, and can it cost me anything". Collapsing it to a boolean would throw away the only
    fact that separates an in-process import check from a call to a third party.
    """

    NONE = "none"  # no probe; the platform infers from configuration instead
    LOCAL = "local"  # in-process / subprocess; no network at all
    ENDPOINT = "endpoint"  # network, to infrastructure the CALLER operates (container, vLLM)
    VENDOR = "vendor"  # network, to a third party — leaves the caller's infrastructure


class ProbeOutcome(StrEnum):
    """What an adapter's probe observed. Strictly the MEASURED vocabulary: an adapter never
    reports `not_configured` or `configured_unverified`, because those are statements about the
    environment that the platform (not the adapter) resolves and grades."""

    LIVE = "live"
    UNREACHABLE = "unreachable"
    UNAUTHORIZED = "unauthorized"
    ERROR = "error"
    UNSUPPORTED = "unsupported"  # the BackendAdapter default — "I do not implement a probe"


class LivenessStatus(StrEnum):
    """The seven-state ladder (internal/design/liveness.md §2), in ascending order of what is known.

    `not_configured` → `configured_unverified` → `live`, with `unreachable`/`unauthorized`/`error`
    as the measured negatives and `not_supported` as the "nothing to say" floor.

    The load-bearing distinction is `configured_unverified` (INFERRED from a local config read —
    no round trip happened) versus `live` (MEASURED — we called it and it answered). Rendering
    those two the same way reintroduces the exact defect this ladder exists to fix: "configured"
    wearing the word "ready". `LivenessReport.measured` states which side a report is on without a
    consumer having to encode this enum's semantics.
    """

    NOT_SUPPORTED = "not_supported"
    NOT_CONFIGURED = "not_configured"
    CONFIGURED_UNVERIFIED = "configured_unverified"
    LIVE = "live"
    UNREACHABLE = "unreachable"
    UNAUTHORIZED = "unauthorized"
    ERROR = "error"


# The statuses that mean "a real round trip happened". Everything else is a local read.
_MEASURED = frozenset(
    {
        LivenessStatus.LIVE,
        LivenessStatus.UNREACHABLE,
        LivenessStatus.UNAUTHORIZED,
        LivenessStatus.ERROR,
    }
)


def is_measured(status: LivenessStatus) -> bool:
    """True when `status` was established by contacting the backend, False when it was inferred
    from configuration. The single definition of the measured/inferred partition."""
    return status in _MEASURED


@dataclass
class ProbeResult:
    """What an adapter's `probe_liveness` observed — a control-plane dataclass (D5), never
    serialized. `detail` is a short human sentence and MUST NOT contain a secret or a provider's
    raw response body; `version` is any version/model string the probe happened to learn.

    Constructed through the classmethods rather than by hand so an adapter never has to remember
    which outcome pairs with which shape.
    """

    outcome: ProbeOutcome
    detail: str = ""
    version: str | None = None

    @classmethod
    def live(cls, detail: str = "", version: str | None = None) -> ProbeResult:
        return cls(ProbeOutcome.LIVE, detail, version)

    @classmethod
    def unreachable(cls, detail: str = "") -> ProbeResult:
        return cls(ProbeOutcome.UNREACHABLE, detail)

    @classmethod
    def unauthorized(cls, detail: str = "") -> ProbeResult:
        return cls(ProbeOutcome.UNAUTHORIZED, detail)

    @classmethod
    def error(cls, detail: str = "") -> ProbeResult:
        return cls(ProbeOutcome.ERROR, detail)

    @classmethod
    def unsupported(cls, detail: str = "") -> ProbeResult:
        return cls(ProbeOutcome.UNSUPPORTED, detail)


class LivenessReport(BaseModel):
    """One backend's liveness answer — mirrors `liveness-report.v0.1.json`.

    `measured` is derivable from `status` (see `is_measured`) and is carried anyway, deliberately:
    the requirement is that a THIRD-PARTY frontend can tell a measurement from an inference
    without encoding this repo's enum semantics. One boolean makes that a field read instead of a
    lookup table, and it stays correct if the ladder ever grows a state.

    `latency_ms` is None for every inferred status — fabricating one would imply a round trip that
    did not happen. `checked_at` is honestly "when this report was produced", which for a measured
    report is when the backend was contacted and for an inferred one is when the environment was
    read.

    `detail` NEVER carries a secret (`check_liveness` redacts it on the way out) and never carries
    a provider's raw response body — providers have been seen echoing a rejected key back inside
    one (see `readiness.auth_rejected_hint`). No field carries an endpoint URL either: a URL can
    embed credentials, so reports name the env VAR, never the value.
    """

    model_config = ConfigDict(extra="forbid")

    # In-band family version (Canon §8 / C12), like every other response-family envelope: this
    # object is returned over HTTP to consumers this repo does not control, so it says what it is.
    schema_version: str = "0.1"
    backend: str
    status: LivenessStatus
    measured: bool
    probe: ProbeKind = ProbeKind.NONE
    checked_at: str  # ISO-8601 UTC, e.g. "2026-08-17T09:41:07Z"
    latency_ms: float | None = Field(default=None, ge=0)
    detail: str = ""
    version: str | None = None

    def to_schema_dict(self) -> dict:
        return self.model_dump(mode="json")
