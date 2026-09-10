"""The FastAPI app behind `openreading serve`. The document path (/v1/parse) speaks the vendored
request and response schemas both directions; the control-plane endpoints (/v1/route,
/v1/backends, /healthz, ...) return small JSON shapes. The full endpoint reference — every
method, body/response shape, limit and the status ladder — is the `openreading.server` package
docstring; this one covers how the app enforces it and what it reads from the environment.

HTTP status mapping (D-v2-8). Exceptions raised by the router/adapters are mapped in ONE place,
_error_envelope (wrapped by _error_response):
  403 ScopeRefused / no eligible backend · 424 missing credentials (named backend) or
  `auth_rejected` · 413 doc too large (TerminalError, `doc_too_large`) · 422 unsupported feature ·
  400 unknown_strategy · 504 retryables exhausted / deadline · 502 PlanExhaustedError / other
  terminal · 500 anything else.
The transport ceiling (M2) returns the same 413 envelope and ``doc_too_large`` backend code.
A declared oversized body fails before parsing, and a chunked body stops when its counted bytes overflow.
The HTTP server frames a declared length itself, so bytes beyond a Content-Length never reach this process.
The middleware waits for downstream cleanup and suppresses competing error responses before returning its 413.
A handler exception raised after the overflow is logged with its traceback instead of being answered.
Multipart ingress is decoded by ``openreading.server.uploads`` before the existing request validation and dispatch.
Its ``UploadError`` names the status the decoder chose, and _error_envelope maps it: 400 is the same
bad_request envelope the JSON legs build by hand, 413 shares ``doc_too_large``, any other status is a
sanitized ``error``.
Request-shape and lookup failures on the JSON legs never become exceptions, so they bypass _error_envelope and are
built by small JSONResponse helpers inside create_app: 400 bad_request (body not JSON, fails the
request schema, bad `jobs` / `timeout_s`) via _bad_request; 404 unknown_backend via
_unknown_backend; 404 unknown_job via _not_found. Three more statuses have a dedicated helper
each (_bad_signature, _unauthorized_response, _scope_denied_response):
  401 bad_signature — POST /v1/webhooks/{backend_id}: a configured webhook secret's verification
    fails, OR the backend declares a webhook_secret field at all and none is configured (BL-50:
    fail closed rather than trust an unsigned event as a genuine vendor result).
  401 unauthorized (BL-159) — caller auth is configured (OPENREADING_API_KEYS non-empty) and this
    request carries no Authorization header, or a bearer value matching no configured key.
  403 scope_denied (BL-159) — the matched API key's backend allow-list (OPENREADING_API_KEY_SCOPES)
    does not include the backend this request named directly, or leaves an unnamed request (whose
    whole router chain it bounds, not just the chosen backend) or a strategy walk with nothing left
    to run. A null-backend request whose top pick is out of scope is rerouted onto the pruned
    chain, not refused.
One endpoint deliberately sits OUTSIDE that mapping: POST /v1/backends/{id}/liveness always
returns 200 with a report, even when the finding is `unreachable` or `unauthorized` — "the backend
is down" is a SUCCESSFUL diagnostic, not a failure of this API, and a 5xx would conflate the two
(internal/design/liveness.md §6.4, DECISIONS D-v7-5). Its only non-200s — 404 unknown backend,
403 scope denied, 400 non-numeric `timeout_s` — are all raised before any probe runs.

Server posture (D-v2-8, extended BL-159): CALLER auth is opt-in and OFF by default — zero
OPENREADING_API_KEYS configured behaves byte-for-byte like every prior release. When configured,
every endpoint except GET /healthz and POST /v1/webhooks/{backend_id} (the two endpoints intended
to stay reachable unauthenticated — a health check and a vendor callback carry no bearer) requires
a valid `Authorization: Bearer <token>`; a key's optional backend allow-list is enforced upstream
of, and independent from, the deployment's own `policy.backends`. A scope-denied request never
reaches make_adapter/build_run_context, so no vendor credential is ever resolved for a backend the
caller isn't scoped to. Every source of an allow-list intersects and none widens.
Configured key values are read ONCE at process startup, from the environment ONLY — the same
deploy-knob pattern OPENREADING_CONFIG follows, never a request body or a CLI flag, so a token
never appears in `ps`, shell history, or a request schema field. Bind 127.0.0.1 by default. CORS
is off unless --cors-origin is passed. When both are
configured, CORS is registered OUTERMOST — see create_app — so a browser's unauthenticated preflight
OPTIONS still gets a CORS answer instead of a 401). RouterConfig comes from the `policy:` block of
the openreading.yaml at OPENREADING_CONFIG, read once at startup, never the request body. Fresh adapter instances per request
(build_registry / make_adapter), so a credential-bound client never leaks across requests
(D-v2-8.1, which also fixes why `api.run_request` — sync, driving the job loop via asyncio.run —
is offloaded with run_in_threadpool, and why fastapi is imported at module level).

Environment variables this module reads. Server-only (the CLI and Python API ignore them):
OPENREADING_API_KEYS, OPENREADING_API_KEY_SCOPES, OPENREADING_SERVER_PATH_ROOT,
OPENREADING_JOB_TTL_S, OPENREADING_MAX_ASYNC_JOBS, OPENREADING_MAX_JOBS_PER_PRINCIPAL,
OPENREADING_MAX_BODY_BYTES, OPENREADING_MAX_COMPARE_BYTES,
OPENREADING_ALLOW_UNSIGNED_WEBHOOKS. OPENREADING_CONFIG and backend credential variables are shared with
the CLI / Python API, which read them through the same strategy loader and EnvCredentialBroker.
  OPENREADING_API_KEYS — comma-separated bearer tokens (_load_api_key_config, once at startup).
    Unset/empty ⇒ caller auth OFF, every endpoint open. An empty ENTRY (stray/trailing comma)
    raises ServerConfigError at startup rather than being dropped: a key is security-bearing and
    a quietly discarded token would leave an operator believing one is configured.
  OPENREADING_API_KEY_SCOPES — comma-separated `token=backend1|backend2` entries narrowing one
    listed token to a backend allow-list. Unset means every token is unscoped. Malformed entries
    include an empty entry, missing `=`, an empty key or list, an unlisted token, duplicate token
    scopes, or scopes configured without API keys. Each raises ServerConfigError at startup and
    names the entry position, never the value.
  OPENREADING_MAX_BODY_BYTES bounds raw incoming request bytes and defaults to 157286400 (150 MiB).
    It is read at module import and applies to JSON and multipart bodies before dispatch.
    Multipart file and metadata limits remain independent when this transport ceiling is raised.
  OPENREADING_SERVER_PATH_ROOT — a directory `document.path` may resolve beneath, checked per
    request by `_gate_document_path`. Unset (the default) refuses every `document.path` at
    every caller-body ingress (/v1/parse, /v1/route, /v1/jobs, /v1/batch, /v1/compare): HTTP
    turns a local field naming a file into a remote file-read primitive, so it stays off until
    an operator opts in. /v1/compare carries no `document.path` field, so the same rule reaches
    it as a shape check instead: a string in `responses[]` would name a file on the server, so
    that endpoint takes response envelopes only and refuses a string outright.
    When set, a path must resolve (symlinks followed first) to a regular file under this
    directory; a link that escapes it is refused the same as a literal `..`. An accepted file is
    then READ AT THE GATE and the request carries its bytes onward — no backend ever re-opens the
    path, so the file cannot be swapped between the check and the read — which bounds it by the
    same size ceiling a URL document obeys (`api._MAX_DOWNLOAD_BYTES`) and makes the filename's
    implied type ride along as `mime_type`. The CLI and Python API never read this var —
    `document.path` there names a file the SAME process already trusts, which is why the gate is
    HTTP-only.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import logging
import os
import secrets
import stat
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# fastapi lives in the [server] extra; this module is only imported when serving/testing, so a
# module-level import is fine (and REQUIRED — under `from __future__ import annotations`, FastAPI
# must resolve the `Request` annotation against these module globals).
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from openreading import __version__, api, config, schemas
from openreading.adapters.registry import BUILTIN_ADAPTERS, build_registry, make_adapter
from openreading.batch.sources import DEFAULT_MAX_ITEMS
from openreading.credentials import (
    DEFAULT_DEADLINE_MS,
    EnvCredentialBroker,
    build_run_context,
)
from openreading.derive.mime import resolve_mime_type
from openreading.ledger.header import slim_request
from openreading.liveness import check_liveness, probe_kind
from openreading.readiness import (
    BackendReadiness,
    auth_hinted,
    backend_readiness,
)
from openreading.router.clock import RealClock
from openreading.router.cost import apply_cost_report
from openreading.router.driver import _DriveSliceExpired, run_to_completion
from openreading.router.executor import BoundedResultCache
from openreading.router.router import Router, RouterConfig
from openreading.server.uploads import UploadError, decode_request, request_body_schema
from openreading.strategies.loader import strip_strategy_prefix
from openreading.types.enums import WaitMode
from openreading.types.errors import (
    MissingCredentialsError,
    PlanExhaustedError,
    RetryableError,
    ScopeRefused,
    TerminalError,
    UnknownStrategyError,
    UnsupportedFeatureError,
)
from openreading.types.job import Job, JobState
from openreading.types.request import OpenReadingRequest
from openreading.types.runtime import ResolvedCredentials, RunContext

_ADAPTER_ERRORS = (
    PlanExhaustedError,
    # The caller's allow-list left this request nothing to run. Unlike the two cases above it is
    # raised from INSIDE, past the door, because these request shapes pick their own backends: a
    # strategy walk in strategies.prune, an unnamed request's router chain in api.run_request, and either one's
    # dispatch-point backstop (strategies.engine._resolve_backend, router.executor.execute_plan).
    # See _out_of_scope_backend for what the door can and cannot decide.
    ScopeRefused,
    UnsupportedFeatureError,
    RetryableError,
    TerminalError,
)

# BL-84: the ceiling on POST /v1/batch's `documents[]` — an unauthenticated body otherwise has no
# size limit beyond "is it a list". Reject, not clamp (same philosophy as the jobs ceiling below):
# this endpoint takes no caller-facing override for either — the request body is exactly the
# untrusted-input boundary this item is about. BL-132: sourced from batch.sources.DEFAULT_MAX_ITEMS
# (the CLI's own directory-expansion default, M4 — internal/design/batch-intake.md:98-100) rather
# than a second, independently-hardcoded literal, so the two limits cannot drift apart.
MAX_BATCH_DOCUMENTS = DEFAULT_MAX_ITEMS

# M2: the ceiling on POST /v1/compare's `responses[]`. Compare is pure CPU (no adapter, no
# credential, no network) — its whole cost is the pairwise `SequenceMatcher` diff the comparison
# engine runs over every pair, O(n^2) in the response count. A constant, not an env knob: nobody
# legitimately compares more responses than there are backends to produce them, so there is no
# deployment for which this ceiling should ever need raising.
_MAX_COMPARE_RESPONSES = 50

# M2 residual: the count above bounds how MANY responses are compared, never how LARGE each one
# is, and size is the multiplier that matters. `text_section` runs a pairwise SequenceMatcher
# matrix — quadratic in the length of each text, twice per pair (once over tokens, once over raw
# characters) — so 50 responses is 1225 diffs whose cost is set entirely by a number nothing
# checked. `_MAX_BODY_BYTES` is far too loose to serve as that bound: it exists to stop a huge
# base64 DOCUMENT reaching /v1/parse, where the work is linear. This is the ceiling for the one
# endpoint whose work is not. Read once at import, like its siblings; a test monkeypatches the
# constant.
_MAX_COMPARE_BODY_BYTES = int(os.environ.get("OPENREADING_MAX_COMPARE_BYTES", str(8 * 1024 * 1024)))


@dataclass
class JobRecord:
    """One async job in the in-memory store. `adapter`/`job`/`req` are retained so GET can poll and
    the webhook can resolve. The store is per-process (documented v0.2 limitation).

    `drive_lock` (BL-83): `job` is one mutable object that a POLL drive hands to
    `run_in_threadpool` — two concurrent `GET /v1/jobs/{job_id}` calls for the same still-pending
    job must not both invoke `adapter.poll()` on it at once. A plain `threading.Lock`, not
    `asyncio.Lock`: each concurrent request may be driven by its own event loop (e.g. under
    `TestClient`, or any multi-worker-loop deployment), and only a thread-level primitive is safe
    to acquire/release across that boundary."""

    job_id: str
    backend: str
    adapter: Any
    job: Job
    # M4: the SLIM request — `document.bytes_base64`/`password`/`url` and `async.webhook_url`
    # already nulled (`ledger.header.slim_request`). Never the caller's own object. A record used
    # to pin the full payload for its whole life, unbounded for a still-running job, and no reader
    # here ever wanted it: `build_run_context` reads runtime and credential config, and `normalize`
    # is handed `slim_request(req)` by `_metered` regardless. Same exclusion list the ledger uses,
    # deliberately — one definition of "what a stored copy of a request may contain".
    req: OpenReadingRequest
    created_ms: int
    # Which caller's allowance this record spends — an opaque digest, never the token (see
    # `_principal_id`). None when caller auth is off, which is also when there is no principal to
    # meter and only the global `_MAX_ASYNC_JOBS` applies.
    principal: str | None = None
    # M5: the secret this job's callback URL carries, for a backend that cannot sign its webhooks.
    # It lives HERE and not on the stored request precisely because `slim_request` nulls
    # `async.webhook_url` — the URL that carried it is not retained anywhere a later reader could
    # recover it from. None for a POLL job, and for one submitted before a token was issued.
    callback_token: str | None = None
    response: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    # BL-77/BL-88 history, corrected by BL-92: this WAS the absolute (RealClock-monotonic)
    # drive-deadline anchor actually threaded into `_drive_job` as its per-call budget — computed
    # once at submit time and RENEWED (never cumulatively depleted) on every slice-only expiry
    # (see the `isinstance(e, _DriveSliceExpired)` branch in get_job below). BL-92: that anchoring
    # was itself a bug — under any polling cadence slower than DEFAULT_DEADLINE_MS, the anchor is
    # already stale by the time the NEXT GET arrives (driver.py's deadline check is the first
    # thing the loop body does, so it raises before `adapter.poll()` is ever called), and rolling
    # the anchor forward again on every such GET makes zero forward progress while still reporting
    # "running". `get_job` no longer threads this value into `_drive_job` at all — it now always
    # passes `None`, so every call measures its own slice from its own start (see `_drive_job`
    # below) regardless of any gap since the last call. This field and its renewal are left in
    # place unmodified (BL-92 is scoped to that one call site) but are no longer load-bearing for
    # the per-call budget; the `isinstance(e, _DriveSliceExpired)` check itself — not latching a
    # deadline-only `RetryableError` as job failure — is the piece still doing real work.
    deadline_ms: float | None = None
    drive_lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)


def _job_dict(rec: JobRecord) -> dict[str, Any]:
    state = "succeeded" if rec.response is not None else ("failed" if rec.error else "running")
    out: dict[str, Any] = {
        "job_id": rec.job_id,
        "state": state,
        "backend": rec.backend,
        "created_ms": rec.created_ms,
    }
    if rec.response is not None:
        out["response"] = rec.response
    if rec.error is not None:
        out["error"] = rec.error
    return out


# M4: a terminal job retains the FULL request (base64 document bytes, document.password) and its
# response until removed -- bounded retention is the point, not tidiness. This server has no
# scheduler thread, so sweeping is lazy: _sweep_jobs runs at the top of every submit/GET call
# rather than on a timer. Plain module globals, read once when this module is imported (the same
# effective timing as process startup for `openreading serve`) rather than per-request like
# _gate_document_path below: a malformed value should fail at boot, not
# crash unpredictably on the first job request that happens to touch it (the same reasoning
# _load_api_key_config gives for parsing OPENREADING_API_KEYS once in create_app, AC-7) — and
# unlike those two, _sweep_jobs is a plain top-level function with no `app` closure to cache a
# parsed value on, so a module global is the only place for it to live. A test that needs a
# different value monkeypatches the constant directly (as several already do for
# DEFAULT_DEADLINE_MS) rather than the environment.
_JOB_TTL_MS = int(os.environ.get("OPENREADING_JOB_TTL_S", "3600")) * 1000
_MAX_ASYNC_JOBS = int(os.environ.get("OPENREADING_MAX_ASYNC_JOBS", "1000"))
# M4: `_MAX_ASYNC_JOBS` alone is one counter shared by everyone, so whoever fills it first 429s
# every other caller. That is denial of service to every other key holder on a server where more
# than one principal has a key. This is each key's own allowance within that total. Only
# meaningful when caller auth is on: with no keys configured every request is the same anonymous
# principal, and metering that would just be `_MAX_ASYNC_JOBS` under another name.
_MAX_JOBS_PER_PRINCIPAL = int(os.environ.get("OPENREADING_MAX_JOBS_PER_PRINCIPAL", "100"))


def _principal_id(key: str) -> str:
    """A stable, opaque id for a configured API key. A digest, never the key: the job store is
    long-lived process memory that ends up in a heap dump, a debugger, or a `repr` in a log, and
    a working credential must not be recoverable from any of them. Truncated because this only
    ever has to separate a handful of configured keys from each other, never resist preimage
    search over an unbounded space."""
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _sweep_jobs(jobs: dict[str, JobRecord], now_ms: int) -> None:
    """Delete every TERMINAL record older than `_JOB_TTL_MS`, measured from `created_ms`. Never
    touches a non-terminal record: a still-running job must not be reaped out from under a caller
    mid-poll, no matter its age."""
    expired = [
        jid
        for jid, rec in jobs.items()
        if rec.job.is_terminal() and now_ms - rec.created_ms > _JOB_TTL_MS
    ]
    for jid in expired:
        del jobs[jid]


def _webhook_secret(backend_id: str) -> str | None:
    """The webhook signing secret from the environment (reducto: REDUCTO_WEBHOOK_SECRET), resolved
    spec-driven via the broker. None → no verification configured."""
    desc = make_adapter(backend_id).descriptor
    probe = OpenReadingRequest.model_validate(
        {"document": {"path": "/probe"}, "backend": {"id": backend_id}}
    )
    return EnvCredentialBroker().resolve(desc, probe).values.get("webhook_secret")


def _webhook_secret_required(backend_id: str) -> bool:
    """True when the backend's own credentials_spec declares a `webhook_secret` field at all — i.e.
    it offers signature verification (today: reducto alone). BL-50: the fail-closed-on-a-MISSING-
    secret gate only applies here. A backend that never declares the field (chunkr, open-ocr) has
    no configured/unconfigured distinction to fail on; it is authenticated by the per-job callback
    token instead (M5, see the webhook handler), not left unverified."""
    desc = make_adapter(backend_id).descriptor
    return any(f.key == "webhook_secret" for f in desc.credentials_spec)


# BL-66 Defect 2: the vendor field an inbound event carries its own job/task id under is backend-
# specific — each adapter's own resolve_webhook already checks the correct one (chunkr: task_id,
# open-ocr: request_id, reducto: job_id) — so the dispatcher must read the SAME field per backend
# rather than one hardcoded name, or a genuine non-reducto callback's real id is never read at all.
# A server/app.py-local mapping (not a new AdapterDescriptor field): the fix is scoped entirely to
# this dispatcher — no other caller needs the value — and it keeps the fix schema-free, which a new
# descriptor field would not (see the BL-66 implementation receipt for the full rationale).
_WEBHOOK_EVENT_ID_FIELDS: dict[str, str] = {
    "reducto": "job_id",
    "chunkr": "task_id",
    "open-ocr": "request_id",
}


def _webhook_event_id(backend_id: str, event: dict) -> Any:
    """The inbound event's own job/task id, read from the field name `backend_id`'s vendor actually
    uses. Falls back to reducto's own `job_id` name for a backend not in the map — today that can
    only be a backend with no WEBHOOK wait mode at all, so no job record could ever match it
    regardless of which key is read; the fallback exists so this never raises."""
    field = _WEBHOOK_EVENT_ID_FIELDS.get(backend_id, "job_id")
    return event.get(field)


# M5: the query parameter carrying a job's callback token back from the vendor. Short and
# opaque — it ends up in vendor dashboards and access logs, where a descriptive name would
# advertise what it is worth stealing.
_CALLBACK_TOKEN_PARAM = "ort"


def _allow_unsigned_webhooks() -> bool:
    """OPENREADING_ALLOW_UNSIGNED_WEBHOOKS=1/true/yes restores the pre-M5 behaviour: an event from
    a backend that cannot sign is trusted on its vendor id alone. Read per request, not at import,
    so it behaves like the other deployment knobs. The escape hatch exists because a vendor that
    strips query parameters from the callback URL it was handed would otherwise have its webhooks
    fail closed with no way back; setting it accepts forgeable completions in exchange."""
    return os.environ.get("OPENREADING_ALLOW_UNSIGNED_WEBHOOKS", "").lower() in ("1", "true", "yes")


def _with_callback_token(req: OpenReadingRequest, token: str) -> OpenReadingRequest:
    """`req` with `_CALLBACK_TOKEN_PARAM=<token>` appended to `async.webhook_url`'s query, leaving
    the caller's own object and its own parameters untouched. This is the ONLY channel by which
    the token reaches the vendor and comes back, which is why the server appends it rather than
    letting a caller supply one: a caller-chosen token is a token another caller can guess."""
    from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

    assert req.async_ is not None and req.async_.webhook_url  # caller checks before calling
    parsed = urlparse(req.async_.webhook_url)
    query = [*parse_qsl(parsed.query, keep_blank_values=True), (_CALLBACK_TOKEN_PARAM, token)]
    url = urlunparse(parsed._replace(query=urlencode(query)))
    return req.model_copy(update={"async_": req.async_.model_copy(update={"webhook_url": url})})


def _verify_svix(secret: str, raw: bytes, headers: dict[str, str]) -> None:
    """Raise (svix WebhookVerificationError) if the signature is invalid. Authenticity ONLY: svix's
    `verify()` also re-parses `raw` as JSON internally and would otherwise raise a JSONDecodeError
    — mis-reported by the caller as a 401 bad_signature — for a malformed-but-genuinely-signed
    body. That parse only ever runs AFTER the signature already matched (standardwebhooks checks
    the HMAC first), so a JSONDecodeError here means "verified, just not JSON" and is swallowed;
    the caller does its own json.loads(raw) right after and maps THAT failure to its own 400,
    independent of signature validity (BL-50)."""
    from svix.webhooks import Webhook

    svix_headers = {k: v for k, v in headers.items() if k.startswith("svix-")}
    with contextlib.suppress(json.JSONDecodeError):
        Webhook(secret).verify(raw, svix_headers)


class _GatedFileRefused(Exception):
    """`_read_gated_file` will not hand back this file. Its message is caller-facing (it becomes a
    400 body), so it names the failure and never the server-side detail behind it."""


def _read_gated_file(target: Path) -> bytes:
    """Open `target` and read it, or raise `_GatedFileRefused`.

    `O_NOFOLLOW` is the point of doing this by hand rather than with `Path.read_bytes()`.
    `resolve(strict=True)` never returns a path whose final component is a symlink, so if one is
    a symlink HERE it was swapped in after containment was proved — the exact TOCTOU this whole
    function exists to shut. Refusing it is right; following it would defeat the containment
    check entirely. (`O_NOFOLLOW` covers the final component only. An intermediate directory in
    an already-resolved path would have to be replaced by a directory symlink in the same
    instant, which needs write access inside the operator's own document root and a far narrower
    race than the one being closed here.)

    The size ceiling is `api._MAX_DOWNLOAD_BYTES` — read through the module so a test can
    monkeypatch it — because this is the same act a URL document already performs: materialize an
    external reference into bytes this process holds. Read to the cap PLUS ONE rather than
    trusting `st_size`, which a writer can grow between the stat and the read.
    """
    try:
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise _GatedFileRefused(
            "document.path could not be opened as a regular file under OPENREADING_SERVER_PATH_ROOT"
        ) from None
    with os.fdopen(fd, "rb") as fh:
        # On the fd, not the path: this is the object actually opened, so nothing can be
        # substituted between the check and the read. A FIFO would otherwise block here forever.
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            raise _GatedFileRefused("document.path must resolve to a regular file")
        cap = api._MAX_DOWNLOAD_BYTES
        data = fh.read(cap + 1)
    if len(data) > cap:
        raise _GatedFileRefused(f"document.path exceeds {cap} bytes")
    return data


def _gate_document_path(req: OpenReadingRequest) -> tuple[str | None, OpenReadingRequest]:
    """`document.path` names a file on the SERVER — on the HTTP surface that is a remote
    file-read primitive, so it is off unless the operator names a directory to serve from.
    Containment is proved on the resolved path (symlinks followed first), so a link that
    escapes the root is refused the same as a literal `..`.

    Returns `(refusal_or_None, request)`. On success the returned request no longer carries a
    `document.path` at all: the file is READ HERE and the request comes back holding its bytes,
    the same shape a URL document takes once `api.materialize_document` has fetched it. That is
    what closes the check-then-open TOCTOU — the gate used to prove containment and then leave
    the adapter to open the path a long way downstream (past routing, credential resolution and
    a threadpool hop), and anything able to write inside the document root could swap the checked
    file for a symlink in that window and be handed a file outside it. The bytes the backend
    parses are now the bytes containment was proved for, and a swap afterwards changes nothing.

    Reading here costs the file's size in memory (plus its base64 inflation) for the life of the
    request, bounded by the same ceiling a URL document obeys — see `_read_gated_file`. The
    filename's implied type is carried over into `mime_type` when the caller sent none, because
    the filename is the only format signal a path carries and reading discards it.
    """
    p = req.document.path
    if p is None:
        return None, req
    root = os.environ.get("OPENREADING_SERVER_PATH_ROOT")
    if not root:
        return (
            "document.path is not accepted over HTTP; send bytes_base64 or url, or set "
            "OPENREADING_SERVER_PATH_ROOT to serve files beneath a directory of your choosing"
        ), req
    root_resolved = Path(root).resolve()
    try:
        target = Path(p).resolve(strict=True)
    except OSError:
        return f"document.path {p!r} does not resolve to a readable file", req
    if not (target.is_relative_to(root_resolved) and target.is_file()):
        return (
            "document.path must resolve to a regular file under OPENREADING_SERVER_PATH_ROOT",
            req,
        )
    try:
        data = _read_gated_file(target)
    except _GatedFileRefused as e:
        return str(e), req
    doc = req.document.model_copy(
        update={
            "path": None,
            "bytes_base64": base64.b64encode(data).decode(),
            "mime_type": resolve_mime_type(
                mime_type=req.document.mime_type, filename=target.name, data=data
            ),
        }
    )
    return None, req.model_copy(update={"document": doc})


class ServerConfigError(Exception):
    """A deployment-level OPENREADING_* setting is malformed. Raised from create_app() at process
    startup — never mid-request — matching the existing strategy-config fail-fast precedent
    (_load_strategy_config(None, allow_cwd=False) a few lines into create_app) so a broken
    deployment refuses to bind a socket instead of 500ing unpredictably on the first request that
    happens to touch the broken setting (BL-159 AC-7). Never carries a configured key's actual
    value — every message below names the SETTING and its POSITION in the comma-separated list,
    never the VALUE, so a startup crash log still meets AC-5's redaction bar."""


