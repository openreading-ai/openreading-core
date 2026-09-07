"""Phase D (Manifest v0.6 §7) — anthropic-claude native Message Batches, replayed via an injected
fake client (NO API calls; the adapter's house pattern). Pins the documented contract shapes
(POST /v1/messages/batches with custom_id+params; poll processing_status; results JSONL with
result.type succeeded/errored) and end-to-end dispatch through run_batch. Live verification is
gated (no ANTHROPIC_API_KEY assumed)."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from openreading import run_batch, schemas
from openreading.adapters.anthropic_claude import AnthropicClaudeAdapter
from openreading.adapters.registry import BUILTIN_ADAPTERS
from openreading.router.clock import FakeClock
from openreading.router.driver import run_to_completion
from openreading.testing.sample_pdf import build_sample_pdf
from openreading.types import JobState, NormalizedResponse
from openreading.types.batch import BatchItemError
from openreading.types.request import OpenReadingRequest
from openreading.types.runtime import ResolvedCredentials, RunContext

FIX = Path(__file__).parent / "fixtures" / "anthropic-claude"
PDF_B64 = base64.b64encode(b"%PDF-1.7 fake").decode()


def _parse_message() -> dict:
    return json.loads((FIX / "parse.json").read_text())


def _extract_message() -> dict:
    return json.loads((FIX / "extract.json").read_text())


def _req(**over) -> OpenReadingRequest:
    body = {
        "document": {"bytes_base64": PDF_B64, "mime_type": "application/pdf"},
        "backend": {"id": "anthropic-claude"},
    }
    body.update(over)
    return OpenReadingRequest.model_validate(body)


class FakeBatchClient:
    """Replays the documented Message Batches contract: create → in_progress, poll → ended, then
    the results JSONL (a mix of succeeded + errored, keyed by custom_id)."""

    def __init__(self, results: list[dict]):
        self._results = results
        self._polls = 0
        self.created: list[dict] | None = None

    def create(self, **kw):  # single-request path, unused here
        return _parse_message()

    def create_batch(self, requests: list[dict]) -> dict:
        self.created = requests
        return {
            "id": "msgbatch_test01",
            "type": "message_batch",
            "processing_status": "in_progress",
        }

    def get_batch(self, batch_id: str) -> dict:
        self._polls += 1
        ended = self._polls >= 2  # first poll in_progress, then ended → exercises the poll loop
        return {
            "id": batch_id,
            "processing_status": "ended" if ended else "in_progress",
            "results_url": "https://api.anthropic.com/v1/messages/batches/x/results"
            if ended
            else None,
        }

    def get_results(self, batch_id: str) -> list[dict]:
        return self._results


def _results_ok_and_error() -> list[dict]:
    return [
        {"custom_id": "item-0", "result": {"type": "succeeded", "message": _parse_message()}},
        {
            "custom_id": "item-1",
            "result": {
                "type": "errored",
                "error": {"type": "invalid_request", "message": "bad params"},
            },
        },
    ]


def test_submit_many_builds_documented_request_shape():
    adapter = AnthropicClaudeAdapter(client=FakeBatchClient(_results_ok_and_error()))
    job = adapter.submit_many([_req(), _req()], RunContext())
    assert job.backend_job_id == "msgbatch_test01"
    assert job.poll_handle and job.poll_handle["batch_id"] == "msgbatch_test01"
    created = adapter._client.created  # type: ignore[attr-defined]
    assert [r["custom_id"] for r in created] == ["item-0", "item-1"]  # custom_id per request
    assert "messages" in created[0]["params"] and "model" in created[0]["params"]  # params shape


def test_poll_then_normalize_many_maps_succeeded_and_errored():
    adapter = AnthropicClaudeAdapter(client=FakeBatchClient(_results_ok_and_error()))
    reqs = [_req(), _req()]
    job = adapter.submit_many(reqs, RunContext())
    clock = FakeClock()
    job = run_to_completion(
        adapter, job, ctx=RunContext(), deadline_ms=clock.now_ms() + 120_000, clock=clock
    )
    assert job.state is JobState.SUCCEEDED  # poll advanced in_progress → ended
    out = adapter.normalize_many(job, reqs)
    assert len(out) == 2
    assert (
        isinstance(out[0], NormalizedResponse) and out[0].document.markdown
    )  # succeeded → response
    assert (
        isinstance(out[1], BatchItemError) and out[1].code == "errored"
    )  # errored → per-item error
    # BL-100: normalize_many must meter each succeeded item off its OWN synthetic per-item job
    # (the batch-level `job.raw.payload` has no top-level "usage" key at all — only the reconstructed
    # per-item message does), matching what report_cost() computes for the identical fixture via the
    # single-document path (test_cost_from_token_usage).
    assert out[0].usage is not None
    assert out[0].usage.input_tokens == 2400
    assert out[0].usage.output_tokens == 180
    assert out[0].usage.cost_usd == pytest.approx(2400 / 1e6 * 5.0 + 180 / 1e6 * 25.0)
    assert out[0].usage.cost_basis == "estimated"


# --- Ledger T4b F4 (Phase C round-2): native-batch page counts must be exact, not heuristic --


def test_normalize_many_exact_page_count_from_real_pdf_bytes_not_citation_heuristic():
    # Round 1's F1 fix moved the exact byte-derived page-count computation into submit() (the
    # single-document path) — but submit_many()/normalize_many() are a separate code path for
    # native Message Batches and never call submit(), so every batch item's synth.raw.payload
    # never got a "_pdf_page_count" key and _page_count() silently fell back to the distinct-
    # cited-pages heuristic for every native-batch item: the same silent-degradation shape round
    # 1's F1 was scored High for, just on the batch path. the review's own repro: a real 2-page PDF,
    # native-batch parse mode reports 1 (only page 1 is cited in the fixture), extract mode
    # reports None (no citations at all to count from).
    doc_bytes = build_sample_pdf()  # a genuine, byte-parseable 2-page PDF (not the fake PDF_B64)
    b64 = base64.b64encode(doc_bytes).decode()
    parse_req = _req(document={"bytes_base64": b64, "mime_type": "application/pdf"})
    extract_req = _req(
        document={"bytes_base64": b64, "mime_type": "application/pdf"},
        extraction_schema={"json_schema": {"type": "object", "properties": {}}},
    )
    reqs = [parse_req, extract_req]
    results = [
        {"custom_id": "item-0", "result": {"type": "succeeded", "message": _parse_message()}},
        {"custom_id": "item-1", "result": {"type": "succeeded", "message": _extract_message()}},
    ]
    adapter = AnthropicClaudeAdapter(client=FakeBatchClient(results))
    job = adapter.submit_many(reqs, RunContext())
    clock = FakeClock()
    job = run_to_completion(
        adapter, job, ctx=RunContext(), deadline_ms=clock.now_ms() + 120_000, clock=clock
    )
    out = adapter.normalize_many(job, reqs)
    # parse mode: the citation heuristic on parse.json's fixture would undercount to 1 (only page
    # 1 is cited) — the exact, byte-derived count (2) must win instead.
    assert isinstance(out[0], NormalizedResponse)
    assert out[0].document.page_count == 2
    # extract mode: no citations at all → the heuristic returns None — the exact count fills it.
    assert isinstance(out[1], NormalizedResponse)
    assert out[1].document.page_count == 2


def test_normalize_many_forwards_credentials_so_cost_report_warning_is_redacted():
    # BL-108: apply_cost_report's own redaction (BL-93) only fires when it is HANDED credentials —
    # normalize_many's apply_cost_report call (BL-100, above) previously passed none at all, so a
    # report_cost() failure on the native-batch path leaked a resolved secret unredacted, unlike
    # every other apply_cost_report call site. Mirrors tests/test_server.py's own
    # test_submit_job_cost_report_warning_redacts_a_secret pattern one level down: the adapter
    # method itself, not the HTTP surface built on top of it.
    secret = "sk-ant-native-leak-0003-must-never-appear"  # noqa: S105 - test fixture, not a real key

    class _LeakyCostAdapter(AnthropicClaudeAdapter):
        def report_cost(self, job):
            raise RuntimeError(f"billing endpoint rejected key {secret}")

    adapter = _LeakyCostAdapter(client=FakeBatchClient(_results_ok_and_error()))
    reqs = [_req(), _req()]
    job = adapter.submit_many(reqs, RunContext())
    clock = FakeClock()
    job = run_to_completion(
        adapter, job, ctx=RunContext(), deadline_ms=clock.now_ms() + 120_000, clock=clock
    )

    # the same ResolvedCredentials shape build_run_context would have produced from a resolved
    # ANTHROPIC_API_KEY — _run_native is expected to pass ctx.credentials through unchanged.
    creds = ResolvedCredentials(values={"api_key": secret}, source="env")
    out = adapter.normalize_many(job, reqs, credentials=creds)
    assert isinstance(out[0], NormalizedResponse)
    # normalize() itself already adds its own page_attribution_unavailable warning (Claude gives
    # no page geometry) — apply_cost_report's warning is appended alongside it, not necessarily
    # first, so find it by code rather than assuming position.
    warnings = out[0].warnings or []
    cost_warning = next((w for w in warnings if w.code == "cost_unavailable"), None)
    assert cost_warning is not None
    assert secret not in cost_warning.message
    assert "***" in cost_warning.message


# --- BL-113: normalize_many's own succeeded-branch mapping step must be crash-isolated too ------


def test_normalize_many_isolates_a_per_item_mapping_crash_and_redacts_it():
    # BL-106 (sprint 17) wrapped _run_native's submit_many→normalize_many block in a crash guard,
    # but that only protects a crash that ESCAPES normalize_many. normalize_many's own
    # succeeded-branch mapping step — self.normalize(synth, ctx, req) plus the apply_cost_report(...)
    # call right after it — had no try/except of its own: a bug there on item k unwound straight
    # past every out.append() for items 0..k-1, discarding their already-mapped results too, not
    # just the offending item's, because `out` is a local list with no outer recovery once the
    # exception has unwound past it. Pinned directly against the real adapter (not a fake, unlike
    # test_run_native_crash_redacts_a_secret_bearing_credential in tests/test_batch_native.py,
    # which exercises a WHOLE-METHOD crash at the _run_native layer and never lets any item
    # succeed first): item 0 succeeds, item 1's own mapping step crashes embedding a secret in the
    # message (mirroring test_normalize_many_forwards_credentials_so_cost_report_warning_is_
    # redacted's _LeakyCostAdapter pattern one method over), item 2 succeeds — all three must
    # survive, in order, and item 1's message must come back scrubbed.
    secret = "sk-ant-native-mapcrash-0005-must-never-appear"  # noqa: S105 - test fixture, not a real key

    class _CrashOnSecondItem(AnthropicClaudeAdapter):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self._normalize_calls = 0

        def normalize(self, job, ctx, req):
            self._normalize_calls += 1
            if self._normalize_calls == 2:  # only item 1's own mapping step misbehaves
                raise ValueError(f"malformed per-item mapping, saw key={secret}")
            return super().normalize(job, ctx, req)

    results = [
        {"custom_id": "item-0", "result": {"type": "succeeded", "message": _parse_message()}},
        {"custom_id": "item-1", "result": {"type": "succeeded", "message": _parse_message()}},
        {"custom_id": "item-2", "result": {"type": "succeeded", "message": _parse_message()}},
    ]
    adapter = _CrashOnSecondItem(client=FakeBatchClient(results))
    reqs = [_req(), _req(), _req()]
    job = adapter.submit_many(reqs, RunContext())
    clock = FakeClock()
    job = run_to_completion(
        adapter, job, ctx=RunContext(), deadline_ms=clock.now_ms() + 120_000, clock=clock
    )

    # the same ResolvedCredentials shape build_run_context/_run_native would hand normalize_many.
    creds = ResolvedCredentials(values={"api_key": secret}, source="env")
    out = adapter.normalize_many(job, reqs, credentials=creds)

    assert len(out) == 3
    # item 0's already-mapped result survives item 1's later crash — the bug this test closes.
    assert isinstance(out[0], NormalizedResponse) and out[0].document.markdown
    # item 1: our own mapping code crashed → isolated as a BatchItemError, never a raise that
    # would take out the whole batch.
    assert isinstance(out[1], BatchItemError)
    assert secret not in out[1].message
    assert "***" in out[1].message
    # item 2: the loop keeps going past the crash rather than stopping early.
    assert isinstance(out[2], NormalizedResponse) and out[2].document.markdown


def test_native_batch_end_to_end_via_run_batch(tmp_path, monkeypatch):
    results = [
        {"custom_id": "item-0", "result": {"type": "succeeded", "message": _parse_message()}},
        {"custom_id": "item-1", "result": {"type": "succeeded", "message": _parse_message()}},
    ]
    monkeypatch.setitem(
        BUILTIN_ADAPTERS,
        "anthropic-claude",
        lambda: AnthropicClaudeAdapter(client=FakeBatchClient(results)),
    )
    d = tmp_path / "c"
    d.mkdir()
    for n in ("a.pdf", "b.pdf"):
        (d / n).write_bytes(build_sample_pdf())
    env = run_batch([str(d)], backend="anthropic-claude")
    schemas.validate_batch_result(env)
    assert env["summary"]["succeeded"] == 2
    assert all(i["transport"] == "native" for i in env["items"])  # dispatched to the native path

    # BL-100: every succeeded native-batch item is metered — not silently absent, and not a
    # single batch-wide report_cost(job) call that would price every item at cost_usd=0.0 (the
    # batch job's raw payload has no top-level "usage" key).
    expected_item_cost = 2400 / 1e6 * 5.0 + 180 / 1e6 * 25.0
    for item in env["items"]:
        assert item["response"]["usage"]["cost_usd"] == pytest.approx(expected_item_cost)
    assert env["summary"]["cost_usd"] == pytest.approx(expected_item_cost * 2)
    assert env["summary"]["cost_bases"] == ["estimated"]


# --- BL-98: the native path compliance-gates the backend it drives, same as platform fan-out ----


def _one_doc_corpus(tmp_path) -> Path:
    d = tmp_path / "c"
    d.mkdir()
    (d / "a.pdf").write_bytes(build_sample_pdf())
    return d
