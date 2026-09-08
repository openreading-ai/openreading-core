"""Public one-call API for document execution, routing, batching, and resume.

The CLI and HTTP server call this module, so every surface shares one execution path.

    import openreading
    doc = openreading.run("loan.pdf", backend="reducto")
    doc = openreading.run("loan.pdf")
    doc = openreading.run("loan.pdf", strategy="main")
    plan = openreading.route("loan.pdf")
    env = openreading.run_batch(["invoices/"], backend="pymupdf", jobs=4)
    doc = openreading.resume("7dbf6b71-adb5-4e90-9188-a184fdba9d05")

Exports and return shapes
-------------------------
An envelope is the schema-valid JSON object returned by a completed operation.

- `run(source, backend=None, **options)` returns a `response.v0.3` envelope.
- `run_batch(sources, backend=None, **options)` returns a `batch-result.v0.2` envelope.
- `route(source, **options)` returns a `RoutePlan` without executing a backend.
- `resume_run(run_id)` returns a response and is exported as `openreading.resume`.
- `build_request`, `run_request`, `prepare_named_backend`, and `materialize_document` support
  the CLI and server through the same lower-level seams.

`source` accepts a path, an HTTP URL, or raw bytes. It never accepts a request mapping.
`openreading.derive.mime` resolves an explicit type, then content, then filename. Unknown content
keeps `mime_type=None`, because inventing `application/pdf` can produce a confident wrong parse.
A missing path raises `SourceNotFoundError`, which retains ordinary `OSError` attributes.

Backend resolution
------------------
`backend` accepts a registry slug, `None`, or the reserved `strategy:<name>` prefix.
`strategy=<name>` is sugar for that prefix. `strategy:none` forces the ordinary resolved chain.
When no backend is named, `policy.backends` supplies the chain in written order.
When that list is absent, the chain contains only `pymupdf` as the configuration-free default.
An empty or unknown-only list raises `ScopeRefused` before any backend runs.

`policy.backends` is a default for automation, not an enforcement boundary for a named backend.
The server's `OPENREADING_API_KEY_SCOPES` setting supplies that boundary for separate callers.
Every active scope intersects with the resolved chain, and an empty intersection returns 403.

An `openreading.yaml` comes from explicit `config`, `OPENREADING_CONFIG`, or the working directory.
The server disables working-directory discovery. A malformed file raises `ConfigError` before
dispatch, including an unknown policy key or a value with the wrong type.

Execution and errors
--------------------
A named backend is credential-checked and executed once. A resolved chain tries its entries in
order and records each failure before continuing. Strategies execute their compiled orchestration
tree and preserve the same caller scope at every dispatch.

`KeyError` means the named backend is unknown. `UnknownStrategyError` means a strategy is absent.
`ScopeRefused` means the caller permitted no runnable backend. `MissingCredentialsError` names
the missing variables. `PlanExhaustedError` carries the trail from a non-empty failed chain.
Adapter failures retain their typed `TerminalError`, `RetryableError`, or feature error taxonomy.

`deadline_ms` applies to a directly named backend. Strategies manage their own node budgets.
`run_batch(deadline_ms=)` overrides the native batch deadline. An explicit zero remains zero.
Library and CLI calls do not use the server's bounded idempotency result cache.

Batch semantics
---------------
Batch intake expands visible files, directories, globs, and URLs without filtering by format.
Every source is dispatched. A backend refusal becomes a failed item with its original reason.
`jobs` is bounded before intake, and `max_items` caps the expansion count.
Native batch dispatch requires a named backend implementing `NativeBatchAdapter` within its limit.
All other batches fan out through `run`, isolating failures in their individual items.

Environment variables read by this module
-----------------------------------------
`OPENREADING_LEDGER` names a directory that stores every strategy document and full response in
plaintext. It creates the journal, header, and `blobs/` entries required by resume.
Nothing here encrypts, expires, or deletes that data. The operator owns its storage policy.

`OPENREADING_ALLOW_PRIVATE_URLS` disables public-address validation when set to any non-empty value.
Without it, URL materialization refuses private destinations, redirects, and oversized responses.
The connection is pinned to the vetted address to prevent DNS rebinding between validation and use.

`env_file` loads key-value lines without overriding existing process variables. A library call
with no `env_file` never discovers `.env` from the working directory.
Backend credentials and aliases are read indirectly through `openreading.credentials`.
`OPENREADING_CONFIG` is read through `openreading.config` during configuration discovery.

Resume contract
---------------
Only strategy runs arm the ledger. Named backend runs and native batches do not journal.
Resume rebuilds the request from the header and content-addressed plaintext blob store.
Stored bytes are verified against each `BlobRef` digest before they are trusted.
The live configuration and plan must match the recorded header, or `HeaderMismatch` refuses.
Recorded terminal steps replay without network access. Previously unreached steps execute normally.
Passwords and webhook URLs are excluded from the header and cannot be recovered on resume.
"""

from __future__ import annotations

import base64
import dataclasses
import errno
import hashlib
import os
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from openreading.adapters._http import error_for_status
from openreading.adapters.registry import build_registry, make_adapter
from openreading.batch.runner import MAX_BATCH_JOBS
from openreading.batch.sources import DEFAULT_MAX_ITEMS
from openreading.config import LoadedFile
from openreading.config import apply as apply_config
from openreading.config import load as load_config_file
from openreading.credentials import (
    DEFAULT_NATIVE_BATCH_DEADLINE_MS,
    EnvCredentialBroker,
    build_run_context,
    load_dotenv,
    secret_values,
)
from openreading.derive.mime import resolve_mime_type
from openreading.ledger.header import (
    JOURNAL_VERSION,
    HeaderMismatch,
    RunHeader,
    compare_header,
    read_header,
    registry_fingerprint,
    slim_request,
    slim_request_dict,
    write_header,
)
from openreading.ledger.header import (
    plan_hash as compute_plan_hash,
)
from openreading.ledger.inline import InlineExecutor, descriptor_digest
from openreading.ledger.jsonl import JsonlJournal
from openreading.ledger.localfs import LocalFsBlobStore
from openreading.ledger.ports import Executor, LedgerArmingError
from openreading.ledger.sanitizer import Sanitizer
from openreading.readiness import auth_hinted, missing_required
from openreading.router.clock import RealClock
from openreading.router.cost import apply_cost_report
from openreading.router.driver import run_to_completion
from openreading.router.executor import BoundedResultCache, execute_plan
from openreading.router.router import RoutePlan, Router, RouterConfig
from openreading.types.errors import (
    AdapterError,
    MissingCredentialsError,
    ScopeRefused,
    SourceNotFoundError,
    TerminalError,
    UnknownStrategyError,
)
from openreading.types.request import OpenReadingRequest

# Reserved `backend.id` prefix for a strategy reference (spec §1.3 / loader.STRATEGY_PREFIX).
# Inlined here so a plain named-backend run never imports the strategy package (guardrail T10).
_STRATEGY_PREFIX = "strategy:"

_MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024  # 100 MB
# Removed keywords that `**request_overrides` would otherwise swallow. `policy=` is the one that
# matters: it was a real parameter until the file became the only container, and left unguarded it
# lands in the overrides bag, where `run()` reports it as an unknown request field and
# `run_batch()` ignores it entirely. Either way the constraints the caller wrote do not apply.
_REMOVED_KWARGS = {
    "policy": "the policy keyword was removed; pass the file's own shape as "
    'config={"version": 1, "policy": {...}}, or a path to an openreading.yaml',
}


def _refuse_removed_kwargs(overrides: dict[str, Any]) -> None:
    for name, hint in _REMOVED_KWARGS.items():
        if name in overrides:
            raise TypeError(hint)


def _document_dict(source: str | bytes, mime_type: str | None) -> dict[str, Any]:
    if isinstance(source, bytes | bytearray):
        # D-v2-9 defaulted these to PDF. Superseded: bytes with no name are the case core knows
        # LEAST about, so inventing a type here was the least defensible of the six guesses it
        # used to make. Sniff them, and answer None when the signature is unknown.
        raw = bytes(source)
        return {
            "bytes_base64": base64.b64encode(raw).decode(),
            "mime_type": resolve_mime_type(mime_type=mime_type, data=raw),
        }
    s = str(source)
    if s.startswith(("http://", "https://")):
        # Preserve the caller's explicit mime_type (None is valid on DocumentInput). There are no
        # bytes to sniff until `materialize_document` downloads them, and that is where the type
        # is resolved for a URL.
        return {"url": s, "mime_type": mime_type}
    p = Path(s)
    if not p.exists():
        # A missing path is refused here so that every Python entry point shares one guard:
        # route(), build_request(), run() and run_batch() (BL-133). It is built with the stdlib's
        # errno-style OSError signature (errno, strerror, filename) so `.strerror` is populated and
        # `str(e)` renders "[Errno 2] ..." exactly as a real FileNotFoundError does (BL-141,
        # BL-143). `cli/app.py`'s `_describe_read_error` relies on that, and so do the two sites in
        # cmd_parse and _cmd_parse_batch that format the exception with a bare `str(e)`.
        raise SourceNotFoundError(errno.ENOENT, "no such file or directory", s)
    raw = p.read_bytes()
    return {
        "bytes_base64": base64.b64encode(raw).decode(),
        "mime_type": resolve_mime_type(mime_type=mime_type, filename=p.name, data=raw),
        "filename": p.name,
    }


def build_request(
    source: str | bytes,
    backend: str | None = None,
    *,
    operation: str | None = None,
    mime_type: str | None = None,
    **overrides: Any,
) -> OpenReadingRequest:
    """Build the `OpenReadingRequest` that the CLI and the server hand to `run_request`.

    `document` and `backend` are derived from `source=` and `backend=` alone. Passing either one
    through `**overrides` raises `ValueError` (BL-105). A named backend's `type` is filled in from
    its descriptor, and a `strategy:<name>` id is never looked up in the registry (T1). The
    `openreading.yaml` `policy:` block is not read here: `openreading.config.apply` folds it into
    the request this returns, once, in whichever call is about to dispatch.
    """
    # The failure this refuses: an overrides bag carrying `document=` or `backend=` won over the
    # request's real document with no error of any kind, the Python-API twin of the /v1/batch
    # `shared` merge bug (BL-105). Reject by name rather than drop, so a caller who forwarded a
    # stray kwarg through run()'s or run_batch()'s passthrough sees the mistake immediately.
    reserved = sorted(set(overrides) & {"document", "backend"})
    if reserved:
        raise ValueError(
            f"build_request() overrides cannot set {', '.join(reserved)} — document/backend come "
            "from source=/backend=, never the overrides bag"
        )
    body: dict[str, Any] = {
        "document": _document_dict(source, mime_type),
        "backend": {"id": backend},
    }
    # A `strategy:<name>` id is a reserved prefix, not a registry slug — never make_adapter it (T1).
    if backend is not None and not backend.startswith("strategy:"):
        body["backend"]["type"] = make_adapter(backend).descriptor.type.value
    if operation:
        body["backend"]["operation"] = operation
    for k, v in overrides.items():
        if v is not None:
            body[k] = v
    return OpenReadingRequest.model_validate(body)


def _assert_public_http_url(url: str) -> str:
    """Refuse URL schemes and destinations a hosted parse must never fetch on a caller's behalf:
    non-http(s), and hosts resolving to loopback/private/link-local/reserved addresses (cloud
    metadata endpoints included). Returns the ONE vetted address the caller must then connect to —
    see `_download`, which pins the connection to it.

    Returning the address rather than just approving the name is what closes DNS rebinding. Every
    answer is checked, and the first is handed back; resolving again at connect time would re-ask
    a resolver whose answer the attacker controls and can change between the two calls, which is
    the whole trick. `OPENREADING_ALLOW_PRIVATE_URLS=1` disables the address check (and, with it,
    the pinning) for intranet document stores."""
    import ipaddress
    import socket
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise TerminalError(
            f"unsupported URL scheme {parsed.scheme!r}", backend_code="unsupported_input"
        )
    host = parsed.hostname
    if not host:
        raise TerminalError("URL has no host", backend_code="unsupported_input")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise TerminalError(f"cannot resolve {host!r}", backend_code="url_not_public") from exc
    vetted = ""
    for info in infos:
        addr = ipaddress.ip_address(info[4][0])
        if not addr.is_global or addr.is_multicast:
            raise TerminalError(
                f"URL host {host!r} resolves to a non-public address",
                backend_code="url_not_public",
            )
        # EVERY answer is checked before any is used, so a resolver that mixes one public address
        # in with a private one cannot get the private one approved by ordering.
        vetted = vetted or str(addr)
    if not vetted:
        raise TerminalError(f"cannot resolve {host!r}", backend_code="url_not_public")
    return vetted


def _pin_to_address(url: str, address: str) -> tuple[str, dict[str, str], dict[str, str]]:
    """`(url, headers, extensions)` for fetching `url` from exactly `address`.

    The URL's host is swapped for the literal address so no name is resolved a second time; the
    original host rides in `Host` (virtual hosting still routes) and in `sni_hostname` (TLS still
    presents and verifies the right certificate). An IPv6 literal is bracketed, as a URL authority
    requires."""
    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(url)
    literal = f"[{address}]" if ":" in address else address
    netloc = f"{literal}:{parsed.port}" if parsed.port else literal
    pinned = urlunparse(parsed._replace(netloc=netloc))
    assert parsed.hostname is not None  # _assert_public_http_url refuses a host-less URL
    return pinned, {"Host": parsed.netloc}, {"sni_hostname": parsed.hostname}