@dataclass(frozen=True)
class ApiKeyConfig:
    """Parsed OPENREADING_API_KEYS / OPENREADING_API_KEY_SCOPES (BL-159). `keys` empty means
    caller auth is OFF: every endpoint behaves exactly as it does with zero configuration (AC-1).
    `scopes` maps a configured key to the backend ids it may reach; a key absent from `scopes` is
    unscoped. It may reach any backend explicitly named by the request or strategy (AC-4)."""

    keys: frozenset[str] = frozenset()
    scopes: dict[str, frozenset[str]] = field(default_factory=dict)

    @property
    def enabled(self) -> bool:
        return bool(self.keys)


def _load_api_key_config() -> ApiKeyConfig:
    """OPENREADING_API_KEYS: comma-separated bearer tokens — unset or empty means caller auth is
    off (AC-1's byte-for-byte-unchanged default). An empty ENTRY here is a hard failure rather
    than silently dropped: a key is
    security-bearing, so a stray comma should never be able to leave an operator believing a token
    is configured when a parse quietly discarded it.

    OPENREADING_API_KEY_SCOPES: comma-separated `token=backend1|backend2|...` entries narrowing
    one already-listed key to a backend allow-list. A key with no entry here is unscoped. Every
    malformed shape below (empty entry, missing '=', empty key/backend-list, a scope for a key
    OPENREADING_API_KEYS never listed, two scopes for the same key) raises ServerConfigError
    (AC-7) rather than guessing at operator intent."""
    raw_keys = os.environ.get("OPENREADING_API_KEYS", "")
    keys: list[str] = []
    if raw_keys.strip():
        for i, part in enumerate(raw_keys.split(","), start=1):
            key = part.strip()
            if not key:
                raise ServerConfigError(
                    f"OPENREADING_API_KEYS entry {i} is empty: check for a stray comma"
                )
            keys.append(key)
    key_set = frozenset(keys)

    raw_scopes = os.environ.get("OPENREADING_API_KEY_SCOPES", "")
    scopes: dict[str, frozenset[str]] = {}
    if raw_scopes.strip():
        if not key_set:
            raise ServerConfigError(
                "OPENREADING_API_KEY_SCOPES is set but OPENREADING_API_KEYS is empty. A scope "
                "needs a key to scope"
            )
        seen: set[str] = set()
        for i, entry in enumerate(raw_scopes.split(","), start=1):
            entry = entry.strip()
            if not entry:
                raise ServerConfigError(
                    f"OPENREADING_API_KEY_SCOPES entry {i} is empty: check for a stray comma"
                )
            if "=" not in entry:
                raise ServerConfigError(
                    f"OPENREADING_API_KEY_SCOPES entry {i} is missing '=' "
                    "(expected token=backend1|backend2)"
                )
            token, _, backends_raw = entry.partition("=")
            token = token.strip()
            if not token:
                raise ServerConfigError(
                    f"OPENREADING_API_KEY_SCOPES entry {i} has an empty key before '='"
                )
            if token not in key_set:
                raise ServerConfigError(
                    f"OPENREADING_API_KEY_SCOPES entry {i} scopes a key that is not listed in "
                    "OPENREADING_API_KEYS"
                )
            if token in seen:
                raise ServerConfigError(
                    f"OPENREADING_API_KEY_SCOPES entry {i} defines a second scope for a key "
                    "that already has one"
                )
            seen.add(token)
            backend_ids = [b.strip() for b in backends_raw.split("|")]
            if not backends_raw.strip() or any(not b for b in backend_ids):
                raise ServerConfigError(
                    f"OPENREADING_API_KEY_SCOPES entry {i} has an empty or malformed backend "
                    "list (expected token=backend1|backend2)"
                )
            scopes[token] = frozenset(backend_ids)
    return ApiKeyConfig(keys=key_set, scopes=scopes)


