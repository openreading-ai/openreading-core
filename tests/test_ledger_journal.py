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
from openreading.ledger.localfs import LocalFsBlobStore, LocalFsKeyStore
from openreading.ledger.ports import PayloadExpired
from openreading.ledger.retention import compute_retention_ceiling_hours, reap, stamp_run
from openreading.ledger.sanitizer import Sanitizer
from openreading.ledger.step import BlobRef, StepRef, StepRequest, StepResult
from openreading.router.cache import document_digest
from openreading.router.clock import FakeClock, RealClock
from openreading.strategies import StrategyConfig
from openreading.strategies.engine import _step_id
from openreading.testing.sample_pdf import build_sample_pdf
from openreading.types.errors import ComplianceRefused, RetryableError, TerminalError
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


def test_shredding_key_makes_payload_unrecoverable_journal_stays_readable(tmp_path):
    root = tmp_path
    journal = JsonlJournal(root / "run1.jsonl")
    keys = LocalFsKeyStore(root / "keys")
    blobs = LocalFsBlobStore(root / "blobs", keys)
    ex = InlineExecutor(journal=journal, blobs=blobs, registry=None, clock=RealClock())

    class FakeResp:
        def to_schema_dict(self):
            return {"secret_bearing": "content", "n": 7}

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

    recs = journal.get(StepRef(run_id="run1", step_path="root", step_seq=0))
    assert [r.status for r in recs] == ["attempted", "ok"]
    ref = recs[1].payload
    assert isinstance(ref, BlobRef)
    assert (
        blobs.get(ref) == json.dumps({"secret_bearing": "content", "n": 7}, sort_keys=True).encode()
    )

    keys.destroy("run1")

    with pytest.raises(PayloadExpired):
        blobs.get(ref)

    # (a) the journal itself is still fully readable
    recs_after = journal.get(StepRef(run_id="run1", step_path="root", step_seq=0))
    assert [r.status for r in recs_after] == ["attempted", "ok"]

    # (c) the plaintext is genuinely gone, not merely flagged: the key file backing it no longer
    # exists at all, so there is no key left anywhere to decrypt the still-present ciphertext with.
    assert not (root / "keys" / "run1.key").exists()
    assert (root / "blobs" / "run1").exists()  # ciphertext itself is untouched by a shred


# ---- plan §8: the T3-consumer-test — something reads the journal back --------------------------


def test_journal_get_round_trips_a_leafs_full_attempted_then_terminal_history(tmp_path):
    path = tmp_path / "run1.jsonl"
    writer = JsonlJournal(path)
    keys = LocalFsKeyStore(tmp_path / "keys")
    blobs = LocalFsBlobStore(tmp_path / "blobs", keys)
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


def test_retention_ceiling_is_min_over_hosted_excluding_local_and_flags_unverified():
    from openreading.types.descriptor import ComplianceProfile

    class D:
        def __init__(self, **kw):
            self.compliance = ComplianceProfile(**kw)

    hosted_a = D(max_retention_hours=48, runs_fully_local=False)
    hosted_b = D(max_retention_hours=12, runs_fully_local=False)
    local = D(max_retention_hours=0, runs_fully_local=True)  # excluded — vendor holds nothing
    ceiling, unverified, zdr = compute_retention_ceiling_hours(
        [hosted_a, hosted_b, local], default_hours=999
    )
    assert ceiling == 12
    assert unverified is False
    assert zdr is False


def test_retention_ceiling_falls_back_to_default_and_flags_unverified_when_hosted_hours_unknown():
    from openreading.types.descriptor import ComplianceProfile

    class D:
        def __init__(self, **kw):
            self.compliance = ComplianceProfile(**kw)

    unverified_hosted = D(max_retention_hours=None, runs_fully_local=False)
    ceiling, unverified, zdr = compute_retention_ceiling_hours(
        [unverified_hosted], default_hours=24
    )
    assert ceiling == 24
    assert unverified is True
    assert zdr is False


