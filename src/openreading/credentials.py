"""BYO-credential resolution. The seam the whole product hangs on: a backend declares WHAT it
needs (descriptor.credentials_spec / config_spec, descriptor schema v0.2 — D-v2-6.1a); this module
resolves those keys from the caller's own environment and builds the RunContext every execution
surface (CLI, server, Python API, web UI, evals, chain executor, strategy engine) runs through.

Posture — pure BYO-key pass-through
-----------------------------------
You bring the provider's own credential; it is read from the process environment per request,
held in memory only for that call, and the charge lands on your own account. OpenReading never
stores, resells, logs, echoes, or bills for it, and never forwards it anywhere but the provider.
The broker never branches on backend type — it is driven entirely by the descriptor's spec. Each
run gets a fresh adapter instance (D-v2-6 / D-v2-8.1), so a credential-bound client is never
shared across requests. Keys never travel in a request body — only `credentials_ref` handles do —
and recorded fixtures are scrubbed of every secret before they touch disk.

Precedence (D-v2-1), highest first
----------------------------------
  1. request-embedded value (`backend.runtime`, config fields only — EXCEPT `endpoint`, refused)
  2. `credentials_ref: "env:<alias>"` -> `<alias>_<KEY>`, alias allow-listed by the operator in
     `OPENREADING_CREDENTIALS_REF_ALIASES` (comma-separated; unset/empty accepts none — closed)
  3. `OPENREADING_<SLUG>_<KEY>` (slug `-` -> `_`, e.g. `OPENREADING_REDUCTO_API_KEY`)
  4. the service-native var(s), in `spec.env` order (e.g. `REDUCTO_API_KEY`)
  5. the provider SDK's own ambient chain (boto3 default chain, GCP ADC, the anthropic SDK's own
     `ANTHROPIC_API_KEY`)
Step 5 is NOT resolved here: when the broker finds nothing it leaves the value unset and the
adapter's SDK falls through to its own chain. That is why aws-textract, google-document-ai and
anthropic-claude work with no OpenReading-specific env on an already-configured machine, and why
`openreading backends` still reports them ready. `openreading backends` names missing VARS, never
values.

`.env` loading (`load_dotenv`): `KEY=VALUE` lines from a dotenv file enter the environment WITHOUT
overriding an already-set process var — an exported shell variable always wins over the file, so a
value exported in an earlier shell command silently beats the file you just edited. The CLI loads
`./.env` (or `--env-file`) on every invocation; the web UI's `create_app` loads `./.env` once;
`openreading.run(env_file=)` / `run_batch(env_file=)` load only when given. Copy `.env.example` and
fill only the backends you use.

Why the `credentials_ref` allow-list exists (BL-162): before it, `env:<anything>` resolved every
credential key as `<anything>_<KEY>`, so an unauthenticated request (server auth is off by default)
could name any prefix and read whatever variable happened to be shaped `<PREFIX>_<KEY>` for that
backend's declared fields — `env:ANTHROPIC_API` against a field keyed `key` reads
`ANTHROPIC_API_KEY`, regardless of which backend the request targets. The rejection message names
the SETTING, never the accepted set.

Why `backend.runtime.endpoint` is refused, not ignored (BL-162, D7's shape — deployment knobs never
come from the request body): combined with the old free-form `credentials_ref`, a request could
resolve an arbitrary secret AND redirect the authenticated call carrying it to a host of the
caller's choosing. Self-hosted backends still set their endpoint per deployment via their own env
var (`QWEN_VL_ENDPOINT`, `DOCLING_SERVE_URL`, `CHUNKR_BASE_URL`); only per-request override is
gone. The other `backend.runtime` fields (`mode`, `image`, `device`, `system_deps_ok`) control local
execution, not a network destination, and stay request-settable.

Default idempotency key (BL-166): a request that omits `idempotency_key` gets a deterministic
digest of (document content, backend, resolved version pin, result-affecting canonical options), so
a retried submit is recognised as the same request without double-billing. Only a `bytes_base64`
or local `path` document qualifies; a `url`/`file_id` document gets no default key (nothing
content-stable exists before the fetch — the same rule the executor's result cache applies, D-v3-3).
Today the key only reaches a hosted vendor's own API for `open-ocr`, the one backend with a real,
documented idempotency mechanism.

Per-backend reference
---------------------
  backend / extra / auth env / non-secret config / operations / limits & notes
  (`ops: none` = the descriptor declares no `operations` list; `AdapterDescriptor.operations`
  defaults to `[]` for the four local/self-hosted parse-only backends)
  pymupdf   [pymupdf]   auth: none (local)   config: none   ops: none
            limits: born-digital PDFs only (no OCR); AGPL
  tesseract [tesseract] auth: none (local)   config: system `tesseract` >=5 + langpacks   ops: none
            limits: rasterizes PDFs via pymupdf
  docling   [docling]   auth: none   config: DOCLING_SERVE_URL   ops: none
            limits: needs a self-hosted docling-serve container
  qwen-vl   [qwen-vl]   auth: QWEN_VL_API_KEY (opt)   config: QWEN_VL_ENDPOINT, QWEN_VL_MODEL
            ops: none   limits: self-hosted OpenAI-compatible endpoint (vLLM/Ollama)
  reducto   [reducto]   auth: REDUCTO_API_KEY (+REDUCTO_WEBHOOK_SECRET, inbound webhooks only)
            config: none   ops: parse, extract, split, classify, edit, pipeline (as DECLARED; the
            adapter implements only parse + extract — any other `backend.operation` falls through
            to the parse path)   limits: sync + webhook + poll; extract is sync-only
  chunkr    [chunkr]    auth: CHUNKR_API_KEY   config: CHUNKR_BASE_URL (self-host)
            ops: parse, extract   limits: task-based (poll/webhook, no sync); ~2,000-page soft cap;
            self-hostable (AGPL)
  pulse     [pulse]     auth: PULSE_API_KEY   config: none   ops: extract
            limits: dual response shape (>5MB / 70pp returns a 1-hour result URL); NO per-element
            confidence; no-train UNVERIFIED -> fails closed
  nuextract [nuextract] auth: NUEXTRACT_API_KEY (or NUMIND_API_KEY)
            config: NUEXTRACT_BASE_URL (on-prem platform)   ops: extract, parse
            limits: typed-template extraction (template != JSON Schema, passed verbatim) +
            NuMarkdown parse (markdown only, NO blocks/bboxes); bytes intake only; no-train
            UNVERIFIED -> fails closed
  open-ocr  [open-ocr]  auth: OPENOCR_API_KEY   config: OPENOCR_ENGINE (default openocr/tesseract)
            ops: parse   limits: OCR aggregator (~20 engines behind one API); plain text only (NO
            blocks/bboxes); per-page USD debit on every response (cost basis BILLED); 10MB body
            cap; no-train UNVERIFIED -> fails closed
  anthropic-claude [anthropic-claude]   auth: ANTHROPIC_API_KEY   config: ANTHROPIC_MODEL (opt)
            ops: parse, extract   limits: native-PDF ~100 pages / 32MB ceiling -> `doc_too_large`
  aws-textract [textract]   auth: AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_SESSION_TOKEN (opt,
            STS/temporary creds) — or the ambient boto3 chain
            config: AWS_REGION (or AWS_DEFAULT_REGION), OPENREADING_TEXTRACT_S3_BUCKET (async only)
            ops: Detect/Analyze[Document|Expense|ID|Lending]
            limits: sync 1 page / async <=3000 pages via S3 (the bucket is required only for the
            multi-page async POLL flow)
  azure-document-intelligence [azure-document-intelligence]
            auth: AZURE_DOCUMENT_INTELLIGENCE_KEY   config: AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT
            ops: prebuilt-read/layout/invoice, custom   limits: LRO poll
  google-document-ai [google-document-ai]   auth: none (ADC only)
            config: GOOGLE_APPLICATION_CREDENTIALS (ADC), GCP_PROJECT_ID, GCP_PROCESSOR_ID
            (+GCP_LOCATION)   ops: OCR/FormParser/LayoutParser/CustomExtractor
            limits: sync `:process` <=15 pages (batchProcess not implemented); a non-`us` region
            auto-pins the regional endpoint
textract and google-document-ai use IAM keypairs / ADC, not API keys — their descriptors say
`byo_mode: cloud_credential` (D-v2-6.1b); the per-key truth is always `credentials_spec`.

Live-test gate gotcha: `make verify-live` only checks that a backend's gate var is SET, not that
its value is reachable. Harmless for a hosted key (a stale key fails loudly), but QWEN_VL_ENDPOINT
and DOCLING_SERVE_URL gate on a LOCAL SERVICE ADDRESS — comment them out when that service is not
running or the live test connects to a dead address instead of skipping.

Actionable failures
-------------------
  * `auth_rejected` — a key that is present but rejected by the provider (HTTP 401/403, IAM
    AccessDenied, GCP PermissionDenied/Unauthenticated, an anthropic AuthenticationError). Every
    surface shows the same key-free sentence, "key was found but rejected by <backend> — check
    <VAR>" (`openreading.readiness.auth_rejected_hint`): single and batch parse, `route --run`,
    `replay`, `calibrate`, and the HTTP body (424 — the same "fix a var" class as missing
    credentials). The provider's own response body is DROPPED, not appended: providers have been
    seen echoing the rejected key back inside it. Never retried, never a silent fall-through.
  * `doc_too_large` — the document exceeds the provider's size/page limit (HTTP 413 or an adapter
    ceiling); the message carries the limit and the `auto` chain falls back to a backend with a
    higher ceiling.
  * Every other failure message is redacted (`secret_values` + `redact`, `***`, longest-first so
    overlapping secrets leave no fragments) before it reaches any surface — CLI, HTTP, batch trail
    — which covers a self-hosted or misconfigured backend echoing request context in a verbose
    error body. Non-secret config (endpoint, region, project id) is deliberately not redacted.
  * Liveness reports (`openreading backends --check`, `POST /v1/backends/{id}/liveness`, the UI's
    Check now) follow the same posture: they name env VARS never values, `detail` is redacted on
    the way out, an `unauthorized` finding reuses the key-free sentence, no field carries the
    endpoint URL (a URL can embed `user:token@host`), and a probe never sends a document, so it can
    never become a data path (`openreading.liveness`, internal/design/liveness.md §3.4).

Time budget: `ctx.deadline_ms` is a BUDGET, not an absolute deadline (DEFAULT_DEADLINE_MS, or
DEFAULT_NATIVE_BATCH_DEADLINE_MS for native batch); callers convert it via
`clock.now_ms() + budget`.

Ledger per-run keys: with `OPENREADING_LEDGER` armed, the ledger root holds `<run_id>.jsonl` (the
journal: one line per StepResult, append-only), `blobs/` (payloads, encrypted under a per-run key)
and `keys/` (dir 0700, one 0600 key file per run). Exclude `keys/` from every backup, replica or
snapshot of the ledger root: crypto-shredding a run (the key-destroy path or the at-arm-time
retention reaper) deletes that one file, which erases the run's content O(1) across every replica
and backup at once — the reason erasure is key destruction rather than blob deletion — and the
guarantee holds only to the degree no other copy of the key survives. A shredded run is
permanently non-replayable by design (`openreading resume RUN_ID` / `openreading.resume(...)`
report `payload_expired`); the journal still answers "what happened", just not "with what
content". Back up `*.jsonl` and `blobs/` as needed, never `keys/` alongside them
(`openreading.ledger`, internal/design/ledger.md §9.4).

Operator knobs that are not credentials (`OPENREADING_LEDGER*`, `OPENREADING_CONFIG`,
`OPENREADING_LLM_DECIDER`, `OPENREADING_API_KEYS*`, the compliance attestations) are documented one
line each in `.env.example` and in the module that reads them (`openreading.api`,
`openreading.strategies.loader` / `.decider`, `openreading.server.app`). The server's HTTP posture
(no caller auth by default) lives in `openreading.server.app`.
"""

