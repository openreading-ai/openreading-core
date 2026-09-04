"""Public one-call API — the Python surface contract. The submit→drive→normalize pipeline is
exposed as `openreading.run()` / `run_batch()` / `route()` / `resume()` and is the single path
the CLI (`cmd_parse`, `cmd_resume`) and the server (`/v1/parse`, `/v1/batch`, `/v1/jobs`)
collapse onto, so the three surfaces cannot drift.

    import openreading
    doc = openreading.run("loan.pdf", backend="reducto")          # named backend
    doc = openreading.run("loan.pdf", backend="auto", policy=p)   # route + execute the chain
    doc = openreading.run("loan.pdf", strategy="main")            # == backend="strategy:main"
    plan = openreading.route("loan.pdf", policy=p)                 # plan only, no execution
    env = openreading.run_batch(["invoices/"], backend="pymupdf", jobs=4)
    doc = openreading.resume("7dbf6b71-adb5-4e90-9188-a184fdba9d05")   # a run id is a UUIDv4

A bare `spec §N`, `plan §N`, `Open Questions §N` or `§N` below names a section of a design record
in the private company repository, under `internal/design/`. See AGENTS.md, "The company repo".
The shipped behaviour is what this module states, and the citation is maintainer provenance only.

Exports and return shapes
-------------------------
An envelope is the one JSON object a call returns. It carries the parsed `document` alongside the
run's own record of it: `status`, `backend`, `usage`, `warnings`, and the `schema_version` that
names the schema it validates against.

- `run(source, backend="auto", *, strategy, config, operation, policy, env_file, mime_type,
  broker, transport, keep_candidates, deadline_ms, on_run_armed, **request_overrides) -> dict`
  — a `response.v0.3` envelope. `**request_overrides` are top-level request fields
  (`outputs`, `extraction_schema`, `features`, `pages`, `idempotency_key`, ...); `document`
  and `backend` are refused there (`ValueError`, BL-105) because a stray forwarded kwarg once
  silently ran a request against the wrong document.
- `run_batch(sources, backend="auto", *, strategy, config, jobs=1, max_jobs=32, max_items=200,
  deadline_ms, env_file, policy, broker, transport, idempotency_key, keep_candidates,
  on_progress, on_preflight, **request_overrides) -> dict` — a `batch-result.v0.1` envelope.
- `route(source, *, policy, operation, mime_type) -> RoutePlan` — `chosen`, `fallbacks`,
  `dropped` ({backend_id: DropReason}), `terminal_reason`, `chain`, `eligible_ids`.
- `resume_run(run_id) -> dict` (exported as `openreading.resume`).
- Lower seams shared with the CLI/server, public by name: `build_request`, `run_request`,
  `prepare_named_backend`, `materialize_document`, `router_config`, `validate_policy`
  (+ `PolicyError`, `POLICY_KEYS`).

`source` is a path, an http(s):// URL, or raw bytes — never a request dict. A path's MIME type is
inferred from its extension (pdf/png/jpg/jpeg/tif/tiff/docx/xlsx/pptx), default
`application/pdf`; bytes default to PDF unless `mime_type=` says otherwise (D-v2-9). A missing
path raises `SourceNotFoundError` (an `OSError` with errno ENOENT, so callers format it like a
real `FileNotFoundError`). URL inputs pass through untouched to backends that ingest URLs
natively (`descriptor.accepts_url`) and are downloaded to bytes for the rest
(`materialize_document`, D-v2-13: httpx, 60 s timeout, 100 MB cap, HTTP errors through the
shared status mapping); offline tests inject an httpx `transport`, and no default path performs
network I/O.

Backend resolution
------------------
`backend` is a registry slug, `"auto"`, or `"strategy:<name>"` — a reserved prefix on the free-
string `backend.id`, not a wire-schema change (D-v3-2); `strategy=` is sugar for it and
`"strategy:none"` is the escape hatch to the plain router. `"auto"` runs the compliance-first
router and executes the resulting chain (`router.executor.execute_plan`); an empty plan raises
`ComplianceRefused` (the router eliminated every backend — a refusal, not a runtime failure;
`PlanExhaustedError` is reserved for a non-empty plan whose backends all failed). An
`openreading.yaml` is discovered ONLY for `auto` / `strategy:` requests (explicit `config=` ->
`OPENREADING_CONFIG` -> `./openreading.yaml`, first hit wins); `auto` + `defaults.strategy` in
that file engages the strategy. A plain named-backend run never imports the strategy package at
all (guardrail T10), so no config file and no strategy means byte-identical legacy behavior. The
four presets (`cost_saver`, `max_accuracy`, `fast`, `offline_first`) work with no file.

A directly-named backend is still compliance-gated against the request (`Router.check_eligible`
-> `ComplianceRefused`), then credential-gated (`MissingCredentialsError` naming the exact vars
plus the descriptor's signup URL) — naming a backend never bypasses the request's own
constraints, on the single, native-batch, or server path alike.

`policy` dict: compliance keys `require_baa`, `no_train_on_data`, `data_region`,
`require_local`, `max_retention` become `request.compliance`; `optimize_for`, `doc_type_hint`
become `request.routing`; `allow_unverified_compliance` (default False = fail closed),
`train_optout_confirmed`, `baa_tier_confirmed` (lists of backend ids) become the deployment-level
`RouterConfig` (D7/D7a: the request schema is `extra="forbid"` and these assert an account-level
fact — an opt-out applied, a tier-gated BAA signed — not a property of one document; a
confirmation that carried eligibility is echoed as a `baa_tier_confirmed` warning). The
server-only `OPENREADING_ALLOW_UNVERIFIED_COMPLIANCE` / `OPENREADING_TRAIN_OPTOUT_CONFIRMED` /
`OPENREADING_BAA_TIER_CONFIRMED` env vars are NOT consulted here; `policy=` is the Python API's
only spelling of them.

Those ten names are the WHOLE policy grammar (`POLICY_KEYS`), and `validate_policy` refuses
anything else before a single key is read: a policy must be an object, every key must be one of
the ten, and every value must type-check against the model that key feeds — `PolicyError`
(a `ValueError`) otherwise, from `route`/`run`/`run_batch`/`build_request`/`router_config` alike,
and exit 3 with a `[<command>] invalid policy <path>: …` line from every CLI `--policy` flag.
An HTTP caller spells the same constraints as `request.compliance` / `request.routing`, which the
request schema and `Compliance`/`Routing` (`extra="forbid"`) already refuse identically; the
policy path is checked in the same strict, non-coercing way so the surfaces cannot disagree.
This is validation of SHAPE only — no key means anything new, and no policy that was enforced
before is enforced differently now. It exists because the alternative is silent: the split into
`compliance` / `routing` / `RouterConfig` used to happen before anything validated the dict, so
an unrecognised key (`require_baaa`, `hipaa`, `gdpr`) was dropped without a word and the
constraint the operator wrote did not exist — every backend eligible, `dropped` empty,
exit 0. A compliance gate that can be turned off by a typo is not a gate.

Exceptions
----------
Every class named here is importable from the top level (`from openreading import
ComplianceRefused`), which is where a caller branching on the type will look for it. The homes are
unchanged: `openreading.types.errors` defines all of them except `PolicyError`, which is defined
here because policy parsing raises it before any backend is involved.

`KeyError` unknown backend slug · `ValueError` reserved override · `PolicyError` (a `ValueError`:
malformed `policy=`; CLI exit 3. Never reaches the server, which has no policy bag — an HTTP
caller's equivalent mistake is a 400 from the request schema) · `SourceNotFoundError` ·
`UnknownStrategyError` (server 400 / CLI exit 2) · `ComplianceRefused` (403 / exit 3) ·
`MissingCredentialsError` (424 / exit 3) · `PlanExhaustedError` (`auto` only: every rung failed;
carries the attempt trail) · `TerminalError` (any adapter failure, INCLUDING an unexpected
exception out of `submit()`/`poll()`/`normalize()`, wrapped after `auth_hinted` redaction so a
plain KeyError never escapes as an undocumented crash — BL-99/BL-106) · `RetryableError` (a
directly-named backend's rate-limit exhaustion or a poll loop past its deadline — it has no next
rung, so it surfaces under its own type; the `auto` path folds the same condition into
`PlanExhaustedError` via D-v2-7.2) · `batch.runner.JobsLimitError` (`jobs > max_jobs`) ·
`LookupError` / `ledger.header.HeaderMismatch` / `PayloadExpired` (resume, below).

Time budgets
------------
`deadline_ms` on `run()` applies to a DIRECTLY-NAMED backend only (default
`credentials.DEFAULT_DEADLINE_MS`, 2 minutes — too short for some hosted async flows such as
Textract's; the CLI's `parse --deadline` is the same knob). `auto` and strategy dispatch manage
their own per-node budgets. `run_batch(deadline_ms=)` overrides the native-batch budget only
(default `DEFAULT_NATIVE_BATCH_DEADLINE_MS`, 1 hour — the one dispatch shape that submits one job
and polls a vendor-side batch documented as "most <1h"). An explicit `0` is honoured as
"fail fast": resolved values are compared with `is not None`, never `or`, because truthiness once
silently turned `0` back into the default (BL-138).

No idempotency cache in library calls (D-v3-3): `run()` / the CLI pass `cache=None` so a library
call always does the work; the server owns the only `BoundedResultCache` and passes it to
`run_request` for the `auto` chain. Silent 15-minute memoization inside a library call is a
footgun, and a replayed response would carry the original run's `cost_usd` into a batch total
nobody was billed for.

Batch semantics (`run_batch`)
-----------------------------
Intake is by FORM: files, directories, globs and URLs are resolved (`batch.sources`) against the
effective format set — a named backend's own `input_formats`, or the union across every READY
backend for `auto` / `strategy:` — and an unsupported file becomes a `skipped` item with a
reason, never a crash. `jobs` is bounded BEFORE intake: `<= 0` clamps to 1 (echoed as the
corrected value), `> max_jobs` raises `JobsLimitError` rather than reaching an unbounded thread
pool; a named backend may cap it further via `descriptor.batch.max_concurrency` (CPU-bound
tesseract), and only the capped value reaches `request.jobs` — the CLI surfaces the reduction on
stderr, so a Python caller who needs the same signal compares its own `jobs` against that field.
`max_items` caps expansion. Dispatch is native iff the whole batch resolved to one
named backend whose descriptor declares `batch.native`, which implements `NativeBatchAdapter`,
with >= 1 live item and no more than `batch.max_items`; otherwise platform fan-out composes
`run()` per item. The envelope is observationally equivalent either way (M10), with
`items[].transport` saying which. On the platform path a per-item failure is isolated into that
item's `error` and never raised (M6); on the native path a batch-level failure out of
`submit_many` / `run_to_completion` / `normalize_many` propagates like a single `run()` error,
because a named backend has no next rung. `policy` is enforced on the native path too (BL-98):
every item shares identical compliance, so checking the first stands for the batch.

Environment variables read by this module
-----------------------------------------
- `OPENREADING_LEDGER` — a DIRECTORY path. Set: arms the Ledger execution plane
  (`_arm_ledger` builds an `InlineExecutor` over `<dir>/<run_id>.jsonl` + `blobs/` + `keys/`),
  which is what makes `resume` possible. Unset: `_arm_ledger` returns `None` and the run takes
  the zero-delta path (`NullJournal`, no blob store) — byte-identical stdout/stderr/exit code to
  a build from before the Ledger existed, asserted by test. Deliberately no CLI flag: arming is
  env and config only (`internal/design/ledger.md` §10). Coverage — `_arm_ledger` has exactly
  two call sites, `_run_strategy_request` and `resume_run`, so ONLY strategy dispatch journals:
  `--strategy X`, `auto` + `defaults.strategy`, `/v1/parse` resolving to a strategy, and a
  `--strategy` batch (one run per item via `run()`). A named backend, `auto` without
  `defaults.strategy`, `strategy:none`, the `/v1/jobs` store and a NATIVE batch journal nothing
  while appearing armed — know which row you are on before relying on a run being resumable.
  Back up `*.jsonl` and `blobs/`, never `keys/` alongside them: erasure works by destroying the
  per-run key, and holds only to the degree no other copy survives.
- `OPENREADING_LEDGER_RETENTION_HOURS` — float hours a run's payloads live before the reaper
  crypto-shreds the key. Default `ledger.retention.DEFAULT_RETENTION_HOURS` (24.0, a provisional
  default rather than a policy recommendation). Read once at arm time and stamped as an absolute
  epoch. A value `float()` cannot parse fails the run at arm time, at exit 1, with a `ValueError`
  that does not name this variable. It is only the STARTING ceiling — `InlineExecutor` tightens it
  per step from each DISPATCHED backend's own `max_retention_hours` and never widens it (a
  merely-eligible backend that never dispatches has zero effect; earlier it could collapse the
  whole run's ceiling to its own strict limit). A ZDR backend on the path suppresses blobs
  entirely. Raise it BEFORE the run: after the key is reaped, replay reports `payload_expired` and
  nothing brings the content back. A fresh run also sweeps the whole ledger root at arm time, so
  stale runs are collected by ordinary use.
- `OPENREADING_ALLOW_PRIVATE_URLS` — read by `_download` (`materialize_document`'s own URL
  fetch). Unset (default): before any request leaves this process, `_assert_public_http_url`
  refuses a non-http(s) scheme and a host that resolves to a loopback/private/link-local/reserved
  address (cloud metadata endpoints included) — `unsupported_input` / `url_not_public`
  respectively. Set to ANY non-empty value, `0` and `false` included, it skips that whole check,
  scheme included, for a deployment whose document store is deliberately intranet-only. Comment
  the variable out or unset it to keep the check. No value of the variable turns the skip off. The
  operator is trusted to have already constrained which URLs can reach `run()`/`route()` in that
  case. The connection is then PINNED to the address that check vetted (original host carried in
  `Host` and SNI), so a DNS answer that changes between the check and the connect — rebinding —
  cannot redirect it, and a redirect is refused rather than followed for the same reason.
- `env_file=` -> `credentials.load_dotenv`: loads `KEY=VALUE` lines WITHOUT overriding an
  already-set process variable (an exported shell var always beats the file); a missing file is
  a no-op. Loaded ONLY when the argument is given (`if env_file:` in `run()` / `run_batch()`):
  with `env_file=None` — the default — this module reads no file at all, and only the exported
  environment applies. The `./.env` default belongs to the CLI (`--env-file`), which calls
  `load_dotenv` itself. A library call never picks up a cwd file implicitly, so importing
  openreading from an unrelated project cannot silently adopt that project's keys. There is no key
  argument anywhere — credentials never travel on the command line or in a call.
- Read indirectly: backend credentials via `EnvCredentialBroker` (`OPENREADING_<SLUG>_<KEY>`
  beats the service-native name; `OPENREADING_CREDENTIALS_REF_ALIASES` allow-lists
  `credentials_ref: "env:<alias>"`, unset = no alias accepted — see `openreading.credentials`);
  `OPENREADING_CONFIG` via `strategies.loader`; `OPENREADING_LLM_DECIDER` via
  `strategies.decider` (the second key of the decider's two-key gate).

Resume contract (`resume_run`)
------------------------------
Takes ONLY the run id — every other option comes from the recorded run, never the call
(`internal/design/ledger.md` §10). Re-derives the run's identity from the LIVE
`openreading.yaml` + registry, compares it with the header written at first arm
(`ledger.header.compare_header`), and on any hard-field mismatch raises `HeaderMismatch` BEFORE
touching the journal — refusing by name rather than silently re-driving under a fresher config.
On a match, the same compiled strategy re-runs against the existing journal: every step already
terminal replays byte-identical with zero network; anything unreached executes for real. The
request is rebuilt from the header's encrypted `document` blob + plaintext `slim_request`
(`document.url` is treated as a secret — routinely a presigned URL — and lives in the blob store,
never the sidecar; `document.password` / `async.webhook_url` are never persisted). `LookupError`
when `OPENREADING_LEDGER` is unset or no header exists (CLI exit 3 for both); `PayloadExpired`
propagates once the run's key is shredded.

Decisions recorded for this module (durable, one line each)
------------------------------------------------------------
- D-v2-9: `run()` takes a path/bytes/URL and returns a schema dict; the CLI collapses onto it
  with zero flag changes — one execution path, so surfaces cannot disagree.
- D-v2-13: one shared `materialize_document` helper; URL passthrough only where the descriptor
  declares `accepts_url` — avoids every adapter re-implementing (and mis-implementing) download.
- D-v3-2: `strategy:` is a reserved `backend.id` prefix, recognized before any registry lookup —
  no request-schema bump, and `make_adapter("strategy:x")` can never be attempted.
- D-v3-3: no idempotency cache outside the server (footgun + phantom batch cost, above).
- D-v2-7.2: a `RetryableError` reaching the executor means "backend exhausted" -> next rung;
  retry policy lives in one place (`router.driver`), not duplicated per caller.
- D7 / D7a: `allow_unverified_compliance` / `train_optout_confirmed` / `baa_tier_confirmed` are
  router config, not request fields, and `tier_gated` BAAs fail closed until confirmed — so the
  compliance-eligible set only ever narrows.
- Guardrail T10: no strategy import on the named-backend path (verified in a subprocess test).
"""

