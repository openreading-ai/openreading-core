"""Inspect captured strategies without acquiring documents, resolving credentials or dispatching providers.

An entrypoint grant includes its configured helpers, matching execution authority in openreading.mcp_server.execution.
List exposes authorized entrypoint names only. Validate checks the selected normalized dependency closure through the shared validator.
Show and normalize are aliases for a scope-pruned structural view. Plan additionally accepts the restricted parse request metadata.
All views use the shared compiler with the operator backend ceiling; absent request metadata uses a synthetic relative reference.
No source bytes are read, so planning cannot establish source identity, format support or successful future execution.

Free-text annotations, backend options, string-valued predicates and judge payloads are omitted from the returned tree view.
The omission count covers removed fields and string array entries, not bytes, and the view is explicitly unsuitable for execution or configuration reconstruction.
Validation returns counts instead of raw messages because messages can include operator configuration or credential-shaped values.
The validation scope excludes unrelated strategies, while compilation retains execution's complete captured configuration semantics.
"""

from __future__ import annotations

from typing import Any

from openreading.adapters.registry import build_registry
from openreading.config import router_config
from openreading.mcp_server.execution import ExecutionConfig, ExecutionRefused
from openreading.strategies.normalize import normalize_strategy
from openreading.strategies.presets import PRESET_NAMES
from openreading.strategies.prune import compile_strategy
from openreading.strategies.validate import validate_config
from openreading.types.errors import ScopeRefused
from openreading.types.strategy_tool import (
    StrategyList,
    StrategyRequest,
    StrategyValidation,
    StrategyView,
)

# Payload-bearing subtrees are not required to understand which backend can run next.
_OMIT = frozenset({"with", "intent", "label", "judge"})
_NAMES = frozenset(
    {"backend", "use", "pick", "on_error", "on_quality_exhausted", "granularity", "operation"}
)


def _references(value: Any) -> set[str]:
    if isinstance(value, list):
        return set().union(*(_references(v) for v in value))
    if not isinstance(value, dict):
        return set()
    refs = {value["use"]} if isinstance(value.get("use"), str) else set()
    for key, child in value.items():
        if key not in _OMIT:
            refs.update(_references(child))
    return refs


def _closure(name, get_tree):
    trees = {}
    pending = [name]
    while pending:
        current = pending.pop()
        if current in trees:
            continue
        tree = get_tree(current)
        if tree is None:
            raise ExecutionRefused("scope_denied")
        trees[current] = tree
        pending.extend(sorted(_references(tree) - trees.keys(), reverse=True))
    return trees


def _view(value: Any) -> tuple[Any, int]:
    if isinstance(value, list):
        children = [_view(child) for child in value if not isinstance(child, str)]
        omitted = sum(isinstance(child, str) for child in value)
        return [child for child, _ in children], omitted + sum(count for _, count in children)
    if not isinstance(value, dict):
        return value, 0
    result, omitted = {}, 0
    for key, child in value.items():
        if key in _OMIT or (isinstance(child, str) and key not in _NAMES):
            omitted += 1
            continue
        result[key], count = _view(child)
        omitted += count
    return result, omitted


def inspect_strategy(authority: ExecutionConfig, arguments: dict) -> dict:
    """Return a bounded-delivery candidate after entrypoint authorization, never raw configuration.

    The tool dispatcher validates nested request metadata against the wire schema before calling this internal handler.
    For example, the schema rejects a string backend before this handler reads its identifier.
    """
    request = StrategyRequest.model_validate(arguments)
    if request.operation == "list":
        return StrategyList(strategies=sorted(authority.allowed_strategies)).wire()
    name = request.strategy
    if name not in authority.allowed_strategies:
        raise ExecutionRefused("scope_denied")
    assert name is not None
    loaded = authority.loaded
    if request.operation == "validate":
        try:
            trees = _closure(name, lambda key: normalize_strategy(key, loaded.config))
            config = loaded.config.model_copy(
                update={
                    "strategies": {
                        key: tree for key, tree in trees.items() if key not in PRESET_NAMES
                    },
                    "defaults": (
                        loaded.config.defaults.model_copy(update={"strategy": None})
                        if loaded.config.defaults
                        else None
                    ),
                }
            )
            issues = validate_config(
                config,
                raw={"strategies": trees},
                plain_info={key: info for key, info in loaded.plain_info.items() if key in trees},
            )
            errors = sum(issue.level == "error" for issue in issues)
            warnings = sum(issue.level == "warning" for issue in issues)
        except (ValueError, KeyError):
            errors, warnings = 1, 0
        return StrategyValidation(
            strategy=name,
            valid=errors == 0,
            error_count=errors,
            warning_count=warnings,
        ).wire()
    value = request.request or {
        "document": {"path": "inspection"},
        "backend": {"id": f"strategy:{name}"},
    }
    # The named entrypoint cannot be replaced by a different strategy through request metadata.
    if value.get("backend", {}).get("id") != f"strategy:{name}":
        raise ExecutionRefused("invalid_request")
    authorized = authority.authorize(value)
    try:
        compiled = compile_strategy(
            authorized.request,
            name,
            loaded.config,
            build_registry(),
            router_config(loaded.config.policy),
            plain_info=loaded.plain_info,
            backend_allowlist=authority.allowed_backends,
        )
    except ScopeRefused:
        raise ExecutionRefused("scope_denied") from None
    except (ValueError, KeyError):
        raise ExecutionRefused("invalid_configuration") from None
    trees = _closure(name, compiled.trees.__getitem__)
    views, omitted = {}, 0
    for key, tree in trees.items():
        views[key], count = _view(tree)
        omitted += count
    return StrategyView(
        strategy=name,
        tree=views[name],
        trees=views,
        dispatchable=list(authorized.backends),
        omitted=omitted,
    ).wire()
