"""BYO-credential resolution. The seam the whole product hangs on: a backend declares WHAT it
needs (descriptor.credentials_spec / config_spec, descriptor schema v0.2 — D-v2-6.1a); this module
resolves those keys from the caller's own environment and builds the RunContext every execution
surface (CLI, server, Python API, evals, chain executor, strategy engine) runs through.

Posture — pure BYO-key pass-through
-----------------------------------
You bring the provider's own credential. It is read per request, and the charge lands on your own
account. A key from the environment or `.env` is read where it already lives. A key from the OS
keychain is cached in this process's memory for up to 60 seconds
(`openreading.keychain.CACHE_TTL_S`), so one run asks the keychain once per variable. That cache
never enters `os.environ`, so a subprocess never inherits it. This package never logs or echoes the
key, and never sends it anywhere but the provider. OpenReading writes a key in exactly one case:
you ran `openreading keys set`, `openreading keys import` or `openreading setup`, and it writes to
your OS keychain (`openreading.keychain`). It never writes a key to a file.
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
Steps 3 and 4 read the process environment, which holds `.env` once it is loaded. When the
process opted into the keychain, the OS keychain sits below both, for the secret variables a
backend declares in step 4 only. For example, an exported `ANTHROPIC_API_KEY` beats the same line
in `.env`, which beats the saved key. The keychain never beats the shell or `.env`, so an export
you made for one shell cannot lose to a value saved weeks ago.
  5. the provider SDK's own ambient chain (boto3 default chain, GCP ADC, the anthropic SDK's own
     `ANTHROPIC_API_KEY`)
Step 5 is NOT resolved here: when the broker finds nothing it leaves the value unset and the
adapter's SDK falls through to its own chain. That is why aws-textract, google-document-ai and
anthropic-claude work with no OpenReading-specific env on a machine their own provider tooling
already configured. `openreading backends` reads the environment and the keychain only. A backend
whose credentials sit in `~/.aws/credentials` or in an ADC file prints `no` under CONFIGURED and
still runs. Readiness also requires the vars a descriptor's `live_gate_env` names, and only the
environment or the keychain can satisfy those (`openreading.readiness`). `openreading backends` names missing VARS, never values.

The keychain, opt-in
--------------------
The CLI opts in through `use_keychain(True)` for every verb except `serve` and `mcp`, which read
the keychain only when started with `--keychain`. A long-running server that reaches for a
keychain can hang at boot or pop an unlock dialog, which is why it waits for the flag. The Python
API reads it only with `keychain=True`, matching `env_file=`. A broker that reads the keychain
uses `keychain_environ()`, a `ChainMap` over `os.environ` and `openreading.keychain.
KeychainMapping`. A saved value is therefore never copied into `os.environ`, so a subprocess such
as the liteparse worker or `tesseract` never inherits it. With `OPENREADING_KEYCHAIN=off`, without
the `[setup]` extra, or with nothing saved, every lookup resolves exactly as it does without the
keychain. A blank variable counts as unset, so an empty `ANTHROPIC_API_KEY=` line left in `.env`
does not hide a saved key. `EnvCredentialBroker.var_source` names where a variable resolves from
(`env`, `.env`, `keychain`, `missing`) for `openreading backends` and `openreading keys list`.

A provider profile's key is bound to its endpoint
-------------------------------------------------
A provider profile is a backend that `openreading.yaml` defines (`openreading.config`), and that
file often arrives with a repository you cloned. Its key therefore reaches only the endpoint you
gave it, through two rules that apply to profiles alone. `bound_origin` names a profile's origin,
which is the `scheme://host[:port]` of its `base_url`, lowercased, with the default port dropped.

- The keychain holds a profile's key under the account `<VAR>@<origin>`, such as
  `OPENROUTER_API_KEY@https://openrouter.ai` (`keychain_account`). A profile reads only the
  account for its own origin, never the plain `<VAR>` account a built-in uses. Two profiles that
  share `key_env` for two hosts never share a saved key.
- The environment and `.env` reach a profile only when its host is loopback, or its origin is
  listed in an exported `OPENREADING_PROVIDER_ORIGINS` (`EnvCredentialBroker.env_allowed`). A
  value a `.env` file set never counts. Otherwise the value is ignored, and the variable reads as
  missing. `openreading backends`, `keys list`
  (SOURCE `env-ignored`), the MCP backends report and the missing-credentials error each say so,
  by name and setting, never by value. `openreading keys set <profile>` and `openreading setup`
  need no setting, because they save to the bound account.

Failure prevented: a committed profile naming `GITHUB_TOKEN`, or a key you saved for OpenRouter,
with its author's `base_url`. Before the binding, the broker sent that key from your shell or
keychain to the author's host. The built-in-owned-name and https rules in `openreading.config`
still apply on top.

`.env` loading (`load_dotenv`): `KEY=VALUE` lines from a dotenv file enter the environment WITHOUT
overriding an already-set process var — an exported shell variable always wins over the file, so a
value exported in an earlier shell command silently beats the file you just edited. The CLI loads
`./.env` (or `--env-file`) on every invocation, `openreading serve` included.
Values are literal: the loader does not expand tilde, variable references, or shell commands.
For example, use an absolute OCR model path rather than $HOME/models in this file. A program that
imports `openreading.server.app.create_app` directly loads nothing, so it calls `load_dotenv`
itself. `openreading.run(env_file=)` / `run_batch(env_file=)` load only when given. Do NOT copy
`.env.example` wholesale: it pre-fills two localhost endpoints (`DOCLING_SERVE_URL`,
`QWEN_VL_ENDPOINT`), which marks those backends configured on a machine where neither is running
and sends a document there before the router falls back to one that works. Write only the keys
you actually hold, one line at a time: `echo 'REDUCTO_API_KEY=sk_...' >> .env`.

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
  docling_local [docling_local] auth: none
            config: DOCLING_LOCAL_ASSETS, DOCLING_LOCAL_TESSERACT, DOCLING_LOCAL_TESSDATA
            limits: explicit verified assets; OCR requires executable and language data
  liteparse [liteparse] auth: none
            config: LITEPARSE_TESSDATA, LITEPARSE_WORKER_MEMORY_BYTES   ops: parse
            limits: PDF and images in a supervised worker; OCR needs pinned eng tessdata
  llamaparse-fast, llamaparse-cost-effective, llamaparse-agentic, llamaparse-agentic-plus
            [llamaparse]   auth: LLAMA_CLOUD_API_KEY (or LLAMA_PARSE_API_KEY)
            config: LLAMA_CLOUD_BASE_URL   ops: parse
            limits: poll only; each slug pins one tier and dated version; cost optimizer off
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
            confidence
  nuextract [nuextract] auth: NUEXTRACT_API_KEY (or NUMIND_API_KEY)
            config: NUEXTRACT_BASE_URL (on-prem platform)   ops: extract, parse
            limits: typed-template extraction (template != JSON Schema, passed verbatim) +
            NuMarkdown parse (markdown only, NO blocks/bboxes); bytes intake only
  open-ocr  [open-ocr]  auth: OPENOCR_API_KEY   config: OPENOCR_ENGINE (default openocr/tesseract)
            ops: parse   limits: OCR aggregator (~20 engines behind one API); plain text only (NO
            blocks/bboxes); 10MB body cap
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
  google-document-ai [google-document-ai]   auth: GOOGLE_APPLICATION_CREDENTIALS (opt: a
            service-account JSON path, else the ambient ADC chain)
            config: GCP_PROJECT_ID (or GOOGLE_CLOUD_PROJECT), GCP_PROCESSOR_ID, GCP_LOCATION
            (opt, default us)   ops: OCR/FormParser/LayoutParser/CustomExtractor
            limits: sync `:process` <=15 pages (batchProcess not implemented); a non-`us` region
            auto-pins the regional endpoint
  google-gemini [google-gemini]   auth: GEMINI_API_KEY   config: GEMINI_MODEL (opt,
            default gemini-3.6-flash)   ops: parse, extract
            limits: INLINE stateless Interactions; PDF bytes or Gemini Files URI; <=50MB/1000pp;
            whole-document Markdown has no native page attribution
  mistral-ocr [mistral-ocr]   auth: MISTRAL_API_KEY   config: MISTRAL_OCR_MODEL (opt,
            default mistral-ocr-latest)   ops: parse, extract
            limits: INLINE OCR/Document AI; public URL or base64 bytes; native Markdown, blocks,
            bounds and confidence
  <profile> [http, + pymupdf for PDFs]   auth: the profile's `key_env` var (bound to its
            endpoint, see above), or none
            config: none (base_url and model come from openreading.yaml)   ops: parse, extract
            limits: one provider profile per `providers:` entry (`openreading.config`)
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
    surface shows the same key-free sentence, "key was found but rejected by <backend>: check
    <VAR>" (`openreading.readiness.auth_rejected_hint`): single and batch parse, `route --run`,
    `replay`, `calibrate`, and the HTTP body (424 — the same "fix a var" class as missing
    credentials). The provider's own response body is DROPPED, not appended: providers have been
    seen echoing the rejected key back inside it. Never retried, never a silent fall-through.
  * `doc_too_large` — the document exceeds the provider's size/page limit (HTTP 413 or an adapter
    ceiling); the message carries the limit and the resolved chain falls back to a backend with a
    higher ceiling.
  * Every other failure message is redacted (`secret_values` + `redact`, `***`, longest-first so
    overlapping secrets leave no fragments) before it reaches any surface — CLI, HTTP, batch trail
    — which covers a self-hosted or misconfigured backend echoing request context in a verbose
    error body. Non-secret config (endpoint, region, project id) is deliberately not redacted.
  * Liveness reports (`openreading backends --check`, `POST /v1/backends/{id}/liveness`) follow
    the same posture: they name env VARS never values, `detail` is redacted on
    the way out, an `unauthorized` finding reuses the key-free sentence, no field carries the
    endpoint URL (a URL can embed `user:token@host`), and a probe never sends a document, so it can
    never become a data path (`openreading.liveness`, internal/design/liveness.md §3.4).

Time budget: `ctx.deadline_ms` is a BUDGET, not an absolute deadline (DEFAULT_DEADLINE_MS, or
DEFAULT_NATIVE_BATCH_DEADLINE_MS for native batch); callers convert it via
`clock.now_ms() + budget`.

Ledger storage: with `OPENREADING_LEDGER` armed, the ledger root holds headers, JSONL journals,
and plaintext content-addressed blobs. The package never encrypts or expires them. Operators
protect the directory and apply their own deletion schedule (`openreading.ledger`).

Operator knobs that are not credentials (`OPENREADING_LEDGER*`, `OPENREADING_CONFIG`,
`OPENREADING_LLM_DECIDER`, and `OPENREADING_API_KEYS*`) are documented one
line each in `.env.example` and in the module that reads them (`openreading.api`,
`openreading.strategies.loader` / `.decider`, `openreading.server.app`). The server's HTTP posture
(no caller auth by default) lives in `openreading.server.app`.

Environment variables this module reads
---------------------------------------
- `OPENREADING_CREDENTIALS_REF_ALIASES`: the `credentials_ref` allow-list above (BL-162).
  Unset or empty accepts no alias.
- `OPENREADING_PROVIDER_ORIGINS`: comma-separated origins whose provider profiles may read their
  key from the environment and `.env`, such as `https://openrouter.ai,https://api.groq.com`. Each
  entry is normalized like a profile's origin, so a path, a default port or upper case does not
  matter. An entry without a scheme matches nothing. Unset or empty lists none, so only loopback
  profiles read the environment. It counts only when the process environment exported it. A
  value `load_dotenv` set, from `./.env`, `--env-file` or `env_file=`, is ignored, and so is the
  keychain (law PS5). A cloned repository can carry a `.env` that lists its author's origin, so
  a setting that widens where a key goes must come from your shell or service environment.
- Every variable a backend declares in `credentials_spec` and `config_spec`, per the precedence
  above.
"""

