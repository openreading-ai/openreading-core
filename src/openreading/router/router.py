"""The 3-stage router (internal/research/openreading/routing_and_compliance.md §4.1): compliance
hard-filter → capability filter → cost/quality/latency scoring. It reads AdapterDescriptors only
and never branches on backend type.

Invariants enforced here:
- Stages 1 & 2 are boolean gates; stage 3 only reorders survivors.
- Compliance is NEVER relaxed by fallback: the fallback chain is drawn ONLY from the
  stage-1-and-2 surviving set (§5.3). A backend dropped for compliance cannot reappear.
- UNVERIFIED compliance fails closed (§4.4), via RouterConfig.allow_unverified_compliance=False.
- A tier-gated BAA or training opt-out counts only where the operator confirmed it; the plan
  records the confirmations its eligible set rests on so the response can name them.
- If the eligible set is empty, the plan is a terminal compliance-bounded failure rather than a
  silent downgrade. Local backends are the guaranteed floor for require_local/require_baa.
"""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass, field, replace

from openreading.adapters.base import BackendAdapter
from openreading.router import compliance as comp
from openreading.router.compliance import DropReason, RouterConfig
from openreading.router.registry import Registry
from openreading.types.descriptor import AdapterDescriptor
from openreading.types.errors import ComplianceRefused
from openreading.types.request import Features, OpenReadingRequest

# request.features flag -> descriptor capability that must be truthy when the flag is set.
_FEATURE_CAPABILITY = {
    "handwriting": "handwriting",
    "forms_key_value": "forms_key_value",
    "signatures": "signatures",
    "classification": "classification",
}

_QUALITY_BY_PRIORITY = {"P0": 1.0, "P1": 0.6, "P2": 0.3}
# (w_quality, w_cost) by optimize_for. No latency field in the descriptor yet, so w_latency≈0.
_WEIGHTS = {
    "accuracy": (1.0, 0.1),
    "cost": (0.2, 1.0),
    "latency": (0.5, 0.1),
    "offline": (0.3, 0.2),
    None: (0.7, 0.5),
}


# mimetypes' builtin table only learned the OOXML vnd.* family after 3.11, and a container with no
# /etc/mime.types has nothing else to learn it from, so pin the three the fleet actually advertises.
_EXT_BY_MIME = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
}


def _truthy_cap(value) -> bool:
    return value not in (False, None, "false", "")


def _format_token(mime: str) -> str:
    """A MIME type -> the token compared against a descriptor's `input_formats`. mimetypes owns
    the mapping because the trailing segment of a MIME type is not its format: the OOXML family
    ends in 'document'/'sheet'/'presentation' and image/jpeg ends in 'jpeg' where the fleet
    advertises 'jpg'. The split is only the last resort, for types nothing recognizes."""
    mime = mime.strip().lower()
    ext = mimetypes.guess_extension(mime)
    if ext:
        return ext.lstrip(".")
    return _EXT_BY_MIME.get(mime) or mime.split("/")[-1].split(".")[-1]


@dataclass
class ScoredBackend:
    adapter: BackendAdapter
    score: float


@dataclass
class RoutePlan:
    chosen: BackendAdapter | None
    fallbacks: list[BackendAdapter] = field(default_factory=list)
    dropped: dict[str, DropReason] = field(default_factory=dict)
    terminal_reason: str | None = None
    # eligible backend id -> the operator confirmation its require_baa eligibility rests on. The
    # executor turns each into a BAA_TIER_CONFIRMED_WARNING on the response it actually returns.
    baa_tier_notes: dict[str, str] = field(default_factory=dict)
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
        return replace(
            self,
            chosen=kept[0] if kept else None,
            fallbacks=kept[1:],
            backend_allowlist=allowlist,
        )


