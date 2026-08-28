"""Backend liveness ("Pulse") — is a backend *actually answering*, as opposed to merely
configured? Design record: internal/design/liveness.md; decisions D-v7-1..6 in
internal/decisions/DECISIONS.md, restated at the end of this docstring. (`internal/` means the
private openreading-internal context repo, not a directory in this tree.)

The defect this fixes
---------------------
`readiness.backend_readiness()` answers "is this backend runnable here": deps import, env vars
resolve. It is offline by construction, which is why `make verify` can call it — and why its
`ready` flag has always meant CONFIGURED, never REACHABLE. `docling` says it out loud:
`Health(ready=True, detail="requires a running docling-serve container")`; `QWEN_VL_ENDPOINT` being
set proves a URL was typed into `.env`, not that anything listens on it. Point `DOCLING_SERVE_URL`
at a closed port and every surface used to show a green "Ready". The acceptance check is that exact
scenario: with `DOCLING_SERVE_URL` on a closed port, `POST /v1/backends/docling/liveness` must now
report `unreachable` (200, `measured: true`), and the Backends page must say the same.

The fix composes readiness without changing it: an optional adapter probe (the measurement only),
a static descriptor declaration (can this be tested, and does testing it leave my network or cost
money — answerable offline), a platform-owned ladder (this module) that turns declaration +
environment + optional probe result into one `LivenessReport`, a vendored schema
(`liveness-report.v0.1.json`), and one dedicated HTTP endpoint. `readiness` is untouched and still
calls only `health()`.

The status ladder (normative — third parties implement against it)
------------------------------------------------------------------
    status                 measured  meaning                                 operator does
    not_supported          no        no probe AND nothing declared to        nothing; cannot be
                                     infer from                              assessed at all
    not_configured         no        a required credential/config var is     set the named env
                                     missing                                 vars
    configured_unverified  no        no probe; every declared requirement    nothing; liveness is
                                     resolves                                UNPROVEN, not proven
    live                   yes       we called it and it answered healthy    nothing
    unreachable            yes       nothing answered: refused, DNS,         start the container /
                                     timeout                                 fix the URL
    unauthorized           yes       it answered and rejected the credential rotate/fix the key
    error                  yes       it answered but not in a way that       read `detail`
                                     proves health (5xx, malformed), or
                                     the probe itself raised

As a sentence: not_configured -> configured_unverified -> live, with unreachable / unauthorized /
error as the measured negatives and not_supported as the "nothing to say" floor. Every state
distinguishes a case that is actually different and actually actionable; the two confusions worth
most are unauthorized vs unreachable ("fix your key" vs "start your container" — opposite actions)
and error vs unreachable (a 503 from a model server still loading means it IS up).

Four rules make the ladder honest, and they live HERE rather than in 13 adapters that would each
have to remember them (D-v7-2):

1. **A known negative beats everything.** `not_configured` is exactly `backend_readiness().ready`
   being False — deliberately the SAME judgement, computed by the same function, so "configured"
   can never mean one thing on the Backends page and another here (`readiness.missing_reason`'s
   docstring records what happened the last time one fact grew two vocabularies). It is never
   softened into an optimistic assumption, and it short-circuits the probe, so we never spend a
   network call proving that a backend with no API key cannot authenticate.
2. **Inference is not measurement — and the SCHEMA says which is which** (D-v7-3).
   `configured_unverified` comes from a local config read: `measured=False`, `latency_ms=None`
   (a fabricated latency would imply a round trip that did not happen), and `checked_at` is
   honestly when the report was produced, not when a backend was contacted. `live` carries
   `measured=True`, a real latency and a real round-trip `checked_at`. `measured` is redundant
   with `status` and included anyway: a third-party frontend must tell measurement from inference
   by one field read, not by encoding this repo's enum, and the boolean stays right if the enum
   grows. A frontend that renders both with the same tick has reintroduced the defect.
3. **Naming.** Not `assumed_configured`: configuration is MEASURED (the broker really resolved
   those vars); what is assumed is liveness. `configured_unverified` states both halves in the
   order they are known, and `unverified` is already this repo's word for an unproven claim
   (`trains_on_customer_data: unverified`, the capability honesty ladder).
4. **`not_supported` is reachable, but no built-in reports it.** "Every declared requirement
   resolves" is a real statement about a backend with an API key and a vacuous one about a
   backend that declares no credentials and no config; reporting both as `configured_unverified`
   would overstate the second. The only built-ins that declare nothing (`pymupdf`, `tesseract`)
   implement a `local` probe, so they are measured; the state exists for the third-party adapter
   with no probe and no declarations, and a fake adapter in the tests keeps it reachable.

The adapter protocol
--------------------
`AdapterProtocol` (8 methods, `@runtime_checkable`) is untouched. The probe is an optional 9th
method behind its own Protocol, `openreading.adapters.base.LivenessProbeAdapter`, following the
`NativeBatchAdapter` precedent exactly (D-v7-1):

    def probe_liveness(self, ctx: RunContext, *, timeout_s: float) -> ProbeResult: ...

`BackendAdapter` supplies a default returning `ProbeResult.unsupported()`, so every existing
subclass works unedited. Dispatch rule: call the probe only when `descriptor.liveness.probe !=
"none"` AND `isinstance(adapter, LivenessProbeAdapter)` — the descriptor is the authority on
WHETHER, the isinstance check is the safety net for an author who declares a probe and forgets to
implement it. A declared probe that still returns `unsupported` (descriptor ahead of the code)
falls back to the inference rather than reporting a measurement that never happened.

What the method may and may not do:

- MAY do network I/O — the one method in the codebase for which that is the point.
- MUST honor `timeout_s` and MUST NOT hang the caller; `probe_http` enforces this for HTTP probes
  and a hand-rolled probe must do the same (an unbounded probe parks a server worker thread).
- MUST NOT send any document, page or caller content. The signature has no `OpenReadingRequest`
  BY CONSTRUCTION, so there is nothing to forward by accident.
- MUST NOT be billable (see "Never billed" below).
- MUST NOT return a secret in any field; the platform redacts on the way out, but the adapter is
  the first line.
- Returns `ProbeResult` (outcome, detail, version — only what it observed). It never builds the
  `LivenessReport` and never decides `not_configured` vs `configured_unverified`; that is the
  platform's ladder. Same division `report_cost` uses: the adapter projects, the platform composes.

The descriptor declaration — `AdapterDescriptor.liveness: LivenessProbe | None`; absent => no
probe, exactly as absent `batch` => platform batching. `LivenessProbe` is `extra="forbid"` and the
schema block is `additionalProperties: false`, so an unknown key in a descriptor's `liveness` block
is rejected rather than silently ignored (a misspelt `timeout` would otherwise ship as "no bound
declared"):

    probe:     "none" | "local" | "endpoint" | "vendor"   (default "none")
    method:    str | None    # "GET /health", "GET /v1/models", "import fitz"
    timeout_s: float | None  # the adapter's own recommended bound
    notes:     str | None    # rate-limit caveats and the like

Schema: `adapter-descriptor.v0.5.json` = v0.4 + this optional block, purely additive (every v0.4
descriptor stays valid). A new FILE rather than an edit because v0.4 is byte-frozen — and not
because v0.4 would reject the block: its root has no `additionalProperties: false`, so a v0.4
descriptor carrying `liveness` already validates. v0.5 exists so the optional property is
DOCUMENTED as available, not merely tolerated.

The kind is decision-relevant, not trivia — it is what an operator needs before clicking a button
that may cost something:

    kind      I/O                         leaves my infrastructure?  example
    none      none                        —                          the default
    local     in-process / subprocess     no                         pymupdf, tesseract
    endpoint  network, to infra YOU run   no                         docling, qwen-vl
    vendor    network, to a third party   YES                        anthropic-claude

A `local` probe is free and instant; a `vendor` probe may count against a rate limit and a UI
should say so before firing it. One boolean would throw away the only fact that separates them.

Redaction: `detail` is generated text and is treated as hostile. Every report leaves this module
through `credentials.redact(detail, secret_values(descriptor, credentials))` — the machinery
`readiness.auth_hinted` already applies at the execution boundary. Independently of that: no probe
ever echoes a provider's response body (providers have been seen echoing the rejected key back
inside it). For `unauthorized`, the platform fills an EMPTY detail with
`readiness.auth_rejected_hint`, the existing key-free sentence — a default, not an override: the
three built-in probes that can observe a rejected credential (docling and qwen-vl via `probe_http`,
anthropic-claude in its own adapter) all return `ProbeResult.unauthorized()` with no detail, so
they all get the hint; the two `local` probes (pymupdf, tesseract) have no credential to reject
and only ever report `live` or `unreachable`. A third-party probe's own detail wins (and is still
redacted). No report carries an endpoint
URL (a URL can embed `user:token@host`). Reports name the env VAR, never the value — the posture
`openreading.credentials` and the Backends page hold.

Never billed — which backends get a probe (D-v7-4)
--------------------------------------------------
A liveness probe MUST NOT be a billed request. Spending the caller's money on a diagnostic they
did not ask to be charged for is worse than not answering; a vendor with no free liveness call
gets the `configured_unverified` inference — that inference is the whole reason the ladder has a
slot for it, since otherwise "no free probe" would render as "cannot be tested", which is useless
and, for a backend whose key is sitting in the environment, misleading. Deliberately NOT a
`billable: bool` descriptor field: that would legitimise the thing the rule forbids, and a field
whose only honest value is `false` is not a field.

Implemented (5):

    docling           endpoint  GET {DOCLING_SERVE_URL}/health   the container's own route
    qwen-vl           endpoint  GET {QWEN_VL_ENDPOINT}/models    OpenAI-compatible list (endpoint
                                                                 already ends in /v1); served
                                                                 model id -> `version`
    pymupdf           local     import fitz + version            in-process, instant
    tesseract         local     the runner's own version()       subprocess, the tesseract binary
    anthropic-claude  vendor    client.models.list()             documented non-billing endpoint
                                (GET /v1/models)                 on the SDK already depended on

Deliberately unsupported (8): aws-textract, azure-document-intelligence, google-document-ai,
chunkr, pulse, nuextract, open-ocr, reducto. No free liveness call is verifiable from a primary
source (a guessed URL would be a fabricated descriptor value — the failure the honesty ladders
exist to prevent), and the build forbids the real vendor call that would verify one, so a probe
that cannot be run cannot be honestly graded. Adding one later is contained and additive: a
descriptor `liveness` block, one method, one offline fault test, one live test (recipe in
`the openreading.adapters docstring (src/openreading/adapters/__init__.py)`).

The report — `liveness-report.v0.1.json`
----------------------------------------
A new schema family, not an edit to an existing one (the leaderboard-report precedent): nothing
about the request/response contract changes, so no consumer, fixture or adapter is affected.
`openreading.types.liveness.LivenessReport` mirrors it and is round-trip tested against the
vendored file. `additionalProperties: false`; SIX fields are required — `schema_version`,
`backend`, `status`, `measured`, `probe`, `checked_at` — and a consumer building or validating the
shape must include all six:

    schema_version  const "0.1" — in-band family version (Canon C12): this object crosses HTTP
                    to consumers this repo does not control, so it says what it is
    backend         registry slug
    status          the 7-value enum above
    measured        did a real round trip happen?
    probe           the static kind, echoed so consumers need no join
    checked_at      when THIS report was produced (UTC, ISO-8601 "Z")
    latency_ms      null unless measured; never negative
    detail          short, redacted, key-free human sentence (optional, default "")
    version         backend-reported version/model, when the probe yields one (optional)

HTTP — `POST /v1/backends/{backend_id}/liveness` (D-v7-5)
---------------------------------------------------------
- Not folded into `GET /v1/backends`, which is free, offline, instant and safe and stays so;
  folding a check in would turn one page load into 13 outbound calls. That endpoint gains one
  additive, static key per row, `liveness_probe` (the declared kind) — a descriptor read that
  lets a UI say "cannot be tested" or "this leaves your network" before probing anything.
- POST, because a probe is neither safe nor idempotent: it makes an outbound call, may wake a
  cold container, may spend a vendor rate limit. GET is defined safe and cacheable, so browsers,
  proxies and link prefetchers may issue one speculatively — a crawler could spend the operator's
  rate limit. POST says "this does work", is not prefetched and is not cached.
- One backend per call. A fan-out `POST /v1/liveness` is the "silently probe 13 vendors" defect
  wearing a POST. One per request keeps cost bounded, attributable and cancellable, and the
  caller — not the server — decides what gets touched; a client that wants all issues N explicit
  requests and may stop after any (the CLI does exactly that).
- 200 + a report body for EVERY probe outcome, `unreachable` and `unauthorized` included. A 502
  would conflate "openreading failed to answer" with "openreading successfully determined the
  backend is down"; the second is a successful diagnostic and the body is its result. This is why
  liveness does not route through the server's `_error_envelope` taxonomy mapping.
- Every non-200 is upstream of any probe — no credential resolved, no call emitted:

      401 unauthorized     caller auth is configured (`OPENREADING_API_KEYS`) and the bearer is
                           missing or matches no key — the server-wide auth layer, before routing
      404 unknown_backend  unknown slug
      403 scope_denied     the caller's API-key allow-list excludes this backend
      400 bad_request      `timeout_s` present but not a number

  Scope MUST apply — the endpoint resolves a vendor credential and emits a call to that vendor,
  exactly the "gate before spend" boundary. Ordering inside the endpoint: `make_adapter` (a
  registry lookup that resolves nothing) runs first so an unknown slug is a 404, THEN scope is
  checked — before the body is parsed and before any credential is resolved. `/v1/parse` gates
  scope before it constructs an adapter at all; the invariant the two share is "no credential
  resolved and no vendor touched for a backend the key may not use", not the line order.
- Optional body `{"timeout_s": <float>}`, clamped to [0.1, 30.0]. Absent => the adapter's declared
  `liveness.timeout_s`, else `DEFAULT_PROBE_TIMEOUT_S` (5.0). The ceiling exists so no caller can
  park a server worker thread. Runs in `run_in_threadpool` like every other blocking call, and the
  server validates the emitted report against the schema before sending it.

Diagnostic, never routing input (D-v7-6)
----------------------------------------
Compliance is never relaxed by liveness. Three structural guarantees, none relying on remembering
a rule:

1. A probe carries no caller data — `probe_liveness(ctx, *, timeout_s)` takes no
   `OpenReadingRequest`; there is no document, page or extraction schema for the compliance gate
   to protect. Enforced by the signature, not by discipline.
2. Nothing in routing, compliance or fallback reads a `LivenessReport`. No module under
   `router/`, `strategies/`, `comparison/`, `evals/` or `batch/` imports this one (pinned by a
   test), so the compliance-eligible set is computed from descriptors exactly as before and
   liveness cannot widen it: a backend the gate refuses stays refused, whatever its pulse.
3. Liveness is a diagnostic read by operators and UIs, not a capability. Making it routing input
   would be a different feature with a different compliance analysis — explicitly out of scope.

Scope enforcement above is the one existing gate liveness does inherit, because there it is the
caller-authorization question, not the compliance question.

Offline-gate safety
-------------------
`make verify` must never make a network call: nothing reachable from the offline gate calls
`probe_liveness()` on a real adapter. Held by:

- No production call site runs offline — the HTTP endpoint, the CLI verb and the web-UI action
  are the three callers of `check_liveness`, and none is exercised by the gate.
- The one render path that COULD have probed structurally cannot: the ladder is split into
  `infer_liveness()` (network-free; returns None when only a call could answer) and
  `check_liveness()` (that plus the measurement). The web UI's `/backends` page calls
  `infer_liveness` on load; only the explicit POST reaches `check_liveness`. "A page load never
  probes" is a property of the call graph, not a promise.
- Offline tests use fakes and respx: the whole ladder against fake adapters (every state incl.
  `not_supported`; the short-circuit; redaction of a secret planted in a detail AND in a version;
  timeout clamping reaching the adapter; schema round-trip + golden; descriptor v0.5 validity and
  additivity; the 5 real probes through injected clients; the structural pins — 8-member
  `AdapterProtocol`, request-free probe signature, no `router/` import), plus the REAL httpx path
  under respx across every deployment state — refused, DNS failure, connect timeout,
  accept-then-never-answer, TLS failure, 401/403 with a present key, 404/5xx, an unparseable 200,
  204, the timeout actually reaching the client — with no socket opened, not even loopback. The
  same lane also drives the two `endpoint` adapters end-to-end through the REAL client under
  respx (docling unreachable and live; qwen-vl served-model, unauthorized on a gated endpoint,
  error while the model server is still loading), so the adapter-to-`probe_http` wiring is
  proven as well as `probe_http` itself. The two `local` probes are exercised through injected
  runners, never by asserting a binary exists.
  Each ladder row has one test proving it reachable and one proving it is not confused with its
  neighbour. The three surfaces are covered offline too: server tests pin the endpoint shapes, 404
  for an unknown slug, 403 scope-denied-before-any-credential and the `timeout_s` validation; CLI
  tests pin "bare `backends` never probes", `--check` probing only the named slugs and `--check
  all` reaching only backends that declare a probe; UI tests pin the opt-in control, the per-row
  live region and "an inferred state never wears a measured tick".
- Real probes (docling, qwen-vl, anthropic-claude) are `@pytest.mark.live`, gated by
  `skip_unless_creds`, so `make verify-live` skips cleanly without keys. A probe test never goes
  in the offline suite.
- Verified by running the offline suite under a `socket.socket.connect` guard that raises on any
  outbound connection.

Surfaces
--------
- CLI: `openreading backends --check SLUG[,SLUG...]` — a flag on the existing verb, not a second
  surface. Bare `openreading backends` never probes. `--check all` is explicit typed consent to
  probe every backend that declares a probe.
- Web UI: `/backends` gets an opt-in per-row "Check now" (POST, htmx-swapped) — never on load,
  never automatic, never polled; rows declaring no probe render the inferred state instead of a
  button that would do nothing. The liveness cell is its OWN live region per row and the POST
  returns only that row's partial, so a check on one row never re-renders the other twelve (a slow
  vendor probe on one row cannot blank the table). Labels: `Ready` -> `Configured`, `Needs
  credentials` -> `Not
  configured`; a check moves through not configured -> configured -> responding, plus unreachable
  / unauthorized with the detail. The measured/inferred distinction is carried by badge value and
  label text, never by a third semaphore colour (the badge macro's two-colour rule; no new
  tokens); an inferred state never wears a measured tick.

Decisions (internal/decisions/DECISIONS.md)
-------------------------------------------
- D-v7-1 — an optional 9th method behind its own Protocol, never an `AdapterProtocol` member. A
  9th member of a `@runtime_checkable` Protocol fails `isinstance` for every adapter lacking it;
  more importantly liveness is GENUINELY optional, and requiring it would force eight of thirteen
  built-ins into "unsupported" stubs that teach nothing and tempt authors toward a billed probe.
- D-v7-2 — the ladder lives in the platform; adapters report only what they observed. Copying
  the ordering, inference and redaction into every adapter is where they would drift.
- D-v7-3 — inference is a first-class schema state, distinct from measurement (enum plus the
  `measured` boolean), so no frontend can render "configured" as "ready" again.
- D-v7-4 — a probe is never a billed request, and there is no `billable` field because it would
  legitimise the forbidden thing.
- D-v7-5 — POST, one backend per call, always 200 with a report; avoids prefetch-spent rate
  limits, the 13-vendor fan-out, and conflating "backend down" with "API failed".
- D-v7-6 — diagnostic, never routing input; a `LivenessReport` cannot widen the
  compliance-eligible set, and the probe cannot become a data path.

Also rejected: reusing `Health` (an offline, dependency-oriented dataclass with `missing_deps` /
`cold` that `readiness` consumes and that is deliberately not schema-bound; overloading it would
make one object mean "deps import" on one path and "the network answered" on another — the exact
ambiguity this module removes); and keeping `ready`/`not_ready` badge aliases plus
`?filter=not_ready` — every consumer is in-tree and was migrated, so they would define a
vocabulary nothing emits. Where a shape's only consumers are in-tree, prefer the cleaner contract;
D-v7-1 is the one place the answer went the other way, on merit rather than compatibility.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from openreading.credentials import (
    EnvCredentialBroker,
    build_run_context,
    redact,
    secret_values,
)
from openreading.readiness import auth_rejected_hint, backend_readiness, missing_reason
from openreading.types.descriptor import AdapterDescriptor
from openreading.types.liveness import (
    LivenessReport,
    LivenessStatus,
    ProbeKind,
    ProbeOutcome,
    ProbeResult,
    is_measured,
)
from openreading.types.request import OpenReadingRequest
from openreading.types.runtime import RunContext

# A probe is a diagnostic, not a workload: five seconds is long enough for a health endpoint on a
# loopback container or a hosted models list, and short enough that a UI's "Check now" never feels
# hung. An adapter may recommend its own via `descriptor.liveness.timeout_s`; a caller may pass its
# own — clamped to [MIN, MAX] so nobody can park a server worker thread indefinitely.
DEFAULT_PROBE_TIMEOUT_S = 5.0
MIN_PROBE_TIMEOUT_S = 0.1
MAX_PROBE_TIMEOUT_S = 30.0

_OUTCOME_TO_STATUS = {
    ProbeOutcome.LIVE: LivenessStatus.LIVE,
    ProbeOutcome.UNREACHABLE: LivenessStatus.UNREACHABLE,
    ProbeOutcome.UNAUTHORIZED: LivenessStatus.UNAUTHORIZED,
    ProbeOutcome.ERROR: LivenessStatus.ERROR,
}


def probe_kind(descriptor: AdapterDescriptor) -> ProbeKind:
    """The backend's STATIC probe declaration — readable offline, with no call. This is what lets
    `GET /v1/backends` and a UI say "cannot be tested" (or "this one leaves your network") without
    probing anything."""
    if descriptor.liveness is None:
        return ProbeKind.NONE
    return ProbeKind(descriptor.liveness.probe)


def resolve_timeout(descriptor: AdapterDescriptor, requested: float | None = None) -> float:
    """The bound this probe actually runs under: the caller's value if given, else the adapter's
    own recommendation, else the default — always clamped. Clamping rather than rejecting keeps a
    silly value from becoming an error the operator has to care about, while still making it
    impossible to hang a worker."""
    declared = descriptor.liveness.timeout_s if descriptor.liveness is not None else None
    value = requested if requested is not None else (declared or DEFAULT_PROBE_TIMEOUT_S)
    return max(MIN_PROBE_TIMEOUT_S, min(MAX_PROBE_TIMEOUT_S, float(value)))


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def probe_request(slug: str) -> OpenReadingRequest:
    """A minimal request the broker can resolve credentials/config off. Deliberately identical in
    shape to `readiness._probe_request` — and it carries NO document content, because a liveness
    check never has one to carry (see the module docstring: a probe can never become a data
    path)."""
    return OpenReadingRequest.model_validate(
        {"document": {"path": "/probe"}, "backend": {"id": slug}}
    )


def _report(
    descriptor: AdapterDescriptor,
    status: LivenessStatus,
    *,
    detail: str = "",
    latency_ms: float | None = None,
    version: str | None = None,
    secrets: set[str] | None = None,
) -> LivenessReport:
    """Build the report, enforcing the two invariants nothing downstream should have to re-check:
    `measured` agrees with the status, and an INFERRED status never carries a latency. Every
    detail passes through `redact` on the way out — the adapter is the first line, this is the
    second (the same defense-in-depth `readiness.auth_hinted` applies at the execution boundary)."""
    measured = is_measured(status)
    if secrets:
        detail = redact(detail, secrets)
        version = redact(version, secrets) if version else version
    return LivenessReport(
        backend=descriptor.id,
        status=status,
        measured=measured,
        probe=probe_kind(descriptor),
        checked_at=_now_iso(),
        latency_ms=latency_ms if measured else None,
        detail=detail,
        version=version,
    )


def infer_liveness(adapter, *, broker: EnvCredentialBroker | None = None) -> LivenessReport | None:
    """The INFERENCE half of the ladder — **guaranteed network-free**. Returns a report when
    configuration alone settles the question (`not_configured`, `configured_unverified`,
    `not_supported`), or None when the backend declares a probe and only a real call can answer.

    This exists as its own function so "a page load never probes" is structurally true rather than
    a promise in a comment: the web UI's `/backends` handler calls THIS on render, and can only
    reach a probe through the explicit, user-initiated POST. `check_liveness` is exactly this plus
    the measurement.
    """
    from openreading.adapters.base import LivenessProbeAdapter

    broker = broker or EnvCredentialBroker()
    descriptor: AdapterDescriptor = adapter.descriptor
    req = probe_request(descriptor.id)
    ctx: RunContext = build_run_context(req, descriptor, broker=broker)
    secrets = secret_values(descriptor, ctx.credentials)

    # Rule 1 — the known negative, and it is READINESS's own judgement rather than a second
    # re-derivation of it. Checked BEFORE any probe so an unconfigured backend never costs a
    # network call to tell us what the environment already did.
    readiness = backend_readiness(adapter, broker=broker)
    if not readiness.ready:
        missing = missing_reason(readiness)
        detail = (
            f"not configured — set {', '.join(missing)}"
            if missing
            else "not configured on this machine"
        )
        return _report(descriptor, LivenessStatus.NOT_CONFIGURED, detail=detail, secrets=secrets)

    # No probe declared (or declared but not implemented): infer from configuration, and say
    # plainly that it is an inference.
    if probe_kind(descriptor) is ProbeKind.NONE or not isinstance(adapter, LivenessProbeAdapter):
        return _inferred(descriptor, secrets)
    return None  # only a real call can answer this one


def check_liveness(
    adapter,
    *,
    broker: EnvCredentialBroker | None = None,
    timeout_s: float | None = None,
) -> LivenessReport:
    """One backend's liveness answer — the single entry point every surface (HTTP, CLI, web UI)
    calls, so the ladder is applied identically everywhere.

    MAY perform network I/O when the backend declares a probe. Never call this from a code path
    `make verify` reaches.
    """
    broker = broker or EnvCredentialBroker()
    descriptor: AdapterDescriptor = adapter.descriptor

    inferred = infer_liveness(adapter, broker=broker)
    if inferred is not None:
        return inferred

    req = probe_request(descriptor.id)
    ctx: RunContext = build_run_context(req, descriptor, broker=broker)
    secrets = secret_values(descriptor, ctx.credentials)

    # Rule 2 — measure.
    bound = resolve_timeout(descriptor, timeout_s)
    started = time.monotonic()
    try:
        result = adapter.probe_liveness(ctx, timeout_s=bound)
    except Exception as e:  # noqa: BLE001 — a probe that raises is an `error` FINDING, never a
        # crash. This is a diagnostic surface: "the probe blew up" is itself information the
        # operator needs, and a 500 would tell them less than a report does.
        elapsed = (time.monotonic() - started) * 1000.0
        return _report(
            descriptor,
            LivenessStatus.ERROR,
            detail=f"probe failed: {type(e).__name__}: {e}",
            latency_ms=elapsed,
            secrets=secrets,
        )
    elapsed = (time.monotonic() - started) * 1000.0

    # An adapter whose descriptor declares a probe but whose code returns UNSUPPORTED (it inherited
    # the ABC default — the descriptor is ahead of the implementation) falls back to the inference
    # rather than reporting a measurement that never happened.
    if result.outcome is ProbeOutcome.UNSUPPORTED:
        return _inferred(descriptor, secrets)

    status = _OUTCOME_TO_STATUS[result.outcome]
    detail = result.detail
    if status is LivenessStatus.UNAUTHORIZED and not detail:
        # Reuse the one key-free, actionable sentence `openreading.credentials` already promises,
        # rather than inventing a second vocabulary for the same event.
        detail = auth_rejected_hint(descriptor.id, descriptor)
    return _report(
        descriptor,
        status,
        detail=detail,
        latency_ms=elapsed,
        version=result.version,
        secrets=secrets,
    )


def _inferred(descriptor: AdapterDescriptor, secrets: set[str]) -> LivenessReport:
    """The no-probe branch. Every declared requirement resolved (rule 1 already returned
    otherwise), so we infer the backend *would* work — and label it as an inference.

    A backend that declares NOTHING has nothing to infer FROM: "every declared requirement
    resolves" is a real statement about a backend with an API key and a vacuous one about a backend
    with no declarations at all. Reporting both as `configured_unverified` would overstate the
    second, so the vacuous case declines to guess and reports `not_supported`."""
    if not (descriptor.credentials_spec or descriptor.config_spec):
        return _report(
            descriptor,
            LivenessStatus.NOT_SUPPORTED,
            detail=(
                "cannot be tested — this backend implements no liveness probe and declares no "
                "credentials or configuration to infer from"
            ),
            secrets=secrets,
        )
    return _report(
        descriptor,
        LivenessStatus.CONFIGURED_UNVERIFIED,
        # Kept short deliberately: this sentence renders inside a table cell, and a longer one
        # widened the column into its neighbour. The badge already says "configured · not
        # verifiable"; this adds the WHY and the fact that nothing was called.
        detail="no free liveness check — inferred from your environment, not measured",
        secrets=secrets,
    )


# --------------------------------------------------------------------------- shared HTTP probe


def probe_http(
    url: str,
    *,
    timeout_s: float,
    headers: dict | None = None,
    env_hint: str | None = None,
    on_ok: Callable[[Any], ProbeResult] | None = None,
    client: Any | None = None,
) -> ProbeResult:
    """The shared HTTP liveness probe: GET `url` and map the outcome onto a `ProbeResult`.

    One helper rather than N copies, because the failure taxonomy is the interesting part and it
    must be identical everywhere: a connect/read/timeout failure is `unreachable` (nothing
    answered), 401/403 is `unauthorized` (something answered and said no), any other non-2xx is
    `error` (something answered, but not in a way that proves health). **Never raises** — a probe
    reports, it does not throw.

    `env_hint` is the env var NAME to point at in the detail. The URL itself is deliberately never
    put in the detail: a URL can embed credentials, and env var names are what every other surface
    already shows (`openreading.credentials`). `on_ok` lets a probe read something useful out of a
    healthy response (e.g. a served model id → `version`). `client` is the seam an adapter's own
    unit tests inject; the branch below it — the REAL httpx client — is not `# pragma: no cover`
    like the other `_Httpx*` clients in this tree, because `tests/test_liveness_http.py` drives it
    under respx across every deployment state (refused, DNS failure, connect/read timeout, TLS
    failure, 401/403, 5xx, an unparseable 200) with no socket opened. The timeout wiring in
    particular is worth covering here rather than deferring to the live lane: an unbounded probe
    parks a server worker thread, and that is not something to discover in production.
    """
    where = f" ({env_hint})" if env_hint else ""
    try:
        if client is not None:
            response = client.get(url, headers=headers or {})
        else:
            import httpx

            # One timeout value covers connect AND read: a backend that accepts the connection and
            # then never answers must fail on the same bound as one that refuses outright.
            with httpx.Client(timeout=timeout_s) as http:
                response = http.get(url, headers=headers or {})
    except Exception as e:  # noqa: BLE001 — connect refused, DNS failure, TLS failure, or the
        # timeout elapsing: from the operator's point of view these are one fact, "nothing
        # answered", and splitting them would be detail without a decision attached.
        return ProbeResult.unreachable(
            f"no response{where} within {timeout_s:g}s ({type(e).__name__})"
        )
    status = getattr(response, "status_code", 0)
    if status in (401, 403):
        return ProbeResult.unauthorized()  # detail filled by check_liveness's key-free hint
    if status >= 400:
        return ProbeResult.error(f"answered HTTP {status}{where}, which does not prove health")
    if on_ok is not None:
        return on_ok(response)
    return ProbeResult.live(f"responding{where}")
