"""Tesseract adapter — fault-injection for the local-adapter seams the happy-path OCR tests skip
(adapter was 87%): health's three degraded branches (no pytesseract, no binary, a version probe
that raises), _rasterize's TerminalError passthrough vs. wrap-the-unexpected, the
no-bytes-and-no-path guard, path-only and image (non-PDF) input, an OCR subprocess that fails, the
page-selection edges, and TSV rows tesseract emits for nothing (blank text / conf -1). All offline
— the injected runner means no `tesseract` binary is needed."""

from __future__ import annotations

import base64
import sys
from io import BytesIO

import pytest

from openreading.adapters.tesseract import TesseractAdapter
from openreading.adapters.tesseract import adapter as adapter_mod
from openreading.testing.sample_pdf import build_sample_pdf
from openreading.types.errors import TerminalError
from openreading.types.request import OpenReadingRequest
from openreading.types.runtime import RunContext

pytest.importorskip("pytesseract", reason="tesseract extra not installed")
pytest.importorskip("PIL")

PDF_B64 = base64.b64encode(build_sample_pdf()).decode()


def _png_b64(width: int = 120, height: int = 40) -> str:
    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (width, height), "white").save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _req(**over) -> OpenReadingRequest:
    body = {
        "document": {"bytes_base64": PDF_B64, "mime_type": "application/pdf"},
        "backend": {"id": "tesseract"},
        "pages": {"ranges": [{"start": 1, "end": 1}]},
    }
    body.update(over)
    return OpenReadingRequest.model_validate(body)


def _tsv(**over) -> dict:
    """One page-level row + one OCR'd word ("Loan"), overridable per test."""
    base = {
        "level": [1, 5],
        "block_num": [0, 1],
        "par_num": [0, 1],
        "line_num": [0, 1],
        "word_num": [0, 1],
        "left": [0, 10],
        "top": [0, 10],
        "width": [100, 20],
        "height": [30, 10],
        "conf": [-1, 96.0],
        "text": ["", "Loan"],
    }
    base.update(over)
    return base


class _OneWordRunner:
    def __init__(self, tsv: dict | None = None) -> None:
        self._tsv = tsv if tsv is not None else _tsv()

    def image_to_data(self, image, lang, dpi, timeout):
        return self._tsv

    def version(self):
        return "5.5.2-fake"


class _BoomRunner(_OneWordRunner):
    def image_to_data(self, image, lang, dpi, timeout):
        raise RuntimeError("tesseract exited with status 1")


class _BoomVersionRunner(_OneWordRunner):
    def version(self):
        raise RuntimeError("no binary to ask")


def _run(adapter, req):
    ctx = RunContext()
    return adapter.normalize(adapter.submit(req, ctx), ctx, req)


# ---- health ------------------------------------------------------------------------------------


def test_health_reports_missing_pytesseract(monkeypatch):
    monkeypatch.setitem(sys.modules, "pytesseract", None)
    h = TesseractAdapter(runner=_OneWordRunner()).health()
    assert not h.ready and any("pytesseract" in d for d in h.missing_deps)


def test_health_reports_the_missing_binary(monkeypatch):
    # only the default (real) runner shells out, so only it is gated on the binary being present
    monkeypatch.setattr(adapter_mod.shutil, "which", lambda _name: None)
    h = TesseractAdapter().health()
    assert not h.ready and any("tesseract binary" in d for d in h.missing_deps)


def test_health_reports_the_runner_version():
    h = TesseractAdapter(runner=_OneWordRunner()).health()
    assert h.ready and h.version == "5.5.2-fake"


def test_health_stays_ready_when_the_version_probe_raises():
    h = TesseractAdapter(runner=_BoomVersionRunner()).health()
    assert h.ready and h.version is None  # an unanswerable version probe is not an outage


# ---- input resolution ----------------------------------------------------------------------


