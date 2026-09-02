"""The run header (internal/design/ledger.md §5.3.1/§6, plan §4.2): a small sidecar written once at a
run's first arm and checked on resume — `config_hash`, `plan_hash`, `registry_fingerprint`,
`journal_version`, and the pinned eligible set's descriptor digests. Layout: a JSON file sibling to
the run's `.jsonl` journal, `{ledger_root}/{run_id}.header.json`.

**Which fields hard-refuse a resume, and why not all five.** `config_hash`/`plan_hash` changing
means the compiled plan itself is no longer the one this journal was recorded against — "openreading
.yaml changed since ..." (§10's own transcript) — and `journal_version` changing means this worker
doesn't understand the journal's own record shape (design doc §6: "An unknown journal family
version refuses; it does not best-effort"). `registry_fingerprint`/`pinned_eligible` are stored and
carried through to the resumed `InlineExecutor` (its own per-step `_gate`, armed with
`pinned_eligible=` from THIS header, not a freshly recomputed one) instead — a drift in a PINNED
backend's own descriptor is refused per-step, live, during the walk (AC-14: "resume is the first
scenario that can exercise" that gate for real), not folded into one blanket pre-walk refusal that
would make the gate's own live check unreachable.

**Reconstructing the request `openreading resume <RUN_ID>` needs.** §10 is explicit that resume
takes "no other flags — every option comes from the ledger," which the plan's own literal five-field
list doesn't by itself supply: recompiling the strategy (to re-derive `config_hash`/`plan_hash` for
comparison) needs the same request `compile_strategy` originally saw. This module extends the header
with two additional, Build-judgment fields for exactly that (internal/design/ledger.md §5.3.1's own
future-substrate framing already names both, `document: BlobRef, slim_request`, ahead of this
tranche's need for them): `document` — the original bytes (or, for a URL-sourced document, the URL
string itself — see below), written through the SAME per-run encrypted `BlobStore` every step
payload already uses (never a second plaintext copy; absent only when the original document was a
`file_id` reference, which carries no bytes and no URL to store) — and `slim_request`, a JSON echo
of the rest of the request with `document.bytes_base64`/`document.url` (both live in the blob
instead) and `document.password`/`async.webhook_url` stripped, matching the same never-persisted
guarantee `test_planted_canaries_in_password_and_webhook_url_never_reach_disk` already pins for
every other ledger artifact. A resumed run whose only remaining (non-terminal) steps need a
password-protected document or an async webhook cannot re-dispatch them for real from the header
alone — a disclosed limitation, not a crash.

**`document.url` is a secret-class field too, not merely a reference.** §9.3 names it explicitly,
verbatim, alongside `document.password`/`async_.webhook_url`: "routinely a presigned URL, forwarded
verbatim" — unconditionally, not by size. Phase C round-1 found the first version of
`slim_request_dict` stripped `bytes_base64`/`password`/`webhook_url` but not `url`, so a URL-sourced
document's presigned URL landed verbatim, in plaintext, in this header file — which `retention.py`'s
`reap()` never touches at all, so nothing ever erases it. Fixed the same way `bytes_base64` already
was: routed through the encrypted blob store instead of the plaintext `slim_request` echo, so it
earns the identical shred/erasure guarantee bytes already had, rather than persisting forever.

**Which of the two shapes a `document` blob holds is its own field, not inferred from `media_type`
(Phase C round-2 Finding 7, LOW).** The first version told bytes and URL apart by comparing
`BlobRef.media_type` against the `DOCUMENT_URL_MEDIA_TYPE` sentinel — but a bytes document's own
`media_type` is `req.document.mime_type`, an unvalidated, caller-supplied string nothing rejects, so
a (deliberately adversarial, or extraordinarily unlucky) caller setting `mime_type` to that exact
sentinel string made a real document's bytes get read back as a URL on resume — an uncaught
`UnicodeDecodeError`, or worse, a silent wrong reconstruction, for a value from the very namespace
this code exists to distrust. `RunHeader.document_is_url` is a dedicated field this module alone
sets (never derived from caller input), so the read side never has anything of the caller's own to
collide with. `DOCUMENT_URL_MEDIA_TYPE` still labels the blob's `media_type` for a human reading it
directly (informational only, no longer load-bearing)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openreading.adapters.registry import BUILTIN_ADAPTERS, make_adapter
from openreading.ledger.inline import descriptor_digest
from openreading.ledger.retention import VALID_RUN_ID
from openreading.ledger.sanitizer import Sanitizer
from openreading.ledger.step import BlobRef
from openreading.types.request import OpenReadingRequest

JOURNAL_VERSION = 1

# The subset of RunHeader fields whose mismatch refuses a resume outright (AC-4) — see the module
# docstring for why `registry_fingerprint`/`pinned_eligible` are deliberately excluded.
_HARD_FIELDS = ("config_hash", "plan_hash", "journal_version")

# A human-readable label for a URL-sourced document's header blob (Finding 3, Phase C round 1,
# a reviewer). NOT the bytes/URL discriminator — Phase C round-2 (Finding 7) found that reading
# `BlobRef.media_type` back to tell the two apart could collide with a caller-supplied
# `document.mime_type` (an unvalidated string on the bytes side); `RunHeader.document_is_url` is
# the real discriminator now, a field this module alone ever sets. Kept only as the blob's own
# `media_type` value, for a human inspecting one directly.
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
    # Phase C round-2 (Finding 7): the bytes-vs-URL discriminator for `document`, set only by
    # `_arm_ledger`'s own write side — never derived from `document.media_type`, which (for the
    # bytes case) is `req.document.mime_type`, an unvalidated string a caller controls.
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
    # own CLI argument -- the same H3 traversal class as retention.reap's stamped run_id, just
    # arriving from a different caller. Refuse before the join, not after.
    if not VALID_RUN_ID.fullmatch(run_id):
        raise ValueError(f"malformed run_id {run_id!r}")
    return ledger_root / f"{run_id}.header.json"


def write_header(
    ledger_root: Path, header: RunHeader, *, sanitizer: Sanitizer | None = None
) -> None:
    """Write-once (plan §4.2: "written once per run at first arm") — a no-op if a header already
    exists for this `run_id`, so a second `_arm_ledger` call within the same run's own lifetime can
    never clobber the identity a resume would compare against.

    `sanitizer`, when given (Finding 10a, Phase C round-1): the SAME `Sanitizer` chokepoint
    every journal/blob write already runs through (§9.3) — a backstop, not the primary defense.
    Construction-time exclusion (`slim_request_dict`'s own field-popping, and Finding 3's routing of
    `document.url`/`bytes_base64` through the encrypted blob store instead) is still what keeps a
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
    CONFIG's identity (compliance posture + router config + every participating descriptor folded
    in too, `prune.py`'s own `_compute_config_hash`); `plan_hash` is the PLAN's: the pruned tree
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
    mutated. Introduced (Ledger T4b §4.2, Phase A round 1, alex F1) because `normalize`'s new
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
    `OpenReadingRequest.model_validate(slim_req.model_dump())`). Disclosed, non-blocking (Phase A
    round 2): nothing in this codebase performs that re-validation today, and `slim_request_dict`'s
    own dict-shaped result has carried the identical characteristic since T3 — do not add a
    re-validation step to `slim_req` construction without accounting for this."""
    doc = req.document.model_copy(update={"bytes_base64": None, "password": None, "url": None})
    update: dict[str, Any] = {"document": doc}
    if req.async_ is not None:
        update["async_"] = req.async_.model_copy(update={"webhook_url": None})
    return req.model_copy(update=update)


def slim_request_dict(req: OpenReadingRequest) -> dict[str, Any]:
    """A resume-safe echo of an `OpenReadingRequest` for the header's own `slim_request` field:
    every field EXCEPT `document.bytes_base64`/`document.url` (both kept out of this JSON sidecar —
    see the header's own `document: BlobRef`, the same per-run-encrypted blob store every step
    payload already uses, never a second plaintext copy) and the two fields
    `test_planted_canaries_in_password_and_webhook_url_never_reach_disk` pins as NEVER reaching
    ledger disk in any form: `document.password`, `async.webhook_url`.

    `document.url` (Finding 3, Phase C round-1): §9.3 names it a secret-class field
    unconditionally, "routinely a presigned URL, forwarded verbatim" — the same three-field list
    this module's own `_arm_ledger` caller and `schemas/step.v0.1.json` already quote verbatim
    elsewhere. Popped here exactly like `bytes_base64`, because it now travels the same way
    `bytes_base64` does: through the encrypted blob store (`_arm_ledger`), not this plaintext echo.

    Delegates to `slim_request` (Ledger T4b §4.2) for the actual exclusion — this function is now
    just that object's `to_schema_dict()` projection, so the two never drift apart again."""
    return slim_request(req).to_schema_dict()
