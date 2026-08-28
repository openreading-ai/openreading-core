"""Phase C (Manifest v0.6) — corpus compare: two batch-result runs paired per-document.

Pairs documents across runs by identity (relpath → filename → sha256), reuses the existing
comparison-report v0.2 per paired document, and rolls verdicts up. Unpaired docs (present in some
runs, not all) are surfaced, not crashed. Pure — house pattern tests/test_compare_structure.py."""

from __future__ import annotations

import pytest

from openreading import schemas
from openreading.comparison.corpus import (
    corpus_pairs,
    corpus_report_dict,
    is_batch_envelope,
    render_corpus_diffs,
    render_corpus_table,
)
from tests.fakes import make_envelope


def _batch(backend: str, items: list[tuple[str, dict | None]]) -> dict:
    """A minimal batch-result envelope: items[(relpath, response|None)]; None → a failed item."""
    return {
        "schema_version": "0.1",
        "status": {"state": "succeeded"},
        "items": [
            {
                "source": {"relpath": rp, "filename": rp, "sha256": rp + "-hash"},
                "state": "succeeded" if resp else "failed",
                "response": resp,
                **({} if resp else {"error": {"code": "backend_error"}}),
            }
            for rp, resp in items
        ],
        "summary": {
            "total": len(items),
            "succeeded": sum(1 for _, r in items if r),
            "failed": sum(1 for _, r in items if not r),
            "skipped": 0,
            "backends": {backend: sum(1 for _, r in items if r)},
        },
    }


def _runs():
    a = _batch(
        "pymupdf",
        [
            ("a.pdf", make_envelope("pymupdf", text="Region Revenue North 4400")),  # → equivalent
            ("b.pdf", make_envelope("pymupdf", text="one two three four five six")),  # → divergent
            ("c.pdf", make_envelope("pymupdf", text="only in run A")),  # → unpaired
        ],
    )
    b = _batch(
        "tesseract",
        [
            ("a.pdf", make_envelope("tesseract", text="Region Revenue North 4400")),
            (
                "b.pdf",
                make_envelope("tesseract", text="completely unrelated content appears here now"),
            ),
        ],
    )
    return [a, b], ["runA", "runB"]


def test_is_batch_envelope_discriminates():
    batches, _ = _runs()
    assert is_batch_envelope(batches[0]) is True
    assert (
        is_batch_envelope(make_envelope("pymupdf", text="hi")) is False
    )  # a response, not a batch


def test_pairs_by_relpath_with_verdicts_and_unpaired():
    batches, labels = _runs()
    pairs = {p.key: p for p in corpus_pairs(batches, labels)}
    assert set(pairs) == {"a.pdf", "b.pdf", "c.pdf"}
    assert pairs["a.pdf"].verdict == "equivalent"  # identical text
    assert pairs["b.pdf"].verdict == "divergent"  # unrelated text
    assert pairs["c.pdf"].verdict == "unpaired"  # only run A has it
    assert pairs["a.pdf"].report is not None and pairs["c.pdf"].report is None


def _tbl(rows: list[list[str]]) -> dict:
    return {"type": "table", "text": "grid", "table": {"rows": rows}}


def test_table_cells_capability_gap_keeps_pair_off_equivalent():
    # BL-58 blast radius: a paired document's verdict is a direct pass-through of the per-document
    # comparison-report's `headline.verdict` (corpus.py:82) — a table-bearing subject paired
    # against a block-less one must not roll up to "equivalent" just because the table comparison
    # never got a chance to run (blocks_section needs >=2 jointly block-capable subjects).
    a = _batch(
        "aws-textract",
        [("t.pdf", make_envelope("aws-textract", pages=[[_tbl([["1", "2"], ["3", "4"]])]]))],
    )
    b = _batch(
        "open-ocr",
        [
            (
                "t.pdf",
                make_envelope("open-ocr", text="a completely different, block-less transcription"),
            )
        ],
    )
    pairs = corpus_pairs([a, b], ["runA", "runB"])
    assert len(pairs) == 1
    assert pairs[0].report is not None
    assert pairs[0].report["alignment"]["capable_subjects"] == 1
    assert pairs[0].verdict != "equivalent"


def test_corpus_report_dict_is_schema_valid_with_rollup():
    batches, labels = _runs()
    pairs = corpus_pairs(batches, labels)
    report = corpus_report_dict(pairs, batches, labels, ["runA.json", "runB.json"])
    schemas.validate_corpus_report(report)
    r = report["rollup"]
    assert (r["documents"], r["equivalent"], r["divergent"], r["unpaired"]) == (3, 1, 1, 1)
    assert report["subjects"][0]["backend_tally"] == {"pymupdf": 3}  # run A has 3 succeeded items
    # documents are ordered deterministically (sorted by identity key)
    assert [d["source"]["relpath"] for d in report["documents"]] == ["a.pdf", "b.pdf", "c.pdf"]


def test_render_table_shows_verdict_per_doc_and_rollup():
    batches, labels = _runs()
    out = render_corpus_table(corpus_pairs(batches, labels), labels)
    assert "a.pdf" in out and "equivalent" in out
    assert "b.pdf" in out and "divergent" in out
    assert "c.pdf" in out and "unpaired" in out
    assert "3 document" in out  # rollup line