def _download(url: str, *, transport=None) -> bytes:
    import httpx  # lazy — only when a URL is actually materialized

    target, headers, extensions = url, {}, {}
    if not os.environ.get("OPENREADING_ALLOW_PRIVATE_URLS"):
        target, headers, extensions = _pin_to_address(url, _assert_public_http_url(url))
    client = (
        httpx.Client(transport=transport, timeout=60.0) if transport else httpx.Client(timeout=60.0)
    )
    with client, client.stream("GET", target, headers=headers, extensions=extensions) as r:
        # Redirects are off (httpx's default), so a 3xx never reaches `>= 400` and used to be read
        # as a successful zero-byte document. It is refused rather than followed: a redirect is the
        # ordinary way to walk a vetted address to an unvetted one, and following it would need the
        # whole guard above re-run per hop.
        if 300 <= r.status_code < 400:
            raise TerminalError(
                f"{url} redirected to {r.headers.get('location', '?')!r}; redirects are not followed",
                backend_code="url_not_public",
            )
        if r.status_code >= 400:
            raise error_for_status(r.status_code, r.headers, message=f"fetch {url}")
        declared = r.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > _MAX_DOWNLOAD_BYTES:
            raise TerminalError(
                f"document at {url} exceeds {_MAX_DOWNLOAD_BYTES} bytes",
                backend_code="doc_too_large",
            )
        chunks: list[bytes] = []
        total = 0
        # Stream with a running count: buffering the whole body first (r.content) would let an
        # oversized or endless response exhaust memory before the limit was ever consulted.
        for chunk in r.iter_bytes():
            total += len(chunk)
            if total > _MAX_DOWNLOAD_BYTES:
                raise TerminalError(
                    f"document at {url} exceeds {_MAX_DOWNLOAD_BYTES} bytes",
                    backend_code="doc_too_large",
                )
            chunks.append(chunk)
    return b"".join(chunks)


def materialize_document(req: OpenReadingRequest, descriptor=None, *, transport=None):
    """If the document is a URL and the target backend can't ingest URLs natively (accepts_url),
    download it to bytes so the backend can run. Backends that accept URLs get the URL untouched.
    `descriptor=None` forces materialization when any resolved chain member needs
    bytes). Offline tests inject an httpx transport; no default path performs network I/O."""
    d = req.document
    if not d.url:
        return req
    if descriptor is not None and descriptor.accepts_url:
        return req
    data = _download(d.url, transport=transport)
    new_doc = d.model_copy(
        update={
            "url": None,
            "bytes_base64": base64.b64encode(data).decode(),
            "mime_type": resolve_mime_type(mime_type=d.mime_type, filename=d.filename, data=data),
        }
    )
    return req.model_copy(update={"document": new_doc})


def route(
    source: str | bytes,
    *,
    config: str | os.PathLike[str] | dict | None = None,
    operation: str | None = None,
    mime_type: str | None = None,
) -> RoutePlan:
    """The routing plan for a document (no execution): the chain that would be tried, in order.
    `config` is a path to an openreading.yaml or a dict of its shape, and without it
    `./openreading.yaml` is discovered. The file's `policy.backends` IS the plan."""
    loaded = load_config_file(config)
    req = build_request(source, None, operation=operation, mime_type=mime_type)
    req, cfg = apply_config(req, loaded.policy if loaded else None, RouterConfig())
    return Router(build_registry(), cfg).route(req)


def _arm_ledger(*args, **kwargs) -> Executor | None:
    """`_arm_ledger_unguarded` with one guarantee added: every OSError it raises is reported as the
    ledger's, by name.

    Arming is entirely filesystem work under `$OPENREADING_LEDGER`, including the journal,
    header, and document blob. It happens
    before any backend runs, so nothing else in this call can raise an OSError to be confused with
    it. Left bare, an unwritable or non-directory ledger root surfaced as `[strategy:s] error:
    PermissionError: [Errno 13] ...` at exit 1, the "unexpected error" rung, naming a path and an
    errno but never the variable that put it there — so an operator who had just turned resume on
    read it as a bug in the parse.
    """
    try:
        return _arm_ledger_unguarded(*args, **kwargs)
    except OSError as e:
        raise LedgerArmingError(os.environ.get("OPENREADING_LEDGER", ""), e) from e


def _arm_ledger_unguarded(
    run_id: str,
    req,
    registry,
    broker,
    clock,
    eligible: list[str],
    *,
    config_hash: str = "",
    plan_tree: dict[str, Any] | None = None,
    strategy_name: str = "",
    resume: bool = False,
) -> Executor | None:
    """Constructs T1's real `InlineExecutor` when `OPENREADING_LEDGER` is set (a directory path;
    no flag, per L1). Unset ⇒ `None`, and `run_strategy`'s own default (an unarmed InlineExecutor,
    `NullJournal` + `blobs=None`) applies — the L1 zero-delta path (internal/design/ledger.md, plan §6).

    The `Sanitizer` backstop (§9.3) is armed with every RESOLVED descriptor's
    actually-resolved secret values (unchanged, unaffected by the above) — a static, no-value
    `Sanitizer()` never has anything to scrub against.

    Ledger T3 (plan §4.2/§4.4): a FRESH run (`resume=False`, the default — every pre-existing
    caller) writes the run's header once, at this first arm, and does NOT pass `pinned_eligible=`
    to `InlineExecutor` (T1's own framing stands for a fresh run: "the gate is inert... there is no
    second worker to disagree with the pinned set"). A RESUME (`resume=True`, `openreading resume
    <RUN_ID>`'s own call) instead reads that header, compares it against a freshly-recomputed one
    from the LIVE `openreading.yaml`/registry, raises `HeaderMismatch` on any hard-field
    disagreement (AC-4) — before touching the journal further — and, on a match, arms the resumed
    `InlineExecutor` WITH `pinned_eligible=` sourced from the header, which is what gives AC-14's
    gate teeth."""
    root = os.environ.get("OPENREADING_LEDGER")
    if not root:
        return None
    ledger_root = Path(root)
    blobs = LocalFsBlobStore(ledger_root / "blobs")
    journal = JsonlJournal(ledger_root / f"{run_id}.jsonl")

    descriptors = [
        registry.get(bid).descriptor for bid in eligible if registry.get(bid) is not None
    ]

    secrets_seen: set[str] = set()
    for desc in descriptors:
        secrets_seen |= secret_values(desc, broker.resolve(desc, req))
    sanitizer = Sanitizer(frozenset(secrets_seen))

    pinned_map = {
        bid: descriptor_digest(registry.get(bid).descriptor)
        for bid in eligible
        if registry.get(bid) is not None
    }
    fresh_header = RunHeader(
        run_id=run_id,
        config_hash=config_hash,
        plan_hash=compute_plan_hash(plan_tree or {}),
        registry_fingerprint=registry_fingerprint(),
        journal_version=JOURNAL_VERSION,
        pinned_eligible=pinned_map,
        strategy_name=strategy_name,
    )

    pinned_eligible = None  # T1's own inert-by-default framing — armed only on resume (AC-14)
    if resume:
        old_header = read_header(ledger_root, run_id)
        if old_header is None:
            raise HeaderMismatch(run_id, [("header", "present", "missing")])
        mismatches = compare_header(old_header, fresh_header)
        if mismatches:
            raise HeaderMismatch(run_id, mismatches)
        pinned_eligible = old_header.pinned_eligible
    else:
        document_ref = None
        document_is_url = False
        if req.document.bytes_base64 is not None:
            raw = base64.b64decode(req.document.bytes_base64)
            digest = "sha256:" + hashlib.sha256(raw).hexdigest()
            document_ref = blobs.put(
                run_id, digest, raw, req.document.mime_type or "application/octet-stream"
            )
        elif req.document.url is not None:
            # A URL routinely contains a bearer token. The blob store is plaintext after removal
            # of ledger encryption, so retaining the URL would persist that token verbatim. Leave
            # the document absent from the header. The fresh run still has its in-memory request,
            # while resume reports the missing input through `_request_from_header`.
            document_ref = None
            document_is_url = True
        write_header(
            ledger_root,
            dataclasses.replace(
                fresh_header,
                document=document_ref,
                document_is_url=document_is_url,
                slim_request=slim_request_dict(req),
            ),
            sanitizer=sanitizer,
        )

    return InlineExecutor(
        journal=journal,
        blobs=blobs,
        registry=registry,
        clock=clock,
        pinned_eligible=pinned_eligible,
        sanitizer=sanitizer,
        ledger_root=ledger_root,
    )


