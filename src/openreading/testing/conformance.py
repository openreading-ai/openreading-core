"""The adapter conformance kit — one reusable suite EVERY adapter must pass (GOAL "The
harness"). Adding adapter N+1 means: implement + fixtures + `check_adapter_conformance`
green. No adapter merges without it. Downstream adapter authors import this too.

It drives an adapter over sample requests (submit → driver → normalize) and asserts the
guardrails hold: the descriptor validates, every normalized response validates against the
response JSON Schema (and thus the anyOf common denominator), all bboxes are canonical [0,1]
with bbox_native, declared-impossible (X) channels are absent AND warned (never faked), the
error taxonomy is respected, report_cost answers without raising and with a pass-through billing
target, and a deterministic adapter is idempotent on resubmit.

**Rollout (DESIGN §7).** The kit no longer stops at the first failure: `check_adapter_conformance`
returns a `ConformanceReport` with `.violations` (raise-worthy) and `.advisories`. A per-run
`strict_checks: set[str]` promotes the advisory-by-default invariants (C1 no-markup-in-text,
C6 deliver-or-warn) to violations for adapters that have been remediated; C7/C3 are strict from
Phase A; C11 (text/blocks coherence) is permanently advisory. `raise_on_violation=True` (default)
keeps the historical "raise on any real violation" behaviour for the existing call sites.

**Ledger T4a — R1/R2/R3 (AC-5/AC-6/AC-7).** Pass `adapter_factory` (a zero-arg callable returning a
FRESH adapter instance sharing no Python-level state with `adapter` — typically a fresh injected fake
client each call, never a shared one) to turn on three additional checks, per `ConformanceCase`:
R2 (no instance caching) right after `submit()`, R1 (fresh-instance resume) by round-tripping the
just-submitted `Job` through `to_dict`/`json.dumps`/`json.loads`/`from_dict` and driving THAT to
completion on a brand-new `adapter_factory()` instance, and R3 (JSON-serializable at every stage)
both immediately after `submit()` and again after the case's own drive-to-completion on `adapter`
finishes. Omitting `adapter_factory` (the default) skips all three — any adapter not yet proven
client-free (all 15 built-ins are client-free; a third-party adapter mid-migration to
protocol v2 may not be yet) keeps passing the kit exactly as before.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from openreading import schemas
from openreading.ledger.header import slim_request
from openreading.router.clock import FakeClock
from openreading.router.driver import run_to_completion
from openreading.types.descriptor import AdapterDescriptor, ConfigField, CredentialField
from openreading.types.enums import ChannelGrade, JobState
from openreading.types.job import Job
from openreading.types.request import OpenReadingRequest
from openreading.types.runtime import Health, RunContext


@dataclass
class ConformanceCase:
    """A sample input an adapter can actually handle (happy path). `deterministic` enables the
    idempotent-resubmit equality check (true for local parsers/fixtures; false for live LLMs)."""

    request: OpenReadingRequest
    ctx: RunContext = field(default_factory=RunContext)
    deterministic: bool = True
    label: str = ""


class ConformanceError(AssertionError):
    pass


@dataclass
class Finding:
    """One kit observation, tagged with its invariant/check id (C1, C6, C7, C8, schema, …)."""

    check: str
    case: str
    message: str

    def __str__(self) -> str:
        return f"({self.check}) case={self.case!r}: {self.message}"


# Checks that are ALWAYS raise-worthy (the historical guardrails + the Phase-A strict additions).
# R1/R2/R3 (Ledger T4a, AC-5/AC-6/AC-7) join this set here — they only ever fire when a caller
# opts in via `adapter_factory` (see check_adapter_conformance's own docstring), so promoting them
# to always-strict cannot regress any existing call site that doesn't pass one.
_ALWAYS_STRICT = frozenset(
    {
        "schema",
        "identity",
        "static",
        "driver",
        "cost",
        "determinism",
        "C4",
        "C5",
        "C8",
        "C7",
        "C3",
        "R1",
        "R2",
        "R3",
    }
)
# Permanently advisory — legitimate divergence (e.g. header filtering) is tolerated.
_PERMANENTLY_ADVISORY = frozenset({"C11"})
# C1 (no-markup-in-text) and C6 (deliver-or-warn) are advisory by default and promoted to
# violations per adapter via `strict_checks` as each is remediated in Phase B.


def _is_violation(check_id: str, strict_checks: set[str]) -> bool:
    if check_id in _PERMANENTLY_ADVISORY:
        return False
    return check_id in _ALWAYS_STRICT or check_id in strict_checks


@dataclass
class ConformanceReport:
    adapter_id: str
    violations: list[Finding] = field(default_factory=list)
    advisories: list[Finding] = field(default_factory=list)

    def raise_if_violations(self) -> None:
        if self.violations:
            body = "\n".join(f"  {f}" for f in self.violations)
            raise ConformanceError(
                f"[{self.adapter_id}] {len(self.violations)} conformance violation(s):\n{body}"
            )


Recorder = Callable[[str, str, str], None]


# --- individual checks -----------------------------------------------------------------


def _walk_blocks(resp: dict[str, Any]) -> Iterable[dict[str, Any]]:
    for page in (resp.get("document", {}) or {}).get("pages", []) or []:
        yield from page.get("blocks", []) or []


def _text_fields(resp: dict[str, Any]) -> Iterable[tuple[str, str]]:
    """Every text-channel field, per the §4 scope rule: document/pages/blocks/chunks text."""
    doc = resp.get("document", {}) or {}
    if doc.get("text"):
        yield ("document.text", doc["text"])
    for pi, page in enumerate(doc.get("pages", []) or []):
        if page.get("text"):
            yield (f"pages[{pi}].text", page["text"])
        for bi, block in enumerate(page.get("blocks", []) or []):
            if block.get("text"):
                yield (f"pages[{pi}].blocks[{bi}].text", block["text"])
    for ci, chunk in enumerate(resp.get("chunks", []) or []):
        if chunk.get("text"):
            yield (f"chunks[{ci}].text", chunk["text"])


def _check_bboxes(resp: dict[str, Any], rec: Recorder, case: str) -> None:
    def check_one(bb: dict[str, Any], where: str) -> None:
        for k in ("x", "y", "w", "h"):
            v = bb.get(k)
            if v is None or not (0.0 <= v <= 1.0):
                rec("C8", case, f"bbox.{k}={v} out of [0,1] at {where}")
        if "bbox_native" not in bb:
            rec("C8", case, f"bbox missing bbox_native at {where} (lossless-conversion rule)")

    for i, b in enumerate(_walk_blocks(resp)):
        if b.get("bbox"):
            check_one(b["bbox"], f"blocks[{i}]")
        for j, cell in enumerate((b.get("table", {}) or {}).get("cells", []) or []):
            if cell.get("bbox"):
                check_one(cell["bbox"], f"blocks[{i}].table.cells[{j}]")


def _check_confidence_bounds(resp: dict[str, Any], rec: Recorder, case: str) -> None:
    """C7 — every numeric confidence anywhere in the response is a float in [0,1] (qualitative
    TypedField.confidence strings are exempt). Mirrors _check_bboxes across the confidence sites."""

    def check(val: Any, where: str) -> None:
        if isinstance(val, bool):
            return
        if isinstance(val, (int, float)) and not (0.0 <= val <= 1.0):
            rec("C7", case, f"confidence {val} out of [0,1] at {where}")

    doc = resp.get("document", {}) or {}
    check((doc.get("doc_type") or {}).get("confidence"), "document.doc_type")
    check(doc.get("confidence"), "document")
    for pi, page in enumerate(doc.get("pages", []) or []):
        check(page.get("confidence"), f"pages[{pi}]")
    for i, b in enumerate(_walk_blocks(resp)):
        check(b.get("confidence"), f"blocks[{i}]")
        for j, cell in enumerate((b.get("table", {}) or {}).get("cells", []) or []):
            check(cell.get("confidence"), f"blocks[{i}].table.cells[{j}]")
    for name, tf in (resp.get("typed_fields") or {}).items():
        check(tf.get("confidence"), f"typed_fields[{name}]")
        for k, cit in enumerate(tf.get("citations") or []):
            check(cit.get("confidence"), f"typed_fields[{name}].citations[{k}]")


_ATX_LINE = re.compile(r"(?m)^\s{0,3}#{1,6}\s+\S")
_HTML_PAIR = re.compile(r"<([a-zA-Z][\w-]*)(?:\s[^>]*)?>.*?</\1\s*>", re.DOTALL)
_MD_TABLE_SEP = re.compile(r"(?m)^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$")


def _markup_reason(text: str) -> str | None:
    """A parseable GFM/HTML STRUCTURE (not a bare character) present in plain text → C1 leak."""
    if _ATX_LINE.search(text):
        return "ATX heading line"
    if _MD_TABLE_SEP.search(text):
        return "markdown table separator row"
    if _HTML_PAIR.search(text):
        return "HTML tag pair"
    return None


def _check_text_plain(resp: dict[str, Any], rec: Recorder, case: str) -> None:
    """C1 — the text channel is plain: no syntax that round-trips as parseable GFM/HTML structure
    (a heading line, a table separator, an HTML tag pair). Bare metacharacters are NOT flagged."""
    for where, text in _text_fields(resp):
        reason = _markup_reason(text)
        if reason:
            rec("C1", case, f"markup in plain text ({reason}) at {where}")


_FENCE_LINE = re.compile(r"(?m)^\s*(```+|~~~+)")


def _check_markdown_gfm(resp: dict[str, Any], rec: Recorder, case: str) -> None:
    """C3 — the markdown channel parses as GFM (weak check: balanced code fences). Strict GFM
    linting is deliberately avoided — real backends legally embed HTML."""
    doc = resp.get("document", {}) or {}
    candidates = [("document.markdown", doc.get("markdown"))]
    for pi, page in enumerate(doc.get("pages", []) or []):
        candidates.append((f"pages[{pi}].markdown", page.get("markdown")))
    for i, b in enumerate(_walk_blocks(resp)):
        candidates.append((f"blocks[{i}].markdown", b.get("markdown")))
    for where, md in candidates:
        if md and len(_FENCE_LINE.findall(md)) % 2 != 0:
            rec("C3", case, f"unbalanced code fence in markdown at {where}")


def _channel_present(resp: dict[str, Any], channel: str) -> bool:
    doc = resp.get("document", {}) or {}
    if channel == "markdown":
        return bool(doc.get("markdown"))
    if channel == "text":
        return bool(doc.get("text"))
    if channel == "blocks":
        return any(True for _ in _walk_blocks(resp))
    if channel == "block_bbox":
        return any(b.get("bbox") for b in _walk_blocks(resp))
    if channel == "block_confidence":
        return any(b.get("confidence") is not None for b in _walk_blocks(resp))
    if channel == "typed_fields":
        return bool(resp.get("typed_fields"))
    if channel == "table_cells":
        return any((b.get("table", {}) or {}).get("cells") for b in _walk_blocks(resp))
    return False


# Every response channel that carries an N/D/X grade — checked for fabrication.
_ALL_X_CHANNELS = (
    "markdown",
    "text",
    "blocks",
    "block_bbox",
    "block_confidence",
    "typed_fields",
    "table_cells",
)
# The subset a caller can EXPLICITLY request via `outputs`. Only these require a warning when
# graded X and requested; bbox/confidence are sub-attributes of blocks (not separately
# requestable), so their absence is normal partial output, not a silent drop.
_WARN_WHEN = {
    "markdown": lambda o: o.markdown,
    "text": lambda o: o.text,
    "blocks": lambda o: o.blocks,
    "typed_fields": lambda o: o.typed_fields,
    "table_cells": lambda o: o.tables == "cells",
}


_WORD = re.compile(r"[a-z0-9]+")
# `Warning.field` is either a bare channel name or a path into the response (`document.text`,
# `pages[0].blocks`, `typed_fields.total`) — split it into segments, keeping `_` inside a segment.
_FIELD_SEGMENT = re.compile(r"[^a-z0-9_]+")


def _response_warnings(resp: dict[str, Any]) -> list[dict[str, Any]]:
    ws: list[dict[str, Any]] = resp.get("warnings") or []
    return ws


def _field_names_channel(field: Any, channel: str) -> bool:
    if not isinstance(field, str):
        return False
    return any(seg == channel for seg in _FIELD_SEGMENT.split(field.lower()))


def _words_name_channel(text: str, channel: str) -> bool:
    words = _WORD.findall(text.lower())
    needle = _WORD.findall(channel)
    return any(words[i : i + len(needle)] == needle for i in range(len(words) - len(needle) + 1))


def _warned(warnings: list[dict[str, Any]], channel: str) -> bool:
    """Does some warning NAME this channel (C5/C6's "never silently drop")?

    The machine-readable `field` attribute is the primary signal — every adapter sets it to the
    channel name (the openreading.adapters runbook, "Channels (N/D/X)"). `code`/`message` are a secondary signal,
    matched on whole words only. WHY not a substring search over the concatenated warnings:
    'context' contains 'text' and 'notable' contains 'table', so an unrelated warning could vouch
    for a channel the adapter silently dropped — disarming the very check meant to catch that.
    """
    return any(
        _field_names_channel(w.get("field"), channel)
        or _words_name_channel(f"{w.get('code') or ''} {w.get('message') or ''}", channel)
        for w in warnings
    )


def _check_x_channels(
    resp: dict[str, Any],
    desc: AdapterDescriptor,
    req: OpenReadingRequest,
    rec: Recorder,
    case: str,
) -> None:
    from openreading.types.request import Outputs

    outputs = req.outputs or Outputs()
    channels = desc.output.channels
    warnings = _response_warnings(resp)
    for channel_name in _ALL_X_CHANNELS:
        if getattr(channels, channel_name) is not ChannelGrade.IMPOSSIBLE:
            continue
        # C4: an X channel must never be fabricated
        if _channel_present(resp, channel_name):
            rec("C4", case, f"channel {channel_name!r} graded X but present (fabricated)")
        # C5: an explicitly-requested X channel must surface a warning (never silently drop)
        requester = _WARN_WHEN.get(channel_name)
        if requester and requester(outputs) and not _warned(warnings, channel_name):
            rec(
                "C5",
                case,
                f"channel {channel_name!r} X and requested but no warning surfaced "
                f"(never silently drop)",
            )


def _check_deliver_or_warn(
    resp: dict[str, Any],
    desc: AdapterDescriptor,
    req: OpenReadingRequest,
    rec: Recorder,
    case: str,
) -> None:
    """C6 — the positive direction of C4/C5: a requested channel graded N or D is populated, or a
    machine-readable warning names it. (Would have caught the P2/P3 silent-empty D channels.)"""
    from openreading.types.request import Outputs

    outputs = req.outputs or Outputs()
    channels = desc.output.channels
    warnings = _response_warnings(resp)
    for channel_name in _ALL_X_CHANNELS:
        grade = getattr(channels, channel_name)
        if grade is ChannelGrade.IMPOSSIBLE:
            continue  # X is C4/C5's job
        requester = _WARN_WHEN.get(channel_name)
        if not (requester and requester(outputs)):
            continue  # only channels the caller actually requested
        if not _channel_present(resp, channel_name) and not _warned(warnings, channel_name):
            rec(
                "C6",
                case,
                f"channel {channel_name!r} graded {grade.value} and requested but neither "
                f"populated nor named in a warning",
            )


def _check_text_blocks_coherence(resp: dict[str, Any], rec: Recorder, case: str) -> None:
    """C11 (permanently advisory) — when both text and blocks are populated, document.text and the
    concatenated block spine should be loosely similar. Legitimate divergence (header filtering) is
    tolerated, so this only ever advises."""
    from openreading.comparison.align import token_similarity

    doc = resp.get("document", {}) or {}
    text = doc.get("text")
    spine = " ".join(b.get("text") or "" for b in _walk_blocks(resp)).strip()
    if not text or not spine:
        return
    sim = token_similarity(text, spine)
    if sim < 0.5:
        rec("C11", case, f"document.text vs block spine token similarity {sim:.2f} < 0.5")


def _check_credential_spec(desc: AdapterDescriptor, rec: Recorder) -> None:
    """v0.2: a backend that needs anything from the environment to run — a key, or a self-hosted
    endpoint/container URL — must DECLARE it, so the env broker and readiness UI can resolve it
    without hard-coding per-adapter keys. Every spec field must name at least one env var; hosted
    APIs must carry a signup_url; and credentials_spec carries ONLY secrets — endpoints, regions,
    and resource ids are ConfigFields, so they resolve into ctx.runtime, stay out of the redactor's
    secret set, and remain overridable per request."""
    for f in desc.credentials_spec:
        if not f.secret:
            rec(
                "static",
                "<spec>",
                f"credentials_spec field {f.key!r} is graded non-secret — endpoints, regions and "
                "resource ids belong in config_spec (ctx.runtime), not credentials_spec",
            )
    prov = desc.provisioning
    needs_env = prov.auth != "none" or any(m in prov.byo_mode for m in ("endpoint", "container"))
    if not needs_env:
        return
    if not desc.credentials_spec and not desc.config_spec:
        rec(
            "static",
            "<spec>",
            "backend needs env credentials/config but declares neither "
            "credentials_spec nor config_spec (v0.2)",
        )
    specs: list[CredentialField | ConfigField] = [*desc.credentials_spec, *desc.config_spec]
    for spec in specs:
        if not spec.key:
            rec("static", "<spec>", "spec field has an empty key")
        if not spec.env:
            rec("static", "<spec>", f"spec field {spec.key!r} names no env var")
    if desc.type.value == "hosted_api" and not desc.signup_url:
        rec("static", "<spec>", "hosted_api must declare a signup_url (v0.2)")


def _check_cost(adapter: Any, job: Job, rec: Recorder, case: str) -> None:
    """`report_cost` feeds `response.usage` at the router's choke points, so the kit holds it to
    two rules. It must not raise on a job the driver just finished: production degrades to an
    unmetered response rather than failing, which is a silent hole in the caller's accounting.
    And `billing_target` must stay pass-through — `openreading` (resale) is never valid, whatever
    the report is built from."""
    try:
        cost = adapter.report_cost(job)
    except Exception as exc:  # noqa: BLE001
        rec("cost", case, f"report_cost raised on a succeeded job: {type(exc).__name__}: {exc}")
        return
    if cost.billing_target not in ("caller_account", "caller_infra"):
        rec("cost", case, f"billing_target={cost.billing_target!r} (pure pass-through only)")


def _check_identity(
    resp: dict[str, Any], desc: AdapterDescriptor, rec: Recorder, case: str
) -> None:
    b = resp.get("backend", {})
    if b.get("id") != desc.id:
        rec("identity", case, f"response.backend.id={b.get('id')!r} != descriptor {desc.id!r}")
    if b.get("type") != desc.type.value:
        rec("identity", case, f"response.backend.type={b.get('type')!r} != {desc.type.value!r}")


def _check_capabilities_type(adapter: Any, rec: Recorder) -> None:
    """`capabilities()` is one of the required 8 methods (adapter_interface.md §2) — callers that
    introspect a backend's live capability view depend on the declared return type being a dict."""
    if not isinstance(adapter.capabilities(), dict):
        rec("static", "<static>", "capabilities() must return a dict")


def _check_health_type(adapter: Any, rec: Recorder) -> None:
    """`health()` is one of the required 8 methods — the readiness probe the router/CLI/server all
    call before driving a backend depends on the declared return type being a Health."""
    if not isinstance(adapter.health(), Health):
        rec("static", "<static>", "health() must return a Health")


def _check_job_backend_id(job: Job, desc: AdapterDescriptor, rec: Recorder, case: str) -> None:
    """The Job `submit()` returns must be stamped with the adapter's OWN descriptor id — this is
    the id the router dispatches follow-up calls (poll/cancel/normalize) against, so a mismatch
    would misattribute the job to (or route it through) the wrong backend."""
    if job.backend_id != desc.id:
        rec("identity", case, f"job.backend_id={job.backend_id!r} != descriptor {desc.id!r}")


def _check_wait_mode(job: Job, desc: AdapterDescriptor, rec: Recorder, case: str) -> None:
    """The Job `submit()` returns must carry a wait_mode the descriptor actually declares in
    `wait_modes` — the driver (`await_result`) branches on `job.wait_mode` to decide whether to
    await a webhook, poll, or treat the job as already terminal (adapter_interface.md §3)."""
    if job.wait_mode not in desc.wait_modes:
        rec("static", case, f"job.wait_mode={job.wait_mode} not in descriptor.wait_modes")


def _check_driver_succeeded(job: Job, rec: Recorder, case: str) -> bool:
    """The driver must reach SUCCEEDED on a ConformanceCase's happy-path input. Returns whether it
    did, so the caller can skip the rest of this case's checks (there's no schema-valid response to
    walk) when a broken submit/poll/resolve_webhook implementation stalls or fails instead."""
    if job.state is not JobState.SUCCEEDED:
        rec("driver", case, f"expected SUCCEEDED after driver, got {job.state} (happy-path)")
        return False
    return True


# --- Ledger T4a: R1 (fresh-instance resume) / R2 (no instance caching) / R3 (JSON round trip) ----
# AC-5/AC-6/AC-7 — internal/design/ledger.md §11-§13. Opt-in via check_adapter_conformance's own
# `adapter_factory` parameter; see that function's docstring for why these are only ever exercised
# when a caller passes one.


def _check_no_cached_client(adapter: Any, rec: Recorder, case: str) -> None:
    """R2 (AC-7): after submit(), no attribute other than the constructor-injected `_client` may
    hold a bound client object. A cached attribute (historically `_active_client`) is itself the
    violation this inspects for — whether or not a later call ever reads it back — because it is
    exactly what leaves a fresh, unrelated instance with nothing to read on a real resume.

    `callable(value)` is excluded: a test that monkeypatches `adapter._get_client = lambda ctx:
    ...` puts a *method*, not a client, in `vars(adapter)` under a name that also happens to end
    in `_client` — a real cached client is always a stateful object, never a bare function."""
    for name, value in vars(adapter).items():
        if (
            name != "_client"
            and name.endswith("_client")
            and value is not None
            and not callable(value)
        ):
            rec("R2", case, f"{name!r} holds a client object after submit() (must not cache)")


def _check_json_serializable(job: Job, rec: Recorder, case: str, *, stage: str) -> None:
    """R3 (AC-6): `json.dumps(job.to_dict())` must succeed at every stage of the Job lifecycle a
    caller can observe it in — not just the terminal one."""
    try:
        json.dumps(job.to_dict())
    except (TypeError, ValueError) as exc:
        rec("R3", case, f"job.to_dict() ({stage}) is not JSON-serializable: {exc}")


def _check_fresh_instance_resume(
    adapter_factory: Callable[[], Any],
    job: Job,
    ctx: RunContext,
    rec: Recorder,
    case: str,
) -> None:
    """R1 (AC-5): round-trip `job` through `to_dict`/`json.dumps`/`json.loads`/`from_dict`, then
    drive THAT copy to completion on a BRAND-NEW instance from `adapter_factory` — sharing no
    Python-level state with the instance that submitted it — proving resume has no instance-
    affinity requirement. A job that is already terminal (INLINE adapters) satisfies this
    trivially, same as it would for a real caller resuming an already-finished job."""
    try:
        resumed = Job.from_dict(json.loads(json.dumps(job.to_dict())))
    except (TypeError, ValueError) as exc:
        rec("R1", case, f"job did not survive the to_dict/json/from_dict round trip: {exc}")
        return
    fresh = adapter_factory()
    clock = FakeClock()
    resumed = run_to_completion(
        fresh, resumed, ctx=ctx, deadline_ms=clock.now_ms() + 60_000, clock=clock
    )
    if resumed.state is not JobState.SUCCEEDED:
        rec(
            "R1",
            case,
            f"fresh-instance resume ended in {resumed.state}, expected SUCCEEDED",
        )


# --- the kit ---------------------------------------------------------------------------


# Phase C default: the kit flipped to all-strict. C1 (no-markup-in-text) and C6 (deliver-or-warn)
# are strict by default now that every built-in adapter is remediated; C7/C3 were always strict;
# C11 stays permanently advisory. Pass strict_checks=frozenset() to opt an (e.g. downstream,
# not-yet-remediated) adapter back to advisory for C1/C6.
_DEFAULT_STRICT: frozenset[str] = frozenset({"C1", "C6"})


def check_adapter_conformance(
    adapter,
    cases: list[ConformanceCase],
    *,
    strict_checks: Iterable[str] = _DEFAULT_STRICT,
    raise_on_violation: bool = True,
    adapter_factory: Callable[[], Any] | None = None,
) -> ConformanceReport:
    """Run the conformance suite and return a ConformanceReport.

    `strict_checks` promotes advisory-by-default invariants (C1, C6) to violations for this run.
    With `raise_on_violation=True` (default) the kit raises ConformanceError if any violation is
    recorded — preserving the historical fail-on-violation behaviour for existing call sites while
    exposing the advisory surface via the returned report.

    `adapter_factory` (Ledger T4a): a zero-arg callable returning a FRESH adapter instance sharing
    no Python-level state with `adapter` (typically the same adapter class constructed with its own
    fresh injected fake client). Passing one turns on R1/R2/R3 per case — see this module's own
    docstring. Omitted (the default), the kit behaves exactly as before this milestone.
    """
    desc = adapter.descriptor
    strict = set(strict_checks)
    report = ConformanceReport(adapter_id=desc.id)

    def rec(check_id: str, case: str, msg: str) -> None:
        bucket = report.violations if _is_violation(check_id, strict) else report.advisories
        bucket.append(Finding(check_id, case, msg))

    # static surface
    try:
        schemas.validate_descriptor(desc.to_schema_dict())
    except Exception as exc:  # noqa: BLE001
        rec("schema", "<static>", f"descriptor fails descriptor schema: {exc}")
    _check_credential_spec(desc, rec)
    _check_capabilities_type(adapter, rec)
    _check_health_type(adapter, rec)
    if not cases:
        rec("static", "<static>", "at least one ConformanceCase is required")

    for case in cases:
        label = case.label or "case"
        job = adapter.submit(case.request, case.ctx)
        _check_job_backend_id(job, desc, rec, label)
        _check_wait_mode(job, desc, rec, label)

        if adapter_factory is not None:
            _check_no_cached_client(adapter, rec, label)
            _check_json_serializable(job, rec, label, stage="post-submit")
            _check_fresh_instance_resume(adapter_factory, job, case.ctx, rec, label)

        clock = FakeClock()
        job = run_to_completion(
            adapter, job, ctx=case.ctx, deadline_ms=clock.now_ms() + 60_000, clock=clock
        )
        if not _check_driver_succeeded(job, rec, label):
            continue

        if adapter_factory is not None:
            _check_json_serializable(job, rec, label, stage="post-drive")

        slim_req = slim_request(case.request)
        resp = adapter.normalize(job, case.ctx, slim_req)
        d = resp.to_schema_dict()
        try:
            schemas.validate_response(d)
        except Exception as exc:  # noqa: BLE001
            rec("schema", label, f"normalized response fails response schema: {exc}")
            continue  # a schema-invalid response can't be meaningfully walked further

        _check_identity(d, desc, rec, label)
        _check_bboxes(d, rec, label)
        _check_confidence_bounds(d, rec, label)
        _check_x_channels(d, desc, case.request, rec, label)
        _check_deliver_or_warn(d, desc, case.request, rec, label)
        _check_markdown_gfm(d, rec, label)
        _check_text_plain(d, rec, label)
        _check_text_blocks_coherence(d, rec, label)

        _check_cost(adapter, job, rec, label)

        if case.deterministic:
            job2 = adapter.submit(case.request, case.ctx)
            job2 = run_to_completion(
                adapter, job2, ctx=case.ctx, deadline_ms=clock.now_ms() + 60_000, clock=clock
            )
            d2 = adapter.normalize(job2, case.ctx, slim_req).to_schema_dict()
            if d2 != d:
                rec("determinism", label, "deterministic adapter not idempotent on resubmit")

    if raise_on_violation:
        report.raise_if_violations()
    return report