class Router:
    def __init__(self, registry: Registry, config: RouterConfig | None = None) -> None:
        self.registry = registry
        self.config = config or RouterConfig()

    # ---- stage 1 -------------------------------------------------------------
    def _compliance_drop(
        self, req: OpenReadingRequest, desc: AdapterDescriptor
    ) -> DropReason | None:
        return comp.evaluate(req.compliance, desc, self.config)

    # ---- stage 2 -------------------------------------------------------------
    def _capability_drop(
        self, req: OpenReadingRequest, desc: AdapterDescriptor
    ) -> DropReason | None:
        feats: Features = req.features or Features()
        caps = desc.capabilities
        for flag, cap_name in _FEATURE_CAPABILITY.items():
            if getattr(feats, flag) and not _truthy_cap(getattr(caps, cap_name, False)):
                return DropReason(2, f"missing_{cap_name}", f"request requires {cap_name}")
        if req.extraction_schema is not None and not _truthy_cap(caps.custom_schema_extraction):
            return DropReason(2, "missing_custom_schema_extraction", "extraction_schema requested")
        # input format gate (loose): only when the backend advertises a format allow-list.
        mime = req.document.mime_type
        if mime and caps.input_formats:
            token = _format_token(mime)
            allowed = {f.lower() for f in caps.input_formats}
            # match the leading token of each advertised format, so an honest annotation like
            # "pdf (rasterized)" still satisfies a "pdf" request (the backend rasterizes it).
            allowed_tokens = {f.split("(")[0].split()[0] for f in allowed if f.split()}
            if token not in allowed_tokens and mime.lower() not in allowed:
                return DropReason(2, "unsupported_format", f"{mime} not in {sorted(allowed)}")
        return None

    # ---- stage 3 -------------------------------------------------------------
    def _score(self, req: OpenReadingRequest, desc: AdapterDescriptor) -> float:
        opt = req.routing.optimize_for if req.routing else None
        wq, wc = _WEIGHTS.get(opt, _WEIGHTS[None])
        prio = desc.router.integration_priority if desc.router else None
        quality = _QUALITY_BY_PRIORITY.get(prio or "", 0.5)
        local = bool(desc.compliance.runs_fully_local)
        lo, hi = desc.cost.usd_per_page_equiv_low, desc.cost.usd_per_page_equiv_high
        if local:
            cost = 0.0
        else:
            # `is not None`, not truthiness: a published 0.0 bound is a real free tier, and a
            # one-sided range must average over the bound it actually has.
            bounds = [b for b in (lo, hi) if b is not None]
            cost = sum(bounds) / len(bounds) if bounds else 0.05  # unpriced → mild penalty
        local_bonus = 0.25 if (local and opt in ("cost", "offline")) else 0.0
        return wq * quality - wc * cost + local_bonus

    # ---- orchestration -------------------------------------------------------
    def route(self, req: OpenReadingRequest) -> RoutePlan:
        dropped: dict[str, DropReason] = {}
        survivors: list[BackendAdapter] = []
        notes: dict[str, str] = {}
        for adapter in self.registry:
            desc = adapter.descriptor
            dr = self._compliance_drop(req, desc) or self._capability_drop(req, desc)
            if dr is not None:
                dropped[desc.id] = dr
            else:
                survivors.append(adapter)
                note = comp.baa_tier_confirmation(req.compliance, desc, self.config)
                if note is not None:
                    notes[desc.id] = note

        if not survivors:
            return RoutePlan(
                chosen=None,
                dropped=dropped,
                terminal_reason="no_compliant_backend",
            )

        scored = sorted(
            (ScoredBackend(a, self._score(req, a.descriptor)) for a in survivors),
            key=lambda s: s.score,
            reverse=True,
        )
        ordered = [s.adapter for s in scored]
        ordered = self._apply_explicit_fallback(req, ordered)
        return RoutePlan(
            chosen=ordered[0], fallbacks=ordered[1:], dropped=dropped, baa_tier_notes=notes
        )

    def _apply_explicit_fallback(
        self, req: OpenReadingRequest, ordered: list[BackendAdapter]
    ) -> list[BackendAdapter]:
        """Honor a caller-supplied routing.fallback ordering WITHIN the eligible set (never
        outside it — compliance is not relaxed). Listed ids move to the front in the given
        order; the rest keep their score order."""
        wanted = req.routing.fallback if req.routing else None
        if not wanted:
            return ordered
        wanted = list(dict.fromkeys(wanted))  # de-dup while preserving caller-given order
        by_id = {a.descriptor.id: a for a in ordered}
        front = [by_id[i] for i in wanted if i in by_id]
        rest = [a for a in ordered if a.descriptor.id not in set(wanted)]
        return front + rest

    def check_eligible(self, req: OpenReadingRequest, backend_id: str) -> BackendAdapter:
        """For a concretely-named backend: raise ComplianceRefused (before submit) if it fails
        the compliance hard-filter; return the adapter if it survives stage 1."""
        adapter = self.registry.get(backend_id)
        if adapter is None:
            raise KeyError(f"backend not registered: {backend_id!r}")
        dr = self._compliance_drop(req, adapter.descriptor)
        if dr is not None:
            raise ComplianceRefused(dr.detail, constraint=dr.code)
        return adapter
