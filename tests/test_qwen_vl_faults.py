"""Qwen-VL adapter — fault-injection for the branches the happy-path fixtures skip: endpoint
resolution, BOTH submit() failure sites routed through the one `_map_error` (the rasterizer runs
before the endpoint is ever reached, so only the message context differs), input variants (raw
image, path on disk, neither), the default page selection, and normalize tolerating malformed
model output (unparseable extraction JSON, missing/garbled data-bbox, text outside a div). All
offline via an injected fake client."""

from __future__ import annotations

import base64
from io import BytesIO

import pytest
from PIL import Image, UnidentifiedImageError

from openreading.adapters.qwen_vl import QwenVLAdapter
from openreading.types import BlockType
from openreading.types.errors import RetryableError, TerminalError
from openreading.types.request import OpenReadingRequest
from openreading.types.runtime import RunContext

fitz = pytest.importorskip("fitz", reason="pymupdf (rasterizer) not installed")

_ONE_BLOCK_HTML = '<div class="text" data-bbox="0 0 8 5">Hi</div>'


def _pdf(pages: int) -> str:
    doc = fitz.open()
    for _ in range(pages):
        doc.new_page(width=200, height=100)
    return base64.b64encode(doc.tobytes()).decode()


def _png(width: int = 8, height: int = 5) -> str:
    buf = BytesIO()
    Image.new("RGB", (width, height), "white").save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _req(**over) -> OpenReadingRequest:
    body = {
        "document": {"bytes_base64": _pdf(1), "mime_type": "application/pdf"},
        "backend": {"id": "qwen-vl", "type": "self_hosted_model"},
    }
    body.update(over)
    return OpenReadingRequest.model_validate(body)


class _ContentClient:
    """Replays one model completion (or raises), so normalize can be driven from arbitrary model
    output instead of a captured fixture."""

    def __init__(self, content: str = _ONE_BLOCK_HTML, raise_exc: Exception | None = None) -> None:
        self._content = content
        self._raise = raise_exc
        self.calls: list[tuple[str, str, str]] = []

    def chat(self, image_data_url, prompt, model):
        if self._raise is not None:
            raise self._raise
        self.calls.append((prompt, model, image_data_url))
        return self._content, "stop"


def _run(adapter, req):
    ctx = RunContext()
    return adapter.normalize(adapter.submit(req, ctx), ctx, req)


# ---- endpoint resolution -------------------------------------------------------------------


def test_health_is_ready_but_names_the_endpoint_requirement():
    health = QwenVLAdapter().health()  # no injected client
    assert health.ready and "endpoint" in (health.detail or "")


def test_missing_endpoint_is_terminal():
    with pytest.raises(TerminalError) as exc:
        QwenVLAdapter().submit(_req(), RunContext())
    assert exc.value.backend_code == "no_endpoint"


# ---- the two submit() failure sites share one _map_error ------------------------------------


def test_rasterizer_failure_is_terminal_and_names_the_stage():
    # Reached before any endpoint call: the bytes claim PNG but no decoder recognises them.
    adapter = QwenVLAdapter(client=_ContentClient())
    req = _req(document={"bytes_base64": "bm90LWFuLWltYWdl", "mime_type": "image/png"})
    with pytest.raises(TerminalError) as exc:
        adapter.submit(req, RunContext())
    assert exc.value.backend_code == UnidentifiedImageError.__name__
    assert str(exc.value).startswith("rasterization failed: ")


def test_endpoint_failure_is_terminal_without_the_rasterizer_context():
    adapter = QwenVLAdapter(client=_ContentClient(raise_exc=RuntimeError("connection reset")))
    with pytest.raises(TerminalError) as exc:
        adapter.submit(_req(), RunContext())
    assert exc.value.backend_code == "RuntimeError"
    assert str(exc.value) == "connection reset"  # same helper, no rasterizer prefix


def test_endpoint_taxonomy_errors_are_not_remapped():
    boom = TerminalError("key rejected", backend_code="auth_rejected")
    adapter = QwenVLAdapter(client=_ContentClient(raise_exc=boom))
    with pytest.raises(TerminalError) as exc:
        adapter.submit(_req(), RunContext())
    assert exc.value.backend_code == "auth_rejected"


