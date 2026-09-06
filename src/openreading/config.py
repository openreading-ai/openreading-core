"""The one reader of `openreading.yaml`: discovery, reading, schema validation, and the union of
a request's own constraints with the file's `policy:` block.

A policy is the short list of things a backend must declare before it may read your document, such
as a signed business associate agreement (BAA) or a European data region. A backend is one parser,
either a local library or a hosted API. Its descriptor is the static record in which it declares
what it reads, what it needs, and its compliance posture. `openreading.yaml` is the only file you
write, and its `policy:` block is the only place a policy is spelled. Nine keys make up the whole
grammar: `require_baa`, `no_train_on_data`, `data_region`, `require_local`, `max_retention`,
`optimize_for`, `allow_unverified_compliance`, `train_optout_confirmed` and `baa_tier_confirmed`.
A malformed block is refused before a backend is contacted, and the CLI reports it as exit 3.

Discovery order (first hit wins; sources are NEVER merged):
  1. an explicit path or an inline dict — CLI `--config PATH`, Python `openreading.run(config=…)`;
  2. the `OPENREADING_CONFIG` environment variable;
  3. `./openreading.yaml` (or `./openreading.yml`) in the working directory — CLI and Python only.

`discover()` takes `allow_cwd`. The server (`openreading serve`) passes `False` and never sniffs
its working directory, because a stray file next to a long-running process must not change which
backends it may reach. `load()` returns `None` when nothing is found anywhere, which is the "no
file, no policy" path. A found-but-broken file raises `ConfigError`: a config you asked for and
that cannot load is an error, never a silent fall-through.

Environment variables this module reads
---------------------------------------
- `OPENREADING_CONFIG` — an explicit path to `openreading.yaml`, precedence step 2. Unset, or set
  to the EMPTY string, which `discover()` treats as unset: fall through to the working-directory
  probe where that is allowed, else no file and no policy. Set non-empty but not pointing at a
  file: `ConfigError`. Every command reads it, not only strategy runs.

Laws
----
Each law names the failure it prevents. P1 to P3 are held by the surfaces above this module, and
P4 to P6 by this one.

- **P1. One file.** `openreading.yaml` is the only file a person writes, and its `policy:` block
  is the only place a policy is spelled. Failure prevented: two documents describing one grammar
  in two syntaxes, and a reader concluding one of them is wrong.
- **P2. No hand-written JSON input.** JSON remains as output, as the wire, and as the schema
  language. Nothing a person authors is JSON. Failure prevented: a policy file with no schema
  behind it, which is what the removed `--policy` flag took.
- **P3. A policy never names a backend.** It names a requirement, and the descriptor meets it or
  does not. A strategy names backends and runs inside the survivors. Failure prevented: a
  per-backend exception that widens the set by naming its way around a constraint. The three
  attestation keys are the deliberate exception, and each one asserts a fact about paperwork
  rather than a preference about a vendor.
- **P4. The union never widens.** Request constraints and file constraints combine
  most-restrictive-wins, in `apply()`, once per request, on every path. Booleans OR to true. A
  region or a retention ceiling the request names wins, and the file supplies one only when the
  request named none. The three attestation keys union into the `RouterConfig`. Failure prevented:
  a path that forgot to union. The server's non-strategy path was that path, so a request naming a
  backend by name reached it carrying none of the operator's constraints.
- **P5. No file is byte-identical to today.** A directory with no `openreading.yaml` and no
  `OPENREADING_CONFIG` routes exactly as it did before this module existed, because `load()`
  returns `None` and `apply()` hands the request straight back. Failure prevented: a silent change
  to every user who never wrote a file.
- **P6. Reading the file never imports the engine.** Nothing here imports
  `openreading.strategies`, which imports its engine, its loader, prune and validate at package
  import. Failure prevented: a `parse --backend pymupdf` that finds a file holding only a
  `policy:` block paying for a package it never runs. `tests/test_config.py` and guardrail T10 in
  `tests/test_strategy_surface.py` are the proof.

Relatives: `openreading.strategies.loader` builds the `StrategyConfig` from the mapping this
module returns and owns everything about strategies. `openreading.router.compliance` owns
`RouterConfig` and what each compliance key means against a descriptor. `openreading.api` calls
`load()` and then `apply()` before it dispatches anything.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from openreading import schemas
from openreading.router.compliance import RouterConfig
from openreading.types.request import Compliance, OpenReadingRequest, Routing

_ENV_VAR = "OPENREADING_CONFIG"
_DEFAULT_FILENAME = "openreading.yaml"
_ALT_FILENAME = "openreading.yml"

# The `policy:` keys that become `request.compliance`. Booleans OR; strings take the request's
# value first. Both tuples are read by `union_compliance` and by nothing else.
_COMPLIANCE_BOOL = ("require_baa", "no_train_on_data", "require_local")
_COMPLIANCE_STR = ("data_region", "max_retention")
# A deliberate SUBSET of Routing: `fallback` is a request field (chain order), not a constraint,
# so a policy can never reorder someone's chain by naming backends. See law P3.
_ROUTING_KEYS = ("doc_type_hint", "optimize_for")


class ConfigError(ValueError):
    """An `openreading.yaml` that cannot be loaded: not found (explicit path), unparseable, or
    invalid against the grammar. Carries a human-readable message that locates the problem."""


@dataclass(frozen=True)
class LoadedFile:
    """One successfully loaded `openreading.yaml`, before anything interprets it.

    `raw` is the schema-valid mapping as written, before the Plain dialect is desugared, so the
    strategy loader and this module see the same bytes. `policy` is the `policy:` sub-dict, or
    `None` when the file carries no block. `path` is `None` for a dict passed to `load()`, which
    has no file behind it. `source_hash` is the sha256 of the file text, distinct from the
    compliance-aware `config_hash` the strategy engine stamps on a response.
    """

    raw: dict
    policy: dict | None
    path: Path | None
    source_hash: str


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


def parse(text: str, *, source: str = "<string>") -> dict:
    """Parse and schema-validate config text into the raw mapping it declares.

    Uses `yaml.safe_load` only (D-v3-1), so a config file can never construct arbitrary Python
    objects and a `!!python/object/apply` tag raises `ConfigError`. `pyyaml` (MIT) is imported
    lazily, so a run that finds no file never pays for the import. Errors locate by node path
    (`strategies.cheap.steps[0].escalate_if`), not line number: `safe_load` discards source marks,
    and a mark-preserving loader was judged not worth it (D-v3-8).
    """
    import yaml  # lazy — only when a config is actually loaded

    # A named stream, not the bare string: PyYAML's Reader labels a `str` input
    # `<unicode string>`, so a syntax error's own location contradicted the filename this
    # message already printed.
    stream = io.StringIO(text)
    stream.name = source
    try:
        raw = yaml.safe_load(stream)
    except yaml.YAMLError as exc:  # malformed YAML
        raise ConfigError(f"{source}: invalid YAML. {exc}") from exc
    return _validated_mapping(raw, source=source)


def _validated_mapping(raw: Any, *, source: str) -> dict:
    """Shape-check and schema-check one already-parsed document. Shared by the file path and the
    dict path so a mapping Python passes inline is refused exactly where a file is."""
    if raw is None:
        raise ConfigError(f"{source}: empty config file")
    if not isinstance(raw, dict):
        raise ConfigError(f"{source}: top level must be a mapping, got {type(raw).__name__}")
    try:
        schemas.validate_strategy_config(raw)
    except Exception as exc:  # jsonschema.ValidationError (or a schema error)
        raise ConfigError(f"{source}: {_format_schema_error(exc)}") from exc
    return raw


def load(
    config: str | os.PathLike[str] | dict | None = None, *, allow_cwd: bool = True
) -> LoadedFile | None:
    """Discover, read and validate `openreading.yaml`, or return None when there is no file.

    `config` is a path, a dict of the file's own shape, or None to run the discovery order. A dict
    is validated exactly as file text is, under the source name `<dict>`, so a shape refused from
    a file is refused from Python. Raises `ConfigError` for a file that will not parse or fails
    the schema, a `policy:` block that is not a policy included.
    """
    if config is not None and not isinstance(config, str | os.PathLike | dict):
        # Without this the value reaches `Path()`, which raises a bare TypeError naming neither
        # the argument nor what it should have been.
        raise ConfigError(
            f"config must be a path or a mapping of the openreading.yaml shape, got "
            f"{type(config).__name__}"
        )
    if isinstance(config, dict):
        # A dict has no bytes of its own, so the hash comes from a canonical rendering of it. Two
        # callers who wrote the same keys in a different order get the same provenance.
        text = json.dumps(config, sort_keys=True, default=str)
        raw = _validated_mapping(config, source="<dict>")
        path = None
        source_hash = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
    else:
        path = discover(config, allow_cwd=allow_cwd)
        if path is None:
            return None
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            # A file the process cannot open is the same class of problem as one that will not
            # parse. Left bare it reaches the surfaces as a raw PermissionError and exits 1 with a
            # traceback, which tells the reader nothing about which file was meant.
            raise ConfigError(f"cannot read config file {path}: {exc.strerror or exc}") from exc
        raw = parse(text, source=str(path))
        source_hash = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
    return LoadedFile(raw=raw, policy=raw.get("policy"), path=path, source_hash=source_hash)


def apply(
    req: OpenReadingRequest, policy: dict | None, base: RouterConfig
) -> tuple[OpenReadingRequest, RouterConfig]:
    """Union the file's `policy:` block into one request and one router configuration (law P4).

    Called once per request, before dispatch, on every path. Idempotent, so a request that already
    carries the file's values is unchanged by a second call. Returns the request untouched and the
    base configuration unchanged when there is no policy, which is what keeps a directory with no
    file byte-identical to every release before this one (law P5).
    """
    if not policy:
        return req, base
    config = merge_router_config(base, policy)
    updates: dict[str, Any] = {}
    effective = union_compliance(req.compliance, policy)
    # A policy of attestations alone constrains nothing, so it must not turn a request that
    # carried no `compliance` object into one full of falses. The echoed request in the response
    # would then differ for a file whose block changes no backend's eligibility.
    if req.compliance is not None or any(v for v in effective.values()):
        updates["compliance"] = Compliance(**effective)
    routing = {k: policy[k] for k in _ROUTING_KEYS if k in policy}
    if routing:
        # The request wins key by key, and the file fills in only what the request left unsaid.
        # `exclude_none` so a field the caller never set does not mask the file's value.
        merged = req.routing.model_dump(exclude_none=True) if req.routing else {}
        updates["routing"] = Routing(**{**routing, **merged})
    return req.model_copy(update=updates), config


def router_config(policy: dict | None) -> RouterConfig:
    """The three deployment-level policy keys as a `RouterConfig` (D7/D7a).

    It validates the whole policy first, because a `route()`-shaped call reaches this with a
    policy that never passed through `build_request`.
    """
    policy = policy or {}
    return RouterConfig(
        allow_unverified_compliance=bool(policy.get("allow_unverified_compliance", False)),
        train_optout_confirmed=frozenset(policy.get("train_optout_confirmed", [])),
        baa_tier_confirmed=frozenset(policy.get("baa_tier_confirmed", [])),
    )


def union_compliance(req_compliance, policy: dict[str, Any] | None) -> dict[str, Any]:
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


def merge_router_config(base: RouterConfig, policy: dict[str, Any] | None) -> RouterConfig:
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


def _format_schema_error(exc: Exception) -> str:
    """Render a jsonschema ValidationError with its instance path, else str()."""
    path = getattr(exc, "absolute_path", None)
    message = getattr(exc, "message", None) or str(exc)
    if path:
        loc = "/".join(str(p) for p in path)
        return f"invalid config at '{loc}': {message}"
    return f"invalid config: {message}"
