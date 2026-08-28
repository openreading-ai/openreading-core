"""The compile pipeline (integration.md §2): request + config -> a pruned tree the engine walks.

1. **Route once, up front.** Run the existing 3-stage `Router.route` for the eligible set + drop
   reasons. When a strategy engages, `routing.fallback` is stripped first so `auto` leaves see
   pure stage-3 order (integration.md §2a).
2. **Normalize** each strategy (extends resolved, shorthand expanded).
3. **Prune** every leaf whose backend the router dropped, carrying the `DropReason`; collapse a
   composite whose children all vanish; a fully-pruned root is today's terminal
   `no_compliant_backend` refusal — never a silent downgrade.

The executor consumes only pruned trees — the current executor invariant ("consumes ONLY the
RoutePlan, can never widen eligibility") lifted to trees.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, fields, replace
from typing import Any

from openreading.adapters.registry import BUILTIN_ADAPTERS
from openreading.router.registry import Registry
from openreading.router.router import RoutePlan, Router, RouterConfig
from openreading.strategies.model import DeciderLLM, StrategyConfig
from openreading.strategies.normalize import normalize_strategy
from openreading.strategies.presets import PRESET_NAMES
from openreading.strategies.trace import DropRecord
from openreading.types.descriptor import AdapterDescriptor
from openreading.types.errors import ComplianceRefused
from openreading.types.request import Compliance, OpenReadingRequest

# compliance keys the file `policy:` block can add to the request's effective compliance.
_COMPLIANCE_BOOL = ("require_baa", "no_train_on_data", "require_local")
_COMPLIANCE_STR = ("data_region", "max_retention")
_DURATION_UNITS = {"ms": 1, "s": 1000, "m": 60_000, "h": 3_600_000}


@dataclass
class CompiledPlan:
    """The output of compilation: the pruned root tree, the pruned tree of every strategy (for
    `use:` reference resolution), the eligible-backend order, drop records, and provenance."""

    name: str
    root: dict[str, Any]
    trees: dict[str, dict[str, Any] | None]
    eligible: list[str]
    dropped: list[DropRecord]
    config_hash: str
    overrides_fallback: bool = False
    warnings: list[tuple[str, str]] = field(default_factory=list)  # (code, message)
    # the post-union effective compliance (request ∪ --policy ∪ file policy:) — feeds route facts
    effective_compliance: dict[str, Any] = field(default_factory=dict)
    # `limits:` operator time ceiling wrapping every strategy-engaged run (spec §6.4)
    max_duration_ms: int | None = None
    # the root strategy was authored in the Plain dialect (internal/design/simple-strategies.md §9) —
    # run_strategy tags gate records with their Plain source word for `explain` grouping.
    plain_sourced: bool = False
    # the file `decider.llm` block (M4) + the effective RouterConfig, so run_strategy can run the
    # two-key enablement + compliance gate on the decider backend (decider.md §1, §3.5).
    decider: DeciderLLM | None = None
    router_config: RouterConfig = field(default_factory=RouterConfig)
    # RoutePlan.baa_tier_notes, carried through so the engine can warn on the responding backend.
    baa_tier_notes: dict[str, str] = field(default_factory=dict)


def compile_strategy(
    req: OpenReadingRequest,
    name: str,
    config: StrategyConfig,
    registry: Registry,
    router_config: RouterConfig | None = None,
    plain_info: dict[str, Any] | None = None,
) -> CompiledPlan:
    """Compile `name` for `req`. Raises ComplianceRefused when the root prunes to nothing (the
    same terminal outcome the `auto` arm gives on an empty plan). `plain_info` (from the loader)
    marks a Plain-dialect root so run_strategy can tag gate records for `explain` grouping (§9)."""
    router_config = router_config or RouterConfig()
    info = plain_info.get(name) if plain_info else None
    plain_sourced = bool(info and getattr(info, "dialect", None) == "plain")

    # (0) union the file `policy:` block into the effective compliance + RouterConfig (spec §1.3:
    # constraints only ADD — compliance is never widened). This feeds BOTH the prune and the
    # route compliance facts.
    effective_compliance = _union_compliance(req.compliance, config.policy)
    router_config = _merge_router_config(router_config, config.policy)

    # (1) build the effective request: unioned compliance + stripped routing.fallback (so auto
    # leaves see pure stage-3 order).
    updates: dict[str, Any] = {}
    if effective_compliance:
        updates["compliance"] = Compliance(**effective_compliance)
    overrides_fallback = bool(req.routing and req.routing.fallback)
    if overrides_fallback and req.routing is not None:
        updates["routing"] = req.routing.model_copy(update={"fallback": None})
    route_req = req.model_copy(update=updates) if updates else req

    plan = Router(registry, router_config).route(route_req)
    eligible = plan.eligible_ids  # chosen + fallbacks, in stage-3 order
    drop_reasons = plan.dropped  # id -> DropReason

    # (2)+(3) normalize + prune every strategy (so `use:` refs resolve against pruned trees).
    # BL-168: iterate in a fixed order — `set()` iteration order is hash-seed-dependent, and it
    # reached `trace.dropped`, `orchestration["dropped"]`, and the ComplianceRefused message below,
    # so the explanation shown for *why a compliant run was refused* was not reproducible.
    all_names = sorted(set(config.strategies) | PRESET_NAMES | {name})
    trees: dict[str, dict[str, Any] | None] = {}
    dropped_records: dict[str, DropRecord] = {}
    for sname in all_names:
        tree = normalize_strategy(sname, config)
        pruned = _prune_node(tree, eligible, drop_reasons, dropped_records)
        trees[sname] = pruned

    root = trees.get(name)
    if root is None:
        sorted_dropped = sorted(dropped_records.values(), key=lambda d: d.backend)
        dropped_list = ", ".join(f"{d.backend}:{d.code}" for d in sorted_dropped)
        raise ComplianceRefused(
            f"strategy {name!r} has no compliant backend for this request (dropped: {dropped_list})",
            constraint="no_compliant_backend",
        )

    config_hash = _compute_config_hash(root, effective_compliance, router_config, registry, plan)

    warnings: list[tuple[str, str]] = []
    if overrides_fallback:
        warnings.append(
            (
                "strategy_overrides_fallback",
                f"routing.fallback ignored; strategy {name!r} owns the chain",
            )
        )

    limits = config.limits
    max_dur = (
        _duration_ms(limits.max_duration_per_doc)
        if limits and limits.max_duration_per_doc
        else None
    )

    return CompiledPlan(
        name=name,
        root=root,
        trees=trees,
        eligible=eligible,
        dropped=sorted(dropped_records.values(), key=lambda d: d.backend),
        config_hash=config_hash,
        overrides_fallback=overrides_fallback,
        warnings=warnings,
        effective_compliance=effective_compliance,
        max_duration_ms=max_dur,
        decider=(config.decider.llm if config.decider else None),
        router_config=router_config,
        plain_sourced=plain_sourced,
        baa_tier_notes=dict(plan.baa_tier_notes),
    )


def _compute_config_hash(
    root: dict[str, Any],
    effective_compliance: dict[str, Any],
    router_config: RouterConfig,
    registry: Registry,
    plan: RoutePlan,
) -> str:
    """BL-163: `config_hash` must change whenever the COMPLIANCE POSTURE changes eligibility, not
    only when the pruned tree's shape happens to change. Hashing `root` alone let three mutually
    exclusive constraints (`require_baa`, `require_local`, `data_region`) share one digest with
    "no compliance at all" whenever they happened to prune the same leaves — and since
    `decision_id = sha256(config_hash|node_path|seq)` and `openreading replay` uses `config_hash`
    as its match key, that meant a trace recorded under one compliance posture could silently
    replay into a run executing under another (`_replay_decision`'s `trace_missing` downgrade is
    NOT changed here — that per-decision fallback is documented behavior, out of this item's
    scope; this closes the WHOLE-TRACE mismatch at load time instead, in `cli/app.py`).

    Folds `root`, `effective_compliance`, `router_config`, and a descriptor digest per backend the
    router actually classified (`eligible` + `dropped` — together every registered backend, since
    `Router.route` classifies each one or the other) into ONE JSON structure before hashing, rather
    than concatenating pre-hashed pieces with a delimiter — this sidesteps any "is `AB`+`C` distinct
    from `A`+`BC`" ambiguity a delimiter-based scheme would need to get right, while still changing
    the digest whenever any of the four inputs changes. `router_config`'s `frozenset` fields are
    sorted to lists first, or the digest would inherit BL-168's exact nondeterminism.

    Never include `credentials_ref`, resolved credentials, or anything secret-bearing — every
    input here is either the pruned tree (backend ids/config, no secrets), the compliance posture,
    deployment-level compliance confirmations (backend ids only), or a backend's own descriptor
    (public capability/compliance metadata)."""
    router_config_canonical = _canonical_router_config(router_config)
    participating_ids = set(plan.eligible_ids) | set(plan.dropped)
    descriptor_digests = sorted(
        f"{bid}:{_hash_json(_descriptor_for(registry, bid).to_schema_dict())}"
        for bid in participating_ids
    )
    payload = [root, effective_compliance, router_config_canonical, descriptor_digests]
    return "sha256:" + _hash_json(payload)


def _canonical_router_config(router_config: RouterConfig) -> dict[str, Any]:
    # BL-163 review: derive generically from the dataclass's own fields rather than
    # hand-enumerating them by name, so a future RouterConfig field can't silently drop out of the
    # hash by being forgotten here. A frozenset/set field is sorted to a list (never left to
    # json.dumps's `default=str` fallback, whose str(frozenset(...)) repr order is itself
    # hash-seed-dependent — the exact nondeterminism class BL-168 fixed, through a different door).
    # BL-163 review round 2: an unrecognized field type raises here, at compile time, rather
    # than silently falling through to `default=str` — which could hash some future field shape
    # (a nested dict/dataclass, a plain set nested inside something) nondeterministically without
    # ever failing loud. Extend this function's dispatch, don't widen the silent fallback.
    canonical: dict[str, Any] = {}
    for f in fields(router_config):
        value = getattr(router_config, f.name)
        if isinstance(value, (frozenset, set)):
            canonical[f.name] = sorted(value)
        elif isinstance(value, (bool, str, int, float, type(None))):
            canonical[f.name] = value
        else:
            raise TypeError(
                f"RouterConfig.{f.name} is a {type(value).__name__}, which "
                "_canonical_router_config doesn't know how to hash deterministically — extend "
                "this function before adding a field of this shape"
            )
    return canonical


def _descriptor_for(registry: Registry, backend_id: str) -> AdapterDescriptor:
    # BL-163 review: every id here comes from plan.eligible_ids/plan.dropped, both
    # populated by Router.route iterating this SAME registry (router.py) — so a miss is a
    # programming error in the caller, not a runtime possibility this function should degrade for.
    adapter = registry.get(backend_id)
    assert adapter is not None, f"{backend_id!r} was classified by the router but isn't registered"
    return adapter.descriptor


def _hash_json(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _union_compliance(req_compliance, policy: dict[str, Any] | None) -> dict[str, Any]:
    """Effective compliance = request ∪ file `policy:` compliance keys, most-restrictive-wins
    (booleans OR to True; region/retention: request wins if set, else the file adds it).
    Constraints only ever ADD — this can never widen the request's compliance."""
    eff: dict[str, Any] = {}
    base = req_compliance.model_dump() if req_compliance else {}
    for k in _COMPLIANCE_BOOL:
        eff[k] = bool(base.get(k)) or bool(policy and policy.get(k))
    for k in _COMPLIANCE_STR:
        val = base.get(k) or (policy.get(k) if policy else None)
        if val is not None:
            eff[k] = val
    return eff


