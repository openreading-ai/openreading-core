"""PyMuPDF adapter — fault injection for the load failures the happy-path fixtures never reach.

The document load (`fitz.open`) once sat OUTSIDE submit()'s try/except, so a corrupt or non-PDF
stream escaped as a raw `fitz.FileDataError` (a RuntimeError) instead of a mapped TerminalError:
undocumented at every surface, and a bare HTTP 500 at /v1/parse because the server's adapter-error
tuple only knows the four taxonomy categories. These pin the mapping at each layer that can reach
submit() — the adapter directly, `openreading.run()`, and `execute_plan` (route --run / a strategy
cascade landing on pymupdf). All offline; no keys, no network.
"""

from __future__ import annotations

import base64

import pytest

import openreading
from openreading.adapters.pymupdf import PyMuPDFAdapter
from openreading.credentials import EnvCredentialBroker
from openreading.router.executor import execute_plan
from openreading.router.router import RoutePlan
from openreading.types.errors import PlanExhaustedError, TerminalError
from openreading.types.request import OpenReadingRequest
from openreading.types.runtime import RunContext

fitz = pytest.importorskip("fitz", reason="pymupdf extra not installed")

CORRUPT_BYTES = b"%PDF-1.7 this is not actually a PDF body at all\n"
CORRUPT_B64 = base64.b64encode(CORRUPT_BYTES).decode()


def _req(**over) -> OpenReadingRequest:
    body: dict = {
        "document": {"bytes_base64": CORRUPT_B64, "mime_type": "application/pdf"},
        "backend": {"id": "pymupdf", "type": "oss_library"},
    }
    body.update(over)
    return OpenReadingRequest.model_validate(body)


def _submit(req: OpenReadingRequest):
    return PyMuPDFAdapter().submit(req, RunContext())


# --- the load itself is mapped -----------------------------------------------------------


def test_corrupt_bytes_raise_terminal_not_raw_fitz_error():
    with pytest.raises(TerminalError) as exc:
        _submit(_req())
    assert exc.value.backend_code == "FileDataError"  # the real fitz class, not a placeholder
    assert isinstance(exc.value.__cause__, fitz.FileDataError)  # original preserved for audit


@pytest.mark.parametrize(
    ("contents", "expected_code"),
    [(None, "FileNotFoundError"), (b"", "EmptyFileError"), (CORRUPT_BYTES, "FileDataError")],
)
def test_path_arm_load_failures_carry_the_fitz_class(tmp_path, contents, expected_code):
    # the path arm of _open is mapped too, and each fitz class reaches the caller distinctly
    doc_path = tmp_path / "loan.pdf"
    if contents is not None:
        doc_path.write_bytes(contents)
    with pytest.raises(TerminalError) as exc:
        _submit(_req(document={"path": str(doc_path), "mime_type": "application/pdf"}))
    assert exc.value.backend_code == expected_code


def test_a_format_this_backend_cannot_read_is_named_before_the_open(tmp_path):
    """A .txt and a .docx used to fail as `PyMuPDF failed: Failed to open stream`, the same line a
    truncated PDF gives, naming neither the problem nor the fix. The folder path already answers
    `skip_reason: unsupported_format`, so the single-document path answers the same way.

    Both request shapes are checked because they are the two a caller can arrive in: the CLI and
    the Python API read the file into `bytes_base64` and carry the name in `filename`, while a
    hand-built request can still hold `path`."""
    notes = tmp_path / "notes.txt"
    notes.write_text("hello\n")
    shapes = [
        {"path": str(notes), "mime_type": "text/plain"},
        {"bytes_base64": CORRUPT_B64, "filename": "notes.txt", "mime_type": "text/plain"},
    ]
    for document in shapes:
        with pytest.raises(TerminalError) as exc:
            _submit(_req(document=document))
        assert exc.value.backend_code == "unsupported_format"
        # the file by name, and the formats read from the descriptor rather than restated
        assert "notes.txt" in str(exc.value)
        assert "pdf, xps, epub, mobi, cbz, svg" in str(exc.value)


def test_a_corrupt_file_of_a_supported_format_keeps_the_pymupdf_error(tmp_path):
    # The extension is all the format check can see, so a .pdf whose bytes are junk must still
    # reach fitz and come back with the fitz class the batch envelope reports.
    doc_path = tmp_path / "loan.pdf"
    doc_path.write_bytes(CORRUPT_BYTES)
    with pytest.raises(TerminalError) as exc:
        _submit(_req(document={"path": str(doc_path), "mime_type": "application/pdf"}))
    assert exc.value.backend_code == "FileDataError"


def test_no_document_source_keeps_its_unsupported_input_code():
    # _open's OWN TerminalError must pass through unwrapped — not re-mapped to
    # backend_code="TerminalError" by the generic handler.
    with pytest.raises(TerminalError) as exc:
        _submit(_req(document={"url": "https://example.com/loan.pdf"}))
    assert exc.value.backend_code == "unsupported_input"


# --- the same failure through the surfaces above the adapter -----------------------------


def test_run_api_surfaces_terminal_error():
    with pytest.raises(TerminalError) as exc:
        openreading.run(CORRUPT_BYTES, backend="pymupdf")
    assert exc.value.backend_code == "FileDataError"


def test_executor_chain_records_the_failure_instead_of_leaking():
    plan = RoutePlan(chosen=PyMuPDFAdapter())
    with pytest.raises(PlanExhaustedError) as exc:
        execute_plan(plan, _req(), broker=EnvCredentialBroker({}))
    assert exc.value.trail == [
        {"backend": "pymupdf", "category": "TerminalError", "code": "FileDataError"}
    ]
