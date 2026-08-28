"""Chunkr adapter — tested against documented-shape fixtures via an injected fake client (NO
network). Fixtures follow the ParseTaskResponse / ExtractTaskResponse shapes from
docs.chunkr.ai/api-references/tasks/* (accessed 2026-07-21); they upgrade to captured-live the
first time `make verify-live` runs with CHUNKR_API_KEY. Exercises the task-based POLL flow,
segment→block reassembly with canonical bbox + confidence, and extract→typed_fields flattening."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openreading.adapters.chunkr import ChunkrAdapter
from openreading.router.clock import FakeClock
from openreading.router.driver import run_to_completion
from openreading.testing import ConformanceCase, check_adapter_conformance
from openreading.types import BlockType, JobState
from openreading.types.enums import WaitMode
from openreading.types.request import OpenReadingRequest
from openreading.types.runtime import RunContext

FIX = Path(__file__).parent / "fixtures" / "chunkr"


def _fixture(name: str) -> dict:
    return json.loads((FIX / f"{name}.json").read_text())


class FakeChunkrClient:
    """Replays the documented-shape fixtures. Task creation returns Processing; get_task returns
    the terminal Succeeded response (the POLL path). Records the `config` dict actually sent to
    the vendor, so tests can assert on it (not just the resulting job.wait_mode)."""

    def __init__(self) -> None:
        self.last_config: dict | None = None

    def create_parse_task(self, document, config):
        self.last_config = config
        return {"task_id": "task_parse_1", "status": "Processing"}

    def create_extract_task(self, document, schema, config):
        self.last_config = config
        return {"task_id": "task_extract_1", "status": "Processing"}

    def get_task(self, task_id):
        return _fixture("extract") if "extract" in task_id else _fixture("parse")


def _req(**over) -> OpenReadingRequest:
    body = {
        "document": {"url": "https://example.com/loan.pdf", "mime_type": "application/pdf"},
        "backend": {"id": "chunkr"},
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


# --- conformance -----------------------------------------------------------------------


def test_chunkr_conforms():
    # Phase B.4: chunkr is remediated — the text channel is a plain projection of the segment
    # content (C1) and its requested N/D channels are delivered (C6), so both are promoted from
    # advisory to strict.
    adapter = ChunkrAdapter(client=FakeChunkrClient())
    check_adapter_conformance(
        adapter,
        [ConformanceCase(request=_req(), deterministic=True, label="parse")],
        strict_checks={"C1", "C6"},
        # Ledger T4a (R1/R2/R3): a fresh instance with its own separate fake client — proves
        # resume has no instance-affinity requirement (AC-5) and nothing is cached on self (AC-7).
        adapter_factory=lambda: ChunkrAdapter(client=FakeChunkrClient()),
    )


# --- task-based POLL + segment reassembly ----------------------------------------------


def test_submit_returns_running_poll_job():
    adapter = ChunkrAdapter(client=FakeChunkrClient())
    job = adapter.submit(_req(), RunContext())
    assert job.wait_mode is WaitMode.POLL and job.state is JobState.RUNNING
    assert job.backend_job_id == "task_parse_1"


def test_parse_reassembles_segments_to_blocks():
    resp = _run(ChunkrAdapter(client=FakeChunkrClient()), _req())
    blocks = resp.document.pages[0].blocks
    types = [b.type for b in blocks]
    assert BlockType.TITLE in types and BlockType.TABLE in types
    title = next(b for b in blocks if b.type is BlockType.TITLE)
    assert title.text == "Loan Application"
    assert title.confidence == pytest.approx(0.98)
    # markdown is the per-chunk content; text is the OCR text
    assert resp.document.markdown and "Loan Application" in resp.document.markdown
    # the Table segment's HTML content becomes the canonical grid (table_cells=D), plain-projected
    # into the text channel (tab-joined rows) with the raw HTML kept on the html channel.
    table = next(b for b in blocks if b.type is BlockType.TABLE)
    assert table.table is not None
    assert table.table.rows == [["Region", "Revenue"], ["North", "4400"]]
    assert table.text == "Region\tRevenue\nNorth\t4400"
    assert table.html and "<table>" in table.html
    assert "<table>" not in (resp.document.text or "")


def test_bbox_canonical_with_native():
    resp = _run(ChunkrAdapter(client=FakeChunkrClient()), _req())
    title = next(b for b in resp.document.pages[0].blocks if b.type is BlockType.TITLE)
    bb = title.bbox
    # left=72 / page_width=612 ≈ 0.1176; top=58 / 792 ≈ 0.0732
    assert bb.x == pytest.approx(72 / 612, abs=1e-4)
    assert bb.y == pytest.approx(58 / 792, abs=1e-4)
    assert 0.0 <= bb.x <= 1.0 and 0.0 <= bb.y <= 1.0
    assert bb.bbox_native is not None and bb.bbox_native.unit == "pixel"


def test_webhook_mode_when_callback_url_present():
    adapter = ChunkrAdapter(client=FakeChunkrClient())
    job = adapter.submit(
        _req(**{"async": {"mode": "async", "webhook_url": "https://cb/hook"}}), RunContext()
    )
    assert job.wait_mode is WaitMode.WEBHOOK and job.webhook_token == "task_parse_1"


def test_webhook_url_forwarded_to_vendor_on_parse():
    """BL-67: without this, the vendor is never told to call back and a WEBHOOK job hangs at
    "running" forever — job.wait_mode flipping to WEBHOOK alone doesn't prove the vendor knows."""
    client = FakeChunkrClient()
    adapter = ChunkrAdapter(client=client)
    adapter.submit(
        _req(**{"async": {"mode": "async", "webhook_url": "https://cb/hook"}}), RunContext()
    )
    assert client.last_config == {"webhook_url": "https://cb/hook"}