from __future__ import annotations

import os
from collections.abc import MutableMapping
from pathlib import Path

from openreading.router.cache import content_key, document_identity
from openreading.types.descriptor import AdapterDescriptor, ConfigField, CredentialField
from openreading.types.errors import TerminalError
from openreading.types.request import OpenReadingRequest
from openreading.types.runtime import ResolvedCredentials, RunContext

# One place defines the default per-call time budget (ms). ctx.deadline_ms is a BUDGET (adapters
# like tesseract read it as a subprocess timeout); callers turn it into an absolute driver
# deadline via clock.now_ms() + budget.
DEFAULT_DEADLINE_MS = 120_000

# BL-135: native-batch dispatch (internal/design/batch-intake.md §7) is not a synchronous-feeling
# round trip like every other path build_run_context serves — a single submit_many call kicks off
# a whole vendor-side batch job. The one real adapter implementing the protocol
# (anthropic_claude) documents its own typical completion time in its own descriptor
# (AdapterDescriptor.batch.notes): "Async, most <1h." Reusing DEFAULT_DEADLINE_MS's 2-minute
# budget there means an ordinary, non-adversarial native-batch call is expected to hit the
# deadline on essentially every invocation that isn't trivially small. 1 hour reflects that
# documented timing with headroom, not a guess; api._run_native uses this instead of
# DEFAULT_DEADLINE_MS unless a caller passes its own override (run_batch's deadline_ms=).
DEFAULT_NATIVE_BATCH_DEADLINE_MS = 3_600_000


