"""Pulse adapter — documented-shape fixtures via an injected fake client (NO network). Covers the
per-class bounding_boxes → blocks reassembly, the no-confidence X channel (+ warning), the dual
inline/is_url response shape, and the compliance fails-closed behavior for its UNVERIFIED no-train
status. Fixtures follow docs.runpulse.com/api-reference/endpoint/extract (accessed 2026-07-21)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openreading.adapters.pulse import PulseAdapter
from openreading.adapters.registry import build_registry
from openreading.router import RouterConfig
from openreading.router.clock import FakeClock
from openreading.router.driver import run_to_completion
from openreading.router.router import Router
from openreading.testing import ConformanceCase, check_adapter_conformance
from openreading.types import BlockType, JobState
from openreading.types.enums import WaitMode
from openreading.types.request import ExtractionSchema, OpenReadingRequest
from openreading.types.runtime import RunContext

FIX = Path(__file__).parent / "fixtures" / "pulse"


def _fixture(name: str) -> dict:
    return json.loads((FIX / f"{name}.json").read_text())


class FakePulseClient:
    def __init__(self, url_mode: bool = False) -> None:
        self._url_mode = url_mode

    def extract(self, document, config):
        return _fixture("extract_url") if self._url_mode else _fixture("extract")

    def get_result(self, url):
        return _fixture("extract")  # resolve the is_url stub

    def get_job(self, extraction_id):
        return _fixture("extract")


def _req(**over) -> OpenReadingRequest:
    body = {
        "document": {"url": "https://example.com/loan.pdf", "mime_type": "application/pdf"},
        "backend": {"id": "pulse"},
    }
    body.update(over)
    return OpenReadingRequest.model_validate(body)


def _run(adapter, req):
    job = adapter.submit(req, RunContext())
    clock = FakeClock()
    job = run_to_completion(
        adapter, job, ctx=RunContext(), deadline_ms=clock.now_ms() + 120_000, clock=clock
    )
    assert job.state is JobState.SUCCEEDED
    return adapter.normalize(job, RunContext(), req)


# --- conformance + reassembly ----------------------------------------------------------


def test_pulse_conforms():
    # Phase B.4: pulse is remediated — its text channel is a plain position-ordered projection (C1)
    # and every requested N/D channel is delivered-or-warned (C6), so both promote to strict.
    adapter = PulseAdapter(client=FakePulseClient())
    check_adapter_conformance(
        adapter,
        [ConformanceCase(request=_req(), deterministic=True, label="extract")],
        strict_checks={"C1", "C6"},
        # Ledger T4a (R1/R2/R3): a fresh instance with its own separate fake client — proves
        # resume has no instance-affinity requirement (AC-5) and nothing is cached on self (AC-7).
        adapter_factory=lambda: PulseAdapter(client=FakePulseClient()),
    )


def test_sync_is_inline():
    adapter = PulseAdapter(client=FakePulseClient())
    job = adapter.submit(_req(), RunContext())
    assert job.wait_mode is WaitMode.INLINE and job.state is JobState.SUCCEEDED


def test_bounding_boxes_reassemble_to_blocks():
    resp = _run(PulseAdapter(client=FakePulseClient()), _req())
    blocks = resp.document.pages[0].blocks
    types = [b.type for b in blocks]
    assert BlockType.TITLE in types and BlockType.TABLE in types
    title = next(b for b in blocks if b.type is BlockType.TITLE)
    assert title.text == "Loan Application"
    assert title.bbox.x == pytest.approx(72 / 612, abs=1e-4)
    assert resp.document.markdown and "Loan Application" in resp.document.markdown
    # the Tables entry's cell_data is derived to a native grid (P1) and projected plainly into text
    table = next(b for b in blocks if b.type is BlockType.TABLE)
    assert table.table and table.table.rows == [["Region", "Revenue"], ["North", "4400"]]
    assert "|" not in (resp.document.text or "") and "Region\tRevenue" in (resp.document.text or "")
    # reading order is by position (Title top, then the table below it), not class grouping
    assert [b.reading_order for b in blocks] == list(range(len(blocks)))


def test_submit_forwards_extraction_schema_as_structured_output():
    captured: dict = {}

    class _Cap:
        def extract(self, document, config):
            captured.update(config)
            return {"markdown": "# x", "bounding_boxes": {}}

        def get_result(self, url):  # pragma: no cover - not hit in this path
            return {}

        def get_job(self, extraction_id):  # pragma: no cover - not hit in this path
            return {}

    schema = {"type": "object", "properties": {"total": {"type": "number"}}}
    adapter = PulseAdapter(client=_Cap())
    adapter.submit(
        _req(extraction_schema={"json_schema": schema, "instructions": "grab totals"}),
        RunContext(),
    )
    assert captured["structured_output"]["schema"] == schema  # documented /extract contract
    assert captured["structured_output"]["schemaPrompt"] == "grab totals"


def test_no_confidence_is_x_channel_and_warned():
    resp = _run(PulseAdapter(client=FakePulseClient()), _req())
    # no block carries a fabricated confidence
    assert all(b.confidence is None for pb in resp.document.pages for b in (pb.blocks or []))
    codes = [w.code for w in (resp.warnings or [])]
    assert "confidence_unavailable" in codes
    # the backend's own warning is surfaced too
    assert any("low-resolution" in (w.message or "") for w in (resp.warnings or []))


def test_async_mode_polls_to_completion():
    adapter = PulseAdapter(client=FakePulseClient())
    job = adapter.submit(_req(**{"async": {"mode": "async"}}), RunContext())
    assert job.wait_mode is WaitMode.POLL and job.state is JobState.RUNNING
    clock = FakeClock()
    job = run_to_completion(
        adapter, job, ctx=RunContext(), deadline_ms=clock.now_ms() + 120_000, clock=clock
    )
    assert job.state is JobState.SUCCEEDED
    resp = adapter.normalize(job, RunContext(), _req())
    assert resp.document.pages and resp.document.pages[0].blocks


def test_dual_shape_is_url_is_resolved():
    # the large-doc {is_url:true,url} stub is resolved to the real result before normalize
    resp = _run(PulseAdapter(client=FakePulseClient(url_mode=True)), _req())
    assert resp.document.pages and resp.document.pages[0].blocks
    assert "Loan Application" in (resp.document.markdown or "")


def test_no_credentials_is_terminal():
    from openreading.types.errors import TerminalError

    with pytest.raises(TerminalError) as exc:
        PulseAdapter().submit(_req(), RunContext())
    assert exc.value.backend_code == "no_credentials"


# --- compliance: UNVERIFIED no-train fails closed (D-v2-10) -----------------------------


def _phi_req() -> OpenReadingRequest:
    return OpenReadingRequest.model_validate(
        {
            "document": {"path": "/d.pdf", "mime_type": "application/pdf"},
            "backend": {"id": "auto"},
            "compliance": {"no_train_on_data": True},
        }
    )


def test_no_train_policy_drops_pulse_by_default():
    plan = Router(build_registry(), RouterConfig()).route(_phi_req())
    assert plan.dropped["pulse"].code == "trains_unverified"
    assert "pulse" not in plan.eligible_ids


def test_allow_unverified_compliance_readmits_pulse():
    plan = Router(build_registry(), RouterConfig(allow_unverified_compliance=True)).route(
        _phi_req()
    )
    assert "pulse" in plan.eligible_ids  # UNVERIFIED no-train allowed only under the explicit flag


# --- live (keyed; skipped without PULSE_API_KEY) --------------------------------------


@pytest.mark.live
def test_live_extract():  # pragma: no cover
    from tests.live_helpers import run_live, sample_pdf_request, skip_unless_creds

    skip_unless_creds("pulse")
    resp = run_live("pulse", PulseAdapter(), sample_pdf_request("pulse"))
    assert resp.document.markdown or resp.document.pages


@pytest.mark.live
def test_live_extract_typed_fields():  # pragma: no cover
    # §10 B.4, VERIFIED live 2026-07-29: `structured_output` forwarding yields typed_fields from
    # structured_output.values (no longer deliver-or-warn). Strict now: the round-trip MUST produce
    # typed_fields with no unverified warning. Per-field confidence + citation-id → geometry
    # resolution (F1) is covered offline against the captured real shape
    # (test_pulse_normalize.test_extract_typed_fields_carry_confidence_and_resolved_citations).
    from tests.live_helpers import run_live, sample_pdf_request, skip_unless_creds

    skip_unless_creds("pulse")
    req = sample_pdf_request("pulse")
    req.extraction_schema = ExtractionSchema(
        json_schema={"type": "object", "properties": {"title": {"type": "string"}}}
    )
    resp = run_live("pulse", PulseAdapter(), req, capture="live_extract_typed_fields")
    assert resp.typed_fields, "pulse extract must return typed_fields (verified 2026-07-29)"
    assert "typed_fields_unverified" not in {w.code for w in (resp.warnings or [])}