def test_map_error_never_rewraps_a_taxonomy_error():
    # The single decision point must be safe at every call site, context or not.
    boom = RetryableError("model loading", backend_code="503", retry_after=5.0)
    adapter = QwenVLAdapter()
    assert adapter._map_error(boom) is boom
    assert adapter._map_error(boom, "rasterization failed") is boom


# ---- input variants --------------------------------------------------------------------------


def test_neither_bytes_nor_path_is_unsupported_input():
    adapter = QwenVLAdapter(client=_ContentClient())
    with pytest.raises(TerminalError) as exc:
        adapter.submit(_req(document={"url": "https://example.test/doc.pdf"}), RunContext())
    assert exc.value.backend_code == "unsupported_input"


def test_path_input_is_read_from_disk(tmp_path):
    pdf = tmp_path / "loan.pdf"
    pdf.write_bytes(base64.b64decode(_pdf(1)))
    client = _ContentClient()
    QwenVLAdapter(client=client).submit(
        _req(document={"path": str(pdf), "mime_type": "application/pdf"}), RunContext()
    )
    assert client.calls[0][2].startswith("data:image/png;base64,")  # rasterized, not sent as PDF


def test_raw_image_input_is_sent_verbatim_at_its_own_dimensions():
    client = _ContentClient()
    resp = _run(
        QwenVLAdapter(client=client),
        _req(document={"bytes_base64": _png(8, 5), "mime_type": "image/png"}),
    )
    assert client.calls[0][2].startswith("data:image/png;base64,")
    page = resp.document.pages[0]
    assert (page.width, page.height) == (8, 5)  # image dims, not a rasterizer DPI product


def test_every_page_is_rendered_when_no_page_selection_is_given():
    client = _ContentClient()
    resp = _run(
        QwenVLAdapter(client=client),
        _req(document={"bytes_base64": _pdf(2), "mime_type": "application/pdf"}),
    )
    assert len(client.calls) == 2
    assert [p.page_number for p in resp.document.pages] == [1, 2]


# ---- normalize tolerates malformed model output ------------------------------------------------


def test_unparseable_extraction_json_yields_no_typed_fields():
    req = _req(extraction_schema={"json_schema": {"type": "object", "properties": {"a": {}}}})
    resp = _run(QwenVLAdapter(client=_ContentClient("I could not read the document.")), req)
    assert resp.typed_fields is None  # nothing invented from a non-JSON completion


def test_json_array_extraction_yields_no_typed_fields():
    req = _req(extraction_schema={"json_schema": {"type": "object", "properties": {"a": {}}}})
    resp = _run(QwenVLAdapter(client=_ContentClient('```json\n["a", "b"]\n```')), req)
    assert resp.typed_fields is None  # a JSON array is not a field map


def test_missing_and_garbled_data_bbox_yield_no_geometry():
    html = (
        '<div class="text">no bbox attribute</div>'
        '<div class="text" data-bbox="left top right bottom">garbled</div>'
    )
    resp = _run(QwenVLAdapter(client=_ContentClient(html)), _req())
    blocks = resp.document.pages[0].blocks
    assert len(blocks) == 2 and all(b.bbox is None for b in blocks)


def test_text_outside_a_div_is_ignored():
    resp = _run(QwenVLAdapter(client=_ContentClient("preamble" + _ONE_BLOCK_HTML)), _req())
    blocks = resp.document.pages[0].blocks
    assert [b.text for b in blocks] == ["Hi"]
    assert "preamble" not in (resp.document.text or "")


def test_unknown_div_class_falls_back_to_text():
    resp = _run(
        QwenVLAdapter(client=_ContentClient('<div class="watermark" data-bbox="0 0 8 5">W</div>')),
        _req(),
    )
    block = resp.document.pages[0].blocks[0]
    assert block.type is BlockType.TEXT and block.native_type == "div.watermark"