def load_dotenv(
    path: str | os.PathLike[str] | None = None,
    environ: MutableMapping[str, str] | None = None,
) -> int:
    """Load `KEY=VALUE` lines from a `.env` file into the environment WITHOUT overriding a var
    that is already set (explicit process env always wins). `export KEY=VALUE`, `#` comments,
    blank lines, and single/double-quoted values are handled. Returns the number of vars set.
    With `path=None`, loads `./.env` if it exists (missing file → no-op, returns 0)."""
    env = environ if environ is not None else os.environ
    p = Path(path) if path is not None else Path(".env")
    if not p.is_file():
        return 0
    count = 0
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, val = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        val = val.strip()
        if val[:1] in ("'", '"'):
            # quoted value: everything up to the matching close quote is data (a `#` inside is
            # kept); anything after the close quote (an inline comment) is dropped.
            end = val.find(val[0], 1)
            val = val[1:end] if end != -1 else val[1:]
        else:
            # unquoted: a `#` at the start of the value or preceded by whitespace begins an
            # inline comment (python-dotenv semantics). `abc#def` stays a value.
            for i, ch in enumerate(val):
                if ch == "#" and (i == 0 or val[i - 1] in " \t"):
                    val = val[:i].rstrip()
                    break
        if not key or key in env:
            continue
        env[key] = val
        count += 1
    return count


