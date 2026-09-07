"""Backend selection: a lookup, not an inference.

The router had three stages. A compliance hard-filter reading a per-vendor table, a capability
gate reading `input_formats` and five `Features` flags, and a scorer weighing a "quality" that was
really this project's own P0/P1/P2 build priority against price ranges typed off a pricing page.
Every input to all three was a claim core could not verify, and being wrong produced a silently
different answer rather than an error.

What replaces them is the caller's own statement, in three rules:

    1. the backend the caller named        -> a chain of one
    2. else `policy.backends`, in order    -> that is the chain
    3. else `pymupdf`                      -> zero credentials, zero config, cannot fail on setup

Rule 3 is what keeps a fresh clone working with no `.env` and no `openreading.yaml`. It is a named
default in one line of documentation, not a decision derived from data.

`auto` is gone with the stages. It asked core to infer, and inference is what left.

The law this module now keeps, which is the one worth carrying forward: **core holds no fact it
cannot verify.** A backend that cannot read a document refuses first-hand, and
`executor.execute_plan` already walks to the next one. Being wrong about a capability costs one
round trip; being wrong about a vendor claim cost a silent exclusion nothing recovered from.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from openreading.adapters.base import BackendAdapter
from openreading.router.compliance import DropReason, RouterConfig
from openreading.router.registry import Registry
from openreading.types.request import OpenReadingRequest


@dataclass
class RoutePlan:
    """What the router resolved: the `chosen` backend, the ordered `fallbacks` behind it, and one
    `DropReason` per backend an allow-list removed. `chain` is the order
    `executor.execute_plan` walks.

    `dropped` used to carry nine compliance codes and two capability ones, all of them core's own
    belief about a vendor. Every entry now names something the CALLER said: a backend their
    allow-list excluded. That is a fact core can state, so the map survives the removal of
    everything that used to fill it.
    """

    chosen: BackendAdapter | None
    fallbacks: list[BackendAdapter] = field(default_factory=list)
    dropped: dict[str, DropReason] = field(default_factory=dict)
    terminal_reason: str | None = None
    # The CALLER's backend allow-list (the server's OPENREADING_API_KEY_SCOPES entry for the
    # presented token), or None when the caller is unscoped. Set by restrict_to() below, which is
    # also what prunes the chain to it — the two are one operation on purpose, so a plan cannot be
    # pruned without arming executor.execute_plan's re-check, and cannot arm that re-check without
    # having been pruned. It travels ON the plan rather than as an execute_plan kwarg so the
    # executor keeps its "consumes ONLY the RoutePlan" invariant and no call site can forget it.
    backend_allowlist: frozenset[str] | None = None

    @property
    def eligible_ids(self) -> list[str]:
        ids = [self.chosen.descriptor.id] if self.chosen else []
        ids += [a.descriptor.id for a in self.fallbacks]
        return ids

    @property
    def chain(self) -> list[BackendAdapter]:
        return ([self.chosen] if self.chosen else []) + self.fallbacks

    def restrict_to(self, allowlist: frozenset[str] | None) -> RoutePlan:
        """This plan with every chain member outside `allowlist` removed. None = unscoped, and
        returns self unchanged.

        The whole CHAIN, not just `chosen`. A plan is chosen plus every fallback the compliance
        router computed, and `executor.execute_plan` walks all of it, so a caller ceiling applied
        to `chosen` alone bounds the first backend and none of the rest: the moment the first one
        fails on a document, the request walks the remaining eligible registry and delivers the
        document to backends the same caller is refused by name.

        Only ever a subtraction, over a list the compliance filter has already produced, so no
        allow-list can readmit a backend compliance dropped (the never-relaxed invariant above).
        The `fallbacks[1:]` reshuffle is not a re-rank: removing a member promotes the next
        surviving one in the router's own stage-3 order, which is what "try the next fallback"
        already means.

        An emptied chain is a `chosen=None` plan, and the caller decides what that means — for a
        scoped request it is 403 `scope_denied`, never a silent success on nothing, and never the
        compliance refusal an already-empty router plan gives (that one's fix is the policy; this
        one's is the token's allow-list).
        """
        if allowlist is None:
            return self
        kept = [a for a in self.chain if a.descriptor.id in allowlist]
        removed = {
            a.descriptor.id: DropReason(1, "scope_denied", "excluded by the caller's allow-list")
            for a in self.chain
            if a.descriptor.id not in allowlist
        }
        return replace(
            self,
            chosen=kept[0] if kept else None,
            fallbacks=kept[1:],
            dropped={**self.dropped, **removed},
            backend_allowlist=allowlist,
        )


class Router:
    """Resolves a request into an ordered chain. No stages, no scoring, no inference."""

    #: Rule 3. Zero credentials and zero config fields, so it cannot fail on setup, which is what
    #: keeps a fresh clone with no `.env` and no `openreading.yaml` able to read a document.
    DEFAULT_BACKEND = "pymupdf"

    def __init__(self, registry: Registry, config: RouterConfig | None = None, **_ignored) -> None:
        # `**_ignored` absorbs the `broker=` keyword that stage 1 needed to resolve a container
        # endpoint. Nothing here reads the environment any more; the argument is swallowed so
        # callers need not all change in the same commit.
        self.registry = registry
        self.config = config or RouterConfig()

    def _resolve_ids(self, req: OpenReadingRequest) -> list[str]:
        """The three rules, in order."""
        named = req.backend.id if req.backend else None
        if named and not named.startswith("strategy:"):
            return [named]
        if self.config.backends is not None:
            return list(self.config.backends)
        return [self.DEFAULT_BACKEND]

    def route(self, req: OpenReadingRequest) -> RoutePlan:
        """The caller's backends, in the caller's order, as a chain.

        An id naming nothing in the registry is skipped rather than raised on: a chain is a
        preference list, and one unavailable entry should not refuse a run the rest can serve.
        A chain that empties is `chosen=None`, which the caller turns into `scope_denied`.
        """
        ordered = [a for i in self._resolve_ids(req) if (a := self.registry.get(i)) is not None]
        ordered = self._apply_explicit_fallback(req, ordered)
        if not ordered:
            return RoutePlan(chosen=None, terminal_reason="no_backend_in_scope")
        return RoutePlan(chosen=ordered[0], fallbacks=ordered[1:])

    def _apply_explicit_fallback(
        self, req: OpenReadingRequest, ordered: list[BackendAdapter]
    ) -> list[BackendAdapter]:
        """Honour a caller-supplied `routing.fallback` ordering within the resolved set. Listed
        ids move to the front in the given order; the rest keep theirs. Never adds a backend the
        chain did not already contain, so it reorders a restriction rather than widening one."""
        wanted = req.routing.fallback if req.routing else None
        if not wanted:
            return ordered
        wanted = list(dict.fromkeys(wanted))  # de-dup, preserving caller-given order
        by_id = {a.descriptor.id: a for a in ordered}
        front = [by_id[i] for i in wanted if i in by_id]
        rest = [a for a in ordered if a.descriptor.id not in set(wanted)]
        return front + rest

    def check_eligible(self, req: OpenReadingRequest, backend_id: str) -> BackendAdapter:
        """The adapter for a concretely-named backend, or KeyError.

        It used to raise `ScopeRefused` for a backend that failed the hard filter. There is
        no filter: a caller who names a backend gets it, and the backend answers for itself.
        """
        adapter = self.registry.get(backend_id)
        if adapter is None:
            raise KeyError(f"backend not registered: {backend_id!r}")
        return adapter