def _run_strategy_request(
    req: OpenReadingRequest,
    name: str,
    strategy_config,
    *,
    broker: EnvCredentialBroker,
    config: RouterConfig,
    transport,
    keep_candidates: bool = False,
    plain_info=None,
    on_run_armed: Callable[[str], None] | None = None,
    backend_allowlist: frozenset[str] | None = None,
) -> dict[str, Any]:
    """Compile + run a named strategy, embedding the orchestration block into the response.
    Raises UnknownStrategyError (→ 400 / exit 2) when the name is absent. `plain_info` (from the
    loader) lets a Plain strategy's gate records carry their source word for `explain` (§9).
    `backend_allowlist` is the caller's ceiling on which backends the walk may reach; it is
    enforced in compile_strategy, which prunes every out-of-scope leaf, and re-checked at every
    dispatch. It raises ScopeRefused (→ 403 scope_denied) when it leaves the walk nothing to
    run."""
    from openreading.strategies import compile_strategy, run_strategy
    from openreading.strategies.model import StrategyConfig
    from openreading.strategies.presets import PRESET_NAMES

    known = (set(strategy_config.strategies) if strategy_config else set()) | PRESET_NAMES
    if name not in known:
        raise UnknownStrategyError(
            f"unknown strategy {name!r}; defined: {', '.join(sorted(known))}",
            name=name,
        )
    if strategy_config is None:
        # No openreading.yaml anywhere — the presets alone are the library (spec §2.8: presets
        # are normative and available configless, exactly as the Strategies page and
        # `strategy list` already treat them). An empty v1 config carries no policy/limits/
        # defaults, so compilation sees only the requested strategy.
        strategy_config = StrategyConfig(version=1)
    registry = build_registry()
    compiled = compile_strategy(
        req,
        name,
        strategy_config,
        registry,
        config,
        plain_info=plain_info,
        backend_allowlist=backend_allowlist,
        broker=broker,
    )
    # Materialize when any backend in the resolved strategy cannot ingest URLs.
    if any(
        not (a := registry.get(bid)) or not a.descriptor.accepts_url
        for bid in compiled.dispatchable
    ):
        req = materialize_document(req, transport=transport)
    run_id = str(uuid.uuid4())
    clock = RealClock()
    executor = _arm_ledger(
        run_id,
        req,
        registry,
        broker,
        clock,
        compiled.dispatchable,
        config_hash=compiled.config_hash,
        plan_tree=compiled.root,
        strategy_name=name,
    )
    if executor is not None and on_run_armed is not None:
        # Ledger T3 (plan §4.4): fired once, immediately after arming succeeds, so a caller (the
        # CLI's `cmd_parse`) can capture the run id into a local variable BEFORE the walk
        # proceeds — the only way its own `except KeyboardInterrupt:` handler can name a real,
        # resumable run id if the walk is interrupted mid-flight.
        on_run_armed(run_id)
    result = run_strategy(
        compiled,
        req,
        registry=registry,
        broker=broker,
        clock=clock,
        keep_candidates=keep_candidates,
        run_id=run_id,
        executor=executor,
    )
    result.response.orchestration = result.orchestration
    return result.response.to_schema_dict()


def prepare_named_backend(
    req: OpenReadingRequest,
    backend: str,
    *,
    broker: EnvCredentialBroker | None = None,
    config: RouterConfig | None = None,
    transport=None,
    deadline_ms: int | None = None,
):
    """Resolve a directly-named backend for execution.

    Construct its adapter, materialize the document, build the RunContext, and credential-check
    it. This helper is shared by
    `run_request`'s named-backend branch (below) and the server's `submit_job` handler so the two
    call sites can't drift out of parity by hand-copying again (BL-91) — every caller gets the
    identical `signup_url`-bearing credentials message for the identical failure.

    `deadline_ms` (BL-153): forwarded to `build_run_context` so a caller holding its own real,
    already-resolved time budget can have it reach `ctx.deadline_ms` here too — the field an
    adapter's own code actually reads (e.g. TesseractAdapter.submit()'s subprocess timeout) — the
    same way BL-146 already wired `execute_plan` and the strategy engine's leaf dispatch. Defaults
    to None (build_run_context's own DEFAULT_DEADLINE_MS fallback applies, still 2 minutes — too
    short for some hosted async backends' ordinary workload, e.g. Textract's async flow). BL-169
    wires a real caller for the `run_request` named-backend path specifically: `run()`'s own
    `deadline_ms` parameter and `parse`'s `--deadline` CLI flag now originate one. `submit_job`
    (the server's own caller of this function) still doesn't — a caller that omits `deadline_ms`
    still resolves to the same default as before, unchanged.

    Raises KeyError (unknown backend), ScopeRefused, or MissingCredentialsError — callers
    map each the same way they map any other adapter-invocation error. On success, returns
    (adapter, req, ctx) ready for `adapter.submit(req, ctx)`."""
    broker = broker or EnvCredentialBroker()
    config = config or RouterConfig()
    adapter = make_adapter(backend)  # KeyError → caller maps to 404
    req = materialize_document(req, adapter.descriptor, transport=transport)
    ctx = build_run_context(req, adapter.descriptor, broker=broker, deadline_ms=deadline_ms)
    missing = missing_required(adapter.descriptor, ctx)
    if missing:
        signup = (
            f" Sign up / configure: {adapter.descriptor.signup_url}"
            if adapter.descriptor.signup_url
            else ""
        )
        raise MissingCredentialsError(
            f"missing required credentials/config: {', '.join(missing)}.{signup}", missing=missing
        )
    return adapter, req, ctx