def test_retention_ceiling_zdr_flag_propagates():
    from openreading.types.descriptor import ComplianceProfile

    class D:
        def __init__(self, **kw):
            self.compliance = ComplianceProfile(**kw)

    zdr_hosted = D(max_retention_hours=48, runs_fully_local=False, zdr_flag="vendor-zdr")
    _ceiling, _unverified, zdr = compute_retention_ceiling_hours([zdr_hosted])
    assert zdr is True


def test_reap_destroys_keys_and_blobs_for_stamped_runs_past_their_ceiling_only():
    root = Path
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        keys = LocalFsKeyStore(root / "keys")
        blobs = LocalFsBlobStore(root / "blobs", keys)
        blobs.put("expired-run", "sha256:" + "0" * 64, b"data", "application/json")
        blobs.put("fresh-run", "sha256:" + "1" * 64, b"data", "application/json")
        stamp_run(root, "expired-run", expires_epoch_ms=1000, zdr=False)
        stamp_run(root, "fresh-run", expires_epoch_ms=99_999_999_999, zdr=False)

        reaped = reap(root, keys, root / "blobs", now_epoch_ms=50_000)

        assert reaped == ["expired-run"]
        assert not (root / "keys" / "expired-run.key").exists()
        assert (root / "keys" / "fresh-run.key").exists()
        assert not (root / "blobs" / "expired-run").exists()
        assert (root / "blobs" / "fresh-run").exists()


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
    # _gate's refusal now raises ComplianceRefused after journaling, instead of returning None for
    # the caller to interpret — the record is journaled before the raise, so SpyJournal still
    # captures it.
    ex = InlineExecutor(
        journal=SpyJournal(),
        blobs=None,
        registry=Registry(),
        clock=RealClock(),
        pinned_eligible={"other-backend": digest},
    )
    with pytest.raises(ComplianceRefused):
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
    with pytest.raises(ComplianceRefused):
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


def test_step_result_payload_rejects_a_set_rather_than_silently_coercing_to_a_list():
    # F7: JsonValue's list[Any] arm otherwise accepts any iterable, silently turning a set into a
    # list rather than raising, against the design's own "never Any... must raise" framing.
    with pytest.raises(pydantic.ValidationError):
        StepResult(step_id="s1", status="ok", attempt=1, payload={1, 2, 3})


def test_zdr_flagged_backend_suppresses_the_blob_write_entirely():
    # review High 1: (rescoped Phase C round-2, Findings 8 and 6): a ZDR-flagged
    # backend's OWN step must retain zero content — InlineExecutor must never call blobs.put for a
    # step whose OWN `backend_id` resolves to a ZDR-flagged descriptor. The whole-run `zdr=`
    # boolean this test used to arm directly is gone; the gate is now a per-step registry lookup
    # off `req.backend_id`, so this test arms a real registry with a ZDR-flagged "reducto" instead.
    from tests.fakes import ScriptedBackend, scripted_registry

    class FailingBlobs:
        def put(self, *a, **kw):
            raise AssertionError("blobs.put must never be called on a ZDR step")

        def get(self, ref):
            raise AssertionError("not exercised")

    class FakeResp:
        def to_schema_dict(self):
            return {"sensitive": "phi content"}

    class SpyJournal:
        def __init__(self):
            self.results = []

        def append(self, result):
            self.results.append(result)
            return result

        def get(self, ref):
            return []

    reducto = ScriptedBackend("reducto", local=False)
    reducto.descriptor = reducto.descriptor.model_copy(
        update={
            "compliance": reducto.descriptor.compliance.model_copy(
                update={"zdr_flag": "zdr_tier_gated"}
            )
        }
    )
    registry = scripted_registry(reducto)

    journal = SpyJournal()
    ex = InlineExecutor(journal=journal, blobs=FailingBlobs(), registry=registry, clock=RealClock())
    req = StepRequest(
        step_id="s1",
        run_id="r1",
        kind="submit",
        step_path="root",
        step_seq=0,
        attempt=1,
        backend_id="reducto",
    )
    result = asyncio.run(ex.exec(req, run=lambda: FakeResp()))
    assert result is not None
    assert result.status == "ok"
    terminal = journal.results[-1]
    assert terminal.status == "ok"
    assert terminal.payload is None  # journal metadata only — no content retained


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


