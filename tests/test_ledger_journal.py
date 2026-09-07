"""Ledger T1 — the journal (internal/design/ledger.md, internal/eng-council/plans/sprint26-T1-plan.md).

G3's exit criterion, clause by clause (plan §4): a run leaves a JSONL journal when
`OPENREADING_LEDGER` is set; unset, the run is byte-identical and touches no disk (L1); shredding a
run's key makes its content unrecoverable while the journal stays readable. Plus the §8 consumer
test (`Journal.get` round-trips a leaf's full `[attempted, terminal]` history) and the AC-18 import
scan.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
from pathlib import Path

import pydantic
import pytest

from openreading import api, schemas
from openreading.ledger.inline import InlineExecutor, NullJournal
from openreading.ledger.jsonl import JsonlJournal
from openreading.ledger.localfs import LocalFsBlobStore
from openreading.ledger.sanitizer import Sanitizer
from openreading.ledger.step import BlobRef, StepRef, StepRequest, StepResult
from openreading.router.cache import document_digest
from openreading.router.clock import FakeClock, RealClock
from openreading.strategies import StrategyConfig
from openreading.strategies.engine import _step_id
from openreading.testing.sample_pdf import build_sample_pdf
from openreading.types.errors import RetryableError, ScopeRefused, TerminalError
from openreading.types.request import DocumentInput

pytest.importorskip("fitz", reason="pymupdf not installed")


@pytest.fixture
def pdf_path(tmp_path):
    p = tmp_path / "doc.pdf"
    p.write_bytes(build_sample_pdf())
    return str(p)


def _strategy_config():
    return StrategyConfig.model_validate(
        {"version": 1, "strategies": {"s": {"steps": [{"backend": "pymupdf"}]}}}
    )


def _run_pymupdf_strategy(pdf_path):
    req = api.build_request(pdf_path, "strategy:s")
    return api.run_request(req, strategy_config=_strategy_config())


# ---- G3 clause 1: a run leaves a JSONL journal when OPENREADING_LEDGER is set -----------------


def test_journal_written_when_ledger_armed(pdf_path, tmp_path, monkeypatch):
    ledger_root = tmp_path / "ledger"
    monkeypatch.setenv("OPENREADING_LEDGER", str(ledger_root))

    result = _run_pymupdf_strategy(pdf_path)
    schemas.validate_response(result)

    files = sorted(ledger_root.glob("*.jsonl"))
    assert len(files) == 1
    lines = [json.loads(line) for line in files[0].read_text().splitlines() if line.strip()]
    assert lines, "journal file was created but empty"
    for line in lines:
        schemas.validate_journal_record(line)

    statuses = [line["status"] for line in lines]
    assert statuses == ["attempted", "ok"], (
        "one leaf must record exactly one attempted record followed by one terminal record"
    )


def test_write_order_attempted_before_submit_terminal_after():
    """A spied journal.append shows 'attempted' called before the fake adapter's run() closure
    (standing in for submit()) is entered, and the terminal record called after it returns (plan
    §6, resolving Phase A round-1 F1) — a direct proof of the ordering guarantee, no process-kill
    fault injection needed (internal/design/ledger.md §12 notes that "needs new machinery")."""
    calls: list[str] = []

    class SpyJournal:
        def append(self, result):
            calls.append(result.status)
            return result

        def get(self, ref):
            return []

    class FakeResp:
        def to_schema_dict(self):
            return {"ok": True}

    class FakeRegistry:
        def get(self, bid):
            return None

    ex = InlineExecutor(
        journal=SpyJournal(), blobs=None, registry=FakeRegistry(), clock=RealClock()
    )

    def run():
        calls.append("submit")
        return FakeResp()

    req = StepRequest(
        step_id="s1",
        run_id="r1",
        kind="submit",
        step_path="root",
        step_seq=0,
        attempt=1,
        backend_id="fake",
    )
    asyncio.run(ex.exec(req, run=run))
    assert calls == ["attempted", "submit", "ok"]


def test_exception_from_run_is_journaled_failed_then_reraised():
    calls: list[str] = []

    class SpyJournal:
        def append(self, result):
            calls.append(result.status)

        def get(self, ref):
            return []

    class FakeRegistry:
        def get(self, bid):
            return None

    ex = InlineExecutor(
        journal=SpyJournal(), blobs=None, registry=FakeRegistry(), clock=RealClock()
    )

    def run():
        raise ValueError("boom")

    req = StepRequest(
        step_id="s1",
        run_id="r1",
        kind="submit",
        step_path="root",
        step_seq=0,
        attempt=1,
        backend_id="fake",
    )
    with pytest.raises(ValueError, match="boom"):
        asyncio.run(ex.exec(req, run=run))
    assert calls == ["attempted", "failed"]


# ---- G3 clause 2: unset ⇒ byte-identical, no files created (L1's zero-delta) -------------------


def test_ledger_unarmed_is_byte_identical_and_touches_no_disk(pdf_path, tmp_path, monkeypatch):
    monkeypatch.delenv("OPENREADING_LEDGER", raising=False)
    before = sorted(tmp_path.rglob("*"))

    result = _run_pymupdf_strategy(pdf_path)
    schemas.validate_response(result)

    after = sorted(tmp_path.rglob("*"))
    assert after == before, "an unarmed run must create no files anywhere (L1, AC-1)"


def test_unarmed_inline_executor_journal_is_a_true_no_op():
    ex = InlineExecutor(journal=NullJournal(), blobs=None, registry=None, clock=RealClock())
    req = StepRequest(
        step_id="s1",
        run_id="r1",
        kind="submit",
        step_path="root",
        step_seq=0,
        attempt=1,
        backend_id="fake",
    )

    class FakeResp:
        def to_schema_dict(self):
            return {"ok": True}

    result = asyncio.run(ex.exec(req, run=lambda: FakeResp()))
    assert isinstance(result.payload, FakeResp)  # ExecResult wraps the live payload (Ledger T3)
    assert result.status == "ok"
    assert result.journal_seq is None  # NullJournal.append leaves it None (L1's zero-delta)
    assert result.replayed is False
    assert ex._journal.get(StepRef(run_id="r1", step_path="root", step_seq=0)) == []


# ---- G3 clause 3: shredding a run's key makes content unrecoverable, record stays readable -----


# ---- plan §8: the T3-consumer-test — something reads the journal back --------------------------


def test_journal_get_round_trips_a_leafs_full_attempted_then_terminal_history(tmp_path):
    path = tmp_path / "run1.jsonl"
    writer = JsonlJournal(path)
    blobs = LocalFsBlobStore(tmp_path / "blobs")
    ex = InlineExecutor(journal=writer, blobs=blobs, registry=None, clock=RealClock())

    class FakeResp:
        def to_schema_dict(self):
            return {"n": 1}

    req = StepRequest(
        step_id="s1",
        run_id="run1",
        kind="submit",
        step_path="root",
        step_seq=0,
        attempt=1,
        backend_id="fake",
        idempotency_key="idem1",
    )
    asyncio.run(ex.exec(req, run=lambda: FakeResp()))

    # a SECOND JsonlJournal instance, pointed at the same file — proves the log is readable by
    # something other than the writer that produced it, per §13's "not a write-only log" guard.
    reader = JsonlJournal(path)
    recs = reader.get(StepRef(run_id="run1", step_path="root", step_seq=0))
    assert len(recs) == 2
    assert recs[0].status == "attempted"
    assert recs[1].status == "ok"
    assert recs[0].idempotency_key == "idem1" == recs[1].idempotency_key

    payload = recs[1].payload
    assert isinstance(payload, BlobRef)
    plaintext = blobs.get(payload)
    assert json.loads(plaintext) == {"n": 1}


# ---- AC-18: no open-core module imports the (not-yet-created) enterprise package ---------------


def test_no_open_core_module_imports_enterprise():
    root = Path(__file__).resolve().parents[1] / "src" / "openreading"
    offenders = [
        path.relative_to(root).as_posix()
        for folder in ("router", "strategies", "batch", "comparison", "evals")
        for path in (root / folder).rglob("*.py")
        if "openreading.enterprise" in path.read_text() or "import enterprise" in path.read_text()
    ]
    assert offenders == []


# ---- Sanitizer (§9.3) ----------------------------------------------------------------------


def test_sanitizer_scrubs_a_planted_secret_from_text_and_bytes():
    s = Sanitizer(frozenset({"sk-super-secret-value"}))
    assert "sk-super-secret-value" not in s.scrub_text("token=sk-super-secret-value end")
    assert b"sk-super-secret-value" not in s.scrub_bytes(b'{"key": "sk-super-secret-value"}')


def test_sanitizer_is_a_no_op_with_no_secret_values():
    s = Sanitizer()
    text = "nothing to scrub here"
    assert s.scrub_text(text) is text  # identity — no wasted allocation on the unarmed path


# ---- Retention (§9.4, plan §7) ---------------------------------------------------------------


def test_blobstore_path_refuses_traversal(tmp_path):
    store = LocalFsBlobStore(tmp_path / "blobs")
    with pytest.raises(ValueError):
        store.put("../escape", "sha256:" + "a" * 64, b"data", "application/octet-stream")


def test_blobstore_path_refuses_malformed_digest(tmp_path):
    """run_id is well-formed here, so the digest regex is the thing that must do the refusing.
    Both guards survive the removal of encryption: they keep a crafted id or digest from escaping
    the blob root, which has nothing to do with whether the bytes were encrypted."""
    store = LocalFsBlobStore(tmp_path / "blobs")
    with pytest.raises(ValueError):
        store.put("fine-run-id", "sha256:../../evil", b"x", "application/octet-stream")


# ---- blobs are written as-is (design/ledger-policy-removal.md) --------------------------------


def test_blobstore_put_and_get_round_trip_the_bytes(tmp_path):
    store = LocalFsBlobStore(tmp_path / "blobs")
    digest = "sha256:" + "a" * 64

    ref = store.put("run1", digest, b"the document, in the clear", "text/plain")

    # Readable with `open()`, which is the plainest statement of what the ledger stores. The key
    # that used to protect this sat one directory away from it.
    assert store._path("run1", digest).read_bytes() == b"the document, in the clear"
    assert store.get(ref) == b"the document, in the clear"


def test_header_path_refuses_traversal_run_id(tmp_path):
    """`openreading resume <RUN_ID>` (api.resume_run -> read_header) hands an operator-typed
    run_id straight to header_path; a traversal-shaped one must never reach the join."""
    from openreading.ledger.header import header_path

    with pytest.raises(ValueError):
        header_path(tmp_path, "../../etc/evil")


# ---- golden fixture (§12's convention) --------------------------------------------------------


def test_golden_journal_fixture_validates():
    fixture = json.loads((Path(__file__).parent / "golden" / "journal" / "v0.1.json").read_text())
    for record in fixture:
        schemas.validate_journal_record(record)


# ---- Phase C round-1 fixes (F1-F7, two High + one key-isolation Medium) ------------------


def test_step_id_is_deterministic_sha256_not_random_uuid():
    # F1: step_id = sha256(run_id ‖ step_path ‖ step_seq), deterministic — a re-journaled leaf at
    # the same key must land on the identical step_id, which a random UUID cannot do.
    a = _step_id("run1", "root", 0)
    b = _step_id("run1", "root", 0)
    assert a == b
    assert len(a) == 64 and all(c in "0123456789abcdef" for c in a)  # hex digest, not a dashed UUID
    expected = hashlib.sha256(b"run1\x00root\x000").hexdigest()
    assert a == expected
    assert _step_id("run1", "root", 1) != a  # a different step_seq must not collide


def test_document_digest_is_content_derived_and_portable_across_relocation(tmp_path):
    # F2: content_key must be built from real content bytes, never document_identity's
    # machine-local (realpath, size, mtime_ns) — the same content at a DIFFERENT path (simulating
    # a run relocated to a different worker) must produce the identical digest.
    content = b"identical bytes, two different paths"
    p1 = tmp_path / "a" / "doc.pdf"
    p2 = tmp_path / "b" / "doc.pdf"
    p1.parent.mkdir()
    p2.parent.mkdir()
    p1.write_bytes(content)
    p2.write_bytes(content)

    d1 = document_digest(DocumentInput(path=str(p1)))
    d2 = document_digest(DocumentInput(path=str(p2)))
    assert d1 == d2 == hashlib.sha256(content).digest()

    p3 = tmp_path / "c" / "doc.pdf"
    p3.parent.mkdir()
    p3.write_bytes(b"different content entirely")
    assert document_digest(DocumentInput(path=str(p3))) != d1

    import base64

    b64_doc = DocumentInput(bytes_base64=base64.b64encode(content).decode())
    assert document_digest(b64_doc) == d1  # same content via bytes_base64 matches the path form

    assert document_digest(DocumentInput(url="https://example.com/doc.pdf")) is None


def test_journaled_step_id_and_content_key_use_the_fixed_formulas(pdf_path, tmp_path, monkeypatch):
    # Integration-level regression guard tying the fix to the real walk, not just the unit above.
    ledger_root = tmp_path / "ledger"
    monkeypatch.setenv("OPENREADING_LEDGER", str(ledger_root))
    _run_pymupdf_strategy(pdf_path)

    (journal_file,) = sorted(ledger_root.glob("*.jsonl"))
    lines = [json.loads(line) for line in journal_file.read_text().splitlines() if line.strip()]
    step_id = lines[0]["step_id"]
    assert len(step_id) == 64 and "-" not in step_id  # sha256 hex, not a UUID
    assert lines[0]["step_id"] == lines[1]["step_id"]  # attempted + terminal share one key
    assert lines[0]["content_key"] is not None  # a local path has a real digest to key from


def test_journal_get_tolerates_a_corrupted_trailing_line_and_still_returns_prior_records(tmp_path):
    # F3: a truncated trailing line (the realistic signature of a crash mid-append) must not cost
    # every earlier, valid record in the same file its readability.
    path = tmp_path / "run1.jsonl"
    journal = JsonlJournal(path)
    journal.append(
        StepResult(
            step_id="s1", run_id="r1", step_path="root", step_seq=0, status="attempted", attempt=1
        )
    )
    journal.append(
        StepResult(step_id="s1", run_id="r1", step_path="root", step_seq=0, status="ok", attempt=1)
    )
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"step_id": "s1", "run_id": "r1", "step_path": "roo')  # truncated, no newline

    recs = journal.get(StepRef(run_id="r1", step_path="root", step_seq=0))
    assert [r.status for r in recs] == ["attempted", "ok"]


def test_journal_append_fsyncs(tmp_path, monkeypatch):
    # F4: append() must fsync, not just close() — the OS page cache alone doesn't survive a hard
    # crash/power loss, which is exactly the event AC-13's write-order guarantee exists to survive.
    import os as os_module

    calls = []
    real_fsync = os_module.fsync

    def spy_fsync(fd):
        calls.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(os_module, "fsync", spy_fsync)
    journal = JsonlJournal(tmp_path / "run1.jsonl")
    journal.append(StepResult(step_id="s1", status="attempted", attempt=1))
    assert len(calls) == 1


def test_l2_gate_refuses_a_backend_outside_the_pinned_set_and_a_digest_mismatch():
    # F5: the L2 gate's substantive branches had zero test coverage — drive both refusal codes and
    # the matching success case directly.
    from tests.fakes import make_backend

    fake = make_backend("fake")
    digest = hashlib.sha256(
        json.dumps(fake.descriptor.to_schema_dict(), sort_keys=True, default=str).encode()
    ).hexdigest()

    class Registry:
        def get(self, bid):
            return fake if bid == "fake" else None

    calls: list[tuple[str, str | None]] = []

    class SpyJournal:
        def append(self, result):
            calls.append((result.status, result.error.code if result.error else None))
            return result

        def get(self, ref):
            return []

    def req(backend_id):
        return StepRequest(
            step_id="s1",
            run_id="r1",
            kind="submit",
            step_path="root",
            step_seq=0,
            attempt=1,
            backend_id=backend_id,
        )

    # not_in_pinned_set: backend_id absent from the pinned map entirely. Ledger T3 (round-3 F10):
    # _gate's refusal now raises ScopeRefused after journaling, instead of returning None for
    # the caller to interpret — the record is journaled before the raise, so SpyJournal still
    # captures it.
    ex = InlineExecutor(
        journal=SpyJournal(),
        blobs=None,
        registry=Registry(),
        clock=RealClock(),
        pinned_eligible={"other-backend": digest},
    )
    with pytest.raises(ScopeRefused):
        asyncio.run(
            ex.exec(
                req("fake"), run=lambda: (_ for _ in ()).throw(AssertionError("must not dispatch"))
            )
        )
    assert calls == [("failed", "not_in_pinned_set")]

    # descriptor_digest_mismatch: pinned but the live descriptor no longer matches
    calls.clear()
    ex = InlineExecutor(
        journal=SpyJournal(),
        blobs=None,
        registry=Registry(),
        clock=RealClock(),
        pinned_eligible={"fake": "0" * 64},
    )
    with pytest.raises(ScopeRefused):
        asyncio.run(
            ex.exec(
                req("fake"), run=lambda: (_ for _ in ()).throw(AssertionError("must not dispatch"))
            )
        )
    assert calls == [("failed", "descriptor_digest_mismatch")]

    # match: gate passes through to a real dispatch
    calls.clear()
    ex = InlineExecutor(
        journal=SpyJournal(),
        blobs=None,
        registry=Registry(),
        clock=RealClock(),
        pinned_eligible={"fake": digest},
    )
    result = asyncio.run(ex.exec(req("fake"), run=lambda: "ok"))
    assert [c[0] for c in calls] == ["attempted", "ok"]
    assert result.status == "ok"
    assert result.payload == "ok"


def test_exception_taxonomy_is_classified_not_hardcoded():
    # F6: error.taxonomy must reflect which base exception type actually fired, matching the
    # codebase's own existing _TAXONOMY pattern (router/executor.py) rather than a placeholder.
    class SpyJournal:
        def __init__(self):
            self.results = []

        def append(self, result):
            self.results.append(result)

        def get(self, ref):
            return []

    journal = SpyJournal()
    ex = InlineExecutor(journal=journal, blobs=None, registry=None, clock=RealClock())
    req = StepRequest(
        step_id="s1",
        run_id="r1",
        kind="submit",
        step_path="root",
        step_seq=0,
        attempt=1,
        backend_id="fake",
    )

    def boom():
        raise RetryableError("rate limited", backend_code="429")

    with pytest.raises(RetryableError):
        asyncio.run(ex.exec(req, run=boom))
    assert journal.results[-1].error.taxonomy == "RetryableError"

    journal2 = SpyJournal()
    ex2 = InlineExecutor(journal=journal2, blobs=None, registry=None, clock=RealClock())

    def boom2():
        raise TerminalError("bad input")

    with pytest.raises(TerminalError):
        asyncio.run(ex2.exec(req, run=boom2))
    assert journal2.results[-1].error.taxonomy == "TerminalError"


def test_exception_message_is_recorded_as_the_step_errors_detail():
    # M9 (security review): `StepError.detail` was left unpopulated at this record site — replay
    # had only `code=type(exc).__name__` (a taxonomy CLASS NAME, e.g. "TerminalError") to
    # reconstruct a message from, silently discarding the original text. This is the record-side
    # half; test_ledger_replay.py's own "...reconstructs the original exception message..." proves
    # the round trip through replay.
    class SpyJournal:
        def __init__(self):
            self.results = []

        def append(self, result):
            self.results.append(result)

        def get(self, ref):
            return []

    journal = SpyJournal()
    ex = InlineExecutor(journal=journal, blobs=None, registry=None, clock=RealClock())
    req = StepRequest(
        step_id="s1",
        run_id="r1",
        kind="submit",
        step_path="root",
        step_seq=0,
        attempt=1,
        backend_id="fake",
    )

    def boom():
        raise TerminalError("quota exceeded for tenant-42", backend_code="quota")

    with pytest.raises(TerminalError):
        asyncio.run(ex.exec(req, run=boom))
    assert journal.results[-1].error.detail == "quota exceeded for tenant-42"


def test_a_secret_embedded_in_an_exceptions_message_is_scrubbed_from_the_recorded_detail(tmp_path):
    """M9's own security-critical half: `detail=str(exc)` records the exception's raw text
    verbatim — an adapter that echoes a live secret value back in its error text (a vendor 401
    body quoting the key it rejected, for instance) must never get to plant that secret on disk.
    `_sanitizer_scrub` already walks `StepResult.error.detail` (built for
    `_missing_credentials_gate`'s own `detail=`, Ledger T3 §4.3b) — this proves the SAME
    chokepoint covers the new `except Exception` call site too, with a real on-disk read, not just
    a `Sanitizer`-class unit test."""
    journal = JsonlJournal(tmp_path / "run1.jsonl")
    secret = "sk-live-canary-9f3a"
    ex = InlineExecutor(
        journal=journal,
        blobs=None,
        registry=None,
        clock=RealClock(),
        sanitizer=Sanitizer(frozenset({secret})),
    )
    req = StepRequest(
        step_id="s1",
        run_id="run1",
        kind="submit",
        step_path="root",
        step_seq=0,
        attempt=1,
        backend_id="fake",
    )

    def boom():
        raise TerminalError(f"upstream rejected key {secret}", backend_code="auth_rejected")

    with pytest.raises(TerminalError):
        asyncio.run(ex.exec(req, run=boom))

    on_disk = (tmp_path / "run1.jsonl").read_bytes()
    assert secret.encode() not in on_disk, (
        "a secret echoed in an exception message must never reach the journal"
    )
    assert b"upstream rejected key" in on_disk  # the safe portion of the message still lands


def test_step_result_payload_rejects_a_set_rather_than_silently_coercing_to_a_list():
    # F7: JsonValue's list[Any] arm otherwise accepts any iterable, silently turning a set into a
    # list rather than raising, against the design's own "never Any... must raise" framing.
    with pytest.raises(pydantic.ValidationError):
        StepResult(step_id="s1", status="ok", attempt=1, payload={1, 2, 3})


def test_arm_ledger_populates_the_sanitizer_with_real_resolved_secret_values(tmp_path, monkeypatch):
    # review High 2: a static, empty Sanitizer() never has anything to scrub against — _arm_ledger
    # must feed it every eligible descriptor's actually-resolved secret values.
    from tests.fakes import ScriptedBackend

    monkeypatch.setenv("FAKE_SECRET_KEY", "sk-live-canary-value")
    monkeypatch.setenv("OPENREADING_LEDGER", str(tmp_path / "ledger"))
    backend = ScriptedBackend("fake-hosted", required_env=["FAKE_SECRET_KEY"])

    class Registry:
        def get(self, bid):
            return backend if bid == "fake-hosted" else None

    from openreading.api import _arm_ledger
    from openreading.credentials import EnvCredentialBroker
    from openreading.types.request import OpenReadingRequest

    req = OpenReadingRequest.model_validate(
        {"document": {"path": "/x.pdf"}, "backend": {"id": "strategy:s"}}
    )
    executor = _arm_ledger(
        "run1", req, Registry(), EnvCredentialBroker(), RealClock(), ["fake-hosted"]
    )
    assert isinstance(executor, InlineExecutor)
    assert "sk-live-canary-value" in executor._sanitizer._secret_values
    assert executor._sanitizer.scrub_text("token=sk-live-canary-value end") == "token=*** end"


def test_planted_canaries_in_password_and_webhook_url_never_reach_disk(
    pdf_path, tmp_path, monkeypatch
):
    # Plan §7's own named T1 test (round-2, a non-blocking review note): plant a canary in each
    # excluded field and scan every byte written to disk for it, per AC-9's literal text.
    from openreading.types.request import OpenReadingRequest

    ledger_root = tmp_path / "ledger"
    monkeypatch.setenv("OPENREADING_LEDGER", str(ledger_root))

    password_canary = "canary-password-do-not-persist-9f3a"
    webhook_canary = "https://canary-webhook-do-not-persist-9f3a.example.com/hook"
    req = OpenReadingRequest.model_validate(
        {
            "document": {"path": pdf_path, "password": password_canary},
            "backend": {"id": "strategy:s"},
            "async": {"webhook_url": webhook_canary},
        }
    )
    api.run_request(req, strategy_config=_strategy_config())

    on_disk = b"".join(p.read_bytes() for p in ledger_root.rglob("*") if p.is_file())
    assert password_canary.encode() not in on_disk
    assert webhook_canary.encode() not in on_disk


def test_planted_canary_in_document_url_never_reaches_disk(tmp_path, monkeypatch):
    # Finding 3 (Phase C round-1): the canary test above plants password/webhook_url but
    # never document.url — a THIRD secret-class field per §9.3 ("routinely a presigned URL,
    # forwarded verbatim," unconditionally, not by size), and the one `slim_request_dict` originally
    # missed. `Router.route` classifies `eligible`/`dropped` over the WHOLE registry, not just a
    # strategy's own named steps (`prune.py`'s `plan.eligible_ids`) — narrowing by `mime_type:
    # "text/html"` alone leaves only URL-native backends (docling/azure-document-intelligence/
    # chunkr, all `accepts_url=True`) eligible, so `materialize_document`'s "any eligible backend
    # can't ingest URLs" guard never fires and the URL reaches `_arm_ledger` still a URL — exactly
    # the shape the reviewer's own repro used.
    from openreading.types.request import OpenReadingRequest

    ledger_root = tmp_path / "ledger"
    monkeypatch.setenv("OPENREADING_LEDGER", str(ledger_root))

    url_canary = "https://storage.example.com/doc.html?X-Amz-Signature=SECRET-PRESIGNED-TOKEN-9f3a"
    req = OpenReadingRequest.model_validate(
        {
            "document": {"url": url_canary, "mime_type": "text/html"},
            "backend": {"id": "strategy:s"},
        }
    )
    cfg = StrategyConfig.model_validate(
        {"version": 1, "strategies": {"s": {"steps": [{"backend": "docling"}]}}}
    )
    with contextlib.suppress(Exception):
        # docling has no live endpoint in this environment — irrelevant to what this checks
        api.run_request(req, strategy_config=cfg)

    on_disk = b"".join(p.read_bytes() for p in ledger_root.rglob("*") if p.is_file())
    assert url_canary.encode() not in on_disk


# ---- Ledger T4b §4.2/§6: normalize's slim_req exclusion --------------------------------------
#
# The disk-level canaries above prove the header's own `slim_request` field never persists the
# four secret-class fields. These prove the SIBLING guarantee `normalize`'s new `(job, ctx,
# slim_req)` signature exists for: the in-memory request object every adapter's `normalize()`
# actually receives never carries them either — checked via the request object's own
# identity/content at the call site (`ledger.header.slim_request`'s own result), not merely "no
# adapter currently reads it" (§4.2's own weaker, pre-existing observation).


def test_slim_request_nulls_all_four_secret_class_fields():
    """Direct, object-level proof of the exact exclusion `normalize`'s `slim_req` argument gets:
    plant a canary in each of the four fields and assert `slim_request(req)` nulls exactly those,
    while every other field (here, `backend`) survives untouched. `document.url` can't coexist with
    `document.bytes_base64` (DocumentInput's own "exactly one of" invariant), so it's planted in a
    second, separate request — the same split `test_planted_canary_in_document_url_never_reaches_
    disk` above uses for the identical reason."""
    from openreading.ledger.header import slim_request
    from openreading.types.request import OpenReadingRequest

    bytes_canary = "Y2FuYXJ5LWJ5dGVzLWRvLW5vdC1wZXJzaXN0"  # base64, not a real document
    password_canary = "canary-password-do-not-reach-normalize-9f3a"
    webhook_canary = "https://canary-webhook-do-not-reach-normalize-9f3a.example.com/hook"
    req = OpenReadingRequest.model_validate(
        {
            "document": {
                "bytes_base64": bytes_canary,
                "password": password_canary,
                "mime_type": "application/pdf",
            },
            "backend": {"id": "pymupdf"},
            "async": {"webhook_url": webhook_canary},
        }
    )
    slim = slim_request(req)
    assert slim.document.bytes_base64 is None
    assert slim.document.password is None
    assert slim.async_.webhook_url is None
    # unrelated fields are untouched — this is a targeted null-out, not a blanket strip
    assert slim.document.mime_type == "application/pdf"
    assert slim.backend.id == "pymupdf"
    # the caller's own req is never mutated (model_copy, not an in-place null)
    assert req.document.bytes_base64 == bytes_canary
    assert req.document.password == password_canary
    assert req.async_.webhook_url == webhook_canary

    url_canary = "https://storage.example.com/doc.html?X-Amz-Signature=SECRET-PRESIGNED-9f3a"
    url_req = OpenReadingRequest.model_validate(
        {
            "document": {"url": url_canary, "mime_type": "text/html"},
            "backend": {"id": "docling"},
        }
    )
    assert slim_request(url_req).document.url is None
    assert url_req.document.url == url_canary  # again, the original is untouched


def test_slim_request_with_no_async_block_is_a_noop_on_that_field():
    """Round-2 review's own flagged guard (Phase A round 2): `req.async_` is `None` on a sync-only
    request — `slim_request` must not blow up calling `model_copy` on it."""
    from openreading.ledger.header import slim_request
    from openreading.types.request import OpenReadingRequest

    req = OpenReadingRequest.model_validate(
        {"document": {"path": "/x.pdf"}, "backend": {"id": "pymupdf"}}
    )
    assert req.async_ is None
    slim = slim_request(req)
    assert slim.async_ is None


def test_normalize_actually_receives_the_slimmed_request_at_a_real_call_site(pdf_path, monkeypatch):
    """Not merely "the helper function works in isolation" — a spy on the real pymupdf adapter's
    own `normalize()` proves the strategy engine's call site (`_execute_leaf_sync`) genuinely
    computes and passes `slim_request(req)`, not the original, secret-bearing `req`, for the three
    of the four fields an offline INLINE run can exercise end-to-end (`document.url` needs a
    URL-accepting backend, which needs a live endpoint — covered instead, object-level, by
    `test_slim_request_nulls_all_four_secret_class_fields` above)."""
    from openreading.adapters.pymupdf.adapter import PyMuPDFAdapter
    from openreading.types.request import OpenReadingRequest

    captured: list = []
    real_normalize = PyMuPDFAdapter.normalize

    def _spy(self, job, ctx, slim_req):
        captured.append((slim_req, ctx))
        return real_normalize(self, job, ctx, slim_req)

    monkeypatch.setattr(PyMuPDFAdapter, "normalize", _spy)
    password_canary = "canary-password-do-not-reach-normalize-9f3a"
    webhook_canary = "https://canary-webhook-do-not-reach-normalize-9f3a.example.com/hook"
    req = OpenReadingRequest.model_validate(
        {
            "document": {"path": pdf_path, "password": password_canary},
            "backend": {"id": "strategy:s"},
            "async": {"webhook_url": webhook_canary},
        }
    )
    api.run_request(req, strategy_config=_strategy_config())

    assert len(captured) == 1
    slim_req_seen, ctx_seen = captured[0]
    assert slim_req_seen.document.password is None
    assert slim_req_seen.async_.webhook_url is None
    # Finding 1 (Phase C round-1): the `ctx` threaded to this same real call site is the
    # real, populated RunContext for this run (deadline_ms/idempotency_key filled in by
    # `build_run_context`) — never a fresh, empty `RunContext()`.
    assert ctx_seen is not None
    assert ctx_seen.deadline_ms is not None
    assert ctx_seen.idempotency_key is not None


# ---- the retention clock is wall time, not process uptime (B5) ---------------------------------


def _null_registry():
    class R:
        def get(self, bid):
            return None

    return R()


def _arm(root, run_id, clock):
    from openreading.credentials import EnvCredentialBroker
    from openreading.types.request import OpenReadingRequest

    req = OpenReadingRequest.model_validate(
        {"document": {"path": "/x.pdf"}, "backend": {"id": "strategy:s"}}
    )
    return api._arm_ledger(run_id, req, _null_registry(), EnvCredentialBroker(), clock, [])


def test_journal_timestamps_are_absolute_utc_epochs(tmp_path):
    """`started_epoch_ms`/`ended_epoch_ms` are the journal's only answer to "when did this run".
    Holding a monotonic reading they load as 1970-01-17 in every audit row, silently."""
    root = tmp_path
    journal = JsonlJournal(root / "run1.jsonl")
    blobs = LocalFsBlobStore(root / "blobs")
    wall_start = 1_700_000_000_000.0
    ex = InlineExecutor(
        journal=journal,
        blobs=blobs,
        registry=None,
        clock=FakeClock(start_ms=16 * 24 * 3600_000, wall_start_ms=wall_start),
    )

    class FakeResp:
        def to_schema_dict(self):
            return {"n": 1}

    req = StepRequest(
        step_id="s1",
        run_id="run1",
        kind="submit",
        step_path="root",
        step_seq=0,
        attempt=1,
        backend_id="fake",
    )
    asyncio.run(ex.exec(req, run=lambda: FakeResp()))

    records = [json.loads(line) for line in (root / "run1.jsonl").read_text().splitlines() if line]
    stamps = [r[k] for r in records for k in ("started_epoch_ms", "ended_epoch_ms") if r.get(k)]
    assert stamps, "the dispatch must journal at least one timestamp"
    for value in stamps:
        assert value == wall_start, (
            "a journal timestamp must be the wall clock, not the process's uptime reading"
        )
