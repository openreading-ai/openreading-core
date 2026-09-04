"""Backend adapters: one package per backend; `base` defines the interface, `registry` the set.

This docstring is the runbook for adding backend N+1. Two adapters (`nuextract`, `open-ocr`)
were built end-to-end with it; every rule below is enforced by the conformance kit
(`openreading.testing.conformance`) or a spec test, so a skipped step is a red `make verify`,
never a silent gap. `make verify` green is the finish line.

The contract in one sentence
----------------------------
An adapter is a static, honest self-description (`AdapterDescriptor`, `protocol_version=2`
required -- no default, no lower value for a new built-in) plus 8 methods --
`health() / capabilities() / submit(req, ctx) / poll(job, ctx) / resolve_webhook(event, job, ctx)
/ cancel(job, ctx) / normalize(job, ctx, slim_req) / report_cost(job)` -- mapping one provider's
API onto the one normalized request/response schema, filling only what the backend truly
produces, warning about what it cannot, and passing the bill straight through to the caller.
Every method that touches a live vendor call (`submit`/`poll`/`resolve_webhook`/`cancel`) takes a
fresh `ctx: RunContext` and builds its own client via `self._get_client(ctx)` -- never a client
bound onto `self` earlier -- so a different, freshly constructed instance (another process, a
resumed job) can drive the job to completion with no instance affinity at all. That property is
what "protocol v2" names and what the kit's R1/R2/R3 checks verify.

Two capabilities are OPTIONAL and sit beside the 8: native batch (`NativeBatchAdapter`) and a
liveness probe (`LivenessProbeAdapter`). Both default to "not supported"; an adapter that ignores
them is complete and correct -- you never have to implement either.

0. Bootstrap: run the generator, then read these files first (in this order)
-----------------------------------------------------------------------------
Run `uv run python scripts/new_adapter.py <slug> --template <existing-slug> --type <type>` -- a
repository-development tool, not a verb on the shipped `openreading` CLI. One run creates every
file in §2's CREATE list and makes five of the seven EDITs. The other two are yours, and the run
report prints HAND next to each, so the mechanical touchpoints cannot land partially. `--type` is
one of the seven shapes the picker table below groups the templates into
(`hosted_api`, `hosted_sync_async`, `hosted_webhook`, `hosted_aggregator`,
`self_hosted_endpoint`, `cloud_sdk`, `in_process`; e.g. `--template chunkr --type hosted_api`);
a template/type pair outside that set is declined, never improvised -- fall back to the manual
walkthrough starting at §1. Every honesty-graded field the generator writes (capabilities,
channels, cost basis, compliance) takes its SAFEST value regardless of the template's own
researched descriptor, because a scaffold has verified nothing; every remaining gap carries a
grep-able scaffold marker (the literal is `MARKER` in `tests/test_scaffold_sentinel.py`, not
repeated here on purpose: that test scans this whole package and fails `make verify` on any line
still carrying it, prose included). The generator changes nothing below this line:
research-first, the honesty ladders and the credentials-vs-config split still apply in full --
it only gets the mechanical fraction of the checklist onto disk correctly on day one, never the
judgment §1 and §3 require.

Do NOT start writing before reading these -- they are short and they ARE the spec:

    adapters/base.py                the 8-method surface + the `assert_supports` guardrail, and
                                    the two optional Protocols
    adapters/_http.py               shared httpx client builder + status -> error-taxonomy map
    types/descriptor.py             every descriptor field and its allowed values
    types/request.py, response.py,  what you consume and what you must emit
      blocks.py
    types/errors.py                 the four-category error taxonomy
    testing/conformance.py          the checks every adapter must pass -- read the assertions,
                                    they are the rules
    ONE template adapter + its      the pattern to copy (picker below)
      two test files

Template picker -- copy the closest shape for your target API:

    hosted API, async task/job + poll, schema extraction,   chunkr/ (+ tests/test_chunkr.py,
      self-host `base_url` override                           tests/test_chunkr_faults.py)
    hosted API, sync-inline + async, dual response shapes   pulse/
    hosted API, webhook with signature verification (svix)  reducto/
    hosted API, temp-project flow, token usage, PARTIAL on  nuextract/
      validation error
    hosted aggregator, INLINE+POLL+WEBHOOK, actual-USD      open_ocr/
      billing
    hosted API with a native multi-document batch endpoint  anthropic_claude/ (§3 Native batch)
    self-hosted model behind an OpenAI-compatible endpoint  qwen_vl/
    self-hosted container speaking HTTP                     docling/
    local library (in-process / subprocess)                 pymupdf/, tesseract/
    cloud-SDK auth (sigv4 / ADC / Entra)                    aws_textract/, google_document_ai/,
                                                            azure_document_intelligence/

1. Research the provider BEFORE writing code
--------------------------------------------
Build from PRIMARY sources (official API docs, an OpenAPI spec, or the vendor's generated SDK --
cloning the SDK repo and grepping its models is often the fastest way to exact wire field names).
Pin down ALL ten; when a fact is unknown, §3 says how to encode "unknown" honestly -- never guess:

 1. Auth: header format, key prefix, env-var convention (also vendor-SDK aliases).
 2. Base URL + whether a self-hosted/on-prem deployment shares the same wire API (-> `base_url`
    ConfigField) or speaks a different protocol (-> separate adapter; note it).
 3. Flow: sync (INLINE), job+poll (POLL), webhook (WEBHOOK) -- and the exact status vocabulary
    (`succeeded`? `completed`? `Succeeded`?) including failure states.
 4. Wire shapes: exact request/response JSON field names incl. casing (`outputTokens` vs
    `output_tokens`). Fixtures and the real client both depend on this.
 5. Input intake: URL? base64 bytes? multipart upload? (drives `accepts_url` + `_input` mapping).
 6. Output channels actually produced: markdown / text / blocks / bbox / per-block confidence /
    typed fields / table cells -- this drives the N/D/X grading.
 7. Pricing + usage counters the response reports (tokens, pages, credits, actual USD).
 8. Compliance posture: BAA? SOC2? explicit no-train statement? retention? A claim that cannot
    be verified from a primary source -> encode fail-closed (§3).
 9. Limits: max pages, max file size, rate limits, retry semantics.
10. Source URLs + access date -- required in `descriptor.sources`.

2. Complete file checklist
--------------------------
Slug is HYPHENATED (`open-ocr`); the package directory is UNDERSCORED (`open_ocr`); the pyproject
extra equals the slug (exception: `aws-textract`'s extra is `textract`, historical -- the
explicit map in `scripts/check_extras_parity.py` is the authority). Files to CREATE:

    src/openreading/adapters/<pkg>/__init__.py   docstring + re-export Adapter & Client Protocol
    src/openreading/adapters/<pkg>/adapter.py    everything else (one file)
    tests/fixtures/<slug>/<op>.json              documented-shape wire fixtures (exact names)
    tests/test_<pkg>.py                          happy path + conformance + live
    tests/test_<pkg>_faults.py                   every branch the happy path skips

Files to EDIT -- each guarded by a test that fails if you forget it:

    adapters/registry.py            import + `BUILTIN_ADAPTERS["<slug>"]` entry. Guard:
                                    everything -- CLI `--backend` choices, server, strategies
                                    all derive from this dict; no other registration point.
    pyproject.toml                  `[project.optional-dependencies]` -> `<slug> =
                                    ["httpx>=0.27"]` (or the SDK dep). Guard: extras-parity
                                    (`scripts/check_extras_parity.py`, wired into
                                    `make verify`) -- fails until the slug has an extra.
    .env.example                    a `# --- <slug> (signup: <url>) ---` block with every env
                                    var. Guard: reviewer eyes.
    openreading.credentials         one line in the module docstring's "Per-backend reference"
                                    (a hand-written list; the generator prints a HAND reminder
                                    and does not edit it). Guard: reviewer eyes.
    src/openreading/adapters/README.md  one row in each of the five Catalog tables (input
                                    formats, install extra and env, compliance, cost and
                                    limits, response channels), every value copied from the
                                    descriptor, plus the pasted `openreading backends` table
                                    re-run. Guard: reviewer eyes.
    tests/test_descriptor_specs.py  `EXPECTED_CRED_KEYS["<slug>"]`,
                                    `EXPECTED_CONFIG_KEYS["<slug>"]`; add the slug to the
                                    `test_accepts_url_only_for_native_url_backends` set IFF
                                    `accepts_url=True`. Guard: parametrized over
                                    `BUILTIN_ADAPTERS` -- a new slug fails until added.
    tests/test_server.py            `test_backends_lists_all_with_readiness` backend count
                                    (`len(rows) == N`). Guard: that test.

3. The adapter file, section by section
---------------------------------------
Module docstring (mandatory, dense) -- the design record; a future agent must be able to
re-derive every descriptor value from it. Record: what the backend is + which ops; the exact
flow (endpoints, poll target, status vocabulary); channel posture (what is emitted, what is X
and why); pricing/usage mapping; credential/env conventions; deliberate non-choices ("the API's
X mode is NOT used because ..."); compliance fail-closed notes; and
`Sources: <urls> (accessed YYYY-MM-DD)`.

Client: a `Protocol` + a real httpx class.

    class FooClient(Protocol):                      # what tests fake
        def create_job(self, body: dict) -> dict: ...
        def get_job(self, job_id: str) -> dict: ...

    class _HttpxFooClient:  # pragma: no cover - real network path
        def __init__(self, api_key: str, base_url: str) -> None:
            from openreading.adapters._http import build_httpx_client   # lazy import!
            self._http = build_httpx_client(base_url=..., headers={...}, timeout=120.0)

- The real client is `# pragma: no cover` -- the offline suite never touches the network.
- On `status >= 400` raise via `_http.error_for_status(status, headers, backend_code=...,
  message=...)`, parsing the provider's error envelope for `backend_code` when it has one.
- httpx (or the SDK) is imported LAZILY: constructing an adapter must never import its runtime
  dep, because the registry instantiates every adapter descriptor-only. DECISIONS D3: an official
  SDK is allowed where signing is non-trivial (boto3 for SigV4, google-cloud-documentai for ADC),
  isolated in that adapter's extra; key-header APIs use httpx. D9: a local-library runtime dep
  (pymupdf, pytesseract) lives in the `dev` group for tests and in its own extra, never in core
  -- so AGPL PyMuPDF can never be pulled in by a core install.

`_descriptor()` -- the honesty rules
- `id` = slug; `type` in `BackendType`; `adapter_impl`; `operations` a subset of
  `{"parse", "extract"}`.
- `wait_modes`: only modes `submit()` can actually return -- conformance asserts membership.
- Capabilities: `"verified"` ONLY after a live run/benchmark by this repo; vendor-doc claims are
  `"claimed"`; absent abilities are `False`. Never grade up.
- Channels (N/D/X): N = backend emits it; D = deterministically derivable from what it emits;
  X = would require fabrication. Graded-X channels must NEVER appear in a response and MUST
  produce a `resp.add_warning(..., field=<channel name>)` whenever the caller requested it
  (`outputs.markdown`/`text`/`blocks` default True; `typed_fields` default False;
  `table_cells` only when `outputs.tables == "cells"`). Conformance enforces both directions and
  reads `field` as the primary signal -- the channel name spelled as whole words in code/message
  is only a fallback, so `field` is what you set.
- Cost: `basis="billed"` only if the response carries the actual charge; `"estimated"` with
  `usd_per_page_equiv_low/high` when projecting from a public price; `"unknown"` (and
  `cost_usd=None` in `report_cost`) when pricing is not public -- NEVER invent a rate.
- Compliance fails closed: no primary-source no-train statement ->
  `trains_on_customer_data="unverified"`; unverified certs -> `False`; BAA only if documented.
  The router drops fail-closed backends under strict policies -- that is the point.
- `credentials_spec` / `config_spec`: every key your code reads from `ctx.credentials.values` /
  `ctx.runtime`, with `env=[...]` in precedence order (service-native var first, vendor-SDK
  aliases after). Secrets stay `secret=True`; endpoints/regions/engine-ids are ConfigFields
  (`secret` not applicable) -- the conformance kit REJECTS a `credentials_spec` field graded
  `secret=False`, so a non-secret in the wrong bucket is a red build. Entirely ambient auth (ADC,
  a boto3 profile) may declare an empty `credentials_spec`. The env broker handles the
  `OPENREADING_<SLUG>_<KEY>` override form automatically -- do not hand-roll env reading.
  DECISIONS D-v2-6.1a: the kit demands a spec whenever the backend is PROVISIONED (`auth != none`
  or an endpoint/container `byo_mode`), not on `runs_fully_local` -- docling/qwen-vl are
  compliance-local yet still need an endpoint URL; the compliance flag and "needs env config"
  are orthogonal. D-v2-6.1b: IAM keypairs and ADC are `byo_mode="cloud_credential"`, not
  `api_key`; `byo_mode` is a coarse hint, `credentials_spec` is the per-key truth.
- `signup_url` (mandatory for `hosted_api`), `accepts_url` (True only for native URL intake),
  `live_gate_env` (the vars gating the live test), `sources` (with access dates).
- `protocol_version`: required, no default, and `2` is the ONLY value a new built-in may declare.
  Every `BUILTIN_ADAPTERS` entry is checked unconditionally
  (`tests/test_protocol_version_guard.py`, no hardcoded allow-list), so an adapter registered at
  `1` fails `make verify` immediately, not later. WHY: the guard keeps the repo from ever
  resting in a half-migrated state -- some adapters resumable, some not, `base.py` carrying two
  signatures, the kit accepting either. (The router's own `PROTOCOL_VERSION_FLOOR` in
  `openreading.router.registry` is a separate, lower-water-mark refusal for a third-party adapter
  installed outside this package; it does not relax the built-in guard.) `2` means:
  `poll`/`resolve_webhook`/`cancel` all take `ctx: RunContext` and build their own client via
  `_get_client(ctx)`; `normalize` takes `(job, ctx, slim_req)`; no adapter attribute other than
  a constructor-injected test client survives past `submit()`. Do NOT declare it on a
  plausible-looking guess -- run the kit with `adapter_factory=` (R1/R2/R3 below) and confirm it
  is green first. There is no partial credit: an adapter not run through R1/R2/R3 is not
  "probably fine at 2", it is undetermined, and undetermined means you have not finished.

The 8 methods -- patterns
- `health()`: injected client -> ready; else try the lazy import and report
  `missing_deps=["httpx (pip install 'openreading[<slug>]')"]`. It answers only "are my deps
  importable here" and is offline by construction (see Liveness for "is it answering").
- `_get_client(ctx)`: the CANONICAL, REQUIRED way any method reaches a vendor client -- not one
  pattern among several. Injected client wins (tests); missing key ->
  `TerminalError(..., backend_code="no_credentials")`; config from `ctx.runtime` with the
  documented default; the line constructing the real client gets `# pragma: no cover`. NEVER
  cache the constructed client onto `self`, anywhere, under any name -- no
  `self._client = client` inside `submit()`/`poll()`, no `self._active_client`. The kit's one
  exemption is the exact attribute name `_client`, set once in `__init__` as the
  constructor-injected test seam (`def __init__(self, client=None): self._client = client`) and
  never reassigned by any of the 8; any OTHER `*_client`-suffixed attribute holding a real
  (non-callable, non-None) object after `submit()` is flagged (`_check_no_cached_client` in
  `openreading.testing.conformance`), so do not reuse the exemption's shape for a second,
  differently-named cache. WHY: before v2 seven adapters carried
  `if client is None:  # pragma: no cover - submit always binds a client first`; that pragma WAS
  the defect -- a fresh instance polling a persisted job raised "no client bound" and the driver
  burned every poll attempt on it before surfacing anything.
- `submit(req, ctx)`: build the provider request from `req` (document intake, op inference
  `req.backend.operation or ("extract" if req.extraction_schema else "parse")`, language/page
  plumbing). `client = self._get_client(ctx)` -- do not bind it to `self`. Wrap provider calls in

      except (RetryableError, TerminalError):
          raise
      except Exception as e:  # noqa: BLE001
          raise self._map_error(e) from e

  INLINE -> return a SUCCEEDED job with `job.raw = RawResult(payload=..., encoding="json",
  media_type="application/json", object_class=<op>)`. POLL/WEBHOOK -> a RUNNING job with
  `backend_job_id`, `poll_handle`, `webhook_token` (webhook only), `next_poll_at = 0.0`. When
  `req.async_.webhook_url` is set it MUST also be forwarded into the vendor's own request body
  (the field name is vendor-specific; `chunkr` and `reducto` show two shapes). Skip this and
  `job.wait_mode` is WEBHOOK locally while the vendor was never told a callback exists: the job
  hangs at "running" forever with no error raised anywhere. Always pass
  `idempotency_key=ctx.idempotency_key` to `new_job` (and to the provider if it supports an
  idempotency header). Any remaining time/attempt budget read from `ctx` (e.g. `ctx.deadline_ms`)
  is resolved with `is not None`, never bare truthiness: `x or default` silently discards a
  legitimate `0` (or a value that reduces to `0` after arithmetic), exactly backwards for a
  "nothing left" signal -- `tesseract/adapter.py`'s deadline -> subprocess-timeout conversion is
  the worked example.
- `poll(job, ctx)`: `client = self._get_client(ctx)` at the top; fetch status; terminal-failure
  vocabulary -> `TerminalError(backend_code=<status>)`; still running ->
  `job.next_poll_at = (job.next_poll_at or 0.0) + 2000.0; return job`; done ->
  `_finish(job, raw)`. Same error-mapping wrapper. `backend_code="deadline_exceeded"` (lowercase,
  exact) is reserved for `openreading.router.driver`'s own per-call slice-expiry sentinel
  (`_DriveSliceExpired`); a genuine vendor-side deadline/timeout may map through the strategies
  engine's `timeout` class (`openreading.strategies.engine.classify_error`, whose `_TIMEOUT_CODES`
  includes `deadline_exceeded` for a `RetryableError`) but should prefer a differently-spelled
  or vendor-native code (e.g. an uppercase gRPC status name) so it stays visually distinct from
  the driver's sentinel -- belt-and-suspenders beside the type-based fix that already makes the
  two uncollidable.
- `resolve_webhook(event, job, ctx)`: no-op when `job.is_terminal()` or the event id does not
  match `webhook_token`/`backend_job_id`; accept both enveloped (`event["data"]`) and flat
  payloads; refetch via `self._get_client(ctx)` when the event has no result body. Signature
  verification (if the backend has one) is this method's job too -- never trust an unverified
  event as a genuine result (see `reducto`). If verification needs a narrower credential than
  the rest of the adapter (a webhook secret with no API key), give it its own more lenient
  client-construction helper rather than reusing `_get_client(ctx)` as-is -- see `reducto`'s
  `_get_webhook_client(ctx)`.
- `cancel(job, ctx)`: optional to override -- the `BackendAdapter` default is a local-only no-op
  that marks a non-terminal job CANCELLED and stops polling, correct for any vendor with no
  server-side cancel endpoint (most hosted async APIs). Override only when the provider
  genuinely offers one, so the caller stops being billed for work still running server-side:
  build the client from `self._get_client(ctx)` exactly like every other method (never rely on
  a client bound at `submit()` time -- the instance that accepted the job may not be the one
  asked to cancel it), issue the vendor cancel call, then fall through to
  `super().cancel(job, ctx)` for the local state update. A subprocess/container adapter
  overrides it to kill the local process instead of calling a vendor.
- `normalize(job, ctx, slim_req)`: read `job.raw.payload` ONLY -- never the payload AND a field
  the caller might have supplied out-of-band. `slim_req` is the caller's own request with
  `document.bytes_base64`/`document.password`/`document.url`/`async.webhook_url` nulled
  (`openreading.ledger.header.slim_request`, computed once by the CALLER -- never inside your
  adapter). Every real `normalize` only reads non-secret fields (`options`,
  `document.mime_type`, page-range/typed-field settings, `extraction_schema`), so honoring this
  costs nothing and closes a latent exposure surface for good -- do not reach for those four
  fields; they are `None` by design, always, even if this backend "would only ever read them
  for something harmless". `ctx` is here purely for signature uniformity with the other four
  methods; no shipped `normalize` reads `ctx.credentials`, and yours should not either -- do
  not invent a use for it. Fill only real channels; respect
  `outputs.markdown/text/blocks/include_backend_raw`; X-channel warnings as above; fill
  `resp.usage` (tokens/pages/cost/duration) whenever the wire response reports them; attach
  `backend_raw` when requested. bboxes ONLY via `types/geometry.to_canonical(...)` (canonical
  [0,1] top-left/y-down + `bbox_native` preserved; DECISIONS D6: the single choke point every
  adapter calls, so no adapter re-derives geometry) and ONLY when the provider gives real page
  dimensions -- return `None` bbox otherwise, never a guess. Unmapped native block types ->
  `BlockType.OTHER` with `native_type` set. A provider "answered but invalid" state ->
  `ResponseState.PARTIAL` with `status.error`, not a raise.
- `report_cost(job)`: thin projection of `job.raw` usage counters. The router calls it right
  after `normalize()` and merges the result into `response.usage` (filling only what normalize
  left unset), so this is what a caller sees as `usage.cost_usd` -- it must not raise, and it
  must not invent a rate. `billing_target` is `"caller_account"` (BYO key) or `"caller_infra"`
  (local/self-hosted); `"openreading"` is forbidden and conformance rejects it. Native-batch
  exception: the router-calls-it-right-after-normalize story is the single-item path only -- a
  `normalize_many` may need to call `openreading.router.cost.apply_cost_report` directly per
  item, since the batch-level `job` it receives has no top-level `usage` to project from.
- `_map_error(e)`: taxonomy errors pass through unchanged; anything else ->
  `TerminalError(str(e), backend_code=type(e).__name__)`. An adapter with more than one wrapped
  call site inside the same method may add an optional `context: str | None = None` argument to
  keep failure sites distinguishable in the message, as `qwen_vl` does.
- `assert_supports(req)` (DECISIONS D13): the fourth taxonomy member, `UnsupportedFeatureError`,
  is realized two ways so "never fabricate / never silently drop a channel the caller asked
  for" holds on every path. An optional-but-unavailable channel -> a `warnings[]` entry plus
  what the backend CAN produce. A primary ask that is structurally impossible on a DIRECTLY
  named backend (e.g. `--backend pymupdf --extract`) -> RAISE, because silently returning
  geometry-only would deny the caller's ask; the router pre-filters routed requests at its
  capability stage, the raise covers the direct-named path it does not. The pure structural
  parsers (`custom_schema_extraction=False`) call it in `submit()`.

The R1-R3 conformance kit -- the actual bar before you declare `protocol_version=2`
Passing `check_adapter_conformance(...)` with no extra arguments (§4) proves response shape,
bboxes, channel honesty, cost shape and determinism -- it does NOT prove protocol v2's
instance-independence claim. That needs an explicit opt-in: pass
`adapter_factory=lambda: MyAdapter(client=FakeMyClient())` (a zero-arg callable returning a FRESH
instance sharing no Python-level state with the one under test -- a new fake client each call,
never a shared one) alongside your `ConformanceCase`s. This turns on three checks per case, and
they must be green BEFORE `protocol_version=2` is written anywhere:

- R1 (fresh-instance resume): the kit round-trips the just-submitted `Job` through
  `to_dict -> json.dumps -> json.loads -> from_dict`, then drives THAT reconstructed job to
  completion on a brand-new `adapter_factory()` instance -- proving a different process (or a
  resumed run) could have picked it up with no shared state. An INLINE-only adapter (no
  `poll`/`resolve_webhook`/`cancel` override) passes trivially -- there is never non-terminal
  state to resume -- but still run the kit to confirm that is really true for yours.
- R2 (no instance caching): immediately after `submit()`, no NEW attribute holding a
  client-shaped object exists on `self`; the constructor-injected `_client` seam is exempt,
  everything else is a violation. This is the literal, automated check for "did you accidentally
  do `self._client = client` inside `submit()`".
- R3 (JSON round trip): `job.to_dict()` survives `json.dumps` at every lifecycle stage a real
  durable executor would persist it at -- immediately after `submit()`, and again after the
  case's own drive-to-completion (a FAILED job carrying `job.error` included; `Job.to_dict`
  exists precisely because `dataclasses.asdict` of a job holding an exception is not JSON).

Omitting `adapter_factory` is not a shortcut to a passing build -- it is a DIFFERENT, WEAKER
claim (the baseline checks stay green either way, since they say nothing about instance
independence), and `protocol_version=2` is specifically the R1/R2/R3 claim.

Two optional capabilities follow before §4: liveness and native batch. Skip both unless the
provider offers a free non-billing liveness call or a genuine multi-document endpoint, and go
straight to "4. Tests -- the recipe" below.

Liveness (optional): `LivenessProbe` + `probe_liveness`
The platform half (`check_liveness`, the status ladder, inference, redaction, `probe_http`) is
`openreading.liveness`. `health()` answers "are my Python deps importable here";
it is offline by construction and cannot know whether the backend is ANSWERING -- `docling`
literally returns `Health(ready=True, detail="requires a running docling-serve container")`.
Liveness is the other half, and it is opt-in.

The one rule that decides whether you implement it at all: A PROBE MUST NEVER BE A BILLED
REQUEST (DECISIONS D-v7-4). If the provider offers a free, non-generating liveness call (a
`/health` route, a models list), implement it. If not, declare NOTHING -- the platform then
reports `configured_unverified` ("configured, but not verified -- inferred from your
environment, not measured"), which is honest and useful. A one-page parse to prove liveness
spends the caller's money on a diagnostic they never asked to pay for, and is worse than not
answering. Deliberately NOT a `billable: bool` descriptor field -- a field would legitimise the
thing the rule forbids. Five built-ins probe today (docling, qwen-vl, pymupdf, tesseract,
anthropic-claude); the other ten declare none, because no free call is verifiable from a
primary source and a guessed URL would be a fabricated descriptor value.

Two pieces:
- Declare it: `descriptor.liveness = LivenessProbe(probe=..., method=..., timeout_s=...,
  notes=...)` (`types/descriptor.py`). `probe` is a KIND, read by a UI offline, with no call, to
  decide whether to even offer a "check" button and what to warn before firing it: `"local"`
  (in-process/subprocess, no network), `"endpoint"` (network, to infrastructure the CALLER
  operates -- a container or model server), `"vendor"` (network, to a third party: leaves their
  network, may count against a rate limit), `"none"` (the default).
- Implement the one-method `LivenessProbeAdapter` Protocol (`adapters/base.py`) -- NOT one of the
  required 8. DECISIONS D-v7-1: `AdapterProtocol` is `@runtime_checkable`, so a 9th member would
  make every non-implementing adapter fail `isinstance`; and most hosted vendors have no free
  call, so a required method would force eight stubs that teach nothing. It follows the
  `NativeBatchAdapter` precedent exactly -- one optional-capability pattern in the codebase, not
  two. The ABC supplies a default returning "unsupported"; the platform feature-detects with
  `isinstance` and calls it only when `descriptor.liveness.probe` is not `"none"`:

      def probe_liveness(self, ctx: RunContext, *, timeout_s: float) -> ProbeResult:
          return probe_http(f"{endpoint}/health", timeout_s=timeout_s,
                            env_hint="MYBACKEND_URL", client=self._probe_client)

  `openreading.liveness.probe_http` already maps the failure taxonomy (connect/timeout ->
  `unreachable`, 401/403 -> `unauthorized`, other non-2xx -> `error`) and never raises -- use it
  rather than re-deriving it, and take a `probe_client=None` constructor kwarg as the test seam,
  exactly as `docling`/`qwen_vl` do.

Hard rules, all enforceable in review:
- Return `ProbeResult`, not a verdict (DECISIONS D-v7-2). You report what you OBSERVED; the
  platform owns the ladder (`not_configured` beats everything, the inference, the redaction
  pass). Never decide `not_configured` yourself -- you would duplicate logic that already ran,
  and copied into fifteen adapters it would drift. Same division `report_cost` uses: the
  adapter projects, the platform composes.
- Honor `timeout_s`, never hang.
- Never carry a secret, and never echo the provider's response body into `detail`/`version` --
  providers have been seen echoing a rejected key back inside an error body. Leave
  `unauthorized`'s detail EMPTY; the platform substitutes the key-free `check <VAR>` sentence.
  Never put the endpoint URL in the report either (a URL can embed credentials); name the env
  var.
- Never accept an `OpenReadingRequest`. The signature has no document parameter on purpose, so a
  probe can never become a data path; and no `router`/`strategies`/`comparison`/`evals`/`batch`
  module imports `openreading.liveness` (pinned by a test), so a pulse can never widen the
  compliance-eligible set (D-v7-6).
- Tests: offline against an injected fake client, live against the real thing. A probe test in
  the offline suite that opens a socket is a defect -- `make verify` must stay network-free.
  Copy `tests/test_liveness.py`'s adapter-probe tests for the offline half, and add one
  `@pytest.mark.live` probe test to `tests/test_<pkg>.py` (see `test_docling.py`,
  `test_qwen_vl.py`, `test_anthropic_claude.py`).

What each status means -- read this before deciding what to return. The probe returns one of
five `ProbeOutcome`s; the platform turns them into the status a user sees. You only ever report
what you OBSERVED, never a verdict:

    you observed                        return                         user is told / will do
    it answered, looks healthy          ProbeResult.live(detail,       `live` "responding" /
                                          version=...)                 nothing
    nothing answered: refused, DNS      ProbeResult.unreachable(       `unreachable` / start the
      failure, TLS failure, your          detail)                      service, fix the URL
      timeout elapsed
    it answered and rejected the        ProbeResult.unauthorized()     `unauthorized` "key
      credential (401/403)                -- EMPTY detail               rejected" / fix the
                                                                       named env var
    it answered, but not in a way       ProbeResult.error(detail)      `error` "check failed" /
      that proves health (5xx, still                                   read the detail; retry
      loading, garbage)
    you cannot probe at all             ProbeResult.unsupported()      the platform INFERS
                                          (the inherited default)      instead / nothing

Two of these are easy to get wrong and expensive when you do:
- `unauthorized` is not `unreachable`: one says "start your container", the other "fix your
  key" -- opposite actions. If something answered, it is not unreachable, whatever it said.
- `error` is not `unreachable`: a 503 from a model server still loading its weights means the
  server is UP; calling it unreachable sends the operator to restart something healthy.
There is no outcome for "I did not bother". If you cannot answer for free, declare no probe and
let the platform report `configured_unverified` -- a labelled inference the user can act on.

Worked example: an LLM-backed backend -- the shape most third-party adapters will have, an
OpenAI-compatible endpoint, self-hosted or hosted. Copy `qwen_vl/adapter.py`; the whole liveness
surface is these three edits.

1. Take a probe seam in `__init__`, separate from the execution client (so a test can drive the
   probe without faking generation, and so a probe can never reach a billed call):

      def __init__(self, client: MyClient | None = None, *, probe_client: Any | None = None):
          self.descriptor = _descriptor()
          self._client = client
          self._probe_client = probe_client

2. Declare it in `_descriptor()`:

      liveness=LivenessProbe(
          probe="endpoint",   # "vendor" if it is somebody else's API, "local" if in-process
          method="GET {MY_LLM_ENDPOINT}/models",
          timeout_s=5.0,
          notes="Free model list; never generates a token, so it is never billed.",
      ),

3. Implement the one method:

      def probe_liveness(self, ctx: RunContext, *, timeout_s: float) -> ProbeResult:
          runtime = ctx.runtime or {}
          creds = (ctx.credentials.values if ctx.credentials else {}) or {}
          endpoint = runtime.get("endpoint")
          api_key = creds.get("api_key")
          return probe_http(
              f"{endpoint.rstrip('/')}/models",
              timeout_s=timeout_s,
              headers={"Authorization": f"Bearer {api_key}"} if api_key else None,
              env_hint="MY_LLM_ENDPOINT",          # the env var NAME, never the URL
              on_ok=lambda r: ProbeResult.live(
                  "responding", version=(r.json().get("data") or [{}])[0].get("id")
              ),
              client=self._probe_client,
          )

That is the entire protocol. `on_ok` is optional -- use it only if a healthy response tells you
something worth reporting (which model is actually loaded answers a question the endpoint URL
alone cannot). If the provider has no free liveness call, delete steps 2 and 3 entirely; do not
reach for a one-token completion -- it is billed, and the platform's inference is honest and free.

Tests -- one offline, one live:

      def test_probe_is_live_when_the_endpoint_answers():  # offline: no socket, ever
          fake = _FakeHttp(_FakeResponse(200, {"data": [{"id": "m"}]}))
          adapter = MyAdapter(probe_client=fake)
          assert check_liveness(adapter, broker=...).status is LivenessStatus.LIVE

      @pytest.mark.live
      def test_live_liveness_probe():  # pragma: no cover
          skip_unless_creds("my-llm")
          assert check_liveness(MyAdapter()).status is LivenessStatus.LIVE

Deployment states worth simulating offline with respx -- copy `tests/test_liveness_http.py`,
which does exactly this for the built-ins: connection refused, DNS failure, connect timeout, a
server that accepts then never answers, 401 with a present-but-wrong key, 503 while loading, and
a 200 whose body cannot be parsed.

In-tree examples, one per kind: `docling` (`endpoint`, `GET {DOCLING_SERVE_URL}/health`),
`qwen_vl` (`endpoint`, `GET {QWEN_VL_ENDPOINT}/models`, reporting the served model as
`version`), `pymupdf`/`tesseract` (`local`), `anthropic_claude` (`vendor`, `models.list()` -- a
documented non-billing endpoint). The other ten built-ins deliberately declare NO probe, for
the reasoning above; apply the same reasoning.

Native batch (optional): `BatchIntake` + `submit_many`/`normalize_many`
Only relevant if the target API offers a genuine multi-document submission endpoint (one call,
many documents). Most backends do not -- platform-level batching (the runner fans out
single-document `submit`/`normalize` calls) is the default and needs no adapter work. If yours
does:
- Declare `descriptor.batch = BatchIntake(native=..., max_items=..., notes="...")`
  (`types/descriptor.py`). `native` follows the same honesty ladder as capabilities:
  `"verified"` only once a live run has proven the contract, `"claimed"` for a documented but
  unaudited implementation, `False` (the default) when there is no native endpoint and the
  platform batches instead. Never grade up.
- Implement the two-method `NativeBatchAdapter` Protocol (`adapters/base.py`) -- not one of the
  required 8, and the ABC supplies no default; the batch runner feature-detects it (`isinstance`
  against the Protocol) and dispatches to it only when `descriptor.batch.native` is truthy:
  `submit_many(reqs, ctx) -> Job` (one POLL/WEBHOOK job for the whole batch) and
  `normalize_many(job, reqs, credentials=None) -> list[NormalizedResponse | BatchItemError]`
  (results mapped back to each request in order; a per-item failure -- whether the vendor
  reports it or the mapping step itself raises -- becomes a `BatchItemError` entry, never a
  raise that takes down the whole batch). `_run_native` passes `ctx.credentials` as
  `credentials`; forward it into any internal `apply_cost_report` call, exactly as
  `anthropic_claude` does, so a `report_cost()` failure's warning gets the same secret redaction
  every other `apply_cost_report` call site gets. The parameter is optional only so the
  Protocol's presence check stays satisfied by older implementations (`@runtime_checkable`
  Protocols check method presence, not exact signature) -- forwarding it is not optional in
  practice.
- Copy from `anthropic_claude/` (Message Batches API -- `submit_many`/`poll`/`normalize_many`),
  the one adapter implementing this today and this shape's row in the §0 picker.
- Dispatch rule (`openreading.api._native_adapter`; full contract in the `openreading.batch`
  module docstring): native iff the backend is directly named (not `auto`, not a strategy),
  `descriptor.batch.native` is truthy, the adapter implements the Protocol, and the live item
  count is within `batch.max_items`; otherwise platform fan-out. Native dispatch defaults its
  deadline to `DEFAULT_NATIVE_BATCH_DEADLINE_MS` (1h), not the 120s single-document default.
- Audited candidates (graded honestly; never promote without evidence): `google-document-ai` and
  `azure-document-intelligence` have real batch APIs but stage through GCS/blob storage the
  adapter does not own (deferred -- platform batching; neither descriptor declares a `batch`
  block, so `AdapterDescriptor.batch` is `None` for both -- the design asked for the blocker to
  be recorded in descriptor `notes`, but as shipped it lives only in the design doc's Appendix A
  audit list, `internal/design/batch-intake.md`, and in `google_document_ai`'s module
  docstring);
  `reducto`, `pulse`, `google-gemini` and `mistral-ocr` are unaudited (platform until proven).
  `aws-textract`, `chunkr`, `open-ocr`,
  the locals and the self-hosted endpoints have no multi-document call (request-level batching
  for `qwen-vl`/`nuextract` is the serving layer's concern, not the adapter's).

Determinism
Same request + same fixture => byte-identical `to_schema_dict()` -- conformance resubmits and
compares. So: no timestamps, no randomness, no dict-order dependence in normalize.

4. Tests -- the recipe
----------------------
Fixtures (`tests/fixtures/<slug>/*.json`): hand-written from the documented wire shapes with
EXACT field names and casing, realistic values, no secrets. They upgrade to scrubbed live
captures the first time someone runs `make verify-live` with the key set and
`OPENREADING_RECORD_FIXTURES=1` (`tests/live_helpers.py`).

`tests/test_<pkg>.py` (happy path):
- A `Fake<X>Client` implementing the Protocol, replaying fixtures; record inputs (`last_body`,
  created/deleted resources, idempotency key) so tests can assert plumbing.
- `check_adapter_conformance(adapter, [ConformanceCase(request=..., deterministic=True,
  label=...)], adapter_factory=lambda: MyAdapter(client=Fake<X>Client()))` with one case per
  operation. This single call enforces descriptor schema validity, response schema validity,
  bbox canonicality, X-channel honesty, cost shape, idempotency, AND (via `adapter_factory=`)
  the R1/R2/R3 instance-independence checks. It is mandatory; no adapter merges without it --
  and `adapter_factory=` specifically is mandatory before you declare `protocol_version=2`, not
  an optional extra.
- Content assertions: the interesting normalize mappings (values preserved, usage filled,
  warnings present, provenance fields like `backend.version`).
- A no-credentials test (`backend_code == "no_credentials"`).
- One `@pytest.mark.live` test using `tests/live_helpers.py` (`skip_unless_creds(slug)` +
  `run_live(...)`) -- skips cleanly without the key.

`tests/test_<pkg>_faults.py` (branch coverage -- the 91% coverage floor is a gate). Cover every
branch the happy path skips. The standard set:
- input variants: each accepted intake + each rejected one
  (`backend_code == "unsupported_input"`)
- submit: taxonomy errors re-raised unchanged; unexpected exception mapped
- provider-failure responses at submit time (sync `failed` status -> TerminalError)
- poll: terminal status raises (+ any cleanup side effect asserted); status/result fetch
  exceptions mapped; in-progress status reschedules then succeeds via `run_to_completion` with
  `FakeClock`
- webhook (if supported): mismatched id ignored; an id-less event against an id-less job is
  also ignored, not hijacked; matching event finishes; bare notification refetches;
  already-terminal is a no-op; OUTBOUND: `submit()`'s vendor request actually carries
  `req.async_.webhook_url` when it is set -- assert against the fake client's recorded
  body/config, not just `job.wait_mode` (the "checked the local field, never checked what the
  vendor received" gap; assertion shape: `test_webhook_url_forwarded_to_vendor_on_parse`)
- op-specific edges (PARTIAL error state, empty result, missing optional wire fields)
- disabled `outputs` suppress channels, warnings, and `backend_raw`
- `report_cost` both before a result exists and after
- unsupported primary asks surface (`assert_supports` -> `UnsupportedFeatureError`) if the
  backend cannot do schema extraction

Use small scripted fakes (`_ScriptedClient(statuses=[...])`, `_RaisingCreate(exc)`) -- no
mocking libraries, no respx; plain classes implementing the Protocol.

5. Verify loop (in this order)
------------------------------
    uv run pytest tests/test_<pkg>.py tests/test_<pkg>_faults.py -p no:cov
    uv run pytest tests/test_descriptor_specs.py tests/test_server.py -p no:cov
    make verify            # ruff (check + format!), pyright, full suite w/ 91% coverage floor,
                           # schema-validate, CLI smoke, strategy smoke
    uv run python -m openreading.cli backends   # slug listed with the right MISSING env hint

`make verify` will catch: an unformatted file (`ruff format` it), a missed count assert, a
coverage dip (add fault tests, never lower the floor), a descriptor schema violation.

6. Definition of done
---------------------
- [ ] `make verify` green (includes the conformance call and the 91% coverage floor)
- [ ] `protocol_version=2` declared, and `adapter_factory=` (R1/R2/R3) actually run green --
      not assumed from the shape of the code
- [ ] every file in the §2 checklist created/edited
- [ ] descriptor values traceable to the module docstring's primary sources (with dates)
- [ ] compliance encoded fail-closed for anything unverified
- [ ] no fabricated channel anywhere; X-channels warn when requested
- [ ] live test skips cleanly without the key; runs against the real API with it
- [ ] liveness: either a real probe (free, non-billing, declared + tested offline and live) or
      NO `liveness` block at all -- never a billed call, and never a guessed endpoint URL
- [ ] `src/openreading/adapters/README.md` catalog rows added and the pasted `backends` table
      re-run
- [ ] commit message records flow, channel posture, cost basis, and deliberate non-choices

Known-honest caveat to state in the PR/commit: the `_Httpx*` real-network client is excluded
from offline coverage by design; the first `make verify-live` run with real keys validates it
and may need small wire fixes (status vocabulary, content types). That is the expected
workflow, not a gap.

One more invariant the whole design leans on (DECISIONS D-v2-8.1, which applies the GOAL2
D-v2-6 per-run-instance invariant to the server): each request builds a fresh registry/adapter
instance (`build_registry`/`make_adapter`) so a credential-bound client never leaks across
requests -- the same reason no method may ever bind a client to `self`.
"""

from __future__ import annotations

from openreading.adapters.base import AdapterProtocol, BackendAdapter

__all__ = ["BackendAdapter", "AdapterProtocol"]
