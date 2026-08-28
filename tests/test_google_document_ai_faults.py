"""Google Document AI adapter — fault-injection for the branches the happy-path proto fixture
skips (adapter was 91%): input variants (path read, url-only rejection), health without the SDK,
the already-typed-error passthrough, every _map_error class (gRPC status name AND bare exception
class name), and the degenerate-proto seams normalize must survive — formFields, an untyped nested
property, a third repeat of a nested sub-type, and a table with no text anchor anywhere. Offline."""

from __future__ import annotations

import base64
import sys

import pytest

from openreading.adapters.google_document_ai import GoogleDocumentAIAdapter
from openreading.credentials import EnvCredentialBroker, build_run_context
from openreading.readiness import auth_hinted
from openreading.types import BlockType
from openreading.types.errors import RetryableError, TerminalError
from openreading.types.request import OpenReadingRequest
from openreading.types.runtime import RunContext

DOC_B64 = base64.b64encode(b"%PDF-1.7 fake").decode()
# the adapter reads project_id/location/processor_id from ctx.runtime, not ctx.credentials
CREDS = {"project_id": "p", "location": "us", "processor_id": "proc"}


def _req(**over) -> OpenReadingRequest:
    body = {
        "document": {"bytes_base64": DOC_B64, "mime_type": "application/pdf"},
        "backend": {"id": "google-document-ai"},
    }
    body.update(over)
    return OpenReadingRequest.model_validate(body)


class _RecordingClient:
    """Records the bytes handed to :process; `exc` turns the call into a fault."""

    def __init__(self, doc: dict | None = None, exc: Exception | None = None) -> None:
        self._doc = doc if doc is not None else {"text": "", "pages": []}
        self._exc = exc
        self.content: bytes | None = None

    def process(self, processor_name, content, mime_type):
        if self._exc is not None:
            raise self._exc
        self.content = content
        return {"document": self._doc}


def _run_doc(doc: dict, **over):
    adapter = GoogleDocumentAIAdapter(client=_RecordingClient(doc))
    req = _req(**over)
    ctx = RunContext(runtime=CREDS)
    return adapter.normalize(adapter.submit(req, ctx), ctx, req)


def _grpc_error(status_name: str, message: str = "upstream said no") -> Exception:
    """A google-api-core style error: the status rides `.code.name`, not the class name."""

    class _Code:
        name = status_name

    class _GoogleAPICallError(Exception):
        code = _Code()

    return _GoogleAPICallError(message)


# ---- input variants ---------------------------------------------------------------------------


def test_path_input_is_read_from_disk(tmp_path):
    pdf = tmp_path / "loan.pdf"
    pdf.write_bytes(b"%PDF-1.7 on disk")
    client = _RecordingClient()
    adapter = GoogleDocumentAIAdapter(client=client)
    req = _req(document={"path": str(pdf), "mime_type": "application/pdf"})
    adapter.submit(req, RunContext(runtime=CREDS))
    assert client.content == b"%PDF-1.7 on disk"  # bytes read from the path, not skipped


def test_url_only_input_is_unsupported():
    adapter = GoogleDocumentAIAdapter(client=_RecordingClient())
    req = _req(document={"url": "https://example.com/loan.pdf", "mime_type": "application/pdf"})
    with pytest.raises(TerminalError) as exc:
        adapter.submit(req, RunContext(runtime=CREDS))
    assert exc.value.backend_code == "unsupported_input"  # :process has no fetch-by-URL


# ---- health ------------------------------------------------------------------------------------


def test_health_reports_the_missing_sdk(monkeypatch):
    monkeypatch.setitem(sys.modules, "google.cloud.documentai", None)
    h = GoogleDocumentAIAdapter().health()  # no injected client → the SDK import decides
    assert not h.ready and any("google-cloud-documentai" in d for d in h.missing_deps)


# ---- submit error mapping ---------------------------------------------------------------------


def test_submit_reraises_taxonomy_errors_unchanged():
    boom = TerminalError("no", backend_code="auth_rejected")
    adapter = GoogleDocumentAIAdapter(client=_RecordingClient(exc=boom))
    with pytest.raises(TerminalError) as exc:
        adapter.submit(_req(), RunContext(runtime=CREDS))
    assert exc.value.backend_code == "auth_rejected"  # not remapped to "TerminalError"


def test_submit_maps_unexpected_error():
    adapter = GoogleDocumentAIAdapter(client=_RecordingClient(exc=ValueError("boom")))
    with pytest.raises(TerminalError) as exc:
        adapter.submit(_req(), RunContext(runtime=CREDS))
    assert exc.value.backend_code == "ValueError"  # _map_error falls back to the exception type


def test_submit_maps_a_retryable_status_through():
    adapter = GoogleDocumentAIAdapter(client=_RecordingClient(exc=_grpc_error("UNAVAILABLE")))
    with pytest.raises(RetryableError) as exc:
        adapter.submit(_req(), RunContext(runtime=CREDS))
    assert exc.value.backend_code == "UNAVAILABLE"


@pytest.mark.parametrize(
    "exc,retryable,code",
    [
        (_grpc_error("PERMISSION_DENIED"), False, "auth_rejected"),
        (_grpc_error("UNAUTHENTICATED"), False, "auth_rejected"),
        (type("Unauthenticated", (Exception,), {})("bad ADC"), False, "auth_rejected"),
        (_grpc_error("RESOURCE_EXHAUSTED"), True, "RESOURCE_EXHAUSTED"),
        (_grpc_error("DEADLINE_EXCEEDED"), True, "DEADLINE_EXCEEDED"),
        (_grpc_error("INTERNAL"), True, "INTERNAL"),
        (_grpc_error("INVALID_ARGUMENT"), False, "INVALID_ARGUMENT"),
        (ValueError("malformed"), False, "ValueError"),
    ],
)
def test_map_error_classification(exc, retryable, code):
    mapped = GoogleDocumentAIAdapter()._map_error(exc)
    assert isinstance(mapped, RetryableError if retryable else TerminalError)
    assert mapped.backend_code == code