from __future__ import annotations

import os
from collections import ChainMap
from collections.abc import Callable, Iterable, Mapping, MutableMapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

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


def dotenv_entries(path: str | os.PathLike[str]) -> list[tuple[int, str, str]]:
    """Parse a dotenv file into `(line_number, KEY, value)` triples, in file order. `export KEY=`,
    `#` comments, blank lines, and single/double-quoted values are handled. A missing file raises
    `OSError`. `load_dotenv` and `openreading keys import` share this one parser, so the two never
    disagree about what a line means."""
    out: list[tuple[int, str, str]] = []
    for number, raw in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
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
        if key:
            out.append((number, key, val))
    return out


# The names `load_dotenv` wrote into `os.environ` in this process. `openreading backends` and
# `openreading keys list` read it to tell a `.env` value from an exported one.
_DOTENV_NAMES: set[str] = set()


def load_dotenv(
    path: str | os.PathLike[str] | None = None,
    environ: MutableMapping[str, str] | None = None,
) -> int:
    """Load `KEY=VALUE` lines from a `.env` file into the environment WITHOUT overriding a var
    that is already set (explicit process env always wins). Returns the number of vars set.
    With `path=None`, loads `./.env` if it exists (missing file → no-op, returns 0). The line
    grammar is `dotenv_entries`."""
    env = environ if environ is not None else os.environ
    p = Path(path) if path is not None else Path(".env")
    if not p.is_file():
        return 0
    count = 0
    for _, key, val in dotenv_entries(p):
        if key in env:
            continue
        env[key] = val
        if environ is None:
            _DOTENV_NAMES.add(key)
        count += 1
    return count