def test_shredded_run_leaves_no_key_byte_anywhere_under_journal_or_blob_directories(tmp_path):
    # a reviewer Medium: the design's own named T1 acceptance test for §9.4 — scan every byte under the
    # journal/blob directories for the key material, not just check the key file's own existence.
    root = tmp_path
    journal = JsonlJournal(root / "run1.jsonl")
    keys = LocalFsKeyStore(root / "keys")
    blobs = LocalFsBlobStore(root / "blobs", keys)
    ex = InlineExecutor(journal=journal, blobs=blobs, registry=None, clock=RealClock())

    class FakeResp:
        def to_schema_dict(self):
            return {"n": 1, "text": "some response content"}

    key_bytes = keys.get_or_create("run1")  # mint the key before dispatch, as put() would anyway
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

    for path in [*root.glob("*.jsonl"), *root.glob("blobs/**/*.bin")]:
        data = path.read_bytes()
        assert key_bytes not in data, f"key material leaked into {path}"


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


def test_retention_stamp_is_an_absolute_utc_epoch(tmp_path, monkeypatch):
    """The stamp outlives the process that wrote it, so it may only hold a clock whose zero point
    outlives the process too. A monotonic reading is uptime: written to disk it reads as 1970 and
    is meaningless to the next process that compares against it."""
    import time

    root = tmp_path / "ledger"
    monkeypatch.setenv("OPENREADING_LEDGER", str(root))
    monkeypatch.delenv("OPENREADING_LEDGER_RETENTION_HOURS", raising=False)
    _arm(root, "run-A", RealClock())

    stamp = json.loads((root / "retention" / "run-A.json").read_text())
    now_wall = time.time() * 1000.0
    assert now_wall < stamp["expires_epoch_ms"] <= now_wall + 24 * 3600_000 + 60_000, (
        "expires_epoch_ms must be `wall now + the retention window`, per .env.example's own "
        "'absolute UTC epoch'"
    )


def test_a_run_past_its_retention_window_is_reaped_after_a_reboot(tmp_path, monkeypatch):
    """`time.monotonic()`'s reference point is the boot, and Python leaves it formally undefined.
    Stamped with a monotonic reading, a run armed on a machine 16 days into its uptime records an
    expiry ~17 days out; after a reboot the reaper's own `now` is minutes, so the comparison says
    "not yet" and the key survives for as long as the next boot session takes to reach 17 days of
    uptime. That is PHI held past a retention window an operator attested to."""
    root = tmp_path / "ledger"
    monkeypatch.setenv("OPENREADING_LEDGER", str(root))
    monkeypatch.setenv("OPENREADING_LEDGER_RETENTION_HOURS", "24")

    day_ms = 24 * 3600_000
    wall_at_arm = 1_700_000_000_000.0

    # A machine 16 days into its uptime arms a run and mints its content key.
    _arm(root, "phi-run", FakeClock(start_ms=16 * day_ms, wall_start_ms=wall_at_arm))
    LocalFsKeyStore(root / "keys").get_or_create("phi-run")
    assert (root / "keys" / "phi-run.key").exists()

    # --- reboot --- uptime restarts near zero; 25 wall-clock hours have passed, so the 24-hour
    # window is over. The next armed run runs the sweep (there is no cron; see ledger/README.md).
    _arm(root, "next-run", FakeClock(start_ms=120_000, wall_start_ms=wall_at_arm + 25 * 3600_000))

    assert not (root / "keys" / "phi-run.key").exists(), (
        "an expired run must be reaped after a reboot — the stamp and the reaper's `now` must "
        "share a clock base whose zero point survives one"
    )
    assert not (root / "retention" / "phi-run.json").exists()


def test_journal_timestamps_are_absolute_utc_epochs(tmp_path):
    """`started_epoch_ms`/`ended_epoch_ms` are the journal's only answer to "when did this run".
    Holding a monotonic reading they load as 1970-01-17 in every audit row, silently."""
    root = tmp_path
    journal = JsonlJournal(root / "run1.jsonl")
    keys = LocalFsKeyStore(root / "keys")
    blobs = LocalFsBlobStore(root / "blobs", keys)
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
