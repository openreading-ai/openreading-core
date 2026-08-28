"""Phase B (Manifest v0.6) — the platform runner (batch/runner.py), invariants M6–M9.

Pure orchestration over a `run_one(source, idem_key) -> response.v0.3 dict` seam (production wires
it to api.run; tests inject fakes — cleaner than a full ScriptedBackend and exercises exactly the
runner's job): per-item isolation, honest aggregation, the status truth table, idempotency-key
derivation, the concurrent pool preserving input order, and the skip warning."""

from __future__ import annotations

import pytest

from openreading import schemas
from openreading.batch.runner import (
    JobsLimitError,
    assemble_result,
    batch_state,
    bound_jobs,
    item_idempotency_key,
    run_batch,
)
from openreading.batch.sources import ResolvedSource
from openreading.types.batch import MAX_BATCH_JOBS, SourceRef


def _src(name, fmt="pdf", sha="0123456789abcdef0000", skip=None, url=None):
    return ResolvedSource(
        ref=SourceRef(
            filename=name,
            format=fmt,
            path=None if url else f"/x/{name}",
            url=url,
            relpath=None if url else name,
            sha256=None if (skip or url) else sha,
        ),
        skip_reason=skip,
    )


def _ok(backend="pymupdf", cost=None, basis=None, pages=None):
    doc = {
        "schema_version": "0.3",
        "status": {"state": "succeeded"},
        "backend": {"id": backend, "type": "oss_library"},
        "document": {"text": "x"},
    }
    usage = {
        k: v
        for k, v in (("cost_usd", cost), ("cost_basis", basis), ("pages_processed", pages))
        if v is not None
    }
    if usage:
        doc["usage"] = usage
    return doc


# --- pure helpers -----------------------------------------------------------------------


def test_item_idempotency_key():
    assert item_idempotency_key(None, "deadbeef") is None  # no base → no fabricated key
    assert item_idempotency_key("K", "0123456789abcdef0000") == "K:0123456789abcdef"  # sha[:16]
    assert item_idempotency_key("K", None) is None  # no sha (URL) → no derived key


def test_batch_state_truth_table():
    assert batch_state(2, 0) == "succeeded"
    assert batch_state(1, 1) == "partial"
    assert batch_state(0, 1) == "failed"  # all failed
    assert batch_state(0, 0) == "failed"  # all skipped / empty → nothing produced


# --- M6 per-item isolation --------------------------------------------------------------


def test_one_failure_does_not_abort_the_batch():
    srcs = [_src("a.pdf"), _src("b.pdf"), _src("c.pdf")]

    def run_one(src, idem):
        if src.ref.filename == "b.pdf":
            raise RuntimeError("boom")
        return _ok()

    res = run_batch(srcs, run_one=run_one)
    assert [i.state for i in res.items] == ["succeeded", "failed", "succeeded"]
    assert res.items[1].error is not None and res.items[1].error.message == "boom"
    assert res.status.state == "partial"
    assert (res.summary.total, res.summary.succeeded, res.summary.failed) == (3, 2, 0 + 1)


def test_failure_error_code_prefers_backend_code():
    from openreading.types.errors import TerminalError

    srcs = [_src("a.pdf")]

    def run_one(src, idem):
        raise TerminalError("nope", backend_code="doc_too_large")

    res = run_batch(srcs, run_one=run_one)
    assert res.items[0].state == "failed" and res.items[0].error.code == "doc_too_large"


# --- skips (M3 warning surfaced here) ---------------------------------------------------


def test_skipped_items_are_not_run_and_produce_a_warning():
    srcs = [_src("a.pdf"), _src("x.docx", fmt="docx", skip="unsupported_format")]
    calls = []

    def run_one(src, idem):
        calls.append(src.ref.filename)
        return _ok()

    res = run_batch(srcs, run_one=run_one)
    assert calls == ["a.pdf"]  # the skipped file is never executed
    assert res.items[1].state == "skipped" and res.items[1].skip_reason == "unsupported_format"
    assert res.summary.skipped == 1
    assert any(w.code == "items_skipped" for w in (res.warnings or []))


def test_all_skipped_is_a_failed_batch():
    srcs = [_src("x.docx", fmt="docx", skip="unsupported_format")]
    res = run_batch(srcs, run_one=lambda s, i: _ok())
    assert res.status.state == "failed" and res.summary.succeeded == 0


# --- BL-147: empty batch + skip progress -------------------------------------------------


def test_assemble_result_on_no_items_attaches_empty_batch_warning():
    # A source list that resolved to zero documents (empty/hidden-only dir, zero-match expansion,
    # an empty `documents: []` request body) must never finish with no warnings key at all — the
    # only prior signal was `summary.total == 0`, indistinguishable from a hang or a crash.
    res = assemble_result([])
    assert res.warnings is not None
    assert [w.code for w in res.warnings] == ["empty_batch"]
    assert res.warnings[0].message == "no source resolved to a document to process"
    assert res.status.state == "failed"  # unchanged: 0 succeeded ⇒ failed (no exit-code change)


def test_run_batch_with_no_sources_produces_empty_batch_warning():
    res = run_batch([], run_one=lambda s, i: _ok())
    assert res.summary.total == 0
    assert res.warnings is not None and res.warnings[0].code == "empty_batch"


def test_non_empty_batch_never_gets_the_empty_batch_warning():
    # a batch with >=1 item (even an all-skipped one) keeps the existing items_skipped shape —
    # empty_batch is reserved for the zero-items case, the two never coexist.
    srcs = [_src("x.docx", fmt="docx", skip="unsupported_format")]
    res = run_batch(srcs, run_one=lambda s, i: _ok())
    codes = [w.code for w in (res.warnings or [])]
    assert "empty_batch" not in codes and "items_skipped" in codes


