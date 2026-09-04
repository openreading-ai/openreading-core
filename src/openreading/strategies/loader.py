"""Discover, parse, and validate an `openreading.yaml` strategy file.

Discovery order (first hit wins; sources are NEVER merged):
  1. an explicit path — CLI `--config PATH`, Python `openreading.run(config=...)`;
  2. the `OPENREADING_CONFIG` environment variable;
  3. `./openreading.yaml` (or `./openreading.yml`) in the working directory — CLI and Python
     API only.

`discover()` takes `allow_cwd`; the server (`openreading serve`) passes `False` and MUST NOT sniff
its working directory — a stray file could otherwise change a long-running service — which makes
`OPENREADING_CONFIG` the only non-flag way to load a strategy file under the server (same posture
as the env-only `RouterConfig`). When nothing is found anywhere, `load_config()` returns `None`,
the caller takes exactly the legacy code path, and the strategy layer never engages ("no file ⇒
no change"; `openreading.api` imports this module lazily and only for `auto` / `strategy:`
requests, so a named-backend run never imports the strategy package at all). A found-but-broken
file — an explicit path that does not exist, an env var that does not point at a file, malformed
YAML, an empty or non-mapping document, a grammar violation, a Plain-dialect desugar error —
raises `ConfigError`: an explicitly requested config that cannot load is an error, never a silent
fall-through to the old behaviour.

Environment:
- `OPENREADING_CONFIG` — explicit path to the strategy file (precedence step 2). Unset — or
  set to the EMPTY string, which `discover()` treats as unset (`if env:`): fall through to the
  cwd probe where allowed, else no config and no strategy layer. Set non-empty but not a file:
  `ConfigError`.

Parsing (`parse_config_raw`) uses `yaml.safe_load` only (D-v3-1), so a config file can never
construct arbitrary Python objects, and a `!!python/object/apply` tag raises `ConfigError`.
`pyyaml` (MIT) is imported lazily, so the no-config path never pays for the import.
The raw mapping is then validated against the vendored `strategy-config` JSON Schema
(`openreading.schemas`, the strict grammar authority — it also rejects retry knobs, which the
strategy layer must never grow), desugared from the Plain dialect to the canonical five-node
longhand (`openreading.strategies.plain`) so everything downstream sees one tree, and built into
a `StrategyConfig` (`openreading.strategies.model`). Errors locate by node path
(`strategies.cheap.steps[0].escalate_if`), not line number: `safe_load` discards source marks,
and a mark-preserving loader was judged not worth it (D-v3-8).

`LoadedConfig` carries provenance: `path`, `source_hash` (sha256 of the file TEXT — distinct
from the compliance-aware normalized-tree `config_hash` the engine stamps on
`orchestration.config_hash`), the schema-valid `raw` dict (validate scans it for secrets), and
`plain_info` (per-strategy Plain classification for `explain`).

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

import hashlib
import io
import os
from dataclasses import dataclass, field
from pathlib import Path

from openreading import schemas
from openreading.strategies.model import RawNode, StrategyConfig

STRATEGY_PREFIX = "strategy:"
_ENV_VAR = "OPENREADING_CONFIG"
_DEFAULT_FILENAME = "openreading.yaml"
_ALT_FILENAME = "openreading.yml"


class ConfigError(ValueError):
    """A strategy file that cannot be loaded: not found (explicit path), unparseable, or invalid
    against the grammar. Carries a human-readable message that locates the problem."""


@dataclass(frozen=True)
class LoadedConfig:
    """A successfully loaded config plus provenance for the trace/debug surfaces."""

    config: StrategyConfig
    path: Path
    source_hash: str  # sha256 of the file text; the NORMALIZED-tree config_hash arrives in 11.2
    raw: dict  # the schema-valid dict, before model construction (validate scans it for secrets)
    # per-strategy Plain-dialect classification + gate provenance (internal/design/simple-strategies.md
    # §9). Empty for files with no `strategies:`; every strategy is classified otherwise.
    plain_info: dict = field(default_factory=dict)


def discover(
    explicit: str | os.PathLike[str] | None = None, *, allow_cwd: bool = True
) -> Path | None:
    """Return the config path per the discovery order, or None. `allow_cwd=False` (the server)
    skips the working-directory probe so a stray file can never change a long-running service."""
    if explicit is not None:
        p = Path(explicit)
        if not p.is_file():
            raise ConfigError(f"config file not found: {p}")
        return p
    env = os.environ.get(_ENV_VAR)
    if env:
        p = Path(env)
        if not p.is_file():
            raise ConfigError(f"{_ENV_VAR}={env} does not point at a file")
        return p
    if allow_cwd:
        for name in (_DEFAULT_FILENAME, _ALT_FILENAME):
            p = Path.cwd() / name
            if p.is_file():
                return p
    return None


def parse_config_raw(text: str, *, source: str = "<string>") -> tuple[StrategyConfig, dict, dict]:
    """Parse, schema-validate, and desugar config text; return (model, raw dict, plain_info).

    The raw dict is post-desugar canonical longhand (Plain map bodies rewritten to the five-node
    grammar — internal/design/simple-strategies.md §7), so everything downstream sees one tree.
    Raises ConfigError on any parse, grammar, or desugar failure, locating `source`.
    """
    import yaml  # lazy — only when a config is actually loaded

    from openreading.strategies.plain import desugar_config  # lazy: avoids loader<->plain cycle

    # A named stream, not the bare string: PyYAML's Reader labels a `str` input
    # `<unicode string>`, so a syntax error's own location contradicted the filename this
    # message already printed.
    stream = io.StringIO(text)
    stream.name = source
    try:
        raw = yaml.safe_load(stream)
    except yaml.YAMLError as exc:  # malformed YAML
        raise ConfigError(f"{source}: invalid YAML. {exc}") from exc
    if raw is None:
        raise ConfigError(f"{source}: empty config file")
    if not isinstance(raw, dict):
        raise ConfigError(f"{source}: top level must be a mapping, got {type(raw).__name__}")

    try:
        schemas.validate_strategy_config(raw)
    except Exception as exc:  # jsonschema.ValidationError (or a schema error)
        raise ConfigError(f"{source}: {_format_schema_error(exc)}") from exc

    try:
        raw, plain_info = desugar_config(raw)  # Plain -> canonical longhand (or a strict no-op)
    except ConfigError as exc:  # a desugar-time §8 violation; already located
        raise ConfigError(f"{source}: {exc}") from exc

    return StrategyConfig.model_validate(raw), raw, plain_info


def parse_config(text: str, *, source: str = "<string>") -> StrategyConfig:
    """Parse + schema-validate config text into a StrategyConfig (raw + plain_info discarded)."""
    return parse_config_raw(text, source=source)[0]


def load_config(
    explicit: str | os.PathLike[str] | None = None, *, allow_cwd: bool = True
) -> LoadedConfig | None:
    """Discover + load the strategy config, or None when no file is found (the legacy path).
    A found-but-broken file raises ConfigError — an explicitly requested config that cannot load
    is an error, never a silent fall-through to today's behavior."""
    path = discover(explicit, allow_cwd=allow_cwd)
    if path is None:
        return None
    text = path.read_text(encoding="utf-8")
    config, raw, plain_info = parse_config_raw(text, source=str(path))
    source_hash = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
    return LoadedConfig(
        config=config, path=path, source_hash=source_hash, raw=raw, plain_info=plain_info
    )


def strip_strategy_prefix(backend_id: str) -> str | None:
    """Return the strategy name if `backend_id` is a `strategy:<name>` reference, else None.
    `strategy:none` is the reserved escape hatch and returns the literal 'none'."""
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


def _format_schema_error(exc: Exception) -> str:
    """Render a jsonschema ValidationError with its instance path, else str()."""
    path = getattr(exc, "absolute_path", None)
    message = getattr(exc, "message", None) or str(exc)
    if path:
        loc = "/".join(str(p) for p in path)
        return f"invalid config at '{loc}': {message}"
    return f"invalid config: {message}"
