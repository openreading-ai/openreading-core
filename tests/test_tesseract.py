"""Tesseract adapter — EXECUTED for real: rasterizes a page of the generated sample PDF and runs
the system `tesseract` binary (skipped if pytesseract or the binary is absent). Asserts conformance
+ that real OCR recovers the title text, carries native per-word confidence, and emits pixel@dpi
canonical bboxes. A tiny fake runner also drives the TSV→blocks mapping deterministically."""

from __future__ import annotations

import base64
import shutil

import pytest

from openreading.adapters.tesseract import TesseractAdapter
from openreading.adapters.tesseract.adapter import _DEFAULT_TIMEOUT_S
from openreading.testing import ConformanceCase, check_adapter_conformance
from openreading.testing.sample_pdf import build_sample_pdf
from openreading.testing.tesseract_probe import tesseract_ocr_works
from openreading.types import BlockType, NativeUnit
from openreading.types.request import OpenReadingRequest
from openreading.types.runtime import RunContext

pytest.importorskip("pytesseract", reason="tesseract extra not installed")
pytest.importorskip("PIL")
# BL-170: `shutil.which` proves the file exists, not that it OCRs — kept here only for
# test_health_reports_binary_presence, which asserts health()'s own (intentionally cheap)
# which-based readiness check, not real OCR capability.
_HAS_BINARY = shutil.which("tesseract") is not None
_TESSERACT_WORKS = tesseract_ocr_works()

PDF_B64 = base64.b64encode(build_sample_pdf()).decode()


def _req(**over) -> OpenReadingRequest:
    body = {
        "document": {"bytes_base64": PDF_B64, "mime_type": "application/pdf"},
        "backend": {"id": "tesseract", "type": "oss_library"},
        "pages": {"ranges": [{"start": 1, "end": 1}]},  # OCR page 1 only (keeps tests fast)
    }
    body.update(over)
    return OpenReadingRequest.model_validate(body)


def _run(adapter, req):
    ctx = RunContext()
    return adapter.normalize(adapter.submit(req, ctx), ctx, req)


# --- a deterministic fake runner drives the TSV → blocks mapping (no binary needed) ------


class FakeRunner:
    def image_to_data(self, image, lang, dpi, timeout):
        # two words on one line: "Loan Application" at ~150dpi pixels
        return {
            "level": [1, 2, 3, 4, 5, 5],
            "block_num": [0, 1, 1, 1, 1, 1],
            "par_num": [0, 0, 1, 1, 1, 1],
            "line_num": [0, 0, 0, 1, 1, 1],
            "word_num": [0, 0, 0, 0, 1, 2],
            "left": [0, 150, 150, 150, 150, 260],
            "top": [0, 100, 100, 100, 100, 100],
            "width": [1275, 300, 300, 300, 100, 190],
            "height": [1650, 40, 40, 40, 40, 40],
            "conf": [-1, -1, -1, -1, 96.5, 95.0],
            "text": ["", "", "", "", "Loan", "Application"],
        }

    def image_to_string(self, image, lang, timeout):
        return "Loan Application"

    def version(self):
        return "5.5.2-fake"


def test_tsv_mapping_with_fake_runner():
    adapter = TesseractAdapter(runner=FakeRunner())
    resp = _run(adapter, _req())
    blocks = resp.document.pages[0].blocks
    line = next(b for b in blocks if b.type is BlockType.TEXT)
    assert line.text == "Loan Application"
    # native per-word confidence aggregated via MIN (a usable quality floor; mean hides one bad word)
    assert line.confidence == pytest.approx(min(96.5, 95.0) / 100.0)
    # pixel bbox at dpi on a 1275x1650 px page (8.5x11in @150dpi)
    assert line.bbox.bbox_native.unit is NativeUnit.PIXEL
    assert line.bbox.bbox_native.dpi == 150
    assert line.bbox.x == pytest.approx(150 / 1275, abs=1e-4)
    assert resp.document.pages[0].unit.value == "pixel"


def test_conformance_with_fake_runner():
    # Phase B.5: tesseract is remediated — its text channel is a single-pass plain projection (C1)
    # and its requested N/D channels are delivered (C6), so both promote from advisory to strict.
    check_adapter_conformance(
        TesseractAdapter(runner=FakeRunner()),
        [ConformanceCase(request=_req(), deterministic=True, label="ocr")],
        strict_checks={"C1", "C6"},
        # Ledger T4a §6: tesseract is one of the 5 untouched-this-tranche adapters — run through
        # R1/R2 too (not assumed), confirming the now-v2 declaration of protocol_version=2 is
        # warranted — R1/R2 genuinely pass, not merely asserted.
        adapter_factory=lambda: TesseractAdapter(runner=FakeRunner()),
    )