def _token_authorized(presented: str, config: ApiKeyConfig) -> str | None:
    """The configured key `presented` matches, or None. Every candidate is compared with
    hmac.compare_digest — never ==/in/startswith against the raw configured value (BL-159 AC-6) —
    and EVERY candidate is checked regardless of whether an earlier one already matched, so the
    number of constant-time comparisons never depends on the presented value's content: a request
    sharing a long valid prefix with a real key takes no measurably different time to reject than
    one sharing none."""
    matched: str | None = None
    for key in config.keys:
        if hmac.compare_digest(presented, key):
            matched = key
    return matched


def _is_auth_exempt_path(path: str) -> bool:
    """GET /healthz and POST /v1/webhooks/{backend_id} — the two endpoints the openreading.server
    docstring's Security section documents as intentionally reachable unauthenticated — stay
    reachable with no Authorization header whether or not any API key is configured (BL-159 AC-8).
    """
    return path == "/healthz" or path.startswith("/v1/webhooks/")


def _unauthorized_response() -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={"error": {"category": "unauthorized", "message": "missing or invalid API key"}},
    )


def _scope_denied_response(backend_id: str) -> JSONResponse:
    return JSONResponse(
        status_code=403,
        content={
            "error": {
                "category": "scope_denied",
                "message": f"this API key is not scoped to reach backend {backend_id!r}",
                "backend_code": backend_id,
            }
        },
    )


def _engages_a_strategy(req: OpenReadingRequest, strategy_config: Any) -> bool:
    """Would api.run_request take a strategy arm for this request?

    Two request shapes reach a strategy walk, and the second is easy to miss: an explicit
    `strategy:<name>` id, and a NULL backend id when the operator's config carries a
    `defaults.strategy` — a strategy walk that names nothing on the wire. Both must be left to the
    walk's own allow-list enforcement rather than gated here against a plain-router pick the
    request is never going to use.
    """
    strat = strip_strategy_prefix(req.backend.id)
    if strat is not None:
        return strat != "none"  # `strategy:none` is the escape hatch back to plain routing
    return bool(
        req.backend.id is None
        and strategy_config
        and strategy_config.defaults
        and strategy_config.defaults.strategy
    )


