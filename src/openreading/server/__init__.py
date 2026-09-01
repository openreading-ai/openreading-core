"""OpenReading HTTP API (`openreading serve`). `from openreading.server import create_app`.

The same one request shape and one response schema as the CLI and `openreading.api`, over HTTP.
`POST /v1/parse` speaks the vendored request/response JSON Schemas (`openreading.schemas`) in
both directions; the control-plane endpoints return the small JSON shapes listed below. The app
is built in `openreading.server.app`, which imports fastapi at MODULE level (DECISIONS D-v2-8.1)
and which this package imports eagerly, so `import openreading.server` needs the `[server]`
extra; uvicorn is imported lazily, inside the CLI's `serve` command, never by
this package. `import openreading` requires neither.

Run
---
    pip install 'openreading[server]'            # fastapi + uvicorn only
    pip install 'openreading[reducto]'           # plus each backend extra you will call
    openreading serve                            # binds 127.0.0.1:8787
    openreading serve --host 0.0.0.0 --port 80   # exposes it; prints a warning (see Security)
    openreading serve --env-file prod.env        # credentials from a .env; never overrides set env
    openreading serve --cors-origin https://app.example.com   # opt-in CORS, repeatable
    make serve-smoke   # boots on an ephemeral port, POSTs the sample PDF, asserts schema-valid

Credentials are read from the SERVER PROCESS ENVIRONMENT through the same broker as the CLI
(`REDUCTO_API_KEY`, the `AWS_*` chain, ...; `openreading.credentials`). Keys never travel in a
request body. Every deployment env var, with its unset behaviour, is listed on
`openreading.server.app`.

Endpoints
---------
POST /v1/parse
    Body: vendored request; `backend.id` names a backend, or "auto" runs the compliance-first
    router and executes the chain (add `compliance` {require_baa, no_train_on_data, data_region,
    require_local, max_retention} and/or `routing.optimize_for`; fallbacks land in `warnings[]`).
    Optional extra key `keep_candidates: true` keeps a strategy run's discarded branches under
    `orchestration.candidates[]` (popped before schema validation; no effect on a direct run).
    Response: vendored response.
    "auto" runs are idempotent for 15 minutes: one bounded in-process cache (256 entries) per app
    replays the stored response and adds an `idempotent_replay` warning naming the backend
    instead of paying again. Key = document CONTENT (inline `bytes_base64`, or a local `path`'s
    realpath, size and mtime) + backend + version pin + result-affecting options; credentials
    never enter it. `url` / `file_id` documents a backend ingests natively are never cached (the
    bytes behind an address can change); the cache dies with the process. Named-backend runs,
    /v1/batch and strategy runs are never replayed (DECISIONS D-v3-3: a replayed batch item would
    sum an unbilled `usage.cost_usd` into the summary; silent memoization inside a library call
    is a footgun, so the server owns the only cache).
POST /v1/route
    Body: vendored request. Response: the plan only, nothing executed —
    {"chosen", "fallbacks": [...], "dropped": {id: {"stage", "code", "reason"}}, "terminal_reason"}.
POST /v1/compare
    Body: {"responses": [<response>, ...], "baseline"?, "truth"?}. Response: comparison report
    (`comparison-report.v0.2.json`). Pure — compares already-computed envelopes, never executes
    a backend. `baseline` is a subject label or an extra response; `truth` an evals `expected`
    dict. Malformed body or fewer than two valid responses → 400. Two batch envelopes compare
    the same way (corpus report). See `openreading.comparison`.
    Limit (M2): `responses` longer than `server.app._MAX_COMPARE_RESPONSES` (50) is REJECTED with
    400 naming count and limit, checked before the comparison engine runs — its pairwise
    `SequenceMatcher` diff is O(n²) in the response count, the same unauthenticated-caller
    CPU-amplification shape `/v1/batch`'s MAX_BATCH_DOCUMENTS already guards against. Constant,
    not an env knob: nobody legitimately compares more responses than there are backends. The
    count is only half the bound, because the cost is quadratic in each response's TEXT as well as
    in how many there are: a body over `OPENREADING_MAX_COMPARE_BYTES` (default 8 MB) is refused
    with 400 before it is even parsed. Refused, never truncated — whatever is accepted is compared
    in full.
POST /v1/batch
    Body: {"documents": [<request.document>, ...], "backend"?, "jobs"?} plus the shared fields
    `outputs`, `extraction_schema`, `features`, `pages`, `compliance`, applied to every item.
    Each document is a `request.document` shape: `bytes_base64`, `url` or `file_id`.
    Response: batch-result envelope (`batch-result.v0.1.json`) — per-item succeeded / failed /
    skipped (a succeeded item carries a full `response`) and a `summary` with real `duration_ms`
    (never a fabricated 0), cost with honest bases, and a per-backend tally.
    Always dispatches through the pooled, timed platform runner (`openreading.batch.runner
    .run_batch`); the CLI/Python batch path shares its per-item semantics (pooled, timed,
    per-item isolation) and envelope shape but may take a native-batch fast path this endpoint
    never takes. `backend` defaults to "auto" (routed per item). `jobs` (default 1) bounds a real
    worker-thread pool and is echoed on `request.jobs`. Directory expansion is client-side; the
    server takes explicit documents. A per-item failure never aborts the batch: 200 even when
    `status.state` is `partial`; `documents: []` → 200 with one `empty_batch` warning and
    `summary.total == 0`, mirroring the CLI's empty-directory behaviour. Unknown named backend →
    404; non-list `documents`, non-integer `jobs`, any unrecognised body key (`max_jobs`
    included), or a non-string `backend` → 400. That last one is the shape trap: `backend` here is
    ONE string shared by every item, while /v1/parse and the vendored request schema take the
    object `{"id": ...}`, so a client reusing its own /v1/parse body builder sends the object and
    is refused by name. It is refused rather than reduced to its `id` because the object also
    carries `operation`, `version`, `credentials_ref` and `runtime` — keeping only the slug would
    silently run a different operation than the caller asked for.
    Limits: `jobs` < 1 is clamped to 1 (no legitimate intent behind a non-positive count);
    `jobs` > MAX_BATCH_JOBS (32, a real ThreadPoolExecutor size) and `documents` longer than
    MAX_BATCH_DOCUMENTS (200 = `batch.sources.DEFAULT_MAX_ITEMS`, the CLI directory-expansion
    default, shared so the two cannot drift) are REJECTED with 400 naming count and limit. Both
    are resource-exhaustion primitives reachable by an unauthenticated caller (a pool that size;
    that many request builds and, per `url` document, a blocking fetch). Neither ceiling takes a
    caller-facing override here — the body IS the untrusted boundary — unlike the CLI's
    `--max-jobs` / `--max-items` and Python `max_jobs=` / `max_items=`, whose local caller can
    already spend local resources any other way.
POST /v1/jobs  /  GET /v1/jobs/{job_id}  /  DELETE /v1/jobs/{job_id}
    Async submit / poll / discard for a NAMED backend ("auto" and "strategy:none" → 400; a real
    `strategy:<name>` id is wrapped as one synthetic job that runs the whole walk inline).
    Body: vendored request. Submit and GET both return the job handle:
    {"job_id", "state": running|succeeded|failed, "backend", "created_ms", "response"?, "error"?}.
    A failed job's `error` is the SAME envelope /v1/parse returns for the identical failure,
    whichever leg failed (submit, a later poll, webhook resolution); only the HTTP status differs:
    a submit-time failure returns its mapped status, GET is always 200 — the fetch succeeded, the
    JOB failed, and `state` says so. The store is in-memory / per-process: a multi-process
    deployment needs a shared store, there is no server-side resume, and `OPENREADING_LEDGER`
    (which arms CLI/Python `parse` / `resume`) does not extend to it (internal/design/ledger.md
    §10). Without a `webhook_url` a job runs in POLL mode, driven one slice per GET.
    Bounded retention (M4): a record holds the SLIM request — `document.bytes_base64`,
    `document.password`, `document.url` and `async.webhook_url` stripped, the same exclusion the
    ledger applies before persisting anything — so an in-flight job does not pin its document, or
    its password, in process memory for as long as it runs. What remains still grows with the
    number of jobs, so the store is bounded twice over: a TERMINAL record older than
    `OPENREADING_JOB_TTL_S`
    seconds (default 3600), measured from its own submit time, is deleted the next time ANY
    submit or GET touches the store — lazily, since this process has no scheduler thread; a
    still-running record is never swept, regardless of age. A submit at or over
    `OPENREADING_MAX_ASYNC_JOBS` (default 1000) total records is refused with 429 before its body
    is even parsed, as is one from a key already holding `OPENREADING_MAX_JOBS_PER_PRINCIPAL`
    (default 100) of them — the global cap alone is one shared counter, so without the per-key
    allowance the caller who fills it denies the endpoint to every other caller. `DELETE /v1/jobs/{job_id}` removes one record immediately and unconditionally
    — 204, whatever its state — freeing a slot without waiting on the TTL; an unknown id is 404
    `unknown_job`, the identical envelope GET's own unknown-id case returns.
POST /v1/webhooks/{backend_id}
    Inbound provider webhook; resolves the job it names and, once terminal, meters it like
    /v1/parse. Response: the job handle. Verification is per-backend, gated on whether the
    backend declares a `webhook_secret` credential at all — today reducto alone (Svix, secret
    from `REDUCTO_WEBHOOK_SECRET`): invalid signature → 401, and so is a MISSING secret — fail
    closed rather than trust a possibly forged vendor result. chunkr and open-ocr offer no
    signature mechanism at all, so their callbacks are authenticated by a PER-JOB CALLBACK TOKEN
    instead (M5): on submit the server generates one, appends it to the `webhook_url` it registers
    with the vendor as `?ort=…`, and keeps it on the job record alone — `slim_request` nulls
    `webhook_url`, so the URL that carried it is retained nowhere. An event for one of those
    backends that cannot present the matching token is 401. Without this the vendor's own task id
    was the only thing standing between a stranger and a forged completion, and that id travels in
    URLs and logs. The token is the server's to choose, never the caller's: a caller-picked value
    is one another caller could guess. `OPENREADING_ALLOW_UNSIGNED_WEBHOOKS=1` restores the old
    behaviour for a vendor that strips query parameters from the URL it was handed, and accepts
    forgeable completions in doing so. The lookup is scoped either way: only records for the
    URL's `{backend_id}` that are themselves waiting in WEBHOOK mode are considered, so an event
    can never settle another backend's job nor any POLL-mode job. The id is read from the field
    each vendor actually uses (`job_id` reducto, `task_id` chunkr, `request_id` open-ocr). The
    callback is always caller-supplied on the submit request —
    `"async": {"mode": "async", "webhook_url": "https://host/v1/webhooks/reducto"}` — the server
    never invents one, only appends its token to it; the CLI/Python API only ever use POLL, so
    nothing there depends on inbound reachability.
POST /v1/backends/{backend_id}/liveness
    Is this backend actually ANSWERING? Optional body {"timeout_s": <float>}, clamped to
    [0.1, 30]. Response: liveness report {"schema_version", "backend", "status", "measured",
    "probe", "checked_at", "latency_ms", "detail", "version"}. `status` is a seven-state ladder:
    not_configured → configured_unverified → live, with unreachable / unauthorized / error as the
    measured negatives and not_supported as the floor. `measured` matters most to a client: true
    = a round trip happened and this is a measurement; false = inferred from local configuration
    (`latency_ms` null). A UI that renders `configured_unverified` like `live` is lying.
    POST, not GET, and one backend per call (DECISIONS D-v7-5): a probe is neither safe nor
    idempotent — outbound call, may wake a cold container, may spend a vendor rate limit — and
    GET is safe/cacheable, so a browser, proxy or prefetcher could issue one unasked; a fan-out
    endpoint is the same defect wearing a POST. Always 200 with a report, even `unreachable` /
    `unauthorized`: "the backend is down" is a successful diagnostic, and a 5xx would conflate it
    with this API failing. The only non-200s, all before any probe: 404 unknown backend, 403
    scope_denied, 400 non-numeric `timeout_s`. A probe is never a billed request (D-v7-4): a
    vendor with no free liveness call declares no probe and reports `configured_unverified`
    instead of spending money to answer.
    Design and full state table: internal/design/liveness.md; implementation
    `openreading.liveness`.
GET /v1/backends
    Readiness for every backend (the `openreading backends` view). Free, offline, instant; never
    probes. Response: [{"slug", "type", "extra_installed", "creds_found", "creds_missing",
    "ready", "liveness_probe"}, ...]. `ready` means CONFIGURED (deps import, declared env
    resolves), never reachable. `liveness_probe` is the static declaration none | local |
    endpoint | vendor, so a client can render "cannot be tested" or warn that a check leaves the
    network without probing; only `vendor` leaves your infrastructure.
GET /healthz
    {"status": "ok", "version": "<openreading version>"}. Reachable unauthenticated, always.

HTTP status codes
-----------------
200  success — including GET /v1/jobs/{id} for a FAILED job (`state` / `error` carry the failure)
204  DELETE /v1/jobs/{id} removed the record — empty body, whatever the job's state was
400  body not valid JSON, fails the request schema, or names a `strategy:<name>` that is neither
     a preset nor defined by the loaded config (`unknown_strategy`; presets run configless)
401  webhook signature invalid, or the backend declares `webhook_secret` and none is configured
     (`bad_signature`); or caller auth is on and the request carries no `Authorization` header,
     or a bearer matching no configured key (`unauthorized`)
403  compliance refused / no eligible backend (`compliance_refused`); or the matched API key's
     allow-list excludes the backend named directly, or the subtraction leaves nothing an
     "auto"/"strategy:none" request's fallback chain or a `strategy:<name>` walk could still
     reach (`scope_denied`) — an out-of-scope top pick is rerouted to an in-scope fallback, not
     refused, so this fires only when no in-scope backend is left
404  unknown backend id / unknown job id (GET or DELETE)
413  document exceeds the size limit (`doc_too_large`); OR (M2) the request BODY itself exceeds
     `OPENREADING_MAX_BODY_BYTES` — `server.app._BodyLimitMiddleware` answers this one straight
     off the transport, before routing or body parsing, so it carries the same envelope shape and
     `doc_too_large` backend_code but never the request-specific detail the parsed-document case
     can give
422  requested feature the backend cannot produce (`unsupported_feature`)
424  a directly-named backend is missing credentials (`missing_credentials`; `missing_env[]`
     names them) or its key was found and REJECTED by the provider (`auth_rejected`; the
     message names the var to check, never the key)
429  POST /v1/jobs at or over `OPENREADING_MAX_ASYNC_JOBS` (`rate_limited`) — the job store's own
     capacity bound, refused before the body is parsed; not a per-caller request-rate throttle
502  plan exhausted — every backend failed (`plan_exhausted`, `trail`) — or other terminal error,
     which INCLUDES two permanent request-shape refusals a client must not retry:
     `credentials_ref_alias_not_allowed` and `endpoint_not_request_configurable`. Read
     `backend_code` before deciding a 502 is a server outage
504  deadline exceeded, retryables exhausted (`retryable_exhausted`)
500  an unhandled server error. This is the ONE status that does not carry the error body below:
     the ASGI framework returns the plain text `Internal Server Error`, so a client parsing every
     response as the envelope crashes on exactly the response it least expected. Branch on the
     status before parsing, and treat a 500 as a bug to report rather than a caller-side
     condition to handle — every condition this server knows about has a coded row above.

Error body — also the shape of a failed job's `error` (`missing_env` only for missing
credentials, `trail` only for `plan_exhausted`):
    {"error": {"category": "terminal", "message": "...", "backend_code": "missing_credentials",
               "missing_env": ["REDUCTO_API_KEY"]}}

Timeouts: a directly-named hosted async backend hard-caps at the generic 120 s
`credentials.DEFAULT_DEADLINE_MS` over HTTP — /v1/parse and /v1/jobs have no `deadline_ms` field
or query param to raise it, and the server's handlers originate none. The escape hatches are the
CLI's `parse` / `compare --deadline` and the Python API's `run(deadline_ms=)`. A long job (a large
Textract document) hits 504 before it finishes; the only workaround today is running it via the
CLI or Python.

Security
--------
Caller authentication is opt-in and OFF by default: with no `OPENREADING_API_KEYS` every endpoint
behaves as it always has, and anyone who can reach the server spends your vendor credits. So:
- the default bind is 127.0.0.1; `--host 0.0.0.0` prints a warning — put your own reverse-proxy
  auth / network policy in front, or configure `OPENREADING_API_KEYS`;
- CORS is off unless `--cors-origin` names the origins you trust;
- a fresh adapter instance is built per request, so a credential-bound client never leaks across
  requests (DECISIONS D-v2-8.1).

`document.path` names a file for the SERVER process to open, not the caller — over HTTP that is a
remote file-read primitive, a second and independent risk from the credit-spend one above, and it
is refused by default on every caller-body ingress (`/v1/parse`, `/v1/route`, `/v1/jobs`,
`/v1/batch`): send `bytes_base64` or `url` instead. An operator who needs it sets
`OPENREADING_SERVER_PATH_ROOT=<dir>` to serve files beneath one directory; the check resolves
symlinks before proving containment, so a link inside that directory pointing outside it is
refused the same as a literal `..`, and an accepted file is read there and then rather than
handed onward as a path for a backend to open later — closing the window in which the checked
file could be swapped for a link out of the root (`server.app._gate_document_path`). Reading it
at the gate bounds it by the same ceiling a URL document obeys. This gate is HTTP-only —
the CLI and `openreading.api` still accept `document.path` unchanged, because there the caller and
the machine granting file access are the same trust domain.

Setting `OPENREADING_API_KEYS` (comma-separated bearer tokens) turns auth on: every endpoint
except GET /healthz and POST /v1/webhooks/{backend_id} then rejects a request without a valid
`Authorization: Bearer <token>` with 401. Environment only, read once at startup, never a CLI
flag or a body field — so a token never lands in `ps`, shell history, or anything the
schema/compliance layer sees. Mint one with `secrets.token_urlsafe(32)`.
`OPENREADING_API_KEY_SCOPES` (`token=backend1|backend2`, comma-separated) narrows a listed token
to a backend allow-list, checked BEFORE any adapter is constructed or vendor credential resolved,
so an out-of-scope request never reaches a paid backend. An unlisted token is unscoped. A scope
only ever NARROWS what compliance and routing already allow, and never widens it: scoping a token
to a backend the request's compliance already dropped does not put it back.

The allow-list covers every backend a request can reach, and a request reaches more than one. A
directly named backend is the whole request, so it is a membership test at the door
(`server.app._out_of_scope_backend`). The other two shapes pick their own backends and are bounded
by the same subtraction applied one layer in, before any adapter is built or vendor credential
resolved:

- A plain `auto` / `strategy:none` request is a router PLAN — the chosen backend plus every
  fallback — and `router.executor.execute_plan` walks all of it. `RoutePlan.restrict_to` prunes
  that whole chain to the allow-list, at the door and again in `api.run_request` before execution.
  Checking the chosen backend alone was a live bypass: a token scoped to the local parser was
  refused `tesseract` and `docling` by name and then delivered the document to both, because a
  document the in-scope pick could not parse fell through to them. `/v1/batch` did it once per
  document.
- A `strategy:<name>` walk — and a plain `auto` request that engages one because the config sets
  `defaults.strategy` — carries the allow-list into `strategies.prune.compile_strategy`, which
  drops every out-of-scope rung and narrows the eligible set an `auto` rung resolves against.

Both shapes then run on what is left, the way they already do after a compliance drop; a strategy
additionally records each removed rung as a `scope_denied` entry in `orchestration.dropped`. An
`auto` request whose top pick is out of scope is therefore rerouted, not refused — a scope bounds
what the router may choose from, and only in-scope backends run either way. When the subtraction
leaves nothing at all, the request is refused with the same 403 `scope_denied` a direct call gets,
never a 502 and never a success on nothing.

Each path is re-checked at its own dispatch point, so a bug in either prune cannot silently reopen
it: `router.executor.execute_plan` for the chain, `strategies.engine._resolve_backend` for the
walk. Both refuse rather than skip, because reaching one means a layer above failed.

A malformed value in either var (empty entry, scope for an unlisted key, two scopes for one key,
...) fails startup naming the setting and the entry's position — never its value. Over the CLI
that surfaces as a single `[serve] ...` line on stderr and exit 3, not a traceback. Token
comparison is constant-time (`hmac.compare_digest`).
What this does NOT add: rate limiting, spend accounting, transport encryption. A scoped caller can
still submit a large batch within its backends, and a token over plain HTTP is readable on the
wire — terminate TLS in front of this like any other credential-bearing endpoint.
Deployment-level router knobs (`OPENREADING_ALLOW_UNVERIFIED_COMPLIANCE`,
`OPENREADING_TRAIN_OPTOUT_CONFIRMED`, `OPENREADING_BAA_TIER_CONFIRMED`) come from the environment,
never the request body (DECISIONS D7: the operator attests an account-level fact, not a
per-document one, and the wire schema is `extra=forbid`).
"""

from __future__ import annotations

from openreading.server.app import ServerConfigError, create_app

# ServerConfigError is exported because create_app() raising it IS part of the startup contract:
# `openreading serve` has to catch it to report a malformed deploy setting as a tagged one-line
# error instead of a traceback (openreading.cli.app.cmd_serve).
__all__ = ["ServerConfigError", "create_app"]