# --- Phase B.5: single-pass text/blocks coherence, source page numbers, escape + min conf ----


class TwoLineFake:
    """Two OCR lines, one carrying markdown metacharacters, plus a SENTINEL image_to_string that
    must never reach the text channel — proving `document.text` is derived from the SAME TSV that
    produces the blocks (single pass), not a second differently-configured OCR pass."""

    def image_to_data(self, image, lang, dpi, timeout):
        # line 1: "Balance [DRAFT]" (confs 90/80), line 2: "Amount 100" (confs 95/99)
        return {
            "level": [1, 5, 5, 5, 5],
            "block_num": [0, 1, 1, 1, 1],
            "par_num": [0, 1, 1, 1, 1],
            "line_num": [0, 1, 1, 2, 2],
            "word_num": [0, 1, 2, 1, 2],
            "left": [0, 100, 310, 100, 260],
            "top": [0, 100, 100, 160, 160],
            "width": [1275, 200, 200, 150, 80],
            "height": [1650, 40, 40, 40, 40],
            "conf": [-1, 90.0, 80.0, 95.0, 99.0],
            "text": ["", "Balance", "[DRAFT]", "Amount", "100"],
        }

    def image_to_string(self, image, lang, timeout):
        return "SENTINEL_STRING_API_TEXT that must not appear in the text channel"

    def version(self):
        return "5.5.2-fake"


def test_document_text_derived_from_same_tsv_as_blocks_single_pass():
    # P1: `document.text` comes from the SAME TSV that produces the blocks (single OCR pass), not
    # the second image_to_string pass — so text and blocks are coherent (C11) and OCR cost halves.
    adapter = TesseractAdapter(runner=TwoLineFake())
    resp = _run(adapter, _req())
    assert "SENTINEL" not in (resp.document.text or "")  # not sourced from image_to_string
    assert resp.document.text == "Balance [DRAFT]\nAmount 100"
    spine = "\n".join(b.text or "" for b in resp.document.pages[0].blocks)
    assert resp.document.text == spine  # the plain text channel IS the block spine


def test_markdown_is_escaped_not_an_unescaped_copy_of_text():
    # P2: the markdown channel escapes literal OCR content (a stray "[" can't be reparsed as
    # markup); the text channel keeps the raw characters. They must no longer be byte-identical.
    adapter = TesseractAdapter(runner=TwoLineFake())
    resp = _run(adapter, _req())
    assert "[DRAFT]" in (resp.document.text or "")  # text keeps literal brackets
    assert "\\[DRAFT\\]" in (resp.document.markdown or "")  # markdown escapes them
    assert resp.document.markdown != resp.document.text


def test_min_confidence_aggregation_over_line_words():
    # P2: per-line confidence is the MIN of member word confidences (a usable quality floor).
    adapter = TesseractAdapter(runner=TwoLineFake())
    resp = _run(adapter, _req())
    first = resp.document.pages[0].blocks[0]
    assert first.text == "Balance [DRAFT]"
    assert first.confidence == pytest.approx(min(90.0, 80.0) / 100.0)  # 0.80, not the mean 0.85


def test_source_page_numbers_preserved_under_subsetting():
    # P0/C9: requesting page 2 must REPORT page 2 (not renumber the selected page to 1); the block
    # geometry's bbox.page must also carry the SOURCE document page number.
    adapter = TesseractAdapter(runner=TwoLineFake())
    resp = _run(adapter, _req(pages={"ranges": [{"start": 2, "end": 2}]}))
    assert [p.page_number for p in resp.document.pages] == [2]
    block = resp.document.pages[0].blocks[0]
    assert block.bbox is not None and block.bbox.page == 2


def test_selected_pages_huge_end_is_cheap():
    """M3: end=10**12 must clamp to the document's page count, not iterate the span.
    (Unfixed code hangs here — that IS the failure mode.)"""
    req = OpenReadingRequest.model_validate(
        {
            "document": {"bytes_base64": "aGk="},
            "backend": {"id": "tesseract", "type": "oss_library"},
            "pages": {"ranges": [{"start": 1, "end": 10**12}]},
        }
    )
    assert TesseractAdapter()._selected_pages(3, req) == [0, 1, 2]