def dotenv_loaded_names() -> frozenset[str]:
    """The variables `load_dotenv` set in `os.environ` during this process."""
    return frozenset(_DOTENV_NAMES)


def declared_secret_envs(descriptors: Iterable[AdapterDescriptor]) -> frozenset[str]:
    """Every service-native variable a backend declares for a SECRET credential field. These are
    the only names the keychain is ever asked about (law PS5). The `OPENREADING_<SLUG>_<KEY>` form
    and every `config_spec` field stay environment-only."""
    return frozenset(
        name for d in descriptors for f in d.credentials_spec if f.secret for name in f.env
    )


PROVIDER_ORIGINS_ENV = "OPENREADING_PROVIDER_ORIGINS"
_DEFAULT_PORTS = {"http": 80, "https": 443}


def endpoint_origin(url: str) -> str:
    """The origin of `url`: `scheme://host[:port]`, lowercased, with the scheme's default port
    dropped and any path left out. `HTTPS://Api.Example:443/v1` is `https://api.example`. Raises
    `ValueError` for a port that is not a number."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if ":" in host:
        host = f"[{host}]"
    port = parts.port
    netloc = host if port is None or port == _DEFAULT_PORTS.get(scheme) else f"{host}:{port}"
    return f"{scheme}://{netloc}"


def bound_origin(descriptor: AdapterDescriptor) -> str | None:
    """The origin a provider profile's key is bound to, or None for every other backend.

    A backend is a profile when the profile set active in this context
    (`openreading.adapters.registry.active_profiles`) holds its id with a `key_env`. Profiles
    cannot shadow a built-in id (`openreading.config`), so a built-in never answers here."""
    from openreading.adapters.registry import active_profiles

    profile = active_profiles().get(descriptor.id)
    if profile is None or profile.key_env is None:
        return None
    return endpoint_origin(profile.base_url)


def keychain_account(descriptor: AdapterDescriptor, name: str) -> str:
    """The keychain account that holds `name` for this backend: `name` itself for a built-in,
    `name@origin` for a provider profile, such as `OPENROUTER_API_KEY@https://openrouter.ai`."""
    origin = bound_origin(descriptor)
    return name if origin is None else f"{name}@{origin}"


def keychain_accounts(descriptors: Iterable[AdapterDescriptor]) -> frozenset[str]:
    """Every keychain account the broker may read for `descriptors`: each declared secret
    variable (`declared_secret_envs`) under the account `keychain_account` gives it."""
    return frozenset(
        keychain_account(d, name)
        for d in descriptors
        for f in d.credentials_spec
        if f.secret
        for name in f.env
    )


def provider_origins_from_dotenv(environ: Mapping[str, str] | None = None) -> bool:
    """True when `OPENREADING_PROVIDER_ORIGINS` in `environ` (default `os.environ`) was set by
    `load_dotenv`, from `./.env`, `--env-file` or `env_file=`, rather than exported."""
    env = os.environ if environ is None else environ
    return (
        env is os.environ and PROVIDER_ORIGINS_ENV in _DOTENV_NAMES and PROVIDER_ORIGINS_ENV in env
    )


def provider_origins(environ: Mapping[str, str] | None = None) -> frozenset[str]:
    """The origins `OPENREADING_PROVIDER_ORIGINS` lists, normalized by `endpoint_origin`. An
    entry without a scheme and a host matches nothing. Unset or empty lists none, and so does a
    value a dotenv file set (`provider_origins_from_dotenv`). A cloned repository's `.env` would
    otherwise allow-list its author's host for the profile its `openreading.yaml` declares."""
    env = os.environ if environ is None else environ
    if provider_origins_from_dotenv(env):
        return frozenset()
    out: set[str] = set()
    for entry in env.get(PROVIDER_ORIGINS_ENV, "").split(","):
        if "://" not in entry:
            continue
        try:
            origin = endpoint_origin(entry)
        except ValueError:
            continue
        if not origin.endswith("://"):
            out.add(origin)
    return frozenset(out)