def run_request(
    req: OpenReadingRequest,
    *,
    broker: EnvCredentialBroker | None = None,
    config: RouterConfig | None = None,
    transport=None,
    strategy_config=None,
    keep_candidates: bool = False,
    plain_info=None,
    cache: BoundedResultCache | None = None,
    deadline_ms: int | None = None,
    on_run_armed: Callable[[str], None] | None = None,
    backend_allowlist: frozenset[str] | None = None,
) -> dict[str, Any]:
    """Execute a request whose backend id is a slug, `None`, or `strategy:<name>`.
    The server calls this with the RouterConfig + strategy config from its env; `run()` calls it
    with the config from a policy + a discovered openreading.yaml. `plain_info` (from the loader)
    carries Plain-dialect gate provenance for `explain`; absent, gates render flat. `cache` is
    the caller's idempotency cache for the resolved chain; the server owns one per app, the CLI and
    `run()` pass none so a library call always does the work (D-v3-3). `deadline_ms` (BL-153,
    wired to a real caller in BL-169) is forwarded to `prepare_named_backend` for the named-backend
    branch only — `run()`'s own `deadline_ms` parameter and the CLI's `--deadline` flag on `parse`
    now originate a real one. Strategy dispatch manages its own per-node
    time budget instead.

    `backend_allowlist` is the CALLER's ceiling on which backends this request may reach (the
    server's per-token `OPENREADING_API_KEY_SCOPES` entry); None means unscoped. Every arm that
    picks its own backends reads it, which is all of them but the directly-named one:

    - Every strategy arm prunes its compiled choices before execution.
    - The resolved-chain arm prunes the router's full chain before execution.
      A caller gating this one at the door can only ever check the router's first pick; the plan
      is chosen plus every fallback, and `execute_plan` walks all of it, so the backends behind
      the first pick were reachable by a request that named any of them and got 403.

    Only the directly-named arm needs nothing here, because there the id IS the request and the
    caller can gate it before the call.

    Raises KeyError (unknown backend), UnknownStrategyError, PlanExhaustedError, ScopeRefused,
    ScopeRefused (the caller's allow-list leaves the walk or resolved chain nothing to
    run), TerminalError, or RetryableError (a directly-named backend's rate-limit exhaustion, or
    router.driver's poll loop past its deadline/MAX_CONSECUTIVE_FAULTS. The chain path folds this
    into PlanExhaustedError via execute_plan/D-v2-7.2 instead, since it can fall back to the next
    backend; a named backend has no next rung, so it surfaces here under its own type)."""
    broker = broker or EnvCredentialBroker()
    config = config or RouterConfig()
    backend = req.backend.id

    # `strategy:<name>` runs that strategy. `strategy:none` ignores any configured default.
    strat = (
        backend[len(_STRATEGY_PREFIX) :]
        if backend and backend.startswith(_STRATEGY_PREFIX)
        else None
    )
    if strat is not None and strat != "none":
        return _run_strategy_request(
            req,
            strat,
            strategy_config,
            broker=broker,
            config=config,
            transport=transport,
            keep_candidates=keep_candidates,
            plain_info=plain_info,
            on_run_armed=on_run_armed,
            backend_allowlist=backend_allowlist,
        )
    if strat == "none":
        backend = None  # escape hatch: the plain chain, no defaults.strategy
    elif (
        backend is None
        and strategy_config
        and strategy_config.defaults
        and strategy_config.defaults.strategy
    ):
        return _run_strategy_request(
            req,
            strategy_config.defaults.strategy,
            strategy_config,
            broker=broker,
            config=config,
            transport=transport,
            keep_candidates=keep_candidates,
            plain_info=plain_info,
            on_run_armed=on_run_armed,
            backend_allowlist=backend_allowlist,
        )

    if backend is None:
        plan = Router(build_registry(), config, broker=broker).route(req)
        if plan.chosen is None:
            # An empty chain means the declared allow-list permitted nothing, or named only
            # backends this build does not carry. It is a refusal rather than a runtime failure:
            # PlanExhaustedError is for a non-empty chain whose backends all failed.
            raise ScopeRefused(
                "no backend left to run: the declared allow-list permits none of the registered "
                "backends",
                constraint=plan.terminal_reason or "no_backend_in_scope",
            )
        if backend_allowlist is not None:
            # The caller's ceiling, applied to the whole CHAIN — chosen plus every fallback — and
            # applied HERE, between routing and execution, because this is the last moment the set
            # of backends this request can reach is known and the first adapter has yet to be
            # built. A null id names no backend, so a check at the door can only speak for the
            # router's first pick; the twelve behind it were reachable, and a document that pymupdf
            # fails on walked straight into them.
            denied = sorted(i for i in plan.eligible_ids if i not in backend_allowlist)
            first_pick = plan.chosen.descriptor.id
            plan = plan.restrict_to(backend_allowlist)
            if plan.chosen is None:
                # Fail closed. Nothing this caller may reach survived, so this is scope's refusal
                # to make. The fix is the token's allow-list. Never return 502 because no backend
                # was allowed to try, so nothing failed.
                raise ScopeRefused(
                    "this API key is not scoped to reach any backend eligible for this request "
                    f"(denied: {', '.join(denied)})",
                    backend_code=first_pick,
                )
        if any(not a.descriptor.accepts_url for a in plan.chain):
            req = materialize_document(req, transport=transport)
        return execute_plan(plan, req, broker=broker, cache=cache).to_schema_dict()

    adapter, req, ctx = prepare_named_backend(
        req, backend, broker=broker, config=config, transport=transport, deadline_ms=deadline_ms
    )
    # build_run_context (inside prepare_named_backend) always resolves deadline_ms to a concrete
    # int (`deadline_ms if deadline_ms is not None else DEFAULT_DEADLINE_MS`) — the field itself
    # stays `int | None` in RunContext's type only because build_run_context is the sole caller
    # required to honor that contract. Asserted, not just typed, so a future caller of
    # run_to_completion below can't silently reintroduce the `or`-truthiness bug this item fixes.
    assert ctx.deadline_ms is not None
    clock = RealClock()
    try:
        with auth_hinted(adapter.descriptor, ctx.credentials):
            job = adapter.submit(req, ctx)
            job = run_to_completion(
                adapter,
                job,
                ctx=ctx,
                # BL-138: mirrors _run_native's fix below — `ctx.deadline_ms` is already fully
                # resolved by build_run_context (`deadline_ms if deadline_ms is not None else
                # DEFAULT_DEADLINE_MS`), so re-deriving it with `or` here is both redundant and
                # would silently discard an explicit `deadline_ms=0` via truthiness the day a
                # caller can reach this branch with one (no such caller exists yet — every
                # build_run_context call site on this path passes deadline_ms=None today — but
                # the identical bug on the native-batch path shows the `or` form isn't safe to
                # leave in place preemptively).
                deadline_ms=clock.now_ms() + ctx.deadline_ms,
                clock=clock,
            )
            slim_req = slim_request(req)
            resp = apply_cost_report(
                adapter, job, adapter.normalize(job, ctx, slim_req), ctx.credentials
            )
            return resp.to_schema_dict()
    except AdapterError:
        raise  # the five _ADAPTER_ERRORS taxonomy types keep their own specific handling downstream
    except Exception as e:
        # BL-99: adapter.normalize() is ordinary adapter code, not one of the five taxonomy types —
        # a plain KeyError/IndexError/ValueError/AttributeError out of it (or submit()/poll()) used
        # to propagate straight out of run_request, past every caller's typed except clauses
        # (server's _ADAPTER_ERRORS catch, the CLI's own (TerminalError, ScopeRefused) catch),
        # to a bare, undocumented crash. auth_hinted (widened above) has already redacted e's
        # message by the time it reaches here; converting it into a TerminalError — already one of
        # run_request's documented raises — gives it the identical structured, non-500 handling
        # BL-85 already gives the three async-job sinks, with no caller-side change required.
        raise TerminalError(str(e)) from e


