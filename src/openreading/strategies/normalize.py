"""Shorthand -> canonical longhand normalization (spec.md §7) + `extends` resolution + the
built-in preset library.

The canonical longhand is the JSON-serialization target for UIs and the LLM generation contract,
and the input the engine walks. Normalization is a **fixed point**: `normalize(shorthand)` equals
the longhand, and `normalize(longhand)` equals itself (proven by test).

Canonical form (what this module emits):
- Every node is a map — no bare strings or lists.
- leaf: `{backend: <id>}` (+ optional operation/version/timeout/with/label/intent/budget/on_error)
- reference: `{use: <name>}` (+ optional label/intent/budget/on_error) — `use` refs stay by NAME
  (resolved at execution, not here); only `extends` is resolved at load (spec §7 rule 8).
- cascade: `{steps: [...], granularity?, on_quality_exhausted?}` — a cascade never keeps a
  top-level `escalate_if` in canonical form; gates live on the steps (see D-v3-7).
- parallel: `{parallel: [...], pick, ...}`; route: `{route: {rules, default}}`;
  decide: `{decide: {among, otherwise}}`.

D-v3-7 (gate distribution): a cascade-level `escalate_if` (`default` -> the §4.4 bundle; a map;
or `off`/absent -> nothing) is copied onto each **non-final** step that is a leaf, a reference,
or a parallel/route/decide node and lacks its own step-position gate, then the cascade-level key
is dropped. A **nested-cascade step is skipped** by distribution: its `escalate_if` is that inner
cascade's own level gate, not a step-position gate, so distributing onto it would be ambiguous and
break idempotency. This narrows spec §7 rule 5's "each step" to "each non-nested-cascade step".

BL-35 (decide candidate identity): a decide node's `among:` members are named by
`candidate_label` and the engine dispatches on that name alone. Normalization is where the names
become fixed, so it is also where a duplicate is caught — an ambiguous action space is a
NormalizeError, never a silently unreachable candidate.
"""

from __future__ import annotations

from typing import Any

from openreading.strategies.loader import STRATEGY_PREFIX
from openreading.strategies.model import RawNode, StrategyConfig
from openreading.strategies.presets import PRESET_NAMES, PRESETS

# spec.md §4.4 — the `default` escalate_if bundle. Tier-1 members carry the load; confidence_below
# is a Tier-2 bonus that is silently inapplicable on confidence-less backends.
DEFAULT_BUNDLE: dict[str, Any] = {
    "scanned_pages_detected": True,
    "garbled": True,
    "empty_pages_over": 0.2,
    "confidence_below": 0.6,
}

# keys that, on a cascade STEP dict, are step-position gates (not node-defining keys).
_STEP_GATE_KEYS = ("escalate_if", "review_if", "review_default")
# keys that, on a parallel BRANCH dict, are branch-position modifiers.
_BRANCH_KEYS = ("start_after", "shadow")
# node-defining discriminators.
_NODE_KEYS = ("backend", "steps", "parallel", "route", "decide", "use")
# composite kinds, in the order `candidate_label` falls back through them.
_COMPOSITE_KEYS = ("steps", "parallel", "route", "decide")
# the engine appends this name to every decide node's candidate list as the fallthrough action
# (engine._eval_decide); it is reserved — an `among:` member that claims it is never dispatched.
_FALLTHROUGH = "otherwise"


class NormalizeError(ValueError):
    """A strategy that cannot be normalized: an unknown `extends`/preset base, an `extends`
    cycle, or a decide node whose candidates do not have distinct names. (Grammar errors are
    caught earlier by schema validation in the loader.)"""


# --------------------------------------------------------------------------- library + extends


def build_library(config: StrategyConfig | None) -> dict[str, RawNode]:
    """Presets overlaid with the config's user strategies. A user strategy named after a preset
    is a load-time error (spec §2.8)."""
    library: dict[str, RawNode] = dict(PRESETS)
    if config is not None:
        for name, node in config.strategies.items():
            if name in PRESET_NAMES:
                raise NormalizeError(
                    f"strategy {name!r} collides with a built-in preset. Rename it, then "
                    f"run `openreading strategy show {name}` to copy the preset body "
                    f"into your file"
                )
            library[name] = node
    return library


def _resolve_extends(name: str, library: dict[str, RawNode], seen: tuple[str, ...]) -> RawNode:
    """Return `name`'s raw node with `extends` chains flattened (shallow, field-level, extending
    wins). Cycles and unknown bases raise NormalizeError."""
    if name in seen:
        cycle = " -> ".join([*seen, name])
        raise NormalizeError(f"extends cycle: {cycle}")
    if name not in library:
        known = ", ".join(sorted(library)) or "(none)"
        raise NormalizeError(f"unknown strategy/preset {name!r}; known: {known}")
    node = library[name]
    if not (isinstance(node, dict) and "extends" in node):
        return node
    base_name = node["extends"]
    base = _resolve_extends(base_name, library, (*seen, name))
    base_dict = _as_dict_form(base)
    merged = dict(base_dict)
    for k, v in node.items():
        if k != "extends":
            merged[k] = v  # extending strategy's clause replaces the base's wholesale
    return merged