def _out_of_scope_backend(
    req: OpenReadingRequest,
    scope: frozenset[str],
    strategy_config: Any = None,
    router_config: RouterConfig | None = None,
) -> str | None:
    """A backend id naming why `req` cannot run under `scope`, or None when it can.

    This is the DOOR check. It answers one question — does this request have anything in scope
    left to run? — and it answers it for the two shapes whose backends are knowable at the door,
    before any adapter is constructed or credential resolved (AC-3):

    - A directly-named backend: the id IS the request, so scope is a membership test.
    - A null-backend (or `strategy:none`) request: the plain router's plan is computed here, and the
      allow-list is applied to the whole CHAIN — chosen plus every fallback — via the same
      `RoutePlan.restrict_to` that `api.run_request` applies before executing. Reading `chosen`
      alone was the bug: the executor walks the chain, so a token scoped to the local parser was
      refused `tesseract` and `docling` by name and then handed both the document the moment the
      in-scope pick failed on it. Sharing `restrict_to` is what keeps the door and the execution
      path from ever disagreeing about which backends this request can reach.

    A null-backend request whose first pick is out of scope is not refused while an in-scope
    fallback survives. The router chooses from the scoped chain rather
    than vetoing the request over a pick the caller never made — the same prune-then-run outcome a
    `strategy:` walk already gets, and it costs nothing: only in-scope backends run either way.
    The refusal is reserved for the chain emptying, which fails closed.

    None is also returned when the plain router's own plan is already empty. That is
    ScopeRefused's call to make, not scope's (BL-159 AC-4: an allow-list only ever subtracts
    from the router plan, and there it subtracted nothing).

    A strategy walk is knowable only from inside, so it is enforced inside: the allow-list travels
    with the request as `api.run_request(backend_allowlist=...)` and lands in
    `strategies.prune.compile_strategy`, which prunes every out-of-scope rung. This happens before
    any adapter is built, and answers 403 `scope_denied` when it leaves the walk nothing to run.
    Returning None here is therefore "someone else checks this one", never "this one is
    unchecked": a `strategy:` id was
    once genuinely exempt, which made any strategy id a way around the allow-list, and the four
    presets need no config file, so every caller of every deployment had one.
    """
    backend_id = req.backend.id
    strat = strip_strategy_prefix(backend_id)
    if _engages_a_strategy(req, strategy_config):
        return None  # checked inside the walk — see above
    if backend_id is None or strat == "none":
        plan = Router(build_registry(), router_config or RouterConfig()).route(req)
        if plan.chosen is None:
            return None  # the router already has no executable chain
        if plan.restrict_to(scope).chosen is not None:
            return None  # something the caller may reach survived; the pruned chain runs
        # Nothing survived. Name the backend the request would have used, which is the one the
        # operator has to add to the token's allow-list for this request shape to work.
        return plan.chosen.descriptor.id
    return backend_id if backend_id not in scope else None


def _readiness_dict(r: BackendReadiness, liveness_probe: str = "none") -> dict[str, Any]:
    """One row of GET /v1/backends. `ready` means CONFIGURED — deps import, declared env resolves —
    and has never meant reachable; `liveness_probe` (Pulse) is the ADDITIVE static declaration of
    whether that second question can be answered at all, and what answering it would do. It is a
    descriptor read: free, offline, no call, which is the whole reason it belongs on this endpoint
    while the liveness ANSWER does not (internal/design/liveness.md §6.1)."""
    return {
        "slug": r.slug,
        "type": r.type,
        "extra_installed": r.extra_installed,
        "missing_deps": r.missing_deps,
        "creds_found": r.creds_found,
        "creds_missing": r.creds_missing,
        "ready": r.ready,
        "liveness_probe": liveness_probe,
    }


def _error_envelope(exc: Exception) -> tuple[int, dict[str, Any]]:
    """(HTTP status, error body) for one failure — the single place the taxonomy is mapped.

    An async job that fails after submit records the body alone: a poll- or webhook-driven failure
    must report the same category/backend_code/missing_env the identical failure reports inline,
    but its HTTP status belongs to the job fetch (always 200), not to the failure.
    """
    status: int
    env: dict[str, Any]
    if isinstance(exc, PlanExhaustedError):
        status, env = 502, {"category": "plan_exhausted", "message": str(exc), "trail": exc.trail}
    elif isinstance(exc, ScopeRefused):
        # Same 403 and the same `scope_denied` category the door check returns for a directly
        # named backend, so a caller sees one answer for one cause however the request was
        # spelled — and deliberately NOT `compliance_refused`, which would send the operator to
        # edit unrelated routing settings when the token's allow-list is the cause.
        status = 403
        env = {
            "category": "scope_denied",
            "message": str(exc),
            "backend_code": exc.backend_code,
        }
    elif isinstance(exc, UnsupportedFeatureError):
        status = 422
        env = {"category": "unsupported_feature", "message": str(exc), "backend_code": exc.feature}
    elif isinstance(exc, RetryableError):
        status = 504
        env = {
            "category": "retryable_exhausted",
            "message": str(exc),
            "backend_code": exc.backend_code,
        }
    elif isinstance(exc, UnknownStrategyError):
        status = 400
        env = {
            "category": "unknown_strategy",
            "message": str(exc),
            "backend_code": "unknown_strategy",
        }
    elif isinstance(exc, MissingCredentialsError):
        status = 424
        env = {"category": "terminal", "message": str(exc), "backend_code": "missing_credentials"}
        env["missing_env"] = exc.missing
    elif isinstance(exc, TerminalError):
        code = exc.backend_code or ""
        status = {"auth_rejected": 424, "doc_too_large": 413}.get(code, 502)
        env = {"category": "terminal", "message": str(exc), "backend_code": exc.backend_code}
    elif isinstance(exc, UploadError):
        # The decoder names its own status. Reading it here, rather than in a handler-side
        # table of the two codes it raises today, keeps a future 415 or 422 from turning into
        # an unexplained 500 that nothing tests.
        status = exc.status_code
        if status == 400:
            env = {"category": "bad_request", "message": exc.message}
        elif status == 413:
            env = {"category": "terminal", "message": exc.message, "backend_code": "doc_too_large"}
        else:
            env = {"category": "error", "message": exc.message}
    else:
        status, env = 500, {"category": "error", "message": str(exc)}
    return status, env


def _error_response(exc: Exception):
    status, env = _error_envelope(exc)
    return JSONResponse(status_code=status, content={"error": env})


def _validation_message(e: Exception) -> str:
    """The one-line reason a body was refused. `jsonschema`'s own `str()` appends the whole
    vendored schema, so the commonest client mistake would otherwise answer with a 54 KB body
    whose first line is the only part anyone reads."""
    from jsonschema import ValidationError

    if isinstance(e, ValidationError):
        return f"{e.message} at {e.json_path}"
    return str(e)


_log = logging.getLogger(__name__)