def run(
    source: str | bytes,
    backend: str | None = None,
    *,
    strategy: str | None = None,
    config: str | os.PathLike[str] | dict | LoadedFile | None = None,
    operation: str | None = None,
    env_file: str | None = None,
    mime_type: str | None = None,
    broker: EnvCredentialBroker | None = None,
    transport=None,
    keep_candidates: bool = False,
    deadline_ms: int | None = None,
    on_run_armed: Callable[[str], None] | None = None,
    **request_overrides: Any,
) -> dict[str, Any]:
    """Run one document through a named backend, the resolved chain (`backend=None`), or a
    strategy, and return a `response.v0.3` envelope. `strategy="<name>"` is sugar for
    `backend="strategy:<name>"`. `source` is a path, an http(s) URL, or raw bytes. `config` points
    at an openreading.yaml, and without it `./openreading.yaml` is discovered. It also accepts a
    `LoadedFile` already read by `openreading.config.load`, which is how `run_batch` gives every
    item of one batch the same snapshot.

    This is a thin wrapper over `run_request` (via `build_request`) and propagates whatever that
    raises, `RetryableError` included. Every exception type this call can raise, and what each one
    means, is in the "Exceptions" section of this module's own docstring.

    `deadline_ms` (BL-169) is the caller's absolute time budget for a DIRECTLY-NAMED backend only,
    forwarded to `run_request`'s named-backend branch (`prepare_named_backend`, BL-153's own
    plumbing). Omitted (the default), a named backend resolves to `credentials.DEFAULT_DEADLINE_MS`
    (2 minutes), which is too short for some hosted async backends' ordinary workload. The CLI
    spelling of the same knob is `parse --deadline`, documented in `openreading.cli`. It has no
    effect on resolved-chain or strategy dispatch, which manage their own node budgets.

    `on_run_armed` is invoked once with the run id immediately after the ledger arms.
    Only a strategy-dispatch path arms one, so a named-backend or resolved-chain run
    never fires it. It mirrors the optional-hook shape of `run_batch`'s own `on_progress` and
    `on_preflight`.
    """
    _refuse_removed_kwargs(request_overrides)
    if env_file:
        load_dotenv(env_file)
    if strategy is not None:
        backend = f"strategy:{strategy}"
    # The file is read on every path so a null backend can resolve its configured chain. The
    # strategy half is built only when a strategy could engage through null or `strategy:`.
    loaded = load_config_file(config)  # CLI/Python discover cwd; None uses built-in defaults.
    strategy_file = None
    if backend is None or backend.startswith(_STRATEGY_PREFIX):
        from openreading.strategies.loader import build_config

        strategy_file = build_config(loaded)
    req = build_request(
        source,
        backend,
        operation=operation,
        mime_type=mime_type,
        **request_overrides,
    )
    req, cfg = apply_config(req, loaded.policy if loaded else None, RouterConfig())
    return run_request(
        req,
        broker=broker,
        config=cfg,
        transport=transport,
        strategy_config=strategy_file.config if strategy_file else None,
        plain_info=strategy_file.plain_info if strategy_file else None,
        keep_candidates=keep_candidates,
        deadline_ms=deadline_ms,
        on_run_armed=on_run_armed,
    )


def _request_from_header(header: RunHeader, blobs: LocalFsBlobStore) -> OpenReadingRequest:
    """Reconstructs the `OpenReadingRequest` a resumed strategy walk needs from the header's own
    `slim_request` + `document` (see `ledger/header.py`'s module docstring for what's deliberately
    NOT recoverable this way — `document.password`/`async.webhook_url`, never persisted).

    `header.document` holds a `bytes_base64` document's bytes. A URL is never persisted, because
    the plaintext blob store cannot safely retain a bearer token embedded in one, and a presigned
    URL routinely is one. Such a header carries `document_is_url=True` with no `document`, and
    resume reports its input as unavailable rather than replaying against something it does not
    have. Materialize the document before arming the ledger if a URL-sourced run must be
    resumable.

    The `document_is_url` branch below reads a URL back out of the blob store, and no header this
    version writes can reach it: the refusal above catches every one. It stays for a header
    written by a build after encryption was removed and before the URL stopped being stored, whose
    blob is a readable URL on disk."""
    body: dict[str, Any] = dict(header.slim_request)
    doc = dict(body.get("document") or {})
    if header.document is None and header.document_is_url:
        raise TerminalError(
            "recorded input payload is unavailable: source URL was not retained",
            backend_code="payload_missing",
        )
    if header.document is not None:
        try:
            raw = blobs.get(header.document)
        except OSError as exc:
            raise TerminalError(
                f"recorded input payload is unavailable: {exc}",
                backend_code="payload_missing",
            ) from exc
        if header.document_is_url:
            doc["url"] = raw.decode("utf-8")
        else:
            doc["bytes_base64"] = base64.b64encode(raw).decode()
    body["document"] = doc
    return OpenReadingRequest.model_validate(body)


