"""World-consistency validation for a strategy config (spec.md §9).

The JSON Schema (checked in the loader) owns the grammar — bare-number durations, missing route
`default`, `decide` `among` < 2, `with:` outside its allow-list, `on_error` naming `compliance`,
thresholds out of domain, unknown predicate/fact keys. This module owns everything the schema
*cannot* express: cross-references (unknown backends and `use`/`extends` targets), cycles,
`decide` `otherwise` membership, per-backend gate bindability, parallel same-backend collisions,
and the warning set (shadowing, deadline/timeout clamps, race-gate mismatches, policy-unreachable
steps).

Location is by file + node **path** (e.g. `strategies.cheap.steps[0].escalate_if`). Line-precise
location is a documented follow-up (D-v3-8): it needs a mark-preserving YAML loader, and the node
path already satisfies the Elm-doctrine "locate" requirement.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from openreading import schemas
from openreading.adapters.registry import BUILTIN_ADAPTERS, make_adapter
from openreading.router import compliance as comp
from openreading.router.compliance import RouterConfig
from openreading.strategies.model import RawNode, StrategyConfig
from openreading.strategies.normalize import (
    DEFAULT_BUNDLE,
    NormalizeError,
    build_library,
    normalize_strategy,
)
from openreading.strategies.plain import ADVANCED_TO_PLAIN, PlainInfo
from openreading.types.enums import ChannelGrade
from openreading.types.request import Compliance

_COMPLIANCE_KEYS = (
    "require_baa",
    "no_train_on_data",
    "data_region",
    "require_local",
    "max_retention",
)

_SECRET_KEY_RE = re.compile(
    r"(?i)(api[_-]?key|secret|token|password|passwd|access[_-]?key|credential|private[_-]?key)"
)
_DURATION_RE = re.compile(r"^([0-9]+)(ms|s|m|h)$")
_DURATION_UNIT_MS = {"ms": 1, "s": 1000, "m": 60_000, "h": 3_600_000}

# gate predicate keys, grouped by what makes them (un)bindable.
_CONFIDENCE_KEYS = {"confidence_below", "page_confidence_below"}
_FIELD_CONF_KEY = "field_confidence_below"
# predicates that are ALWAYS available (Tier-1 + absence-fires) — a gate containing any of these
# can always fire, so it is never "all-unbindable".
_ALWAYS_AVAILABLE = {
    "chars_per_page_below",
    "empty_pages_over",
    "garbled",
    "garble_score_over",
    "scanned_pages_detected",
    "text_source",
    "table_sanity_below",
    "zero_blocks",
    "matches_regex",
    "sample_percent",
    "warning_code",
    "fields_required",
    "doc_type_confidence_below",  # conservative: treat as bindable (classification is fuzzy)
}


# Keys the grammar accepts and no engine code reads (model.py §6.1 `max_attempts`, §8
# `defaults.advanced`). They are refused rather than warned about, and refused rather than left
# silent, because each one is a safety limit: an author writes it to bound a run that spends money
# at a vendor, and every surface that could have contradicted them agreed instead — the schema
# accepted the key, `validate` said OK, and `describe` narrated "Makes at most 1 attempts." back.
# A kill switch that reports itself armed and is not is worse than no kill switch, so the config
# that declares one now fails to validate, and the message names the ceiling that does hold.
_UNENFORCED = {
    "max_attempts": (
        "budget.max_attempts is not enforced. No engine code reads it, so this caps nothing and"
        " the run makes as many attempts as the tree allows. Remove it; bound the run with"
        " budget.max_duration or limits.max_duration_per_doc, which are enforced"
    ),
    "circuit_breaker": (
        "defaults.advanced.circuit_breaker is not enforced. No engine code reads it, so no"
        " backend is ever benched after repeated failures and skipped(circuit_open) is never"
        " emitted. Remove it; there is no per-backend breaker in this version"
    ),
    "attempt_timeout": (
        "defaults.advanced.attempt_timeout is not enforced. No engine code reads it, so an"
        " attempt is bounded only by the enclosing deadline. Remove it; bound the run with"
        " budget.max_duration or limits.max_duration_per_doc, which are enforced"
    ),
}


@dataclass(frozen=True)
class ValidationIssue:
    level: str  # "error" | "warning"
    path: str
    message: str  # explains + suggests

    def render(self, source: str) -> str:
        return f"{self.level.upper()} {source}:{self.path}: {self.message}"


class _Ctx:
    def __init__(
        self,
        config: StrategyConfig,
        library: dict[str, RawNode],
        policy: dict[str, Any] | None,
        plain_info: dict[str, PlainInfo] | None = None,
    ) -> None:
        self.config = config
        self.library = library
        self.policy = policy
        self.plain_info = plain_info or {}
        # dialect of the strategy currently being walked (set per-strategy in validate_config); lets
        # a re-usable check phrase its message in Plain vocabulary only for Plain-dialect strategies.
        self.current_dialect: str | None = None
        self.decider_configured = config.decider is not None and config.decider.llm is not None
        self.issues: list[ValidationIssue] = []
        # compliance context for the steps-unreachable check (built once).
        self._compliance: Compliance | None = None
        self._router_config: RouterConfig | None = None
        if policy and any(k in policy for k in _COMPLIANCE_KEYS):
            self._compliance = Compliance(**{k: policy[k] for k in _COMPLIANCE_KEYS if k in policy})
            self._router_config = RouterConfig(
                allow_unverified_compliance=bool(policy.get("allow_unverified_compliance", False)),
                train_optout_confirmed=frozenset(policy.get("train_optout_confirmed", [])),
                baa_tier_confirmed=frozenset(policy.get("baa_tier_confirmed", [])),
            )

    def policy_drop(self, desc) -> str | None:
        """Return a drop reason if the effective policy would filter this backend out, else None."""
        if self._compliance is None or self._router_config is None:
            return None
        dr = comp.evaluate(self._compliance, desc, self._router_config)
        return dr.code if dr is not None else None

    def err(self, path: str, message: str) -> None:
        self.issues.append(ValidationIssue("error", path, message))

    def warn(self, path: str, message: str) -> None:
        self.issues.append(ValidationIssue("warning", path, message))


def validate_config(
    config: StrategyConfig,
    *,
    raw: dict[str, Any] | None = None,
    plain_info: dict[str, PlainInfo] | None = None,
) -> list[ValidationIssue]:
    """Return every world-consistency issue (errors + warnings) for `config`. The compliance
    context is the file's own `policy:` block, which is the only place a policy is written; `raw`
    is the pre-model dict, scanned for secret-pattern keys the schema's open sub-trees (`policy`,
    `with.*`) don't lock down.
    `plain_info` (from the loader) tags each strategy's dialect so §8 issues on a Plain body are
    phrased in Plain vocabulary, and surfaces the desugar-computed Plain warnings."""
    library: dict[str, RawNode]
    try:
        library = build_library(config)  # raises on preset-name collision
    except NormalizeError as e:
        return [ValidationIssue("error", "strategies", str(e))]

    # The effective policy is validated here, not just at run time, because this command exists to
    # find a broken config BEFORE a run does — and because `_Ctx` builds its own Compliance +
    # RouterConfig out of this same raw dict for the steps-unreachable check. An unvalidated block
    # makes that advice wrong in the direction that reads as reassurance: a typo'd key produces no
    # unreachable-step warning at all, and a quoted `allow_unverified_compliance: "false"` coerces
    # truthy and reports a `trains_on_customer_data: unverified` backend as reachable. When the
    # policy is refused the context is dropped rather than built from a dict we do not trust, so
    # the rest of the file is still checked and this error is the only thing said about the policy.
    checked_policy, policy_issue = _checked_policy(config)
    ctx = _Ctx(config, library, checked_policy, plain_info)
    if policy_issue is not None:
        ctx.issues.append(policy_issue)

    if raw is not None:
        _scan_secrets(raw, "", ctx)

    advanced = config.defaults.advanced if config.defaults else None
    if advanced is not None:
        if advanced.circuit_breaker is not None:
            ctx.err("defaults.advanced.circuit_breaker", _UNENFORCED["circuit_breaker"])
        if advanced.attempt_timeout is not None:
            ctx.err("defaults.advanced.attempt_timeout", _UNENFORCED["attempt_timeout"])

    for name in config.strategies:
        try:
            tree = normalize_strategy(name, config)  # resolves extends (cycles, unknown base)
        except NormalizeError as e:
            ctx.err(f"strategies.{name}", str(e))
            continue
        info = ctx.plain_info.get(name)
        ctx.current_dialect = info.dialect if info else None
        _check_use_cycles(name, library, (), f"strategies.{name}", ctx)
        _walk(
            tree,
            f"strategies.{name}",
            ctx,
            ancestor_deadline_ms=None,
        )
        if info is not None:  # desugar-computed Plain warnings (§8 rows needing the original body)
            for rel, message in info.warnings:
                ctx.warn(f"strategies.{name}.{rel}", message)

    return ctx.issues


# --------------------------------------------------------------------------- helpers


def _checked_policy(
    config: StrategyConfig,
) -> tuple[dict[str, Any] | None, ValidationIssue | None]:
    """The file's `policy:` block, or `(None, issue)` when it is not a well-formed policy object.

    Reported as an ERROR, not a warning: `openreading.schemas` refuses the identical block where
    the file is read, so a file this returns an issue for cannot run at all. `strategy validate`
    saying OK about a config that `strategy plan` refuses would be the worse half of the same
    defect.

    This surface checks the block against the same schema rather than trusting the loader that
    read it, because `validate_config` takes a `StrategyConfig` and a caller embedding this
    package can build one without going through a file at all.
    """
    block = dict(config.policy) if config.policy else None
    if block is None:
        return None, None
    try:
        schemas.validate_strategy_config({"version": 1, "policy": block})
    except Exception as e:  # noqa: BLE001 — jsonschema.ValidationError, or a schema error
        return None, ValidationIssue("error", "policy", getattr(e, "message", None) or str(e))
    return block, None


def _descriptor(slug: str):
    if slug not in BUILTIN_ADAPTERS:
        return None
    return make_adapter(slug).descriptor


def _parse_duration_ms(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    m = _DURATION_RE.match(value)
    if not m:
        return None
    return int(m.group(1)) * _DURATION_UNIT_MS[m.group(2)]


def _scan_secrets(obj: Any, path: str, ctx: _Ctx) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and _SECRET_KEY_RE.search(k):
                ctx.err(
                    f"{path}.{k}" if path else k,
                    f"key {k!r} looks like a secret. Strategy configs never carry credentials. "
                    "Backends resolve keys from the environment (see the openreading.credentials"
                    " docstring)",
                )
            _scan_secrets(v, f"{path}.{k}" if path else str(k), ctx)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _scan_secrets(v, f"{path}[{i}]", ctx)


# --------------------------------------------------------------------------- reference graph


def _check_use_cycles(
    name: str, library: dict[str, RawNode], seen: tuple[str, ...], path: str, ctx: _Ctx
) -> None:
    """Detect `use:` reference cycles (extends cycles are caught by normalize)."""
    if name in seen:
        ctx.err(path, f"use reference cycle: {' -> '.join([*seen, name])}")
        return
    if name not in library:
        return
    for ref in _use_targets(library[name]):
        if ref == "none":
            ctx.err(
                path,
                "strategy:none is the reserved escape hatch (force the legacy path), "
                "not a usable strategy node",
            )
        elif ref not in library:
            ctx.err(path, f"unknown strategy reference {ref!r}; define it or fix the name")
        else:
            _check_use_cycles(ref, library, (*seen, name), path, ctx)


def _use_targets(node: RawNode) -> list[str]:
    """Strategy names a node references. Distinguishes a reference node `{use: <name>}` (a string
    and no `when`) from a route rule `{when, use: <node>}`, whose `use` holds a node — a bare
    backend id there is NOT a strategy reference."""
    out: list[str] = []

    def walk(n: Any) -> None:
        if isinstance(n, str):
            if n.startswith("strategy:"):
                out.append(n[len("strategy:") :])
        elif isinstance(n, list):
            for x in n:
                walk(x)
        elif isinstance(n, dict):
            if "route" in n and isinstance(n["route"], dict):
                for rule in n["route"].get("rules", []):
                    walk(rule.get("use"))
                walk(n["route"].get("default"))
                for k, v in n.items():
                    if k != "route":
                        walk(v)
                return
            if "decide" in n and isinstance(n["decide"], dict):
                for m in n["decide"].get("among", []):
                    walk(m)
                walk(n["decide"].get("otherwise"))
                for k, v in n.items():
                    if k != "decide":
                        walk(v)
                return
            if isinstance(n.get("use"), str) and "when" not in n:
                out.append(n["use"])  # reference node (no nested nodes to recurse)
                return
            for v in n.values():
                walk(v)

    walk(node)
    return out


def _dispatchable(node: Any, library: dict[str, RawNode], seen: frozenset[str]) -> set[str]:
    """Concrete backend ids a node's subtree can dispatch (`auto` excluded; use refs resolved)."""
    if isinstance(node, str):
        if node == "auto" or node.startswith("strategy:"):
            name = node[len("strategy:") :] if node.startswith("strategy:") else None
            return _dispatchable_ref(name, library, seen) if name else set()
        return {node}
    if isinstance(node, list):
        return set().union(*(_dispatchable(x, library, seen) for x in node)) if node else set()
    if not isinstance(node, dict):
        return set()
    if "backend" in node:
        b = node["backend"]
        return set() if b == "auto" else {b}
    if "use" in node:
        return _dispatchable_ref(node["use"], library, seen)
    out: set[str] = set()
    for key in ("steps", "parallel", "among"):
        for child in node.get(key, []):
            out |= _dispatchable(child, library, seen)
    if "route" in node:
        for rule in node["route"].get("rules", []):
            out |= _dispatchable(rule.get("use"), library, seen)
        out |= _dispatchable(node["route"].get("default"), library, seen)
    if "decide" in node:
        for m in node["decide"].get("among", []):
            out |= _dispatchable(m, library, seen)
        out |= _dispatchable(node["decide"].get("otherwise"), library, seen)
    return out