def _slug_env(slug: str, key: str) -> str:
    """The OpenReading override env var for a (backend, key) pair: OPENREADING_<SLUG>_<KEY>."""
    return f"OPENREADING_{slug.replace('-', '_').upper()}_{key.upper()}"


def _credentials_ref_aliases(env: MutableMapping[str, str]) -> frozenset[str]:
    """BL-162: `credentials_ref: "env:<alias>"` may only select a prefix the OPERATOR has
    explicitly allow-listed via `OPENREADING_CREDENTIALS_REF_ALIASES` (comma-separated) — never an
    arbitrary caller-chosen env-var prefix. Before this, `env:<anything>` resolved every
    credential key as `<anything>_<KEY>`, so an unauthenticated request could name any prefix and
    read whatever environment variable happened to end in `_<KEY>` for that backend (e.g.
    `env:ANTHROPIC_API` against a field keyed `key` reads `ANTHROPIC_API_KEY`). Unset/empty means
    no alias is accepted — fails closed, matching this codebase's compliance posture generally."""
    return frozenset(
        s.strip()
        for s in env.get("OPENREADING_CREDENTIALS_REF_ALIASES", "").split(",")
        if s.strip()
    )


class EnvCredentialBroker:
    """Resolves a descriptor's credential/config spec from the environment. Inject an explicit
    `environ` dict in tests; defaults to `os.environ`."""

    def __init__(self, environ: MutableMapping[str, str] | None = None) -> None:
        self._env: MutableMapping[str, str] = environ if environ is not None else os.environ

    def _ref_prefix(self, req: OpenReadingRequest) -> str | None:
        ref = req.backend.credentials_ref
        if not ref:
            return None
        if not ref.startswith("env:"):
            # a raw secret in credentials_ref is NEVER honored (the schema forbids it too).
            raise TerminalError(
                f"unsupported credentials_ref scheme in {ref!r}; only 'env:<PREFIX>' is "
                f"supported in v0.2 (each key resolves from <PREFIX>_<KEY>)",
                backend_code="unsupported_credentials_ref",
            )
        alias = ref[len("env:") :]
        if alias not in _credentials_ref_aliases(self._env):
            # BL-162 review (bruce): does NOT enumerate the configured allow-list. This message
            # reaches an unauthenticated caller verbatim (server auth is off by default) — naming
            # the accepted set here would hand a caller the operator's internal alias/vault naming
            # for free, the same class of unauthenticated information exposure this defect exists
            # to close. An operator who set OPENREADING_CREDENTIALS_REF_ALIASES already knows its
            # contents; naming the SETTING (below) is enough to diagnose a misconfiguration without
            # disclosing its VALUE, matching this codebase's own error-message posture elsewhere
            # (ServerConfigError, server/app.py: names settings and positions, never values).
            raise TerminalError(
                f"credentials_ref alias {alias!r} is not recognized "
                f"(see OPENREADING_CREDENTIALS_REF_ALIASES)",
                backend_code="credentials_ref_alias_not_allowed",
            )
        return alias

    def _lookup(
        self,
        slug: str,
        field: CredentialField | ConfigField,
        ref_prefix: str | None,
        request_value: str | None = None,
    ) -> str | None:
        if request_value is not None:
            return request_value
        if ref_prefix is not None:
            v = self._env.get(f"{ref_prefix}_{field.key.upper()}")
            if v:
                return v
        v = self._env.get(_slug_env(slug, field.key))
        if v:
            return v
        for name in field.env:
            v = self._env.get(name)
            if v:
                return v
        return None

    def resolve(
        self, descriptor: AdapterDescriptor, req: OpenReadingRequest
    ) -> ResolvedCredentials:
        """Secrets/credentials from credentials_spec → ResolvedCredentials.values. Keys the broker
        can't find are left out (the adapter's ambient SDK chain, if any, takes over)."""
        prefix = self._ref_prefix(req)
        values: dict[str, object] = {}
        for f in descriptor.credentials_spec:
            v = self._lookup(descriptor.id, f, prefix)
            if v is not None:
                values[f.key] = v
        return ResolvedCredentials(values=values, source="env" if values else None)

    def resolve_config(self, descriptor: AdapterDescriptor, req: OpenReadingRequest) -> dict:
        """Non-secret config from config_spec → ctx.runtime dict, merged with the request's typed
        backend.runtime.

        `endpoint` is operator configuration only (BL-162) and is refused, not merely ignored, if
        a request supplies one — D7's shape (`server/app.py:_server_router_config`): deployment
        knobs never come from the request body. Before this, a request-supplied endpoint always
        won (`out = dict(runtime_req)` then config_spec's `request_value=` short-circuit), so a
        caller could redirect a hosted backend's authenticated call — key included — to a host of
        their choosing. Every other `backend.runtime` field (`mode`, `image`, `device`,
        `system_deps_ok`) is unaffected — those control local execution, not a network
        destination, and stay request-settable."""
        prefix = self._ref_prefix(req)
        runtime_req = (
            req.backend.runtime.model_dump(exclude_none=True) if req.backend.runtime else {}
        )
        if "endpoint" in runtime_req:
            raise TerminalError(
                "backend.runtime.endpoint is operator configuration only and cannot be set in a "
                "request; configure the backend's endpoint via its environment variable instead",
                backend_code="endpoint_not_request_configurable",
            )
        out: dict[str, object] = dict(runtime_req)  # pass through remaining typed runtime fields
        for f in descriptor.config_spec:
            v = self._lookup(descriptor.id, f, prefix, request_value=runtime_req.get(f.key))
            if v is not None:
                out[f.key] = v
        return out


