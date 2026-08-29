"""The execution trace: attempt records + the `orchestration` block (execution.md §7,
integration.md §4).

The block is returned ALONGSIDE the response in v0.3 M1; embedding it into `response.orchestration`
(with the additive response-schema bump) lands with the surface wiring in 11.6 (D-v3-5). Warnings
go straight onto `response.warnings` now — that channel already exists in the v0.1 schema.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# execution.md §7 — the closed attempt-category vocabulary.
CATEGORIES = frozenset(
    {
        "succeeded",
        "skipped(missing_credentials)",
        "skipped(circuit_open)",
        "deadline_pruned",
        "quality_escalated",
        "review_escalated",
        "raced_lost",
        "judged_lost",
        "shadow",
        "merge_base",
        "merge_source",
        "decider_call",
        "judge_call",
    }
)


@dataclass
class GateRecord:
    predicate: str
    threshold: Any
    observed: Any
    fired: bool
    unavailable: bool = False
    # the Plain word this predicate was compiled from (internal/design/simple-strategies.md §9), set
    # only on a Plain-dialect strategy's gates; None (and absent from as_dict) for advanced gates,
    # which `explain` renders flat exactly as today.
    source: str | None = None

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "predicate": self.predicate,
            "threshold": self.threshold,
            "observed": self.observed,
            "fired": self.fired,
        }
        if self.unavailable:
            d["skipped"] = "signal_unavailable"
        if self.source is not None:
            d["source"] = self.source
        return d


@dataclass
class Attempt:
    backend: str
    category: str  # a member of CATEGORIES, or "error(<class>)"
    node: str  # node path / label
    duration_ms: int | None = None
    cost_usd: float | None = None
    cost_basis: str | None = None  # backend-reported basis for cost_usd, e.g. "billed"
    detail: str = ""
    # The adapter's OWN machine-readable failure code (`TerminalError.backend_code`), forwarded
    # rather than classified. The engine's error CLASS is a closed, schema-versioned set with an
    # `on_error` routing contract, and it cannot tell a permanent host fault (a missing local
    # binary, which will fail identically forever) from a transient provider blip without
    # branching on backend type. So `error(provider_error)` stays as it is and this carries what
    # the backend already knew: `detail` is prose for a human, `code` is what a monitor groups by.
    code: str | None = None
    gates: list[GateRecord] = field(default_factory=list)
    # cross-branch disagreement telemetry on a pick:best winner (§11); None otherwise.
    disagreement: float | None = None
    # Ledger T2 §7.3/§4.2: `bind_gates`/`recategorize` update `gates`/`category` (so every existing
    # consumer reading the CURRENT value off an Attempt keeps working unchanged — no schema break)
    # but also append a follow-on record here, so the SEQUENCE of updates survives rather than only
    # its final state (L4's append-only law, extended from the journal to the in-memory trace).
    revisions: list[dict[str, Any]] = field(default_factory=list)

    def bind_gates(self, records: list[GateRecord]) -> None:
        self.gates = records
        self.revisions.append({"kind": "gates_bound", "gates": [g.as_dict() for g in records]})

    def recategorize(self, new_category: str) -> None:
        self.revisions.append({"kind": "recategorized", "from": self.category, "to": new_category})
        self.category = new_category

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"backend": self.backend, "category": self.category, "node": self.node}
        if self.duration_ms is not None:
            d["duration_ms"] = self.duration_ms
        if self.cost_usd is not None:
            d["cost_usd"] = self.cost_usd
        if self.cost_basis is not None:
            d["cost_basis"] = self.cost_basis
        if self.detail:
            d["detail"] = self.detail
        if self.code:
            d["code"] = self.code
        if self.gates:
            d["gates"] = [g.as_dict() for g in self.gates]
        if self.disagreement is not None:
            d["disagreement"] = round(self.disagreement, 4)
        if self.revisions:
            d["revisions"] = self.revisions
        return d


@dataclass
class DropRecord:
    backend: str
    stage: int
    code: str
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "stage": self.stage,
            "code": self.code,
            "detail": self.detail,
        }


@dataclass
class Trace:
    """Accumulates a strategy run's attempts + compile-time drops, and builds the orchestration
    block. One Trace per request-level walk."""

    strategy: str
    config_hash: str
    attempts: list[Attempt] = field(default_factory=list)
    dropped: list[DropRecord] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)  # M4
    pages: list[dict[str, Any]] = field(default_factory=list)  # M5 (page-granularity provenance)
    merge: list[dict[str, Any]] = field(
        default_factory=list
    )  # M5 (pick:merge per-field provenance)
    webhook_dropped: list[dict[str, Any]] = field(default_factory=list)  # T8 late-delivery drops
    _dp_seq: int = 0  # walk-global decision-point counter → deterministic decision_id (M4)

    def record(self, attempt: Attempt) -> None:
        self.attempts.append(attempt)

    def assign_pages(self, pages: list[dict[str, Any]]) -> None:
        """Append this paged cascade's per-page provenance (Ledger T2 §7.3/§4.2) — a bare
        `self.pages = [...]` reassignment would clobber an earlier paged cascade's own contribution
        if a walk contains more than one (`granularity: page`) node."""
        self.pages.extend(pages)

    def next_dp_seq(self) -> int:
        """The next decision-point sequence number for a deterministic `decision_id`. Only the LLM
        decision points (gate_band / decide) draw from it; the deterministic route record does not."""
        n = self._dp_seq
        self._dp_seq += 1
        return n

    def total_cost(self) -> float:
        return sum(a.cost_usd or 0.0 for a in self.attempts)

    def orchestration(self, *, chosen_backend: str | None, outcome: str) -> dict[str, Any]:
        """Build the orchestration dict. `fallback_depth` = attempts that reached a backend
        before the returned result (successful or escalated rungs walked past)."""
        depth = max(0, sum(1 for a in self.attempts if a.category != "succeeded"))
        block: dict[str, Any] = {
            "strategy": self.strategy,
            "config_hash": self.config_hash,
            "chosen_backend": chosen_backend,
            "fallback_depth": depth,
            "outcome": outcome,  # "ok" | "degraded"
            "attempts": [a.as_dict() for a in self.attempts],
            "decisions": self.decisions,
        }
        if self.dropped:
            block["dropped"] = [d.as_dict() for d in self.dropped]
        if self.pages:
            block["pages"] = self.pages
        if self.merge:
            block["merge"] = self.merge
        if self.webhook_dropped:
            block["webhook_dropped"] = self.webhook_dropped
        return block
