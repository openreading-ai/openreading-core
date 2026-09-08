"""Turn one loaded `openreading.yaml` into the `StrategyConfig` the engine walks.

Discovery, reading, schema validation and the `policy:` block belong to `openreading.config`,
which every surface calls whether or not a strategy is involved. This module owns what is left:
the Plain-dialect desugar, the model build, and the provenance a trace carries. `load_config()`
is the one-call spelling for callers that want both halves, and it returns `None` when
`openreading.config.load()` finds no file — the caller then takes exactly the legacy code path
and the strategy layer never engages. A found-but-broken file raises `ConfigError`, re-exported
here so existing imports hold.

Desugar (`openreading.strategies.plain`) rewrites the Plain dialect to the canonical five-node
longhand, so everything downstream sees one tree. Errors locate by node path
(`strategies.cheap.steps[0].escalate_if`), not line number: `yaml.safe_load` discards source
marks, and a mark-preserving loader was judged not worth it (D-v3-8).

`LoadedConfig` carries provenance: `path` (`None` for a dict passed to `config.load`),
`source_hash` (sha256 of the file text, distinct from the normalized-tree
`config_hash` the engine stamps on `orchestration.config_hash`), the schema-valid `raw` dict
(validate scans it for secrets), and `plain_info` (per-strategy Plain classification for
`explain`).

Wire prefix (D-v3-2): `backend.id: "strategy:<name>"` is a documented reserved prefix of the
free-string `backend.id` — no request-schema bump. `strip_strategy_prefix` is the recognizer
the server (`openreading.server.app`) uses for `/v1/parse`, `/v1/jobs` and the API-key scope
check. `openreading.api` deliberately does NOT import it: both of its `make_adapter` call sites
(`build_request`, `run_request`) guard with an inlined `_STRATEGY_PREFIX` / `startswith` check
before touching the registry — a bare registry lookup would `KeyError` and surface as a
misleading 404 `unknown_backend` — so that a plain named-backend run never imports the strategy
package (guardrail T10). `strategy:none` is the reserved escape hatch (force the legacy path)
and returns the literal `'none'`. An unknown name on the wire is `UnknownStrategyError`
(`backend_code: unknown_strategy`), raised by `api._run_strategy_request` against the loaded
strategies ∪ built-in presets, which the surfaces map to HTTP 400 (a malformed ask against this
deployment's config, not a missing resource) and CLI exit 2 (the same caller-error class as an
unknown `--backend`). `resolve_strategy` is the library helper that maps a name to its raw node
body, for callers embedding this package. The surfaces do not use it, so its `ConfigError` never
becomes the wire's `unknown_strategy`. The CLI's `--strategy` (on `parse` only) is a separate flag
rather than an overload of `--backend` because `--backend` is an argparse `choices=` list that
would reject the prefix at parse time.

Execution semantics of a loaded tree live in `openreading.strategies.engine`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from openreading.config import ConfigError, LoadedFile, load, parse
from openreading.strategies.model import RawNode, StrategyConfig

STRATEGY_PREFIX = "strategy:"

# Re-exported so `from openreading.strategies.loader import ConfigError` keeps working; the class
# itself lives in `openreading.config`, which raises it.
__all__ = [
    "STRATEGY_PREFIX",
    "ConfigError",
    "LoadedConfig",
    "build_config",
    "load_config",
    "parse_config",
    "parse_config_raw",
    "resolve_strategy",
    "strip_strategy_prefix",
]


@dataclass(frozen=True)
class LoadedConfig:
    """A successfully loaded config plus provenance for the trace/debug surfaces."""

    config: StrategyConfig
    path: Path | None  # None when the caller passed a dict rather than a path
    source_hash: str  # sha256 of the file text; the NORMALIZED-tree config_hash arrives in 11.2
    raw: dict  # the schema-valid dict, before model construction (validate scans it for secrets)
    # per-strategy Plain-dialect classification + gate provenance (internal/design/simple-strategies.md
    # §9). Empty for files with no `strategies:`; every strategy is classified otherwise.
    plain_info: dict = field(default_factory=dict)


def parse_config_raw(text: str, *, source: str = "<string>") -> tuple[StrategyConfig, dict, dict]:
    """Parse, schema-validate, and desugar config text; return (model, raw dict, plain_info).

    The raw dict is post-desugar canonical longhand (Plain map bodies rewritten to the five-node
    grammar — internal/design/simple-strategies.md §7), so everything downstream sees one tree.
    Raises ConfigError on any parse, grammar, or desugar failure, locating `source`.
    """
    return _desugar_and_build(parse(text, source=source), source=source)


def _desugar_and_build(raw: dict, *, source: str) -> tuple[StrategyConfig, dict, dict]:
    """Desugar one schema-valid mapping to canonical longhand and build the model from it."""
    from openreading.strategies.plain import desugar_config  # lazy: avoids loader<->plain cycle

    try:
        raw, plain_info = desugar_config(raw)  # Plain -> canonical longhand (or a strict no-op)
    except ConfigError as exc:  # a desugar-time §8 violation; already located
        raise ConfigError(f"{source}: {exc}") from exc

    return StrategyConfig.model_validate(raw), raw, plain_info


def parse_config(text: str, *, source: str = "<string>") -> StrategyConfig:
    """Parse + schema-validate config text into a StrategyConfig (raw + plain_info discarded)."""
    return parse_config_raw(text, source=source)[0]


def load_config(
    explicit: str | os.PathLike[str] | dict | None = None, *, allow_cwd: bool = True
) -> LoadedConfig | None:
    """Discover + load the strategy config, or None when no file is found (the legacy path).
    A found-but-broken file raises ConfigError — an explicitly requested config that cannot load
    is an error, never a silent fall-through to today's behavior."""
    return build_config(load(explicit, allow_cwd=allow_cwd))


def build_config(loaded: LoadedFile | None) -> LoadedConfig | None:
    """Build the strategy half of an already-loaded file, or None when there was no file.

    `openreading.api` calls `openreading.config.load` once, on every path, and reaches here only
    when a strategy is actually engaged. Splitting the read from the build is what lets a
    named-backend run see the file's `policy:` block without importing this package (law P6).
    """
    if loaded is None:
        return None
    source = str(loaded.path) if loaded.path is not None else "<dict>"
    config, raw, plain_info = _desugar_and_build(loaded.raw, source=source)
    return LoadedConfig(
        config=config,
        path=loaded.path,
        source_hash=loaded.source_hash,
        raw=raw,
        plain_info=plain_info,
    )


def strip_strategy_prefix(backend_id: str | None) -> str | None:
    """Return the strategy name if `backend_id` is a `strategy:<name>` reference, else None.
    `strategy:none` is the reserved escape hatch and returns the literal 'none'.

    `None` in means the caller named no backend, which is not a strategy reference either."""
    if backend_id is None:
        return None
    if backend_id.startswith(STRATEGY_PREFIX):
        return backend_id[len(STRATEGY_PREFIX) :]
    return None


def resolve_strategy(config: StrategyConfig, name: str) -> RawNode:
    """Look up a named strategy, raising `ConfigError` when the name is absent. A library
    helper for callers embedding this package. The wire's `unknown_strategy` (HTTP 400, CLI
    exit 2) comes from `api.UnknownStrategyError` instead."""
    if name not in config.strategies:
        known = ", ".join(config.strategy_names()) or "(none)"
        raise ConfigError(f"unknown strategy {name!r}; defined strategies: {known}")
    return config.strategies[name]