def test_channel_provenance_populated():
    adapter = TesseractAdapter(runner=FakeRunner())
    resp = _run(adapter, _req())
    prov = resp.channel_provenance or {}
    assert prov.get("text") == "native" and prov.get("markdown") == "derived"
    assert prov.get("blocks") == "native"


# --- real execution against the system tesseract binary ---------------------------------


@pytest.mark.skipif(not _TESSERACT_WORKS, reason="system tesseract binary not installed or broken")
def test_real_tesseract_recovers_title_text():
    adapter = TesseractAdapter()  # real pytesseract runner
    resp = _run(adapter, _req())
    text = (resp.document.text or "").lower()
    # real OCR of the rasterized page 1 recovers the title (allow OCR imperfection on one token)
    assert "openreading" in text or "test" in text or "document" in text
    # every block carries a real [0,1] confidence and a pixel bbox
    blocks = resp.document.pages[0].blocks
    assert blocks, "expected OCR text blocks"
    for b in blocks:
        assert b.confidence is not None and 0.0 <= b.confidence <= 1.0
        assert b.bbox.bbox_native.unit is NativeUnit.PIXEL


@pytest.mark.skipif(not _TESSERACT_WORKS, reason="system tesseract binary not installed or broken")
def test_real_tesseract_conforms():
    check_adapter_conformance(
        TesseractAdapter(),
        [ConformanceCase(request=_req(), deterministic=True, label="real-ocr")],
        strict_checks={"C1", "C6"},
    )


def test_health_reports_binary_presence():
    h = TesseractAdapter().health()
    if _HAS_BINARY:
        assert h.ready
    else:
        assert not h.ready and any("tesseract" in d for d in h.missing_deps)


def test_typed_fields_and_tables_warned_when_requested():
    adapter = TesseractAdapter(runner=FakeRunner())
    resp = _run(adapter, _req(outputs={"typed_fields": True}, features={"tables": True}))
    codes = {w.field for w in (resp.warnings or [])}
    assert "typed_fields" in codes and "tables" in codes


# --- BL-154: ctx.deadline_ms -> subprocess-timeout conversion (arithmetic on valid input, not
# an injected fault, so this lives here rather than in test_tesseract_faults.py) --------------


class RecordingRunner:
    """Records the `timeout` kwarg actually forwarded to `image_to_data`; `submit()` never reads
    the returned TSV's contents, so an empty-but-well-shaped dict is enough."""

    def __init__(self) -> None:
        self.timeouts: list[int] = []

    def image_to_data(self, image, lang, dpi, timeout):
        self.timeouts.append(timeout)
        return {
            "level": [],
            "block_num": [],
            "par_num": [],
            "line_num": [],
            "word_num": [],
            "left": [],
            "top": [],
            "width": [],
            "height": [],
            "conf": [],
            "text": [],
        }

    def version(self):
        return "5.5.2-fake"


@pytest.mark.parametrize(
    ("deadline_ms", "expected_timeout"),
    [
        # None: no deadline supplied -> the 60s default, unchanged.
        (None, _DEFAULT_TIMEOUT_S),
        # 0: this council's own "fail fast, no time left" signal. Must NOT become the 60s default
        # (the pre-fix bug) and must NOT be passed through as literal 0 either — pytesseract's own
        # timeout_manager treats a falsy timeout as "no timeout at all" (verified against the
        # installed pytesseract==0.3.13: `if not seconds: yield proc.communicate()[1]`, no timeout
        # kwarg), i.e. UNLIMITED time, the exact opposite of the signal. Floors to 1.
        (0, 1),
        # sub-second remaining budget (1 <= deadline_ms < 1000): truncates to 0 whole seconds,
        # which must floor to 1 for the same reason as the deadline_ms=0 case above, not 60s.
        (500, 1),
        (999, 1),
        # multi-second budgets convert to whole seconds via floor division, unaffected by the fix.
        (1000, 1),
        (1500, 1),
        (2500, 2),
    ],
)
def test_submit_forwards_deadline_ms_as_timeout_seconds(deadline_ms, expected_timeout):
    runner = RecordingRunner()
    adapter = TesseractAdapter(runner=runner)
    adapter.submit(_req(), RunContext(deadline_ms=deadline_ms))
    assert runner.timeouts == [expected_timeout]