def build_run_context(
    req: OpenReadingRequest,
    descriptor: AdapterDescriptor,
    *,
    broker: EnvCredentialBroker | None = None,
    deadline_ms: int | None = None,
) -> RunContext:
    """The one factory every execution surface uses to turn a request + descriptor into a
    RunContext. Fills resolved credentials, runtime config, the already-checked compliance block,
    the idempotency key, and the time budget. Bare RunContext() construction outside tests is a
    defect.

    `deadline_ms` (BL-146): every pre-existing caller keeps getting DEFAULT_DEADLINE_MS unchanged
    (this param defaults to None, which means "use the generic default") — but a caller that has
    already resolved its own, real, workload-appropriate budget (the plain `execute_plan` path's
    `deadline_ms=` argument; the strategy engine's per-node `budget_ms` remaining-time value) can
    pass it explicitly so it actually reaches `ctx.deadline_ms` — the field an adapter's own code
    reads (e.g. TesseractAdapter.submit()'s subprocess timeout) — instead of every caller being
    stuck on the one hardcoded two-minute constant regardless of what was declared.

    `idempotency_key` (BL-166): a caller-omitted key is no longer left unset — it defaults to
    `_default_idempotency_key`, so every submit carries a stable key before it ever reaches an
    adapter, without the caller having to think about it."""
    broker = broker or EnvCredentialBroker()
    creds = broker.resolve(descriptor, req)
    runtime = broker.resolve_config(descriptor, req)
    return RunContext(
        credentials=creds if creds.values else None,
        compliance=req.compliance.model_dump(exclude_none=True) if req.compliance else None,
        runtime=runtime or None,
        deadline_ms=deadline_ms if deadline_ms is not None else DEFAULT_DEADLINE_MS,
        idempotency_key=(
            req.idempotency_key
            if req.idempotency_key is not None
            else _default_idempotency_key(req, descriptor)
        ),
    )