def test_webhook_url_forwarded_to_vendor_on_extract():
    client = FakeChunkrClient()
    adapter = ChunkrAdapter(client=client)
    adapter.submit(
        _req(
            backend={"id": "chunkr", "operation": "extract"},
            extraction_schema={"json_schema": {"type": "object"}},
            **{"async": {"mode": "async", "webhook_url": "https://cb/hook"}},
        ),
        RunContext(),
    )
    assert client.last_config == {"webhook_url": "https://cb/hook"}


def test_no_webhook_url_sends_empty_config():
    client = FakeChunkrClient()
    ChunkrAdapter(client=client).submit(_req(), RunContext())
    assert client.last_config == {}


# --- extract → typed_fields ------------------------------------------------------------


def test_extract_flattens_results_to_typed_fields():
    adapter = ChunkrAdapter(client=FakeChunkrClient())
    req = _req(
        backend={"id": "chunkr", "operation": "extract"},
        extraction_schema={"json_schema": {"type": "object"}},
    )
    resp = _run(adapter, req)
    assert resp.typed_fields["total"].value == "$4,400.00"
    assert resp.typed_fields["total"].confidence == pytest.approx(0.9)  # metrics "High"
    assert resp.typed_fields["region"].confidence == pytest.approx(0.6)  # "Medium"
    # the fixture's citations block (page 1) is captured onto the field (was dropped; P1)
    assert resp.typed_fields["total"].citations[0].page == 1


def test_no_credentials_is_terminal():
    from openreading.types.errors import TerminalError

    adapter = ChunkrAdapter()  # no injected client, no creds in ctx
    with pytest.raises(TerminalError) as exc:
        adapter.submit(_req(), RunContext())
    assert exc.value.backend_code == "no_credentials"


# --- live (keyed; skipped without CHUNKR_API_KEY) --------------------------------------


@pytest.mark.live
def test_live_parse():  # pragma: no cover
    from tests.live_helpers import run_live, sample_pdf_request, skip_unless_creds

    skip_unless_creds("chunkr")
    resp = run_live("chunkr", ChunkrAdapter(), sample_pdf_request("chunkr"))
    assert resp.document.pages or resp.document.markdown