def _dispatchable_ref(
    name: str | None, library: dict[str, RawNode], seen: frozenset[str]
) -> set[str]:
    if name is None or name in seen or name not in library:
        return set()
    return _dispatchable(library[name], library, seen | {name})


# --------------------------------------------------------------------------- tree walk


def _walk(
    node: dict[str, Any],
    path: str,
    ctx: _Ctx,
    *,
    ancestor_deadline_ms: int | None,
) -> None:
    # disagreement_over compares parallel branches (§11) — valid only on a pick:best parallel step.
    is_best_parallel = "parallel" in node and node.get("pick") == "best"
    for gk in ("escalate_if", "review_if"):
        g = node.get(gk)
        has_disagree = isinstance(g, dict) and any(
            k == "disagreement_over" for k, _ in _gate_predicate_keys(g)
        )
        if has_disagree and not is_best_parallel:
            ctx.err(
                f"{path}.{gk}",
                "disagreement_over compares parallel branches, so it only works on a `pick: best`"
                " parallel step",
            )

    budget = node.get("budget") or {}
    node_dur_ms = _parse_duration_ms(budget.get("max_duration"))
    if budget.get("max_attempts") is not None:
        ctx.err(f"{path}.budget.max_attempts", _UNENFORCED["max_attempts"])

    eff_deadline = _min_opt(node_dur_ms, ancestor_deadline_ms)
    child_kw = dict(ancestor_deadline_ms=eff_deadline)

    if "backend" in node:
        _check_leaf(node, path, ctx, eff_deadline)
    elif "use" in node:
        pass  # existence + cycles handled by the reference graph
    elif "steps" in node:
        _check_cascade(node, path, ctx, child_kw)
    elif "parallel" in node:
        _check_parallel(node, path, ctx, child_kw)
    elif "route" in node:
        for i, rule in enumerate(node["route"]["rules"]):
            _walk(rule["use"], f"{path}.route.rules[{i}].use", ctx, **child_kw)
        _walk(node["route"]["default"], f"{path}.route.default", ctx, **child_kw)
        _check_shadowed_rules(node["route"]["rules"], path, ctx)
    elif "decide" in node:
        _check_decide(node, path, ctx, child_kw)