from __future__ import annotations

import base64
import dataclasses
import difflib
import errno
import hashlib
import os
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from openreading.adapters._http import error_for_status
from openreading.adapters.registry import build_registry, make_adapter
from openreading.batch.runner import MAX_BATCH_JOBS
from openreading.batch.sources import DEFAULT_MAX_ITEMS
from openreading.credentials import (
    DEFAULT_NATIVE_BATCH_DEADLINE_MS,
    EnvCredentialBroker,
    build_run_context,
    load_dotenv,
    secret_values,
)
from openreading.ledger.header import (
    DOCUMENT_URL_MEDIA_TYPE,
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
from openreading.ledger.localfs import LocalFsBlobStore, LocalFsKeyStore
from openreading.ledger.ports import Executor, LedgerArmingError
from openreading.ledger.retention import (
    DEFAULT_RETENTION_HOURS,
    reap,
    stamp_run,
)
from openreading.ledger.sanitizer import Sanitizer
from openreading.readiness import auth_hinted, missing_required
from openreading.router.clock import RealClock
from openreading.router.compliance import BAA_TIER_CONFIRMED_WARNING, baa_tier_confirmation
from openreading.router.cost import apply_cost_report
from openreading.router.driver import run_to_completion
from openreading.router.executor import BoundedResultCache, execute_plan
from openreading.router.router import RoutePlan, Router, RouterConfig
from openreading.types.errors import (
    AdapterError,
    ComplianceRefused,
    MissingCredentialsError,
    ScopeRefused,
    SourceNotFoundError,
    TerminalError,
    UnknownStrategyError,
)
from openreading.types.request import Compliance, OpenReadingRequest, Routing

# Reserved `backend.id` prefix for a strategy reference (spec §1.3 / loader.STRATEGY_PREFIX).
# Inlined here so a plain named-backend run never imports the strategy package (guardrail T10).
_STRATEGY_PREFIX = "strategy:"

_MIME_BY_EXT = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
# The policy key set, DERIVED from the three things a policy key can become — never re-typed
# beside them. A second, hand-maintained list is how a key gets added to the router and silently
# dropped by the loader (or the reverse): before this, `_apply_policy` filtered the caller's dict
# down to its own copy of the compliance names one line BEFORE `Compliance(extra="forbid")` could
# see it, so a misspelled or invented key was discarded in silence and the constraint the operator
# wrote did not exist. `tests/test_policy_validation.py` pins the derivation.
COMPLIANCE_POLICY_KEYS = tuple(Compliance.model_fields)  # -> request.compliance
# A deliberate SUBSET of Routing: `fallback` is a request field (chain order), not a constraint.
ROUTING_POLICY_KEYS = ("doc_type_hint", "optimize_for")  # -> request.routing
# -> RouterConfig (D7/D7a). A dataclass, so validate_policy type-checks these three by hand;
# test_policy_keys_are_derived_from_the_models_they_feed fails if a fourth arrives unchecked.
ROUTER_CONFIG_POLICY_KEYS = tuple(RouterConfig.__dataclass_fields__)
POLICY_KEYS = tuple(
    sorted({*COMPLIANCE_POLICY_KEYS, *ROUTING_POLICY_KEYS, *ROUTER_CONFIG_POLICY_KEYS})
)
_MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024  # 100 MB


class PolicyError(ValueError):
    """A `policy=` / `--policy` value that is not a well-formed policy object.

    A `ValueError`, because a policy is an argument the caller wrote, not a backend outcome: it
    never belongs in the `AdapterError` ladder. The CLI re-raises it under its own tag for exit 3
    (`cli.app._load_policy`), and the server never sees it — an HTTP caller spells the same
    constraints as `request.compliance` / `request.routing`, which the request schema and the
    pydantic models already refuse the same way.
    """


def validate_policy(policy: Any) -> dict[str, Any] | None:
    """Refuse a malformed policy before any of it is read, on every non-HTTP surface.

    `None` means "no policy" (the documented Python default) and passes through. Anything else
    must be an object whose keys are all in `POLICY_KEYS` and whose values type-check against the
    model each key feeds. Returns the policy unchanged; it validates, it never rewrites.

    Value types are checked by delegating to `Compliance` / `Routing` rather than re-stating them,
    so the policy surface and the request schema cannot disagree about what `optimize_for` accepts.
    `RouterConfig` is a dataclass with no validation of its own, so its three keys are checked
    here — and strictly: `bool("false")` is `True`, so a *string* under
    `allow_unverified_compliance` used to switch the fail-closed posture ON, and a bare string
    under `train_optout_confirmed` became a frozenset of its characters, confirming no backend at
    all while looking like it confirmed one.
    """
    if policy is None:
        return None
    if not isinstance(policy, dict):
        raise PolicyError(
            f"policy must be a JSON object, got {type(policy).__name__}; "
            f"valid keys: {', '.join(POLICY_KEYS)}"
        )
    unknown = sorted(k for k in policy if k not in POLICY_KEYS)
    if unknown:
        named = []
        for key in unknown:
            near = difflib.get_close_matches(str(key), POLICY_KEYS, n=1)
            named.append(f"{key!r}" + (f" (did you mean {near[0]!r}?)" if near else ""))
        plural = "s" if len(unknown) > 1 else ""
        raise PolicyError(
            f"unknown policy key{plural}: {', '.join(named)}; valid keys: {', '.join(POLICY_KEYS)}"
        )
    try:
        # strict=: the HTTP surface type-checks the same values against request.v0.2.json BEFORE
        # pydantic sees them, and JSON Schema does not coerce. Without strict=, `require_baa:
        # "yes"` would be accepted here and rejected over HTTP — the same divergence in a new place.
        Compliance.model_validate(
            {k: policy[k] for k in COMPLIANCE_POLICY_KEYS if k in policy}, strict=True
        )
        Routing.model_validate(
            {k: policy[k] for k in ROUTING_POLICY_KEYS if k in policy}, strict=True
        )
    except ValidationError as e:
        errors = "; ".join(f"{'.'.join(str(p) for p in d['loc'])}: {d['msg']}" for d in e.errors())
        raise PolicyError(f"invalid policy value: {errors}") from e
    if not isinstance(policy.get("allow_unverified_compliance", False), bool):
        raise PolicyError("invalid policy value: allow_unverified_compliance must be true or false")
    for key in ("train_optout_confirmed", "baa_tier_confirmed"):
        value = policy.get(key, [])
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise PolicyError(f"invalid policy value: {key} must be a list of backend ids")
    return policy


def _document_dict(source: str | bytes, mime_type: str | None) -> dict[str, Any]:
    if isinstance(source, bytes | bytearray):
        # bytes with no explicit mime default to PDF (documented; D-v2-9).
        return {
            "bytes_base64": base64.b64encode(bytes(source)).decode(),
            "mime_type": mime_type or "application/pdf",
        }
    s = str(source)
    if s.startswith(("http://", "https://")):
        # Preserve the caller's explicit mime_type (None is valid on DocumentInput) instead of
        # dropping it here -- materialize_document's own `d.mime_type or "application/pdf"`
        # fallback is what supplies the PDF default when the caller gave none (L1).
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
    mime = mime_type or _MIME_BY_EXT.get(p.suffix.lower(), "application/pdf")
    return {
        "bytes_base64": base64.b64encode(p.read_bytes()).decode(),
        "mime_type": mime,
        "filename": p.name,
    }


def _apply_policy(body: dict[str, Any], policy: dict | None) -> None:
    # Validate the WHOLE policy first, then split. Splitting first made the split a silent
    # whitelist: whatever it did not recognize never reached a validator (see validate_policy).
    policy = validate_policy(policy)
    if not policy:
        return
    compliance = {k: policy[k] for k in COMPLIANCE_POLICY_KEYS if k in policy}
    if compliance:
        body["compliance"] = compliance
    routing = {k: policy[k] for k in ROUTING_POLICY_KEYS if k in policy}
    if routing:
        body["routing"] = routing


def router_config(policy: dict | None) -> RouterConfig:
    """The three deployment-level policy keys as a `RouterConfig` (D7/D7a).

    It validates the whole policy first, because a `route()`-shaped call reaches this with a
    policy that never passed through `build_request`.
    """
    policy = validate_policy(policy) or {}
    return RouterConfig(
        allow_unverified_compliance=bool(policy.get("allow_unverified_compliance", False)),
        train_optout_confirmed=frozenset(policy.get("train_optout_confirmed", [])),
        baa_tier_confirmed=frozenset(policy.get("baa_tier_confirmed", [])),
    )


def build_request(
    source: str | bytes,
    backend: str = "auto",
    *,
    operation: str | None = None,
    mime_type: str | None = None,
    policy: dict | None = None,
    **overrides: Any,
) -> OpenReadingRequest:
    """Build the `OpenReadingRequest` that the CLI and the server hand to `run_request`.

    `document` and `backend` are derived from `source=` and `backend=` alone. Passing either one
    through `**overrides` raises `ValueError` (BL-105). A named backend's `type` is filled in from
    its descriptor, and a `strategy:<name>` id is never looked up in the registry (T1). `policy`
    is validated whole before it is split into `compliance` and `routing`.
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
    if backend != "auto" and not backend.startswith("strategy:"):
        body["backend"]["type"] = make_adapter(backend).descriptor.type.value
    if operation:
        body["backend"]["operation"] = operation
    _apply_policy(body, policy)
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
    `descriptor=None` forces materialization (used for the auto path when any chain member needs
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
            "mime_type": d.mime_type or "application/pdf",
        }
    )
    return req.model_copy(update={"document": new_doc})


def route(
    source: str | bytes,
    *,
    policy: dict | None = None,
    operation: str | None = None,
    mime_type: str | None = None,
) -> RoutePlan:
    """The compliance-first routing plan for a document (no execution)."""
    req = build_request(source, "auto", operation=operation, mime_type=mime_type, policy=policy)
    return Router(build_registry(), router_config(policy)).route(req)


def _arm_ledger(*args, **kwargs) -> Executor | None:
    """`_arm_ledger_unguarded` with one guarantee added: every OSError it raises is reported as the
    ledger's, by name.

    Arming is entirely filesystem work under `$OPENREADING_LEDGER` — creating the key and blob
    stores, the reaper sweep, the retention stamp, the header, the document blob — and it happens
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

    Also runs the at-run-start reaper sweep and stamps this run's own retention ceiling (Open
    Questions §9 item 2's recommendation (a); plan §7). `OPENREADING_LEDGER_RETENTION_HOURS`
    overrides the T1 provisional default. The choice of a real default is tracked in
    `internal/eng-council/FOUNDER-INBOX.md` and is not settled here.

    Ledger T3: a FRESH run's stamp is the operator default ALONE — nothing has dispatched yet, so
    nothing narrows it. Earlier, this stamped `compute_retention_ceiling_hours` over `eligible`
    (the request's WHOLE registry-wide compliance/capability survivor set, `Router.route`'s own
    stage-1/2 output — correct and appropriately broad for the `Sanitizer` arming below, where
    over-inclusion is harmless, but not for this) — so a backend merely eligible for the document
    type, never named by the compiled strategy nor dispatched, could collapse the ceiling (and
    force `zdr`) to its own strict limit for a run that never went near it. `InlineExecutor` now
    tightens (never widens) this stamp itself, per step, from each backend's own descriptor, ONLY
    as it actually dispatches (`ledger/retention.py`'s `tighten_retention`, called from
    `ledger/inline.py`'s live "ok" branch) — a backend that stays merely eligible has zero effect
    on the stamp. The same rescoping applies to ZDR blob suppression
    (`InlineExecutor._is_zdr_backend`, a per-step registry lookup replacing the old whole-run
    `zdr=` boolean this function used to compute and pass in).

    The `Sanitizer` backstop (§9.3) is still armed with every ELIGIBLE descriptor's
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
    keys = LocalFsKeyStore(ledger_root / "keys")
    blobs = LocalFsBlobStore(ledger_root / "blobs", keys)
    journal = JsonlJournal(ledger_root / f"{run_id}.jsonl")

    # Wall clock, never `now_ms()`: the stamp is read back by a LATER process, and a monotonic
    # reading's zero point is the boot (`openreading.router.clock`). The reaper's own `now` must
    # come from the same base as the stamp it compares against, so both read `now_wall_ms()`.
    now_ms = int(clock.now_wall_ms())
    if not resume:
        reap(ledger_root, keys, ledger_root / "blobs", now_epoch_ms=now_ms)
    descriptors = [
        registry.get(bid).descriptor for bid in eligible if registry.get(bid) is not None
    ]
    default_hours = float(
        os.environ.get("OPENREADING_LEDGER_RETENTION_HOURS", DEFAULT_RETENTION_HOURS)
    )
    if not resume:
        # Nothing has dispatched yet — the sane, un-narrowed starting point (see this function's
        # own docstring). `InlineExecutor` tightens this per step, per backend, as the walk
        # actually runs; a merely-eligible backend that never dispatches never touches it.
        stamp_run(
            ledger_root, run_id, expires_epoch_ms=now_ms + int(default_hours * 3600_000), zdr=False
        )

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
            # `document.url` is a secret-class field (§9.3, "routinely a presigned URL, forwarded
            # verbatim," unconditionally, not by size) that must never land in the plaintext
            # `slim_request` sidecar (`slim_request_dict` already strips it). Routed through the
            # SAME per-run encrypted blob store a `bytes_base64` document's own bytes already use,
            # so it earns the identical shred/erasure guarantee instead of persisting forever in a
            # file `reap()` never touches.
            raw = req.document.url.encode("utf-8")
            digest = "sha256:" + hashlib.sha256(raw).hexdigest()
            document_ref = blobs.put(run_id, digest, raw, DOCUMENT_URL_MEDIA_TYPE)
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


def reap_expired_now() -> list[str]:
    """Reap every expired stamped run immediately, independent of any run arming (finding M7).

    `_arm_ledger_unguarded`'s own sweep only runs when a NEW run arms, so a server that has gone
    idle since its last request would otherwise hold that run's expired content (encrypted
    document blobs, and the key that unlocks them) past its retention ceiling indefinitely —
    nothing else in this module ever revisits the ledger root unprompted. `server.app.create_app`
    calls this once at startup, and `server.app._sweep_retention_forever` keeps calling it on a
    timer for as long as the server serves, so a process that never goes busy again still enforces
    expiry on schedule rather than only when something happens to wake it; a fully idle CLI-only
    install still only enforces on its next run.

    Mirrors `_arm_ledger_unguarded`'s own path construction exactly — keys at `<root>/keys`, blobs
    at `<root>/blobs`, the wall clock for the epoch `reap` compares stamps against — so the two
    sweeps can never disagree about where a run's content lives. No-op (`[]`) when
    `OPENREADING_LEDGER` is unset, the same "arming is env-only, no flag" contract documented on
    `_arm_ledger_unguarded` — equally a no-op when it is SET but names something other than a
    directory, and equally a no-op on ANY other `OSError` while constructing the key store or
    reaping (M7 review finding): `keys` or `blobs` existing as a plain file one level down still
    makes `LocalFsKeyStore.__init__`'s `mkdir(exist_ok=True)` raise `FileExistsError` (`exist_ok`
    only suppresses the case where the target is already a directory), and a permissions error is
    always possible under a root this process doesn't fully control. Fail open: this is a
    best-effort startup cleanup, not a request a caller is waiting on, so it must never be the
    reason `create_app` fails to boot — the one attacker-reachable vector, a malformed
    `retention/*.json` stamp, is already handled inside `reap()` itself and never raises here.

    Deliberately outside this module's documented Python API surface (no `__all__` entry, no row
    in the "Exports and return shapes" section above): it is a server operational concern, not a
    document-processing recipe, and its only caller is `create_app`.
    """
    root = os.environ.get("OPENREADING_LEDGER")
    if not root:
        return []
    ledger_root = Path(root)
    if ledger_root.exists() and not ledger_root.is_dir():
        return []
    try:
        keys = LocalFsKeyStore(ledger_root / "keys")
        return reap(
            ledger_root, keys, ledger_root / "blobs", now_epoch_ms=int(RealClock().now_wall_ms())
        )
    except OSError:
        # Fail open (see docstring): keys/blobs existing as a file (FileExistsError from mkdir),
        # a permissions error under the ledger root, or any other filesystem surprise here must
        # not take the whole server down over a best-effort startup cleanup.
        return []


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
    enforced in compile_strategy (which is what bounds an `auto` rung) and re-checked at every
    dispatch, and raises ScopeRefused (→ 403 scope_denied) when it leaves the walk nothing to
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
        # defaults, so compilation sees only the request's own compliance.
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
    )
    # materialize a URL to bytes if any eligible backend can't ingest URLs (mirrors the auto arm)
    if any(
        not (a := registry.get(bid)) or not a.descriptor.accepts_url for bid in compiled.eligible
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
        compiled.eligible,
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
    """Resolve a directly-named backend (not `auto`, not `strategy:<name>`) for execution:
    construct its adapter, compliance-gate it against the request exactly as the `auto` router
    would (a named backend does not get to skip the request's own compliance constraints —
    GAP-1), materialize the document, build the RunContext, and credential-gate it. Shared by
    `run_request`'s named-backend branch (below) and the server's `submit_job` handler so the two
    call sites can't drift out of parity by hand-copying again (BL-91) — every caller gets the
    identical compliance verdict and the identical `signup_url`-bearing credentials message for
    the identical failure.

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

    Raises KeyError (unknown backend), ComplianceRefused, or MissingCredentialsError — callers
    map each the same way they map any other adapter-invocation error. On success, returns
    (adapter, req, ctx) ready for `adapter.submit(req, ctx)`."""
    broker = broker or EnvCredentialBroker()
    config = config or RouterConfig()
    adapter = make_adapter(backend)  # KeyError → caller maps to 404
    # A directly-named backend is still subject to the request's compliance constraints — raise
    # ComplianceRefused (→ 403) rather than silently ignoring them.
    if req.compliance is not None:
        Router(build_registry(), config).check_eligible(req, backend)
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
    """Execute a fully-built request (backend.id names a backend, 'auto', or 'strategy:<name>').
    The server calls this with the RouterConfig + strategy config from its env; `run()` calls it
    with the config from a policy + a discovered openreading.yaml. `plain_info` (from the loader)
    carries Plain-dialect gate provenance for `explain` (§9); absent, gates render flat. `cache` is
    the caller's idempotency cache for the `auto` chain; the server owns one per app, the CLI and
    `run()` pass none so a library call always does the work (D-v3-3). `deadline_ms` (BL-153,
    wired to a real caller in BL-169) is forwarded to `prepare_named_backend` for the named-backend
    branch only — `run()`'s own `deadline_ms` parameter and the CLI's `--deadline` flag on `parse`
    now originate a real one; `auto`/strategy dispatch is untouched, it manages its own per-node
    time budget instead.

    `backend_allowlist` is the CALLER's ceiling on which backends this request may reach (the
    server's per-token `OPENREADING_API_KEY_SCOPES` entry); None means unscoped. Every arm that
    picks its own backends reads it, which is all of them but the directly-named one:

    - Both strategy arms — including the one an `auto` request takes when `defaults.strategy` is
      configured, which is a strategy walk wearing an `auto` id and would otherwise be gated as
      though the plain router had chosen.
    - The plain `auto` arm, which prunes the router's CHAIN to the allow-list before executing it.
      A caller gating this one at the door can only ever check the router's first pick; the plan
      is chosen plus every fallback, and `execute_plan` walks all of it, so the backends behind
      the first pick were reachable by a request that named any of them and got 403.

    Only the directly-named arm needs nothing here, because there the id IS the request and the
    caller can gate it before the call.

    Raises KeyError (unknown backend), UnknownStrategyError, PlanExhaustedError, ComplianceRefused,
    ScopeRefused (the caller's allow-list leaves the walk, or the pruned `auto` chain, nothing to
    run), TerminalError, or RetryableError (a directly-named backend's rate-limit exhaustion, or
    router.driver's poll loop past its deadline/MAX_CONSECUTIVE_FAULTS — the `auto` path folds this
    into PlanExhaustedError via execute_plan/D-v2-7.2 instead, since it can fall back to the next
    backend; a named backend has no next rung, so it surfaces here under its own type)."""
    broker = broker or EnvCredentialBroker()
    config = config or RouterConfig()
    backend = req.backend.id

    # `strategy:<name>` — run the strategy; `strategy:none` forces the legacy path (ignore any
    # defaults.strategy); otherwise `auto` + defaults.strategy engages that strategy (spec §1.3).
    strat = backend[len(_STRATEGY_PREFIX) :] if backend.startswith(_STRATEGY_PREFIX) else None
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
        backend = "auto"  # escape hatch: plain router, no defaults.strategy
    elif (
        backend == "auto"
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

    if backend == "auto":
        plan = Router(build_registry(), config).route(req)
        if plan.chosen is None:
            # the router eliminated every backend on compliance/capability → refused, NOT a
            # runtime failure (PlanExhaustedError is for a non-empty plan whose backends all fail).
            # Checked BEFORE the allow-list below, so an already-empty plan stays compliance's
            # call: scope removed nothing there, and only ever subtracts (BL-159 AC-4).
            dropped = ", ".join(f"{i}:{dr.code}" for i, dr in sorted(plan.dropped.items()))
            raise ComplianceRefused(
                f"no eligible backend for the request (dropped: {dropped})",
                constraint=plan.terminal_reason or "no_compliant_backend",
            )
        if backend_allowlist is not None:
            # The caller's ceiling, applied to the whole CHAIN — chosen plus every fallback — and
            # applied HERE, between routing and execution, because this is the last moment the set
            # of backends this request can reach is known and the first adapter has yet to be
            # built. `auto` names no backend, so a check at the door can only ever speak for the
            # router's first pick; the twelve behind it were reachable, and a document that pymupdf
            # fails on walked straight into them.
            denied = sorted(i for i in plan.eligible_ids if i not in backend_allowlist)
            first_pick = plan.chosen.descriptor.id
            plan = plan.restrict_to(backend_allowlist)
            if plan.chosen is None:
                # Fail closed. Nothing this caller may reach survived, so this is scope's refusal
                # to make and not compliance's: the fix is the token's allow-list, and answering
                # `compliance_refused` would send the operator to edit a policy that is not the
                # problem. Never a 502 either — no backend was allowed to try, so nothing failed.
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
            note = baa_tier_confirmation(req.compliance, adapter.descriptor, config)
            if note is not None:
                resp.add_warning(BAA_TIER_CONFIRMED_WARNING, note, adapter.descriptor.id)
            return resp.to_schema_dict()
    except AdapterError:
        raise  # the five _ADAPTER_ERRORS taxonomy types keep their own specific handling downstream
    except Exception as e:
        # BL-99: adapter.normalize() is ordinary adapter code, not one of the five taxonomy types —
        # a plain KeyError/IndexError/ValueError/AttributeError out of it (or submit()/poll()) used
        # to propagate straight out of run_request, past every caller's typed except clauses
        # (server's _ADAPTER_ERRORS catch, the CLI's own (TerminalError, ComplianceRefused) catch),
        # to a bare, undocumented crash. auth_hinted (widened above) has already redacted e's
        # message by the time it reaches here; converting it into a TerminalError — already one of
        # run_request's documented raises — gives it the identical structured, non-500 handling
        # BL-85 already gives the three async-job sinks, with no caller-side change required.
        raise TerminalError(str(e)) from e


def run(
    source: str | bytes,
    backend: str = "auto",
    *,
    strategy: str | None = None,
    config: str | None = None,
    operation: str | None = None,
    policy: dict | None = None,
    env_file: str | None = None,
    mime_type: str | None = None,
    broker: EnvCredentialBroker | None = None,
    transport=None,
    keep_candidates: bool = False,
    deadline_ms: int | None = None,
    on_run_armed: Callable[[str], None] | None = None,
    **request_overrides: Any,
) -> dict[str, Any]:
    """Run one document through a named backend, the compliance-first router (`backend="auto"`),
    or a strategy, and return a `response.v0.3` envelope. `strategy="<name>"` is sugar for
    `backend="strategy:<name>"`. `source` is a path, an http(s) URL, or raw bytes. `config` points
    at an openreading.yaml, and without it `./openreading.yaml` is discovered.

    This is a thin wrapper over `run_request` (via `build_request`) and propagates whatever that
    raises, `RetryableError` included. Every exception type this call can raise, and what each one
    means, is in the "Exceptions" section of this module's own docstring.

    `deadline_ms` (BL-169) is the caller's absolute time budget for a DIRECTLY-NAMED backend only,
    forwarded to `run_request`'s named-backend branch (`prepare_named_backend`, BL-153's own
    plumbing). Omitted (the default), a named backend resolves to `credentials.DEFAULT_DEADLINE_MS`
    (2 minutes), which is too short for some hosted async backends' ordinary workload. The CLI
    spelling of the same knob is `parse --deadline`, documented in `openreading.cli`. It has no
    effect on `backend="auto"` or `strategy="..."` dispatch, which manage their own per-node budget.

    `on_run_armed` (Ledger T3, plan §4.4) is invoked once, with the run id, immediately after the
    ledger arms. Only a strategy-dispatch path arms one, so a plain named-backend or `auto` run
    never fires it. It mirrors the optional-hook shape of `run_batch`'s own `on_progress` and
    `on_preflight`.
    """
    if env_file:
        load_dotenv(env_file)
    if strategy is not None:
        backend = f"strategy:{strategy}"
    # Discover the openreading.yaml ONLY when it could matter — an `auto` request (defaults.strategy)
    # or a `strategy:` id. A plain named backend never engages a strategy, so the strategy package is
    # not imported at all (guardrail T10: no file / no strategy ⇒ no strategy-module import).
    loaded = None
    if backend == "auto" or backend.startswith(_STRATEGY_PREFIX):
        from openreading.strategies.loader import load_config

        loaded = load_config(config)  # CLI/Python discover cwd; None → legacy path
    req = build_request(
        source,
        backend,
        operation=operation,
        mime_type=mime_type,
        policy=policy,
        **request_overrides,
    )
    return run_request(
        req,
        broker=broker,
        config=router_config(policy),
        transport=transport,
        strategy_config=loaded.config if loaded else None,
        plain_info=loaded.plain_info if loaded else None,
        keep_candidates=keep_candidates,
        deadline_ms=deadline_ms,
        on_run_armed=on_run_armed,
    )


def _request_from_header(header: RunHeader, blobs: LocalFsBlobStore) -> OpenReadingRequest:
    """Reconstructs the `OpenReadingRequest` a resumed strategy walk needs from the header's own
    `slim_request` + `document` (see `ledger/header.py`'s module docstring for what's deliberately
    NOT recoverable this way — `document.password`/`async.webhook_url`, never persisted).

    `header.document` holds EITHER a `bytes_base64` document's own bytes OR a URL-sourced
    document's `document.url` string. Both are routed through the same encrypted blob store rather
    than the plaintext `slim_request` echo. `header.document_is_url` tells the two apart (NOT
    `media_type`, which for the bytes case is a caller-supplied, unvalidated `mime_type` that could
    collide with a sentinel value)."""
    body: dict[str, Any] = dict(header.slim_request)
    doc = dict(body.get("document") or {})
    if header.document is not None:
        # BlobStore.get raises PayloadExpired once the run's key is shredded — left to propagate
        # uncaught: the whole-run analog of AC-10's "resume reports expired" per-step signal.
        raw = blobs.get(header.document)
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
    - A scope that only narrowed the eligible set — the `auto`-rung case, where the tree is
      identical either way — leaves `plan_hash` matching, and correctly so. The header's
      `pinned_eligible` carries that narrowed set, and `_arm_ledger(resume=True)` arms the resumed
      executor's per-step gate from THIS header rather than a freshly recomputed set, so an `auto`
      rung re-resolves inside the original scope rather than across the whole registry.

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
    from openreading.strategies.loader import load_config
    from openreading.strategies.model import StrategyConfig

    keys = LocalFsKeyStore(ledger_root / "keys")
    blobs = LocalFsBlobStore(ledger_root / "blobs", keys)
    req = _request_from_header(header, blobs)

    loaded = load_config(None)
    strategy_config = loaded.config if loaded else StrategyConfig(version=1)
    registry = build_registry()
    broker = EnvCredentialBroker()
    config = router_config(None)  # §10: "no other flags" — a resume never takes its own --policy
    compiled = compile_strategy(req, header.strategy_name, strategy_config, registry, config)
    clock = RealClock()
    executor = _arm_ledger(
        run_id,
        req,
        registry,
        broker,
        clock,
        compiled.eligible,
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


def _effective_formats(backend: str, broker: EnvCredentialBroker) -> set[str]:
    """The M3 supported-format set for a batch (internal/design/batch-intake.md §4): a directly named
    backend contributes its own input_formats; `auto` / a `strategy:` id contributes the union
    across every READY backend (an unready backend can't take anything). Normalized tokens."""
    from openreading.adapters.registry import BUILTIN_ADAPTERS
    from openreading.batch.sources import normalize_input_format
    from openreading.readiness import backend_readiness

    def _fmts(desc) -> set[str]:
        return {normalize_input_format(f) for f in desc.capabilities.input_formats}

    if backend == "auto" or backend.startswith(_STRATEGY_PREFIX):
        out: set[str] = set()
        for slug in BUILTIN_ADAPTERS:
            adapter = make_adapter(slug)
            if backend_readiness(adapter, broker=broker).ready:
                out |= _fmts(adapter.descriptor)
        return {f for f in out if f}
    return {f for f in _fmts(make_adapter(backend).descriptor) if f}


def run_batch(
    sources: list[str],
    backend: str = "auto",
    *,
    strategy: str | None = None,
    config: str | None = None,
    jobs: int = 1,
    max_jobs: int = MAX_BATCH_JOBS,
    max_items: int = DEFAULT_MAX_ITEMS,
    deadline_ms: int | None = None,
    env_file: str | None = None,
    policy: dict | None = None,
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
    propagates out of this call like any single `run()` error: TerminalError, ComplianceRefused, or
    RetryableError (a directly-named backend has no next rung to fall back to, exactly like
    `run_request`'s own named-backend branch — see its docstring). `_run_native`'s own docstring
    already makes this promise; it is repeated here because this is the function most callers
    actually read (BL-128)."""
    from openreading.batch import runner as _batch_runner
    from openreading.batch.sources import resolve_intake
    from openreading.types.batch import BatchRequestEcho

    jobs = _batch_runner.bound_jobs(jobs, max_jobs=max_jobs)
    # Like `jobs`, `policy` is an argument about the WHOLE batch, so it is checked before intake
    # rather than per item. On the platform path a per-item failure is isolated into that item's
    # `error` and never raised (M6) — correct for a document that could not be read, wrong for a
    # policy the caller mistyped, which would otherwise come back as N identical item errors and a
    # zero exit instead of one refusal.
    validate_policy(policy)

    if env_file:
        load_dotenv(env_file)
    if strategy is not None:
        backend = f"{_STRATEGY_PREFIX}{strategy}"
    broker = broker or EnvCredentialBroker()

    resolved = resolve_intake(
        list(sources), supported_formats=_effective_formats(backend, broker), max_items=max_items
    )
    if on_preflight is not None:
        on_preflight(resolved, backend)

    # §6: a named backend may cap platform concurrency (e.g. CPU-bound tesseract) via
    # descriptor.batch.max_concurrency — the runner takes min(requested, cap).
    if backend != "auto" and not backend.startswith(_STRATEGY_PREFIX):
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
            policy=policy,
            deadline_ms=deadline_ms,
            **request_overrides,
        )

    def run_one(src, idem):
        source = src.ref.path or src.ref.url
        return run(
            source,
            backend=backend,
            config=config,
            policy=policy,
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


def _native_adapter(backend: str, resolved: list, broker: EnvCredentialBroker):
    """§7 dispatch rule → the adapter to use for a native batch, or None for platform fan-out.
    Native iff: a directly named backend (not auto/strategy), its descriptor declares `batch.native`
    truthy, it implements the NativeBatchAdapter protocol, there is >=1 non-skipped item, and the
    count is within `batch.max_items`."""
    from openreading.adapters.base import NativeBatchAdapter

    if backend == "auto" or backend.startswith(_STRATEGY_PREFIX):
        return None
    try:
        adapter = make_adapter(backend)
    except KeyError:
        return None
    bi = adapter.descriptor.batch
    if not bi or not bi.native or not isinstance(adapter, NativeBatchAdapter):
        return None
    live = [r for r in resolved if r.skip_reason is None]
    if not live:
        return None
    if bi.max_items is not None and len(live) > bi.max_items:
        return None  # too many for one native batch → fall back to platform
    return adapter


def _run_native(
    adapter,
    resolved: list,
    backend: str,
    *,
    broker: EnvCredentialBroker,
    transport,
    idempotency_key: str | None,
    request_echo,
    on_progress,
    policy: dict | None = None,
    deadline_ms: int | None = None,
    **request_overrides: Any,
) -> dict[str, Any]:
    """Execute a whole batch through an adapter's native submit_many/normalize_many (§7). Per-item
    results (NormalizedResponse | BatchItemError) map to succeeded/failed items with
    transport="native"; skipped items are merged back in input order. A batch-level failure (e.g.
    submit_many raising) propagates like any run() error. Envelope-equivalent to platform (M10).

    A directly-named backend does not get to skip the request's own compliance constraints on this
    path either (BL-98): `policy` is applied per item via `build_request` exactly like the platform
    path's `run()` already does — closing a structural no-op, since `policy` is `run_batch`'s own
    named parameter and previously could never reach here at all — and the resulting
    `req.compliance` (from either the `policy=` or the raw `compliance=` override spelling) is
    enforced with the same `Router.check_eligible` call `run_request`'s named-backend branch uses,
    before `adapter.submit_many` ever sees a document. Every item in a batch shares identical
    policy/overrides, so `req.compliance` is identical across `reqs` — checking the first stands in
    for the whole batch.

    `deadline_ms` (BL-135): `run_batch`'s own override, forwarded here. None (the default) means
    `build_run_context` falls back to `DEFAULT_NATIVE_BATCH_DEADLINE_MS` rather than the generic,
    two-minute `DEFAULT_DEADLINE_MS` every other, synchronous-feeling path uses — this is the ONE
    dispatch shape in the whole codebase where a caller submits one job and then polls a
    vendor-side batch that the adapter's own descriptor documents as routinely taking up to an
    hour, so it gets its own, larger, still-overridable default rather than inheriting the generic
    constant every other path also reuses."""
    import time

    from openreading.batch import runner as _batch_runner
    from openreading.batch.runner import item_idempotency_key
    from openreading.types.batch import BatchItem, BatchItemError

    started = time.perf_counter()
    live = [r for r in resolved if r.skip_reason is None]
    reqs = []
    for src in live:
        source = src.ref.path or src.ref.url
        idem = item_idempotency_key(idempotency_key, src.ref.sha256)
        overrides = {k: v for k, v in request_overrides.items() if v is not None}
        req = build_request(source, backend, idempotency_key=idem, policy=policy, **overrides)
        req = materialize_document(req, adapter.descriptor, transport=transport)
        reqs.append(req)

    if reqs[0].compliance is not None:
        Router(build_registry(), router_config(policy)).check_eligible(reqs[0], backend)

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
    li = 0
    for src in resolved:
        if src.skip_reason is not None:
            items.append(BatchItem(source=src.ref, state="skipped", skip_reason=src.skip_reason))
        else:
            res = results[li] if li < len(results) else BatchItemError(code="missing_result")
            li += 1
            if isinstance(res, BatchItemError):
                items.append(
                    BatchItem(source=src.ref, state="failed", error=res, transport="native")
                )
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