def test_missing_bytes_and_path_is_unsupported_input():
    adapter = TesseractAdapter(runner=_OneWordRunner())
    req = _req(document={"url": "https://example.com/scan.png", "mime_type": "image/png"})
    with pytest.raises(TerminalError) as exc:
        adapter.submit(req, RunContext())
    # the guard's own TerminalError passes through submit untouched — never re-wrapped as
    # "rasterization failed" with a backend_code of "TerminalError"
    assert exc.value.backend_code == "unsupported_input"
    assert "rasterization failed" not in str(exc.value)


def test_unreadable_bytes_are_wrapped_as_a_terminal_rasterization_failure():
    adapter = TesseractAdapter(runner=_OneWordRunner())
    junk = base64.b64encode(b"not an image at all").decode()
    req = _req(document={"bytes_base64": junk, "mime_type": "image/png"})
    with pytest.raises(TerminalError) as exc:
        adapter.submit(req, RunContext())
    assert "rasterization failed" in str(exc.value)
    assert exc.value.backend_code == "UnidentifiedImageError"  # the real cause is kept


def test_path_only_input_is_rasterized(tmp_path):
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(build_sample_pdf())
    adapter = TesseractAdapter(runner=_OneWordRunner())
    resp = _run(adapter, _req(document={"path": str(pdf)}))  # no mime: the .pdf suffix decides
    assert resp.document.page_count == 1 and resp.document.text == "Loan"


def test_image_input_skips_the_pdf_rasterizer():
    adapter = TesseractAdapter(runner=_OneWordRunner())
    req = _req(document={"bytes_base64": _png_b64(), "mime_type": "image/png"})
    resp = _run(adapter, req)
    page = resp.document.pages[0]
    assert (page.width, page.height) == (120, 40)  # the image's own pixel dims, no re-rasterization
    assert page.page_number == 1


# ---- OCR failure -----------------------------------------------------------------------------


def test_ocr_subprocess_failure_is_terminal():
    adapter = TesseractAdapter(runner=_BoomRunner())
    with pytest.raises(TerminalError) as exc:
        adapter.submit(_req(), RunContext())
    assert "tesseract failed" in str(exc.value)
    assert exc.value.backend_code == "RuntimeError"


# ---- page selection --------------------------------------------------------------------------


def test_no_page_selection_ocrs_every_page():
    adapter = TesseractAdapter(runner=_OneWordRunner())
    req = OpenReadingRequest.model_validate(
        {
            "document": {"bytes_base64": PDF_B64, "mime_type": "application/pdf"},
            "backend": {"id": "tesseract"},
        }
    )
    resp = _run(adapter, req)
    assert [p.page_number for p in resp.document.pages] == [1, 2]


def test_max_pages_truncates_the_selection():
    adapter = TesseractAdapter(runner=_OneWordRunner())
    resp = _run(adapter, _req(pages={"ranges": [{"start": 1, "end": 2}], "max_pages": 1}))
    assert [p.page_number for p in resp.document.pages] == [1]


# ---- TSV rows that carry no text ---------------------------------------------------------------


def test_blank_and_unrecognized_words_are_dropped():
    # tesseract emits rows for whitespace and for regions it could not read (conf -1); neither may
    # become a block — a conf -1 word would otherwise poison the line's MIN-confidence floor.
    tsv = _tsv(
        level=[1, 5, 5, 5],
        block_num=[0, 1, 1, 1],
        par_num=[0, 1, 1, 1],
        line_num=[0, 1, 1, 1],
        word_num=[0, 1, 2, 3],
        left=[0, 10, 40, 70],
        top=[0, 10, 10, 10],
        width=[100, 20, 20, 20],
        height=[30, 10, 10, 10],
        conf=[-1, 96.0, 90.0, -1],
        text=["", "Loan", "   ", "unread"],
    )
    adapter = TesseractAdapter(runner=_OneWordRunner(tsv))
    resp = _run(adapter, _req())
    blocks = resp.document.pages[0].blocks
    assert len(blocks) == 1 and blocks[0].text == "Loan"
    assert blocks[0].confidence == pytest.approx(0.96)