def _min_opt(a: Any, b: Any) -> Any:
    vals = [x for x in (a, b) if x is not None]
    return min(vals) if vals else None


def _check_leaf(node: dict[str, Any], path: str, ctx: _Ctx, eff_deadline_ms: Any) -> None:
    slug = node["backend"]
    if slug == "auto":
        return
    desc = _descriptor(slug)
    if desc is None:
        ctx.err(
            f"{path}.backend",
            f"unknown backend {slug!r}; known: {', '.join(sorted(BUILTIN_ADAPTERS))} (or 'auto')",
        )
        return
    # leaf timeout larger than the effective deadline (clamped)
    t_ms = _parse_duration_ms(node.get("timeout"))
    if t_ms is not None and eff_deadline_ms is not None and t_ms > eff_deadline_ms:
        ctx.warn(
            f"{path}.timeout",
            f"per-attempt timeout {node['timeout']} exceeds the effective deadline and will be "
            "clamped",
        )
    # steps unreachable under the file's own `policy:` block
    drop = ctx.policy_drop(desc)
    if drop is not None:
        ctx.warn(
            f"{path}.backend",
            f"{slug!r} is filtered out by the policy ({drop}). This step can never run in that "
            "compliance context. Remove it or relax the policy",
        )
    # step-position gate bindability + `missing:` on a backend that cannot produce typed fields
    for gate_key in ("escalate_if", "review_if"):
        gate = node.get(gate_key)
        if isinstance(gate, dict):
            _check_gate_bindable(gate, desc, slug, f"{path}.{gate_key}", ctx)
            keys = {k for k, _ in _gate_predicate_keys(gate)}
            if (
                "fields_required" in keys
                and desc.output.channels.typed_fields == ChannelGrade.IMPOSSIBLE
            ):
                word = "missing" if ctx.current_dialect == "plain" else "fields_required"
                ctx.warn(
                    f"{path}.{gate_key}",
                    f"{word}: {slug!r} cannot produce typed fields, so this criterion fires on "
                    "every document. This rung will always escalate",
                )
    if "review_if" in node and not ctx.decider_configured:
        ctx.warn(
            f"{path}.review_if",
            "review_if is set but no decider is configured; it will "
            "resolve to review_default. Configure `decider:` + OPENREADING_LLM_DECIDER to use an LLM",
        )


