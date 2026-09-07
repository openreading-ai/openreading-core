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
  most-restrictive-wins, in `apply()`, once per request, on every path. It is an INTERSECTION, so
  neither source can weaken the other. Booleans OR to true. `max_retention` keeps the lower of the
  two ceilings. `data_region` has no ordering and a request cannot name two regions, so two
  different values refuse rather than pick a winner. The three attestation keys union into the
  `RouterConfig`. Failure prevented: a path that forgot to union. The server's non-strategy path
  was that path, so a request naming a backend by name reached it carrying none of the operator's
  constraints. A precedence rule where the request simply won was the same failure wearing a
  reasonable face: a file demanding `max_retention: zero` and a request asking for `48h` produced
  `48h`, so the deployment's ceiling was whatever the caller last said.
- **PF2. A public call is safe on its own.** `apply()`, `router_config()` and
  `strategies.compile_strategy` enforce the policy they are handed without relying on an earlier
  loader call, and validate a mapping into `openreading.types.policy.Policy` before reading a
  field. The schema guards file text; only this guards a dict a caller built in Python, where
  `bool("false")` is `True` and `frozenset("aws-textract")` is a set of characters.
- **PF3. Resume compares the policy as written, not only its effect.** The ledger stores the
  request after this fold, so removing a file constraint used to leave the stored request carrying
  it and the run identity unchanged. `prune._compute_config_hash` folds in the block as written,
  so both adding and removing a constraint refuse the resume.
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
from openreading.types.errors import ScopeRefused
from openreading.types.policy import Policy, coerce_policy
from openreading.types.request import OpenReadingRequest

_ENV_VAR = "OPENREADING_CONFIG"
_DEFAULT_FILENAME = "openreading.yaml"
_ALT_FILENAME = "openreading.yml"

# The `policy:` keys that become `request.compliance`. The three booleans OR; the two strings
# each have their own rule, because neither is a boolean and they do not share a domain.
# A deliberate SUBSET of Routing. `fallback` is a request field (chain order), not a constraint,
# so a policy can never reorder someone's chain by naming backends (law P3). `doc_type_hint` left
# the policy grammar with `strategy-config` v0.3: no routing stage reads it, and a key that does
# nothing in a file that gates compliance is one a reader will try to rely on.


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

    @property
    def content_hash(self) -> str:
        """sha256 over the CANONICAL parsed mapping, so reordering keys or changing indentation
        does not change it. `source_hash` is the file's bytes and answers "is this the same
        text"; this answers "is this the same configuration", which is what an artifact identity
        or a resume comparison actually means."""
        canonical = json.dumps(self.raw, sort_keys=True, separators=(",", ":"), default=str)
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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
    config: str | os.PathLike[str] | dict | LoadedFile | None = None, *, allow_cwd: bool = True
) -> LoadedFile | None:
    """Discover, read and validate `openreading.yaml`, or return None when there is no file.

    `config` is a path, a dict of the file's own shape, an already-loaded snapshot, or None to run
    the discovery order. A dict
    is validated exactly as file text is, under the source name `<dict>`, so a shape refused from
    a file is refused from Python. Raises `ConfigError` for a file that will not parse or fails
    the schema, a `policy:` block that is not a policy included.
    """
    if isinstance(config, LoadedFile):
        # A snapshot handed back in. `run_batch` reads the file once, before intake, and passes
        # the result to every item, so a file edited while a batch runs cannot make one document
        # travel under a policy its siblings never saw (law PF4).
        return config
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
    req: OpenReadingRequest, policy: Policy | dict | None, base: RouterConfig
) -> tuple[OpenReadingRequest, RouterConfig]:
    """Union the file's `policy:` block into one request and one router configuration (law P4).

    Called once per request, before dispatch, on every path. Idempotent, so a request that already
    carries the file's values is unchanged by a second call. Returns the request untouched and the
    base configuration unchanged when there is no policy, which is what keeps a directory with no
    file byte-identical to every release before this one (law P5). A mapping is validated into a
    `Policy` first, so a caller who never went through a file gets the same refusals a file gets
    (law PF2).
    """
    policy = coerce_policy(policy)
    if policy is None:
        return req, base
    return req, merge_router_config(base, policy)


def router_config(policy: Policy | dict | None) -> RouterConfig:
    """The file's `backends` allow-list as a `RouterConfig`.

    A mapping is validated into a `Policy` first (law PF2), so a caller who never went through a
    file gets the same refusals a file gets. `None` and an absent list mean no restriction from
    this source; an EMPTY list permits nothing, which is deliberate and is the fail-closed
    direction the compliance keys used to hold.
    """
    policy = coerce_policy(policy)
    if policy is None:
        return RouterConfig()
    return RouterConfig(backends=None if policy.backends is None else tuple(policy.backends))


def _one_region(request: str | None, file: str | None) -> str | None:
    """The single region both sources agree on, or a refusal naming both.

    Regions are names rather than quantities, so there is no stricter one to pick and no value
    that means "both". Fabricating one would teach the router a region no backend declares; taking
    either side would let that side overrule the other. A refusal is the honest intersection.
    """
    if request is None or file is None:
        return request if file is None else file
    if request.strip().lower() == file.strip().lower():
        return request
    raise ScopeRefused(
        f"data_region conflict: the request asks for {request!r} and openreading.yaml requires "
        f"{file!r}, and no backend can satisfy both",
        constraint="region_conflict",
    )


def merge_router_config(base: RouterConfig, policy: Policy | dict | None) -> RouterConfig:
    """Fold the file's `backends` allow-list into the RouterConfig.

    Every source of an allow-list INTERSECTS; none widens. A caller's own list and the file's are
    both restrictions, so the effective set is what both permit, and an empty list from either
    permits nothing. `replace` rather than a fresh RouterConfig, so a field this fold does not
    name carries forward instead of silently resetting to its default.
    """
    policy = coerce_policy(policy)
    if policy is None or policy.backends is None:
        return base
    incoming = tuple(policy.backends)
    if base.backends is None:
        return replace(base, backends=incoming)
    # Intersect, keeping the base's order: the narrower of two restrictions is what survives.
    keep = set(incoming)
    return replace(base, backends=tuple(b for b in base.backends if b in keep))


def _format_schema_error(exc: Exception) -> str:
    """Render a jsonschema ValidationError with its instance path, else str()."""
    path = getattr(exc, "absolute_path", None)
    message = getattr(exc, "message", None) or str(exc)
    if path:
        loc = "/".join(str(p) for p in path)
        return f"invalid config at '{loc}': {message}"
    return f"invalid config: {message}"