def _as_dict_form(node: RawNode) -> dict[str, Any]:
    """Minimal dict form of a shorthand, for merging an `extends` base."""
    if isinstance(node, str):
        return _string_node(node)
    if isinstance(node, list):
        return {"steps": list(node)}
    return dict(node)


# --------------------------------------------------------------------------- node normalization


def _string_node(s: str) -> dict[str, Any]:
    if s.startswith(STRATEGY_PREFIX):
        return {"use": s[len(STRATEGY_PREFIX) :]}
    return {"backend": s}


def _node_kind(node: dict[str, Any]) -> str:
    for k in _NODE_KEYS:
        if k in node:
            return k
    raise NormalizeError(f"node has no defining key ({', '.join(_NODE_KEYS)}): {node!r}")


def _sub(path: str, segment: str) -> str:
    return f"{path}.{segment}" if path else segment


def candidate_label(node: Any) -> str:
    """The name a decide candidate answers to: the `use:` ref, the leaf backend, an explicit
    `label:`, else the node kind. This IS the decider's action space (the tool enum) and the key
    the engine dispatches on (engine._eval_decide) — so it must be unique within one `among:`."""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        if "use" in node:
            return node["use"]
        if "backend" in node:
            return node["backend"]
        if node.get("label"):
            return node["label"]
        for kind in _COMPOSITE_KEYS:
            if kind in node:
                return kind
    return "candidate"


def normalize_node(raw: RawNode, path: str = "") -> dict[str, Any]:
    """Normalize a standalone node (a named strategy body, a route target, a decide member) to
    canonical longhand. Does NOT resolve `extends` (that is strategy-level, done before this).
    `path` locates the node for error messages (validate.py's convention, e.g.
    `strategies.invoices.steps[1]`); it never affects the output."""
    if isinstance(raw, str):
        return _string_node(raw)
    if isinstance(raw, list):
        return _normalize_cascade({"steps": list(raw)}, path)
    if not isinstance(raw, dict):
        raise NormalizeError(f"not a node: {raw!r}")

    kind = _node_kind(raw)
    if kind == "backend":
        return _normalize_leaf(raw)
    if kind == "use":
        return _normalize_reference(raw)
    if kind == "steps":
        return _normalize_cascade(raw, path)
    if kind == "parallel":
        return _normalize_parallel(raw, path)
    if kind == "route":
        return _normalize_route(raw, path)
    return _normalize_decide(raw, path)


def _passthrough(raw: dict[str, Any], out: dict[str, Any], keys: tuple[str, ...]) -> None:
    for k in keys:
        if k in raw:
            out[k] = raw[k]


def _normalize_leaf(raw: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"backend": raw["backend"]}
    _passthrough(
        raw,
        out,
        ("operation", "version", "timeout", "with", "label", "intent", "budget", "on_error"),
    )
    return out


def _normalize_reference(raw: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"use": raw["use"]}
    _passthrough(raw, out, ("label", "intent", "budget", "on_error"))
    return out


def _normalize_cascade(raw: dict[str, Any], path: str = "") -> dict[str, Any]:
    raw_steps = raw["steps"]
    gate = _cascade_gate(raw.get("escalate_if"))  # dict (expanded) | None
    steps = [_normalize_step(s, _sub(path, f"steps[{i}]")) for i, s in enumerate(raw_steps)]
    if gate is not None:
        last = len(steps) - 1
        for i, step in enumerate(steps):
            if i == last:
                continue  # never gate the final step (nothing to escalate to)
            if _is_nested_cascade(step):
                continue  # a nested cascade owns its escalation (D-v3-7)
            if "escalate_if" not in step:
                step["escalate_if"] = dict(gate)
    out: dict[str, Any] = {"steps": steps}
    _passthrough(
        raw, out, ("granularity", "on_quality_exhausted", "label", "intent", "budget", "on_error")
    )
    return out


def _cascade_gate(value: Any) -> dict[str, Any] | None:
    """Resolve a cascade-level escalate_if to the concrete gate map to distribute, or None.
    `off` / boolean `false` (YAML coerces an unquoted `off`/`no`/`false` to a boolean) → no gates."""
    if value is None or value is False or value == "off":
        return None
    if value == "default":
        return dict(DEFAULT_BUNDLE)
    if isinstance(value, dict):
        return value
    raise NormalizeError(f"invalid cascade escalate_if: {value!r}")


