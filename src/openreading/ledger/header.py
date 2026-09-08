"""Persist the run identity required for deterministic strategy resume.

Each run writes one JSON header beside its JSONL journal. The header records the request
projection, plan hashes, journal version, and pinned adapter descriptor digests. Resume refuses
when a hard identity field differs, while the executor checks pinned descriptors per step.

Document bytes and source URLs live in the content-addressed blob store. The plaintext header
omits those values, document passwords, and webhook URLs. ``document_is_url`` distinguishes a URL
blob from document bytes without trusting the caller-controlled media type.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openreading.adapters.registry import BUILTIN_ADAPTERS, make_adapter
from openreading.ledger.inline import descriptor_digest
from openreading.ledger.localfs import VALID_RUN_ID
from openreading.ledger.sanitizer import Sanitizer
from openreading.ledger.step import BlobRef
from openreading.types.request import OpenReadingRequest

JOURNAL_VERSION = 1

# The subset of RunHeader fields whose mismatch refuses a resume outright (AC-4) — see the module
# docstring for why `registry_fingerprint`/`pinned_eligible` are deliberately excluded.
_HARD_FIELDS = ("config_hash", "plan_hash", "journal_version")

# A human-readable label for a URL-sourced document's header blob. NOT the bytes/URL
# discriminator: reading `BlobRef.media_type` back to tell the two apart could collide with a
# caller-supplied `document.mime_type`, an unvalidated string on the bytes side.
# `RunHeader.document_is_url` is the real discriminator, a field this module alone ever sets.
# This constant is kept only as the blob's own `media_type` value, for a human inspecting one
# directly.
DOCUMENT_URL_MEDIA_TYPE = "application/x-openreading-document-url"


class HeaderMismatch(Exception):
    """Raised by a resume-mode arm when any `_HARD_FIELDS` entry disagrees with the run's
    original header. `fields` is `[(field_name, original_value, live_value), ...]`, in `_HARD_
    FIELDS` order — the CLI's `cmd_resume` renders each as its own `[resume] refused: ...` line."""

    def __init__(self, run_id: str, fields: list[tuple[str, Any, Any]]) -> None:
        names = ", ".join(f[0] for f in fields)
        super().__init__(f"run {run_id!r}: header mismatch on {names}")
        self.run_id = run_id
        self.fields = fields


@dataclass(frozen=True)
class RunHeader:
    run_id: str
    config_hash: str
    plan_hash: str
    registry_fingerprint: str
    journal_version: int
    pinned_eligible: dict[str, str] = field(default_factory=dict)
    strategy_name: str = ""
    document: BlobRef | None = None
    # The bytes-vs-URL discriminator for `document`, set only by `_arm_ledger`'s own write side.
    # It is never derived from `document.media_type`, which for the bytes case is
    # `req.document.mime_type`, an unvalidated string a caller controls.
    document_is_url: bool = False
    slim_request: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "config_hash": self.config_hash,
            "plan_hash": self.plan_hash,
            "registry_fingerprint": self.registry_fingerprint,
            "journal_version": self.journal_version,
            "pinned_eligible": dict(self.pinned_eligible),
            "document_is_url": self.document_is_url,
            "strategy_name": self.strategy_name,
            "document": self.document.model_dump(mode="json") if self.document else None,
            "slim_request": self.slim_request,
        }

    @classmethod
    def from_dict(cls, obj: dict[str, Any]) -> RunHeader:
        doc = obj.get("document")
        return cls(
            run_id=obj["run_id"],
            config_hash=obj["config_hash"],
            plan_hash=obj["plan_hash"],
            registry_fingerprint=obj["registry_fingerprint"],
            journal_version=obj["journal_version"],
            pinned_eligible=dict(obj.get("pinned_eligible") or {}),
            strategy_name=obj.get("strategy_name", ""),
            document=BlobRef.model_validate(doc) if doc else None,
            document_is_url=bool(obj.get("document_is_url", False)),
            slim_request=dict(obj.get("slim_request") or {}),
        )


def header_path(ledger_root: Path, run_id: str) -> Path:
    # `resume <RUN_ID>` (api.resume_run -> read_header) hands this straight from the operator's
    # own CLI argument. Refuse malformed path segments before joining them to the ledger root.
    if not VALID_RUN_ID.fullmatch(run_id):
        raise ValueError(f"malformed run_id {run_id!r}")
    return ledger_root / f"{run_id}.header.json"


def write_header(
    ledger_root: Path, header: RunHeader, *, sanitizer: Sanitizer | None = None
) -> None:
    """Write-once (plan §4.2: "written once per run at first arm") — a no-op if a header already
    exists for this `run_id`, so a second `_arm_ledger` call within the same run's own lifetime can
    never clobber the identity a resume would compare against.

    `sanitizer`, when given: the SAME `Sanitizer` chokepoint every journal/blob write already runs
    through (§9.3). It is a backstop, not the primary defense.
    Construction-time exclusion and routing document content through the blob store keep a
    secret-class field out of this JSON in the first place; this catches anything that slips past
    it — a literal resolved credential value that happens to also appear verbatim elsewhere in the
    request body, for instance."""
    p = header_path(ledger_root, header.run_id)
    if p.exists():
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(header.to_dict(), sort_keys=True)
    if sanitizer is not None:
        text = sanitizer.scrub_text(text)
    p.write_text(text, encoding="utf-8")