def test_on_progress_reaches_total_and_renders_skip_reason_for_a_mixed_batch():
    # Tier 2/1: a skip-classified source used to be written into items[idx] without ever calling
    # _emit, so on_progress (and the [N/total] counter it drives) silently stalled short of total
    # whenever any item was skipped. A healthy batch with one skip among successes must now still
    # invoke on_progress exactly `total` times, and the skip's own event must carry its skip_reason.
    srcs = [_src("a.pdf"), _src("x.docx", fmt="docx", skip="unsupported_format"), _src("b.pdf")]
    events = []
    res = run_batch(
        srcs,
        run_one=lambda s, i: _ok(),
        on_progress=lambda done, total, item: events.append((done, total, item)),
    )
    assert len(events) == len(srcs) == 3  # every source reached on_progress, not just the two run
    assert {e[0] for e in events} == {1, 2, 3} and all(e[1] == 3 for e in events)
    skip_events = [item for _, _, item in events if item.state == "skipped"]
    assert len(skip_events) == 1 and skip_events[0].skip_reason == "unsupported_format"
    assert res.summary.total == 3  # sanity: the fix doesn't change aggregation, only progress


# --- M8 honest aggregation --------------------------------------------------------------


def test_aggregation_sums_cost_tallies_backends_and_bases():
    srcs = [_src("a.pdf"), _src("b.pdf"), _src("c.pdf")]
    resp = {
        "a.pdf": _ok("reducto", cost=0.10, basis="estimated", pages=2),
        "b.pdf": _ok("reducto", cost=0.20, basis="metered", pages=3),
        "c.pdf": _ok("pymupdf"),  # no cost reported
    }
    res = run_batch(srcs, run_one=lambda s, i: resp[s.ref.filename])
    assert res.summary.cost_usd == pytest.approx(0.30)  # only items that reported a cost
    assert set(res.summary.cost_bases) == {
        "estimated",
        "metered",
    }  # distinct bases, no fake precision
    assert res.summary.backends == {"reducto": 2, "pymupdf": 1}
    assert res.summary.pages_processed == 5


def test_no_cost_reported_leaves_cost_usd_absent():
    res = run_batch([_src("a.pdf")], run_one=lambda s, i: _ok("pymupdf"))
    assert res.summary.cost_usd is None


# --- concurrency preserves input order (M1) ---------------------------------------------


def test_jobs_pool_completes_all_and_preserves_input_order():
    srcs = [_src(f"{i}.pdf") for i in range(8)]

    def run_one(src, idem):
        return {**_ok(), "document": {"text": src.ref.filename}}

    res = run_batch(srcs, run_one=run_one, jobs=4)
    assert [i.response["document"]["text"] for i in res.items] == [f"{i}.pdf" for i in range(8)]
    assert res.summary.succeeded == 8


# --- idempotency + progress + valid envelope --------------------------------------------


def test_idempotency_key_is_derived_and_passed_to_run_one():
    seen = {}

    def run_one(src, idem):
        seen["idem"] = idem
        return _ok()

    run_batch([_src("a.pdf")], run_one=run_one, idempotency_key="BATCH")
    assert seen["idem"] == "BATCH:0123456789abcdef"


def test_on_progress_is_called_per_completed_item():
    srcs = [_src("a.pdf"), _src("b.pdf")]
    events = []
    run_batch(
        srcs,
        run_one=lambda s, i: _ok(),
        on_progress=lambda done, total, item: events.append((done, total, item.source.filename)),
    )
    assert {e[0] for e in events} == {1, 2} and all(e[1] == 2 for e in events)


def test_result_is_a_schema_valid_batch_envelope():
    res = run_batch([_src("a.pdf")], run_one=lambda s, i: _ok())
    schemas.validate_batch_result(res.to_schema_dict())
    assert res.items[0].transport == "platform"


# --- BL-84: bound_jobs — the ONE shared floor/ceiling bounds-check ----------------------
#
# Deliberately NOT exercised through run_batch(): bound_jobs is called by each of the three
# surfaces (server, CLI, Python API) before run_batch is ever reached, never by run_batch itself
# (see its own docstring) — so its contract is tested directly here, standalone.


def test_bound_jobs_floor_clamps_non_positive_to_one():
    assert bound_jobs(0) == 1
    assert bound_jobs(-1) == 1
    assert bound_jobs(-5000) == 1


def test_bound_jobs_passes_through_values_within_bounds():
    assert bound_jobs(1) == 1
    assert bound_jobs(8) == 8


def test_bound_jobs_ceiling_is_inclusive():
    assert bound_jobs(MAX_BATCH_JOBS) == MAX_BATCH_JOBS  # the limit itself is still allowed


def test_bound_jobs_rejects_above_the_ceiling():
    with pytest.raises(JobsLimitError) as ei:
        bound_jobs(MAX_BATCH_JOBS + 1)
    msg = str(ei.value)
    assert str(MAX_BATCH_JOBS + 1) in msg  # names the offending count
    assert str(MAX_BATCH_JOBS) in msg  # names the limit (mirrors SourceLimitError's own shape)


def test_bound_jobs_a_wildly_oversized_request_is_also_rejected():
    # The live-reproduced BL-84 repro number — a request this large must never reach a real
    # ThreadPoolExecutor(max_workers=...).
    with pytest.raises(JobsLimitError):
        bound_jobs(5_000_000)


def test_bound_jobs_honors_a_caller_supplied_max_jobs_override():
    assert bound_jobs(5, max_jobs=10) == 5
    with pytest.raises(JobsLimitError):
        bound_jobs(11, max_jobs=10)
    # the floor is unaffected by the override
    assert bound_jobs(0, max_jobs=10) == 1