def _gate_predicate_keys(gate: dict[str, Any]) -> list[tuple[str, Any]]:
    """Flatten a gate's leaf predicates, recursing through any_of/all_of to any depth.

    Order-free and structure-free by design: callers that need the boolean structure (whether the
    gate can fire at all, and which leaf is the dead one) use `_gate_can_fire` / `_unbindable_leaves`
    instead, because a flat leaf list cannot tell an OR from an AND.
    """
    out: list[tuple[str, Any]] = []
    for k, v in gate.items():
        if k in ("any_of", "all_of"):
            for sub in v:
                out.extend(_gate_predicate_keys(sub))
        else:
            out.append((k, v))
    return out


def _predicate_binds(key: str, value: Any, desc) -> bool:
    """True if a predicate could ever fire on this backend."""
    if key in _ALWAYS_AVAILABLE:
        return True
    # a wrapper with on_missing: escalate fires on absence -> always bindable
    if isinstance(value, dict) and value.get("on_missing") == "escalate":
        return True
    if key in _CONFIDENCE_KEYS:
        return desc.output.channels.block_confidence != ChannelGrade.IMPOSSIBLE
    if key == _FIELD_CONF_KEY:
        if isinstance(value, dict) and value.get("on_missing") == "escalate":
            return True
        return desc.output.channels.typed_fields != ChannelGrade.IMPOSSIBLE
    return True  # unknown / conservative: assume bindable


