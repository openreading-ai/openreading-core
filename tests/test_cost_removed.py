"""Core counts what the vendor reported, and never converts it to money.

. Cost was three things wearing one word.

An **observation**: `pages_processed`, `credits`, `input_tokens`, `output_tokens` are counters the
vendor returned for this call, and `duration_ms` comes from a clock this package owns. Those stay.

An **assertion**: `descriptor.cost.usd_per_page_equiv_*` on fifteen adapters, plus private price
tables in two of them, one dated `accessed 2026-06-24`. Someone read a pricing page and typed
numbers into Python.

A **derivation** that launders the second into the first: `router/cost.py` turned those tables into
`response.usage.cost_usd` and set it beside `input_tokens`, where a caller had no way to tell which
number was counted and which was guessed.

The assertion and the derivation go. A caller who wants dollars multiplies the counters by the
prices on their own invoice, which is the only price true for them: it carries their tier and
their negotiated rate, and this package can see neither.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from openreading import api
from openreading.adapters.registry import BUILTIN_ADAPTERS, make_adapter

SRC = Path(__file__).resolve().parents[1] / "src" / "openreading"


@pytest.fixture
def pdf_path(tmp_path):
    from openreading.testing.sample_pdf import build_sample_pdf

    path = tmp_path / "doc.pdf"
    path.write_bytes(build_sample_pdf())
    return str(path)


def test_no_descriptor_carries_a_price():
    for slug in BUILTIN_ADAPTERS:
        assert not hasattr(make_adapter(slug).descriptor, "cost")


def test_no_adapter_keeps_a_private_price_table():
    """`anthropic_claude._MODEL_PRICE` dated itself `accessed 2026-06-24`, and
    `azure_document_intelligence`, `aws_textract` and `mistral_ocr` each carried an equivalent.

    A price table is a module-level name binding a rate, so that is what this looks for. Prose is
    left alone deliberately: `open_ocr`'s docstring has to be able to SAY that its vendor returns a
    debit and that core drops it, and a text scan for "usd" would forbid the explanation along with
    the practice."""
    banned = re.compile(r"^_\w*(PRICE|USD|RATE)\w*\s*[:=]", re.M)
    for path in (SRC / "adapters").rglob("adapter.py"):
        found = banned.findall(path.read_text())
        assert not found, f"{path.name} still holds a price table: {found}"


def test_a_response_carries_counters_and_no_dollars(pdf_path):
    resp = api.run(pdf_path, backend="pymupdf")
    usage = resp.get("usage") or {}
    assert "cost_usd" not in usage
    assert "cost_basis" not in usage
    assert "pages_processed" in usage, "the counters the vendor reported still travel"


def test_the_response_schema_has_no_dollar_field():
    from openreading import schemas

    doc = json.loads((SRC / "schemas" / schemas.RESPONSE_SCHEMA_FILE).read_text())
    usage = doc["properties"]["usage"]["properties"]
    assert "cost_usd" not in usage
    assert "cost_basis" not in usage
    assert "input_tokens" in usage, "vendor-reported counters stay"


def test_a_batch_summary_reports_no_dollars(pdf_path, tmp_path):
    env = api.run_batch([pdf_path], backend="pymupdf")
    assert "cost_usd" not in env["summary"]
    assert "cost_bases" not in env["summary"]


def test_compare_has_no_cost_outlier_finding():
    """It compared two of core's own estimates against each other and called one an outlier, out
    of a CLOSED finding vocabulary."""
    from openreading import schemas

    doc = json.loads((SRC / "schemas" / schemas.COMPARISON_REPORT_SCHEMA_FILE).read_text())
    assert "cost_outlier" not in json.dumps(doc)


def test_the_leaderboard_reports_no_cost_per_doc():
    """A benchmark over real documents carried a cost column that was never measured during the
    benchmark: it came straight from the descriptor."""
    from openreading.evals import run_leaderboard

    report = run_leaderboard(
        "src/openreading/evals/sample", ["pymupdf", "tesseract"], api.build_registry()
    ).to_schema_dict()
    for row in report["backends"]:
        assert "cost_per_doc" not in row


def test_apply_cost_report_still_fills_vendor_counters():
    """The deletion is inside this function, so this is the test that catches an over-broad cut."""
    from openreading.router.cost import apply_cost_report
    from openreading.types.cost import CostReport
    from openreading.types.enums import BackendType, ResponseState, WaitMode
    from openreading.types.job import Job
    from openreading.types.response import BackendInfo, Document, NormalizedResponse, Status

    report = CostReport(native_unit="page", native_quantity=3.0, duration_ms=17)
    job = Job(id="j", backend_id="b", wait_mode=WaitMode.INLINE)
    blank = NormalizedResponse(
        status=Status(state=ResponseState.SUCCEEDED),
        backend=BackendInfo(id="b", type=BackendType.OSS_LIBRARY),
        document=Document(text="t"),
    )
    resp = apply_cost_report(_FakeAdapter(report), job, blank, None)
    assert resp.usage is not None
    assert resp.usage.pages_processed == 3
    assert resp.usage.duration_ms == 17


class _FakeAdapter:
    descriptor = None

    def __init__(self, report):
        self._report = report

    def report_cost(self, job, ctx=None):
        return self._report


def test_preflight_counts_rather_than_prices(tmp_path):
    """A scope preflight: documents, pages, calls, and which targets bill the caller's account."""
    from openreading.evals.preflight import scope_run
    from openreading.evals.subset import plan_subset
    from openreading.evals.targets import BenchmarkTarget
    from openreading.testing.sample_pdf import build_sample_pdf

    corpus = tmp_path / "corpus"
    (corpus / "g").mkdir(parents=True)
    (corpus / "g" / "a.pdf").write_bytes(build_sample_pdf())
    (corpus / "g" / "a.test.json").write_text(json.dumps({"expected": {}}))

    rendered = scope_run(
        plan_subset(corpus, limit=0), [BenchmarkTarget.parse("backend:reducto")]
    ).render()

    assert "$" not in rendered
    assert "page(s)" in rendered and "call(s)" in rendered