def env_ignored_reason() -> str:
    """Why a remote provider profile does not read its key from the environment, as text that
    follows "not sent to <origin>,". Names the setting, never a value."""
    if provider_origins_from_dotenv():
        return (
            f"because {PROVIDER_ORIGINS_ENV} was set by a .env file, and a file cannot widen "
            "where a key goes. To allow that origin, export it in your shell instead."
        )
    return f"because {PROVIDER_ORIGINS_ENV} does not list that origin."


def _loopback_origin(origin: str) -> bool:
    from openreading.config import _is_loopback  # lazy: config is not needed on most runs

    return _is_loopback(urlsplit(origin).hostname or "")


def _registered_secret_envs() -> frozenset[str]:
    # Imported here, not at module top: the adapter registry imports modules that import this one.
    # `known_backends`, not the built-ins alone: a provider profile's key is a declared secret
    # too, so `openreading keys set <profile>` saves a key the broker can read back. Its account
    # is bound to the profile's origin, so the plain `key_env` name never joins this set.
    from openreading.adapters.registry import known_backends

    return keychain_accounts(factory().descriptor for factory in known_backends().values())


class _KeychainChain(ChainMap[str, str]):
    """`ChainMap(os.environ, KeychainMapping)` with the broker's own notion of "set".

    The broker already treats an empty variable as unset and moves to the next name. A blank
    `ANTHROPIC_API_KEY=` line left in `.env` would otherwise hide the saved key behind a value the
    broker then ignores. So a lookup skips an empty value and returns the first non-empty one, and
    returns the empty one only when no later layer has a value. Writes go to `os.environ` as with
    any `ChainMap`, and the broker never writes."""

    def __getitem__(self, key: str) -> str:
        empty: str | None = None
        for mapping in self.maps:
            try:
                value = mapping[key]
            except KeyError:
                continue
            if value:
                return value
            if empty is None:
                empty = value
        if empty is not None:
            return empty
        raise KeyError(key)

    def get(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        try:
            return self[key]
        except KeyError:
            return default


def keychain_environ(names: Iterable[str] | None = None) -> MutableMapping[str, str]:
    """The process environment with the keychain behind it: `os.environ` first, then the keychain
    for the declared secret variables only. `names` narrows the set, and defaults to every
    variable a registered backend declares. Keychain values are read through this view and are
    never copied into `os.environ` (law PS3)."""
    from openreading.keychain import KeychainMapping

    allowed: Iterable[str] | Callable[[], Iterable[str]] = (
        _registered_secret_envs if names is None else frozenset(names)
    )
    return _KeychainChain(os.environ, KeychainMapping(allowed))


# The process-level opt-in. False until a surface asks: the CLI does for every verb but `serve`
# and `mcp`, and `openreading serve --keychain` does for the server.
_USE_KEYCHAIN = False


def use_keychain(on: bool) -> bool:
    """Make every `EnvCredentialBroker()` built without an explicit `environ` read the keychain
    (`on=True`) or the environment alone (`on=False`). Returns the previous setting, so a caller
    can restore it."""
    global _USE_KEYCHAIN
    previous, _USE_KEYCHAIN = _USE_KEYCHAIN, on
    return previous


def keychain_in_use() -> bool:
    """True when this process opted in through `use_keychain(True)`."""
    return _USE_KEYCHAIN


def _slug_env(slug: str, key: str) -> str:
    """The OpenReading override env var for a (backend, key) pair: OPENREADING_<SLUG>_<KEY>."""
    return f"OPENREADING_{slug.replace('-', '_').upper()}_{key.upper()}"


def _credentials_ref_aliases(env: Mapping[str, str]) -> frozenset[str]:
    """BL-162: `credentials_ref: "env:<alias>"` may only select a prefix the OPERATOR has
    explicitly allow-listed via `OPENREADING_CREDENTIALS_REF_ALIASES` (comma-separated) — never an
    arbitrary caller-chosen env-var prefix. Before this, `env:<anything>` resolved every
    credential key as `<anything>_<KEY>`, so an unauthenticated request could name any prefix and
    read whatever environment variable happened to end in `_<KEY>` for that backend (e.g.
    `env:ANTHROPIC_API` against a field keyed `key` reads `ANTHROPIC_API_KEY`). Unset/empty means
    no alias is accepted. This fails closed when configuration is ambiguous."""
    return frozenset(
        s.strip()
        for s in env.get("OPENREADING_CREDENTIALS_REF_ALIASES", "").split(",")
        if s.strip()
    )


class EnvCredentialBroker:
    """Resolves a descriptor's credential/config spec from the environment. Inject an explicit
    `environ` mapping in tests. The default is `os.environ`, or `keychain_environ()` once the
    process called `use_keychain(True)`."""

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        if environ is None:
            environ = keychain_environ() if _USE_KEYCHAIN else os.environ
        self._env: Mapping[str, str] = environ

    def _layers(self) -> list[Mapping[str, str]]:
        """The environment first, then the keychain when this broker reads it."""
        return list(self._env.maps) if isinstance(self._env, _KeychainChain) else [self._env]

    def env_allowed(self, origin: str | None) -> bool:
        """Whether a backend bound to `origin` may read its key from the environment and `.env`.
        Always for a built-in (`origin` None). For a provider profile, only when the origin is
        loopback or `OPENREADING_PROVIDER_ORIGINS` lists it. The list is read from the
        environment layer alone, never the keychain (law PS5)."""
        if origin is None or _loopback_origin(origin):
            return True
        return origin in provider_origins(self._layers()[0])

    def var_source(self, name: str, descriptor: AdapterDescriptor | None = None) -> str:
        """Where `name` resolves from for this broker: `env` (exported), `.env` (set by
        `load_dotenv`), `keychain`, or `missing`. Names a place, never a value. With a provider
        profile's `descriptor`, the environment counts only when `env_allowed` says so, and the
        keychain is asked for the profile's bound account (`keychain_account`)."""
        origin = bound_origin(descriptor) if descriptor is not None else None
        layers = self._layers()
        first = layers[0]
        if first.get(name) and self.env_allowed(origin):
            return ".env" if first is os.environ and name in _DOTENV_NAMES else "env"
        account = name if origin is None else f"{name}@{origin}"
        if any(layer.get(account) for layer in layers[1:]):
            return "keychain"
        return "missing"

    def ignored_env(self, descriptor: AdapterDescriptor) -> list[str]:
        """The credential variables set in the environment that this backend does not read,
        because it is a provider profile whose origin `env_allowed` refuses. Names only."""
        origin = bound_origin(descriptor)
        if origin is None or self.env_allowed(origin):
            return []
        first = self._layers()[0]
        names = [
            n
            for f in descriptor.credentials_spec
            for n in (_slug_env(descriptor.id, f.key), *f.env)
        ]
        return [n for n in dict.fromkeys(names) if first.get(n)]

    def secret_sources(self, descriptor: AdapterDescriptor) -> list[tuple[str, str]]:
        """One `(VAR, SOURCE)` pair per secret credential field, in descriptor order. VAR is the
        name that resolves (the `OPENREADING_<SLUG>_<KEY>` form included), else the field's
        primary variable with SOURCE `missing`. The pick follows `_lookup`: every name in the
        environment first, then the keychain. `openreading keys list` prints these rows."""
        rows: list[tuple[str, str]] = []
        for f in descriptor.credentials_spec:
            if not f.secret:
                continue
            names = [_slug_env(descriptor.id, f.key), *f.env]
            sources = [(n, self.var_source(n, descriptor)) for n in names]
            found = next(((n, src) for n, src in sources if src in ("env", ".env")), None)
            found = found or next(((n, src) for n, src in sources if src == "keychain"), None)
            rows.append(found or (f.env[0] if f.env else names[0], "missing"))
        return rows

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
            # BL-162: this message does NOT enumerate the configured allow-list. It
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
        *,
        origin: str | None = None,
    ) -> str | None:
        if request_value is not None:
            return request_value
        names = [_slug_env(slug, field.key), *field.env]
        if ref_prefix is not None:
            names.insert(0, f"{ref_prefix}_{field.key.upper()}")
        # Law PS2: every name is tried in the environment before any name in the keychain. Asked
        # name by name through the chain, a value saved under LLAMA_CLOUD_API_KEY beat an exported
        # LLAMA_PARSE_API_KEY, so an export made for this shell lost to a key saved weeks ago.
        layers = self._layers()
        for index, layer in enumerate(layers):
            if index == 0 and not self.env_allowed(origin):
                # A profile's key reaches its endpoint from the environment only with the
                # operator's consent. Without it, a committed openreading.yaml could name
                # GITHUB_TOKEN with its author's base_url and receive your token.
                continue
            for name in names:
                account = name if index == 0 or origin is None else f"{name}@{origin}"
                v = layer.get(account)
                if v:
                    return v
        return None

    def resolve(
        self, descriptor: AdapterDescriptor, req: OpenReadingRequest
    ) -> ResolvedCredentials:
        """Secrets/credentials from credentials_spec → ResolvedCredentials.values. Keys the broker
        can't find are left out (the adapter's ambient SDK chain, if any, takes over). A provider
        profile's key follows the binding in the module docstring."""
        prefix = self._ref_prefix(req)
        origin = bound_origin(descriptor)
        values: dict[str, object] = {}
        for f in descriptor.credentials_spec:
            v = self._lookup(descriptor.id, f, prefix, origin=origin)
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
    RunContext. Fills resolved credentials, runtime config, the idempotency key, and the time
    budget. Bare RunContext() construction outside tests is a defect.

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