def _gate_can_fire(gate: dict[str, Any], desc) -> bool:
    """Whether this gate could ever fire on `desc`, in the boolean structure `evaluate_gate`
    actually evaluates: a gate map and `any_of` OR their members, `all_of` ANDs them and an empty
    `all_of` never fires.

    Flattening the tree to a leaf list and asking "does any leaf bind?" gets `all_of` backwards.
    One conjunct that can never fire kills the whole conjunction, because `signals.evaluate_gate`
    requires `all(s.fired for s in sub)` and a predicate whose signal the backend cannot produce is
    traced `signal_unavailable` and never fires. Such a gate is as dead as a lone `confidence_below`
    on a confidence-less backend, and a flatten-and-count check reports it clean.
    """
    fires = False
    for key, value in gate.items():
        if key == "any_of":
            fires = fires or any(_gate_can_fire(sub, desc) for sub in value)
        elif key == "all_of":
            fires = fires or (bool(value) and all(_gate_can_fire(sub, desc) for sub in value))
        else:
            fires = fires or _predicate_binds(key, value, desc)
    return fires


def _unbindable_leaves(gate: dict[str, Any], desc, path: str) -> list[tuple[str, str]]:
    """(node path, predicate key) for every leaf predicate that can never fire on `desc`.

    Located at the leaf's own path rather than the gate's, because "somewhere under this gate one
    predicate is dead" is not a locatable message once a gate nests.
    """
    out: list[tuple[str, str]] = []
    for key, value in gate.items():
        if key in ("any_of", "all_of"):
            for i, sub in enumerate(value):
                out.extend(_unbindable_leaves(sub, desc, f"{path}.{key}[{i}]"))
        elif not _predicate_binds(key, value, desc):
            out.append((f"{path}.{key}", key))
    return out


