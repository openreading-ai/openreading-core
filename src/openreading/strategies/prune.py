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
from dataclasses import dataclass, field, fields
from typing import Any

from openreading.adapters.registry import BUILTIN_ADAPTERS
from openreading.config import apply as apply_policy
from openreading.config import union_compliance
from openreading.router.compliance import DropReason
from openreading.router.registry import Registry
from openreading.router.router import RoutePlan, Router, RouterConfig
from openreading.strategies.model import DeciderLLM, StrategyConfig
from openreading.strategies.normalize import normalize_strategy
from openreading.strategies.presets import PRESET_NAMES
from openreading.strategies.trace import DropRecord
from openreading.types.descriptor import AdapterDescriptor
from openreading.types.errors import ComplianceRefused, ScopeRefused
from openreading.types.request import Compliance, OpenReadingRequest

_DURATION_UNITS = {"ms": 1, "s": 1000, "m": 60_000, "h": 3_600_000}

# The caller allow-list drop. Stage 0, not 1 or 2: it is neither the compliance filter nor the
# capability filter but a ceiling on the CALLER, applied to whatever those two already allowed.
# The code is the wire word the server answers with (403 `scope_denied`), so a trace and an
# error body name the same thing.
_SCOPE_CODE = "scope_denied"
_SCOPE_DROP = DropReason(
    stage=0,
    code=_SCOPE_CODE,
    detail="this API key is not scoped to reach this backend",
)


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
    # the post-union effective compliance (request ∪ file policy:) — feeds route facts
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
    # The caller's backend allow-list, carried so the engine can re-check every id it actually
    # dispatches. Belt and braces on purpose: the prune above bounds `auto` by SHORTENING a
    # list, and a list is not a filter — nothing downstream re-reads it, so a future node type
    # that resolves a backend some other way would reopen the hole in silence. None = unscoped.
    backend_allowlist: frozenset[str] | None = None


def compile_strategy(
    req: OpenReadingRequest,
    name: str,
    config: StrategyConfig,
    registry: Registry,
    router_config: RouterConfig | None = None,
    plain_info: dict[str, Any] | None = None,
    backend_allowlist: frozenset[str] | None = None,
) -> CompiledPlan:
    """Compile `name` for `req`. Raises ComplianceRefused when the root prunes to nothing (the
    same terminal outcome the `auto` arm gives on an empty plan). `plain_info` (from the loader)
    marks a Plain-dialect root so run_strategy can tag gate records for `explain` grouping (§9).

    `backend_allowlist` is the CALLER's ceiling on which backends this walk may reach at all —
    the server's `OPENREADING_API_KEY_SCOPES` allow-list for the presented token; None means
    unscoped and nothing here changes. It is enforced HERE, in the same pass that already prunes
    for compliance, for three reasons:

    - It is the only place that bounds an `auto` leaf. `auto` names no backend, so a reachable set
      computed by reading the config cannot bound it: `max_accuracy` is `steps: [auto, auto]` and
      names none at all. `auto` resolves at walk time against `eligible` and nothing else
      (engine._resolve_backend), so narrowing `eligible` narrows `auto` exactly, with no need to
      conservatively assume the whole registry and refuse every scoped token.
    - It runs before any adapter is constructed and before any credential is resolved, which is
      the property the allow-list is FOR: an out-of-scope backend must never get as far as having
      its vendor key read, let alone spent.
    - It keeps the allow-list the same KIND of thing the compliance filter is — a subtraction from
      the eligible set. A `strategy:<name>` id was previously exempt from the check entirely,
      which made any strategy id (including the four presets, which need no config file and so are
      available to every caller of every deployment) a universal bypass of the allow-list.

    Never a widening: `backend_allowlist` only ever removes, so no scope can readmit a backend
    compliance already dropped.
    """
    router_config = router_config or RouterConfig()
    info = plain_info.get(name) if plain_info else None
    plain_sourced = bool(info and getattr(info, "dialect", None) == "plain")

    # (0) fold the file `policy:` block in, defensively (law PF2). `openreading.api` already ran
    # this fold once before dispatch, and the fold is an intersection, so running it again over
    # the same block changes nothing. It is repeated here because `compile_strategy` and
    # `StrategyConfig` are public exports: a caller who builds the config itself and compiles it
    # reaches no other surface, and without this line their `policy:` block was advisory. The
    # first version of this change removed the fold from here on the grounds that `api` had done
    # it, which is true of every caller inside this package and of none outside it.
    req, router_config = apply_policy(req, config.policy, router_config)
    effective_compliance = union_compliance(req.compliance, None)

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

    # (1b) subtract the caller's allow-list. Order matters: this runs AFTER the router, over the
    # set the router already approved, so it can only ever shorten `eligible` — the one thing an
    # allow-list must never be able to do is put a backend back. Narrowing `eligible` is also what
    # bounds every `auto` leaf in the tree, since `auto` is resolved against nothing else.
    if backend_allowlist is not None:
        eligible = [s for s in eligible if s in backend_allowlist]

    # (2)+(3) normalize + prune every strategy (so `use:` refs resolve against pruned trees).
    # BL-168: iterate in a fixed order — `set()` iteration order is hash-seed-dependent, and it
    # reached `trace.dropped`, `orchestration["dropped"]`, and the ComplianceRefused message below,
    # so the explanation shown for *why a compliant run was refused* was not reproducible.
    all_names = sorted(set(config.strategies) | PRESET_NAMES | {name})
    trees: dict[str, dict[str, Any] | None] = {}
    dropped_records: dict[str, DropRecord] = {}
    # Drops made while pruning the REQUESTED strategy, kept apart from the rest. This loop compiles
    # every other strategy and every preset too (so `use:` refs resolve against pruned trees), and
    # their drops describe trees this request never walks — naming one of those in the refusal
    # below would point the reader at a backend their strategy does not even mention.
    root_records: dict[str, DropRecord] = {}
    for sname in all_names:
        tree = normalize_strategy(sname, config)
        records = root_records if sname == name else dropped_records
        trees[sname] = _prune_node(tree, eligible, drop_reasons, records, backend_allowlist)
    for slug, rec in root_records.items():
        dropped_records.setdefault(slug, rec)

    root = trees.get(name)
    if root is None:
        sorted_dropped = sorted(dropped_records.values(), key=lambda d: d.backend)
        dropped_list = ", ".join(f"{d.backend}:{d.code}" for d in sorted_dropped)
        # Which refusal the caller gets decides which file they go and edit, so the two causes
        # stay apart: an allow-list that removed the last runnable backend is the token's problem
        # and answers `scope_denied`; anything else is the policy's and answers the compliance
        # refusal this has always raised. A tree emptied by BOTH reports scope, because scope is
        # the narrower and later subtraction — relaxing the policy alone would not make it run.
        scope_drops = sorted(d.backend for d in root_records.values() if d.code == _SCOPE_CODE)
        if scope_drops:
            raise ScopeRefused(
                f"this API key is not scoped to reach any backend strategy {name!r} can run "
                f"(denied: {', '.join(scope_drops)})",
                backend_code=scope_drops[0],
            )
        raise ComplianceRefused(
            f"strategy {name!r} has no compliant backend for this request (dropped: {dropped_list})",
            constraint="no_compliant_backend",
        )

    config_hash = _compute_config_hash(
        root, effective_compliance, router_config, registry, plan, config.policy
    )

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
        backend_allowlist=backend_allowlist,
    )