def _default_idempotency_key(req: OpenReadingRequest, descriptor: AdapterDescriptor) -> str | None:
    """BL-166: a caller-omitted idempotency key defaults to a pure function of (content, backend,
    resolved version, canonical options) — deterministic, secret-free (`canonical_options` already
    excludes credentials and `webhook_url`), and available before any vendor call, so a retried
    submit can be recognized as the same request instead of double-billing or double-processing.

    None when the document has no stable content identity (a URL/file_id locator, or an unreadable
    local path) — the same conservative rule the executor's own result cache already applies to
    the identical problem: an unidentifiable document gets no default key either; the caller must
    supply one explicitly if it wants retry safety for that document. Not a "pure function of
    bytes" for a path-sourced document specifically — see `document_identity`'s own docstring
    (`router/cache.py`) and internal/design/ledger.md §5.5 for why that's a known, tracked
    limitation, not an oversight."""
    identity = document_identity(req.document)
    if identity is None:
        return None
    version = descriptor.runtime.version_pin or "" if descriptor.runtime else ""
    return content_key(identity, descriptor.id, version, req.to_schema_dict())


def secret_values(descriptor: AdapterDescriptor, creds: ResolvedCredentials | None) -> set[str]:
    """The resolved values of the fields graded secret — for redaction. Non-secret config
    (endpoints, region, project id) is intentionally excluded."""
    if creds is None:
        return set()
    secret_keys = {f.key for f in descriptor.credentials_spec if f.secret}
    return {str(v) for k, v in creds.values.items() if k in secret_keys and v}


def redact(text: str, secrets: set[str]) -> str:
    """Replace every secret value in `text` with `***`. Longest-first so overlapping secrets don't
    leave fragments behind."""
    for s in sorted(secrets, key=len, reverse=True):
        if s:
            text = text.replace(s, "***")
    return text