def resume_run(run_id: str) -> dict[str, Any]:
    """`openreading resume <RUN_ID>` (internal/design/ledger.md §10, plan §4.4): re-derive the run's
    identity from the LIVE openreading.yaml + registry, compare it against the header written at
    the run's first arm, and — on a match — re-drive the SAME compiled strategy against the
    EXISTING journal: every step already terminal there replays byte-identical (§4.3, zero network,
    AC-3); anything genuinely unreached executes for real. No other input is taken — "every option
    comes from the ledger" (§10) — the original request is reconstructed from the header's own
    `document`/`slim_request` fields via `_request_from_header`.

    A run the SERVER armed for a scoped caller resumes correctly without the caller's allow-list,
    which is worth stating because the `compile_strategy` call below deliberately passes none. A
    resume is a CLI/library action with no token concept, so the scope cannot come from the caller;
    it comes from the ledger, like every other option:

    - A scope that pruned a named rung changed the compiled tree, so `plan_hash` no longer matches
      and the resume hard-refuses (`plan_hash` is one of the three identity fields, `_HARD_FIELDS`).
    - A scope that pruned nothing leaves `plan_hash` matching, and correctly so. The header's
      `pinned_eligible` still carries the set the original run could dispatch, and
      `_arm_ledger(resume=True)` arms the resumed executor's per-step gate from THIS header rather
      than a freshly recomputed one, so a policy edit between the two halves of a run cannot let
      the resume reach a backend the original could not.

    Raises `LookupError` when `OPENREADING_LEDGER` is unset or no header exists for `run_id`, or
    `ledger.header.HeaderMismatch` when the live config/plan/journal-version identity no longer
    matches the run's original header (AC-4) — the CLI maps each to its own printed refusal."""
    root = os.environ.get("OPENREADING_LEDGER")
    if not root:
        raise LookupError("OPENREADING_LEDGER is not set, so there is no run to resume from")
    ledger_root = Path(root)
    header = read_header(ledger_root, run_id)
    if header is None:
        raise LookupError(f"no recorded run {run_id!r} under {ledger_root}")

    from openreading.strategies import compile_strategy, run_strategy
    from openreading.strategies.loader import build_config
    from openreading.strategies.model import StrategyConfig

    blobs = LocalFsBlobStore(ledger_root / "blobs")
    req = _request_from_header(header, blobs)

    loaded = load_config_file(None)
    strategy_file = build_config(loaded)
    strategy_config = strategy_file.config if strategy_file else StrategyConfig(version=1)
    registry = build_registry()
    broker = EnvCredentialBroker()
    # §10: "no other flags" — a resume takes every option from the ledger and the live file.
    req, config = apply_config(req, loaded.policy if loaded else None, RouterConfig())
    compiled = compile_strategy(req, header.strategy_name, strategy_config, registry, config)
    # The resumed walk may dispatch only what the ORIGINAL run could. `pinned_eligible` records
    # that set, and re-imposing it as the allow-list is what stops a policy edit between the two
    # halves of a run from widening it. `compiled.eligible` is left alone: it is the chain an
    # unnamed request would walk today, reported for the operator, and no node resolves against it.
    original_dispatchable = frozenset(header.pinned_eligible)
    compiled.backend_allowlist = original_dispatchable
    clock = RealClock()
    executor = _arm_ledger(
        run_id,
        req,
        registry,
        broker,
        clock,
        compiled.dispatchable,
        config_hash=compiled.config_hash,
        plan_tree=compiled.root,
        strategy_name=header.strategy_name,
        resume=True,
    )
    assert executor is not None  # OPENREADING_LEDGER was already confirmed set above
    result = run_strategy(
        compiled,
        req,
        registry=registry,
        broker=broker,
        clock=clock,
        run_id=run_id,
        executor=executor,
    )
    result.response.orchestration = result.orchestration
    return result.response.to_schema_dict()


def run_batch(
    sources: list[str],
    backend: str | None = None,
    *,
    strategy: str | None = None,
    config: str | os.PathLike[str] | dict | LoadedFile | None = None,
    jobs: int = 1,
    max_jobs: int = MAX_BATCH_JOBS,
    max_items: int = DEFAULT_MAX_ITEMS,
    deadline_ms: int | None = None,
    env_file: str | None = None,
    broker: EnvCredentialBroker | None = None,
    transport=None,
    idempotency_key: str | None = None,
    keep_candidates: bool = False,
    on_progress=None,
    on_preflight=None,
    **request_overrides: Any,
) -> dict[str, Any]:
    """Run many documents (a mix of files / dirs / globs / http(s) URLs) as ONE batch, returning a
    batch-result envelope (internal/design/batch-intake.md §6). Composes the single-document `run()`
    per item via the platform runner — the single-document contract is untouched. `on_progress`
    (done, total, item) and `on_preflight` (resolved, backend) are optional CLI hooks.
    `keep_candidates` is a named parameter, not a request override: it is a per-run execution
    choice `run()` consumes, and the native path would otherwise hand it to `build_request`.

    `jobs` is bounds-checked by the shared `batch.runner.bound_jobs` helper (BL-84) BEFORE intake
    is even resolved: `jobs<=0` clamps to 1 (echoed as the corrected value, never the raw input);
    `jobs` over `max_jobs` raises `batch.runner.JobsLimitError`, a catchable exception, rather than
    silently returning a schema-invalid envelope or reaching a real, unbounded
    `ThreadPoolExecutor`. `max_jobs` (default `MAX_BATCH_JOBS`) is this surface's own escape hatch
    past the default ceiling — the same "sane default + override" shape `max_items` already has.

    `deadline_ms` (BL-135) is the one override for native-batch dispatch's own time budget
    (`_run_native`'s `build_run_context` call — §7): None (the default) lets `_run_native` fall
    back to `DEFAULT_NATIVE_BATCH_DEADLINE_MS`, already a longer, native-batch-appropriate budget
    reflecting the one real adapter's own documented "most <1h" timing rather than the generic
    two-minute `DEFAULT_DEADLINE_MS` every synchronous-feeling path uses; pass an explicit value
    (larger for a bigger batch, smaller to fail fast) to override that default too. Has no effect
    when `backend` resolves to the platform fan-out path instead of native dispatch — the platform
    path composes `run()` per item exactly as before, each on its own unrelated default budget.

    A per-item failure on the platform fan-out path is isolated into that item's `BatchItem.error`
    and never raised (M6) — but when `backend` resolves to ONE native-batch-capable adapter (§7),
    a batch-level failure out of that adapter's `submit_many`/`run_to_completion`/`normalize_many`
    propagates out of this call like any single `run()` error: TerminalError, ScopeRefused, or
    RetryableError (a directly-named backend has no next rung to fall back to, exactly like
    `run_request`'s own named-backend branch — see its docstring). `_run_native`'s own docstring
    already makes this promise; it is repeated here because this is the function most callers
    actually read (BL-128)."""
    from openreading.batch import runner as _batch_runner
    from openreading.batch.sources import resolve_intake
    from openreading.types.batch import BatchRequestEcho

    _refuse_removed_kwargs(request_overrides)
    jobs = _batch_runner.bound_jobs(jobs, max_jobs=max_jobs)
    # Like `jobs`, the file is an input to the WHOLE batch, so it is read and its `policy:` block
    # checked before intake rather than per item. On the platform path a per-item failure is
    # isolated into that item's `error` and never raised (M6) — correct for a document that could
    # not be read, wrong for a policy the operator mistyped, which would otherwise come back as N
    # identical item errors and a zero exit instead of one refusal.
    loaded = load_config_file(config)

    if env_file:
        load_dotenv(env_file)
    if strategy is not None:
        backend = f"{_STRATEGY_PREFIX}{strategy}"
    broker = broker or EnvCredentialBroker()

    # No `supported_formats`: intake dispatches every source the caller named, and a backend that
    # cannot read one refuses first-hand. Computing the set meant sweeping readiness for every
    # registered backend on the way into a run that then ignored the answer.
    resolved = resolve_intake(list(sources), max_items=max_items)
    if on_preflight is not None:
        on_preflight(resolved, backend)

    # §6: a named backend may cap platform concurrency (e.g. CPU-bound tesseract) via
    # descriptor.batch.max_concurrency — the runner takes min(requested, cap).
    if backend is not None and not backend.startswith(_STRATEGY_PREFIX):
        try:
            bi = make_adapter(backend).descriptor.batch
        except KeyError:
            bi = None
        if bi and bi.max_concurrency:
            jobs = min(jobs, bi.max_concurrency)

    echo = BatchRequestEcho(
        backend=backend, strategy=strategy, jobs=jobs, source_args=list(sources)
    )

    # §7 dispatch: native path iff the whole batch resolved to one named backend whose descriptor
    # declares batch.native and which implements the protocol; else platform fan-out (M10: the
    # envelope is observationally equivalent either way).
    native = _native_adapter(backend, resolved, broker)
    if native is not None:
        return _run_native(
            native,
            resolved,
            backend,
            broker=broker,
            transport=transport,
            idempotency_key=idempotency_key,
            request_echo=echo,
            on_progress=on_progress,
            config_file=loaded,
            deadline_ms=deadline_ms,
            **request_overrides,
        )

    def run_one(src, idem):
        source = src.ref.path or src.ref.url
        return run(
            source,
            # The snapshot, not the path: `run` hands a LoadedFile straight back out of
            # `config.load`, so no item re-reads or re-validates the file (law PF4).
            backend=backend,
            config=loaded,
            broker=broker,
            transport=transport,
            idempotency_key=idem,
            keep_candidates=keep_candidates,
            **request_overrides,
        )

    result = _batch_runner.run_batch(
        resolved,
        run_one=run_one,
        jobs=jobs,
        idempotency_key=idempotency_key,
        request_echo=echo,
        on_progress=on_progress,
    )
    return result.to_schema_dict()