# M2: without a transport ceiling, unauthenticated callers can force unbounded buffering
# before schema validation. Count raw bytes independently of endpoint decoders.
# The default derives from the document cap (`api._MAX_DOWNLOAD_BYTES`, `doc_too_large`): base64
# inflates a binary document by ~4/3, plus headroom for the JSON envelope or multipart framing,
# so 100 MiB becomes 150 MiB. Deriving it keeps the two caps from drifting apart when one moves.
_MAX_BODY_BYTES = int(
    os.environ.get("OPENREADING_MAX_BODY_BYTES", str(api._MAX_DOWNLOAD_BYTES * 3 // 2))
)


class _BodyLimitMiddleware:
    """Count incoming bytes and return one 413 after downstream cleanup on overflow.

    A declared oversized body is refused before parsing or authentication starts.
    A chunked body stops at the same ceiling once its counted bytes pass it. The HTTP
    server frames a declared Content-Length itself, so this middleware never sees
    bytes beyond one. Downstream handlers may mistake the cutoff for a disconnect or
    malformed JSON, so their responses are suppressed after overflow is recorded, and
    an exception they raise instead is logged rather than answered.
    """

    def __init__(self, app, max_bytes: int) -> None:
        self.app, self.max = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        declared = dict(scope.get("headers") or []).get(b"content-length")
        if declared and declared.isdigit() and int(declared) > self.max:
            return await self._too_large(send)
        seen = 0
        overflow = False
        response_started = False

        async def counted_receive():
            nonlocal seen, overflow
            if overflow:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max:
                    overflow = True
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message):
            nonlocal response_started
            # A response committed before the overflow keeps flowing: dropping its tail would
            # leave the client with a truncated body and no second status to explain it.
            if response_started or not overflow:
                response_started |= message["type"] == "http.response.start"
                await send(message)

        try:
            await self.app(scope, counted_receive, guarded_send)
        except Exception:  # noqa: BLE001 - overflow can surface as any handler decode failure
            if not overflow:
                raise
            # The 413 below is the right answer to the client, but a handler bug that surfaced
            # in the same request would otherwise vanish with it: ServerErrorMiddleware sits
            # outside this one, so nothing else ever records the traceback.
            _log.warning(
                "handler raised after the request body overflowed; answering 413", exc_info=True
            )
        if overflow and not response_started:
            # Upload handlers consume the complete body before responding. Waiting
            # for them to unwind here closes partially parsed temporary files first.
            await self._too_large(send)

    async def _too_large(self, send):
        # Built by the same mapper the handlers use, so a field added to the terminal branch
        # reaches the transport leg too, and the message names the limit that fired.
        _, env = _error_envelope(
            TerminalError(
                f"request body too large: exceeds {self.max} bytes", backend_code="doc_too_large"
            )
        )
        body = json.dumps({"error": env}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def create_app(*, cors_origins: list[str] | None = None):
    """Build the FastAPI app that `openreading serve` runs.

    The deployment settings this module has not already read at import time are parsed here,
    once, from the process environment. A malformed value raises `ServerConfigError` before the
    app binds a socket, so a broken setting fails at startup rather than on a later request.
    Pass `cors_origins` to allow those browser origins, or leave it None to keep CORS off. The
    returned app owns the in-memory job store and the resolved-chain result cache for as long as it
    lives. The full endpoint contract is the `openreading.server` package
    docstring, and this module's docstring above lists the settings.
    """
    app = FastAPI(title="OpenReading", version=__version__)
    jobs: dict[str, JobRecord] = {}
    app.state.jobs = jobs  # exposed for tests to seed async/webhook jobs offline
    # Idempotency cache for the /v1/parse resolved chain: one per app, so it lives as long as the
    # server process and never crosses into another app instance (D-v3-3).
    app.state.result_cache = BoundedResultCache()

    # The openreading.yaml is loaded ONLY from OPENREADING_CONFIG — the server never sniffs its
    # cwd (spec §1.2). A broken file fails fast at startup, and so does a `policy:` block that is
    # not a policy: an operator learns the file is wrong from the process that will not start,
    # rather than from the first caller whose request happened to engage a strategy.
    from openreading.strategies.loader import build_config as _build_strategy_config

    _loaded = config.load(None, allow_cwd=False)
    _strategy = _build_strategy_config(_loaded)
    app.state.strategy_config = _strategy.config if _strategy else None
    # Read deployment defaults once. Explicit backend ids bypass `policy.backends`, while a null
    # backend uses that list as its default chain.
    app.state.policy = _loaded.policy if _loaded else None
    app.state.router_config = config.router_config(app.state.policy)

    # BL-159: parsed ONCE here, not per-request — a malformed
    # OPENREADING_API_KEYS/_SCOPES config must fail server startup (AC-7), and re-parsing per
    # request would only ever surface that on the first authenticated request instead.
    api_key_config = _load_api_key_config()
    app.state.api_key_config = api_key_config

    # Registered BEFORE the optional CORS middleware below so CORS ends up OUTERMOST (Starlette/
    # FastAPI's add_middleware prepends — the LAST middleware added runs FIRST on a request): a
    # browser's unauthenticated CORS preflight (OPTIONS) is answered by CORSMiddleware before it
    # ever reaches this gate, exactly like it would with zero API keys configured.
    @app.middleware("http")
    async def _caller_auth(request: Request, call_next):  # noqa: ANN001, ANN202 - fastapi-typed
        # AC-1: zero keys configured ⇒ skip entirely — byte-for-byte today's behavior, the same
        # "off by default" contract every other OPENREADING_* deploy knob in this file honors.
        # AC-8: the two intentionally-open paths stay open whether or not any key is configured.
        if not api_key_config.enabled or _is_auth_exempt_path(request.url.path):
            return await call_next(request)
        scheme, _, presented = request.headers.get("authorization", "").partition(" ")
        matched_key = (
            _token_authorized(presented, api_key_config) if scheme.lower() == "bearer" else None
        )
        if matched_key is None:
            return _unauthorized_response()
        # Stashed for the handler's own scope check (AC-3/AC-4) — None means unscoped (reaches
        # every backend), matching a disabled-auth request's own
        # `getattr(request.state, "api_key_scope", None)` default exactly.
        request.state.api_key_scope = api_key_config.scopes.get(matched_key)
        # M4: the per-principal job allowance needs an identity to meter, and this is the only
        # place a verified one exists. A digest, never `matched_key` itself — see `_principal_id`.
        request.state.principal = _principal_id(matched_key)
        return await call_next(request)

    # M2: registered next (still BEFORE the optional CORS block), same prepend rule as above — CORS
    # stays outermost of all three when configured, so even a 413 this middleware raises still
    # picks up CORS headers for a legitimate cross-origin browser caller to read. Outer to
    # _caller_auth so an oversized body is rejected before spending even a cheap token comparison.
    app.add_middleware(_BodyLimitMiddleware, max_bytes=_MAX_BODY_BYTES)

    if cors_origins:
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(cors_origins),
            allow_methods=["*"],
            allow_headers=["*"],
        )

    def _bad_request(detail: str):
        return JSONResponse(
            status_code=400, content={"error": {"category": "bad_request", "message": detail}}
        )

    def _unknown_backend(e: Exception):
        # `str()` of a KeyError is the repr of its argument, which would wrap the whole sentence
        # in a second pair of quotes on the wire.
        message = e.args[0] if e.args else str(e)
        return JSONResponse(
            status_code=404,
            content={"error": {"category": "unknown_backend", "message": str(message)}},
        )

    def _not_found(category: str, message: str):
        return JSONResponse(
            status_code=404, content={"error": {"category": category, "message": message}}
        )

    def _job_store_full():
        return JSONResponse(
            status_code=429,
            content={
                "error": {
                    "category": "rate_limited",
                    "message": (
                        f"async job store is full ({_MAX_ASYNC_JOBS}); retry after jobs expire "
                        "or DELETE finished jobs"
                    ),
                }
            },
        )

    def _principal_jobs_full():
        return JSONResponse(
            status_code=429,
            content={
                "error": {
                    "category": "rate_limited",
                    "message": (
                        f"this key already has {_MAX_JOBS_PER_PRINCIPAL} async jobs; retry after "
                        "they expire or DELETE finished ones"
                    ),
                }
            },
        )

    def _bad_signature():
        return JSONResponse(
            status_code=401,
            content={
                "error": {"category": "bad_signature", "message": "invalid webhook signature"}
            },
        )

    async def _parse_request(request: Request) -> OpenReadingRequest:
        body = await decode_request(request)  # raises on invalid JSON → caught by caller
        schemas.validate_request(body)  # vendored request schema (raises → 400)
        req = OpenReadingRequest.model_validate(body)
        # Shared by /v1/route and /v1/jobs — a ValueError here lands in each caller's own
        # existing except→400, so this one gate covers both ingress points. The request it
        # RETURNS is the gated one: a rooted document.path has already been read into bytes by
        # this point, so nothing downstream ever holds a path to re-open (see _gate_document_path).
        refusal, req = _gate_document_path(req)
        if refusal is not None:
            raise ValueError(refusal)
        return req

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"status": "ok", "version": __version__}

    @app.get("/v1/backends")
    def backends() -> list[dict[str, Any]]:
        # Free, offline, instant, safe — and it stays that way. Folding a liveness CHECK in here
        # would turn one page load into 15 outbound calls; only the static `liveness_probe`
        # DECLARATION is added, which is a descriptor read (internal/design/liveness.md §6.1).
        broker = EnvCredentialBroker()
        rows = []
        for s in sorted(BUILTIN_ADAPTERS):
            adapter = make_adapter(s)
            rows.append(
                _readiness_dict(
                    backend_readiness(adapter, broker=broker),
                    liveness_probe=probe_kind(adapter.descriptor).value,
                )
            )
        return rows

    @app.post("/v1/backends/{backend_id}/liveness")
    async def backend_liveness(backend_id: str, request: Request):
        """Is this backend actually answering? (internal/design/liveness.md §6.)

        POST, not GET, because a probe is neither safe nor idempotent in the HTTP sense: it causes
        an outbound call, may wake a cold container, and may consume a vendor rate limit. A GET is
        defined as safe and cacheable, and browsers, proxies and link prefetchers are entitled to
        issue one speculatively — which would spend the operator's rate limit without anyone
        asking. ONE backend per call, never a fan-out over the registry: that would be the
        "silently probe 15 vendors" defect wearing a POST.

        Always 200 with a report — including `unreachable` and `unauthorized`. Mapping "the
        backend is down" to 5xx would conflate *openreading failed* with *openreading
        successfully determined the backend is down*; the second is a successful diagnostic and
        the report body IS its result. This is why liveness deliberately does not route through
        `_error_response`. The only non-200s are upstream of any probe: 404 unknown backend,
        403 scope_denied, and 400 for a non-numeric `timeout_s` in the body.
        """
        try:
            adapter = make_adapter(backend_id)
        except KeyError as e:
            return _unknown_backend(e)
        # BL-159: this endpoint resolves a vendor credential and emits a call to that vendor, so it
        # is exactly the "gate before spend" boundary the key allow-list exists for. Gated before
        # any credential is resolved, matching /v1/parse's ordering.
        scope = getattr(request.state, "api_key_scope", None)
        if scope is not None and backend_id not in scope:
            return _scope_denied_response(backend_id)
        timeout_s: float | None = None
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — an empty/absent body is the normal case, not an error
            body = None
        if isinstance(body, dict) and body.get("timeout_s") is not None:
            try:
                timeout_s = float(body["timeout_s"])
            except (TypeError, ValueError):
                return _bad_request('"timeout_s" must be a number')
        # Blocking (network) work off the event loop, like every other dispatch in this file. The
        # probe's own bound is clamped by check_liveness, so a caller cannot park a worker.
        report = await run_in_threadpool(
            check_liveness, adapter, broker=EnvCredentialBroker(), timeout_s=timeout_s
        )
        env = report.to_schema_dict()
        schemas.validate_liveness_report(env)  # never emit a non-conforming report
        return env

    @app.post(
        "/v1/parse",
        openapi_extra={"requestBody": request_body_schema(include_keep_candidates=True)},
    )
    async def parse(request: Request):
        try:
            body = await decode_request(request)
        except UploadError as e:
            return _error_response(e)
        except Exception as e:  # noqa: BLE001 — invalid JSON is a 400
            return _bad_request(f"invalid JSON body: {e}")
        # v0.4: opt-in candidate retention (strategy runs). Popped before schema validation so the
        # strict request schema (additionalProperties: false) still passes.
        keep = bool(body.pop("keep_candidates", False)) if isinstance(body, dict) else False
        try:
            schemas.validate_request(body)
            req = OpenReadingRequest.model_validate(body)
        except Exception as e:  # noqa: BLE001 — any validation failure is a 400
            return _bad_request(_validation_message(e))
        refusal, req = _gate_document_path(req)
        if refusal is not None:
            return _bad_request(refusal)
        # Apply deployment routing defaults before resolving a null backend or strategy.
        try:
            req, router_config = config.apply(req, app.state.policy, app.state.router_config)
        except _ADAPTER_ERRORS as e:
            return _error_response(e)
        # BL-159 AC-3: scope-gate BEFORE run_request ever constructs an adapter or resolves a
        # vendor credential — for both a directly-named backend outside the key's allow-list and
        # a null-backend request the router would otherwise have resolved.
        scope = getattr(request.state, "api_key_scope", None)
        try:
            # The scope check reroutes an unnamed request, so endpoint and alias refusals can start here.
            if scope is not None:
                denied = _out_of_scope_backend(req, scope, app.state.strategy_config, router_config)
                if denied is not None:
                    return _scope_denied_response(denied)
            # run_request is sync and drives the job loop via asyncio.run internally, which cannot
            # nest inside this endpoint's event loop → run it in a worker thread.
            result = await run_in_threadpool(
                api.run_request,
                req,
                config=router_config,
                strategy_config=app.state.strategy_config,
                keep_candidates=keep,
                cache=app.state.result_cache,
                # The half of the gate the door check above cannot do: a strategy walk chooses its
                # own backends, so the allow-list travels with the request and is enforced where
                # those choices are made (ScopeRefused → 403 scope_denied, same as the door).
                backend_allowlist=scope,
            )
        except KeyError as e:
            return _unknown_backend(e)
        except _ADAPTER_ERRORS as e:
            return _error_response(e)
        schemas.validate_response(result)  # never emit a non-conforming response
        return result

    @app.post("/v1/route", openapi_extra={"requestBody": request_body_schema()})
    async def route(request: Request):
        try:
            req = await _parse_request(request)
        except UploadError as e:
            return _error_response(e)
        except Exception as e:  # noqa: BLE001
            return _bad_request(_validation_message(e))
        try:
            req, router_config = config.apply(req, app.state.policy, app.state.router_config)
            # Routing belongs inside this block because it raises too: stage 1 resolves each
            # backend's endpoint through the credential broker, so a body carrying
            # `runtime.endpoint` or an unapproved `credentials_ref` alias is refused here rather
            # than at execution. Both are documented 502s, and only a caught one is a 502.
            plan = Router(build_registry(), router_config).route(req)
        except _ADAPTER_ERRORS as e:  # a caller-scope refusal is a 403, never a 500
            return _error_response(e)
        return {
            "chosen": plan.chosen.descriptor.id if plan.chosen else None,
            "fallbacks": [a.descriptor.id for a in plan.fallbacks],
            "dropped": {
                i: {"stage": dr.stage, "code": dr.code, "reason": dr.detail}
                for i, dr in sorted(plan.dropped.items())
            },
            "terminal_reason": plan.terminal_reason,
        }

    @app.post("/v1/compare")
    async def compare_endpoint(request: Request):
        # Pure (DESIGN L1): compares already-computed response envelopes; never executes a backend.
        raw = await request.body()
        # Measured on the raw bytes and BEFORE json.loads, because parsing a body this size is
        # itself part of the cost being refused. Refusal, never truncation: whatever is accepted
        # is compared in full.
        if len(raw) > _MAX_COMPARE_BODY_BYTES:
            return _bad_request(
                f"compare body exceeds {_MAX_COMPARE_BODY_BYTES} bytes; comparison cost is "
                "quadratic in the text it is given"
            )
        try:
            body = json.loads(raw)
        except Exception as e:  # noqa: BLE001 — any JSON failure is a 400
            return _bad_request(f"invalid JSON body: {e}")
        if not isinstance(body, dict) or not isinstance(body.get("responses"), list):
            return _bad_request('body must be {"responses": [...], "baseline"?, "truth"?}')
        # Same rule as `_gate_document_path`: a body field naming a local file is a remote
        # file-read primitive over HTTP, so this endpoint takes envelopes only. The CLI's
        # `openreading compare a.json b.json` still takes paths, because there the caller and
        # the process are the same trust domain.
        if any(not isinstance(r, dict) for r in body["responses"]):
            return _bad_request('"responses" over HTTP must be response envelopes, not file paths')
        # M2: reject BEFORE the comparison engine ever runs — its pairwise SequenceMatcher diff is
        # O(n^2) in len(responses), so an uncapped list is a CPU-amplification primitive reachable
        # by an unauthenticated caller, the same shape of risk /v1/batch's MAX_BATCH_DOCUMENTS
        # guards against.
        if len(body["responses"]) > _MAX_COMPARE_RESPONSES:
            return _bad_request(
                f"too many responses to compare "
                f"({len(body['responses'])} > {_MAX_COMPARE_RESPONSES})"
            )
        from openreading.comparison import CompareInputError
        from openreading.comparison import compare as _compare

        try:
            return _compare(
                body["responses"], baseline=body.get("baseline"), truth=body.get("truth")
            )
        except CompareInputError as e:
            return _bad_request(str(e))

    @app.post("/v1/batch")
    async def batch_endpoint(request: Request):
        # Manifest v0.6: many documents → one batch-result envelope. The server composes per-item
        # requests from the given document shapes (no directory expansion — that is a client-side
        # concept, §9) and drives them through the SAME pooled, timed platform runner
        # `api.run_batch` already uses (`batch.runner.run_batch`), not a hand-rolled loop — so
        # `summary.duration_ms` is real elapsed time (never the fabricated literal 0), the caller's
        # `jobs` is honored as a bounded worker pool, and `jobs` is echoed on `request` (BL-78).
        # M6 per-item isolation is preserved: run_batch's own per-item wrapper (_run_item) catches
        # any exception — request-build or execution — and turns it into a `failed` item without
        # aborting the batch, the same as the two except clauses this replaces used to.
        from openreading.batch import runner as batch_runner
        from openreading.batch.sources import ResolvedSource, format_of
        from openreading.strategies.presets import PRESET_NAMES
        from openreading.types.batch import BatchRequestEcho, SourceRef

        try:
            body = await request.json()
        except Exception as e:  # noqa: BLE001
            return _bad_request(f"invalid JSON body: {e}")
        if not isinstance(body, dict) or not isinstance(body.get("documents"), list):
            return _bad_request('body must be {"documents": [<document>...], "backend"?, ...}')
        docs = body["documents"]
        if len(docs) > MAX_BATCH_DOCUMENTS:
            return _bad_request(
                f"documents count {len(docs)} is over the max-documents limit "
                f"({MAX_BATCH_DOCUMENTS})"
            )
        backend = body.get("backend")
        # `backend` is one shared string for the whole batch here, but it is an OBJECT
        # (`{"id": ...}`) on /v1/parse and in the vendored request schema, so a client reusing its
        # own /v1/parse body builder sends the object form — which used to reach `make_adapter`
        # unstringified and escape as a raw TypeError: HTTP 500, body `Internal Server Error`, the
        # one response a client written from the documented "every error body has one shape"
        # ladder cannot parse. Refused rather than reduced to its `id`, because the object also
        # carries `operation`, `version`, `credentials_ref` and `runtime` — accepting the shape
        # and keeping only the slug would silently run a different operation than the caller asked
        # for, which is the failure this project refuses everywhere else.
        # A missing `backend` means the caller named none, which is a valid request: selection
        # falls to `policy.backends` and then to the documented default. Only a present-but-wrong
        # SHAPE is refused below.
        if backend is not None and not isinstance(backend, str):
            named = backend.get("id") if isinstance(backend, dict) else None
            # Only echo a slug the caller actually wrote. Naming a default here would tell
            # someone who sent `{}` to run the whole batch on a backend they never asked for,
            # which is the silent substitution this refusal exists to prevent.
            hint = (
                f'Send "backend": "{named}"'
                if isinstance(named, str) and named
                else 'Send "backend" as one backend id, for example "pymupdf"'
            )
            return _bad_request(
                f'"backend" on this endpoint is one string shared by every item. {hint}.'
            )
        # BL-105: `shared` (merged into EVERY per-item request below, in run_one) is an ALLOWLIST
        # of the fields meant to apply batch-wide — not a blocklist of the three batch-envelope-
        # only keys (documents/backend/jobs). A blocklist let `document` (singular) — a genuine
        # OpenReadingRequest field, just not one that belongs at this position in the body — sail
        # through BL-102's "is this a real field name" check untouched and land in `shared` exactly
        # like a legitimate shared field would; run_one's dict literal ({"document":
        # docs_by_relpath[relpath], "backend": {...}, **shared}) then let shared's own `document`
        # key, appearing last, silently win over every item's real per-item document. Enumerating
        # the allowed set explicitly (matching the openreading.server docstring's own documented
        # shared-field list) is also forward-safe: a future OpenReadingRequest field only becomes
        # an implicit batch-wide override if it's deliberately added here, never merely because
        # pydantic
        # recognizes the name. None of the four allowed fields have a JSON alias distinct from
        # their attribute name (only `async_`/`async` does, and `async_` is deliberately excluded
        # from this batch-shared set), so an allowlist of attribute names is exact here — no alias
        # table needed.
        batch_shared_fields = {"outputs", "extraction_schema", "features", "pages"}
        shared = {k: v for k, v in body.items() if k in batch_shared_fields}
        # BL-102: validate once, here, before a single item is attempted — unlike /v1/parse, which
        # validates its whole body against the vendored JSON Schema (additionalProperties: false)
        # before touching pydantic at all, nothing upstream of this point used to check the body's
        # keys, so an unrecognized key reached pydantic once PER ITEM, deep inside run_one, where
        # run_batch's own M6 per-item-isolation wrapper caught the resulting ValidationError and
        # turned it into a `failed` item — reporting an HTTP 200 with every item failing instead of
        # surfacing the single request-shape problem it actually is.
        unrecognized = sorted(
            k
            for k in body
            if k not in batch_shared_fields and k not in ("documents", "backend", "jobs")
        )
        if unrecognized:
            message = f"unrecognized field(s) in batch body: {', '.join(unrecognized)}"
            if "max_jobs" in unrecognized:
                # max_jobs specifically is the foreseeable mistake, not a contrived one: it is the
                # correctly-spelled parameter name for the identical semantic control on this
                # feature's other two surfaces (--max-jobs on the CLI, max_jobs= in the Python
                # API), sitting one field below `jobs` — which this endpoint DOES accept — in the
                # same request body.
                message += (
                    "; 'max_jobs' is not supported on this endpoint. See the openreading.server "
                    "docstring's jobs-ceiling note"
                )
            return _bad_request(message)
        # BL-159 AC-3/AC-4: `backend` (top-level, shared by every item) is scope-gated the same
        # way a named /v1/parse or /v1/jobs backend is — before make_adapter is even reached below
        # for the direct-name case, so an out-of-scope batch never resolves any vendor credential.
        scope = getattr(request.state, "api_key_scope", None)
        if backend is not None and not str(backend).startswith("strategy:"):
            try:
                make_adapter(backend)
            except KeyError as e:
                return _unknown_backend(e)
            if scope is not None and backend not in scope:
                return _scope_denied_response(str(backend))
        strat = strip_strategy_prefix(backend)
        if strat is not None and strat != "none":
            # BL-102's own rule, applied to the other request-shape mistake: a strategy id that
            # names nothing is one problem with the body, not N identical item failures.
            known = set(PRESET_NAMES)
            if app.state.strategy_config is not None:
                known |= set(app.state.strategy_config.strategies)
            if strat not in known:
                return _error_response(
                    UnknownStrategyError(
                        f"unknown strategy {strat!r}; defined: {', '.join(sorted(known))}",
                        name=strat,
                    )
                )
        raw_jobs = body.get("jobs", 1)
        # int() would truncate 2.7 to 2 and parse "3" as 3, so the documented "non-integer jobs
        # -> 400" contract only held for values int() itself refused. `bool` is an int subclass,
        # hence the explicit exclusion.
        if isinstance(raw_jobs, bool) or not isinstance(raw_jobs, int):
            return _bad_request('"jobs" must be an integer')
        try:
            # BL-84: floor clamps to 1, ceiling rejects — the shared helper (batch.runner
            # .bound_jobs), called here before BatchRequestEcho is built below, exactly like the
            # other two surfaces. No caller-facing override on this surface (see MAX_BATCH_DOCUMENTS
            # above): a "max_jobs" field in the body is rejected outright by the unrecognized-field
            # check above (BL-102) rather than silently ignored, which is what this comment
            # incorrectly claimed before that fix landed.
            jobs = batch_runner.bound_jobs(raw_jobs)
        except batch_runner.JobsLimitError:
            # The shared helper's message names --max-jobs and max_jobs=, the CLI and Python
            # overrides. Neither reaches this endpoint, where the body is the untrusted
            # boundary, so the HTTP refusal states the limit and stops, exactly as the sibling
            # max-documents refusal above does.
            return _bad_request(
                f"jobs={raw_jobs} is over the max-jobs limit ({batch_runner.MAX_BATCH_JOBS})"
            )

        sources: list[ResolvedSource] = []
        docs_by_relpath: dict[str, Any] = {}
        for i, doc in enumerate(docs):
            ref = SourceRef(
                filename=(doc.get("filename") if isinstance(doc, dict) else None) or f"doc-{i}",
                format=format_of(
                    (doc.get("filename") or doc.get("url") or "") if isinstance(doc, dict) else ""
                ),
                url=doc.get("url") if isinstance(doc, dict) else None,
                relpath=str(i),
            )
            sources.append(ResolvedSource(ref=ref))
            docs_by_relpath[str(i)] = doc

        # BL-159 AC-3/AC-4 (continued): `backend is None` (or "strategy:none") can route each
        # item to a DIFFERENT backend — capability/format scoring reads each item's own document,
        # so no single upfront plan speaks for the whole batch the way it can for /v1/parse's one
        # document. Build each item's real request (exactly as run_one below does) and check it
        # BEFORE run_batch ever calls run_one for real, so a scope violation on any one item
        # rejects the whole batch up front rather than letting earlier items already spend against
        # a real backend while a later one is still found out of scope mid-pool.
        if scope is not None and (backend is None or strip_strategy_prefix(str(backend)) == "none"):
            for doc in docs_by_relpath.values():
                try:
                    item_req = OpenReadingRequest.model_validate(
                        {"document": doc, "backend": {"id": backend}, **shared}
                    )
                    refusal, item_req = _gate_document_path(item_req)
                    if refusal is not None:
                        raise ValueError(refusal)
                    item_req, item_cfg = config.apply(
                        item_req, app.state.policy, app.state.router_config
                    )
                except Exception:  # noqa: BLE001 — an unbuildable item is run_batch's own M6
                    # per-item-isolation concern (surfaces there as a `failed` item); it is not a
                    # scope decision, so this pre-check defers to that existing path.
                    continue
                denied = _out_of_scope_backend(item_req, scope, app.state.strategy_config, item_cfg)
                if denied is not None:
                    return _scope_denied_response(denied)

        def run_one(src: ResolvedSource, _idem: str | None) -> dict[str, Any]:
            # May run on any of up to `jobs` concurrent worker threads (run_batch's own bounded
            # pool). Resolve the original per-item document by the same index used as
            # SourceRef.relpath above, then drive it through the single-document pipeline exactly
            # as the old per-item loop did.
            relpath = src.ref.relpath
            assert relpath is not None  # every source built above sets relpath=str(i)
            req = OpenReadingRequest.model_validate(
                {
                    "document": docs_by_relpath[relpath],
                    "backend": {"id": backend},
                    **shared,
                }
            )
            # M6: raising here (rather than checking earlier) lets _run_item's own per-item
            # isolation turn a refused document.path into a `failed` item, never aborting the
            # rest of the batch — the same containment as any other bad item.
            refusal, req = _gate_document_path(req)
            if refusal is not None:
                raise ValueError(refusal)
            # A refusal here is this ITEM's outcome, isolated like any other bad item (M6).
            req, item_router_config = config.apply(req, app.state.policy, app.state.router_config)
            return api.run_request(
                req,
                config=item_router_config,
                strategy_config=app.state.strategy_config,
                # A `strategy:` batch is the highest-volume way to reach an out-of-scope backend —
                # one request, `documents[]` items of spend — and the top-level `backend` check
                # above deliberately skips strategy ids, so this is the gate for that shape.
                backend_allowlist=scope,
            )

        echo = BatchRequestEcho(
            backend=str(backend), jobs=jobs, source_args=[s.ref.filename for s in sources]
        )
        # run_batch is synchronous (serial when jobs==1, else its own bounded ThreadPoolExecutor)
        # and measures real wall-clock time internally — run the whole call off the event loop
        # thread, same as the single-item run_in_threadpool calls this endpoint used to make.
        result = await run_in_threadpool(
            batch_runner.run_batch, sources, run_one=run_one, jobs=jobs, request_echo=echo
        )
        env = result.to_schema_dict()
        schemas.validate_batch_result(env)
        return env

    @app.post("/v1/jobs", openapi_extra={"requestBody": request_body_schema()})
    async def submit_job(request: Request):
        _sweep_jobs(jobs, int(time.time() * 1000))
        # Checked BEFORE anything else -- parsing the body, resolving an adapter, spending a
        # vendor call -- so a full store fails fast rather than doing real work for a job it is
        # about to refuse to keep.
        if len(jobs) >= _MAX_ASYNC_JOBS:
            return _job_store_full()
        # Checked in the same breath as the global cap and for the same reason — before the body
        # is parsed or a vendor call is spent on a job this store is about to refuse to keep.
        principal = getattr(request.state, "principal", None)
        if principal is not None:
            held = sum(1 for r in jobs.values() if r.principal == principal)
            if held >= _MAX_JOBS_PER_PRINCIPAL:
                return _principal_jobs_full()
        try:
            req = await _parse_request(request)
        except UploadError as e:
            return _error_response(e)
        except Exception as e:  # noqa: BLE001
            return _bad_request(_validation_message(e))
        try:
            req, router_config = config.apply(req, app.state.policy, app.state.router_config)
        except _ADAPTER_ERRORS as e:  # a caller-scope refusal is a 403, never a 500
            return _error_response(e)
        backend = req.backend.id
        if backend is None:
            # Refused BEFORE the strategy branch below, not after it. `strip_strategy_prefix(None)`
            # is None, so the branch was already unreachable for a null id, and the guard sitting
            # under it left `backend` typed `str | None` through code that hands it to `Job` and
            # `JobRecord`, both of which require a `str`. This is the same 400, one step earlier,
            # and it is what makes `backend` a plain `str` for the rest of the handler.
            return _bad_request("async jobs require a named backend or a 'strategy:<name>'")

        # `strategy:<name>` — wrap the WHOLE strategy walk as one synthetic job (integration.md
        # §3.4). The walk runs via api.run_request (same as /v1/parse); for local/offline backends
        # it resolves immediately, so the job is created already succeeded with its orchestration.
        strat = strip_strategy_prefix(backend)
        if strat is not None and strat != "none":
            try:
                result = await run_in_threadpool(
                    api.run_request,
                    req,
                    config=router_config,
                    strategy_config=app.state.strategy_config,
                    # Same walk, same gate as /v1/parse: wrapping it in a job must not be a way to
                    # reach a backend the token is refused when it asks synchronously.
                    backend_allowlist=getattr(request.state, "api_key_scope", None),
                )
            except KeyError as e:
                return _unknown_backend(e)
            except _ADAPTER_ERRORS as e:
                # UnknownStrategyError is a TerminalError, so this catches it too;
                # _error_response maps it to its own 400 before the generic terminal branch.
                return _error_response(e)
            schemas.validate_response(result)
            sjob = Job(
                id=uuid.uuid4().hex,
                backend_id=backend,
                wait_mode=WaitMode.INLINE,
                state=JobState.SUCCEEDED,
            )
            rec = JobRecord(
                sjob.id,
                backend,
                None,
                sjob,
                slim_request(req),
                int(time.time() * 1000),
                principal=principal,
                response=result,
            )
            jobs[sjob.id] = rec
            return _job_dict(rec)

        if strat == "none":  # strategy:none forces the legacy router path
            return _bad_request(
                "async jobs require a named backend; 'strategy:none' names none and asks the "
                "router to resolve one, which a job cannot do because its backend is its identity"
            )
        # BL-159 AC-3: `backend` is guaranteed a literal named id by this point (both unnamed
        # cases already returned above) — scope-gate it before prepare_named_backend constructs
        # an adapter or resolves a vendor credential.
        scope = getattr(request.state, "api_key_scope", None)
        if scope is not None:
            denied = _out_of_scope_backend(req, scope, app.state.strategy_config, router_config)
            if denied is not None:
                return _scope_denied_response(denied)
        try:
            # Use the same named-backend request and credential setup as /v1/parse.
            # deadline_ms=None (BL-153): no request-schema field originates a real per-request
            # deadline for this path yet — see api.prepare_named_backend's own docstring.
            adapter, req, ctx = api.prepare_named_backend(
                req, backend, config=router_config, deadline_ms=None
            )
        except KeyError as e:
            return _unknown_backend(e)
        except _ADAPTER_ERRORS as e:
            return _error_response(e)
        # M5: issued here — after prepare_named_backend, before submit — because submit is the
        # call that hands the callback URL to the vendor, and the token has to already be in it.
        callback_token: str | None = None
        if req.async_ is not None and req.async_.webhook_url:
            callback_token = secrets.token_urlsafe(32)
            req = _with_callback_token(req, callback_token)
        try:
            with auth_hinted(adapter.descriptor, ctx.credentials):
                job = await run_in_threadpool(adapter.submit, req, ctx)
        except _ADAPTER_ERRORS as e:
            return _error_response(e)
        rec = JobRecord(
            job.id,
            backend,
            adapter,
            job,
            # Slimmed AFTER submit, never before: `adapter.submit` is the one caller that needs
            # the real bytes and the real password, and it has already had them by this line.
            slim_request(req),
            int(time.time() * 1000),
            principal=principal,
            callback_token=callback_token,
            # BL-77: anchor the drive-deadline to submission, once — not to "now" on every GET.
            deadline_ms=RealClock().now_ms() + DEFAULT_DEADLINE_MS,
        )
        if job.is_terminal():
            try:
                # BL-93: auth_hinted wraps this call too (it previously closed right after
                # adapter.submit() above), so an AdapterError normalize() raises gets the same
                # attach_auth_hint + secret redaction the identical failure gets from submit()
                # itself three lines up, instead of reaching rec.error carrying a raw secret value.
                with auth_hinted(adapter.descriptor, ctx.credentials):
                    rec.response = _metered(adapter, job, req, ctx, ctx.credentials)
            except Exception as e:  # noqa: BLE001 - BL-85: a non-adapter exception out of
                # normalize() becomes a failed job, not a crash — the same guard the webhook leg
                # already has, applied here so the documented cross-leg error-shape contract
                # (the openreading.server docstring) actually holds for the submit leg too.
                _, rec.error = _error_envelope(e)
        jobs[job.id] = rec
        return _job_dict(rec)

    @app.get("/v1/jobs/{job_id}")
    async def get_job(job_id: str):
        _sweep_jobs(jobs, int(time.time() * 1000))
        rec = jobs.get(job_id)
        if rec is None:
            return _not_found(
                "unknown_job",
                f"no job with id {job_id!r}. The job store is in memory only, so a server "
                "restart or a TTL expiry drops the record.",
            )
        pending = rec.response is None and rec.error is None and not rec.job.is_terminal()
        # BL-83: rec.job is one mutable object and the drive below crosses into a real OS thread
        # via run_in_threadpool — two concurrent GETs for the same still-pending job_id must not
        # both invoke adapter.poll() on it at once. acquire(blocking=False) is the non-blocking,
        # thread-safe "is a drive already in flight?" check; it never stalls the event loop
        # either way. A caller that loses the race falls straight through to reporting rec's
        # current state rather than launching a second, redundant (and, for a billed-per-poll
        # backend, doubly-charged) drive.
        if (
            pending
            and rec.job.wait_mode is not WaitMode.WEBHOOK
            and rec.drive_lock.acquire(blocking=False)
        ):
            try:
                # POLL jobs are driven on demand; WEBHOOK jobs complete via /v1/webhooks. This
                # handler builds its own RunContext independently of submit_job's (not carried on
                # JobRecord), via the same build_run_context(...) factory every other execution
                # surface uses — NOT a bare RunContext(credentials=...), which would drop
                # ctx.runtime and break azure_document_intelligence's poll() unconditionally
                # (Ledger T4a, F8): with no client ever cached on `rec.adapter` across calls
                # (T4a item 3), THIS is now the credential/runtime source poll() actually builds
                # its client from — no longer merely for redaction, since a fresh drive here may
                # be the first (or only) time this process ever calls poll() on this job.
                ctx = build_run_context(
                    rec.req, rec.adapter.descriptor, broker=EnvCredentialBroker()
                )
                with auth_hinted(rec.adapter.descriptor, ctx.credentials):
                    # BL-92: always `None`, never `rec.deadline_ms` — see the JobRecord.deadline_ms
                    # field comment for why threading the stored anchor in here was the bug.
                    rec.job = await run_in_threadpool(_drive_job, rec.adapter, rec.job, ctx, None)
                    if rec.job.is_terminal():
                        # BL-93: also thread creds through for apply_cost_report's OWN redaction
                        # (a report_cost() failure never raises, so it is never caught — let alone
                        # redacted — by this enclosing auth_hinted(...) block on its own).
                        rec.response = _metered(rec.adapter, rec.job, rec.req, ctx, ctx.credentials)
            except _ADAPTER_ERRORS as e:
                # BL-77/BL-88: a _DriveSliceExpired — raised ONLY by driver.py's own per-call
                # deadline check, never by an adapter — means THIS call's own drive-deadline slice
                # elapsed; it is not genuine exhaustion — the job is still healthy and
                # non-terminal, it just outran this one call's slice. Leave rec.job/rec.error
                # untouched (the job stays "running") instead of latching this job "failed"
                # forever with zero recovery path — the NEXT GET already gets a fresh slice
                # unconditionally (BL-92: `_drive_job` is always called with `None` above, not a
                # stored anchor), so no further action is needed here to make that happen; the
                # `rec.deadline_ms` update below is kept only for the field's own historical shape
                # (see its comment) and is not what gives the next call its fresh slice. Discrimi-
                # nated by TYPE, not by `backend_code` string content (BL-88): `backend_code` is
                # ordinary adapter-writable free text with no uniqueness constraint, and
                # the openreading.strategies.model docstring's own classifier table names
                # "deadline_exceeded" as vocabulary adapters should prefer for a genuine vendor
                # deadline — a bare string match could misclassify that real exhaustion as a
                # harmless slice expiry. Any
                # OTHER RetryableError (a real MAX_CONSECUTIVE_FAULTS exhaustion, re-raised from poll()
                # itself, whatever backend_code the adapter gave it — including this exact sentinel
                # string) and every other _ADAPTER_ERRORS member still records rec.error exactly as
                # before.
                if isinstance(e, _DriveSliceExpired):
                    rec.deadline_ms = RealClock().now_ms() + DEFAULT_DEADLINE_MS
                else:
                    _, rec.error = _error_envelope(e)
            except Exception as e:  # noqa: BLE001 - BL-85: a non-adapter exception out of
                # _metered() (e.g. from normalize()) becomes a failed job, not a crash — the same
                # guard the webhook leg already has. Ordered AFTER the narrower
                # `except _ADAPTER_ERRORS` clause above, never replacing it: BL-88 adds an
                # isinstance check inside that narrower clause, and a taxonomy error must always be
                # caught there first — this broader clause only ever sees what _ADAPTER_ERRORS
                # does not already claim.
                _, rec.error = _error_envelope(e)
            finally:
                rec.drive_lock.release()
        return _job_dict(rec)

    @app.delete("/v1/jobs/{job_id}")
    async def delete_job(job_id: str):
        # No TTL sweep here (unlike submit/GET): a caller naming a specific id is acting on that
        # id directly, not merely touching the store, so this frees the slot immediately and
        # unconditionally -- regardless of job state or age -- rather than waiting on the lazy
        # staleness check submit/GET use to bound unattended growth.
        if jobs.pop(job_id, None) is None:
            return _not_found(
                "unknown_job",
                f"no job with id {job_id!r}. The job store is in memory only, so a server "
                "restart or a TTL expiry drops the record.",
            )
        return Response(status_code=204)

    @app.post("/v1/webhooks/{backend_id}")
    async def webhook(backend_id: str, request: Request):
        try:
            make_adapter(backend_id)  # validate backend exists
        except KeyError as e:
            return _unknown_backend(e)
        raw = await request.body()
        headers = {k.lower(): v for k, v in request.headers.items()}
        secret = _webhook_secret(backend_id)
        if secret:
            try:
                _verify_svix(secret, raw, headers)
            except Exception:  # noqa: BLE001 - any verify failure is a 401
                return _bad_signature()
        elif _webhook_secret_required(backend_id):
            # BL-50: this backend declares webhook_secret but none is configured — fail closed
            # instead of falling through to trust an unsigned, unverifiable event as a genuine
            # vendor result (previously silently accepted, including any fabricated billing data).
            return _bad_signature()
        try:
            event = json.loads(raw)
        except ValueError:
            return _bad_request("invalid JSON body")
        # A vendor event is an object. Anything else reaches the `event[...]` writes below as a
        # TypeError, which escapes as the framework's plain-text 500 rather than the one error
        # shape every other refusal on this API uses.
        if not isinstance(event, dict):
            return _bad_request("webhook body must be a JSON object")
        event["headers"] = headers
        # BL-82: thread the raw bytes through too, so a bound adapter's own resolve_webhook/
        # verify_webhook check (the dispatcher-level _verify_svix above is the primary gate; this is
        # the adapter-side backstop for whichever caller ends up with a credential-bound client)
        # verifies the REAL body instead of always defaulting to b"" and rejecting every genuinely
        # valid signature.
        event["_raw"] = raw
        # BL-66: scoped by BOTH backend_id (Defect 1 — previously any job whose backend_job_id/
        # webhook_token happened to match `jid` resolved here, regardless of which backend's URL
        # was posted to) AND wait_mode is WEBHOOK (Defect 1 — previously a POLL-only job, which
        # never advertises webhook support at all, matched just as readily). jid itself is now read
        # via the posted-to backend's own vendor field name (Defect 2), not hardcoded to reducto's.
        jid = _webhook_event_id(backend_id, event)
        rec = next(
            (
                r
                for r in jobs.values()
                if r.backend == backend_id
                and r.job.wait_mode is WaitMode.WEBHOOK
                # BL-70: an id-less event (jid is None, e.g. a POST body that omits the id
                # field) must never match a job whose own backend_job_id/webhook_token are ALSO
                # both None — submit() can leave both None when a vendor's otherwise-2xx create-
                # task response omitted its id field. Without this guard, `None in (None, None)`
                # is True by construction, hijacking that job with zero id knowledge required.
                and jid is not None
                and jid in (r.job.backend_job_id, r.job.webhook_token)
            ),
            None,
        )
        if rec is None:
            field = _WEBHOOK_EVENT_ID_FIELDS.get(backend_id, "job_id")
            detail = (
                f"the event carries no {field!r} field"
                if jid is None
                else f"no job with {field} {jid!r}"
            )
            return _not_found(
                "unknown_job",
                f"{detail}. A webhook resolves only a job this process submitted and is still "
                "holding.",
            )
        # M5: for a backend that declares no webhook_secret there is no signature to check, so the
        # per-job callback token this server appended to the URL it registered is the ONLY thing
        # separating a genuine completion from one anybody who learned the vendor's task id could
        # forge. On a server with several keys configured, that forgery lands in a job a
        # different key holder submitted. Checked after the lookup, not before, so an event
        # naming no job at all is still the 404 it always was rather than leaking a different
        # answer for ids that do and do not exist.
        if not secret and not _allow_unsigned_webhooks():
            presented = request.query_params.get(_CALLBACK_TOKEN_PARAM)
            if (
                rec.callback_token is None
                or presented is None
                or not hmac.compare_digest(rec.callback_token, presented)
            ):
                return _bad_signature()
        # fresh adapter (client=None) — the server already verified the signature, so resolve_webhook
        # maps the event without re-verifying.
        adapter = make_adapter(backend_id)
        try:
            # builds its own RunContext independently of submit_job's too — see get_job's own
            # comment for why this is build_run_context(...), not a bare RunContext(credentials=
            # ...): resolve_webhook() now requires ctx with no default (Ledger T4a, F9), and a
            # fresh adapter (client=None) builds its real client from ctx.credentials/ctx.runtime
            # here, the same way poll() does.
            ctx = build_run_context(rec.req, adapter.descriptor, broker=EnvCredentialBroker())
            with auth_hinted(adapter.descriptor, ctx.credentials):
                rec.job = adapter.resolve_webhook(event, rec.job, ctx)
        except _ADAPTER_ERRORS as e:
            return _error_response(e)
        if rec.job.is_terminal() and rec.response is None:
            try:
                # BL-93: a second `with auth_hinted(...)` block, reusing the same `ctx` the
                # resolve_webhook call three lines up already resolved — the first block closes
                # right after resolve_webhook, so this leg's _metered() call previously ran
                # unguarded, the webhook-side twin of Leg 1's identical gap.
                with auth_hinted(adapter.descriptor, ctx.credentials):
                    rec.response = _metered(adapter, rec.job, rec.req, ctx, ctx.credentials)
            except Exception as e:  # noqa: BLE001 - a bad payload becomes a failed job, not a crash
                _, rec.error = _error_envelope(e)
        return _job_dict(rec)

    return app