def read_header(ledger_root: Path, run_id: str) -> RunHeader | None:
    p = header_path(ledger_root, run_id)
    if not p.exists():
        return None
    return RunHeader.from_dict(json.loads(p.read_text(encoding="utf-8")))


def registry_fingerprint() -> str:
    """sha256 over EVERY built-in adapter's own descriptor digest (`inline.descriptor_digest`) —
    the whole registry's identity, distinct from the pinned ELIGIBLE subset (plan §4.2): a backend
    added or removed, or any existing one's descriptor changed, moves this value even for a
    backend outside this run's own eligible set. Computed from the installed adapter catalog
    directly (`adapters.registry.BUILTIN_ADAPTERS`/`make_adapter`), not from a caller-supplied
    `Registry` instance — `_arm_ledger`'s own callers (including tests) pass registries that only
    implement `.get(bid)`, never a whole-catalog enumeration."""
    digests = {bid: descriptor_digest(make_adapter(bid).descriptor) for bid in BUILTIN_ADAPTERS}
    return "sha256:" + hashlib.sha256(json.dumps(digests, sort_keys=True).encode()).hexdigest()


def plan_hash(root: dict[str, Any]) -> str:
    """sha256 of the compiled/pruned strategy tree alone (plan §4.2) — `config_hash` is the
    configuration identity, including router config and participating descriptors. `plan_hash`
    identifies the compiled tree
    that actually got walked."""
    return (
        "sha256:"
        + hashlib.sha256(json.dumps(root, sort_keys=True, default=str).encode()).hexdigest()
    )


def compare_header(old: RunHeader, new: RunHeader) -> list[tuple[str, Any, Any]]:
    """Every `_HARD_FIELDS` entry that differs between the run's original header (`old`) and a
    freshly-recomputed one for the SAME `run_id` (`new`) — empty means resume may proceed."""
    out: list[tuple[str, Any, Any]] = []
    for f in _HARD_FIELDS:
        ov, nv = getattr(old, f), getattr(new, f)
        if ov != nv:
            out.append((f, ov, nv))
    return out


def slim_request(req: OpenReadingRequest) -> OpenReadingRequest:
    """The object-level form of the same exclusion `slim_request_dict` (below) performs: an
    `OpenReadingRequest` with `document.bytes_base64`/`document.password`/`document.url` and
    `async.webhook_url` nulled out via `model_copy(update=...)` — the caller's own `req` is never
    mutated. Introduced (Ledger T4b §4.2) because `normalize`'s new
    `(job, ctx, slim_req)` signature needs an actual request OBJECT: every one of the 15 adapters'
    `normalize` bodies reads its request parameter by attribute (`req.options`,
    `req.document.mime_type`, ...), not by dict key, so the dict `slim_request_dict` returns can't
    serve that need directly. `slim_request_dict` is now a thin wrapper over this function, so both
    call sites share one exclusion list instead of two independently maintained ones.

    `model_copy(update=...)` does not re-run pydantic validators — a documented characteristic of
    `model_copy`, not a bug introduced here. If the original `req.document` was sourced via
    `bytes_base64` or `url` and this function nulls that exact field, the resulting
    `slim_req.document` can silently violate `DocumentInput`'s own "exactly one of
    bytes_base64|url|path|file_id" invariant if anyone ever re-validates it (e.g.
    `OpenReadingRequest.model_validate(slim_req.model_dump())`). Disclosed and non-blocking:
    nothing in this codebase performs that re-validation today, and `slim_request_dict`'s
    own dict-shaped result has carried the identical characteristic since T3 — do not add a
    re-validation step to `slim_req` construction without accounting for this."""
    doc = req.document.model_copy(update={"bytes_base64": None, "password": None, "url": None})
    update: dict[str, Any] = {"document": doc}
    if req.async_ is not None:
        update["async_"] = req.async_.model_copy(update={"webhook_url": None})
    return req.model_copy(update=update)


def slim_request_dict(req: OpenReadingRequest) -> dict[str, Any]:
    """A resume-safe echo of an `OpenReadingRequest` for the header's own `slim_request` field:
    every field except `document.bytes_base64` and `document.url`. Those values use the header's
    `document: BlobRef` instead. It also excludes the two fields
    `test_planted_canaries_in_password_and_webhook_url_never_reach_disk` pins as NEVER reaching
    ledger disk in any form: `document.password`, `async.webhook_url`.

    A document URL can contain a presigned credential. It therefore travels through the blob store
    like document bytes, rather than appearing in the request echo.

    Delegates to `slim_request` (Ledger T4b §4.2) for the actual exclusion — this function is now
    just that object's `to_schema_dict()` projection, so the two never drift apart again."""
    return slim_request(req).to_schema_dict()