def _check_gate_bindable(gate: dict[str, Any], desc, slug: str, path: str, ctx: _Ctx) -> None:
    if not _gate_predicate_keys(gate):
        return
    dead = _unbindable_leaves(gate, desc, path)
    if not dead:
        return
    keys = sorted({k for _, k in dead})
    if not _gate_can_fire(gate, desc):
        if ctx.current_dialect == "plain":  # re-phrase in the four-word vocabulary (§8)
            words = ", ".join(sorted({ADVANCED_TO_PLAIN.get(k, k) for k in keys}))
            ctx.err(
                path,
                f"the {words} check can never fire on {slug!r}, which reports no confidence. Add "
                "a criterion that works everywhere, e.g. `looks_bad: true`",
            )
            return
        named = ", ".join(keys)
        if all(not _predicate_binds(k, v, desc) for k, v in _gate_predicate_keys(gate)):
            ctx.err(
                path,
                f"gate can never fire on {slug!r}: none of its predicates ({named}) bind, "
                f"because {slug} emits no confidence. Add an always-available signal such as "
                "chars_per_page_below or garbled, or set on_missing: escalate",
            )
        else:
            # every live branch runs through an all_of that one dead conjunct closes.
            ctx.err(
                path,
                f"gate can never fire on {slug!r}: {named} never binds ({slug} emits no "
                "confidence) and an all_of fires only when every member fires, so the whole "
                f"conjunction is dead. Move {named} out of the all_of (a gate map ORs its keys) "
                "or set on_missing: escalate",
            )
        return
    # The gate still fires on its other members, so a dead leaf is dead weight rather than a dead
    # gate — a warning at the leaf, not an error. The exception is the shipped `default` bundle,
    # whose `confidence_below` is a documented Tier-2 bonus "silently inapplicable on
    # confidence-less backends" (normalize.DEFAULT_BUNDLE): designed degradation, not an oversight.
    if gate == DEFAULT_BUNDLE:
        return
    for leaf_path, key in dead:
        if ctx.current_dialect == "plain":
            word = ADVANCED_TO_PLAIN.get(key, key)
            ctx.warn(
                leaf_path,
                f"the {word} check can never fire on {slug!r}, which reports no confidence. The "
                "rest of the gate carries it, so this word does nothing here. Drop it or move it "
                "to a rung whose backend reports confidence",
            )
        else:
            ctx.warn(
                leaf_path,
                f"{key} can never fire on {slug!r}, which emits no confidence. The gate still "
                "fires on its other predicates, so this one is dead weight. Remove it, set "
                "on_missing: escalate, or move it to a rung whose backend reports confidence",
            )


