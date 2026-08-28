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
