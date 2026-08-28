"""Phase B (Manifest v0.6) — api.run_batch end-to-end, OFFLINE via the local pymupdf backend
(no keys, no network). Proves the full wiring: intake resolution → M3 format filter from the real
descriptor → the platform runner → the existing single-document run() per item → one schema-valid
batch envelope with honest skip/success accounting (the §12 milestone acceptance, minus compare)."""

from __future__ import annotations

import inspect

import pytest

from openreading import run_batch, schemas
from openreading.batch.sources import DEFAULT_MAX_ITEMS
from openreading.testing.sample_pdf import build_sample_pdf


def _corpus(tmp_path):
    d = tmp_path / "corpus"
    d.mkdir()
    pdf = build_sample_pdf()
    (d / "a.pdf").write_bytes(pdf)
    (d / "sub").mkdir()
    (d / "sub" / "b.pdf").write_bytes(pdf)
    (d / "note.docx").write_bytes(b"PK not-really-a-docx")  # a known format pymupdf can't take
    return d


def test_run_batch_dir_with_pymupdf_is_schema_valid_and_honest(tmp_path):
    env = run_batch([str(_corpus(tmp_path))], backend="pymupdf")
    schemas.validate_batch_result(env)  # M9-adjacent: the envelope itself is valid

    states = {i["source"]["relpath"]: i["state"] for i in env["items"]}
    assert states == {"a.pdf": "succeeded", "note.docx": "skipped", "sub/b.pdf": "succeeded"}
    # skip is honest, with a reason (docx is a known doc type pymupdf's input_formats excludes)
    docx = next(i for i in env["items"] if i["source"]["relpath"] == "note.docx")
    assert docx["skip_reason"] == "unsupported_format"

    s = env["summary"]
    assert (s["total"], s["succeeded"], s["failed"], s["skipped"]) == (3, 2, 0, 1)
    assert env["status"]["state"] == "succeeded"  # skips alone never fail a batch
    assert s["backends"] == {"pymupdf": 2}
    assert any(w["code"] == "items_skipped" for w in env.get("warnings", []))


def test_run_batch_succeeded_item_carries_a_valid_response_envelope(tmp_path):
    env = run_batch([str(_corpus(tmp_path))], backend="pymupdf")
    ok = next(i for i in env["items"] if i["state"] == "succeeded")
    assert ok["transport"] == "platform"
    schemas.validate_response(ok["response"])  # M9: items are full response.v0.3 envelopes
    assert ok["response"]["backend"]["id"] == "pymupdf"


def test_run_batch_jobs_pool_offline(tmp_path):
    """jobs>1 must be observationally identical to jobs=1, not merely the same item COUNT:
    pymupdf's find_tables() mutates process-global state, so an unserialized pool silently
    returned tight glyph boxes (title top 64.35 instead of the ascender-inflated 58.5)."""
    env = run_batch([str(_corpus(tmp_path))], backend="pymupdf", jobs=4)
    assert [i["source"]["relpath"] for i in env["items"]] == ["a.pdf", "note.docx", "sub/b.pdf"]
    assert env["summary"]["succeeded"] == 2

    for item in (i for i in env["items"] if i["state"] == "succeeded"):
        schemas.validate_response(item["response"])
        page = item["response"]["document"]["pages"][0]
        title = next(b for b in page["blocks"] if b["type"] == "title")
        assert title["text"] == "OpenReading Test Document"
        assert title["bbox"]["bbox_native"]["coords"][1] == pytest.approx(58.5, abs=1e-2)
        assert title["bbox"]["y"] == pytest.approx(58.5 / 792.0, abs=1e-4)
        table = next(b for b in page["blocks"] if b["type"] == "table")
        assert table["table"]["rows"][0] == ["Region", "Units", "Revenue"]
        assert table["table"]["rows"][3] == ["West", "42", "1650"]


def test_run_batch_max_items_guard(tmp_path):
    from openreading.batch.sources import SourceLimitError

    d = _corpus(tmp_path)
    with pytest.raises(SourceLimitError):
        run_batch([str(d)], backend="pymupdf", max_items=1)


# --- BL-84: jobs floor/ceiling on the Python API surface --------------------------------


def test_run_batch_jobs_floor_clamps_to_one_and_is_echoed(tmp_path):
    # jobs<=0 used to pass straight through unclamped: the batch still ran (jobs<=1 is the
    # ordinary serial path) but the echo carried the raw, nonsensical value — a schema-invalid
    # envelope (schema: "jobs": {"minimum": 1}) returned silently, with no exception at all.
    env = run_batch([str(_corpus(tmp_path))], backend="pymupdf", jobs=0)
    schemas.validate_batch_result(env)
    assert env["request"]["jobs"] == 1  # corrected value, not the raw 0
    assert env["summary"]["succeeded"] == 2


def test_run_batch_jobs_ceiling_raises_a_catchable_error(tmp_path):
    from openreading.batch.runner import MAX_BATCH_JOBS, JobsLimitError

    with pytest.raises(JobsLimitError):
        run_batch([str(_corpus(tmp_path))], backend="pymupdf", jobs=MAX_BATCH_JOBS + 1)


def test_run_batch_jobs_ceiling_rejects_before_touching_the_filesystem(tmp_path):
    # The bounds-check happens before intake resolution — an over-ceiling call must fail even
    # against a source that doesn't exist, proving it never gets far enough to need one.
    from openreading.batch.runner import JobsLimitError

    with pytest.raises(JobsLimitError):
        run_batch([str(tmp_path / "does-not-exist")], backend="pymupdf", jobs=5_000_000)


def test_run_batch_max_jobs_override_mirrors_max_items_escape_hatch_shape(tmp_path):
    # BL-94: this test's name calls max_jobs= an "escape hatch," but the body only ever narrowed
    # the ceiling (max_jobs=2, under the default 32) — the one scenario an escape hatch actually
    # means (jobs strictly above the default ceiling, permitted only because max_jobs= raises it
    # past that point) was never run, so BatchRequestEcho's own structural Field(le=32) crashing
    # that exact input went uncaught by a test with this exact name.
    env = run_batch([str(_corpus(tmp_path))], backend="pymupdf", jobs=50, max_jobs=100)
    schemas.validate_batch_result(env)
    assert env["request"]["jobs"] == 50
    assert env["summary"]["succeeded"] == 2


def test_run_batch_max_items_default_tracks_the_shared_constant():
    """BL-145: `run_batch`'s bare `max_items` default must come from `batch.sources`'
    `DEFAULT_MAX_ITEMS` rather than an independently-written literal, so the two can't silently
    drift apart. Asserted via `==`, not `is`: CPython caches small ints as singletons, so an
    identity check would pass today even against an unfixed, independently-hardcoded `200` — for
    the wrong reason — and would only ever fail once the values had already diverged numerically,
    which is backwards for a drift-detection test."""
    default = inspect.signature(run_batch).parameters["max_items"].default
    assert default == DEFAULT_MAX_ITEMS