def _metered(
    adapter, job: Job, req, ctx: RunContext, credentials: ResolvedCredentials | None = None
) -> dict:
    """Normalize + meter a finished async job — the /v1/jobs and /v1/webhooks surfaces return the
    same response envelope as /v1/parse, so `usage` is filled the same way.

    `credentials` (BL-93): forwarded to `apply_cost_report` so a `report_cost()` failure's warning
    is redacted the same way a `normalize()` failure's message is by the caller's own
    `auth_hinted(...)` wrap — `_metered()` itself raises nothing new, it only threads the value
    through to the one boundary (`apply_cost_report`) that swallows its own exception and can never
    be protected by a `with auth_hinted(...):` block around this call.

    `ctx` (Ledger T4b): every caller already has one in scope (it built/reused it to drive the job
    this same call is normalizing) — threaded through to `normalize`'s new `(job, ctx, slim_req)`
    signature; `slim_request(req)` is computed here, once, rather than at each of the three
    call sites."""
    return apply_cost_report(
        adapter, job, adapter.normalize(job, ctx, slim_request(req)), credentials
    ).to_schema_dict()


def _drive_job(adapter, job: Job, ctx: RunContext, deadline_ms: float | None = None) -> Job:
    """Drive one job forward by one GET's worth of polling. BL-92: `get_job` always passes `None`
    now — every call measures its own DEFAULT_DEADLINE_MS-sized slice fresh from THIS call's own
    start (below), rather than being handed a stored anchor (BL-77's `JobRecord.deadline_ms`) that
    a caller polling slower than DEFAULT_DEADLINE_MS could already have outrun before the call even
    began. The `deadline_ms` parameter itself is untouched — still honored exactly as before for
    any direct caller (e.g. tests) — it is never non-None from get_job in practice anymore.

    `ctx` (Ledger T4a): threaded straight through to `run_to_completion` — see `get_job`'s own
    comment for why this is no longer merely a redaction nicety.
    """
    clock = RealClock()
    if deadline_ms is None:
        deadline_ms = clock.now_ms() + DEFAULT_DEADLINE_MS
    return run_to_completion(adapter, job, ctx=ctx, deadline_ms=deadline_ms, clock=clock)


def _registry():
    from openreading.adapters.registry import build_registry

    return build_registry()