def _check_cascade(node: dict[str, Any], path: str, ctx: _Ctx, child_kw: dict) -> None:
    steps = node["steps"]
    last = len(steps) - 1
    paged = node.get("granularity") == "page"
    for i, step in enumerate(steps):
        spath = f"{path}.steps[{i}]"
        # granularity:page — a non-first rung whose backend lacks native page-range selection runs
        # document granularity for that rung (spec §2.7); warn so the author knows the escalation
        # re-parses the WHOLE doc, not just the failing pages.
        if paged and i > 0 and isinstance(step, dict) and isinstance(step.get("backend"), str):
            desc = _descriptor(step["backend"])
            if desc is not None and not getattr(desc.capabilities, "page_range_selection", False):
                ctx.warn(
                    spath,
                    f"granularity: page but backend {step['backend']!r} lacks native page-range "
                    "selection. This rung runs document granularity and re-parses the whole doc",
                )
        # a raced final rung whose branches carry their own gates: those branch gates won't run
        if i == last and step.get("pick") == "fastest":
            for j, br in enumerate(step.get("parallel", [])):
                if isinstance(br, dict) and ("escalate_if" in br or "review_if" in br):
                    ctx.warn(
                        f"{spath}.parallel[{j}]",
                        "branch gates are not evaluated under "
                        "pick: fastest; gate the enclosing cascade step instead",
                    )
        _walk(step, spath, ctx, **child_kw)


def _check_parallel(node: dict[str, Any], path: str, ctx: _Ctx, child_kw: dict) -> None:
    branches = node["parallel"]
    # sibling subtrees that can dispatch the same backend
    sets = [_dispatchable(br, ctx.library, frozenset()) for br in branches]
    for a in range(len(sets)):
        for b in range(a + 1, len(sets)):
            overlap = sets[a] & sets[b]
            if overlap:
                ctx.err(
                    f"{path}.parallel",
                    f"branches {a} and {b} can both dispatch {sorted(overlap)}. Duplicate "
                    "concurrent dispatch is a no-op, so remove one",
                )

    # judged with >3 candidates: the pairwise sweep is 2·(n−1) LLM calls — slow, not cheap
    if (
        node.get("judge")
        and len([b for b in branches if not (isinstance(b, dict) and b.get("shadow"))]) > 3
    ):
        ctx.warn(
            f"{path}",
            "judged comparison over >3 candidates makes many pairwise LLM calls "
            "(single-elimination). It will be slow, so consider fewer branches",
        )

    for i, br in enumerate(branches):
        _walk(br, f"{path}.parallel[{i}]", ctx, **child_kw)


def _check_decide(node: dict[str, Any], path: str, ctx: _Ctx, child_kw: dict) -> None:
    d = node["decide"]
    among = d.get("among", [])
    otherwise = d.get("otherwise")
    if otherwise is not None and otherwise not in among:
        ctx.err(
            f"{path}.decide.otherwise",
            "otherwise must be one of the among candidates so the "
            "engine default is always a choice the decider could also make",
        )
    if not ctx.decider_configured:
        ctx.warn(
            f"{path}.decide",
            "decide node with no decider configured; it will always take "
            "otherwise. Configure `decider:` + OPENREADING_LLM_DECIDER to let an LLM choose",
        )
    for i, m in enumerate(among):
        _walk(m, f"{path}.decide.among[{i}]", ctx, **child_kw)
    if otherwise is not None:
        _walk(otherwise, f"{path}.decide.otherwise", ctx, **child_kw)


def _check_shadowed_rules(rules: list[dict[str, Any]], path: str, ctx: _Ctx) -> None:
    seen: list[Any] = []
    for i, rule in enumerate(rules):
        when = rule.get("when")
        if any(when == prev for prev in seen):
            ctx.warn(
                f"{path}.route.rules[{i}]",
                "this rule's `when` duplicates an earlier rule "
                "and can never fire (first match wins); remove or reorder it",
            )
        seen.append(when)
