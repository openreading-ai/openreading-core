"""Backend readiness: is a backend runnable here, and if not, exactly
which env vars are missing? Drives `openreading backends` and the server's GET /v1/backends. All
resolution is offline (no network, no key validation): it reports what the broker CAN find in the
environment, never whether the provider would accept it.

It also owns the other half of the credential story, the key the broker DID find but the provider
REJECTED (`backend_code="auth_rejected"`). `auth_rejected_hint` renders the one sentence the
`openreading.credentials` docstring promises. `auth_hinted` stamps it onto the failure at the
execution boundary, so every surface (CLI, HTTP API, batch item, plan trail) names the env var to
fix without each one re-deriving it. `auth_hinted` also redacts any OTHER secret the broker
resolved for the backend out of every failure's message, not only the auth_rejected one (BL-37).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from openreading.credentials import EnvCredentialBroker, redact, secret_values
from openreading.types.errors import AdapterError
from openreading.types.request import OpenReadingRequest
from openreading.types.runtime import ResolvedCredentials

# A trail entry (executor.Attempt / strategies.trace.Attempt as_dict) means "the key was rejected"
# when it carries either the router taxonomy's backend_code or the strategy engine's error class.
_AUTH_TRAIL_CODES = frozenset({"auth_rejected", "permission_denied", "invalid_key"})
_AUTH_TRAIL_CATEGORY = "error(auth)"


@dataclass
class BackendReadiness:
    """What `openreading backends` and `GET /v1/backends` report for one backend.

    Every field carries env var NAMES, never their values. `ready` means configured on this
    machine: the extra's dependencies import, every required field resolves, and no `live_gate_env`
    var is missing. It never means the provider answered, which only `openreading.liveness` can
    say.
    """

    slug: str
    type: str
    extra_installed: bool
    missing_deps: list[str] = field(default_factory=list)
    creds_found: list[str] = field(default_factory=list)  # primary env var of each resolved field
    creds_missing: list[str] = field(default_factory=list)  # primary env var of each unresolved
    required_missing: list[str] = field(default_factory=list)  # required-and-unresolved → not ready
    signup_url: str | None = None
    ready: bool = False


def missing_reason(readiness: BackendReadiness) -> list[str]:
    """What to NAME on a not-ready backend, most precise reason first: the required-and-unresolved
    vars, else the broader unresolved set (an ambient-chain backend blocked by `live_gate_env` has
    an empty `required_missing`), else the missing extras — so a not-ready row is never blank about
    why. Empty only for a backend that is not ready and declares nothing at all.

    One function because every surface that groups backends by readiness must say the same thing
    about the same backend. Keeping three copies of `a or b or c` is how one registry grew two
    vocabularies, and that is the defect this replaced (internal/design/ui-app/qa-ive-v2.md, H11).
    """
    return list(readiness.required_missing or readiness.creds_missing or readiness.missing_deps)


def missing_required(descriptor, ctx) -> list[str]:
    """The primary env var of every required credential/config field the broker did NOT resolve
    into `ctx`. Empty → the backend has everything it declared it needs (ambient-chain backends,
    whose fields are all optional, are never blocked here)."""
    creds = ctx.credentials.values if ctx.credentials else {}
    runtime = ctx.runtime or {}
    out: list[str] = []
    for f in descriptor.credentials_spec:
        if f.required and f.key not in creds:
            out.append(f.env[0] if f.env else f.key)
    for f in descriptor.config_spec:
        if f.required and f.key not in runtime:
            out.append(f.env[0] if f.env else f.key)
    return out


def primary_secret_env(descriptor) -> str | None:
    """The env var to check when a key was found but REJECTED: the first required secret the
    backend declares, else any declared var. None for a backend that declares no env at all."""
    for f in descriptor.credentials_spec:
        if f.required and f.secret and f.env:
            return f.env[0]
    for f in descriptor.credentials_spec:
        if f.env:
            return f.env[0]
    return None


def _descriptor_for(slug: str):
    from openreading.adapters.registry import make_adapter

    try:
        return make_adapter(slug).descriptor
    except KeyError:
        return None


def auth_rejected_hint(backend: str, descriptor=None) -> str:
    """The one sentence every surface shows for `auth_rejected`, the sentence the
    `openreading.credentials` docstring promises: the key was found, the provider said no, here is
    the var to check. `descriptor=None` resolves it from the registry by slug. It NEVER carries the
    provider's response body, because providers have been seen echoing the rejected key back."""
    if descriptor is None:
        descriptor = _descriptor_for(backend)
    env = primary_secret_env(descriptor) if descriptor is not None else None
    hint = f": check {env}" if env else ""
    signup = (
        f" (signup: {descriptor.signup_url})"
        if descriptor is not None and descriptor.signup_url
        else ""
    )
    return f"key was found but rejected by {backend}{hint}{signup}"