def test_render_diffs_shows_value_deltas_for_divergent_docs():
    batches, labels = _runs()
    out = render_corpus_diffs(corpus_pairs(batches, labels), labels)
    # the divergent doc shows the ACTUAL content each side captured that the other missed (the value
    # diff), not counts — runA has "one two three...", runB has "completely unrelated...".
    assert "b.pdf" in out and "divergent" in out and "shared" in out
    assert "only runA captured" in out and "one two three four five six" in out
    assert "only runB captured" in out and "completely unrelated content appears here now" in out
    # equivalent/unpaired docs collapse to a one-line verdict — no value block before b.pdf
    a_idx, b_idx = out.find("a.pdf"), out.find("b.pdf")
    assert a_idx != -1 and b_idx != -1
    assert "shared" not in out[a_idx:b_idx] and "captured" not in out[a_idx:b_idx]
    # and the footer shows how to dump one document's full text
    assert "jq" in out and ".response.document.text" in out


def test_render_diffs_caps_value_lines_per_side():
    # a divergent doc where one side captured many unique lines → capped with a "… N more" tail
    many = "\n".join(f"unique line number {i} only here" for i in range(20))
    a = _batch("pymupdf", [("big.pdf", make_envelope("pymupdf", text=many))])
    b = _batch(
        "tesseract", [("big.pdf", make_envelope("tesseract", text="totally different words"))]
    )
    out = render_corpus_diffs(corpus_pairs([a, b], ["runA", "runB"]), ["runA", "runB"], limit=8)
    assert "only runA captured (20)" in out  # the true count is shown
    assert out.count("        + unique line number") == 8  # but only `limit` lines are rendered
    assert "… 12 more" in out


def test_empty_when_no_shared_documents():
    a = _batch("pymupdf", [("x.pdf", make_envelope("pymupdf", text="x"))])
    b = _batch("tesseract", [("y.pdf", make_envelope("tesseract", text="y"))])
    pairs = corpus_pairs([a, b], ["runA", "runB"])
    assert {p.key for p in pairs} == {"x.pdf", "y.pdf"}
    assert all(p.verdict == "unpaired" for p in pairs)  # nothing shared → all unpaired


# --- CLI routing (batch-vs-batch → corpus) ----------------------------------------------


def _write_runs(tmp_path):
    import json

    batches, _ = _runs()
    fa, fb = tmp_path / "runA.json", tmp_path / "runB.json"
    fa.write_text(json.dumps(batches[0]))
    fb.write_text(json.dumps(batches[1]))
    return fa, fb


def test_cli_routes_batch_vs_batch_to_corpus_table(tmp_path, capsys):
    from openreading.cli.app import main

    fa, fb = _write_runs(tmp_path)
    rc = main(["compare", str(fa), str(fb), "--format", "table"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "CORPUS COMPARE" in out and "3 document" in out
    assert "divergent" in out and "equivalent" in out and "unpaired" in out


def test_cli_corpus_json_is_schema_valid(tmp_path, capsys):
    import json

    from openreading.cli.app import main

    fa, fb = _write_runs(tmp_path)
    rc = main(["compare", str(fa), str(fb), "--format", "json"])
    corpus = json.loads(capsys.readouterr().out)
    assert rc == 0
    schemas.validate_corpus_report(corpus)
    assert corpus["rollup"]["documents"] == 3


def test_cli_mixing_batch_and_response_is_a_usage_error(tmp_path, capsys):
    import json

    from openreading.cli.app import main

    batches, _ = _runs()
    fa = tmp_path / "runA.json"
    fa.write_text(json.dumps(batches[0]))
    fr = tmp_path / "resp.json"
    fr.write_text(json.dumps(make_envelope("pymupdf", text="x")))
    rc = main(["compare", str(fa), str(fr), "--format", "table"])
    assert rc == 2 and "cannot mix" in capsys.readouterr().err


@pytest.mark.live
@pytest.mark.skipif(
    __import__("shutil").which("tesseract") is None, reason="tesseract not installed"
)
def test_live_local_corpus_pymupdf_vs_tesseract(tmp_path, capsys):  # pragma: no cover
    # §12 acceptance: two batch runs over the SAME 3-file corpus (pymupdf vs tesseract, both local)
    # → a schema-valid corpus report. Real backends, no network/keys — gated live for suite speed.
    import json

    from openreading import run_batch
    from openreading.cli.app import main
    from openreading.testing.sample_pdf import build_sample_pdf

    d = tmp_path / "corpus"
    d.mkdir()
    for n in ("a.pdf", "b.pdf", "c.pdf"):
        (d / n).write_bytes(build_sample_pdf())
    (tmp_path / "runA.json").write_text(json.dumps(run_batch([str(d)], backend="pymupdf")))
    (tmp_path / "runB.json").write_text(json.dumps(run_batch([str(d)], backend="tesseract")))
    capsys.readouterr()  # discard backend stdout chatter from the batch runs so stdout is only the report
    rc = main(
        ["compare", str(tmp_path / "runA.json"), str(tmp_path / "runB.json"), "--format", "json"]
    )
    corpus = json.loads(capsys.readouterr().out)
    assert rc == 0
    schemas.validate_corpus_report(corpus)
    assert corpus["rollup"]["documents"] == 3
