"""Corpus subsetting and the spending preflight.

Both publisher formats are built by hand in ``tests/conftest.py``, to the shape their own loader
documents: ParseBench's JSONL corpus (``{category}.jsonl`` whose ``pdf`` key is a path relative to
the root) and ExtractBench's sidecar corpus (``<group>/<stem>.pdf`` beside ``<stem>.test.json``).
Building them rather than downloading keeps this in the offline lane, where a test that spends
money or waits on HuggingFace does not belong.

What these lock down is the promise that makes a small run safe: the subset a reader asks for is
the subset that runs, it is written in the format the publisher reads back, and the price is
counted in pages rather than documents before anything bills.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from openreading.evals.preflight import (
    CONFIRM_ABOVE_USD,
    confirm,
    count_pages,
    estimate_cost,
)
from openreading.evals.subset import (
    CorpusError,
    discover_documents,
    materialize_subset,
    plan_subset,
    select_documents,
)
from openreading.evals.targets import BenchmarkTarget
from tests.conftest import jsonl_corpus as _jsonl_corpus
from tests.conftest import sidecar_corpus as _sidecar_corpus

# --- discovery -------------------------------------------------------------------------------


def test_jsonl_corpus_counts_one_document_per_pdf_not_per_rule(tmp_path) -> None:
    layout, documents = discover_documents(_jsonl_corpus(tmp_path))

    assert layout == "jsonl"
    # 4 categories x 3 documents, each asserted by 2 rules.
    assert len(documents) == 12
    assert documents[0].doc_id == "chart/doc0"
    assert documents[0].relative == "pdfs/chart/doc0.pdf"


def test_sidecar_corpus_ignores_the_test_json_siblings(tmp_path) -> None:
    layout, documents = discover_documents(_sidecar_corpus(tmp_path))

    assert layout == "sidecar"
    assert len(documents) == 6
    assert [d.doc_id for d in documents[:2]] == ["long/doc0", "long/doc1"]


def test_discovery_refuses_an_empty_directory(tmp_path) -> None:
    (tmp_path / "empty").mkdir()
    with pytest.raises(CorpusError, match="no benchmark documents"):
        discover_documents(tmp_path / "empty")


# --- selection -------------------------------------------------------------------------------


def test_small_limit_spreads_across_categories(tmp_path) -> None:
    _, documents = discover_documents(_jsonl_corpus(tmp_path))

    chosen = select_documents(documents, limit=2)

    # Two documents from two different categories. Taking the first two rows of chart.jsonl would
    # tell the reader nothing about tables, which is the whole point of a small first run.
    assert len({doc.group for doc in chosen}) == 2
    assert [doc.doc_id for doc in chosen] == ["chart/doc0", "layout/doc0"]


def test_limit_zero_and_oversized_limit_take_everything(tmp_path) -> None:
    _, documents = discover_documents(_jsonl_corpus(tmp_path))

    assert select_documents(documents, limit=0) == documents
    assert select_documents(documents, limit=999) == documents


def test_selection_is_stable_across_calls(tmp_path) -> None:
    _, documents = discover_documents(_jsonl_corpus(tmp_path))

    # A rerun must pick the same documents, or the publisher's resume re-bills a different set.
    assert select_documents(documents, limit=5) == select_documents(documents, limit=5)


def test_named_documents_win_over_limit(tmp_path) -> None:
    _, documents = discover_documents(_jsonl_corpus(tmp_path))

    chosen = select_documents(documents, limit=2, names=("table/doc2",))

    assert [doc.doc_id for doc in chosen] == ["table/doc2"]


def test_an_unknown_document_name_is_an_error_not_a_silent_skip(tmp_path) -> None:
    _, documents = discover_documents(_jsonl_corpus(tmp_path))

    with pytest.raises(CorpusError, match="no benchmark document named"):
        select_documents(documents, limit=2, names=("nope",))


def test_negative_limit_is_refused(tmp_path) -> None:
    _, documents = discover_documents(_jsonl_corpus(tmp_path))

    with pytest.raises(ValueError, match="negative"):
        select_documents(documents, limit=-1)


# --- materialization -------------------------------------------------------------------------


def test_jsonl_subset_is_readable_by_the_publisher_loader(tmp_path) -> None:
    source = _jsonl_corpus(tmp_path / "full")
    plan = plan_subset(source, limit=2)

    out = materialize_subset(plan, tmp_path / "subset")

    # Same shape, fewer documents: rules for the chosen PDFs only, and each PDF at the relative
    # path its rows name, because the loader resolves `root / row["pdf"]`.
    assert (out / "pdfs/chart/doc0.pdf").is_file()
    assert (out / "pdfs/layout/doc0.pdf").is_file()
    assert not (out / "pdfs/chart/doc1.pdf").exists()
    kept = [json.loads(line) for line in (out / "chart.jsonl").read_text().splitlines()]
    assert {row["pdf"] for row in kept} == {"pdfs/chart/doc0.pdf"}
    assert len(kept) == 2  # both rules for that one document survive
    # A category with nothing selected is dropped, not written empty: the publisher discovers its
    # evaluation groups from the files present.
    assert not (out / "table.jsonl").exists()
    assert json.loads((out / "expected_markdown.json").read_text()).keys() == {
        "pdfs/chart/doc0.pdf",
        "pdfs/layout/doc0.pdf",
    }


def test_sidecar_subset_carries_the_test_json_with_its_document(tmp_path) -> None:
    source = _sidecar_corpus(tmp_path / "full")
    plan = plan_subset(source, limit=2)

    out = materialize_subset(plan, tmp_path / "subset")

    for document in plan.documents:
        assert (out / document.relative).is_file()
        # Without the sidecar the document has no schema and no expected values, so it scores
        # nothing and the run looks like a failure of the backend.
        assert (out / document.group / f"{Path(document.relative).stem}.test.json").is_file()


def test_materializing_twice_replaces_rather_than_merges(tmp_path) -> None:
    source = _jsonl_corpus(tmp_path / "full")
    materialize_subset(plan_subset(source, limit=4), tmp_path / "subset")

    out = materialize_subset(plan_subset(source, limit=2), tmp_path / "subset")

    assert not (out / "table.jsonl").exists()


def test_a_complete_plan_reports_itself_complete(tmp_path) -> None:
    source = _jsonl_corpus(tmp_path / "full")

    assert plan_subset(source, limit=0).is_complete
    assert not plan_subset(source, limit=2).is_complete


# --- the preflight ---------------------------------------------------------------------------


def test_pages_are_counted_from_the_documents_not_assumed(tmp_path) -> None:
    plan = plan_subset(_jsonl_corpus(tmp_path), limit=2)

    pages, unknown = count_pages(plan)

    # The bundled sample is two pages, so two documents bill four pages. A document count would
    # have said two, which is the understatement this whole module exists to remove.
    assert (pages, unknown) == (4, 0)


def test_estimate_prices_per_page_and_names_the_range(tmp_path) -> None:
    plan = plan_subset(_jsonl_corpus(tmp_path), limit=2)

    estimate = estimate_cost(plan, [BenchmarkTarget.parse("backend:reducto")])

    # reducto declares $0.015 to $0.06 a page, over four pages.
    assert estimate.pages == 4
    assert estimate.targets[0].low_usd == pytest.approx(0.06)
    assert estimate.targets[0].high_usd == pytest.approx(0.24)
    assert "4 page(s)" in estimate.render()
    assert "per-page rates, not a quote" in estimate.render()


def test_a_local_backend_prices_at_zero_and_never_prompts(tmp_path) -> None:
    plan = plan_subset(_jsonl_corpus(tmp_path), limit=2)

    estimate = estimate_cost(plan, [BenchmarkTarget.parse("backend:pymupdf")])

    assert estimate.high_usd == 0.0
    assert estimate.needs_confirmation is False


def test_a_strategy_target_is_unpriced_and_always_asks(tmp_path) -> None:
    plan = plan_subset(_jsonl_corpus(tmp_path), limit=2)

    estimate = estimate_cost(plan, [BenchmarkTarget.parse("strategy:main")])

    # Escalation means one document is one or more billed calls, and nothing here knows how many.
    # Guessing the low end would read as a quote for a run that can cost several times it.
    assert estimate.unpriced == ("strategy:main",)
    assert estimate.needs_confirmation is True
    assert "escalates" in estimate.render()


def test_a_token_billed_backend_is_unpriced_rather_than_free(tmp_path) -> None:
    plan = plan_subset(_jsonl_corpus(tmp_path), limit=2)

    estimate = estimate_cost(plan, [BenchmarkTarget.parse("backend:google-gemini")])

    assert estimate.unpriced == ("backend:google-gemini",)
    assert "no per-page rate" in estimate.render()


def test_a_large_run_crosses_the_confirmation_threshold(tmp_path) -> None:
    plan = plan_subset(_jsonl_corpus(tmp_path, per_category=30), limit=0)

    estimate = estimate_cost(plan, [BenchmarkTarget.parse("backend:reducto")])

    assert estimate.high_usd > CONFIRM_ABOVE_USD
    assert estimate.needs_confirmation is True


def test_estimate_says_how_much_of_the_corpus_is_left_behind(tmp_path) -> None:
    plan = plan_subset(_jsonl_corpus(tmp_path), limit=2)

    rendered = estimate_cost(plan, [BenchmarkTarget.parse("backend:pymupdf")]).render()

    assert "2 document(s) of 12 prepared (10 not run)" in rendered


# --- the confirmation gate -------------------------------------------------------------------


def test_yes_flag_skips_the_prompt(tmp_path) -> None:
    plan = plan_subset(_jsonl_corpus(tmp_path), limit=2)
    estimate = estimate_cost(plan, [BenchmarkTarget.parse("strategy:main")])

    assert confirm(estimate, assume_yes=True) is True


def test_a_free_run_never_asks(tmp_path) -> None:
    plan = plan_subset(_jsonl_corpus(tmp_path), limit=2)
    estimate = estimate_cost(plan, [BenchmarkTarget.parse("backend:pymupdf")])

    assert confirm(estimate, assume_yes=False) is True


def test_no_terminal_refuses_rather_than_blocking(tmp_path, monkeypatch) -> None:
    plan = plan_subset(_jsonl_corpus(tmp_path), limit=2)
    estimate = estimate_cost(plan, [BenchmarkTarget.parse("strategy:main")])
    monkeypatch.setattr("sys.stdin", io.StringIO(""))  # StringIO.isatty() is False
    stream = io.StringIO()

    # A CI job hung on stdin is a worse failure than one that stops and names the flag.
    assert confirm(estimate, assume_yes=False, stream=stream) is False
    assert "pass --yes" in stream.getvalue()


@pytest.mark.parametrize(
    ("answer", "proceeds"), [("y\n", True), ("yes\n", True), ("\n", False), ("n\n", False)]
)
def test_a_terminal_answer_decides(tmp_path, monkeypatch, answer: str, proceeds: bool) -> None:
    plan = plan_subset(_jsonl_corpus(tmp_path), limit=2)
    estimate = estimate_cost(plan, [BenchmarkTarget.parse("strategy:main")])

    class _Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr("sys.stdin", _Tty(answer))

    assert confirm(estimate, assume_yes=False, stream=io.StringIO()) is proceeds