def attach_auth_hint(exc: AdapterError, descriptor) -> None:
    """Rewrite an `auth_rejected` failure's message into the actionable, key-free hint, in place.
    Every downstream surface renders `str(exc)`, so this is what makes the hint reach batch item
    errors, plan trails, and HTTP error bodies alike. A no-op for any other failure; idempotent, so
    the innermost boundary (the one that knows which backend actually ran) wins."""
    if exc.backend_code != "auth_rejected" or exc.auth_hint:
        return
    msg = auth_rejected_hint(descriptor.id, descriptor)
    exc.auth_hint = msg
    exc.message = msg
    exc.args = (msg,)


@contextmanager
def auth_hinted(descriptor, credentials: ResolvedCredentials | None = None) -> Iterator[None]:
    """Wrap an adapter invocation (submit / drive / normalize) so a rejected key leaves this block
    already carrying its `check <VAR>` hint. `credentials` is optional — every pre-existing caller
    keeps working unchanged — but when a caller passes the same ResolvedCredentials it already
    resolved into its RunContext, any secret value that bag holds is also redacted (`***`) out of
    the failure's message, mirroring the defense-in-depth `router.executor.execute_plan` applies on
    its own per-attempt boundary (BL-37). Runs AFTER attach_auth_hint, so the auth_rejected hint
    text — which never carries a secret — passes through untouched.

    `adapter.normalize()` is ordinary adapter code, not one of the five taxonomy types — an adapter
    author who misses a malformed-payload shape produces a plain KeyError/IndexError/ValueError/
    AttributeError, not an AdapterError. The except AdapterError clause above is blind to it, so a
    second, broader clause redacts it too (BL-99): never attach_auth_hint (an AdapterError-only
    operation that reads exc.backend_code, which a plain exception doesn't have), just the same
    redact() scrub, mutating .args directly since a plain exception has no .message attribute for
    str(exc) to read."""
    try:
        yield
    except AdapterError as e:
        attach_auth_hint(e, descriptor)
        e.message = redact(e.message, secret_values(descriptor, credentials))
        e.args = (e.message,)
        raise
    except Exception as e:
        e.args = (redact(str(e), secret_values(descriptor, credentials)),)
        raise


def auth_rejected_backends(trail: list[dict]) -> list[str]:
    """The backends in a PlanExhaustedError trail whose attempt failed on a REJECTED key. Trails
    drop the failure message, so a caller rendering a trail re-derives the hint from these slugs.
    De-duplicated, in trail order."""
    out: list[str] = []
    for entry in trail:
        backend = entry.get("backend")
        rejected = (
            entry.get("code") in _AUTH_TRAIL_CODES or entry.get("category") == _AUTH_TRAIL_CATEGORY
        )
        if backend and rejected and backend not in out:
            out.append(backend)
    return out


def _probe_request(slug: str) -> OpenReadingRequest:
    # a minimal request the broker can read backend.credentials_ref / backend.runtime off of.
    return OpenReadingRequest.model_validate(
        {"document": {"path": "/probe"}, "backend": {"id": slug}}
    )


def backend_readiness(adapter, *, broker: EnvCredentialBroker | None = None) -> BackendReadiness:
    """One backend's readiness, resolved offline: no network call, no key validation.

    Calls `adapter.health()`, resolves every declared credential and config field through
    `broker`, and records the primary env var of each field that resolved or is missing. `ready`
    is False when `health()` says so, or when a required field is unresolved. A missing
    `live_gate_env` var also makes it False, for the reason the comment below gives.
    """
    broker = broker or EnvCredentialBroker()
    desc = adapter.descriptor
    health = adapter.health()
    req = _probe_request(desc.id)
    creds = broker.resolve(desc, req)
    runtime = broker.resolve_config(desc, req)

    found: list[str] = []
    missing: list[str] = []
    required_missing: list[str] = []
    for f in [*desc.credentials_spec, *desc.config_spec]:
        env_name = f.env[0] if f.env else f.key
        resolved = f.key in creds.values or f.key in runtime
        (found if resolved else missing).append(env_name)
        if f.required and not resolved:
            required_missing.append(env_name)

    # `live_gate_env` names the vars an ambient-chain backend (credentials_spec/config_spec fields
    # all declared optional on purpose — e.g. anthropic-claude's SDK-ambient api_key, aws-textract's
    # boto3 credential/region chain) ACTUALLY needs to authenticate; it already drives the keyed
    # live-test lane's own skip decision (tests/live_helpers.gate_env). `ready` used to ignore it
    # entirely, so a backend could render `status: ready` on the same Backends row that lists one of
    # these exact vars in "Credentials missing" — this folds it in so that contradiction can't
    # happen. `required_missing` above stays an honest read of the descriptor's own `required`
    # flags; this only widens what makes `ready` False, so a backend with no live_gate_env declared
    # (every non-ambient-chain adapter) is unaffected.
    live_gate_missing = [v for v in desc.live_gate_env if v in missing]

    return BackendReadiness(
        slug=desc.id,
        type=desc.type.value,
        extra_installed=not health.missing_deps,
        missing_deps=list(health.missing_deps),
        creds_found=found,
        creds_missing=missing,
        required_missing=required_missing,
        signup_url=desc.signup_url,
        ready=health.ready and not required_missing and not live_gate_missing,
    )