def _merge_router_config(base: RouterConfig, policy: dict[str, Any] | None) -> RouterConfig:
    """Fold the file policy's deployment keys into the RouterConfig (allow_unverified_compliance
    OR-s to True; train_optout_confirmed / baa_tier_confirmed union). `replace` rather than a fresh
    RouterConfig, so a field this fold does not name carries forward instead of silently resetting
    to its default."""
    if not policy:
        return base
    allow = base.allow_unverified_compliance or bool(policy.get("allow_unverified_compliance"))
    optout = set(base.train_optout_confirmed) | set(policy.get("train_optout_confirmed", []))
    baa_tier = set(base.baa_tier_confirmed) | set(policy.get("baa_tier_confirmed", []))
    return replace(
        base,
        allow_unverified_compliance=allow,
        train_optout_confirmed=frozenset(optout),
        baa_tier_confirmed=frozenset(baa_tier),
    )


def _duration_ms(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    for suffix, mult in _DURATION_UNITS.items():
        if value.endswith(suffix) and value[: -len(suffix)].isdigit():
            return int(value[: -len(suffix)]) * mult
    return None


def _prune_node(
    node: dict[str, Any],
    eligible: list[str],
    drop_reasons: dict[str, Any],
    dropped_records: dict[str, DropRecord],
) -> dict[str, Any] | None:
    """Return the node with dropped-backend leaves removed, or None if it collapses entirely."""
    if "backend" in node:
        slug = node["backend"]
        if slug == "auto":
            return node
        if slug in drop_reasons:
            _record_drop(slug, drop_reasons[slug], dropped_records)
            return None
        return node  # eligible (or an unknown id validate already flagged) — keep

    if "use" in node:
        return node  # resolved at walk time against the (possibly-None) referenced tree

    if "steps" in node:
        kept = [
            p
            for s in node["steps"]
            if (p := _prune_node(s, eligible, drop_reasons, dropped_records))
        ]
        if not kept:
            return None
        return {**node, "steps": kept}

    if "parallel" in node:
        kept = [
            p
            for b in node["parallel"]
            if (p := _prune_node(b, eligible, drop_reasons, dropped_records))
        ]
        if not kept:
            return None
        return {**node, "parallel": kept}

    if "route" in node:
        r = node["route"]
        rules = []
        for rule in r.get("rules", []):
            pruned = _prune_node(rule["use"], eligible, drop_reasons, dropped_records)
            if pruned is not None:
                rules.append({**rule, "use": pruned})
        default = _prune_node(r["default"], eligible, drop_reasons, dropped_records)
        if default is None:
            return None  # a route with no reachable default collapses (integration.md §2c)
        return {**node, "route": {**r, "rules": rules, "default": default}}

    if "decide" in node:
        d = node["decide"]
        among = [
            p for m in d["among"] if (p := _prune_node(m, eligible, drop_reasons, dropped_records))
        ]
        otherwise = _prune_node(d["otherwise"], eligible, drop_reasons, dropped_records)
        # BL-54: the operator's own configured otherwise: was itself pruned while at least one
        # among: survivor remains (integration.md §2c) — mark the substitution below so the engine
        # can trace it (decision_record's downgraded field) instead of it reading identically to
        # "nothing unusual happened". Only true substitution counts, not the separate "among: also
        # empties" collapse a few lines down, which returns None for the whole node regardless.
        otherwise_pruned = otherwise is None and bool(among)
        if otherwise is None:
            otherwise = among[0] if among else None
        if not among or otherwise is None:
            return None
        new_decide = {**d, "among": among, "otherwise": otherwise}
        if otherwise_pruned:
            new_decide["otherwise_pruned"] = True
        return {**node, "decide": new_decide}

    return node


def _record_drop(slug: str, reason: Any, dropped_records: dict[str, DropRecord]) -> None:
    if slug in dropped_records:
        return
    stage = getattr(reason, "stage", 0)
    code = getattr(reason, "code", "dropped")
    detail = getattr(reason, "detail", "")
    dropped_records[slug] = DropRecord(backend=slug, stage=stage, code=code, detail=detail)


def is_known_backend(slug: str) -> bool:
    return slug in BUILTIN_ADAPTERS