def _native_adapter(backend: str | None, resolved: list, broker: EnvCredentialBroker):
    """§7 dispatch rule → the adapter to use for a native batch, or None for platform fan-out.
    Native iff: a directly named backend (not a strategy, and not absent), its descriptor declares
    `batch.native`
    truthy, it implements the NativeBatchAdapter protocol, there is at least one item, and the
    count is within `batch.max_items`."""
    from openreading.adapters.base import NativeBatchAdapter

    if backend is None or backend.startswith(_STRATEGY_PREFIX):
        return None
    try:
        adapter = make_adapter(backend)
    except KeyError:
        return None
    bi = adapter.descriptor.batch
    if not bi or not bi.native or not isinstance(adapter, NativeBatchAdapter):
        return None
    live = list(resolved)
    if not live:
        return None
    if bi.max_items is not None and len(live) > bi.max_items:
        return None  # too many for one native batch → fall back to platform
    return adapter


def _run_native(
    adapter,
    resolved: list,
    backend: str | None,
    *,
    broker: EnvCredentialBroker,
    transport,
    idempotency_key: str | None,
    request_echo,
    on_progress,
    config_file=None,
    deadline_ms: int | None = None,
    **request_overrides: Any,
) -> dict[str, Any]:
    """Execute a whole batch through an adapter's native submit and normalize methods.

    Per-item results map to succeeded or failed items with `transport="native"`. A batch-level
    failure propagates like any `run()` error. Every item shares the same policy and overrides.

    `deadline_ms` (BL-135): `run_batch`'s own override, forwarded here. None (the default) means
    `build_run_context` falls back to `DEFAULT_NATIVE_BATCH_DEADLINE_MS` rather than the generic,
    two-minute `DEFAULT_DEADLINE_MS` every other synchronous path uses. This is the one
    dispatch shape in the whole codebase where a caller submits one job and then polls a
    vendor-side batch that the adapter's own descriptor documents as routinely taking up to an
    hour, so it gets its own, larger, still-overridable default rather than inheriting the generic
    constant every other path also reuses."""
    import time

    from openreading.batch import runner as _batch_runner
    from openreading.batch.runner import item_idempotency_key
    from openreading.types.batch import BatchItem, BatchItemError

    started = time.perf_counter()
    live = list(resolved)
    file_block = config_file.policy if config_file is not None else None
    cfg = RouterConfig()
    reqs = []
    for src in live:
        source = src.ref.path or src.ref.url
        idem = item_idempotency_key(idempotency_key, src.ref.sha256)
        overrides = {k: v for k, v in request_overrides.items() if v is not None}
        req = build_request(source, backend, idempotency_key=idem, **overrides)
        req, cfg = apply_config(req, file_block, cfg)
        req = materialize_document(req, adapter.descriptor, transport=transport)
        reqs.append(req)

    ctx = build_run_context(
        reqs[0],
        adapter.descriptor,
        broker=broker,
        deadline_ms=deadline_ms if deadline_ms is not None else DEFAULT_NATIVE_BATCH_DEADLINE_MS,
    )
    # build_run_context always resolves deadline_ms to a concrete int (its own `is not None`
    # check above, or the explicit fallback passed in here) — the field itself stays `int | None`
    # in RunContext's type only because build_run_context is the sole caller required to honor
    # that contract. Asserted, not just typed, so a future edit here can't silently reintroduce
    # the `or`-truthiness bug this item fixes (BL-138).
    assert ctx.deadline_ms is not None
    clock = RealClock()
    try:
        with auth_hinted(adapter.descriptor, ctx.credentials):
            job = adapter.submit_many(reqs, ctx)
            job = run_to_completion(
                adapter,
                job,
                ctx=ctx,
                # BL-138: `ctx.deadline_ms` is already fully resolved by build_run_context (above)
                # — `deadline_ms if deadline_ms is not None else DEFAULT_NATIVE_BATCH_DEADLINE_MS`.
                # Re-deriving it here with `ctx.deadline_ms or DEFAULT_NATIVE_BATCH_DEADLINE_MS`
                # silently discarded an explicit `deadline_ms=0` ("fail fast") back to the 1h
                # default via Python truthiness (0 is falsy). Use the resolved value directly.
                deadline_ms=clock.now_ms() + ctx.deadline_ms,
                clock=clock,
            )
            # BL-108: forwarded so a report_cost() failure inside normalize_many's own
            # apply_cost_report call gets the same redaction every other call site's warning does.
            results = adapter.normalize_many(job, reqs, credentials=ctx.credentials)
    except AdapterError:
        raise  # the five _ADAPTER_ERRORS taxonomy types keep their own specific handling downstream
    except Exception as e:
        # BL-106: the sixth BL-99-class call site — this block had no try/except of any kind, so a
        # plain KeyError/IndexError/ValueError/AttributeError out of normalize_many (the concrete
        # trigger: AnthropicClaudeAdapter.normalize_many's per-item self.normalize(synth, req) call)
        # or out of submit_many/run_to_completion propagated straight out of run_batch(), discarding
        # the ENTIRE native batch's results — not just the offending item's — contradicting
        # internal/design/batch-intake.md's M6 per-item-isolation invariant. auth_hinted (already
        # passed ctx.credentials above) has already redacted e's message by the time it reaches
        # here; converting it into a TerminalError gives it the identical structured handling
        # run_request's own BL-99 fix gives the single-document path, with no caller-side change
        # required.
        raise TerminalError(str(e)) from e

    items: list[BatchItem] = []
    total = len(resolved)
    for li, src in enumerate(resolved):
        res = results[li] if li < len(results) else BatchItemError(code="missing_result")
        if isinstance(res, BatchItemError):
            items.append(BatchItem(source=src.ref, state="failed", error=res, transport="native"))
        else:
            items.append(
                BatchItem(
                    source=src.ref,
                    state="succeeded",
                    response=res.to_schema_dict(),
                    transport="native",
                )
            )
        if on_progress is not None:
            on_progress(len(items), total, items[-1])

    duration_ms = int((time.perf_counter() - started) * 1000)
    return _batch_runner.assemble_result(
        items, request_echo=request_echo, duration_ms=duration_ms
    ).to_schema_dict()