# ---- BL-155: credentials_path is redacted, not leaked, once auth_hinted wraps a failure --------


def test_bad_adc_path_is_redacted_once_auth_hinted_wraps_it():
    """The finding's own live (fully offline) repro: a bad/missing ADC path raises an error with
    the path embedded in its message, before any network activity — mirroring the shape of a real
    google.auth.exceptions.DefaultCredentialsError. credentials_path is now a CredentialField
    (BL-155), so the broker resolves it into ctx.credentials (not ctx.runtime) and auth_hinted's
    existing redaction (BL-37/BL-99) scrubs it out of the raised message the same as any other
    secret — where before this fix credentials_spec was empty, so secret_values had structurally
    nothing to redact and the path reached TerminalError.message untouched."""
    bad_path = "/Users/vic/.config/gcloud/sa-prod-key.json"
    env = {
        "GCP_PROJECT_ID": "p",
        "GCP_PROCESSOR_ID": "proc",
        "GCP_LOCATION": "us",
        "GOOGLE_APPLICATION_CREDENTIALS": bad_path,
    }
    descriptor = GoogleDocumentAIAdapter().descriptor
    req = _req()
    ctx = build_run_context(req, descriptor, broker=EnvCredentialBroker(env))
    # the point of the fix: the bad path resolves into the redaction-eligible bag, not ctx.runtime
    assert ctx.credentials is not None
    assert ctx.credentials.values["credentials_path"] == bad_path

    boom = Exception(
        f"File {bad_path} was not found. Google Application Default Credentials not found."
    )
    adapter = GoogleDocumentAIAdapter(client=_RecordingClient(exc=boom))

    with pytest.raises(TerminalError) as exc, auth_hinted(descriptor, credentials=ctx.credentials):
        adapter.submit(req, ctx)

    assert bad_path not in str(exc.value)
    assert "***" in str(exc.value)


# ---- degenerate protos normalize must survive ---------------------------------------------


def test_form_fields_become_typed_fields_and_nameless_ones_are_dropped():
    doc = {
        "text": "Borrower Name:\nJane Doe\n",
        "pages": [
            {
                "pageNumber": 1,
                "dimension": {"width": 612.0, "height": 792.0, "unit": "POINTS"},
                "formFields": [
                    {
                        "fieldName": {"textAnchor": {"textSegments": [{"endIndex": "14"}]}},
                        "fieldValue": {
                            "textAnchor": {
                                "textSegments": [{"startIndex": "15", "endIndex": "23"}]
                            },
                            "confidence": 0.94,
                        },
                    },
                    {  # OCR found a value but no key → nothing to name the field with
                        "fieldName": {"textAnchor": {"textSegments": []}},
                        "fieldValue": {
                            "textAnchor": {"textSegments": [{"endIndex": "14"}]},
                        },
                    },
                ],
            }
        ],
    }
    resp = _run_doc(doc, outputs={"typed_fields": True})
    assert set(resp.typed_fields) == {"Borrower Name"}  # trailing ":" stripped, nameless dropped
    field = resp.typed_fields["Borrower Name"]
    assert field.value == "Jane Doe" and field.confidence == pytest.approx(0.94)


def test_untyped_nested_property_is_skipped_and_a_third_repeat_appends():
    doc = {
        "text": "",
        "pages": [],
        "entities": [
            {
                "type": "invoice",
                "properties": [
                    {"mentionText": "orphan"},  # no `type` → nothing to key it by
                    {"type": "tax", "mentionText": "1.00"},
                    {"type": "tax", "mentionText": "2.00"},
                    {"type": "tax", "mentionText": "3.00"},
                ],
            }
        ],
    }
    resp = _run_doc(doc, outputs={"typed_fields": True})
    # the third repeat appends to the existing list instead of re-nesting it
    assert resp.typed_fields["invoice"].value == {"tax": ["1.00", "2.00", "3.00"]}


def test_table_without_any_text_anchor_falls_back_to_bbox_ordering():
    def _poly(y):
        return {
            "normalizedVertices": [
                {"x": 0.1, "y": y},
                {"x": 0.5, "y": y},
                {"x": 0.5, "y": y + 0.03},
                {"x": 0.1, "y": y + 0.03},
            ]
        }

    doc = {
        "text": "Alpha\n",
        "pages": [
            {
                "pageNumber": 1,
                "dimension": {"width": 612.0, "height": 792.0, "unit": "POINTS"},
                "paragraphs": [
                    {
                        "layout": {
                            "textAnchor": {"textSegments": [{"endIndex": "5"}]},
                            "boundingPoly": _poly(0.1),
                        }
                    }
                ],
                # a detected table with geometry but no textAnchor on the table OR its cells
                "tables": [
                    {
                        "layout": {"boundingPoly": _poly(0.5)},
                        "headerRows": [],
                        "bodyRows": [{"cells": [{}]}],
                    }
                ],
            }
        ],
    }
    resp = _run_doc(doc)
    blocks = resp.document.pages[0].blocks
    assert [b.type for b in blocks] == [BlockType.TEXT, BlockType.TABLE]  # ordered by bbox y
    table = blocks[1]
    assert table.text is None and table.table is not None  # no invented cell text