def _compute_config_hash(
    root: dict[str, Any],
    effective_compliance: dict[str, Any],
    router_config: RouterConfig,
    registry: Registry,
    plan: RoutePlan,
    file_policy=None,
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

    `file_policy` is the `policy:` block AS WRITTEN, and it is folded in beside the effect it had
    (law PF3). The effect alone is not enough for a resume. The ledger stores the request after
    the block was folded into it, so REMOVING a constraint from the file left the stored request
    still carrying it, the recomputed effective compliance identical, and the digest unchanged:
    the resume ran under a policy the file no longer asked for and reported no mismatch. Adding a
    constraint was always caught, because it changes the effect. Hashing the source catches both
    directions. It is the parsed block rather than the file's bytes, so reindenting or reordering
    keys is not a different run.

    Folds `root`, `effective_compliance`, `router_config`, the file policy, and a descriptor
    digest per backend the router actually classified (`eligible` + `dropped` — together every registered backend, since
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
    written = file_policy.model_dump(exclude_none=True) if file_policy is not None else None
    payload = [root, effective_compliance, router_config_canonical, descriptor_digests, written]
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
    backend_allowlist: frozenset[str] | None = None,
) -> dict[str, Any] | None:
    """Return the node with dropped-backend leaves removed, or None if it collapses entirely."""
    if "backend" in node:
        slug = node["backend"]
        if slug == "auto":
            # Bounded by `eligible`, which compile_strategy has already intersected with the
            # allow-list — an `auto` leaf can only resolve to something in it.
            return node
        if backend_allowlist is not None and slug not in backend_allowlist:
            # Checked against the allow-list DIRECTLY, not against `eligible`: an id the router
            # never ranked (an unknown slug, or one dropped for an unrelated reason) must still
            # not survive as a leaf the walk would dispatch. Ahead of the compliance branch below
            # only so the drop carries the reason that is actually actionable for the caller.
            _record_drop(slug, _SCOPE_DROP, dropped_records)
            return None
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
            if (p := _prune_node(s, eligible, drop_reasons, dropped_records, backend_allowlist))
        ]
        if not kept:
            return None
        return {**node, "steps": kept}

    if "parallel" in node:
        kept = [
            p
            for b in node["parallel"]
            if (p := _prune_node(b, eligible, drop_reasons, dropped_records, backend_allowlist))
        ]
        if not kept:
            return None
        return {**node, "parallel": kept}

    if "route" in node:
        r = node["route"]
        rules = []
        for rule in r.get("rules", []):
            pruned = _prune_node(
                rule["use"], eligible, drop_reasons, dropped_records, backend_allowlist
            )
            if pruned is not None:
                rules.append({**rule, "use": pruned})
        default = _prune_node(
            r["default"], eligible, drop_reasons, dropped_records, backend_allowlist
        )
        if default is None:
            return None  # a route with no reachable default collapses (integration.md §2c)
        return {**node, "route": {**r, "rules": rules, "default": default}}

    if "decide" in node:
        d = node["decide"]
        among = [
            p
            for m in d["among"]
            if (p := _prune_node(m, eligible, drop_reasons, dropped_records, backend_allowlist))
        ]
        otherwise = _prune_node(
            d["otherwise"], eligible, drop_reasons, dropped_records, backend_allowlist
        )
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