def _normalize_step(raw: RawNode, path: str = "") -> dict[str, Any]:
    """A cascade step: a node, plus optional step-position gates (escalate_if/review_if/
    review_default) when the node is NOT a nested cascade."""
    if isinstance(raw, (str, list)):
        return normalize_node(raw, path)
    if not isinstance(raw, dict):
        raise NormalizeError(f"not a step: {raw!r}")
    kind = _node_kind(raw)
    if kind == "steps":
        # nested cascade: its escalate_if is the inner level gate — normalize as a whole node.
        return _normalize_cascade(raw, path)
    # leaf / reference / parallel / route / decide step: split off step-position gates.
    node_part = {k: v for k, v in raw.items() if k not in _STEP_GATE_KEYS}
    node = normalize_node(node_part, path)
    for gk in _STEP_GATE_KEYS:
        if gk in raw:
            node[gk] = _expand_step_gate(gk, raw[gk])
    return node


def _expand_step_gate(key: str, value: Any) -> Any:
    if key == "escalate_if" and value == "default":
        return dict(DEFAULT_BUNDLE)
    return value


def _is_nested_cascade(step: dict[str, Any]) -> bool:
    return "steps" in step


def _normalize_parallel(raw: dict[str, Any], path: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {
        "parallel": [
            _normalize_branch(b, _sub(path, f"parallel[{i}]"))
            for i, b in enumerate(raw["parallel"])
        ],
        "pick": raw["pick"],
    }
    _passthrough(
        raw, out, ("judge", "on_win", "require", "merge", "label", "intent", "budget", "on_error")
    )
    return out


def _normalize_branch(raw: RawNode, path: str = "") -> dict[str, Any]:
    """A parallel branch: a node, plus optional start_after/shadow."""
    if isinstance(raw, (str, list)):
        return normalize_node(raw, path)
    if not isinstance(raw, dict):
        raise NormalizeError(f"not a branch: {raw!r}")
    node_part = {k: v for k, v in raw.items() if k not in _BRANCH_KEYS}
    node = normalize_node(node_part, path)
    for bk in _BRANCH_KEYS:
        if bk in raw:
            node[bk] = raw[bk]
    return node


def _normalize_route(raw: dict[str, Any], path: str = "") -> dict[str, Any]:
    r = raw["route"]
    rpath = _sub(path, "route")
    rules = [
        {"when": rule["when"], "use": normalize_node(rule["use"], f"{rpath}.rules[{i}].use")}
        for i, rule in enumerate(r.get("rules", []))
    ]
    default = normalize_node(r["default"], f"{rpath}.default")
    out: dict[str, Any] = {"route": {"rules": rules, "default": default}}
    _passthrough(raw, out, ("label", "intent", "budget", "on_error"))
    return out


def _normalize_decide(raw: dict[str, Any], path: str = "") -> dict[str, Any]:
    d = raw["decide"]
    dpath = _sub(path, "decide")
    among = [normalize_node(m, f"{dpath}.among[{i}]") for i, m in enumerate(d["among"])]
    _check_candidate_labels(among, dpath)
    out: dict[str, Any] = {
        "decide": {
            "among": among,
            "otherwise": normalize_node(d["otherwise"], f"{dpath}.otherwise"),
        }
    }
    _passthrough(raw, out, ("label", "intent", "budget", "on_error"))
    return out


def _check_candidate_labels(among: list[dict[str, Any]], dpath: str) -> None:
    """Fail closed on an ambiguous decide action space (BL-35). The decider's tool enum and the
    engine's dispatch are both keyed on `candidate_label`, and both are satisfied by the FIRST
    member carrying a name — a duplicate silently makes every later twin unreachable, with a trace
    that looks entirely normal. Two anonymous same-kind sub-trees are the easy way to hit it."""
    seen: dict[str, int] = {}
    for i, member in enumerate(among):
        label = candidate_label(member)
        if label == _FALLTHROUGH:
            raise NormalizeError(
                f"{dpath}: among[{i}] resolves to the candidate label {label!r}, which is the "
                f"engine's own fallthrough action, so it could never be dispatched. Rename it"
            )
        if label in seen:
            raise NormalizeError(
                f"{dpath}: among[{seen[label]}] and among[{i}] both resolve to the candidate "
                f"label {label!r}; the decider dispatches by label, so the second is unreachable. "
                f"Give a composite candidate an explicit `label:`, or split duplicate leaves into "
                f"separately named strategies"
            )
        seen[label] = i


# --------------------------------------------------------------------------- strategy / config


def normalize_strategy(name: str, config: StrategyConfig | None) -> dict[str, Any]:
    """Resolve `extends` for `name` against the preset+user library, then normalize to canonical
    longhand. Raises NormalizeError on unknown base, cycle, or preset-name collision."""
    library = build_library(config)
    if name not in library:
        known = ", ".join(sorted(library)) or "(none)"
        raise NormalizeError(f"unknown strategy {name!r}; known: {known}")
    resolved = _resolve_extends(name, library, ())
    return normalize_node(resolved, f"strategies.{name}")


def normalize_config(config: StrategyConfig) -> dict[str, dict[str, Any]]:
    """Normalize every named user strategy (extends resolved) to canonical longhand."""
    return {name: normalize_strategy(name, config) for name in config.strategies}
